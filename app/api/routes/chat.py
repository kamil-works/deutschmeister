"""
Text-to-text chat endpoint.
Gemini REST API ile metin tabanlı Almanca ders.
Tüm geçmiş DB'de saklanır ve her istekte context olarak gönderilir.
"""
import json
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from google import genai
from google.genai import types

from app.core.config import get_settings
from app.core.database import get_db
from app.models.db import ChatMessage, Profile, PhonemeScore
from app.services.curriculum_engine import CurriculumEngine
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


def _load_system_prompt() -> str:
    try:
        with open(SYSTEM_PROMPT_PATH, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "You are a helpful German language tutor for Turkish-speaking learners."


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
        f"## KULLANICI PROFİLİ",
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


# ── Geçmiş getir ──────────────────────────────────────────────────────────

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



async def _chat_logic(profile_id: str, message: str, db=None) -> str:
    """
    Sohbet mantığı — HTTP endpoint ve Slack bot her ikisi de bu fonksiyonu kullanır.
    Döndürür: model yanıt metni (str).
    db geçilmezse yeni AsyncSession açılır.
    """
    from app.core.database import AsyncSessionLocal
    import json as _json
    from sqlalchemy import text as _text

    _close_db = False
    if db is None:
        db = AsyncSessionLocal()
        _close_db = True

    try:
        profile = await db.get(Profile, profile_id)
        if not profile:
            return "Profil bulunamadı."

        user_msg = ChatMessage(profile_id=profile_id, role="user", content=message)
        db.add(user_msg)
        await db.flush()

        result = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.profile_id == profile_id)
            .order_by(ChatMessage.created_at)
        )
        all_messages = list(result.scalars().all())

        system_prompt  = _load_system_prompt()
        phoneme_scores = await _get_phoneme_summary(db, profile_id)

        plan_injection = None
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
            _strat_row  = await db.execute(
                _text("SELECT agent_strategy, weekly_grammar_target FROM profiles WHERE id = :pid"),
                {"pid": profile_id}
            )
            _strat_data = _strat_row.fetchone()
            _agent_strategy = None
            _grammar_target = None
            if _strat_data:
                try:
                    _agent_strategy = _json.loads(_strat_data[0]) if _strat_data[0] else None
                except Exception:
                    pass
                _grammar_target = _strat_data[1]

            from app.api.routes.vocabulary import _build_prompt_injection
            plan_injection = _build_prompt_injection(
                plan, last_insight,
                agent_strategy=_agent_strategy,
                weekly_grammar_target=_grammar_target,
            )
        except Exception as exc:
            logger.warning("curriculum_plan_failed", error=str(exc))

        system_prompt += "\n\n" + _build_context(profile, phoneme_scores, plan_injection)

        contents = [
            types.Content(role=msg.role, parts=[types.Part(text=msg.content)])
            for msg in all_messages
        ]

        try:
            client = genai.Client(api_key=settings.gemini_api_key)
            response = client.models.generate_content(
                model=settings.gemini_model_text,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.7,
                    max_output_tokens=2048,
                ),
            )
            reply_text = response.text or "Yanıt oluşturulamadı."
        except Exception as exc:
            logger.error("gemini_chat_error", error=str(exc))
            reply_text = "Bir hata oluştu, lütfen tekrar dene."

        model_msg = ChatMessage(profile_id=profile_id, role="model", content=reply_text)
        db.add(model_msg)
        await db.flush()
        await db.commit()

        return reply_text
    finally:
        if _close_db:
            await db.close()

# ── Mesaj gönder ───────────────────────────────────────────────────────────

@router.post("", response_model=ChatResponse)
async def send_message(
    body: ChatMessageIn,
    db: AsyncSession = Depends(get_db),
):
    """
    _chat_logic wrapper — HTTP endpoint için ince katman.
    Tüm iş mantığı _chat_logic içinde, Slack bot da aynı fonksiyonu çağırır.
    """
    profile = await db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profil bulunamadı.")

    # Kullanıcı mesajını DB'ye kaydet (ilk kayıt, history için)
    user_msg = ChatMessage(profile_id=body.profile_id, role="user", content=body.message)
    db.add(user_msg)
    await db.flush()
    await db.refresh(user_msg)

    # Tüm geçmişi al
    result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.profile_id == body.profile_id)
        .order_by(ChatMessage.created_at)
    )
    all_messages = list(result.scalars().all())

    # System prompt + plan injection
    import json as _json
    from sqlalchemy import text as _text
    system_prompt  = _load_system_prompt()
    phoneme_scores = await _get_phoneme_summary(db, body.profile_id)
    plan_injection = None
    try:
        fsrs_engine  = FSRSEngine(db)
        curriculum   = CurriculumEngine(db)
        analyzer     = SessionAnalyzer(db)
        last_insight = await analyzer.get_last_insight(body.profile_id)
        fsrs_stats   = await fsrs_engine.get_stats(body.profile_id)
        plan = await curriculum.get_session_plan(
            profile_id=body.profile_id,
            fsrs_stats=fsrs_stats,
            last_session_insight=last_insight,
        )
        _strat_row  = await db.execute(
            _text("SELECT agent_strategy, weekly_grammar_target FROM profiles WHERE id = :pid"),
            {"pid": body.profile_id}
        )
        _strat_data = _strat_row.fetchone()
        _agent_strategy = None
        _grammar_target = None
        if _strat_data:
            try:
                _agent_strategy = _json.loads(_strat_data[0]) if _strat_data[0] else None
            except Exception:
                pass
            _grammar_target = _strat_data[1]
        from app.api.routes.vocabulary import _build_prompt_injection
        plan_injection = _build_prompt_injection(
            plan, last_insight,
            agent_strategy=_agent_strategy,
            weekly_grammar_target=_grammar_target,
        )
    except Exception as exc:
        logger.warning("curriculum_plan_failed", error=str(exc))

    system_prompt += "\n\n" + _build_context(profile, phoneme_scores, plan_injection)

    contents = [
        types.Content(role=msg.role, parts=[types.Part(text=msg.content)])
        for msg in all_messages
    ]

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = client.models.generate_content(
            model=settings.gemini_model_text,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.7,
                max_output_tokens=2048,
            ),
        )
        reply_text = response.text or "Üzgünüm, yanıt oluşturulamadı."
    except Exception as exc:
        logger.error("gemini_chat_error", error=str(exc))
        reply_text = "Bir hata oluştu, lütfen tekrar dene."

    model_msg = ChatMessage(profile_id=body.profile_id, role="model", content=reply_text)
    db.add(model_msg)
    await db.flush()
    await db.refresh(model_msg)
    await db.commit()

    logger.info("chat_message_sent", profile_id=body.profile_id,
                user_len=len(body.message), reply_len=len(reply_text))

    return ChatResponse(
        reply=reply_text,
        user_msg=ChatMessageOut.model_validate(user_msg),
        model_msg=ChatMessageOut.model_validate(model_msg),
    )
