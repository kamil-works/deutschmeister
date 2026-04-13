import json
import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Exercise, PhonemeScore, Profile
from app.schemas.structured_outputs import (
    ExerciseGenerateRequest,
    ExerciseGenerateResponse,
    ExerciseOut,
)
from app.services.gemini_service import generate_exercises

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/api/exercises", tags=["exercises"])

WEAK_PHONEME_THRESHOLD = 60   # Bu skorun altı "zayıf" sayılır
MAX_WEAK_PHONEMES = 3         # Odak için en fazla 3 fonem


@router.post("/generate", response_model=ExerciseGenerateResponse)
async def generate(
    body: ExerciseGenerateRequest,
    db: AsyncSession = Depends(get_db),
) -> ExerciseGenerateResponse:
    """
    Profil için egzersiz seti üretir.
    focus_phonemes boşsa SQLite'taki zayıf fonemler otomatik seçilir.
    """
    profile = await db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )

    focus = body.focus_phonemes

    # focus_phonemes belirtilmemisse zayıf fonemler otomatik seç
    if not focus:
        focus = await _get_weak_phonemes(body.profile_id, db)

    # Grounding: CurriculumEngine'den o oturumun onaylanmış kelimelerini çek
    allowed_vocabulary: list[str] | None = None
    try:
        from app.services.curriculum_engine import CurriculumEngine
        from app.services.fsrs_engine import FSRSEngine
        fsrs = FSRSEngine(db)
        stats = await fsrs.get_stats(body.profile_id)
        curriculum = CurriculumEngine(db)
        plan = await curriculum.get_session_plan(
            profile_id=body.profile_id,
            fsrs_stats=stats,
        )
        # Yeni kelimeler + tekrar kelimelerini birleştir
        vocab_words = [w.get("word", "") for w in (plan.vocabulary or [])]
        review_words = [w.get("word", "") for w in (plan.review_words or [])]
        combined = [w for w in vocab_words + review_words if w]
        if combined:
            allowed_vocabulary = combined
            logger.info(
                "exercise_grounding_active",
                profile_id=body.profile_id,
                word_count=len(combined),
            )
    except Exception as exc:
        # Grounding başarsız olursa egzersiz yine üretilir — sadece kısıtsız
        logger.warning("exercise_grounding_failed", error=str(exc))

    try:
        result = await generate_exercises(
            profile_id=body.profile_id,
            level=profile.level,
            focus_phonemes=focus,
            count=body.count,
            allowed_vocabulary=allowed_vocabulary,
        )
    except Exception as exc:
        logger.error("generate_exercises_failed", error=str(exc), profile_id=body.profile_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Egzersiz üretimi başarısız. Lütfen tekrar deneyin.",
        )

    # Üretilen egzersizleri SQLite'a kaydet
    for ex in result.exercises:
        db.add(Exercise(
            profile_id=body.profile_id,
            type=ex.type,
            content=json.dumps(ex.model_dump(), ensure_ascii=False),
        ))
    await db.flush()

    return result


@router.get("/{profile_id}", response_model=list[ExerciseOut])
async def list_exercises(
    profile_id: str,
    completed: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[Exercise]:
    """Profil egzersizleri — varsayılan: tamamlanmamışlar."""
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )

    stmt = (
        select(Exercise)
        .where(Exercise.profile_id == profile_id)
        .where(Exercise.completed == completed)
        .order_by(Exercise.created_at.desc())
        .limit(50)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch("/{exercise_id}/complete", response_model=ExerciseOut)
async def complete_exercise(exercise_id: int, db: AsyncSession = Depends(get_db)) -> Exercise:
    """Egzersizi tamamlandı olarak işaretle."""
    ex = await db.get(Exercise, exercise_id)
    if not ex:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Egzersiz bulunamadı.",
        )
    ex.completed = True
    await db.flush()
    await db.refresh(ex)
    return ex


# ── Yardımcı fonksiyon ──────────────────────────────────────────────────────

async def _get_weak_phonemes(profile_id: str, db: AsyncSession) -> list[str]:
    """
    SQLite'tan ortalama skoruna göre zayıf fonemleri döner.
    Hiç skor yoksa boş liste döner (Gemini genel egzersiz üretir).
    """
    from sqlalchemy import func
    result = await db.execute(
        select(PhonemeScore.phoneme, func.avg(PhonemeScore.score).label("avg"))
        .where(PhonemeScore.profile_id == profile_id)
        .group_by(PhonemeScore.phoneme)
        .having(func.avg(PhonemeScore.score) < WEAK_PHONEME_THRESHOLD)
        .order_by(func.avg(PhonemeScore.score))
        .limit(MAX_WEAK_PHONEMES)
    )
    return [row.phoneme for row in result.all()]
