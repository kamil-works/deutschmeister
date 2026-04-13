"""
FSRSEngine — Spaced Repetition Servisi
========================================
py-fsrs kütüphanesini DeutschMeister'ın SQLite DB'siyle birleştirir.

Kullanım:
    engine = FSRSEngine(db_session)
    await engine.initialize_cards(profile_id, level="A1", topic_slug="ev_yasami")
    cards = await engine.get_due_cards(profile_id, limit=10)
    await engine.record_review(profile_id, word_id=42, rating=3)
    stats = await engine.get_stats(profile_id)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from math import exp
from typing import Optional

from fsrs import Card as FSRSCard, Rating, Scheduler, State
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# FSRS Scheduler — %90 retention hedefi
_scheduler = Scheduler(desired_retention=0.90)

# Rating int → fsrs Rating eşlemesi
# 1=Again(unuttu), 2=Hard(zor), 3=Good(iyi), 4=Easy(kolay)
RATING_MAP: dict[int, Rating] = {
    1: Rating.Again,
    2: Rating.Hard,
    3: Rating.Good,
    4: Rating.Easy,
}

STATE_NAMES = {
    1: "learning",
    2: "review",
    3: "relearning",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_dt(val) -> datetime:
    """String veya datetime → timezone-aware datetime"""
    if val is None:
        return _now()
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    # SQLite'tan string gelirse
    try:
        dt = datetime.fromisoformat(str(val))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return _now()


def _retrievability(stability: float | None, last_review: datetime | None) -> float:
    """
    Şu an hatırlama olasılığı (0.0 - 1.0).
    FSRS formülü: R = (1 + FACTOR * t / S)^DECAY
    t = last_review'dan bu yana geçen gün
    """
    if not stability or stability <= 0 or not last_review:
        return 1.0
    elapsed = (_now() - _to_dt(last_review)).total_seconds() / 86400
    if elapsed <= 0:
        return 1.0
    FACTOR = -0.5
    DECAY = -0.5
    try:
        r = (1 + FACTOR * elapsed / stability) ** DECAY
        return max(0.0, min(1.0, r))
    except Exception:
        return 1.0


class FSRSEngine:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ──────────────────────────────────────────────────────────────────────
    # initialize_cards
    # ──────────────────────────────────────────────────────────────────────
    async def initialize_cards(
        self,
        profile_id: str,
        level: str,
        topic_slug: str | None = None,
    ) -> int:
        """
        Profil için ilk kart setini oluşturur.
        Zaten var olan kartları atlar (INSERT OR IGNORE).
        Döner: eklenen kart sayısı
        """
        topic_filter = ""
        params: dict = {"profile_id": profile_id, "level": level}

        if topic_slug:
            topic_filter = "AND t.slug = :topic_slug"
            params["topic_slug"] = topic_slug

        result = await self.db.execute(text(f"""
            INSERT OR IGNORE INTO fsrs_cards
              (profile_id, word_id, stability, difficulty, retrievability,
               state, due, reps, lapses, created_at)
            SELECT
              :profile_id,
              w.id,
              0.0, 5.0, 1.0,
              'new',
              datetime('now'),
              0, 0,
              datetime('now')
            FROM words w
            LEFT JOIN topics t ON w.topic_id = t.id
            WHERE w.level = :level
              {topic_filter}
        """), params)
        await self.db.commit()
        count = result.rowcount
        logger.info(f"initialize_cards: {count} kart eklendi "
                    f"(profile={profile_id}, level={level}, topic={topic_slug})")
        return count

    # ──────────────────────────────────────────────────────────────────────
    # get_due_cards
    # ──────────────────────────────────────────────────────────────────────
    async def get_due_cards(
        self,
        profile_id: str,
        topic_slug: str | None = None,
        level: str | None = None,
        limit: int = 10,
        include_new: bool = True,
    ) -> list[dict]:
        """
        Bugün tekrar edilmesi gereken kelimeleri döner.
        Önce review/relearning kartlar, sonra yeni kartlar.
        """
        topic_filter = "AND t.slug = :topic_slug" if topic_slug else ""
        level_filter = "AND w.level = :level" if level else ""

        params: dict = {
            "profile_id": profile_id,
            "now": _now().isoformat(),
            "limit": limit,
        }
        if topic_slug:
            params["topic_slug"] = topic_slug
        if level:
            params["level"] = level

        # Önce review/relearning (due olanlar)
        due_rows = await self.db.execute(text(f"""
            SELECT
              w.id, w.word, w.article, w.plural, w.word_type,
              w.translation_tr, w.example_de, w.level,
              t.slug as topic_slug, t.name_tr as topic_name,
              fc.id as card_id, fc.stability, fc.difficulty,
              fc.state, fc.due, fc.reps, fc.lapses, fc.last_review
            FROM fsrs_cards fc
            JOIN words w ON fc.word_id = w.id
            LEFT JOIN topics t ON w.topic_id = t.id
            WHERE fc.profile_id = :profile_id
              AND fc.state IN ('learning','review','relearning')
              AND fc.due <= :now
              {topic_filter}
              {level_filter}
            ORDER BY fc.due ASC
            LIMIT :limit
        """), params)
        rows = due_rows.fetchall()

        # Yeni kartlar (günde maks 10)
        if include_new and len(rows) < limit:
            new_limit = min(10, limit - len(rows))
            params["new_limit"] = new_limit
            new_rows = await self.db.execute(text(f"""
                SELECT
                  w.id, w.word, w.article, w.plural, w.word_type,
                  w.translation_tr, w.example_de, w.level,
                  t.slug as topic_slug, t.name_tr as topic_name,
                  fc.id as card_id, fc.stability, fc.difficulty,
                  fc.state, fc.due, fc.reps, fc.lapses, fc.last_review
                FROM fsrs_cards fc
                JOIN words w ON fc.word_id = w.id
                LEFT JOIN topics t ON w.topic_id = t.id
                WHERE fc.profile_id = :profile_id
                  AND fc.state = 'new'
                  {topic_filter}
                  {level_filter}
                ORDER BY RANDOM()
                LIMIT :new_limit
            """), params)
            rows = list(rows) + list(new_rows.fetchall())

        return [self._row_to_dict(r) for r in rows]

    # ──────────────────────────────────────────────────────────────────────
    # record_review
    # ──────────────────────────────────────────────────────────────────────
    async def record_review(
        self,
        profile_id: str,
        word_id: int,
        rating: int,  # 1=Again, 2=Hard, 3=Good, 4=Easy
    ) -> dict:
        """
        Öğrencinin bir kelimeyi değerlendirmesi → FSRS kart güncelleme.
        Kart henüz yoksa otomatik oluşturur.
        """
        fsrs_rating = RATING_MAP.get(rating, Rating.Good)

        # Mevcut kart durumunu oku
        result = await self.db.execute(text("""
            SELECT id, state, stability, difficulty, due, last_review, reps, lapses
            FROM fsrs_cards
            WHERE profile_id = :profile_id AND word_id = :word_id
        """), {"profile_id": profile_id, "word_id": word_id})
        row = result.fetchone()

        # fsrs Card objesi oluştur
        fsrs_card = FSRSCard()
        if row:
            card_id = row[0]
            # DB state → fsrs State
            state_name = row[1]
            if state_name == "review":
                fsrs_card.state = State.Review
            elif state_name == "relearning":
                fsrs_card.state = State.Relearning
            else:
                fsrs_card.state = State.Learning

            fsrs_card.stability = row[2] or None
            fsrs_card.difficulty = row[3] or None
            fsrs_card.due = _to_dt(row[4])
            fsrs_card.last_review = _to_dt(row[5]) if row[5] else None
        else:
            card_id = None  # Yeni kart oluşacak

        # FSRS algoritması çalıştır
        updated_card, review_log = _scheduler.review_card(fsrs_card, fsrs_rating)

        new_state = STATE_NAMES.get(updated_card.state.value, "learning")
        new_reps = (row[6] + 1) if row else 1
        new_lapses = (row[7] + 1) if (row and rating == 1) else (row[7] if row else 0)
        now_str = _now().isoformat()

        if card_id:
            await self.db.execute(text("""
                UPDATE fsrs_cards SET
                  stability = :stability,
                  difficulty = :difficulty,
                  retrievability = :retrievability,
                  state = :state,
                  due = :due,
                  reps = :reps,
                  lapses = :lapses,
                  last_review = :last_review
                WHERE id = :card_id
            """), {
                "stability": updated_card.stability,
                "difficulty": updated_card.difficulty,
                "retrievability": _retrievability(updated_card.stability, _now()),
                "state": new_state,
                "due": updated_card.due.isoformat(),
                "reps": new_reps,
                "lapses": new_lapses,
                "last_review": now_str,
                "card_id": card_id,
            })
        else:
            # Yeni kart — kelime DB'de var mı kontrol et
            await self.db.execute(text("""
                INSERT OR REPLACE INTO fsrs_cards
                  (profile_id, word_id, stability, difficulty, retrievability,
                   state, due, reps, lapses, last_review, created_at)
                VALUES
                  (:profile_id, :word_id, :stability, :difficulty, :retrievability,
                   :state, :due, :reps, :lapses, :last_review, :now)
            """), {
                "profile_id": profile_id,
                "word_id": word_id,
                "stability": updated_card.stability,
                "difficulty": updated_card.difficulty,
                "retrievability": _retrievability(updated_card.stability, _now()),
                "state": new_state,
                "due": updated_card.due.isoformat(),
                "reps": new_reps,
                "lapses": new_lapses,
                "last_review": now_str,
                "now": now_str,
            })

        await self.db.commit()
        logger.debug(f"record_review: word={word_id} rating={rating} → state={new_state} "
                     f"due={updated_card.due.date()}")

        return {
            "word_id": word_id,
            "rating": rating,
            "new_state": new_state,
            "next_review": updated_card.due.isoformat(),
            "stability": round(updated_card.stability or 0, 2),
            "difficulty": round(updated_card.difficulty or 0, 2),
        }

    # ──────────────────────────────────────────────────────────────────────
    # get_stats
    # ──────────────────────────────────────────────────────────────────────
    async def get_stats(
        self,
        profile_id: str,
        level: str | None = None,
    ) -> dict:
        """
        Öğrenme sürecinin tam istatistiği.
        Motivasyon geri bildirimi için kullanılır.

        Döner:
        {
          due_today: 5,
          new: 23,
          learning: 8,
          review: 15,
          relearning: 2,
          mastered: 102,      # state=review AND lapses=0 AND stability>21
          total_cards: 150,
          retention_rate: 0.87,
          streak_days: 5,
          weak_words: [...],  # lapses >= 3
          motivation_message: "Harika! ..."
        }
        """
        level_filter = "AND w.level = :level" if level else ""
        params: dict = {"profile_id": profile_id, "now": _now().isoformat()}
        if level:
            params["level"] = level

        # Temel istatistikler
        stats_result = await self.db.execute(text(f"""
            SELECT
              SUM(CASE WHEN fc.state IN ('learning','review','relearning')
                            AND fc.due <= :now THEN 1 ELSE 0 END) as due_today,
              SUM(CASE WHEN fc.state = 'new' THEN 1 ELSE 0 END) as new_count,
              SUM(CASE WHEN fc.state = 'learning' THEN 1 ELSE 0 END) as learning_count,
              SUM(CASE WHEN fc.state = 'review' THEN 1 ELSE 0 END) as review_count,
              SUM(CASE WHEN fc.state = 'relearning' THEN 1 ELSE 0 END) as relearning_count,
              SUM(CASE WHEN fc.state = 'review'
                            AND fc.lapses = 0
                            AND fc.stability >= 21 THEN 1 ELSE 0 END) as mastered_count,
              COUNT(*) as total_cards,
              AVG(CASE WHEN fc.reps > 0 THEN fc.retrievability ELSE NULL END) as avg_retention
            FROM fsrs_cards fc
            JOIN words w ON fc.word_id = w.id
            WHERE fc.profile_id = :profile_id
              {level_filter}
        """), params)
        row = stats_result.fetchone()

        due_today = int(row[0] or 0)
        new_count = int(row[1] or 0)
        learning_count = int(row[2] or 0)
        review_count = int(row[3] or 0)
        relearning_count = int(row[4] or 0)
        mastered_count = int(row[5] or 0)
        total_cards = int(row[6] or 0)
        retention_rate = round(float(row[7] or 0), 2)

        # Zayıf kelimeler (çok tekrar gerektiren)
        weak_result = await self.db.execute(text(f"""
            SELECT w.article, w.word, w.translation_tr, fc.lapses
            FROM fsrs_cards fc
            JOIN words w ON fc.word_id = w.id
            WHERE fc.profile_id = :profile_id
              AND fc.lapses >= 3
              {level_filter}
            ORDER BY fc.lapses DESC
            LIMIT 5
        """), params)
        weak_words = [
            {
                "word": f"{r[0] or ''} {r[1]}".strip(),
                "translation": r[2],
                "lapses": r[3],
            }
            for r in weak_result.fetchall()
        ]

        # Streak hesaplama (son kaç gün ardışık review yapıldı)
        streak = await self._calculate_streak(profile_id)

        # Motivasyon mesajı
        msg = self._motivation_message(
            due_today=due_today,
            mastered_count=mastered_count,
            retention_rate=retention_rate,
            streak_days=streak,
        )

        return {
            "due_today": due_today,
            "new": new_count,
            "learning": learning_count,
            "review": review_count,
            "relearning": relearning_count,
            "mastered": mastered_count,
            "total_cards": total_cards,
            "retention_rate": retention_rate,
            "streak_days": streak,
            "weak_words": weak_words,
            "motivation_message": msg,
        }

    async def _calculate_streak(self, profile_id: str) -> int:
        """Kaç gün ardışık review yapıldı?"""
        result = await self.db.execute(text("""
            SELECT DISTINCT DATE(last_review) as review_date
            FROM fsrs_cards
            WHERE profile_id = :profile_id AND last_review IS NOT NULL
            ORDER BY review_date DESC
            LIMIT 30
        """), {"profile_id": profile_id})
        dates = [row[0] for row in result.fetchall()]
        if not dates:
            return 0

        streak = 0
        today = _now().date()
        for i, d in enumerate(dates):
            try:
                review_date = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                continue
            expected = today - timedelta(days=i)
            if review_date == expected:
                streak += 1
            else:
                break
        return streak

    def _motivation_message(
        self,
        due_today: int,
        mastered_count: int,
        retention_rate: float,
        streak_days: int,
    ) -> str:
        """Öğrenci durumuna göre Türkçe motivasyon mesajı."""
        if streak_days >= 7:
            return f"{streak_days} gündür kesintisiz çalışıyorsun! Harika bir alışkanlık."
        if mastered_count >= 100:
            return f"{mastered_count} kelimeyi tam öğrendin! Sözlüğün genişliyor."
        if due_today == 0:
            return "Bugünlük tüm tekrarlar bitti. Yarın devam edelim!"
        if retention_rate >= 0.85:
            return f"Hatırlama oranın %{int(retention_rate*100)} — çok iyi gidiyorsun."
        if due_today >= 10:
            return f"Bugün {due_today} kelime seni bekliyor. Kısa bir seansta bitirebilirsin."
        return f"Bugün {due_today} kelime tekrarı var. Hadi başlayalım!"

    # ──────────────────────────────────────────────────────────────────────
    # bulk_review_from_analysis
    # ──────────────────────────────────────────────────────────────────────
    async def bulk_review_from_analysis(
        self,
        profile_id: str,
        mastered: list[str],
        struggled: list[str],
    ) -> dict:
        """
        SessionAnalyzer çıktısından toplu FSRS güncelleme.
        mastered (kelime listesi) → rating=4 (Easy)
        struggled (kelime listesi) → rating=1 (Again)
        """
        updated = 0
        for word_str in mastered:
            word = word_str.replace("der ", "").replace("die ", "").replace("das ", "").strip()
            result = await self.db.execute(text(
                "SELECT id FROM words WHERE word = :word LIMIT 1"
            ), {"word": word})
            row = result.fetchone()
            if row:
                await self.record_review(profile_id, row[0], rating=4)
                updated += 1

        for word_str in struggled:
            word = word_str.replace("der ", "").replace("die ", "").replace("das ", "").strip()
            result = await self.db.execute(text(
                "SELECT id FROM words WHERE word = :word LIMIT 1"
            ), {"word": word})
            row = result.fetchone()
            if row:
                await self.record_review(profile_id, row[0], rating=1)
                updated += 1

        logger.info(f"bulk_review: {len(mastered)} mastered, {len(struggled)} struggled → {updated} güncellendi")
        return {"updated": updated, "mastered": len(mastered), "struggled": len(struggled)}

    # ──────────────────────────────────────────────────────────────────────
    # Yardımcı
    # ──────────────────────────────────────────────────────────────────────
    def _row_to_dict(self, row) -> dict:
        stability = row[11]
        last_review = row[17]
        return {
            "word_id": row[0],
            "word": row[1],
            "article": row[2],
            "plural": row[3],
            "word_type": row[4],
            "translation_tr": row[5],
            "example_de": row[6],
            "level": row[7],
            "topic_slug": row[8],
            "topic_name": row[9],
            "card_id": row[10],
            "stability": round(float(stability), 2) if stability else 0.0,
            "difficulty": round(float(row[12]), 2) if row[12] else 5.0,
            "state": row[13],
            "due": str(row[14]),
            "reps": row[15] or 0,
            "lapses": row[16] or 0,
            "retrievability": round(_retrievability(stability, last_review), 2),
        }
