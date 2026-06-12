"""Async SQLAlchemy engine + session factory with SQLite WAL tuning."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from lt_sync.config import Settings, get_settings
from lt_sync.logging_setup import log
from lt_sync.state.models import Base


def _ensure_sqlite_dir(settings: Settings) -> None:
    db_path = settings.database_path
    if db_path is not None:
        db_path.parent.mkdir(parents=True, exist_ok=True)


def make_engine(settings: Settings | None = None):  # type: ignore[no-untyped-def]
    settings = settings or get_settings()
    _ensure_sqlite_dir(settings)
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        future=True,
    )

    if settings.database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _conn_record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


def make_sessionmaker(engine) -> async_sessionmaker[AsyncSession]:  # type: ignore[no-untyped-def]
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db(engine, settings: Settings | None = None) -> None:  # type: ignore[no-untyped-def]
    settings = settings or get_settings()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _runtime_migrate(conn, settings)


async def _runtime_migrate(conn, settings: Settings) -> None:  # type: ignore[no-untyped-def]
    """Lightweight, idempotent SQLite migrations (alembic isn't wired at runtime).

    Adds `link.ticktick_list_id` to legacy DBs and backfills existing rows to the
    legacy single list, so multi-pair poll-scoping doesn't silently drop them.
    """
    if not settings.database_url.startswith("sqlite"):
        return
    cols = {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(link)")).all()}
    if "ticktick_list_id" not in cols:
        log.info("migrating: ALTER link ADD COLUMN ticktick_list_id")
        await conn.exec_driver_sql("ALTER TABLE link ADD COLUMN ticktick_list_id VARCHAR(64)")
        await conn.execute(
            text(
                "UPDATE link SET ticktick_list_id = :legacy "
                "WHERE ticktick_list_id IS NULL"
            ),
            {"legacy": settings.ticktick_list_id},
        )
        await conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_link_ticktick_list_id ON link (ticktick_list_id)"
        )


@asynccontextmanager
async def session_scope(
    sm: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Standard transactional unit-of-work."""
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = ["Path", "init_db", "make_engine", "make_sessionmaker", "session_scope"]
