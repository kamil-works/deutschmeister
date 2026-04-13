"""
SQLAlchemy async modelleri — CLAUDE.md §SQLite Schema ile birebir uyumlu.
Tüm ID'ler UUID string, tüm tarihler UTC datetime.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(String, default="beginner")  # beginner | intermediate | advanced
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # AI Beyin — Layer 2: Agent Expression Engine
    # Tüm değerler SessionAnalyzer tarafından her seans sonunda güncellenir
    agent_strategy: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    # Layer 1: Constitution — bu haftanın gramer hedefi (sistem veya admin yazar)
    weekly_grammar_target: Mapped[str | None] = mapped_column(String, nullable=True)
    # Slack entegrasyonu
    slack_user_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    slack_channel_id: Mapped[str | None] = mapped_column(String, nullable=True)

    sessions: Mapped[list["Session"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    phoneme_scores: Mapped[list["PhonemeScore"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    exercises: Mapped[list["Exercise"]] = relationship(back_populates="profile", cascade="all, delete-orphan")
    chat_messages: Mapped[list["ChatMessage"]] = relationship(back_populates="profile", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(String, ForeignKey("profiles.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_s: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mode: Mapped[str] = mapped_column(String, default="conversation")  # conversation | pronunciation

    profile: Mapped["Profile"] = relationship(back_populates="sessions")
    phoneme_scores: Mapped[list["PhonemeScore"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class PhonemeScore(Base):
    __tablename__ = "phoneme_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String, ForeignKey("profiles.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String, ForeignKey("sessions.id"), nullable=True)
    phoneme: Mapped[str] = mapped_column(String, nullable=False)  # ü | ö | ä | ch-ich | ch-ach | r | sch
    score: Mapped[int] = mapped_column(Integer, nullable=False)   # 0-100
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    profile: Mapped["Profile"] = relationship(back_populates="phoneme_scores")
    session: Mapped["Session | None"] = relationship(back_populates="phoneme_scores")


class ChatMessage(Base):
    """Text-to-text chat mesajları. Profil başına tüm geçmiş saklanır."""
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String, ForeignKey("profiles.id"), nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False)  # user | model
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    profile: Mapped["Profile"] = relationship(back_populates="chat_messages")


class Exercise(Base):
    __tablename__ = "exercises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[str] = mapped_column(String, ForeignKey("profiles.id"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # pronunciation | vocabulary | grammar
    content: Mapped[str] = mapped_column(Text, nullable=False)  # JSON string
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)

    profile: Mapped["Profile"] = relationship(back_populates="exercises")
