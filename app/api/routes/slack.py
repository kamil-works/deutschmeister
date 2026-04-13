"""
Slack Bot entegrasyonu — DeutschMeister

Mimari kural: Bu dosya aptal bir UI katmanıdır.
  - İş mantığı YOKTUR.
  - Kullanıcıdan mesaj alır → backend endpoint'lerine iletir → Slack'e yazar.
  - Tek bilen şey: slack_user_id ↔ profile_id mapping.

Gerekli ortam değişkenleri:
  SLACK_BOT_TOKEN      → xoxb-... (Bot User OAuth Token)
  SLACK_SIGNING_SECRET → Slack App ayarlarından

Slack App kurulumu:
  1. https://api.slack.com/apps → "Create New App"
  2. "Event Subscriptions" → Request URL: https://<your-domain>/slack/events
  3. Subscribe to bot events: message.channels, message.im
  4. "Interactivity & Shortcuts" → Request URL: https://<your-domain>/slack/actions
  5. "OAuth & Permissions" → Bot Token Scopes:
       channels:history, channels:read, chat:write, im:history, im:read, im:write
  6. Install to workspace → SLACK_BOT_TOKEN al
"""

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import text

from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()

SLACK_BOT_TOKEN      = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_API            = "https://slack.com/api"

STATIC_DIR = Path(__file__).parent.parent.parent / "static"


# ─────────────────────────────────────────────────────────────────────────────
# Yardımcı: Slack imza doğrulama
# ─────────────────────────────────────────────────────────────────────────────

def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """HMAC-SHA256 ile Slack request'ini doğrula."""
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET tanımlı değil — doğrulama atlandı")
        return True  # Geliştirme ortamı

    # 5 dakikadan eski istekleri reddet (replay attack)
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
    except (ValueError, TypeError):
        return False

    base = f"v0:{timestamp}:{body.decode()}"
    expected = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(),
        base.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


# ─────────────────────────────────────────────────────────────────────────────
# Yardımcı: Slack'e mesaj gönder
# ─────────────────────────────────────────────────────────────────────────────

async def _slack_post(channel: str, text: str = None, blocks: list = None):
    """Slack chat.postMessage çağrısı."""
    if not SLACK_BOT_TOKEN:
        logger.warning("SLACK_BOT_TOKEN tanımlı değil — mesaj gönderilemedi")
        return

    payload = {"channel": channel}
    if text:
        payload["text"] = text
    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{SLACK_API}/chat.postMessage",
            headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
            json=payload,
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error("Slack postMessage hatası: %s", data.get("error"))


# ─────────────────────────────────────────────────────────────────────────────
# Yardımcı: slack_user_id → profile_id
# ─────────────────────────────────────────────────────────────────────────────

async def _get_profile_id(slack_user_id: str) -> str | None:
    """profiles tablosundan slack_user_id ile profile_id bul."""
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            text("SELECT id FROM profiles WHERE slack_user_id = :uid"),
            {"uid": slack_user_id},
        )
        result = row.fetchone()
        return result[0] if result else None


# ─────────────────────────────────────────────────────────────────────────────
# GET /session/{session_id}  →  session.html serve et
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/session/{session_id}", response_class=HTMLResponse)
async def serve_session(session_id: str):
    """
    Slack'ten gelen link bu endpoint'e gelir.
    session.html'i döndürür; JS tarafı ?id= parametresini okur.
    """
    html_path = STATIC_DIR / "session.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="session.html bulunamadı")

    # HTML içindeki audio-processor.js yolunu kontrol etmek için
    # session_id'yi query param olarak redirect et
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/static/session.html?id={session_id}")


# ─────────────────────────────────────────────────────────────────────────────
# POST /slack/events  →  Slack Events API webhook
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """
    Slack Events API webhook.
    Desteklenen event'ler:
      - url_verification  → challenge döndür (Slack kurulum doğrulama)
      - message           → kullanıcı mesajı → /api/chat'e ilet → Slack'e yaz
    """
    body = await request.body()

    # İmza doğrula
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=403, detail="Geçersiz Slack imzası")

    payload = json.loads(body)
    event_type = payload.get("type")

    # ── Slack kurulum doğrulaması ──────────────────────────────────────────
    if event_type == "url_verification":
        return {"challenge": payload.get("challenge")}

    # ── Mesaj event'i ──────────────────────────────────────────────────────
    if event_type == "event_callback":
        event = payload.get("event", {})
        if event.get("type") == "message" and not event.get("bot_id"):
            # Bot kendi mesajlarını işleme
            background_tasks.add_task(_handle_message_event, event)

    # Slack 3 saniye içinde 200 bekler — işlemi arka plana al
    return Response(status_code=200)


async def _handle_message_event(event: dict):
    """
    Kullanıcı mesajını /api/chat endpoint'ine iletir,
    yanıtı Slack'e gönderir.
    """
    slack_user_id = event.get("user")
    channel       = event.get("channel")
    text_msg      = event.get("text", "").strip()

    if not slack_user_id or not text_msg:
        return

    # Özel komutları yakala
    lower = text_msg.lower()
    if lower in ("/ders", "/ders başlat", "ders başlat", "ses dersi"):
        await _handle_voice_command(slack_user_id, channel)
        return

    # Profile bul
    profile_id = await _get_profile_id(slack_user_id)
    if not profile_id:
        await _slack_post(
            channel,
            text=(
                "Henüz bir profilin yok. "
                "Lütfen /yeni-profil <isim> <yaş> <seviye> formatıyla profil oluştur.\n"
                "Seviye: A1, A2, B1 veya B2"
            ),
        )
        return

    # Chat logic'i direkt çağır
    try:
        from app.api.routes.chat import _chat_logic
        reply = await _chat_logic(profile_id=profile_id, message=text_msg)
        await _slack_post(channel, text=reply)
    except Exception as e:
        logger.error("chat_error", profile_id=profile_id, error=str(e))
        await _slack_post(channel, text="Öğretmen şu an yanıt veremiyor, biraz sonra tekrar dene.")


async def _handle_voice_command(slack_user_id: str, channel: str):
    """
    Kullanıcı ses dersi istedi → session oluştur → link gönder.
    """
    profile_id = await _get_profile_id(slack_user_id)
    if not profile_id:
        await _slack_post(channel, text="Önce bir profil oluşturman gerekiyor.")
        return

    try:
        from app.core.database import AsyncSessionLocal
        from app.models.db import Session as SessionModel
        import uuid, datetime

        async with AsyncSessionLocal() as db:
            session = SessionModel(
                id=str(uuid.uuid4()),
                profile_id=profile_id,
                mode="conversation",
                started_at=datetime.datetime.utcnow(),
            )
            db.add(session)
            await db.commit()
            await db.refresh(session)
            session_id = session.id

        base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
        session_url = f"{base_url}/session/{session_id}"

        await _slack_post(
            channel,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Sesli Almanca dersin hazır!* 🎙️\n\nAşağıdaki butona tıklayarak dersi başlatabilirsin.",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "🎙️ Dersi Başlat"},
                            "style": "primary",
                            "url": session_url,
                            "action_id": "start_voice_session",
                        }
                    ],
                },
            ],
        )
    except Exception as e:
        logger.error("Ses seansı oluşturma hatası: %s", e)
        await _slack_post(channel, text="Ses dersi başlatılamadı. Backend'i kontrol et.")


# ─────────────────────────────────────────────────────────────────────────────
# POST /slack/actions  →  Block Kit buton/modal etkileşimleri
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/slack/actions")
async def slack_actions(request: Request):
    """
    Block Kit buton tıklamaları buraya gelir.
    Şu an sadece 200 döner — buton URL'i direkt açtığı için
    sunucu tarafında işlem gerekmez.
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=403, detail="Geçersiz Slack imzası")

    return Response(status_code=200)


# ─────────────────────────────────────────────────────────────────────────────
# POST /slack/commands  →  Slash komutları (opsiyonel)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/slack/commands")
async def slack_commands(request: Request, background_tasks: BackgroundTasks):
    """
    /ders slash komutu → ses dersi başlat.

    Slack App ayarlarında:
      Slash Commands → /ders → Request URL: https://<domain>/slack/commands
    """
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")
    if not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=403, detail="Geçersiz Slack imzası")

    from urllib.parse import parse_qs
    params        = parse_qs(body.decode())
    slack_user_id = params.get("user_id", [""])[0]
    channel_id    = params.get("channel_id", [""])[0]

    background_tasks.add_task(_handle_voice_command, slack_user_id, channel_id)

    # Slack 3 saniye içinde immediate response bekler
    return {"response_type": "ephemeral", "text": "Ders hazırlanıyor…"}
