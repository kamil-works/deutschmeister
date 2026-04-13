"""
Tüm Pydantic request/response modelleri.
Route katmanı sadece bu şemaları görür — SQLAlchemy modeli dışarı sızmaz.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    age: int | None = Field(None, ge=4, le=120)
    level: Literal["beginner", "intermediate", "advanced"] = "beginner"


class ProfileUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    age: int | None = Field(None, ge=4, le=120)
    level: Literal["beginner", "intermediate", "advanced"] | None = None


class ProfileOut(BaseModel):
    id: str
    name: str
    age: int | None
    level: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class SessionCreate(BaseModel):
    profile_id: str
    mode: Literal["conversation", "pronunciation"] = "conversation"


class SessionOut(BaseModel):
    id: str
    profile_id: str
    started_at: datetime
    ended_at: datetime | None
    duration_s: int | None
    mode: str

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Phoneme / Pronunciation
# ---------------------------------------------------------------------------

VALID_PHONEMES = {"ü", "ö", "ä", "ch-ich", "ch-ach", "r", "sch"}

# Gemini'nin döndürebileceği IPA/alternatif semboller → geçerli fonem ID'sine mapping
_IPA_TO_PHONEME: dict[str, str] = {
    # sch / ʃ
    "ʃ": "sch", "sh": "sch",
    # ü
    "yː": "ü", "ʏ": "ü", "y": "ü",
    # ö
    "øː": "ö", "œ": "ö", "ø": "ö",
    # ä
    "ɛː": "ä", "ɛ": "ä",
    # ch-ich
    "ç": "ch-ich", "ich-laut": "ch-ich", "ch_ich": "ch-ich",
    # ch-ach
    "x": "ch-ach", "ach-laut": "ch-ach", "ch_ach": "ch-ach",
    # r
    "ʁ": "r", "ʀ": "r",
}


class PhonemeResult(BaseModel):
    phoneme: str
    score: int = Field(..., ge=0, le=100)
    feedback: str  # Türkçe, tek cümle

    @field_validator("phoneme")
    @classmethod
    def validate_phoneme(cls, v: str) -> str:
        # Zaten geçerliyse doğrudan döndür
        if v in VALID_PHONEMES:
            return v
        # IPA/alternatif sembolse eşleştir
        mapped = _IPA_TO_PHONEME.get(v)
        if mapped:
            return mapped
        # Bulunamadıysa hata ver
        raise ValueError(f"Geçersiz fonem: {v}. Geçerliler: {VALID_PHONEMES}")


class PronunciationAssessResult(BaseModel):
    """Gemini'nin system prompt'a uygun döndürdüğü JSON'un Pydantic karşılığı."""
    word: str
    overall_score: int = Field(..., ge=0, le=100)
    phonemes: list[PhonemeResult]
    tip: str  # Türkçe, tek cümle


class AssessRequest(BaseModel):
    profile_id: str
    session_id: str | None = None
    audio_base64: str  # 16kHz mono Int16 PCM, base64
    target_word: str | None = None  # Kullanıcının okuması gereken Almanca kelime/cümle


class AssessResponse(BaseModel):
    profile_id: str
    session_id: str | None
    result: PronunciationAssessResult
    saved: bool = False  # SQLite'a kaydedildi mi


class PhonemeProgressItem(BaseModel):
    phoneme: str
    average_score: float
    last_score: int
    session_count: int


class PhonemeProgressResponse(BaseModel):
    profile_id: str
    phonemes: list[PhonemeProgressItem]


# ---------------------------------------------------------------------------
# Exercise
# ---------------------------------------------------------------------------

class ExerciseGenerateRequest(BaseModel):
    profile_id: str
    focus_phonemes: list[str] | None = None  # Boşsa zayıf fonemler otomatik seçilir
    count: int = Field(5, ge=1, le=20)


class ExerciseItem(BaseModel):
    type: Literal["pronunciation", "vocabulary", "grammar"]
    instruction: str          # Türkçe yönerge
    target_text: str          # Almanca hedef metin
    hint: str | None = None   # İpucu (isteğe bağlı)
    phonemes_targeted: list[str] = []


class ExerciseGenerateResponse(BaseModel):
    profile_id: str
    exercises: list[ExerciseItem]


class ExerciseOut(BaseModel):
    id: int
    profile_id: str
    type: str
    content: str   # JSON string — istemci parse eder
    created_at: datetime
    completed: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Chat (Text-to-Text)
# ---------------------------------------------------------------------------

class ChatMessageIn(BaseModel):
    profile_id: str
    message: str = Field(..., min_length=1, max_length=5000)


class ChatMessageOut(BaseModel):
    id: int
    role: str            # user | model
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatResponse(BaseModel):
    reply: str
    user_msg: ChatMessageOut
    model_msg: ChatMessageOut


# ---------------------------------------------------------------------------
# Genel
# ---------------------------------------------------------------------------

class ErrorResponse(BaseModel):
    detail: str   # Her zaman Türkçe
