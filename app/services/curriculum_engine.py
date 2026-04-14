"""
CurriculumEngine — i+1 Adaptive Curriculum
============================================
Krashen'in i+1 prensibi + çok-faktörlü seviye değerlendirmesi.

Seviye kararı SADECE FSRS'e dayanmaz:
  - FSRS mastery_rate (ağırlık: %40)
  - SessionAnalyzer level_assessment (ağırlık: %35)
  - Hata kalıpları (error_patterns) (ağırlık: %15)
  - anxiety_signal (güvenlik freni — yüksek kaygıda seviye ATLAMA) (%10)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Seviye sırası
LEVELS = ["A1", "A2", "B1", "B2"]

# anxiety_signal → session_size çarpanı
ANXIETY_SIZE_FACTOR = {
    "low":    1.0,   # normal
    "medium": 0.75,  # %25 küçült
    "high":   0.5,   # %50 küçült — kısa, kolay, düşük baskı
}

# anxiety_signal → i+ seviyesi değişikliği
ANXIETY_LEVEL_OVERRIDE = {
    "low":    0,   # plan değişmez
    "medium": 0,   # plan değişmez
    "high":  -1,   # i+1 yerine i (mevcut seviye) kullan
}


@dataclass
class SessionPlan:
    profile_id: str
    target_level: str           # öğrencinin mevcut seviyesi
    plus_one_level: str         # i+1 seviyesi
    focus_topic_slug: str       # bu oturumun ana konusu
    focus_topic_tr: str         # Türkçe konu adı
    vocabulary: list[dict]      # öğretilecek yeni kelimeler
    review_words: list[dict]    # FSRS due olan tekrar kelimeleri
    artikel_drill: list[dict]   # der/die/das pratiği
    grammar_focus: str          # bu oturumun gramer odağı
    session_size: int           # toplam kelime sayısı
    anxiety_signal: str         # low|medium|high
    motivation_message: str     # giriş mesajı


class CurriculumEngine:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ──────────────────────────────────────────────────────────────────────
    # get_session_plan
    # ──────────────────────────────────────────────────────────────────────
    async def get_session_plan(
        self,
        profile_id: str,
        requested_topic: str | None = None,
        fsrs_stats: dict | None = None,
        last_session_insight: dict | None = None,
    ) -> SessionPlan:
        """
        Oturum planı oluşturur. Tüm modüllerin giriş noktası.

        Parametreler:
          requested_topic: Kullanıcı/frontend'in istediği konu (opsiyonel)
          fsrs_stats: FSRSEngine.get_stats() çıktısı (opsiyonel, varsa daha iyi plan)
          last_session_insight: SessionAnalyzer'ın son oturum analizi (opsiyonel)
        """
        # 1. Profil bilgisi
        profile = await self._get_profile(profile_id)
        current_level = profile.get("level", "A1")  # A1|A2|B1|B2

        # 2. anxiety_signal — son oturum analizinden veya default
        anxiety = "low"
        if last_session_insight:
            anxiety = last_session_insight.get("anxiety_signal", "low")

        # 3. i+1 seviyesi — kaygı yüksekse mevcut seviyede kal
        level_shift = ANXIETY_LEVEL_OVERRIDE.get(anxiety, 0)
        current_idx = LEVELS.index(current_level) if current_level in LEVELS else 0
        plus_one_idx = max(0, min(len(LEVELS) - 1, current_idx + 1 + level_shift))
        plus_one_level = LEVELS[plus_one_idx]

        # 4. Konu seçimi
        focus_topic = await self._select_topic(
            profile_id=profile_id,
            requested_topic=requested_topic,
            current_level=current_level,
            last_insight=last_session_insight,
        )

        # 5. Dinamik session_size
        base_size = 15
        size_factor = ANXIETY_SIZE_FACTOR.get(anxiety, 1.0)
        session_size = max(5, int(base_size * size_factor))

        # 6. Gramer odağı
        grammar_focus = self._select_grammar_focus(
            level=current_level,
            error_patterns=last_session_insight.get("error_patterns", []) if last_session_insight else [],
            anxiety=anxiety,
        )

        # 7. Kelimeler — due (tekrar) + yeni (i+1'den)
        review_words = []
        if fsrs_stats and fsrs_stats.get("due_today", 0) > 0:
            review_words = await self._get_due_words(
                profile_id, topic_slug=focus_topic["slug"],
                limit=min(5, session_size // 3)
            )

        new_word_count = session_size - len(review_words)
        vocabulary = await self._get_new_words(
            current_level=current_level,
            plus_one_level=plus_one_level,
            topic_slug=focus_topic["slug"],
            limit=new_word_count,
            profile_id=profile_id,
            anxiety=anxiety,
        )

        # 8. Artikel drill — her zaman 3-5 isim
        artikel_count = 0 if anxiety == "high" else 3
        artikel_drill = await self._get_artikel_drill(
            profile_id=profile_id,
            level=current_level,
            limit=artikel_count,
        )

        # 9. Motivasyon mesajı
        msg = self._opening_message(
            anxiety=anxiety,
            topic_tr=focus_topic["name_tr"],
            due_count=len(review_words),
            new_count=len(vocabulary),
        )

        logger.info(
            f"SessionPlan: profile={profile_id} level={current_level} "
            f"topic={focus_topic['slug']} anxiety={anxiety} size={session_size}"
        )

        return SessionPlan(
            profile_id=profile_id,
            target_level=current_level,
            plus_one_level=plus_one_level,
            focus_topic_slug=focus_topic["slug"],
            focus_topic_tr=focus_topic["name_tr"],
            vocabulary=vocabulary,
            review_words=review_words,
            artikel_drill=artikel_drill,
            grammar_focus=grammar_focus,
            session_size=session_size,
            anxiety_signal=anxiety,
            motivation_message=msg,
        )

    # ──────────────────────────────────────────────────────────────────────
    # update_level — çok-faktörlü seviye değerlendirmesi
    # ──────────────────────────────────────────────────────────────────────
    async def update_level(
        self,
        profile_id: str,
        fsrs_stats: dict,
        last_session_insight: dict | None = None,
    ) -> dict:
        """
        Seviyeyi günceller. FSRS'e ek olarak SessionAnalyzer bulgularını da kullanır.

        Faktör ağırlıkları:
          FSRS mastery_rate        → %40
          SessionAnalyzer level_assessment → %35
          error_patterns sayısı   → %15
          anxiety_signal          → %10 (güvenlik freni)

        Döner: {old_level, new_level, changed, reason}
        """
        profile = await self._get_profile(profile_id)
        current_level = profile.get("level", "A1")
        current_idx = LEVELS.index(current_level) if current_level in LEVELS else 0

        anxiety = "low"
        if last_session_insight:
            anxiety = last_session_insight.get("anxiety_signal", "low")

        # Kaygı yüksekse seviye değişikliği yok — güvenlik freni
        if anxiety == "high":
            return {
                "old_level": current_level,
                "new_level": current_level,
                "changed": False,
                "reason": "Kaygı yüksek — seviye değişikliği ertelendi",
            }

        # ── Faktör 1: FSRS mastery_rate (%40) ──
        total = fsrs_stats.get("total_cards", 0)
        mastered = fsrs_stats.get("mastered", 0)
        fsrs_mastery = (mastered / total) if total > 0 else 0.0
        retention = fsrs_stats.get("retention_rate", 0.0)
        fsrs_score = (fsrs_mastery * 0.6 + retention * 0.4)  # normalize

        # ── Faktör 2: SessionAnalyzer level_assessment (%35) ──
        analyzer_score = 0.5  # default: belirsiz
        if last_session_insight:
            assessment = last_session_insight.get("level_assessment", current_level)
            # Ör: "A1+" → mevcut seviyenin üstünde → yükselt
            if "+" in str(assessment):
                analyzer_score = 0.85
            elif assessment in LEVELS:
                assessment_idx = LEVELS.index(assessment)
                if assessment_idx > current_idx:
                    analyzer_score = 0.90
                elif assessment_idx < current_idx:
                    analyzer_score = 0.30
                else:
                    analyzer_score = 0.60

        # ── Faktör 3: Hata kalıpları (%15) ──
        error_count = 0
        if last_session_insight:
            error_count = len(last_session_insight.get("error_patterns", []))
        error_score = max(0.0, 1.0 - (error_count * 0.15))  # her hata kalıbı %15 düşürür

        # ── Faktör 4: Kaygı (%10) ──
        anxiety_score = {"low": 0.9, "medium": 0.5, "high": 0.1}.get(anxiety, 0.5)

        # ── Ağırlıklı toplam ──
        composite = (
            fsrs_score * 0.40 +
            analyzer_score * 0.35 +
            error_score * 0.15 +
            anxiety_score * 0.10
        )

        # ── Karar ──
        new_idx = current_idx
        reason = f"Bileşik skor: {composite:.2f} (FSRS:{fsrs_score:.2f} Analiz:{analyzer_score:.2f})"

        if composite >= 0.80 and current_idx < len(LEVELS) - 1:
            # Seviye atlamak için 3 ardışık oturumda bu skoru tutmalı
            consecutive = await self._get_consecutive_high_score(profile_id)
            if consecutive >= 3:
                new_idx = current_idx + 1
                reason += f" — {consecutive} oturumda yüksek skor → seviye atlandı"
            else:
                reason += f" — {consecutive}/3 oturum tamamlandı, henüz atlamıyor"
        elif composite < 0.45 and current_idx > 0:
            new_idx = current_idx - 1
            reason += " — Düşük skor → seviye düşürüldü"

        new_level = LEVELS[new_idx]
        changed = new_level != current_level

        if changed:
            await self.db.execute(text("""
                UPDATE profiles SET level = :level WHERE id = :profile_id
            """), {"level": new_level, "profile_id": profile_id})
            await self.db.commit()
            logger.info(f"Level güncellendi: {current_level} → {new_level} ({reason})")

        return {
            "old_level": current_level,
            "new_level": new_level,
            "changed": changed,
            "composite_score": round(composite, 2),
            "reason": reason,
        }

    # ──────────────────────────────────────────────────────────────────────
    # Yardımcı metodlar
    # ──────────────────────────────────────────────────────────────────────
    async def _get_profile(self, profile_id: str) -> dict:
        result = await self.db.execute(text("""
            SELECT id, name, level FROM profiles WHERE id = :id
        """), {"id": profile_id})
        row = result.fetchone()
        if not row:
            return {"id": profile_id, "name": "Öğrenci", "level": "A1"}
        return {"id": row[0], "name": row[1], "level": row[2] or "A1"}

    async def _select_topic(
        self,
        profile_id: str,
        requested_topic: str | None,
        current_level: str,
        last_insight: dict | None,
    ) -> dict:
        """Konu seçimi: istenen > analizden önerilen > FSRS due > rastgele"""

        # 1. Kullanıcı istemişse
        if requested_topic:
            result = await self.db.execute(text(
                "SELECT slug, name_tr FROM topics WHERE slug = :s"
            ), {"s": requested_topic})
            row = result.fetchone()
            if row:
                return {"slug": row[0], "name_tr": row[1]}

        # 2. SessionAnalyzer'ın önerisini kullan
        if last_insight and last_insight.get("recommended_next_topic"):
            slug = last_insight["recommended_next_topic"]
            result = await self.db.execute(text(
                "SELECT slug, name_tr FROM topics WHERE slug = :s"
            ), {"s": slug})
            row = result.fetchone()
            if row:
                return {"slug": row[0], "name_tr": row[1]}

        # 3. En çok due kart olan topic
        result = await self.db.execute(text("""
            SELECT t.slug, t.name_tr, COUNT(*) as due_count
            FROM fsrs_cards fc
            JOIN words w ON fc.word_id = w.id
            JOIN topics t ON w.topic_id = t.id
            WHERE fc.profile_id = :profile_id
              AND fc.state IN ('learning','review','relearning')
              AND fc.due <= datetime('now')
              AND w.level = :level
            GROUP BY t.slug
            ORDER BY due_count DESC
            LIMIT 1
        """), {"profile_id": profile_id, "level": current_level})
        row = result.fetchone()
        if row:
            return {"slug": row[0], "name_tr": row[1]}

        # 4. Hiç girilmemiş topic — merak tetikle
        result = await self.db.execute(text("""
            SELECT t.slug, t.name_tr
            FROM topics t
            WHERE t.min_level <= :level
              AND t.slug NOT IN (
                SELECT DISTINCT t2.slug FROM fsrs_cards fc
                JOIN words w ON fc.word_id = w.id
                JOIN topics t2 ON w.topic_id = t2.id
                WHERE fc.profile_id = :profile_id
              )
            ORDER BY RANDOM() LIMIT 1
        """), {"profile_id": profile_id, "level": current_level})
        row = result.fetchone()
        if row:
            return {"slug": row[0], "name_tr": row[1]}

        # 5. Son çare — rastgele
        result = await self.db.execute(text(
            "SELECT slug, name_tr FROM topics ORDER BY RANDOM() LIMIT 1"
        ))
        row = result.fetchone()
        return {"slug": row[0], "name_tr": row[1]} if row else {"slug": "gunluk_yasam", "name_tr": "Günlük Yaşam"}

    async def _get_due_words(self, profile_id: str, topic_slug: str, limit: int) -> list[dict]:
        result = await self.db.execute(text("""
            SELECT w.id, w.word, w.article, w.translation_tr, fc.state, fc.lapses
            FROM fsrs_cards fc
            JOIN words w ON fc.word_id = w.id
            JOIN topics t ON w.topic_id = t.id
            WHERE fc.profile_id = :profile_id
              AND t.slug = :slug
              AND fc.due <= datetime('now')
              AND fc.state != 'new'
            ORDER BY fc.due ASC LIMIT :limit
        """), {"profile_id": profile_id, "slug": topic_slug, "limit": limit})
        return [{"word_id": r[0], "word": r[1], "article": r[2],
                 "translation_tr": r[3], "state": r[4], "lapses": r[5]}
                for r in result.fetchall()]

    async def _get_new_words(
        self,
        current_level: str,
        plus_one_level: str,
        topic_slug: str,
        limit: int,
        profile_id: str,
        anxiety: str,
    ) -> list[dict]:
        """
        i+1 prensibi: mevcut seviyeden %80, plus_one'dan %20.
        Kaygı yüksekse sadece mevcut seviye.
        """
        current_limit = limit if anxiety == "high" else max(1, int(limit * 0.80))
        plus_limit = 0 if anxiety == "high" else limit - current_limit

        rows = []
        for lv, lim in [(current_level, current_limit), (plus_one_level, plus_limit)]:
            if lim <= 0:
                continue
            result = await self.db.execute(text("""
                SELECT w.id, w.word, w.article, w.plural, w.translation_tr,
                       w.example_de, w.level, w.has_tricky_article
                FROM words w
                JOIN topics t ON w.topic_id = t.id
                WHERE t.slug = :slug AND w.level = :level
                  AND w.id NOT IN (
                    SELECT word_id FROM fsrs_cards WHERE profile_id = :profile_id
                  )
                ORDER BY RANDOM() LIMIT :limit
            """), {"slug": topic_slug, "level": lv,
                   "profile_id": profile_id, "limit": lim})
            rows.extend(result.fetchall())

        return [
            {
                "word_id": r[0], "word": r[1], "article": r[2], "plural": r[3],
                "translation_tr": r[4], "example_de": r[5], "level": r[6],
                "has_tricky_article": bool(r[7]),
                "is_new": True,
            }
            for r in rows
        ]

    async def _get_artikel_drill(self, profile_id: str, level: str, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        result = await self.db.execute(text("""
            SELECT w.id, w.word, w.article, w.translation_tr
            FROM words w
            WHERE w.word_type = 'noun' AND w.level = :level AND w.article IS NOT NULL
            ORDER BY RANDOM() LIMIT :limit
        """), {"level": level, "limit": limit})
        return [{"word_id": r[0], "word": r[1], "article": r[2], "translation_tr": r[3]}
                for r in result.fetchall()]

    def _select_grammar_focus(self, level: str, error_patterns: list, anxiety: str) -> str:
        if anxiety == "high":
            return "Serbest konuşma — gramer odağı yok"
        # Hata kalıplarına göre öncelik ver
        if "artikel_neutrum" in error_patterns:
            return "das-Artikel (tarafsız cins)"
        if "word_order_V2" in error_patterns:
            return "Cümle düzeni — Fiil 2. sıraya"
        if "dativ_case" in error_patterns:
            return "Dativ hâli (mit/bei/von + Dativ)"
        # Seviyeye göre varsayılan
        defaults = {
            "A1": "Temel cümle yapısı (Subjekt + Verb + Objekt)",
            "A2": "Perfekt (geçmiş zaman — bin/habe + Partizip)",
            "B1": "Konjunktiv II (würde/könnte/sollte)",
            "B2": "Passiv ve Modalverb kombinasyonları",
        }
        return defaults.get(level, "Serbest pratik")

    async def _get_consecutive_high_score(self, profile_id: str) -> int:
        """Son kaç oturumda ardışık yüksek skor? (Profile tablosunda session_high_score_streak)"""
        # Basit implementasyon — gerçek uygulamada session_insights tablosu kullanılacak
        result = await self.db.execute(text("""
            SELECT COUNT(*) FROM profiles WHERE id = :id
        """), {"id": profile_id})
        # TODO: session_insights tablosu hazır olunca buraya bağlanacak
        return 1  # Şimdilik 1 döner — 3 gerekiyor, yani hemen atlamaz

    def _opening_message(
        self,
        anxiety: str,
        topic_tr: str,
        due_count: int,
        new_count: int,
    ) -> str:
        if anxiety == "high":
            return f"Bugün {topic_tr} konusunu beraber keşfedelim. Yavaş ve rahat bir tempo ile başlayalım."
        if due_count > 0:
            return (f"Bugün {topic_tr} konusunda {due_count} tekrar + "
                    f"{new_count} yeni kelime var. Hazır mısın?")
        return f"Bugün {topic_tr} konusundan {new_count} yeni kelime öğreneceğiz."
