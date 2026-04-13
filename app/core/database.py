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


async def init_db() -> None:
    """Tabloları oluştur (Alembic'ten önce geliştirme kolaylığı için)."""
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
