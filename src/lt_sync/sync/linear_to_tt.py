"""Create a TickTick task mirroring a Linear issue + persist the link."""

from __future__ import annotations

from datetime import UTC, date, datetime

from lt_sync.linear.types import LinearIssue
from lt_sync.logging_setup import log
from lt_sync.state import repo
from lt_sync.state.db import session_scope
from lt_sync.sync import mappers
from lt_sync.sync.engine import SyncContext


async def create_tt_for_linear(ctx: SyncContext, issue: LinearIssue) -> str | None:
    """Create a TickTick task mirroring `issue`. Returns the new TT task id."""
    payload: dict[str, object] = {
        "projectId": ctx.settings.ticktick_list_id,
        "title": issue.title,
        "content": _strip_fence(issue.description),
        "priority": mappers.linear_priority_to_tt(issue.priority),
        "timeZone": ctx.settings.ticktick_default_tz,
    }
    if issue.due_date:
        try:
            d = date.fromisoformat(issue.due_date)
            iso = datetime(d.year, d.month, d.day, tzinfo=UTC).isoformat(
                timespec="milliseconds"
            )
            payload["dueDate"] = iso.replace("+00:00", "+0000")
            payload["isAllDay"] = True
        except ValueError:
            pass
    if issue.state_type == "completed":
        payload["status"] = mappers.TT_COMPLETED
    elif issue.state_type == "canceled":
        payload["status"] = mappers.TT_WONTDO
    else:
        payload["status"] = mappers.TT_OPEN

    tt = await ctx.ticktick.create_task(payload)
    log.info("created TT task from Linear issue", ident=issue.identifier, ttid=tt.id)

    # Inject Linear-side fenced block + sync label so the next poll/webhook
    # treats it as a regular linked pair.
    label_ids = list(issue.label_ids)
    if ctx.sync_label.id not in label_ids:
        label_ids.append(ctx.sync_label.id)
    # On creation: replace Linear description entirely with the fenced block.
    # The original Linear text is already mirrored into tt.content (and thus into
    # the fence body), so preserving it outside the fence would duplicate it.
    description = mappers.render_fenced_description(tt)
    refreshed = await ctx.linear.update_issue(
        issue.id, description=description, label_ids=label_ids
    )

    async with session_scope(ctx.sm) as session:
        await repo.upsert_link(
            session,
            linear_id=refreshed.id,
            linear_ident=refreshed.identifier,
            ttid=tt.id,
            last_seen_l_updated_at=refreshed.updated_at,
            last_seen_t_updated_at=tt.modified_time,
        )
    return tt.id


def _strip_fence(description: str | None) -> str:
    """Strip our fenced block (if any) so we don't recursively echo it into TT.content."""
    _, outside = mappers.split_outside_fence(description)
    return outside.strip()
