"""
Günlük ders hatırlatma servisi.

Her gün belirlenen saatte (varsayılan: 09:00 UTC = 12:00 TR) tüm aktif
profillere Slack üzerinden hatırlatma mesajı gönderir.

Mimari:
  - main.py startup'ta asyncio.create_task() ile arka planda çalışır
  - Her saat başı "gönderilecek mi?" kontrolü yapar
  - Aynı gün iki kez gönderimi önlemek için DB'de last_reminder_date tutulur
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone, date

import httpx
from sqlalchemy import text

from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_API = "https://slack.com/api"

# Hatırlatma saati (UTC). Türkiye UTC+3 → 09:00 UTC = 12:00 TR
REMINDER_HOUR_UTC = int(os.getenv("REMINDER_HOUR_UTC", "9"))


async def _slack_dm(user_id: str, text_msg: str, blocks: list | None = None) -> None:
    """Kullanıcıya Slack DM gönder."""
    if not SLACK_BOT_TOKEN:
        return
    payload: dict = {"channel": user_id}
    if text_msg:
        payload["text"] = text_msg
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
            logger.error("reminder_slack_error user=%s err=%s", user_id, data.get("error"))


async def _send_reminders() -> int:
    """
    Slack bağlı tüm profillere bugün henüz hatırlatma gönderilmediyse gönderir.
    Döner: gönderilen mesaj sayısı.
    """
    today = date.today().isoformat()
    sent = 0

    async with AsyncSessionLocal() as db:
        # Slack bağlı, bugün hatırlatma almamış profiller
        result = await db.execute(text("""
            SELECT id, name, level, slack_user_id
            FROM profiles
            WHERE slack_user_id IS NOT NULL
              AND slack_user_id != ''
              AND (last_reminder_date IS NULL OR last_reminder_date != :today)
        """), {"today": today})
        profiles = result.fetchall()

        for profile_id, name, level, slack_user_id in profiles:
            # FSRS: bugün tekrar edilecek kart sayısını çek
            due_result = await db.execute(text("""
                SELECT COUNT(*) FROM fsrs_cards
                WHERE profile_id = :pid
                  AND due <= datetime('now')
                  AND state != 'new'
            """), {"pid": profile_id})
            due_count = due_result.scalar() or 0

            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"Guten Morgen, *{name}*! ☀️\n\n"
                            f"Bugün Almanca dersin seni bekliyor. "
                            + (f"*{due_count}* tekrar kelimen var. " if due_count > 0 else "")
                            + "Hazır mısın?"
                        ),
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "📚 Derse Başla"},
                            "style": "primary",
                            "action_id": "start_lesson_reminder",
                        }
                    ],
                },
            ]

            await _slack_dm(
                slack_user_id,
                text_msg=f"Guten Morgen {name}! Bugün Almanca dersin seni bekliyor.",
                blocks=blocks,
            )

            # last_reminder_date güncelle
            await db.execute(text("""
                UPDATE profiles SET last_reminder_date = :today WHERE id = :pid
            """), {"today": today, "pid": profile_id})
            await db.commit()
            sent += 1
            logger.info("reminder_sent profile_id=%s slack=%s", profile_id, slack_user_id)

    return sent


async def reminder_loop() -> None:
    """
    Sürekli çalışan arka plan döngüsü.
    Her saat başı kontrol eder, belirlenen saatte hatırlatma gönderir.
    """
    logger.info("daily_reminder_loop_started hour_utc=%d", REMINDER_HOUR_UTC)
    while True:
        try:
            now = datetime.now(timezone.utc)
            if now.hour == REMINDER_HOUR_UTC and now.minute < 5:
                sent = await _send_reminders()
                logger.info("daily_reminders_sent count=%d", sent)
            # Bir sonraki kontrol: 55 dakika sonra (aynı saat içinde çift tetiklenmeyi önler)
            await asyncio.sleep(55 * 60)
        except asyncio.CancelledError:
            logger.info("daily_reminder_loop_stopped")
            break
        except Exception as exc:
            logger.error("reminder_loop_error err=%s", exc)
            await asyncio.sleep(60)  # hata sonrası 1 dk bekle, sonra devam et
