# path: plugins/invite_tracker/models.py
"""Database models for invite tracking"""

from sqlalchemy import Column, DateTime, Integer, String, Boolean
from database.models import Base, utc_now



class InviteTracker(Base):
    """Discord invites as they were last listed.

    No longer written or read: the invite listing is kept in memory. The table
    stays so older databases keep their rows.
    """
    __tablename__ = "invite_tracker"

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(32), nullable=False)
    invite_code = Column(String(32), nullable=False)
    inviter_id = Column(String(32), nullable=False)  # Discord ID of who created the invite
    inviter_name = Column(String(255))
    uses = Column(Integer, default=0)
    max_uses = Column(Integer, default=0)  # 0 = unlimited
    created_at = Column(DateTime, default=utc_now)
    expires_at = Column(DateTime)
    is_temporary = Column(Boolean, default=False)


class InviteUse(Base):
    """Track who joined via which invite"""
    __tablename__ = "invite_uses"

    id = Column(Integer, primary_key=True)
    guild_id = Column(String(32), nullable=False)
    invite_code = Column(String(32), nullable=False)
    inviter_id = Column(String(32), nullable=False)  # Who created the invite
    inviter_name = Column(String(255))
    joiner_id = Column(String(32), nullable=False)  # Who joined using the invite
    joiner_name = Column(String(255))
    joined_at = Column(DateTime, default=utc_now)
    # No role is handed out on joining any more; these stay for older databases
    # and their rows.
    auto_role_assigned = Column(Boolean, default=False)
    role_id = Column(String(32))  # Role that was assigned (if any)
