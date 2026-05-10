"""Core 2-way sync engine.

Each sync action operates on a single (linear_id, ttid) pair under a per-link
asyncio mutex; the underlying SQLite session uses optimistic-locking (row_version).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lt_sync.config import Settings
from lt_sync.linear.client import LinearClient
from lt_sync.linear.types import LinearIssue, LinearLabel, LinearProject, LinearTeam
from lt_sync.logging_setup import log
from lt_sync.state import repo
from lt_sync.state.db import session_scope
from lt_sync.state.models import EventSource, Side
from lt_sync.sync import mappers
from lt_sync.sync.conflict import Decision, Direction, decide
from lt_sync.ticktick.client import TickTickClient
from lt_sync.ticktick.types import TTTask


@dataclass(slots=True)
class SyncContext:
    """Shared dependencies passed to engine operations."""

    settings: Settings
    sm: async_sessionmaker[AsyncSession]
    linear: LinearClient
    ticktick: TickTickClient
    team: LinearTeam
    project: LinearProject
    sync_label: LinearLabel
    delegated_label: LinearLabel | None
    tombstoned_label: LinearLabel | None


def _state_by_id(team: LinearTeam, state_id: str):  # type: ignore[no-untyped-def]
    return next((s for s in team.states if s.id == state_id), None)


def _label_by_name(team: LinearTeam, name: str) -> LinearLabel | None:
    for lab in team.labels:
        if lab.name.strip().lower() == name.strip().lower():
            return lab
    return None


def compute_hash(issue: LinearIssue, tt: TTTask) -> str:
    """Canonical hash from joint Linear+TickTick state."""
    block, _ = mappers.split_outside_fence(issue.description)
    inside = block.body if block else ""
    return mappers.canonical_hash(
        title=issue.title,
        description_inside_fence=inside,
        state_type=issue.state_type,
        priority=issue.priority,
        tt_status=tt.status,
        tt_priority=tt.priority,
        tt_items_signature=mappers.items_signature(tt.items),
    )


# ── TT → Linear writes ──────────────────────────────────────────────────────


async def _apply_tt_to_linear(
    ctx: SyncContext, *, issue: LinearIssue, tt: TTTask
) -> LinearIssue:
    current_state = _state_by_id(ctx.team, issue.state_id)
    target_state = mappers.pick_linear_state_from_tt(
        tt.status, states=ctx.team.states, current=current_state
    )
    target_priority = mappers.tt_priority_to_linear(
        tt.priority, current_linear_priority=issue.priority
    )
    description = mappers.merge_with_existing_description(tt, issue.description)

    label_ids = list(issue.label_ids)
    # Always carry the sync marker.
    if ctx.sync_label.id not in label_ids:
        label_ids.append(ctx.sync_label.id)

    # Delegated column → label.
    delegated_id = ctx.delegated_label.id if ctx.delegated_label else None
    column_label = mappers.map_column_to_label(_column_name(ctx, tt.column_id))
    if column_label == "Delegated" and delegated_id and delegated_id not in label_ids:
        label_ids.append(delegated_id)
    elif delegated_id and column_label != "Delegated" and delegated_id in label_ids:
        label_ids.remove(delegated_id)

    due = _tt_due_to_linear_date(tt.due_date)

    updated = await ctx.linear.update_issue(
        issue.id,
        title=tt.title,
        description=description,
        state_id=target_state.id,
        priority=target_priority,
        project_id=ctx.project.id if issue.project_id != ctx.project.id else None,
        label_ids=label_ids,
        due_date=due,
    )
    return updated


def _column_name(ctx: SyncContext, column_id: str | None) -> str | None:
    return None  # populated in upper layer that has TT project data


def _tt_due_to_linear_date(tt_due: str | None) -> str | None:
    """TickTick 'YYYY-MM-DDTHH:MM:SS.000+0000' → 'YYYY-MM-DD' for Linear."""
    if not tt_due:
        return None
    return tt_due[:10] if len(tt_due) >= 10 else None


# ── Linear → TT writes ──────────────────────────────────────────────────────


async def _apply_linear_to_tt(
    ctx: SyncContext, *, issue: LinearIssue, tt: TTTask
) -> TTTask:
    payload: dict[str, Any] = {
        "id": tt.id,
        "projectId": tt.project_id,
        "title": issue.title,
    }
    # Only mirror description if the user has changed it outside fence (preserve TT content).
    # In v1 we don't push checklist edits back; keep tt.content untouched unless title only changed.
    payload["priority"] = mappers.linear_priority_to_tt(issue.priority)

    target_status = mappers.linear_state_to_tt_status(issue.state_type)
    if target_status != tt.status:
        # status change handled below via dedicated endpoint when transitioning to completed/wontDo.
        if target_status == mappers.TT_COMPLETED:
            await ctx.ticktick.complete_task(tt.project_id, tt.id)
            tt.status = mappers.TT_COMPLETED
        elif target_status == mappers.TT_WONTDO:
            payload["status"] = mappers.TT_WONTDO
        else:
            payload["status"] = mappers.TT_OPEN

    if issue.due_date and issue.due_date != _tt_due_to_linear_date(tt.due_date):
        from datetime import date, datetime

        try:
            d = date.fromisoformat(issue.due_date)
            iso = datetime(d.year, d.month, d.day, tzinfo=UTC).isoformat(
                timespec="milliseconds"
            )
            payload["dueDate"] = iso.replace("+00:00", "+0000")
            payload["isAllDay"] = True
        except ValueError:
            pass

    updated = await ctx.ticktick.update_task(tt.id, payload)
    return updated


# ── Public entry points ─────────────────────────────────────────────────────


async def sync_pair(
    ctx: SyncContext,
    *,
    issue: LinearIssue,
    tt: TTTask,
    inbound: Direction,
    delivery_id: str,
    source: EventSource,
) -> Decision:
    """Process a single linked pair. Acquires per-link mutex."""
    lock = repo.link_lock(f"linear:{issue.id}")
    async with lock:
        async with session_scope(ctx.sm) as session:
            if await repo.event_seen(session, source, delivery_id):
                log.debug("event already processed", source=source.value, delivery_id=delivery_id)
                return Decision(Direction.NOOP, "duplicate_delivery")

            link = await repo.get_link_by_linear(session, issue.id)
            if link is None:
                link = await repo.upsert_link(
                    session,
                    linear_id=issue.id,
                    linear_ident=issue.identifier,
                    ttid=tt.id,
                    last_seen_l_updated_at=issue.updated_at,
                    last_seen_t_updated_at=tt.modified_time,
                )

            new_hash = compute_hash(issue, tt)
            decision = decide(link=link, issue=issue, tt=tt, new_hash=new_hash, inbound=inbound)

            if decision.direction is Direction.NOOP:
                log.debug("sync noop", ident=issue.identifier, ttid=tt.id, reason=decision.reason)
                await repo.record_event(
                    session,
                    source=source,
                    delivery_id=delivery_id,
                    payload_hash=new_hash,
                    action="noop",
                    link_id=link.id,
                )
                return decision

            try:
                if decision.direction is Direction.TT_TO_LINEAR:
                    updated_issue = await _apply_tt_to_linear(ctx, issue=issue, tt=tt)
                    issue = updated_issue
                    await repo.mark_synced(
                        session,
                        link,
                        new_hash=compute_hash(issue, tt),
                        side=Side.LINEAR,
                        echo_window=timedelta(seconds=ctx.settings.echo_window_sec),
                    )
                else:
                    updated_tt = await _apply_linear_to_tt(ctx, issue=issue, tt=tt)
                    tt = updated_tt
                    await repo.mark_synced(
                        session,
                        link,
                        new_hash=compute_hash(issue, tt),
                        side=Side.TICKTICK,
                        echo_window=timedelta(seconds=ctx.settings.echo_window_sec),
                    )
                action = decision.direction.value
                await repo.record_event(
                    session,
                    source=source,
                    delivery_id=delivery_id,
                    payload_hash=new_hash,
                    action=action,
                    link_id=link.id,
                )
                log.info("synced", direction=action, ident=issue.identifier, ttid=tt.id, reason=decision.reason)
            except Exception as exc:
                await repo.record_event(
                    session,
                    source=source,
                    delivery_id=delivery_id,
                    payload_hash=new_hash,
                    action="error",
                    error=str(exc)[:512],
                    link_id=link.id,
                )
                log.error("sync failed", direction=decision.direction.value, error=str(exc))
                raise
        return decision


__all__ = ["SyncContext", "compute_hash", "sync_pair"]
