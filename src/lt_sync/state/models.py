"""SQLAlchemy ORM models for sync state."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Side(enum.StrEnum):
    LINEAR = "linear"
    TICKTICK = "ticktick"


class EventSource(enum.StrEnum):
    LINEAR_WEBHOOK = "linear_webhook"
    LINEAR_BACKFILL = "linear_backfill"
    TT_POLL = "tt_poll"
    OUR_WRITE = "our_write"
    CLI = "cli"


class Link(Base):
    """Mapping between a Linear issue and a TickTick task."""

    __tablename__ = "link"

    id: Mapped[int] = mapped_column(primary_key=True)
    linear_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    linear_ident: Mapped[str] = mapped_column(String(32), index=True)  # e.g. HMC-277
    ttid: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    # Which TickTick list this link's task lives in — identifies the sync pair.
    # Nullable for the runtime ALTER on legacy DBs; backfilled to the legacy list.
    ticktick_list_id: Mapped[str | None] = mapped_column(String(64), index=True)

    # Canonical hash of the joint state (title+priority+status+description-fenced+items)
    hash_canonical: Mapped[str | None] = mapped_column(String(64))

    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_l_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_t_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Echo windows: ignore inbound events of the same hash before this timestamp
    echo_until_l: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    echo_until_t: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # TickTick poll miss counter (for tombstone confirmation)
    tt_miss_count: Mapped[int] = mapped_column(Integer, default=0)

    row_version: Mapped[int] = mapped_column(Integer, default=0)
    tombstoned: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.utcnow(),
        onupdate=lambda: datetime.utcnow(),
    )


class OAuthToken(Base):
    """Single-row table holding the current TickTick OAuth access token."""

    __tablename__ = "oauth_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="ticktick", unique=True)
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.utcnow(),
        onupdate=lambda: datetime.utcnow(),
    )


class EventLog(Base):
    """Idempotency log for inbound/outbound events."""

    __tablename__ = "event_log"
    __table_args__ = (
        UniqueConstraint("source", "delivery_id", name="uq_event_source_delivery"),
        Index("ix_event_processed_at", "processed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[EventSource] = mapped_column(Enum(EventSource))
    delivery_id: Mapped[str] = mapped_column(String(128))
    payload_hash: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))  # e.g. "create", "update", "skip"
    error: Mapped[str | None] = mapped_column(Text)
    link_id: Mapped[int | None] = mapped_column(Integer, index=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )


class Tombstone(Base):
    """Records deletions, so we don't re-create on the next poll."""

    __tablename__ = "tombstone"

    id: Mapped[int] = mapped_column(primary_key=True)
    side: Mapped[Side] = mapped_column(Enum(Side))
    linear_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ttid: Mapped[str | None] = mapped_column(String(64), index=True)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.utcnow()
    )
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
