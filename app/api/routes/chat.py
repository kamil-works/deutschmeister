"""
Text-to-text chat endpoint.
Gemini REST API ile metin tabanlı Almanca ders.

Session lifecycle:
  - İlk mesajda sessions tablosuna kayıt açılır (aktif session ± 8 saat)
  - SessionPlan bir kez üretilip sessions.plan_json'a yazılır (sticky plan)
  - Sonraki mesajlarda aynı plan kullanılır (konu ortada değişmez)
  - Veda tespitinde session kapatılır + SessionAnalyzer arka planda tetiklenir
"""
import asyncio
import dataclasses
import json
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from google import genai
from google.genai import types

from app.core.config import get_settings
from app.core.database import get_db
from app.models.db import ChatMessage, Profile, PhonemeScore
from app.services.curriculum_engine import CurriculumEngine, SessionPlan
from app.services.fsrs_engine import FSRSEngine
from app.services.session_analyzer import SessionAnalyzer
from app.schemas.structured_outputs import (
    ChatMessageIn,
    ChatMessageOut,
    ChatResponse,
)

logger = structlog.get_logger(__name__)
settings = get_settings()
router = APIRouter(prefix="/api/chat", tags=["chat"])

SYSTEM_PROMPT_PATH = "app/agent/prompts/chat_tutor_system.txt"

# Veda tespiti için anahtar kelimeler (küçük harf, noktalama işaretleri olmadan)
_FAREWELL_KEYWORDS = {
    "tschüss", "tschuss", "auf wiedersehen", "auf wiedersehn",
    "güle güle", "görüşürüz", "hoşça kal", "bye", "ciao",
    "görüşmek üzere", "görüşürüz", "iyi günler", "iyi geceler",
}


# ── Yardımcı fonksiyonlar ──────────────────────────────────────────────────

def _load_system_prompt() -> str:
    try:
        with open(SYSTEM_PROMPT_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "You are a helpful German language tutor for Turkish-speaking learners."


def _is_farewell(message: str) -> bool:
    """Kullanıcının mesajı bir veda mı?"""
    lower = message.lower().strip().rstrip("!.,? ")
    return lower in _FAREWELL_KEYWORDS or any(kw in lower for kw in _FAREWELL_KEYWORDS)


def _build_context(
    profile: Profile,
    phoneme_scores: list[dict],
    session_plan_injection: str | None = None,
) -> str:
    """Profil + fonem ilerleme + AI beyin planı bilgisini system prompt'a ekle."""
    parts = []

    # AI Beyin oturum planı — varsa EN BAŞA ekle (en yüksek öncelik)
    if session_plan_injection:
        parts.append(session_plan_injection)
        parts.append("")

    parts += [
        "## KULLANICI PROFİLİ",
        f"- Ad: {profile.name}",
        f"- Yaş: {profile.age or 'belirtilmemiş'}",
        f"- Seviye: {profile.level}",
    ]

    if phoneme_scores:
        parts.append("\n## TELAFFUZ İLERLEMESİ (güncel skorlar)")
        for ps in phoneme_scores:
            parts.append(f"- {ps['phoneme']}: ortalama {ps['avg']:.0f}/100 ({ps['count']} ölçüm)")

    return "\n".join(parts)


async def _get_phoneme_summary(db: AsyncSession, profile_id: str) -> list[dict]:
    """Profilin fonem skorlarını özetle."""
    from sqlalchemy import func
    result = await db.execute(
        select(
            PhonemeScore.phoneme,
            func.avg(PhonemeScore.score).label("avg"),
            func.count().label("count"),
        )
        .where(PhonemeScore.profile_id == profile_id)
        .group_by(PhonemeScore.phoneme)
    )
    return [{"phoneme": r.phoneme, "avg": r.avg, "count": r.count} for r in result.all()]


# ── Session lifecycle yardımcıları ─────────────────────────────────────────

async def _get_or_create_active_session(db: AsyncSession, profile_id: str) -> tuple[str, str | None, bool]:
    """
    Açık bir session varsa döner; yoksa yeni oluşturur.
    Returns: (session_id, plan_json | None, is_new_session)

    Aktif session tanımı: ended_at IS NULL AND son 8 saat içinde başlamış.
    8 saat: Kullanıcı sabah ders yapar, öğleden sonra devam edebilir.
    """
    result = await db.execute(text("""
        SELECT id, plan_json
        FROM sessions
        WHERE profile_id = :pid
          AND ended_at IS NULL
          AND started_at >= datetime('now', '-8 hours')
        ORDER BY started_at DESC
        LIMIT 1
    """), {"pid": profile_id})
    row = result.fetchone()
    if row:
        return row[0], row[1], False  # mevcut session

    # Yeni session aç
    session_id = str(uuid.uuid4())
    await db.execute(text("""
        INSERT INTO sessions (id, profile_id, mode, started_at)
        VALUES (:sid, :pid, 'conversation', datetime('now'))
    """), {"sid": session_id, "pid": profile_id})
    await db.flush()
    logger.info("session_created", profile_id=profile_id, session_id=session_id)
    return session_id, None, True


async def _store_plan(db: AsyncSession, session_id: str, plan: SessionPlan) -> None:
    """SessionPlan'ı JSON olarak session'a yazar (sticky plan)."""
    plan_dict = dataclasses.asdict(plan)
    await db.execute(text("""
        UPDATE sessions SET plan_json = :pj WHERE id = :sid
    """), {"pj": json.dumps(plan_dict, ensure_ascii=False), "sid": session_id})


def _restore_plan(plan_json: str) -> SessionPlan | None:
    """JSON string'den SessionPlan dataclass'ını geri oluşturur."""
    try:
        return SessionPlan(**json.loads(plan_json))
    except Exception:
        return None


async def _close_session_background(profile_id: str, session_id: str) -> None:
    """
    Session'ı kapatır ve SessionAnalyzer'ı arka planda tetikler.
    Ayrı bir DB session kullanır — caller'ın session'ından bağımsız.
    """
    from app.core.database import AsyncSessionLocal
    from app.api.routes.vocabulary import _run_analysis_background

    async with AsyncSessionLocal() as db:
        await db.execute(text("""
            UPDATE sessions
            SET ended_at   = datetime('now'),
                duration_s = CAST(
                    (julianday('now') - julianday(started_at)) * 86400
                    AS INTEGER
                )
            WHERE id = :sid AND ended_at IS NULL
        """), {"sid": session_id})
        await db.commit()
    logger.info("session_closed", profile_id=profile_id, session_id=session_id)

    # SessionAnalyzer arka planda — messages=None → DB'den okur
    await _run_analysis_background(
        profile_id=profile_id,
        session_id=session_id,
        messages=None,
    )


# ── Ana chat mantığı ───────────────────────────────────────────────────────

async def _chat_logic(profile_id: str, message: str, db=None) -> str:
    """
    Sohbet mantığı — HTTP endpoint ve Slack bot her ikisi de kullanır.
    Döndürür: model yanıt metni (str).
    db geçilmezse yeni AsyncSession açılır.

    Session lifecycle:
    1. Aktif session yoksa yeni aç, plan üret, plan_json'a yaz.
    2. Aktif session varsa plan_json'dan plan yükle (konu değişmez).
    3. Gemini'ye sistem prompt + plan + geçmiş ile yanıt al.
    4. Mesaj vedaysa: session'ı kapat + arka planda analiz başlat.
    """
    from app.core.database import AsyncSessionLocal

    _close_db = False
    if db is None:
        db = AsyncSessionLocal()
        _close_db = True

    try:
        profile = await db.get(Profile, profile_id)
        if not profile:
            return "Profil bulunamadı."

        # ── 1. Session aç / mevcut session'ı bul ──────────────────────────
        session_id, plan_json, is_new_session = await _get_or_create_active_session(
            db, profile_id
        )

        # ── 2. Plan: yeni session → üret + kaydet; mevcut → yükle ─────────
        plan: SessionPlan | None = None
        plan_injection: str | None = None

        if not is_new_session and plan_json:
            # Mevcut session — sticky plan
            plan = _restore_plan(plan_json)

        if plan is None:
            # Yeni session VEYA plan_json bozuk → yeni plan üret
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
                await _store_plan(db, session_id, plan)
            except Exception as exc:
                logger.warning("curriculum_plan_failed", error=str(exc))

        # Plan'dan injection string oluştur (her mesajda taze agent_strategy ile)
        if plan is not None:
            try:
                strat_row = await db.execute(
                    text("SELECT agent_strategy, weekly_grammar_target FROM profiles WHERE id = :pid"),
                    {"pid": profile_id},
                )
                strat_data = strat_row.fetchone()
                agent_strategy = None
                grammar_target = None
                if strat_data:
                    try:
                        agent_strategy = json.loads(strat_data[0]) if strat_data[0] else None
                    except Exception:
                        pass
                    grammar_target = strat_data[1]

                from app.api.routes.vocabulary import _build_prompt_injection
                # last_insight: yeni session üretimde zaten kullanıldı;
                # mevcut session için tekrar çekmiyoruz (performans)
                plan_injection = _build_prompt_injection(
                    plan, None,
                    agent_strategy=agent_strategy,
                    weekly_grammar_target=grammar_target,
                )
            except Exception as exc:
                logger.warning("plan_injection_failed", error=str(exc))

        # ── 3. Kullanıcı mesajını kaydet ───────────────────────────────────
        user_msg = ChatMessage(profile_id=profile_id, role="user", content=message)
        db.add(user_msg)
        await db.flush()

        # ── 4. Geçmiş: sadece bu session içindeki mesajlar ─────────────────
        # Session başlangıç zamanını al
        session_row = await db.execute(text("""
            SELECT started_at FROM sessions WHERE id = :sid
        """), {"sid": session_id})
        session_started = session_row.scalar()

        if session_started:
            history_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.profile_id == profile_id)
                .where(ChatMessage.created_at >= session_started)
                .order_by(ChatMessage.created_at)
            )
        else:
            # Fallback: son 50 mesaj
            history_result = await db.execute(
                select(ChatMessage)
                .where(ChatMessage.profile_id == profile_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(50)
            )
        session_messages = list(history_result.scalars().all())
        # Fallback durumunda ters sıralama düzelt
        if not session_started:
            session_messages = list(reversed(session_messages))

        # ── 5. Gemini'ye gönder ────────────────────────────────────────────
        phoneme_scores = await _get_phoneme_summary(db, profile_id)
        system_prompt = _load_system_prompt()
        system_prompt += "\n\n" + _build_context(profile, phoneme_scores, plan_injection)

        contents = [
            types.Content(role=msg.role, parts=[types.Part(text=msg.content)])
            for msg in session_messages
        ]

        try:
            from app.services.tool_handlers import build_tools, dispatch_tool
            gemini_client = genai.Client(api_key=settings.gemini_api_key)
            tools = build_tools()
            reply_text = "Yanıt oluşturulamadı."

            # Tool calling döngüsü: Gemini tool_call dönmediği sürece tekrarla
            for _ in range(10):  # sonsuz döngü yerine güvenli üst limit
                response = gemini_client.models.generate_content(
                    model=settings.gemini_model_text,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        tools=tools,
                        temperature=0.7,
                        max_output_tokens=2048,
                    ),
                )

                candidate = response.candidates[0] if response.candidates else None
                if not candidate:
                    break

                # Tool call var mı?
                fn_call = next(
                    (p.function_call for p in candidate.content.parts if p.function_call),
                    None,
                )

                if fn_call is None:
                    # Tool call yok — yanıt hazır
                    reply_text = response.text or "Yanıt oluşturulamadı."
                    break

                # Tool'u çalıştır, sonucu contents'e ekle
                tool_result = await dispatch_tool(
                    name=fn_call.name,
                    args=dict(fn_call.args),
                    db=db,
                    profile_id=profile_id,
                )
                contents.append(candidate.content)
                contents.append(types.Content(
                    role="tool",
                    parts=[types.Part(
                        function_response=types.FunctionResponse(
                            name=fn_call.name,
                            response=tool_result,
                        )
                    )],
                ))

        except Exception as exc:
            logger.error("gemini_chat_error", error=str(exc))
            reply_text = "Bir hata oluştu, lütfen tekrar dene."

        # ── 6. Model yanıtını kaydet ───────────────────────────────────────
        model_msg = ChatMessage(profile_id=profile_id, role="model", content=reply_text)
        db.add(model_msg)
        await db.flush()
        await db.commit()

        logger.info(
            "chat_message_sent",
            profile_id=profile_id,
            session_id=session_id,
            is_new_session=is_new_session,
            farewell=_is_farewell(message),
        )

        # ── 7. Veda tespiti → session kapat (arka planda) ──────────────────
        if _is_farewell(message):
            asyncio.create_task(
                _close_session_background(profile_id, session_id)
            )

        return reply_text

    finally:
        if _close_db:
            await db.close()


# ── HTTP Endpoint ──────────────────────────────────────────────────────────

@router.get("/history/{profile_id}", response_model=list[ChatMessageOut])
async def get_chat_history(
    profile_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Profil için tüm mesaj geçmişini döndür."""
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profil bulunamadı.")

    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.profile_id == profile_id)
        .order_by(ChatMessage.created_at)
    )
    return list(result.scalars().all())


@router.post("", response_model=ChatResponse)
async def send_message(
    body: ChatMessageIn,
    db: AsyncSession = Depends(get_db),
):
    """HTTP chat endpoint — _chat_logic delegasyonu."""
    profile = await db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profil bulunamadı.")

    reply = await _chat_logic(body.profile_id, body.message, db)

    # Son 2 mesajı al (user + model) — _chat_logic commit etti
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.profile_id == body.profile_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(2)
    )
    recent = list(reversed(result.scalars().all()))

    return ChatResponse(
        reply=reply,
        user_msg=ChatMessageOut.model_validate(recent[0]),
        model_msg=ChatMessageOut.model_validate(recent[1]),
    )
