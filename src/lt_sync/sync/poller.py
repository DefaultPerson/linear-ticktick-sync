"""TickTick poll loop: scans one pair's list and reconciles diffs against Linear.

Each pair = (Linear team → TickTick list). `poll_once` is called per pair and is
given the full set of contexts so it can **re-home** a task whose Linear issue has
moved to another configured team (instead of falsely tombstoning it).

Detects, scoped to this pair's list/links:
- Newly created TT tasks → create Linear issue in this pair's team.
- Updated TT tasks → push to Linear via sync_pair.
- A task whose issue moved to another pair → relocate the task to that pair's list.
- Disappeared TT tasks → miss counter; after threshold mark Linear issue canceled.
"""

from __future__ import annotations

from collections.abc import Sequence

from lt_sync.linear.types import LinearIssue
from lt_sync.logging_setup import log
from lt_sync.state import repo
from lt_sync.state.db import session_scope
from lt_sync.state.models import EventSource, Link, Side
from lt_sync.sync import mappers
from lt_sync.sync.conflict import Direction
from lt_sync.sync.engine import SyncContext, ctx_for_issue, sync_pair
from lt_sync.ticktick.types import TTProjectData, TTTask

TT_MISS_THRESHOLD = 2


def _column_lookup(data: TTProjectData) -> dict[str, str]:
    return {c.id: c.name for c in data.columns}


def _ctx_for_issue(all_ctxs: Sequence[SyncContext], issue: LinearIssue) -> SyncContext | None:
    return ctx_for_issue(list(all_ctxs), issue)


async def poll_once(
    ctx: SyncContext, all_ctxs: Sequence[SyncContext] | None = None
) -> dict[str, int]:
    all_ctxs = all_ctxs or [ctx]
    log.info("poll cycle start", team=ctx.team.key, list=ctx.ticktick_list_id)
    counts = {"polled": 0, "created": 0, "updated": 0, "rehomed": 0, "tombstoned": 0, "errors": 0}
    try:
        data = await ctx.ticktick.get_project_data(ctx.ticktick_list_id)
    except Exception as exc:
        log.error("poll TT fetch failed", team=ctx.team.key, error=str(exc))
        return counts
    counts["polled"] = len(data.tasks)
    cols = _column_lookup(data)
    seen_ttids: set[str] = {t.id for t in data.tasks}

    # Bulk-fetch this pair's Linear issues — one GraphQL call instead of N.
    try:
        all_issues = await ctx.linear.list_team_issues(
            ctx.team.key,
            project_id=ctx.project.id if ctx.project else None,
            limit=250,
        )
    except Exception as exc:
        log.error("poll Linear fetch failed", team=ctx.team.key, error=str(exc))
        return counts
    issues_by_id = {i.id: i for i in all_issues}
    log.info("poll fetched", team=ctx.team.key, tt_count=len(data.tasks), linear_count=len(all_issues))

    for tt in data.tasks:
        try:
            await _process_tt_task(ctx, all_ctxs, tt, cols, counts, issues_by_id)
        except Exception as exc:
            log.error("poll task failed", ttid=tt.id, error=str(exc))
            counts["errors"] += 1

    # Hidden-status reconciliation + tombstone detection, scoped to THIS pair's links.
    # TickTick `/project/{id}/data` filters out status=2 (completed) and status=-1
    # (wontDo); a link whose ttid is missing here may be hidden-but-alive, moved to
    # another pair, or actually deleted. Probe directly to decide.
    async with session_scope(ctx.sm) as session:
        active = await repo.list_active_links(session, ctx.ticktick_list_id)
    for link in active:
        if link.ttid in seen_ttids:
            async with session_scope(ctx.sm) as session:
                fresh = await repo.get_link_by_ttid(session, link.ttid)
                if fresh:
                    await repo.reset_tt_miss(session, fresh)
            continue
        try:
            await _reconcile_missing_link(ctx, all_ctxs, link, counts, issues_by_id)
        except Exception as exc:
            log.error("missing-link reconcile failed", ttid=link.ttid, error=str(exc))
            counts["errors"] += 1

    log.info("poll cycle done", team=ctx.team.key, **counts)
    return counts


async def _process_tt_task(
    ctx: SyncContext,
    all_ctxs: Sequence[SyncContext],
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
    if issue is not None:
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
        return

    # Issue not in this pair's bulk set. Distinguish moved-team vs genuinely deleted
    # via a direct fetch — never tombstone on bulk-set absence alone.
    probe = await ctx.linear.find_issue_by_id(link.linear_id)
    if probe is not None:
        target = _ctx_for_issue(all_ctxs, probe)
        if target is not None and target.ticktick_list_id != ctx.ticktick_list_id:
            await _rehome(ctx, target, tt, probe, counts)
            return
        log.info("poll: issue out of pair scope; skipping", ident=probe.identifier, ttid=tt.id)
        return

    # Confirmed gone → tombstone this link.
    log.warning("linear issue missing for link; tombstoning", linear_id=link.linear_id)
    async with session_scope(ctx.sm) as session:
        link2 = await repo.get_link_by_ttid(session, tt.id)
        if link2:
            await repo.add_tombstone(
                session, side=Side.LINEAR, linear_id=link.linear_id, ttid=tt.id, note="linear_missing"
            )
            await repo.mark_tombstoned(session, link2)
    counts["tombstoned"] += 1


async def _reconcile_missing_link(
    ctx: SyncContext,
    all_ctxs: Sequence[SyncContext],
    link_in: Link,
    counts: dict[str, int],
    issues_by_id: dict[str, LinearIssue],
) -> None:
    """A pair-scoped link whose ttid is absent from this poll. Probe + decide."""
    linear_id = link_in.linear_id
    ttid = link_in.ttid
    probe = await ctx.ticktick.get_task(ctx.ticktick_list_id, ttid)
    if probe is not None:
        # Hidden-but-alive (completed/wontDo). Sync the hidden status to Linear,
        # unless the issue has moved to another pair → re-home instead.
        async with session_scope(ctx.sm) as session:
            fresh = await repo.get_link_by_ttid(session, ttid)
            if fresh:
                await repo.reset_tt_miss(session, fresh)
        issue = issues_by_id.get(linear_id)
        if issue is None:
            moved = await ctx.linear.find_issue_by_id(linear_id)
            target = _ctx_for_issue(all_ctxs, moved) if moved else None
            if target is not None and target.ticktick_list_id != ctx.ticktick_list_id:
                await _rehome(ctx, target, probe, moved, counts)  # type: ignore[arg-type]
            return
        delivery_id = (
            f"tt:{probe.id}:{probe.modified_time.isoformat() if probe.modified_time else 'na'}"
        )
        decision = await sync_pair(
            ctx,
            issue=issue,
            tt=probe,
            inbound=Direction.TT_TO_LINEAR,
            delivery_id=delivery_id,
            source=EventSource.TT_POLL,
        )
        if decision.direction.value != "noop":
            counts["updated"] += 1
        return

    # Task not in this list at all. Moved-to-other-pair vs deleted?
    moved = await ctx.linear.find_issue_by_id(linear_id)
    target = _ctx_for_issue(all_ctxs, moved) if moved else None
    if target is not None and target.ticktick_list_id != ctx.ticktick_list_id:
        async with session_scope(ctx.sm) as session:
            fresh = await repo.get_link_by_linear(session, linear_id)
            if fresh:
                await repo.relocate_link(
                    session,
                    fresh,
                    new_ttid=fresh.ttid,
                    new_list_id=target.ticktick_list_id,
                    new_linear_ident=moved.identifier if moved else None,
                )
        log.info("re-homed link to other pair", linear_id=linear_id, to_list=target.ticktick_list_id)
        counts["rehomed"] += 1
        return

    # Genuinely missing → miss counter → tombstone after threshold.
    async with session_scope(ctx.sm) as session:
        fresh = await repo.get_link_by_ttid(session, ttid)
        if fresh is None:
            return
        misses = await repo.increment_tt_miss(session, fresh)
    if misses < TT_MISS_THRESHOLD:
        return
    await _tombstone_linear_issue(ctx, linear_id, ttid)
    async with session_scope(ctx.sm) as session:
        fresh = await repo.get_link_by_ttid(session, ttid)
        if fresh:
            await repo.add_tombstone(
                session, side=Side.TICKTICK, linear_id=linear_id, ttid=ttid, note="tt_missing_2x"
            )
            await repo.mark_tombstoned(session, fresh)
    counts["tombstoned"] += 1


async def _rehome(
    ctx_from: SyncContext,
    ctx_to: SyncContext,
    tt: TTTask,
    issue: LinearIssue | None,
    counts: dict[str, int],
) -> None:
    """Relocate a TickTick task to another pair's list + re-home its link."""
    new_ttid = await _relocate_tt_task(ctx_from, ctx_to, tt)
    async with session_scope(ctx_from.sm) as session:
        fresh = await repo.get_link_by_ttid(session, tt.id)
        if fresh is None and issue is not None:
            fresh = await repo.get_link_by_linear(session, issue.id)
        if fresh:
            await repo.relocate_link(
                session,
                fresh,
                new_ttid=new_ttid,
                new_list_id=ctx_to.ticktick_list_id,
                new_linear_ident=issue.identifier if issue else None,
            )
    log.info(
        "re-homed task to other pair",
        ident=issue.identifier if issue else None,
        from_list=ctx_from.ticktick_list_id,
        to_list=ctx_to.ticktick_list_id,
        new_ttid=new_ttid,
    )
    counts["rehomed"] += 1


async def _relocate_tt_task(ctx_from: SyncContext, ctx_to: SyncContext, tt: TTTask) -> str:
    """Move `tt` into ctx_to's list. Try in-place projectId update; else recreate+delete.

    Returns the (possibly new) ttid.
    """
    target = ctx_to.ticktick_list_id
    try:
        await ctx_from.ticktick.update_task(tt.id, {"id": tt.id, "projectId": target})
        moved = await ctx_to.ticktick.get_task(target, tt.id)
        if moved is not None and moved.project_id == target:
            return tt.id
    except Exception as exc:
        log.warning("tt in-place move failed; recreating", ttid=tt.id, error=str(exc))

    created = await ctx_to.ticktick.create_task(_recreate_payload(tt, target, ctx_to))
    try:
        await ctx_from.ticktick.delete_task(ctx_from.ticktick_list_id, tt.id)
    except Exception as exc:
        log.warning("deleting old task after recreate failed", ttid=tt.id, error=str(exc))
    return created.id


def _recreate_payload(tt: TTTask, project_id: str, ctx_to: SyncContext) -> dict[str, object]:
    payload: dict[str, object] = {
        "projectId": project_id,
        "title": tt.title,
        "priority": tt.priority,
        "status": tt.status,
        "timeZone": tt.time_zone or ctx_to.settings.ticktick_default_tz,
    }
    if tt.content:
        payload["content"] = tt.content
    if tt.due_date:
        payload["dueDate"] = tt.due_date
        payload["isAllDay"] = tt.is_all_day
    if tt.start_date:
        payload["startDate"] = tt.start_date
    if tt.items:
        payload["items"] = [{"title": it.title, "status": it.status} for it in tt.items]
    return payload


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
        project_id=ctx.project.id if ctx.project else None,
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
            ticktick_list_id=ctx.ticktick_list_id,
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
