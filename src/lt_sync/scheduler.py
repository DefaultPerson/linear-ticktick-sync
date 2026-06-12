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


def make_scheduler(ctxs: list[SyncContext]) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone="UTC")
    s = ctxs[0].settings
    now = datetime.now(tz=UTC)

    for i, ctx in enumerate(ctxs):
        # Stagger pairs so their TickTick fetches don't all fire at once.
        sched.add_job(
            poll_once,
            "interval",
            seconds=s.poll_interval_sec,
            args=[ctx, ctxs],
            id=f"tt_poll:{ctx.team.key}",
            max_instances=1,
            coalesce=True,
            next_run_time=now + timedelta(seconds=5 + i * 10),
        )
        sched.add_job(
            _linear_backfill,
            "interval",
            seconds=s.linear_backfill_interval_sec,
            args=[ctx],
            id=f"linear_backfill:{ctx.team.key}",
            max_instances=1,
            coalesce=True,
            next_run_time=now + timedelta(seconds=15 + i * 10),
        )

    # Token check is global (one TickTick token) — add exactly once.
    sched.add_job(
        _token_expiry_check,
        "interval",
        seconds=s.token_check_interval_sec,
        args=[ctxs[0]],
        id="token_check",
        max_instances=1,
        coalesce=True,
        next_run_time=now + timedelta(seconds=30),
    )
    return sched


async def _linear_backfill(ctx: SyncContext) -> None:
    """Periodic: scan this pair's team issues, mirror missing ones to TickTick + re-eval."""
    issues = await ctx.linear.list_team_issues(
        ctx.team.key, project_id=ctx.project.id if ctx.project else None, limit=250
    )
    log.info("linear backfill", count=len(issues))
    from lt_sync.state.models import EventSource
    from lt_sync.sync.conflict import Direction
    from lt_sync.sync.engine import sync_pair
    from lt_sync.sync.linear_to_tt import create_tt_for_linear

    for issue in issues:
        async with session_scope(ctx.sm) as session:
            link = await repo.get_link_by_linear(session, issue.id)
        if link is None:
            try:
                await create_tt_for_linear(ctx, issue)
            except Exception as exc:
                log.warning("backfill create-tt failed", ident=issue.identifier, error=str(exc))
            continue
        if link.tombstoned:
            continue
        tt = await ctx.ticktick.get_task(ctx.ticktick_list_id, link.ttid)
        if tt is None:
            continue

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
