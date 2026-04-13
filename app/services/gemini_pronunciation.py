"""
Telaffuz değerlendirme servisi — Gemini REST API kullanır.
(Gerçek zamanlı telaffuz Live API proxy üzerinden yapılır.
Bu servis kayıtlı ses dosyaları için tek seferlik değerlendirme içindir.)
"""
import base64
import json
import structlog
from google import genai
from google.genai import types

from app.core.config import get_settings
from app.schemas.structured_outputs import (
    AssessRequest,
    AssessResponse,
    PronunciationAssessResult,
)
from app.utils.audio import validate_pcm_format

logger = structlog.get_logger(__name__)
settings = get_settings()

PRONUNCIATION_PROMPT_PATH = "app/agent/prompts/pronunciation_assessment.txt"


def _load_prompt(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


async def assess_pronunciation(request: AssessRequest) -> AssessResponse:
    """
    Base64 PCM ses verisini Gemini'ye gönderir, fonem geri bildirimi alır.
    Gerçek zamanlı değil — egzersiz tamamlama değerlendirmesi için.
    target_word varsa Gemini'ye hedef kelime bildirilir (daha doğru değerlendirme).
    """
    client = genai.Client(api_key=settings.gemini_api_key)

    # Ses verisi doğrulama
    try:
        pcm_bytes = base64.b64decode(request.audio_base64)
    except Exception:
        raise ValueError("Geçersiz ses verisi: base64 decode edilemedi.")

    info = validate_pcm_format(pcm_bytes, expected_rate=16000)
    if not info["valid"]:
        raise ValueError("Geçersiz ses formatı: çift sayıda byte olmalı (Int16 PCM).")

    logger.debug(
        "assess_pronunciation_request",
        profile_id=request.profile_id,
        target_word=request.target_word,
        audio_ms=info["expected_duration_ms"],
    )

    system_prompt = _load_prompt(PRONUNCIATION_PROMPT_PATH)

    # Hedef kelimeyi prompt'a ekle — Gemini ne beklediğini bilmesi için kritik
    if request.target_word:
        word_context = (
            f"\n\nKullanıcının okuması gereken Almanca metin: '{request.target_word}'\n"
            "Yukarıdaki ses kaydında kullanıcı bu metni okuyor. "
            "Bu metni baz alarak değerlendirme yap ve JSON bloğunu döndür."
        )
    else:
        word_context = (
            "\n\nSes kaydında hangi Almanca kelime/cümle okunduğunu algıla ve değerlendir. "
            "JSON bloğunu ekle."
        )

    # Gemini'ye ses + metin prompt gönder
    audio_part = types.Part(
        inline_data=types.Blob(
            mime_type="audio/pcm;rate=16000",
            data=pcm_bytes,
        )
    )
    text_part = types.Part(text=(system_prompt + word_context))

    try:
        response = client.models.generate_content(
            model=settings.gemini_model_text,
            contents=types.Content(
                role="user",
                parts=[audio_part, text_part],
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )

        # JSON mode aktif — yanıt artık doğrudan geçerli JSON
        raw_text = response.text or "{}"
        # Fallback: eski parse mantığını koru (Markdown code block olasılığına karşı)
        result_data = _extract_json_from_response(raw_text)
        result = PronunciationAssessResult(**result_data)

        return AssessResponse(
            profile_id=request.profile_id,
            session_id=request.session_id,
            result=result,
        )

    except Exception as exc:
        logger.error("pronunciation_assess_failed", error=str(exc), profile_id=request.profile_id)
        raise RuntimeError(f"Telaffuz değerlendirmesi başarısız: {exc}") from exc


def _extract_json_from_response(text: str) -> dict:
    """
    Gemini yanıtından JSON bloğunu çıkarır.
    Model bazen ```json ... ``` bloğu içinde, bazen düz JSON döner.
    """
    # ```json ... ``` bloğu varsa çıkar
    if "```json" in text:
        start = text.index("```json") + 7
        end = text.index("```", start)
        return json.loads(text[start:end].strip())

    # ``` ... ``` bloğu
    if "```" in text:
        start = text.index("```") + 3
        end = text.index("```", start)
        return json.loads(text[start:end].strip())

    # Son { ... } bloğunu bul
    brace_start = text.rfind("{")
    brace_end = text.rfind("}") + 1
    if brace_start != -1 and brace_end > brace_start:
        return json.loads(text[brace_start:brace_end])

    raise ValueError(f"Yanıtta JSON bulunamadı: {text[:200]}")
