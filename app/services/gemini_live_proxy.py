"""
Gemini Live API WebSocket proxy — google-genai SDK tabanlı.

Önceki sürümdeki sorunlar:
  - websockets kütüphanesiyle ham bağlantı → API değişince kırılıyor
  - v1alpha endpoint deprecated
  - snake_case field'lar (JSON proto camelCase bekliyor)
  - CurriculumEngine/FSRSEngine hiç çağrılmıyordu
  - Tool calling yoktu → artikeller hallüsinasyon

Bu sürümde:
  - google-genai SDK kullanılıyor (URL/versiyon sorunları SDK'nın sorunu)
  - CurriculumEngine session başında çağrılıyor → plan oluşturuluyor
  - Tool calling eklendi → Gemini kelime üretemez, DB'den çeker
  - Her tool call loglanıyor → test sırasında görünür
"""
from __future__ import annotations

import asyncio
import base64
import json

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from google import genai
from google.genai import types
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.services.curriculum_engine import CurriculumEngine
from app.services.fsrs_engine import FSRSEngine
from app.services.session_analyzer import SessionAnalyzer
from app.services.tool_handlers import build_tools, dispatch_tool

logger = structlog.get_logger(__name__)
settings = get_settings()

TUTOR_PROMPT_PATH        = "app/agent/prompts/tutor_system.txt"
PRONUNCIATION_PROMPT_PATH = "app/agent/prompts/pronunciation_assessment.txt"


def _load_prompt(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("prompt_not_found", path=path)
        return "You are a helpful German language tutor for Turkish-speaking learners."


def _build_plan_injection(plan) -> str:
    """SessionPlan'dan Gemini sistem promptuna eklenecek metin bloğu."""
    if plan is None:
        return ""
    lines = [
        "\n\n[OTURUM PLANI]",
        f"Hedef seviye: {getattr(plan, 'target_level', '')}",
        f"Konu: {getattr(plan, 'focus_topic_tr', '')}",
        f"Gramer odağı: {getattr(plan, 'grammar_focus', '')}",
        f"Kaygı sinyali: {getattr(plan, 'anxiety_signal', 'low')}",
    ]
    vocab = getattr(plan, "vocabulary", []) or []
    if vocab:
        lines.append("Yeni kelimeler (DB'den doğrulanmış):")
        for w in vocab[:10]:
            article = w.get("article", "")
            word    = w.get("word", "")
            tr      = w.get("translation_tr", "")
            lines.append(f"  - {article} {word} → {tr}")
    review = getattr(plan, "review_words", []) or []
    if review:
        lines.append("Tekrar kelimeleri:")
        for w in review[:5]:
            lines.append(f"  - {w.get('article','')} {w.get('word','')}")
    motivation = getattr(plan, "motivation_message", "")
    if motivation:
        lines.append(f"Motivasyon: {motivation}")
    return "\n".join(lines)


async def run_proxy(
    client_ws: WebSocket,
    session_id: str,
    mode: str,
    profile_name: str,
    level: str,
    profile_id: str,
    db: AsyncSession,
) -> None:
    """
    Bidirectional proxy: tarayıcı ↔ FastAPI ↔ Gemini Live API

    Yeni: CurriculumEngine session başında çağrılıyor + tool calling aktif.
    """
    log = logger.bind(session_id=session_id, mode=mode)

    api_key = settings.gemini_api_key
    if not api_key:
        await client_ws.close(code=1011, reason="GEMINI_API_KEY ayarlanmamış")
        return

    # ── 1. CurriculumEngine → SessionPlan ─────────────────────────────────
    plan = None
    try:
        fsrs_engine  = FSRSEngine(db)
        curriculum   = CurriculumEngine(db)
        analyzer     = SessionAnalyzer(db)
        last_insight = await analyzer.get_last_insight(profile_id)
        fsrs_stats   = await fsrs_engine.get_stats(profile_id)
        plan = await curriculum.get_session_plan(
            profile_id=profile_id,
            fsrs_stats=fsrs_stats,
            last_session_insight=last_insight,
        )
        log.info("curriculum_plan_generated", level=getattr(plan, "target_level", ""))
    except Exception as exc:
        log.warning("curriculum_plan_failed", error=str(exc))

    # ── 2. Sistem prompt + plan injection ─────────────────────────────────
    prompt_path    = PRONUNCIATION_PROMPT_PATH if mode == "pronunciation" else TUTOR_PROMPT_PATH
    system_prompt  = _load_prompt(prompt_path)
    system_prompt += f"\n\nKullanıcı: {profile_name}, Seviye: {level}"
    system_prompt += _build_plan_injection(plan)

    # ── 3. Gemini Live bağlantısı ──────────────────────────────────────────
    try:
        gemini_client = genai.Client(api_key=api_key)

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=system_prompt,
            tools=build_tools(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Aoede"
                    )
                )
            ),
        )

        async with gemini_client.aio.live.connect(
            model=settings.gemini_model_live,
            config=live_config,
        ) as gemini_session:
            log.info("gemini_live_connected")

            # Client'a bağlantı hazır sinyali ver
            try:
                await client_ws.send_text(json.dumps({"setupComplete": {}}))
            except Exception:
                pass

            await asyncio.gather(
                _client_to_gemini(client_ws, gemini_session, log),
                _gemini_to_client(client_ws, gemini_session, db, profile_id, log),
            )

    except WebSocketDisconnect:
        log.info("ws_client_disconnected")
    except Exception as exc:
        log.error("proxy_fatal_error", error=str(exc))
        try:
            await client_ws.close(code=1011, reason="Sunucu hatası")
        except Exception:
            pass


async def _client_to_gemini(
    client_ws: WebSocket,
    gemini_session,
    log,
) -> None:
    """
    Tarayıcıdan gelen ses chunk'larını Gemini'ye ilet.
    Client format: {"realtime_input": {"media_chunks": [{"mime_type": "audio/pcm", "data": b64}]}}
    """
    try:
        while True:
            raw = await client_ws.receive_text()
            msg = json.loads(raw)

            chunks = (
                msg.get("realtime_input", {}).get("media_chunks", [])
                or msg.get("realtimeInput", {}).get("mediaChunks", [])
            )
            for chunk in chunks:
                pcm_bytes = base64.b64decode(chunk.get("data", ""))
                if not pcm_bytes:
                    continue
                await gemini_session.send(
                    input=types.LiveClientRealtimeInput(
                        media_chunks=[
                            types.Blob(
                                mime_type="audio/pcm;rate=16000",
                                data=pcm_bytes,
                            )
                        ]
                    )
                )
    except WebSocketDisconnect:
        log.info("client_disconnected_audio")
    except Exception as exc:
        log.error("client_to_gemini_error", error=str(exc))


async def _gemini_to_client(
    client_ws: WebSocket,
    gemini_session,
    db: AsyncSession,
    profile_id: str,
    log,
) -> None:
    """
    Gemini'den gelen yanıtları işle:
      - Ses verisi → binary frame olarak client'a gönder
      - Tool call → handler çalıştır, sonucu Gemini'ye gönder (logla)
      - turnComplete → client'a bildir
    """
    try:
        async for response in gemini_session:
            # ── Ses ──
            if response.data:
                await client_ws.send_bytes(response.data)

            # ── Tool call ──
            if response.tool_call:
                fn_responses = []
                for fc in response.tool_call.function_calls:
                    result = await dispatch_tool(
                        name=fc.name,
                        args=dict(fc.args),
                        db=db,
                        profile_id=profile_id,
                    )
                    log.info("voice_tool_call", name=fc.name, result_keys=list(result.keys()))
                    fn_responses.append(
                        types.FunctionResponse(
                            id=fc.id,
                            name=fc.name,
                            response=result,
                        )
                    )
                await gemini_session.send(
                    input=types.LiveClientToolResponse(
                        function_responses=fn_responses,
                    )
                )

            # ── Turn complete ──
            sc = getattr(response, "server_content", None)
            if sc and getattr(sc, "turn_complete", False):
                try:
                    await client_ws.send_text(
                        json.dumps({"serverContent": {"turnComplete": True}})
                    )
                except Exception:
                    pass

            # ── Interrupted ──
            if sc and getattr(sc, "interrupted", False):
                try:
                    await client_ws.send_text(
                        json.dumps({"serverContent": {"interrupted": True}})
                    )
                except Exception:
                    pass

    except Exception as exc:
        log.error("gemini_to_client_error", error=str(exc))
