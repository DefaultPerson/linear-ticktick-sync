"""Initial reconciliation between an existing Linear team and TickTick list.

Workflow:
1. Pull all Linear team issues + all TickTick list tasks.
2. Compute candidate (linear_issue, tt_task) pairs by:
   - rapidfuzz token_set_ratio on titles >= match_threshold
   - within match_due_window_days of each other (or both null)
3. Output a TSV plan:
       ttid | tt_title | linear_ident | linear_title | score | due_diff_days | action
4. On --confirm pass:
   - For matched pairs: write Link, ensure issue is in `hm` project, add sync label,
     inject fenced block.
   - For TT-only tasks: create Linear issues.
   - For Linear-only issues already labelled `ticktick-sync` but unmatched: tombstone.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from rapidfuzz import fuzz

from lt_sync.linear.types import LinearIssue
from lt_sync.logging_setup import log
from lt_sync.state import repo
from lt_sync.state.db import session_scope
from lt_sync.state.models import Side
from lt_sync.sync import mappers
from lt_sync.sync.engine import SyncContext
from lt_sync.ticktick.types import TTTask


@dataclass(slots=True)
class MatchPlanRow:
    ttid: str
    tt_title: str
    linear_ident: str
    linear_title: str
    score: int
    due_diff_days: int | None
    action: str  # "link" | "create_linear" | "tombstone_linear"

    def to_tsv(self) -> str:
        return "\t".join(
            [
                self.ttid,
                self.tt_title.replace("\t", " "),
                self.linear_ident,
                self.linear_title.replace("\t", " "),
                str(self.score),
                "" if self.due_diff_days is None else str(self.due_diff_days),
                self.action,
            ]
        )


def _due_diff_days(linear_due: str | None, tt_due: str | None) -> int | None:
    if not linear_due or not tt_due:
        return None
    try:
        ld = date.fromisoformat(linear_due)
        td = date.fromisoformat(tt_due[:10])
    except ValueError:
        return None
    return abs((ld - td).days)


def build_match_plan(
    *,
    issues: list[LinearIssue],
    tt_tasks: list[TTTask],
    threshold: int,
    due_window: int,
    sync_label_name: str,
) -> list[MatchPlanRow]:
    rows: list[MatchPlanRow] = []
    matched_linear: set[str] = set()
    matched_tt: set[str] = set()

    # Stage 1: TT × Linear → best candidates above threshold
    for tt in tt_tasks:
        best: tuple[LinearIssue | None, int, int | None] = (None, 0, None)
        for issue in issues:
            if issue.id in matched_linear:
                continue
            score = int(fuzz.token_set_ratio(tt.title, issue.title))
            if score < threshold:
                continue
            diff = _due_diff_days(issue.due_date, tt.due_date)
            if diff is not None and diff > due_window:
                continue
            if score > best[1]:
                best = (issue, score, diff)
        if best[0] is not None:
            matched_linear.add(best[0].id)
            matched_tt.add(tt.id)
            rows.append(
                MatchPlanRow(
                    ttid=tt.id,
                    tt_title=tt.title,
                    linear_ident=best[0].identifier,
                    linear_title=best[0].title,
                    score=best[1],
                    due_diff_days=best[2],
                    action="link",
                )
            )

    # Stage 2: TT-only → create Linear
    for tt in tt_tasks:
        if tt.id in matched_tt:
            continue
        rows.append(
            MatchPlanRow(
                ttid=tt.id,
                tt_title=tt.title,
                linear_ident="",
                linear_title="",
                score=0,
                due_diff_days=None,
                action="create_linear",
            )
        )

    # Stage 3: Linear with sync label but no match → tombstone
    for issue in issues:
        if issue.id in matched_linear:
            continue
        if sync_label_name in issue.label_names:
            rows.append(
                MatchPlanRow(
                    ttid="",
                    tt_title="",
                    linear_ident=issue.identifier,
                    linear_title=issue.title,
                    score=0,
                    due_diff_days=None,
                    action="tombstone_linear",
                )
            )

    return rows


def write_plan_tsv(rows: list[MatchPlanRow], path: Path) -> None:
    path.write_text(
        "ttid\ttt_title\tlinear_ident\tlinear_title\tscore\tdue_diff_days\taction\n"
        + "\n".join(r.to_tsv() for r in rows)
        + "\n",
        encoding="utf-8",
    )


def read_plan_tsv(path: Path) -> list[MatchPlanRow]:
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")
        return [
            MatchPlanRow(
                ttid=r["ttid"],
                tt_title=r["tt_title"],
                linear_ident=r["linear_ident"],
                linear_title=r["linear_title"],
                score=int(r["score"] or 0),
                due_diff_days=int(r["due_diff_days"]) if r["due_diff_days"] else None,
                action=r["action"],
            )
            for r in reader
        ]


# ── Apply ───────────────────────────────────────────────────────────────────


async def apply_plan(
    ctx: SyncContext,
    rows: list[MatchPlanRow],
    *,
    issues_by_ident: dict[str, LinearIssue],
    tasks_by_id: dict[str, TTTask],
) -> dict[str, int]:
    counts = {"linked": 0, "created_linear": 0, "tombstoned_linear": 0, "errors": 0}
    for row in rows:
        try:
            if row.action == "link":
                await _link_existing(ctx, row, issues_by_ident, tasks_by_id)
                counts["linked"] += 1
            elif row.action == "create_linear":
                await _create_linear(ctx, row, tasks_by_id)
                counts["created_linear"] += 1
            elif row.action == "tombstone_linear":
                await _tombstone_linear(ctx, row, issues_by_ident)
                counts["tombstoned_linear"] += 1
        except Exception as exc:
            log.error("apply row failed", action=row.action, ident=row.linear_ident, ttid=row.ttid, error=str(exc))
            counts["errors"] += 1
    return counts


async def _link_existing(
    ctx: SyncContext,
    row: MatchPlanRow,
    issues_by_ident: dict[str, LinearIssue],
    tasks_by_id: dict[str, TTTask],
) -> None:
    issue = issues_by_ident.get(row.linear_ident)
    tt = tasks_by_id.get(row.ttid)
    if issue is None or tt is None:
        return

    description = mappers.render_description(tt)
    label_ids = list(issue.label_ids)
    if ctx.sync_label.id not in label_ids:
        label_ids.append(ctx.sync_label.id)

    project_id = (
        ctx.project.id if (ctx.project is not None and issue.project_id != ctx.project.id) else None
    )
    await ctx.linear.update_issue(
        issue.id,
        description=description,
        project_id=project_id,
        label_ids=label_ids,
    )

    refreshed = await ctx.linear.find_issue_by_id(issue.id) or issue
    async with session_scope(ctx.sm) as session:
        await repo.upsert_link(
            session,
            linear_id=refreshed.id,
            linear_ident=refreshed.identifier,
            ttid=tt.id,
            ticktick_list_id=ctx.ticktick_list_id,
            last_seen_l_updated_at=refreshed.updated_at,
            last_seen_t_updated_at=tt.modified_time,
        )
    log.info("linked", ident=refreshed.identifier, ttid=tt.id)


async def _create_linear(
    ctx: SyncContext,
    row: MatchPlanRow,
    tasks_by_id: dict[str, TTTask],
) -> None:
    tt = tasks_by_id.get(row.ttid)
    if tt is None:
        return
    state = mappers.pick_linear_state_from_tt(tt.status, states=ctx.team.states, current=None)
    priority = mappers.tt_priority_to_linear(tt.priority)
    description = mappers.render_description(tt)
    due = tt.due_date[:10] if tt.due_date and len(tt.due_date) >= 10 else None

    issue = await ctx.linear.create_issue(
        team_id=ctx.team.id,
        title=tt.title,
        description=description,
        state_id=state.id,
        priority=priority,
        project_id=ctx.project.id if ctx.project else None,
        label_ids=[ctx.sync_label.id],
        due_date=due,
    )
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
    log.info("created linear", ident=issue.identifier, ttid=tt.id)


async def _tombstone_linear(
    ctx: SyncContext, row: MatchPlanRow, issues_by_ident: dict[str, LinearIssue]
) -> None:
    issue = issues_by_ident.get(row.linear_ident)
    if issue is None:
        return
    canceled = next((s for s in ctx.team.states if s.type == "canceled"), None)
    label_ids = list(issue.label_ids)
    if ctx.tombstoned_label and ctx.tombstoned_label.id not in label_ids:
        label_ids.append(ctx.tombstoned_label.id)
    await ctx.linear.update_issue(
        issue.id,
        state_id=canceled.id if canceled else None,
        label_ids=label_ids,
    )
    async with session_scope(ctx.sm) as session:
        await repo.add_tombstone(session, side=Side.TICKTICK, linear_id=issue.id, ttid=None, note="orphan_label")
    log.info("tombstoned orphan", ident=issue.identifier)
