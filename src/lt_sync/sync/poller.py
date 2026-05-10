"""TickTick poll loop: scans the configured list and reconciles diffs against Linear.

Detects:
- Newly created TT tasks → create Linear issue.
- Updated TT tasks → push to Linear via sync_pair.
- Disappeared TT tasks → increment miss counter; after threshold (2 misses)
  mark Linear issue as canceled (`Noted`) + label `tombstoned-from-ticktick`.
"""

from __future__ import annotations

from lt_sync.linear.types import LinearIssue
from lt_sync.logging_setup import log
from lt_sync.state import repo
from lt_sync.state.db import session_scope
from lt_sync.state.models import EventSource, Side
from lt_sync.sync import mappers
from lt_sync.sync.conflict import Direction
from lt_sync.sync.engine import SyncContext, sync_pair
from lt_sync.ticktick.types import TTProjectData, TTTask

TT_MISS_THRESHOLD = 2


def _column_lookup(data: TTProjectData) -> dict[str, str]:
    return {c.id: c.name for c in data.columns}


async def poll_once(ctx: SyncContext) -> dict[str, int]:
    log.info("poll cycle start")
    counts = {"polled": 0, "created": 0, "updated": 0, "tombstoned": 0, "errors": 0}
    try:
        data = await ctx.ticktick.get_project_data(ctx.settings.ticktick_list_id)
    except Exception as exc:
        log.error("poll TT fetch failed", error=str(exc))
        return counts
    counts["polled"] = len(data.tasks)
    cols = _column_lookup(data)
    seen_ttids: set[str] = {t.id for t in data.tasks}

    # Bulk-fetch linked Linear issues — one GraphQL call instead of N find_issue_by_id.
    try:
        all_issues = await ctx.linear.list_team_issues(
            ctx.settings.linear_team_key, project_id=ctx.project.id, limit=250
        )
    except Exception as exc:
        log.error("poll Linear fetch failed", error=str(exc))
        return counts
    issues_by_id = {i.id: i for i in all_issues}
    log.info("poll fetched", tt_count=len(data.tasks), linear_count=len(all_issues))

    for tt in data.tasks:
        try:
            await _process_tt_task(ctx, tt, cols, counts, issues_by_id)
        except Exception as exc:
            log.error("poll task failed", ttid=tt.id, error=str(exc))
            counts["errors"] += 1

    # 2) Tombstone detection: links whose ttid is missing this round.
    # NOTE: TickTick `/project/{id}/data` only returns active (status=0) tasks;
    # completed (status=2) and wontDo (status=-1) are filtered server-side.
    # Before tombstoning we probe the task directly — if it still exists, the
    # link is fine and we just reset the miss counter.
    async with session_scope(ctx.sm) as session:
        active = await repo.list_active_links(session)
        for link in active:
            if link.ttid in seen_ttids:
                await repo.reset_tt_miss(session, link)
                continue
            misses = await repo.increment_tt_miss(session, link)
            if misses < TT_MISS_THRESHOLD:
                continue
            probe = await ctx.ticktick.get_task(ctx.settings.ticktick_list_id, link.ttid)
            if probe is not None:
                # Task exists but is hidden from the active list (completed / wontDo).
                await repo.reset_tt_miss(session, link)
                continue
            await _tombstone_linear_issue(ctx, link.linear_id, link.ttid)
            await repo.add_tombstone(
                session, side=Side.TICKTICK, linear_id=link.linear_id, ttid=link.ttid,
                note="tt_missing_2x",
            )
            await repo.mark_tombstoned(session, link)
            counts["tombstoned"] += 1
    log.info("poll cycle done", **counts)
    return counts


async def _process_tt_task(
    ctx: SyncContext,
    tt: TTTask,
    cols: dict[str, str],
    counts: dict[str, int],
    issues_by_id: dict[str, LinearIssue],
) -> None:
    async with session_scope(ctx.sm) as session:
        link = await repo.get_link_by_ttid(session, tt.id)
    if link is None:
        await _create_linear_for_tt(ctx, tt, cols)
        counts["created"] += 1
        return

    issue = issues_by_id.get(link.linear_id)
    if issue is None:
        log.warning("linear issue missing for link; will tombstone", linear_id=link.linear_id)
        async with session_scope(ctx.sm) as session:
            link2 = await repo.get_link_by_ttid(session, tt.id)
            if link2:
                await repo.add_tombstone(
                    session, side=Side.LINEAR, linear_id=link.linear_id, ttid=tt.id,
                    note="linear_missing",
                )
                await repo.mark_tombstoned(session, link2)
        return

    delivery_id = f"tt:{tt.id}:{tt.modified_time.isoformat() if tt.modified_time else 'na'}"
    decision = await sync_pair(
        ctx,
        issue=issue,
        tt=tt,
        inbound=Direction.TT_TO_LINEAR,
        delivery_id=delivery_id,
        source=EventSource.TT_POLL,
    )
    if decision.direction.value != "noop":
        counts["updated"] += 1


async def _create_linear_for_tt(ctx: SyncContext, tt: TTTask, cols: dict[str, str]) -> None:
    """Create a new Linear issue mirroring this TT task and store the link."""
    state = mappers.pick_linear_state_from_tt(tt.status, states=ctx.team.states, current=None)
    priority = mappers.tt_priority_to_linear(tt.priority)
    description = mappers.render_description(tt)

    label_ids = [ctx.sync_label.id]
    column_label = mappers.map_column_to_label(cols.get(tt.column_id or ""))
    if column_label == "Delegated" and ctx.delegated_label:
        label_ids.append(ctx.delegated_label.id)

    due = tt.due_date[:10] if tt.due_date and len(tt.due_date) >= 10 else None

    issue = await ctx.linear.create_issue(
        team_id=ctx.team.id,
        title=tt.title,
        description=description,
        state_id=state.id,
        priority=priority,
        project_id=ctx.project.id,
        label_ids=label_ids,
        due_date=due,
    )
    log.info("created Linear issue from TT task", ident=issue.identifier, ttid=tt.id)
    async with session_scope(ctx.sm) as session:
        await repo.upsert_link(
            session,
            linear_id=issue.id,
            linear_ident=issue.identifier,
            ttid=tt.id,
            last_seen_l_updated_at=issue.updated_at,
            last_seen_t_updated_at=tt.modified_time,
        )


async def _tombstone_linear_issue(ctx: SyncContext, linear_id: str, ttid: str) -> None:
    issue = await ctx.linear.find_issue_by_id(linear_id)
    if issue is None:
        return
    canceled = next((s for s in ctx.team.states if s.type == "canceled"), None)
    if canceled is None:
        return
    new_label_ids = list(issue.label_ids)
    if ctx.tombstoned_label and ctx.tombstoned_label.id not in new_label_ids:
        new_label_ids.append(ctx.tombstoned_label.id)
    await ctx.linear.update_issue(linear_id, state_id=canceled.id, label_ids=new_label_ids)
    log.info("tombstoned linear issue", linear_id=linear_id, ttid=ttid)
