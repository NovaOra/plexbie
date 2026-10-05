# path: database/models.py
"""SQLAlchemy models for Plexbie"""
from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


def utc_now() -> datetime:
    """Current UTC time, timezone-aware: the default for every timestamp column."""
    return datetime.now(timezone.utc)


class KeyValueStore(Base):
    """Generic key-value store for plugin data"""
    __tablename__ = "key_value_store"

    id = Column(Integer, primary_key=True)
    namespace = Column(String(64), nullable=False, index=True)
    key = Column(String(255), nullable=False, index=True)
    value = Column(Text, nullable=False)

    # Without this, kv_set's select-then-insert could race and write two rows for
    # one logical key, after which kv_get's scalar_one_or_none() raised
    # MultipleResultsFound on every subsequent read of it.
    __table_args__ = (
        UniqueConstraint("namespace", "key", name="uq_key_value_store_namespace_key"),
    )

