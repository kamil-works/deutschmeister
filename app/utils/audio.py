"""
Ses format dönüşüm yardımcıları.
Tüm ses işlemi burada — route'larda veya proxy'de ses işleme yapılmaz.
"""
import base64
import struct
import wave
import io
import math


def base64_to_pcm_bytes(b64: str) -> bytes:
    """Base64 string → ham PCM bytes."""
    return base64.b64decode(b64)


def pcm_bytes_to_base64(pcm: bytes) -> str:
    """Ham PCM bytes → base64 string."""
    return base64.b64encode(pcm).decode("utf-8")


def float32_list_to_int16_pcm(samples: list[float]) -> bytes:
    """
    Float32 [-1.0, 1.0] listesi → Int16 PCM little-endian bytes.
    Test ve doğrulama amaçlı — production'da dönüşüm frontend AudioWorklet'te yapılır.
    """
    out = bytearray()
    for s in samples:
        clamped = max(-1.0, min(1.0, s))
        val = int(clamped * 0x7FFF) if clamped >= 0 else int(clamped * 0x8000)
        out += struct.pack("<h", val)  # little-endian signed short
    return bytes(out)


def pcm_bytes_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """
    Ham Int16 PCM bytes → WAV dosyası bytes.
    Kayıt ve debug amaçlı.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)      # Mono
        wf.setsampwidth(2)      # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def generate_test_tone(frequency: float = 440.0, duration_s: float = 1.0, sample_rate: int = 16000) -> bytes:
    """
    Belirli frekansta test tonu üretir — format doğrulama için.
    Çıktı: Int16 PCM @ sample_rate Hz, mono.
    """
    num_samples = int(sample_rate * duration_s)
    samples = [
        math.sin(2 * math.pi * frequency * i / sample_rate) * 0.5
        for i in range(num_samples)
    ]
    return float32_list_to_int16_pcm(samples)


def validate_pcm_format(pcm_bytes: bytes, expected_rate: int = 16000) -> dict:
    """
    PCM verisini doğrular ve bilgi döner.
    Debugging için kullanılır.
    """
    byte_count = len(pcm_bytes)
    sample_count = byte_count // 2  # Int16 = 2 byte/sample
    duration_ms = (sample_count / expected_rate) * 1000

    return {
        "byte_count": byte_count,
        "sample_count": sample_count,
        "expected_duration_ms": round(duration_ms, 1),
        "expected_rate_hz": expected_rate,
        "valid": byte_count % 2 == 0,  # Int16 için çift sayıda byte olmalı
    }
