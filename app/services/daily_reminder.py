"""
Günlük ders hatırlatma servisi.

İki mod:
  1. Sabit saat: Her gün REMINDER_HOUR_UTC saatinde tüm profillere gönderir.
  2. Snooze: Kullanıcı "sonra yapalım" deyince bot saat sorar, o saatte hatırlatır.
     reminder_snoozed_until = "HH:MM" (UTC) — o dakikada tek seferlik tetiklenir.

Mimari:
  - main.py startup'ta asyncio.create_task() ile arka planda çalışır
  - Her dakika snooze kontrolü, her saat de günlük kontrol yapar
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

# Varsayılan günlük hatırlatma saati (UTC). 09:00 UTC = 12:00 TR
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


def _reminder_blocks(name: str, due_count: int) -> tuple[list, str]:
    """Hatırlatma mesajı blokları ve fallback metin."""
    text_msg = f"Ders vakti, {name}! Almanca seni bekliyor."
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Hey *{name}*, ders vakti! 📚\n\n"
                    + (f"*{due_count}* tekrar kelimen var. " if due_count > 0 else "")
                    + "Başlamak için mesaj yaz veya `ders başlat` de."
                ),
            },
        },
    ]
    return blocks, text_msg


async def _send_reminder_to_profile(
    db,
    profile_id: str,
    name: str,
    level: str,
    slack_user_id: str,
    mark_daily: bool = False,
) -> None:
    """Tek bir profile hatırlatma gönderir."""
    due_result = await db.execute(text("""
        SELECT COUNT(*) FROM fsrs_cards
        WHERE profile_id = :pid
          AND due <= datetime('now')
          AND state != 'new'
    """), {"pid": profile_id})
    due_count = due_result.scalar() or 0

    blocks, text_msg = _reminder_blocks(name, due_count)
    await _slack_dm(slack_user_id, text_msg=text_msg, blocks=blocks)

    if mark_daily:
        today = date.today().isoformat()
        await db.execute(text(
            "UPDATE profiles SET last_reminder_date = :today WHERE id = :pid"
        ), {"today": today, "pid": profile_id})

    # snooze temizle
    await db.execute(text(
        "UPDATE profiles SET reminder_snoozed_until = NULL WHERE id = :pid"
    ), {"pid": profile_id})
    await db.commit()
    logger.info("reminder_sent profile_id=%s snooze=%s", profile_id, not mark_daily)


async def _check_snooze_reminders() -> None:
    """Her dakika çağrılır. Vakti gelen snooze hatırlatmalarını gönderir."""
    now_utc = datetime.now(timezone.utc).strftime("%H:%M")
    async with AsyncSessionLocal() as db:
        result = await db.execute(text("""
            SELECT id, name, level, slack_user_id
            FROM profiles
            WHERE slack_user_id IS NOT NULL
              AND reminder_snoozed_until = :now
        """), {"now": now_utc})
        profiles = result.fetchall()
        for profile_id, name, level, slack_user_id in profiles:
            await _send_reminder_to_profile(db, profile_id, name, level, slack_user_id)


async def _send_daily_reminders() -> int:
    """
    Günlük sabit saatte bugün henüz hatırlatma gönderilmemiş profillere gönderir.
    snooze ayarlı profilleri atlar (onlar kendi saatlerinde gelecek).
    """
    today = date.today().isoformat()
    sent = 0
    async with AsyncSessionLocal() as db:
        result = await db.execute(text("""
            SELECT id, name, level, slack_user_id
            FROM profiles
            WHERE slack_user_id IS NOT NULL
              AND slack_user_id != ''
              AND (last_reminder_date IS NULL OR last_reminder_date != :today)
              AND (reminder_snoozed_until IS NULL OR reminder_snoozed_until = '')
        """), {"today": today})
        profiles = result.fetchall()
        for profile_id, name, level, slack_user_id in profiles:
            await _send_reminder_to_profile(
                db, profile_id, name, level, slack_user_id, mark_daily=True
            )
            sent += 1
    return sent


async def reminder_loop() -> None:
    """
    Sürekli çalışan arka plan döngüsü.
    - Her dakika: snooze kontrolü
    - Her saat başı: günlük hatırlatma kontrolü
    """
    logger.info("daily_reminder_loop_started hour_utc=%d", REMINDER_HOUR_UTC)
    last_daily_hour = -1
    while True:
        try:
            now = datetime.now(timezone.utc)

            # Snooze kontrolü — her dakika
            await _check_snooze_reminders()

            # Günlük hatırlatma — saat başı, aynı saatte bir kez
            if now.hour == REMINDER_HOUR_UTC and last_daily_hour != now.hour:
                sent = await _send_daily_reminders()
                last_daily_hour = now.hour
                logger.info("daily_reminders_sent count=%d", sent)

            await asyncio.sleep(60)  # her dakika kontrol et
        except asyncio.CancelledError:
            logger.info("daily_reminder_loop_stopped")
            break
        except Exception as exc:
            logger.error("reminder_loop_error err=%s", exc)
            await asyncio.sleep(60)
