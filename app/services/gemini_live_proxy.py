"""
Gemini Live API WebSocket proxy.

MİMARİ:
  Client WS ──► FastAPI ──► Gemini Live WS
  Her session için bire-bir bağlantı kurulur.
  Proxy katmanı ses byte'larını parse etmez, olduğu gibi iletir.

CLAUDE.md'den kritik kurallar:
  1. setup mesajı her şeyden ÖNCE gönderilir
  2. VAD: endOfSpeechSensitivity=END_SENSITIVITY_LOW, silenceDurationMs=300
  3. goAway gelince session_handle ile yeniden bağlan
  4. 20s sessizlikte keepalive gönder
  5. response.data zaten base64 → decode et, ham PCM gönder
"""
import asyncio
import base64
import json
import logging
import time
from typing import Any

import structlog
import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app.core.config import get_settings

logger = structlog.get_logger(__name__)
settings = get_settings()

# Gemini Live API WebSocket URL
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent"
    "?key={api_key}"
)

# Sistem promptları
TUTOR_PROMPT_PATH = "app/agent/prompts/tutor_system.txt"
PRONUNCIATION_PROMPT_PATH = "app/agent/prompts/pronunciation_assessment.txt"

KEEPALIVE_INTERVAL_S = 20   # Gemini ~30s'de idle bağlantıyı keser
SESSION_WARN_S = 9 * 60     # 9. dakikada client'a uyarı


def _load_prompt(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("prompt_not_found", path=path)
        return "You are a helpful German language tutor for Turkish-speaking learners."


def _build_setup_message(mode: str, profile_name: str, level: str) -> dict[str, Any]:
    """
    Gemini'ye gönderilecek ilk setup mesajı.
    CLAUDE.md: setup mesajı ses verisi gelmeden ÖNCE gönderilmeli.
    """
    prompt_path = PRONUNCIATION_PROMPT_PATH if mode == "pronunciation" else TUTOR_PROMPT_PATH
    system_instruction = _load_prompt(prompt_path)

    # Profil bilgisini sisteme ekle
    system_instruction += (
        f"\n\nKullanıcı profili: Ad={profile_name}, Seviye={level}. "
        "Her zaman Türkçe yanıt ver."
    )

    return {
        "setup": {
            "model": f"models/{settings.gemini_model_live}",
            "generation_config": {
                "response_modalities": ["AUDIO"],

            },
            "system_instruction": {
                "parts": [{"text": system_instruction}]
            },
            "realtime_input_config": {
                "automatic_activity_detection": {
                    "disabled": False,
                    # CLAUDE.md: Cümle ortasında kesilmeyi önler
                    "end_of_speech_sensitivity": "END_SENSITIVITY_LOW",
                    "silence_duration_ms": 300,
                }
            },
            # Session resumption: goAway gelince context korunur
            "session_resumption": {},
            # Transkript: debugging + SQLite özet için
            "input_audio_transcription": {},
            "output_audio_transcription": {},
        }
    }


async def run_proxy(
    client_ws: WebSocket,
    session_id: str,
    mode: str,
    profile_name: str,
    level: str,
) -> None:
    """
    İki yönlü proxy döngüsü.
    client_ws (SvelteKit browser) ↔ gemini_ws (Gemini Live API)
    """
    api_key = settings.gemini_api_key
    if not api_key:
        await client_ws.close(code=1011, reason="Sunucu yapılandırma hatası")
        return

    url = GEMINI_WS_URL.format(api_key=api_key)
    log = logger.bind(session_id=session_id, mode=mode)
    session_start = time.time()
    session_handle: str | None = None  # goAway sonrası resumption için

    try:
        async with websockets.connect(url, ping_interval=None) as gemini_ws:
            log.info("gemini_ws_connected")

            # ── KURAL 1: Setup mesajı ÖNCE gönderilir ──
            setup_msg = _build_setup_message(mode, profile_name, level)
            await gemini_ws.send(json.dumps(setup_msg))
            log.debug("setup_sent", mode=mode)

            # Setup ACK bekle (Gemini setupComplete döner)
            # Native-audio modeli binary formatında da yanıt verebilir
            raw_ack = await gemini_ws.recv()
            if isinstance(raw_ack, bytes):
                try:
                    ack = json.loads(raw_ack.decode('utf-8'))
                except Exception:
                    ack = {"setupComplete": True}  # binary setupComplete
            else:
                try:
                    ack = json.loads(raw_ack)
                except Exception:
                    ack = {}
            if "setupComplete" not in ack:
                log.warning("unexpected_setup_response", data=str(ack)[:200])
            else:
                log.info("setup_complete_received")
            # İstemciye setupComplete bildir
            try:
                await client_ws.send_text(json.dumps({"setupComplete": {}}))
            except Exception:
                pass

            async def client_to_gemini() -> None:
                """Client'tan gelen JSON mesajlarını Gemini'ye ilet."""
                try:
                    while True:
                        data = await client_ws.receive_text()
                        if settings.debug_ws:
                            log.debug("client→gemini", byte_count=len(data))
                        await gemini_ws.send(data)
                except WebSocketDisconnect:
                    log.info("client_disconnected")
                except Exception as exc:
                    log.error("client_to_gemini_error", error=str(exc))

            async def gemini_to_client() -> None:
                """
                Gemini'den gelen mesajları işle ve client'a ilet.

                CLAUDE.md'den keşfedilen kritik gerçekler (Nisan 2026):
                  - Tüm Gemini yanıtları bytes (binary) frame olarak gelir
                  - Her frame json.loads(bytes) ile decode edilebilir
                  - Ses verisi: serverContent.modelTurn.parts[].inlineData (base64)
                  - Output transkript: serverContent.outputTranscription.text
                  - Input transkript: serverContent.inputTranscription.text
                  - turnComplete: serverContent.turnComplete = true

                Strateji:
                  1. Ses içeren frame'lerde PCM'i binary olarak gönder
                  2. Aynı frame'de başka alan varsa (transkript, turnComplete),
                     ses'i sil → JSON olarak da gönder (ama 'thought' alanlarını filtrele)
                  3. Ses olmayan frame'leri olduğu gibi JSON olarak ilet
                """
                nonlocal session_handle
                try:
                    async for raw in gemini_ws:
                        if settings.debug_ws:
                            log.debug("gemini→client", byte_count=len(raw))

                        # Gemini her zaman bytes gönderir — JSON decode et
                        try:
                            data = raw if isinstance(raw, bytes) else raw.encode('utf-8')
                            msg = json.loads(data)
                        except Exception:
                            # Gerçek binary (ses dışı) — yoksay
                            log.debug("non_json_binary_skipped", size=len(raw))
                            continue

                        # GoAway: Gemini bağlantıyı kapatmak üzere
                        if "goAway" in msg:
                            log.warning("go_away_received", time_left_s=msg["goAway"].get("timeLeft"))
                            await client_ws.send_text(json.dumps({"type": "goAway"}))
                            continue

                        # Session resumption handle güncelle
                        if "sessionResumptionUpdate" in msg:
                            update = msg["sessionResumptionUpdate"]
                            if update.get("resumable") and update.get("newHandle"):
                                session_handle = update["newHandle"]
                                log.debug("session_handle_updated")
                            continue

                        try:
                            server_content = msg.get("serverContent", {})

                            # ── Ses verisi: base64 → PCM binary frame ──
                            parts = server_content.get("modelTurn", {}).get("parts", [])
                            has_audio = False
                            for part in parts:
                                if "inlineData" in part:
                                    inline = part["inlineData"]
                                    if inline.get("mimeType", "").startswith("audio/pcm"):
                                        raw_pcm = base64.b64decode(inline["data"])
                                        await client_ws.send_bytes(raw_pcm)
                                        has_audio = True

                            # ── JSON mesajı client'a ilet (ses frame'leri hariç) ──
                            # Transkript, turnComplete, generationComplete vb. içeren mesajlar
                            # Thought (düşünce zinciri) mesajlarını filtrele — gereksiz
                            is_thought_only = (
                                parts
                                and all(
                                    part.get("thought", False) and "inlineData" not in part
                                    for part in parts
                                )
                            )

                            has_transcript = (
                                "outputTranscription" in server_content
                                or "inputTranscription" in server_content
                            )
                            has_control = (
                                server_content.get("turnComplete")
                                or server_content.get("generationComplete")
                                or server_content.get("interrupted")
                            )

                            if "serverContent" in msg and not is_thought_only:
                                # Ses binary olarak gönderildiyse inlineData'yı JSON'dan sil
                                if has_audio:
                                    clean_parts = [
                                        p for p in parts
                                        if "inlineData" not in p
                                    ]
                                    if clean_parts or has_transcript or has_control:
                                        # inlineData'sız temiz mesajı gönder
                                        clean_msg = dict(msg)
                                        clean_sc = dict(server_content)
                                        if "modelTurn" in clean_sc:
                                            if clean_parts:
                                                clean_sc["modelTurn"] = {"parts": clean_parts}
                                            else:
                                                del clean_sc["modelTurn"]
                                        clean_msg["serverContent"] = clean_sc
                                        await client_ws.send_text(json.dumps(clean_msg))
                                else:
                                    # Ses olmayan — olduğu gibi gönder
                                    await client_ws.send_text(json.dumps(msg))

                            elif "toolCall" in msg:
                                await client_ws.send_text(json.dumps(msg))

                            if has_transcript or has_control:
                                log.debug(
                                    "transcript_or_control_forwarded",
                                    has_output_transcript=has_transcript,
                                    turn_complete=server_content.get("turnComplete"),
                                )

                        except Exception as exc:
                            log.error("message_parse_error", error=str(exc))

                except websockets.exceptions.ConnectionClosed:
                    log.info("gemini_ws_closed")
                except Exception as exc:
                    log.error("gemini_to_client_error", error=str(exc))

            async def keepalive_sender() -> None:
                """
                CLAUDE.md: Gemini ~30s sessizlikte bağlantıyı keser.
                20s'de bir keepalive gönder.
                """
                try:
                    while True:
                        await asyncio.sleep(KEEPALIVE_INTERVAL_S)
                        elapsed = time.time() - session_start
                        if elapsed >= SESSION_WARN_S:
                            await client_ws.send_text(json.dumps({
                                "type": "sessionWarning",
                                "message": "Oturum 1 dakika içinde sona erecek."
                            }))
                        keepalive = {
                            "clientContent": {
                                "turnComplete": False
                            }
                        }
                        await gemini_ws.send(json.dumps(keepalive))
                        log.debug("keepalive_sent", elapsed_s=round(elapsed))
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    log.debug("keepalive_stopped", reason=str(exc))

            # Üç görevi eş zamanlı çalıştır
            tasks = await asyncio.gather(
                client_to_gemini(),
                gemini_to_client(),
                keepalive_sender(),
                return_exceptions=True,
            )

            for task_result in tasks:
                if isinstance(task_result, Exception):
                    log.error("proxy_task_exception", error=str(task_result))

    except websockets.exceptions.InvalidURI:
        log.error("invalid_gemini_uri")
        await client_ws.close(code=1011, reason="Bağlantı adresi geçersiz")
    except Exception as exc:
        log.error("proxy_fatal_error", error=str(exc))
        try:
            await client_ws.close(code=1011, reason="Sunucu hatası")
        except Exception:
            pass
