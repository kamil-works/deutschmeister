"""
Async SQLAlchemy engine + session factory.
Her route'da `get_db` dependency ile async session alınır.
"""
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.db import Base

logger = logging.getLogger(__name__)

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,      # SQL logları için True yap
    connect_args={"check_same_thread": False},  # SQLite için gerekli
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    """SQLite ALTER TABLE ADD COLUMN — kolon zaten varsa sessizce geçer."""
    try:
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
    except Exception:
        pass  # kolon zaten var


async def init_db() -> None:
    """Tabloları oluştur + eksik kolonları ekle."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Kelime veritabanı tabloları (SQLAlchemy Base dışında — raw SQL)
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS topics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                slug        TEXT UNIQUE NOT NULL,
                name_de     TEXT NOT NULL,
                name_tr     TEXT NOT NULL,
                description_tr TEXT,
                min_level   TEXT DEFAULT 'A1',
                parent_slug TEXT
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS words (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                word                TEXT NOT NULL,
                article             TEXT,
                plural              TEXT,
                word_type           TEXT DEFAULT 'noun',
                base_form           TEXT,
                translation_tr      TEXT,
                example_de          TEXT,
                example_tr          TEXT,
                level               TEXT NOT NULL,
                source              TEXT DEFAULT 'goethe',
                frequency_rank      INTEGER,
                topic_id            INTEGER REFERENCES topics(id),
                has_tricky_article  INTEGER DEFAULT 0,
                created_at          TEXT DEFAULT (datetime('now')),
                UNIQUE(word, level, source)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS fsrs_cards (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id      TEXT NOT NULL REFERENCES profiles(id),
                word_id         INTEGER NOT NULL REFERENCES words(id),
                stability       REAL DEFAULT 0.0,
                difficulty      REAL DEFAULT 5.0,
                retrievability  REAL DEFAULT 1.0,
                state           TEXT DEFAULT 'new',
                due             TEXT DEFAULT (datetime('now')),
                reps            INTEGER DEFAULT 0,
                lapses          INTEGER DEFAULT 0,
                last_review     TEXT,
                created_at      TEXT DEFAULT (datetime('now')),
                UNIQUE(profile_id, word_id)
            )
        """))
        # İndeksler
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_words_level  ON words(level)",
            "CREATE INDEX IF NOT EXISTS idx_words_topic  ON words(topic_id)",
            "CREATE INDEX IF NOT EXISTS idx_words_type   ON words(word_type)",
            "CREATE INDEX IF NOT EXISTS idx_fsrs_profile ON fsrs_cards(profile_id)",
            "CREATE INDEX IF NOT EXISTS idx_fsrs_due     ON fsrs_cards(due)",
        ]:
            await conn.execute(text(idx_sql))

        # session_insights SQLAlchemy modeli dışında — raw SQL ile oluştur
        await conn.execute(text("""
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

        # Sonradan eklenen kolonlar — var olan DB'lere de ekle
        await _add_column_if_missing(conn, "profiles", "last_reminder_date", "TEXT")
        await _add_column_if_missing(conn, "profiles", "reminder_snoozed_until", "TEXT")
        await _add_column_if_missing(conn, "profiles", "reminder_state", "TEXT")
        await _add_column_if_missing(conn, "sessions", "plan_json", "TEXT")

        # Kelime veritabanı seed — tablolar boşsa otomatik doldur
        await _seed_vocabulary(conn)

        # daily_logs tablosu (SQLAlchemy Base.metadata ile oluşturulur ama garantilemek için)
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS daily_logs (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id       TEXT NOT NULL,
                log_date         TEXT NOT NULL,
                session_count    INTEGER DEFAULT 0,
                total_duration_s INTEGER DEFAULT 0,
                words_learned    INTEGER DEFAULT 0,
                words_struggled  INTEGER DEFAULT 0,
                words_mastered   INTEGER DEFAULT 0,
                session_quality  REAL,
                anxiety_signal   TEXT,
                ai_impressions   TEXT,
                error_patterns   TEXT,
                created_at       TEXT DEFAULT (datetime('now')),
                updated_at       TEXT DEFAULT (datetime('now')),
                UNIQUE(profile_id, log_date)
            )
        """))


async def _seed_vocabulary(conn) -> None:
    """
    Kelime ve bağlam tablolarını seed dosyasından doldurur.
    Yalnızca tablolar boşsa çalışır — var olan veriye dokunmaz.
    """
    word_count = (await conn.execute(text("SELECT COUNT(*) FROM words"))).scalar() or 0
    if word_count > 0:
        return  # Zaten dolu

    seed_path = Path(__file__).parent.parent.parent / "scripts" / "vocab_seed.json"
    if not seed_path.exists():
        logger.warning("vocab_seed.json bulunamadı: %s", seed_path)
        return

    logger.info("vocab_seed yükleniyor: %s", seed_path)
    with open(seed_path, encoding="utf-8") as f:
        seed = json.load(f)

    # topics
    for t in seed["topics"]:
        await conn.execute(text("""
            INSERT OR IGNORE INTO topics (slug, name_de, name_tr, description_tr, min_level, parent_slug)
            VALUES (:slug, :name_de, :name_tr, :description_tr, :min_level, :parent_slug)
        """), {
            "slug": t["slug"], "name_de": t["name_de"], "name_tr": t["name_tr"],
            "description_tr": t.get("description_tr"), "min_level": t["min_level"],
            "parent_slug": t.get("parent_slug"),
        })

    # topic slug → id map
    rows = (await conn.execute(text("SELECT id, slug FROM topics"))).fetchall()
    slug_to_id = {r[1]: r[0] for r in rows}

    # words — batch insert
    for w in seed["words"]:
        topic_id = slug_to_id.get(w.get("topic_slug"))
        await conn.execute(text("""
            INSERT OR IGNORE INTO words
                (word, article, plural, word_type, base_form, translation_tr,
                 level, example_de, example_tr, has_tricky_article, topic_id, source)
            VALUES
                (:word, :article, :plural, :word_type, :base_form, :translation_tr,
                 :level, :example_de, :example_tr, :has_tricky_article, :topic_id, 'goethe')
        """), {
            "word": w["word"], "article": w.get("article"), "plural": w.get("plural"),
            "word_type": w.get("word_type", "noun"), "base_form": w.get("base_form"),
            "translation_tr": w.get("translation_tr"), "level": w["level"],
            "example_de": w.get("example_de"), "example_tr": w.get("example_tr"),
            "has_tricky_article": w.get("has_tricky_article", 0), "topic_id": topic_id,
        })

    final = (await conn.execute(text("SELECT COUNT(*) FROM words"))).scalar()
    logger.info("vocab_seed tamamlandı: %d kelime yüklendi", final)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends() ile kullanılacak session factory."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
