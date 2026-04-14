"""
DeutschMeister v2 — FastAPI uygulaması giriş noktası.
"""
import asyncio
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.api.routes import chat, exercises, profiles, pronunciation, voice, vocabulary, slack
from app.services.daily_reminder import reminder_loop
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal, init_db
from app.services.session_analyzer import SessionAnalyzer

# Yapılandırılmış JSON loglama
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
)

logger = structlog.get_logger(__name__)
settings = get_settings()


def create_app() -> FastAPI:
    app = FastAPI(
        title="DeutschMeister API",
        description="Türkçe konuşan aileler için AI destekli Almanca öğretmeni.",
        version="2.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # CORS — development: tüm origin'leri kabul et (tunnel + localhost)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Static dosyalar — session.html + audio-processor.js
    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Router'ları kaydet
    app.include_router(profiles.router)
    app.include_router(voice.router)
    app.include_router(pronunciation.router)
    app.include_router(exercises.router)
    app.include_router(chat.router)
    app.include_router(vocabulary.router, prefix="/api", tags=["vocabulary"])
    app.include_router(slack.router, tags=["slack"])

    @app.on_event("startup")
    async def startup() -> None:
        logger.info("app_starting", cors_origins=settings.cors_origins_list)
        await init_db()
        logger.info("database_ready")
        asyncio.create_task(reminder_loop())

        # Önceki oturumdan kalan başarısız analizleri yeniden dene
        try:
            async with AsyncSessionLocal() as db:
                analyzer = SessionAnalyzer(db)
                retried = await analyzer.retry_failed_analyses()
                if retried > 0:
                    logger.info("startup_retry_analyses", retried=retried)
                else:
                    logger.info("startup_retry_analyses", retried=0, note="pending kayıt yok")
        except Exception as e:
            # Retry başarısız olsa bile uygulama ayağa kalksın
            logger.error("startup_retry_failed", error=str(e))

    @app.on_event("shutdown")
    async def shutdown() -> None:
        logger.info("app_shutting_down")

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok", "version": "2.0.0"}

    return app


app = create_app()
