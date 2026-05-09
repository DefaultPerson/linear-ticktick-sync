"""APScheduler jobs: TT poll, Linear backfill, token expiry check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from lt_sync.logging_setup import log
from lt_sync.notify import maybe_pushover
from lt_sync.state import repo
from lt_sync.state.db import session_scope
from lt_sync.sync.engine import SyncContext
from lt_sync.sync.poller import poll_once


def make_scheduler(ctx: SyncContext) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    s = ctx.settings

    sched.add_job(
        poll_once,
        "interval",
        seconds=s.poll_interval_sec,
        args=[ctx],
        id="tt_poll",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(tz=UTC) + timedelta(seconds=5),
    )
    sched.add_job(
        _linear_backfill,
        "interval",
        seconds=s.linear_backfill_interval_sec,
        args=[ctx],
        id="linear_backfill",
        max_instances=1,
        coalesce=True,
    )
    sched.add_job(
        _token_expiry_check,
        "interval",
        seconds=s.token_check_interval_sec,
        args=[ctx],
        id="token_check",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(tz=UTC) + timedelta(seconds=30),
    )
    return sched


async def _linear_backfill(ctx: SyncContext) -> None:
    """Defensive: scan Linear issues with our sync label and re-evaluate against TT."""
    issues = await ctx.linear.list_team_issues(
        ctx.settings.linear_team_key, project_id=ctx.project.id, limit=250
    )
    log.info("linear backfill", count=len(issues))
    for issue in issues:
        async with session_scope(ctx.sm) as session:
            link = await repo.get_link_by_linear(session, issue.id)
        if link is None or link.tombstoned:
            continue
        tt = await ctx.ticktick.get_task(ctx.settings.ticktick_list_id, link.ttid)
        if tt is None:
            continue
        from lt_sync.state.models import EventSource
        from lt_sync.sync.conflict import Direction
        from lt_sync.sync.engine import sync_pair

        delivery = f"backfill:{issue.id}:{int(datetime.now(tz=UTC).timestamp())}"
        try:
            await sync_pair(
                ctx,
                issue=issue,
                tt=tt,
                inbound=Direction.LINEAR_TO_TT,
                delivery_id=delivery,
                source=EventSource.LINEAR_BACKFILL,
            )
        except Exception as exc:
            log.warning("backfill sync failed", ident=issue.identifier, error=str(exc))


async def _token_expiry_check(ctx: SyncContext) -> None:
    async with session_scope(ctx.sm) as session:
        tok = await repo.get_token(session, "ticktick")
    if tok is None:
        log.warning("no TickTick token stored")
        return
    expires_at = tok.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    delta = expires_at - datetime.now(tz=UTC)
    if delta < timedelta(days=1):
        msg = f"TickTick token expires in {delta} — re-authorize ASAP."
        log.error(msg)
        maybe_pushover(ctx.settings, title="TickTick token expiring", message=msg)
    elif delta < timedelta(days=7):
        msg = f"TickTick token expires in {delta.days}d. Schedule re-auth."
        log.warning(msg)
        maybe_pushover(ctx.settings, title="TickTick token soon", message=msg)
