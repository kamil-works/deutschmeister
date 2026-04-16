"""
SessionAnalyzer — Knowledge Tracing + Post-Session Analiz
===========================================================
Her oturum SONUNDA (oturum sırasında değil) çalışır.
FastAPI BackgroundTask olarak tetiklenir — kullanıcıyı bekletmez.

Özellikler:
  - Gemini API timeout/rate limit için retry mekanizması (exponential backoff)
  - Başarısız analizler session_insights tablosunda 'failed' olarak işaretlenir
  - Bir sonraki oturum başında tekrar denenir
  - Sonuçlar FSRSEngine.bulk_review_from_analysis() ile FSRS'e yansıtılır
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Retry ayarları
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0  # saniye — her denemede 2x artar (2, 4, 8)

# Minimum analiz için gereken mesaj sayısı
MIN_MESSAGES_FOR_ANALYSIS = 3

ANALYZER_PROMPT = """Sen bir Almanca dil öğretmenisin ve öğrenci-AI sohbetini analiz ediyorsun.
Türk öğrencilerle çalışıyorsun.

Aşağıdaki sohbet geçmişini incele ve JSON formatında analiz döndür.

JSON Şeması:
{
  "mastered": ["kelime1", "der Hund", "gehen"],
  "struggled": ["das Mädchen", "Konjunktiv"],
  "error_patterns": ["artikel_neutrum", "word_order_V2", "dativ_case", "plural_form", "verb_conjugation"],
  "anxiety_signal": "low",
  "engagement": "high",
  "recommended_next_topic": "topic_slug",
  "level_assessment": "A1+",
  "session_quality": 0.75,
  "summary_tr": "Türkçe kısa özet (1-2 cümle)",
  "tutor_observations": "Bu öğrenciye dair öğretmen notu — max 100 kelime Türkçe. Ne işe yaradı? Nerede takıldı? Bir sonraki seans için öneri.",
  "engagement_hooks": ["futbol", "yemek"],
  "scaffolding_style": "Direct"
}

Kurallar:
- mastered: Öğrencinin doğru ve akıcı kullandığı kelimeler
- struggled: Yanlış, eksik veya hiç kullanamadığı kelimeler

❗ ÖNEMLİ — YALIN HAL KURALI:
mastered ve struggled listelerindeki kelimeler MUTLAKA sözlükteki yalın halleriyle yazılmalıdır:
  - İsimler: nominatif tekil + artikel ile → "der Hund", "das Haus", "die Stadt"
  - Fiiller: mastar haliyle (infinitiv) → "gehen", "sein", "kaufen"
  - Sıfatlar: yükleme sıfatı haliyle (predicative) → "groß", "schnell"
  YANLIŞ: "bin gegangen", "dem Haus", "größer" → bunlar DB'de bulunamaz ve FSRS'e EKLENMİYOR
  DOĞRU: "gehen", "das Haus", "groß" → bunlar sözlük girişleridir

- error_patterns sadece şunlardan seçilebilir: artikel_neutrum, artikel_maskulin, 
  artikel_feminin, word_order_V2, dativ_case, akkusativ_case, plural_form,
  verb_conjugation, verb_position, umlaut_pronunciation, compound_word
- anxiety_signal: "high" = çok kısa yanıtlar, "bilmiyorum" tekrarı, uzun sessizlikler
- engagement: "low"/"medium"/"high"
- recommended_next_topic: şu sluglardan biri: kisisel, aile, ev_yasami, gunluk_yasam,
  yiyecek_icecek, alisveris, saglik, hastane, is_hayati, egitim, seyahat, sosyal_yasam
- level_assessment: A1/A1+/A2/A2+/B1/B1+/B2
- session_quality: 0.0-1.0 arası
- tutor_observations: Sadece bu öğrenciye özgü öğretmen notu. Şu soruları yanıtla:
    1) Ne işe yaradı? (somut örnekle)
    2) Nerede sıkıştı? Türkçe kaynaklı bir karışıklık var mıydı?
    3) Bir sonraki seansta yaklaşımını nasıl ayarlayacaksın?
    Maks 100 kelime, Türkçe, somut ve eyleme dönük yaz.
- engagement_hooks: Bu sohbette öğrencinin ilgisini çeken somut konular/nesneler (max 5). Örnek: ["futbol", "araba", "mutfak"]
- scaffolding_style: Bu öğrenci için en verimli öğretme tarzı. Sadece birini seç: "Socratic" | "Direct" | "Playful" | "Structured"

Sadece JSON döndür, başka metin ekleme.
"""


class SessionAnalyzer:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ──────────────────────────────────────────────────────────────────────
    # analyze_session — ana giriş noktası (BackgroundTask olarak çağrılır)
    # ──────────────────────────────────────────────────────────────────────
    async def analyze_session(
        self,
        profile_id: str,
        session_id: str,
        messages: list[dict] | None = None,
    ) -> dict | None:
        """
        Oturum analizini çalıştırır.
        messages: [{"role": "user"/"assistant", "content": "..."}]
        messages yoksa DB'den çeker.

        BackgroundTask kullanımı:
            background_tasks.add_task(
                analyzer.analyze_session, profile_id, session_id
            )
        """
        # Analiz zaten yapıldıysa atla
        existing = await self.db.execute(text("""
            SELECT status FROM session_insights
            WHERE session_id = :sid AND status = 'completed'
        """), {"sid": session_id})
        if existing.fetchone():
            logger.debug(f"Session {session_id} zaten analiz edilmiş")
            return None

        # Mesajları DB'den çek (verilmediyse)
        if not messages:
            messages = await self._fetch_messages(profile_id)

        if len(messages) < MIN_MESSAGES_FOR_ANALYSIS:
            logger.info(f"Session {session_id}: {len(messages)} mesaj — analiz atlandı")
            return None

        # Transcript oluştur
        transcript = self._build_transcript(messages)

        # Retry mekanizması
        insight = None
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                insight = await self._call_gemini(transcript, profile_id)
                break
            except Exception as e:
                last_error = e
                logger.warning(f"Analiz denemesi {attempt}/{MAX_RETRIES} başarısız: {e}")
                if attempt < MAX_RETRIES:
                    delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)

        if not insight:
            await self._save_insight(session_id, profile_id, status="failed",
                                     error=str(last_error))
            logger.error(f"Session {session_id} analizi {MAX_RETRIES} denemede başarısız")
            return None

        # DB'ye kaydet
        await self._save_insight(session_id, profile_id, status="completed", data=insight)

        # FSRSEngine'i güncelle (import burada — circular import önleme)
        await self._update_fsrs(profile_id, insight)

        # Agent Strategy'yi güncelle (Layer 2 — Reflection)
        await self._update_agent_strategy(profile_id, insight)

        # DailyLog'u güncelle
        await self._update_daily_log(profile_id, session_id, insight)

        logger.info(
            f"Session {session_id} analiz tamamlandı: "
            f"mastered={len(insight.get('mastered',[]))} "
            f"struggled={len(insight.get('struggled',[]))} "
            f"anxiety={insight.get('anxiety_signal')} "
            f"scaffolding={insight.get('scaffolding_style','?')}"
        )
        return insight

    # ──────────────────────────────────────────────────────────────────────
    # get_last_insight — CurriculumEngine için son analiz
    # ──────────────────────────────────────────────────────────────────────
    async def get_last_insight(self, profile_id: str) -> dict | None:
        """Son başarılı oturum analizini döner."""
        result = await self.db.execute(text("""
            SELECT data FROM session_insights
            WHERE profile_id = :pid AND status = 'completed'
            ORDER BY created_at DESC LIMIT 1
        """), {"pid": profile_id})
        row = result.fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────
    # retry_failed — başlangıçta başarısız olanları yeniden dene
    # ──────────────────────────────────────────────────────────────────────
    async def retry_failed_analyses(self) -> int:
        """
        Startup veya cron'da çağrılır.
        'failed' + 'pending' kayıtları yeniden dener.
        'pending' = sunucu çökmeden önce BackgroundTask hiç başlamamış demektir.
        """
        result = await self.db.execute(text("""
            SELECT session_id, profile_id FROM session_insights
            WHERE status IN ('failed', 'pending')
            ORDER BY created_at DESC LIMIT 20
        """))
        rows = result.fetchall()
        retried = 0
        for row in rows:
            try:
                insight = await self.analyze_session(row[0], row[1])
                if insight:
                    retried += 1
            except Exception as e:
                logger.error(f"Retry başarısız: session={row[0]} hata={e}")
        return retried

    # ──────────────────────────────────────────────────────────────────────
    # Yardımcı metodlar
    # ──────────────────────────────────────────────────────────────────────
    async def _fetch_messages(self, profile_id: str) -> list[dict]:
        result = await self.db.execute(text("""
            SELECT role, content FROM chat_messages
            WHERE profile_id = :pid
            ORDER BY created_at DESC LIMIT 30
        """), {"pid": profile_id})
        rows = result.fetchall()
        # Kronolojik sıra
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    def _build_transcript(self, messages: list[dict]) -> str:
        lines = []
        for m in messages:
            role = "Öğrenci" if m.get("role") == "user" else "Öğretmen"
            content = str(m.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    async def _call_gemini(self, transcript: str, profile_id: str) -> dict:
        """Gemini REST API çağrısı — timeout 30 saniye."""
        import os, httpx

        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            raise ValueError("GEMINI_API_KEY eksik")

        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.5-flash:generateContent"
        )

        payload = {
            "contents": [
                {"role": "user", "parts": [{"text": f"Sohbet:\n\n{transcript}"}]}
            ],
            "systemInstruction": {"parts": [{"text": ANALYZER_PROMPT}]},
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                url,
                json=payload,
                params={"key": api_key},
            )
            resp.raise_for_status()
            data = resp.json()

        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        # JSON temizle
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        insight = json.loads(raw)

        # Zorunlu alanlar var mı?
        for key in ["mastered", "struggled", "error_patterns", "anxiety_signal"]:
            if key not in insight:
                insight[key] = [] if key != "anxiety_signal" else "low"

        return insight

    async def _save_insight(
        self,
        session_id: str,
        profile_id: str,
        status: str,
        data: dict | None = None,
        error: str | None = None,
    ) -> None:
        # Tablo yoksa oluştur
        await self.db.execute(text("""
            CREATE TABLE IF NOT EXISTS session_insights (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                profile_id  TEXT NOT NULL,
                status      TEXT DEFAULT 'pending',
                data        TEXT,
                error       TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """))

        data_json = json.dumps(data, ensure_ascii=False) if data else None
        await self.db.execute(text("""
            INSERT INTO session_insights (session_id, profile_id, status, data, error)
            VALUES (:sid, :pid, :status, :data, :error)
            ON CONFLICT DO NOTHING
        """), {
            "sid": session_id,
            "pid": profile_id,
            "status": status,
            "data": data_json,
            "error": error,
        })
        await self.db.commit()

    async def _filter_by_dictionary(self, words: list[str]) -> tuple[list[str], list[str]]:
        """
        Dictionary Gate: Verilen kelime listesini DB'deki words tablosuna göre filtreler.

        Strateji (iki adımlı):
          1. Exact match: lowercase normalize edilmiş word ve article+word kombinasyonu aranır
          2. Fallback: artikel (ön ek) çıkarılıp sadece kelime kısmı aranır

        Döner: (geçerli_kelimeler, reddedilen_kelimeler)
        """
        if not words:
            return [], []

        valid, rejected = [], []
        for raw_word in words:
            word = raw_word.strip()
            if not word:
                continue

            # Artikel önekini çıkar: "der Hund" → "Hund", "die Stadt" → "Stadt"
            parts = word.split()
            artikel_prefixes = {"der", "die", "das", "ein", "eine"}
            if len(parts) >= 2 and parts[0].lower() in artikel_prefixes:
                bare_word = " ".join(parts[1:])
            else:
                bare_word = word

            # DB sorgusu: exact match (case-insensitive) VEYA bare_word match
            result = await self.db.execute(text("""
                SELECT id FROM words
                WHERE LOWER(word) = LOWER(:bare)
                   OR LOWER(word) = LOWER(:full)
                LIMIT 1
            """), {"bare": bare_word, "full": word})
            row = result.fetchone()

            if row:
                valid.append(word)
            else:
                rejected.append(word)
                logger.warning(
                    "dictionary_gate_rejected",
                    extra={"word": word, "reason": "not_in_db"}
                )

        if rejected:
            logger.info(
                "dictionary_gate_summary",
                extra={"valid": len(valid), "rejected": len(rejected), "rejected_words": rejected}
            )
        return valid, rejected

    async def _update_fsrs(self, profile_id: str, insight: dict) -> None:
        """
        Mastered/struggled kelimeleri FSRS'e yansıt.
        Dictionary Gate: sadece DB'de bulunan kelimeler FSRS'e girer.
        Reddedilen kelimeler loglanır — hallüsinasyon tespiti için izlenebilir.
        """
        from app.services.fsrs_engine import FSRSEngine

        raw_mastered = insight.get("mastered", [])
        raw_struggled = insight.get("struggled", [])

        mastered, rejected_m = await self._filter_by_dictionary(raw_mastered)
        struggled, rejected_s = await self._filter_by_dictionary(raw_struggled)

        all_rejected = rejected_m + rejected_s
        if all_rejected:
            logger.warning(
                "hallucination_detected",
                extra={"profile_id": profile_id, "rejected_words": all_rejected}
            )

        engine = FSRSEngine(self.db)
        await engine.bulk_review_from_analysis(
            profile_id=profile_id,
            mastered=mastered,
            struggled=struggled,
        )

    async def _update_agent_strategy(self, profile_id: str, insight: dict) -> None:
        """
        Layer 2 — Agent Expression Engine güncellemesi.
        Her seans sonunda SessionAnalyzer çıktısından
        tutor_observations, engagement_hooks, scaffolding_style alınır
        ve profiles.agent_strategy JSON'una yazılır.

        Constitution (Layer 1) hiçbir zaman bu metod tarafından değiştirilmez.
        """
        tutor_obs = insight.get("tutor_observations", "")
        eng_hooks = insight.get("engagement_hooks", [])
        scaffolding = insight.get("scaffolding_style", "Direct")
        error_patterns = insight.get("error_patterns", [])

        # Geçerli scaffolding_style değerlerini dogrula (PCE validation — basit)
        valid_styles = {"Socratic", "Direct", "Playful", "Structured"}
        if scaffolding not in valid_styles:
            scaffolding = "Direct"

        # Mevcut stratejiyi çek
        result = await self.db.execute(text("""
            SELECT agent_strategy FROM profiles WHERE id = :pid
        """), {"pid": profile_id})
        row = result.fetchone()
        try:
            current = json.loads(row[0]) if row and row[0] else {}
        except Exception:
            current = {}

        # sessions_count artır
        ped_state = current.get("pedagogical_state", {})
        sessions_count = ped_state.get("sessions_count", 0) + 1

        new_strategy = {
            "tutor_observations": tutor_obs or current.get("tutor_observations", ""),
            "engagement_hooks": eng_hooks if eng_hooks else current.get("engagement_hooks", []),
            "scaffolding_style": scaffolding,
            "emotional_calibration": insight.get("anxiety_signal", "neutral"),
            "pedagogical_state": {
                "current_complexity": min(1.0 + sessions_count * 0.05, 2.0),  # yavaş artış
                "sessions_count": sessions_count,
                "last_error_patterns": error_patterns,
            },
        }

        await self.db.execute(text("""
            UPDATE profiles SET agent_strategy = :strategy WHERE id = :pid
        """), {
            "strategy": json.dumps(new_strategy, ensure_ascii=False),
            "pid": profile_id,
        })
        await self.db.commit()
        logger.info(
            f"agent_strategy güncellendi: profile={profile_id} "
            f"scaffolding={scaffolding} sessions={sessions_count}"
        )

    async def _update_daily_log(self, profile_id: str, session_id: str, insight: dict) -> None:
        """
        Günlük ders logunu güncelle. Her analiz sonrası çağrılır.
        Aynı günde birden fazla ders varsa mevcut kaydı günceller.
        Tarih Türkiye saatiyle (UTC+3) hesaplanır.
        """
        from datetime import timezone, timedelta
        import json as _json

        tr_tz = timezone(timedelta(hours=3))
        today_tr = datetime.now(tr_tz).strftime("%Y-%m-%d")

        # Session süresini çek
        sess_result = await self.db.execute(text("""
            SELECT duration_s FROM sessions WHERE id = :sid
        """), {"sid": session_id})
        sess_row = sess_result.fetchone()
        duration_s = sess_row[0] if sess_row and sess_row[0] else 0

        mastered = len(insight.get("mastered", []))
        struggled = len(insight.get("struggled", []))
        quality = insight.get("session_quality", 0.0)
        anxiety = insight.get("anxiety_signal", "low")
        summary = insight.get("summary_tr", "")
        errors = _json.dumps(insight.get("error_patterns", []), ensure_ascii=False)

        # Bugün için kayıt var mı?
        existing = await self.db.execute(text("""
            SELECT id, session_count, total_duration_s, words_learned,
                   words_struggled, words_mastered
            FROM daily_logs
            WHERE profile_id = :pid AND log_date = :today
        """), {"pid": profile_id, "today": today_tr})
        row = existing.fetchone()

        if row:
            log_id = row[0]
            await self.db.execute(text("""
                UPDATE daily_logs SET
                    session_count    = session_count + 1,
                    total_duration_s = total_duration_s + :dur,
                    words_learned    = words_learned + :learned,
                    words_struggled  = words_struggled + :struggled,
                    words_mastered   = words_mastered + :mastered,
                    session_quality  = :quality,
                    anxiety_signal   = :anxiety,
                    ai_impressions   = :summary,
                    error_patterns   = :errors,
                    updated_at       = datetime('now')
                WHERE id = :lid
            """), {
                "dur": duration_s, "learned": mastered, "struggled": struggled,
                "mastered": mastered, "quality": quality, "anxiety": anxiety,
                "summary": summary, "errors": errors, "lid": log_id,
            })
        else:
            await self.db.execute(text("""
                INSERT INTO daily_logs
                    (profile_id, log_date, session_count, total_duration_s,
                     words_learned, words_struggled, words_mastered,
                     session_quality, anxiety_signal, ai_impressions, error_patterns,
                     created_at, updated_at)
                VALUES
                    (:pid, :today, 1, :dur, :learned, :struggled, :mastered,
                     :quality, :anxiety, :summary, :errors,
                     datetime('now'), datetime('now'))
            """), {
                "pid": profile_id, "today": today_tr, "dur": duration_s,
                "learned": mastered, "struggled": struggled, "mastered": mastered,
                "quality": quality, "anxiety": anxiety, "summary": summary, "errors": errors,
            })

        await self.db.commit()
        logger.info(f"daily_log_updated profile={profile_id} date={today_tr}")
