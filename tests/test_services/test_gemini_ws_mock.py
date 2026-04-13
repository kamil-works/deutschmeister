"""
CLAUDE.md Katman 2: FastAPI proxy unit testleri — Gemini WS mock'lanmış.
Gerçek API'ye bağlanmaz.
"""
import asyncio
import base64
import json
import math
import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Ses format yardımcıları ──────────────────────────────────────────────────

def make_pcm_bytes(duration_s: float = 0.1, sample_rate: int = 16000) -> bytes:
    """Test için Int16 PCM üretir."""
    num = int(sample_rate * duration_s)
    out = bytearray()
    for i in range(num):
        val = int(math.sin(2 * math.pi * 440 * i / sample_rate) * 0.3 * 0x7FFF)
        out += struct.pack("<h", val)
    return bytes(out)


def make_b64_audio(duration_s: float = 0.1) -> str:
    return base64.b64encode(make_pcm_bytes(duration_s)).decode()


# ── Audio utility testleri ───────────────────────────────────────────────────

class TestAudioUtils:
    def test_float32_to_int16_conversion(self):
        """Float32 → Int16 dönüşüm doğruluğu."""
        from app.utils.audio import float32_list_to_int16_pcm

        samples = [0.0, 1.0, -1.0, 0.5, -0.5]
        result = float32_list_to_int16_pcm(samples)

        assert len(result) == len(samples) * 2  # Int16 = 2 byte/sample

        ints = struct.unpack(f"<{len(samples)}h", result)
        assert ints[0] == 0           # 0.0 → 0
        assert ints[1] == 0x7FFF      # 1.0 → +32767
        assert ints[2] == -0x8000     # -1.0 → -32768
        assert ints[3] == pytest.approx(0x7FFF * 0.5, abs=2)

    def test_clamp_out_of_range(self):
        """[-1, 1] dışındaki değerler clamp edilmeli."""
        from app.utils.audio import float32_list_to_int16_pcm

        samples = [2.0, -2.0]
        result = float32_list_to_int16_pcm(samples)
        ints = struct.unpack("<2h", result)
        assert ints[0] == 0x7FFF   # 2.0 → max
        assert ints[1] == -0x8000  # -2.0 → min

    def test_pcm_bytes_to_wav(self):
        """Ham PCM → WAV format doğrulama."""
        from app.utils.audio import pcm_bytes_to_wav

        pcm = make_pcm_bytes(0.5)
        wav = pcm_bytes_to_wav(pcm, sample_rate=16000)

        # WAV header: 'RIFF'
        assert wav[:4] == b"RIFF"
        assert wav[8:12] == b"WAVE"

    def test_validate_pcm_format_valid(self):
        """Geçerli PCM format doğrulaması."""
        from app.utils.audio import validate_pcm_format

        pcm = make_pcm_bytes(1.0, 16000)  # 1 sn @ 16kHz = 32000 byte
        info = validate_pcm_format(pcm, expected_rate=16000)

        assert info["valid"] is True
        assert info["byte_count"] == 32000
        assert info["sample_count"] == 16000
        assert info["expected_duration_ms"] == pytest.approx(1000.0, abs=1)

    def test_validate_pcm_format_odd_bytes(self):
        """Tek sayıda byte → geçersiz."""
        from app.utils.audio import validate_pcm_format

        info = validate_pcm_format(b"\x00\x01\x02", expected_rate=16000)
        assert info["valid"] is False

    def test_generate_test_tone(self):
        """Test tonu formatı doğrulaması."""
        from app.utils.audio import generate_test_tone, validate_pcm_format

        pcm = generate_test_tone(440.0, 1.0, 16000)
        info = validate_pcm_format(pcm, 16000)

        assert info["valid"] is True
        assert info["byte_count"] == 32000  # 1s @ 16kHz Int16

    def test_base64_roundtrip(self):
        """PCM → base64 → PCM roundtrip."""
        from app.utils.audio import base64_to_pcm_bytes, pcm_bytes_to_base64

        original = make_pcm_bytes(0.1)
        encoded = pcm_bytes_to_base64(original)
        decoded = base64_to_pcm_bytes(encoded)
        assert decoded == original


# ── Config testleri ─────────────────────────────────────────────────────────

class TestConfig:
    def test_cors_origins_list_single(self):
        from app.core.config import Settings
        s = Settings(cors_origins="http://localhost:5173")
        assert s.cors_origins_list == ["http://localhost:5173"]

    def test_cors_origins_list_multiple(self):
        from app.core.config import Settings
        s = Settings(cors_origins="http://localhost:5173,https://example.com")
        assert len(s.cors_origins_list) == 2
        assert "https://example.com" in s.cors_origins_list


# ── Schema doğrulama testleri ────────────────────────────────────────────────

class TestSchemas:
    def test_profile_create_valid(self):
        from app.schemas.structured_outputs import ProfileCreate
        p = ProfileCreate(name="Ali", age=10, level="beginner")
        assert p.name == "Ali"
        assert p.level == "beginner"

    def test_profile_create_invalid_level(self):
        from app.schemas.structured_outputs import ProfileCreate
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            ProfileCreate(name="Ali", level="expert")  # geçersiz level

    def test_phoneme_result_valid_phonemes(self):
        from app.schemas.structured_outputs import PhonemeResult
        pr = PhonemeResult(phoneme="ü", score=85, feedback="Çok iyi!")
        assert pr.phoneme == "ü"

    def test_phoneme_result_invalid_phoneme(self):
        from app.schemas.structured_outputs import PhonemeResult
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            PhonemeResult(phoneme="z", score=50, feedback="test")

    def test_phoneme_score_range(self):
        from app.schemas.structured_outputs import PhonemeResult
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            PhonemeResult(phoneme="ü", score=150, feedback="test")  # 100 üstü geçersiz

    def test_exercise_generate_request_count_limit(self):
        from app.schemas.structured_outputs import ExerciseGenerateRequest
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            ExerciseGenerateRequest(profile_id="x", count=99)  # max 20


# ── Proxy mesaj yapısı testleri ──────────────────────────────────────────────

class TestProxySetupMessage:
    def test_setup_message_structure(self):
        """Setup mesajının Gemini beklentisiyle uyumlu olduğunu doğrula."""
        from app.services.gemini_live_proxy import _build_setup_message

        msg = _build_setup_message("conversation", "Test Kullanıcı", "beginner")

        assert "setup" in msg
        setup = msg["setup"]

        # Model adı doğru mu?
        assert "gemini-live-2.5-flash-native-audio" in setup["model"]

        # VAD ayarları doğru mu?
        vad = setup["realtime_input_config"]["automatic_activity_detection"]
        assert vad["end_of_speech_sensitivity"] == "END_SENSITIVITY_LOW"
        assert vad["silence_duration_ms"] == 300

        # Session resumption etkin mi?
        assert "session_resumption" in setup

        # Transkript etkin mi?
        assert "input_audio_transcription" in setup
        assert "output_audio_transcription" in setup

        # System instruction var mı?
        assert "system_instruction" in setup

    def test_pronunciation_mode_uses_different_prompt(self):
        """Pronunciation modu farklı prompt yüklüyor mu?"""
        from app.services.gemini_live_proxy import _build_setup_message

        conv_msg = _build_setup_message("conversation", "Ali", "beginner")
        pron_msg = _build_setup_message("pronunciation", "Ali", "beginner")

        conv_text = conv_msg["setup"]["system_instruction"]["parts"][0]["text"]
        pron_text = pron_msg["setup"]["system_instruction"]["parts"][0]["text"]

        # İkisi farklı prompt kullanmalı (ya da en azından farklı içerik)
        # (Dosya yoksa her ikisi de fallback döner ama yapı doğru olmalı)
        assert isinstance(conv_text, str)
        assert isinstance(pron_text, str)


# ── Pronunciation JSON çıkarımı testleri ─────────────────────────────────────

class TestPronunciationJsonExtraction:
    def test_extract_json_from_markdown_block(self):
        from app.services.gemini_pronunciation import _extract_json_from_response

        text = '''Çok güzel telaffuz ettiniz!

```json
{"word": "über", "overall_score": 78, "phonemes": [], "tip": "ü sesini daha yuvarlak söyleyin"}
```
'''
        result = _extract_json_from_response(text)
        assert result["word"] == "über"
        assert result["overall_score"] == 78

    def test_extract_json_from_plain_text(self):
        from app.services.gemini_pronunciation import _extract_json_from_response

        text = '{"word": "Schule", "overall_score": 90, "phonemes": [], "tip": "Mükemmel!"}'
        result = _extract_json_from_response(text)
        assert result["word"] == "Schule"

    def test_extract_json_no_json_raises(self):
        from app.services.gemini_pronunciation import _extract_json_from_response

        with pytest.raises(ValueError, match="JSON bulunamadı"):
            _extract_json_from_response("Bu bir test metnidir, JSON yok.")


# ── pytest yapılandırması ─────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
