# path: plugins/watch_party/models.py
"""Database models for watch party tracking"""
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Boolean, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from database.models import Base, utc_now



class WatchParty(Base):
    """Track watch party sessions (alias for WatchPartySession for backwards compatibility)"""
    __tablename__ = "watch_party_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Session identification
    voice_channel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    voice_channel_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Streamer info
    streamer_discord_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    streamer_plex_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Media being watched (if detectable)
    media_title: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    media_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)  # movie, episode

    # Timing
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WatchPartyCredit(Base):
    """Accumulated watch party credits per user"""
    __tablename__ = "watch_party_credits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # User identification (store both for flexibility)
    discord_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    plex_username: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Accumulated time (in seconds)
    total_duration: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Stats
    total_sessions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_credited_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    # Unique constraint: one record per plex_username
    __table_args__ = (
        Index("ix_watch_party_credits_plex_username_unique", "plex_username", unique=True),
    )


class WatchPartyParticipant(Base):
    """Individual participation records (for detailed history)"""
    __tablename__ = "watch_party_participations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Link to session
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("watch_party_sessions.id"), nullable=False, index=True)

    # Participant
    discord_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    plex_username: Mapped[str] = mapped_column(String(255), nullable=False)

    # Timing (participant may join/leave mid-session)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    left_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Calculated credit for this participation
    duration_credited: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # seconds
