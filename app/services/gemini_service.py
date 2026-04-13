"""
Gemini REST API servisi — egzersiz üretimi için.
Live API (ses) için gemini_live_proxy.py kullanılır.
Chat için chat.py direkt genai.Client kullanır.
"""
import json
import structlog
from google import genai
from google.genai import types

from app.core.config import get_settings
from app.schemas.structured_outputs import ExerciseItem, ExerciseGenerateResponse

logger = structlog.get_logger(__name__)
settings = get_settings()

EXERCISE_PROMPT_PATH = "app/agent/prompts/exercise_generation.txt"


def _get_client() -> genai.Client:
    return genai.Client(api_key=settings.gemini_api_key)


def _load_prompt(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        logger.warning("prompt_not_found", path=path)
        return ""


async def generate_exercises(
    profile_id: str,
    level: str,
    focus_phonemes: list[str],
    count: int,
    allowed_vocabulary: list[str] | None = None,  # Grounding: sadece bu kelimeler kullanılabilir
) -> ExerciseGenerateResponse:
    """
    Gemini text API kullanarak telaffuz + kelime egzersizleri üretir.
    allowed_vocabulary verilirse Gemini sadece o kelimelerle egzersiz üretir (grounding).
    Hata durumunda sabit fallback egzersizler döner.
    """
    client = _get_client()
    phoneme_str = ", ".join(focus_phonemes) if focus_phonemes else "genel Almanca telaffuz"
    base_prompt = _load_prompt(EXERCISE_PROMPT_PATH)

    # Grounding: izin verilen kelime listesi
    vocab_block = ""
    if allowed_vocabulary:
        vocab_list = ", ".join(allowed_vocabulary[:20])  # max 20 kelime
        vocab_block = (
            f"\n\n⚠️ KELIME KISITI (Grounding):\n"
            f"Egzersizlerde YALNIZCA şu onaylanmış Goethe/CEFR kelimelerini kullan: {vocab_list}\n"
            "Bu listenin dışındaki hiçbir kelimeyi egzersize EKLEME. "
            "Eklersen sistem reddeder."
        )

    user_prompt = (
        f"{base_prompt}\n\n"
        f"Seviye: {level}\n"
        f"Odak fonemler: {phoneme_str}\n"
        f"Egzersiz sayısı: {count}"
        f"{vocab_block}\n\n"
        "Yanıtını şu JSON formatında ver (başka bir şey yazma):\n"
        '{"exercises": [\n'
        '  {"type": "pronunciation|vocabulary|grammar", '
        '"instruction": "Türkçe yönerge", '
        '"target_text": "Almanca hedef metin", '
        '"hint": "ipucu veya null", '
        '"phonemes_targeted": ["fonem1"]}\n'
        "]}"
    )

    try:
        response = client.models.generate_content(
            model=settings.gemini_model_text,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.8,
                max_output_tokens=4096,
            ),
        )
        data = json.loads(response.text)
        exercises = [ExerciseItem(**ex) for ex in data.get("exercises", [])]
        return ExerciseGenerateResponse(profile_id=profile_id, exercises=exercises)

    except Exception as exc:
        logger.error("exercise_generation_failed", error=str(exc), profile_id=profile_id)
        return _fallback_exercises(profile_id, count)


def _fallback_exercises(profile_id: str, count: int) -> ExerciseGenerateResponse:
    """Gemini başarısız olursa sabit örnek egzersizler döner."""
    templates = [
        ExerciseItem(type="pronunciation", instruction="Bu kelimeyi yüksek sesle okuyun",
                     target_text="Schule", hint="'Sch' sesi Türkçe 'ş' gibi okunur",
                     phonemes_targeted=["sch"]),
        ExerciseItem(type="pronunciation", instruction="Bu kelimeyi yüksek sesle okuyun",
                     target_text="nicht", hint="'ch' sesi için dilinizi ön damağa yaklaştırın",
                     phonemes_targeted=["ch-ich"]),
        ExerciseItem(type="pronunciation", instruction="Bu kelimeyi yüksek sesle okuyun",
                     target_text="müde", hint="'ü' sesi için dudaklarınızı yuvarlayın",
                     phonemes_targeted=["ü"]),
        ExerciseItem(type="pronunciation", instruction="Bu kelimeyi yüksek sesle okuyun",
                     target_text="schön", hint="'ö' sesi için dudakları yuvarlayın, 'e' deyin",
                     phonemes_targeted=["sch", "ö"]),
        ExerciseItem(type="pronunciation", instruction="Bu kelimeyi yüksek sesle okuyun",
                     target_text="Buch", hint="'ch' sesi gırtlaktan 'h' gibi",
                     phonemes_targeted=["ch-ach"]),
    ]
    return ExerciseGenerateResponse(profile_id=profile_id, exercises=templates[:count])
