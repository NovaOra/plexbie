"""Database models for user management"""
from datetime import datetime
from typing import Optional

from sqlalchemy import String, Integer, DateTime, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from database.models import Base, utc_now



class PlexUser(Base):
    """Track Plex users and their Discord associations"""
    __tablename__ = "plex_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Discord info
    discord_id: Mapped[Optional[int]] = mapped_column(Integer, unique=True, nullable=True, index=True)
    discord_username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Plex info
    plex_username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    plex_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # One row per Plex account: sign-in finds a person by it (database/session.py
    # adds the index to databases made before this).
    plex_user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, unique=True)

    # Activity tracking
    last_watched: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    days_inactive: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warning_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Top-watcher exemption. is_top_watcher records the standing observed on the
    # last pass, so that *dropping out* can be detected rather than merely being
    # outside the top three. exemption_lost_at is when that happened, and becomes
    # the baseline the inactivity clock counts from.
    is_top_watcher: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # An admin's "Never remove": tracked and shown, never warned or removed for inactivity.
    never_remove: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The name shown for them (Manage → People → Rename); the Plex username stays as is.
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    exemption_lost_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    def __repr__(self):
        return f"<PlexUser(plex_username=\"{self.plex_username}\", discord_id={self.discord_id}, days_inactive={self.days_inactive})>"
