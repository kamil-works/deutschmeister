"""
Ses WebSocket proxy ve yardımcı HTTP endpoint'leri.

WS /api/voice/ws/{session_id}  — Gemini Live API proxy
POST /api/sessions             — Yeni session oluştur
"""
import structlog
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.db import Profile, Session
from app.schemas.structured_outputs import SessionCreate, SessionOut
from app.services.gemini_live_proxy import run_proxy

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["voice"])


# ── Session yönetimi ────────────────────────────────────────────────────────

@router.post("/api/sessions", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreate, db: AsyncSession = Depends(get_db)) -> Session:
    # Profil var mı kontrol et
    profile = await db.get(Profile, body.profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Profil bulunamadı.",
        )
    session = Session(profile_id=body.profile_id, mode=body.mode)
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session


@router.get("/api/sessions/{session_id}", response_model=SessionOut)
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)) -> Session:
    session = await db.get(Session, session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Oturum bulunamadı.",
        )
    return session


# ── WebSocket Proxy ─────────────────────────────────────────────────────────

@router.websocket("/api/voice/ws/{session_id}")
async def voice_websocket(
    websocket: WebSocket,
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Gemini Live API WebSocket proxy.
    Client bağlanır → session DB'den alınır → profil bilgisi çekilir →
    Gemini'ye setup mesajı gönderilir → çift yönlü ses akışı başlar.
    """
    log = logger.bind(session_id=session_id)

    # Session var mı?
    session = await db.get(Session, session_id)
    if not session:
        await websocket.close(code=4004, reason="Oturum bulunamadı.")
        return

    # Profil bilgisini al
    profile = await db.get(Profile, session.profile_id)
    if not profile:
        await websocket.close(code=4004, reason="Profil bulunamadı.")
        return

    await websocket.accept()
    log.info("ws_client_accepted", profile_id=profile.id, mode=session.mode)

    try:
        await run_proxy(
            client_ws=websocket,
            session_id=session_id,
            mode=session.mode,
            profile_name=profile.name,
            level=profile.level,
        )
    except WebSocketDisconnect:
        log.info("ws_client_disconnected")
    except Exception as exc:
        log.error("ws_proxy_error", error=str(exc))
    finally:
        # Session bitiş zamanını kaydet
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        session.ended_at = now
        if session.started_at:
            # SQLite driver timezone-naive olarak okuyabilir — normalize et
            started = session.started_at
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = now - started
            session.duration_s = int(elapsed.total_seconds())
        try:
            await db.commit()
        except Exception as e:
            log.error("session_close_db_error", error=str(e))
        log.info("session_ended", duration_s=session.duration_s)
