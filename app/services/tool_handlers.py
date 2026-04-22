"""
Gemini tool calling handler'ları.

Gemini bir tool çağırdığında bu modüldeki fonksiyonlar çalışır.
Her fonksiyon DB'den gerçek veri döner — Gemini asla kelime üretemez.
Her çağrı structlog ile loglanır → test sırasında görünür.
"""
from __future__ import annotations

import structlog
from google.genai import types
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.fsrs_engine import FSRSEngine

logger = structlog.get_logger(__name__)


# ── Tool tanımları (chat + voice için ortak) ───────────────────────────────

def build_tools() -> list[types.Tool]:
    """Hem chat hem voice proxy tarafından kullanılan ortak tool listesi."""
    return [
        types.Tool(function_declarations=[
            types.FunctionDeclaration(
                name="get_vocabulary_word",
                description=(
                    "Almanca kelime veritabanından kelime çek. "
                    "Yeni bir Almanca kelime öğretmek istediğinde MUTLAKA bu tool'u çağır. "
                    "Asla kendi başına kelime veya artikel üretme."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "level": types.Schema(
                            type=types.Type.STRING,
                            description="CEFR seviyesi: A1, A2, B1 veya B2",
                        ),
                        "topic": types.Schema(
                            type=types.Type.STRING,
                            description="Konu slug (opsiyonel), örn: 'hayvanlar', 'ev_yasami'",
                        ),
                    },
                    required=["level"],
                ),
            ),
            types.FunctionDeclaration(
                name="update_word_review",
                description=(
                    "Kullanıcının bir kelimeyi ne kadar bildiğini kaydet (FSRS). "
                    "Kullanıcı bir kelimeyi doğru veya yanlış söylediğinde çağır."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "word_id": types.Schema(
                            type=types.Type.INTEGER,
                            description="get_vocabulary_word veya get_due_words'den gelen word_id",
                        ),
                        "rating": types.Schema(
                            type=types.Type.INTEGER,
                            description="1=Bilmedi/Tekrar, 2=Zor, 3=İyi/Doğru, 4=Çok kolay",
                        ),
                    },
                    required=["word_id", "rating"],
                ),
            ),
            types.FunctionDeclaration(
                name="get_due_words",
                description=(
                    "Bugün tekrar edilmesi gereken kelimeleri listele (FSRS algoritması). "
                    "Ders başında tekrar kelimelerini görmek için çağır."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "limit": types.Schema(
                            type=types.Type.INTEGER,
                            description="Maksimum kelime sayısı (varsayılan: 5)",
                        ),
                    },
                ),
            ),
        ])
    ]


# ── Tool dispatcher ────────────────────────────────────────────────────────

async def dispatch_tool(
    name: str,
    args: dict,
    db: AsyncSession,
    profile_id: str,
) -> dict:
    """Gemini'den gelen tool çağrısını ilgili handler'a yönlendir."""
    if name == "get_vocabulary_word":
        return await handle_get_vocabulary_word(
            db=db,
            profile_id=profile_id,
            level=args.get("level", "A1"),
            topic=args.get("topic"),
        )
    if name == "update_word_review":
        return await handle_update_fsrs(
            db=db,
            profile_id=profile_id,
            word_id=int(args["word_id"]),
            rating=int(args["rating"]),
        )
    if name == "get_due_words":
        return await handle_get_due_words(
            db=db,
            profile_id=profile_id,
            limit=int(args.get("limit", 5)),
        )
    logger.warning("unknown_tool_call", name=name)
    return {"error": f"Bilinmeyen tool: {name}"}


# ── Handler'lar ────────────────────────────────────────────────────────────

async def handle_get_vocabulary_word(
    db: AsyncSession,
    profile_id: str,
    level: str,
    topic: str | None = None,
) -> dict:
    """
    words tablosundan bir kelime çeker.
    Öncelik: daha önce gösterilmemiş (fsrs state='new') kelimeler.
    Döner: { word_id, word, article, plural, translation_tr, example_de, level }
    """
    topic_join = "LEFT JOIN topics t ON w.topic_id = t.id" if topic else ""
    topic_filter = "AND t.slug = :topic" if topic else ""

    params: dict = {"profile_id": profile_id, "level": level}
    if topic:
        params["topic"] = topic

    # Önce: bu profil için hiç gösterilmemiş (state=new) kelimeler
    row = await db.execute(text(f"""
        SELECT w.id, w.word, w.article, w.plural, w.translation_tr,
               w.example_de, w.level
        FROM words w
        {topic_join}
        JOIN fsrs_cards fc ON fc.word_id = w.id AND fc.profile_id = :profile_id
        WHERE w.level = :level
          AND fc.state = 'new'
          {topic_filter}
        ORDER BY RANDOM()
        LIMIT 1
    """), params)
    result = row.fetchone()

    # Yoksa seviyedeki herhangi bir kelime
    if not result:
        row = await db.execute(text(f"""
            SELECT w.id, w.word, w.article, w.plural, w.translation_tr,
                   w.example_de, w.level
            FROM words w
            {topic_join}
            WHERE w.level = :level
              {topic_filter}
            ORDER BY RANDOM()
            LIMIT 1
        """), params)
        result = row.fetchone()

    if not result:
        logger.warning("no_word_found", level=level, topic=topic, profile_id=profile_id)
        return {"error": "Bu seviye için kelime bulunamadı", "level": level}

    word_data = {
        "word_id":        result[0],
        "word":           result[1],
        "article":        result[2] or "",
        "plural":         result[3] or "",
        "translation_tr": result[4] or "",
        "example_de":     result[5] or "",
        "level":          result[6],
    }

    logger.info(
        "tool_get_word",
        profile_id=profile_id,
        word=word_data["word"],
        article=word_data["article"],
        level=level,
        topic=topic,
    )
    return word_data


async def handle_update_fsrs(
    db: AsyncSession,
    profile_id: str,
    word_id: int,
    rating: int,
) -> dict:
    """
    FSRSEngine.record_review() çağırır.
    Döner: { next_due, new_state, word }
    """
    engine = FSRSEngine(db)
    result = await engine.record_review(profile_id, word_id, rating)

    logger.info(
        "tool_fsrs_update",
        profile_id=profile_id,
        word_id=word_id,
        rating=rating,
        new_state=result.get("new_state"),
        next_review=result.get("next_review", ""),
    )
    return {
        "word_id":    word_id,
        "new_state":  result.get("new_state", ""),
        "next_review": result.get("next_review", ""),
    }


async def handle_get_due_words(
    db: AsyncSession,
    profile_id: str,
    limit: int = 5,
) -> dict:
    """
    FSRSEngine.get_due_cards() çağırır — bugün tekrar edilecek kartlar.
    Döner: { due_count, words: [{ word_id, word, article, translation_tr }] }
    """
    engine = FSRSEngine(db)
    cards = await engine.get_due_cards(profile_id, limit=limit)

    words = [
        {
            "word_id":        c["word_id"],
            "word":           c["word"],
            "article":        c.get("article", ""),
            "translation_tr": c.get("translation_tr", ""),
            "state":          c.get("state", ""),
        }
        for c in cards
    ]

    logger.info(
        "tool_get_due_words",
        profile_id=profile_id,
        due_count=len(words),
    )
    return {"due_count": len(words), "words": words}
