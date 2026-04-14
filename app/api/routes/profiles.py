from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Profile
from app.schemas.structured_outputs import ProfileCreate, ProfileOut, ProfileUpdate
from app.services.fsrs_engine import FSRSEngine

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


@router.post("", response_model=ProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(body: ProfileCreate, db: AsyncSession = Depends(get_db)) -> Profile:
    profile = Profile(name=body.name, age=body.age, level=body.level)
    db.add(profile)
    await db.flush()
    await db.refresh(profile)

    # Yeni profil için FSRS kartlarını otomatik oluştur
    engine = FSRSEngine(db)
    await engine.initialize_cards(profile_id=profile.id, level=profile.level)

    return profile


@router.get("", response_model=list[ProfileOut])
async def list_profiles(db: AsyncSession = Depends(get_db)) -> list[Profile]:
    result = await db.execute(select(Profile).order_by(Profile.created_at))
    return list(result.scalars().all())


@router.get("/{profile_id}", response_model=ProfileOut)
async def get_profile(profile_id: str, db: AsyncSession = Depends(get_db)) -> Profile:
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )
    return profile


@router.patch("/{profile_id}", response_model=ProfileOut)
async def update_profile(
    profile_id: str,
    body: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
) -> Profile:
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )
    update_data = body.model_dump(exclude_none=True)
    for key, value in update_data.items():
        setattr(profile, key, value)
    await db.flush()
    await db.refresh(profile)
    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, db: AsyncSession = Depends(get_db)) -> None:
    profile = await db.get(Profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )
    await db.delete(profile)
