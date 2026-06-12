"""Repository layer: per-link mutex + optimistic-locking helpers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from lt_sync.state.models import EventLog, EventSource, Link, OAuthToken, Side, Tombstone

_link_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def link_lock(key: str) -> asyncio.Lock:
    """Async mutex per link key (linear_id or ttid). Process-local; safe for single-instance."""
    return _link_locks[key]


def utc_now() -> datetime:
    return datetime.now(UTC)


# ─── Link CRUD ────────────────────────────────────────────────────────────────


async def get_link_by_linear(session: AsyncSession, linear_id: str) -> Link | None:
    res = await session.execute(select(Link).where(Link.linear_id == linear_id))
    return res.scalar_one_or_none()


async def get_link_by_ttid(session: AsyncSession, ttid: str) -> Link | None:
    res = await session.execute(select(Link).where(Link.ttid == ttid))
    return res.scalar_one_or_none()


async def upsert_link(
    session: AsyncSession,
    *,
    linear_id: str,
    linear_ident: str,
    ttid: str,
    ticktick_list_id: str,
    hash_canonical: str | None = None,
    last_seen_l_updated_at: datetime | None = None,
    last_seen_t_updated_at: datetime | None = None,
) -> Link:
    link = await get_link_by_linear(session, linear_id)
    if link is None:
        link = await get_link_by_ttid(session, ttid)
    if link is None:
        link = Link(
            linear_id=linear_id,
            linear_ident=linear_ident,
            ttid=ttid,
            ticktick_list_id=ticktick_list_id,
            hash_canonical=hash_canonical,
            last_seen_l_updated_at=last_seen_l_updated_at,
            last_seen_t_updated_at=last_seen_t_updated_at,
        )
        session.add(link)
        await session.flush()
        return link
    link.linear_id = linear_id
    link.linear_ident = linear_ident
    link.ttid = ttid
    link.ticktick_list_id = ticktick_list_id
    if hash_canonical is not None:
        link.hash_canonical = hash_canonical
    if last_seen_l_updated_at is not None:
        link.last_seen_l_updated_at = last_seen_l_updated_at
    if last_seen_t_updated_at is not None:
        link.last_seen_t_updated_at = last_seen_t_updated_at
    return link


async def relocate_link(
    session: AsyncSession,
    link: Link,
    *,
    new_ttid: str,
    new_list_id: str,
    new_linear_ident: str | None = None,
) -> None:
    """Re-home a link to a different sync pair after an issue moved teams.

    Preserves `hash_canonical` + echo windows so the next sync NOOPs instead of
    re-writing both sides. Resets the TT-miss counter (the task isn't gone, it moved).
    """
    link.ttid = new_ttid
    link.ticktick_list_id = new_list_id
    if new_linear_ident is not None:
        link.linear_ident = new_linear_ident
    link.tt_miss_count = 0
    link.row_version += 1


async def mark_synced(
    session: AsyncSession,
    link: Link,
    *,
    new_hash: str,
    side: Side,
    echo_window: timedelta,
) -> None:
    """Update hash + echo window after a successful write."""
    now = utc_now()
    link.hash_canonical = new_hash
    link.last_synced_at = now
    if side is Side.LINEAR:
        link.echo_until_l = now + echo_window
    else:
        link.echo_until_t = now + echo_window
    link.row_version += 1


async def list_active_links(
    session: AsyncSession, ticktick_list_id: str | None = None
) -> list[Link]:
    stmt = select(Link).where(Link.tombstoned.is_(False))
    if ticktick_list_id is not None:
        stmt = stmt.where(Link.ticktick_list_id == ticktick_list_id)
    res = await session.execute(stmt)
    return list(res.scalars())


async def all_ttids(session: AsyncSession) -> set[str]:
    res = await session.execute(select(Link.ttid).where(Link.tombstoned.is_(False)))
    return {row[0] for row in res.all()}


async def all_linear_ids(session: AsyncSession) -> set[str]:
    res = await session.execute(select(Link.linear_id).where(Link.tombstoned.is_(False)))
    return {row[0] for row in res.all()}


async def increment_tt_miss(session: AsyncSession, link: Link) -> int:
    link.tt_miss_count += 1
    return link.tt_miss_count


async def reset_tt_miss(session: AsyncSession, link: Link) -> None:
    if link.tt_miss_count != 0:
        link.tt_miss_count = 0


async def mark_tombstoned(session: AsyncSession, link: Link) -> None:
    await session.execute(
        update(Link).where(Link.id == link.id).values(tombstoned=True, row_version=Link.row_version + 1)
    )


# ─── OAuth token ──────────────────────────────────────────────────────────────


async def get_token(session: AsyncSession, provider: str = "ticktick") -> OAuthToken | None:
    res = await session.execute(select(OAuthToken).where(OAuthToken.provider == provider))
    return res.scalar_one_or_none()


async def save_token(
    session: AsyncSession,
    *,
    provider: str,
    access_token: str,
    expires_at: datetime,
    refresh_token: str | None,
    scope: str | None,
) -> OAuthToken:
    tok = await get_token(session, provider)
    if tok is None:
        tok = OAuthToken(
            provider=provider,
            access_token=access_token,
            expires_at=expires_at,
            refresh_token=refresh_token,
            scope=scope,
        )
        session.add(tok)
    else:
        tok.access_token = access_token
        tok.expires_at = expires_at
        tok.refresh_token = refresh_token
        tok.scope = scope
    await session.flush()
    return tok


# ─── Event log (idempotency) ──────────────────────────────────────────────────


async def event_seen(
    session: AsyncSession, source: EventSource, delivery_id: str
) -> bool:
    res = await session.execute(
        select(EventLog.id).where(
            EventLog.source == source, EventLog.delivery_id == delivery_id
        )
    )
    return res.scalar_one_or_none() is not None


async def record_event(
    session: AsyncSession,
    *,
    source: EventSource,
    delivery_id: str,
    payload_hash: str | None,
    action: str | None,
    error: str | None = None,
    link_id: int | None = None,
) -> EventLog:
    evt = EventLog(
        source=source,
        delivery_id=delivery_id,
        payload_hash=payload_hash,
        action=action,
        error=error,
        link_id=link_id,
    )
    session.add(evt)
    await session.flush()
    return evt


# ─── Tombstone ────────────────────────────────────────────────────────────────


async def add_tombstone(
    session: AsyncSession,
    *,
    side: Side,
    linear_id: str | None = None,
    ttid: str | None = None,
    note: str | None = None,
) -> Tombstone:
    ts = Tombstone(side=side, linear_id=linear_id, ttid=ttid, note=note)
    session.add(ts)
    await session.flush()
    return ts


async def list_pending_tombstones(session: AsyncSession) -> list[Tombstone]:
    res = await session.execute(select(Tombstone).where(Tombstone.applied.is_(False)))
    return list(res.scalars())


__all__ = [
    "add_tombstone",
    "all_linear_ids",
    "all_ttids",
    "event_seen",
    "get_link_by_linear",
    "get_link_by_ttid",
    "get_token",
    "increment_tt_miss",
    "link_lock",
    "list_active_links",
    "list_pending_tombstones",
    "mark_synced",
    "mark_tombstoned",
    "record_event",
    "relocate_link",
    "reset_tt_miss",
    "save_token",
    "upsert_link",
    "utc_now",
]
