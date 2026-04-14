"""
Async SQLAlchemy engine + session factory.
Her route'da `get_db` dependency ile async session alınır.
"""
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.models.db import Base

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
