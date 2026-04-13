from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import PhonemeScore, Profile
from app.schemas.structured_outputs import (
    AssessRequest,
    AssessResponse,
    PhonemeProgressItem,
    PhonemeProgressResponse,
)
from app.services.gemini_pronunciation import assess_pronunciation

router = APIRouter(prefix="/api/pronunciation", tags=["pronunciation"])


@router.post("/assess", response_model=AssessResponse)
async def assess(body: AssessRequest, db: AsyncSession = Depends(get_db)) -> AssessResponse:
    """
    Tek seferlik telaffuz değerlendirmesi (gerçek zamanlı değil).
    Ses Live API WS üzerinden, bu endpoint kayıtlı egzersiz tamamlama için.
    """
    profile = await db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )

    try:
        result = await assess_pronunciation(body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    # Fonem skorlarını SQLite'a kaydet
    for phoneme_result in result.result.phonemes:
        score_row = PhonemeScore(
            profile_id=body.profile_id,
            session_id=body.session_id,
            phoneme=phoneme_result.phoneme,
            score=phoneme_result.score,
        )
        db.add(score_row)

    await db.flush()
    result.saved = True
    return result


@router.get("/progress/{profile_id}", response_model=PhonemeProgressResponse)
async def get_progress(profile_id: str, db: AsyncSession = Depends(get_db)) -> PhonemeProgressResponse:
    """Profil için fonem bazlı ilerleme özeti."""
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )

    # Her fonem için ortalama skor, son skor ve oturum sayısı
    result = await db.execute(
        select(
            PhonemeScore.phoneme,
            func.avg(PhonemeScore.score).label("avg_score"),
            func.max(PhonemeScore.score).label("last_score"),
            func.count(PhonemeScore.id).label("session_count"),
        )
        .where(PhonemeScore.profile_id == profile_id)
        .group_by(PhonemeScore.phoneme)
        .order_by(func.avg(PhonemeScore.score))  # En zayıf önce
    )

    rows = result.all()
    items = [
        PhonemeProgressItem(
            phoneme=row.phoneme,
            average_score=round(float(row.avg_score), 1),
            last_score=int(row.last_score),
            session_count=int(row.session_count),
        )
        for row in rows
    ]

    return PhonemeProgressResponse(profile_id=profile_id, phonemes=items)
