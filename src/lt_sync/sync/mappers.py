"""Pure transformations between TickTick and Linear domain objects.

All functions are side-effect free so they can be unit-tested without I/O.
Edge cases per §6.3 of the plan are encoded here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lt_sync.linear.types import LinearIssue, LinearState
from lt_sync.ticktick.types import TTChecklistItem, TTTask

# ─── Status ──────────────────────────────────────────────────────────────────

# TickTick status codes
TT_OPEN = 0
TT_COMPLETED = 2
TT_WONTDO = -1


def linear_state_to_tt_status(state_type: str) -> int:
    """Map Linear state.type to TickTick status code."""
    if state_type == "completed":
        return TT_COMPLETED
    if state_type == "canceled":
        return TT_WONTDO
    # backlog | unstarted | started | triage → open
    return TT_OPEN


def pick_linear_state_from_tt(
    tt_status: int,
    *,
    states: list[LinearState],
    current: LinearState | None,
) -> LinearState:
    """Decide the Linear state for a given TickTick status.

    Edge cases (§6.3):
    - TT open + Linear in {backlog, unstarted, started} → keep current.
    - TT open + Linear in {completed, canceled} → reset to "Todo" (terminal_revert).
    - TT open + new (current is None) → "Todo".
    - TT completed → first state.type==completed (preferring "Done").
    - TT wontDo → first state.type==canceled (preferring "Noted").
    """
    if tt_status == TT_COMPLETED:
        return _pick_state_by_type(states, "completed", prefer_name="Done")
    if tt_status == TT_WONTDO:
        return _pick_state_by_type(states, "canceled", prefer_name="Noted")
    # tt_status == TT_OPEN (or anything else we treat as open)
    if current is not None and current.type in {"backlog", "unstarted", "started"}:
        return current
    return _pick_state_by_type(states, "unstarted", prefer_name="Todo")


def _pick_state_by_type(
    states: Iterable[LinearState], type_: str, *, prefer_name: str | None = None
) -> LinearState:
    candidates = [s for s in states if s.type == type_]
    if not candidates:
        # fallback: any state — but this shouldn't happen for HMC
        return next(iter(states))
    if prefer_name:
        for s in candidates:
            if s.name == prefer_name:
                return s
    return candidates[0]


# ─── Priority ────────────────────────────────────────────────────────────────


_TT_TO_LINEAR_PRIORITY = {0: 0, 1: 4, 3: 3, 5: 2}
_LINEAR_TO_TT_PRIORITY = {0: 0, 1: 5, 2: 5, 3: 3, 4: 1}


def tt_priority_to_linear(tt_priority: int, *, current_linear_priority: int | None = None) -> int:
    """Map TickTick priority to Linear priority.

    Special: if Linear was "Urgent" (1) and TT high (5), keep "Urgent" — TT can't represent it.
    For legacy/non-canonical TT values, clamp to nearest known bucket.
    """
    if tt_priority not in _TT_TO_LINEAR_PRIORITY:
        tt_priority = _clamp_tt_priority(tt_priority)
    target = _TT_TO_LINEAR_PRIORITY[tt_priority]
    if current_linear_priority == 1 and tt_priority == 5:
        return 1  # preserve Urgent
    return target


def linear_priority_to_tt(linear_priority: int) -> int:
    return _LINEAR_TO_TT_PRIORITY.get(linear_priority, 0)


def _clamp_tt_priority(p: int) -> int:
    buckets = [0, 1, 3, 5]
    return min(buckets, key=lambda b: abs(b - p))


# ─── Description rendering ──────────────────────────────────────────────────
#
# Linear's Markdown renderer treats HTML comments as visible text, so we can't
# hide a `<!-- ttid=… -->` marker inside the description. Identification of
# synced issues lives instead in the `ticktick-sync` label + the `link` row
# (linear_id → ttid). The description therefore contains only the user-visible
# payload from TickTick (content + checklist) and is fully rewritten on every
# sync — Linear-side edits to the description body are NOT preserved.

_LEGACY_FENCE_RE = re.compile(
    r"<!-- ticktick-sync:start(?: ttid=[^ >]+)? -->\n?(?P<body>.*?)\n?<!-- ticktick-sync:end -->",
    re.DOTALL,
)


def render_description(tt: TTTask) -> str:
    """Linear description from a TickTick task: content + checklist (no markers)."""
    parts: list[str] = []
    if tt.content:
        parts.append(tt.content.strip())
    if tt.items:
        if parts:
            parts.append("")
        parts.append("## Subtasks")
        for it in tt.items:
            mark = "x" if it.status == 1 else " "
            parts.append(f"- [{mark}] {it.title}")
    return "\n".join(parts).rstrip()


def strip_legacy_fence(description: str | None) -> str:
    """Best-effort removal of legacy <!-- ticktick-sync:* --> markers from older issues."""
    if not description:
        return ""
    m = _LEGACY_FENCE_RE.search(description)
    if not m:
        return description.strip()
    body = (m.group("body") or "").strip()
    outside = (description[: m.start()] + description[m.end() :]).strip()
    if outside:
        # Drop outside text if it duplicates the fenced body (legacy bug).
        return body if body in outside or outside in body else f"{body}\n\n{outside}"
    return body


# ─── Checklist round-trip (TT items → markdown, markdown → diff plan) ────────


_CHECK_RE = re.compile(r"^\s*-\s+\[(?P<m>[ xX])\]\s+(?P<title>.+?)\s*$")
_SUBTASKS_HEADING_RE = re.compile(r"(?m)^##\s+Subtasks\s*$")


def parse_checklist_lines(text: str) -> list[tuple[bool, str]]:
    """Extract `[ ]` / `[x]` lines into (checked, title) pairs."""
    out: list[tuple[bool, str]] = []
    for line in text.splitlines():
        m = _CHECK_RE.match(line)
        if not m:
            continue
        checked = m.group("m").lower() == "x"
        out.append((checked, m.group("title").strip()))
    return out


def split_linear_description(text: str | None) -> tuple[str, list[tuple[bool, str]]]:
    """Split a Linear description into (body, checklist).

    The description is the inverse of `render_description`: an optional body
    followed by `## Subtasks` and `- [ ]/[x]` lines. Anything before the
    heading is `body`; lines after it are parsed as a checklist.
    """
    if not text:
        return "", []
    parts = _SUBTASKS_HEADING_RE.split(text, maxsplit=1)
    body = parts[0].rstrip()
    items = parse_checklist_lines(parts[1]) if len(parts) > 1 else []
    return body, items


# ─── TickTick title/content from Linear (when we create new TT task) ─────────


def linear_date_to_tt_all_day_iso(linear_due: str, tz_name: str) -> str | None:
    """Render a Linear YYYY-MM-DD as a TickTick all-day ISO at *local* midnight.

    Why: TickTick stores `dueDate` as a full ISO with offset and decides display
    purely off that instant. A UTC midnight (`…T00:00:00.000+0000`) surfaces as
    03:00 in +03 zones — even with `isAllDay=True`. Emitting midnight in the
    user's TZ (e.g. `…T00:00:00.000+0300`) lets the UI render the bare date.
    Offset is encoded TickTick-style without the colon (`+0300`, not `+03:00`).
    """
    try:
        d = date.fromisoformat(linear_due)
    except ValueError:
        return None
    try:
        tz: ZoneInfo | type[UTC] = ZoneInfo(tz_name) if tz_name else UTC  # type: ignore[assignment]
    except ZoneInfoNotFoundError:
        tz = UTC  # type: ignore[assignment]
    iso = datetime(d.year, d.month, d.day, tzinfo=tz).isoformat(timespec="milliseconds")
    if len(iso) >= 6 and iso[-3] == ":":
        iso = iso[:-3] + iso[-2:]
    return iso


def linear_to_tt_payload(
    issue: LinearIssue,
    *,
    project_id: str,
    default_tz: str = "Europe/Moscow",
) -> dict[str, object]:
    """Build TickTick create-payload from a Linear issue."""
    payload: dict[str, object] = {
        "projectId": project_id,
        "title": issue.title,
        "content": _strip_fenced(issue.description),
        "priority": linear_priority_to_tt(issue.priority),
        "timeZone": default_tz,
    }
    if issue.due_date:
        iso = linear_date_to_tt_all_day_iso(issue.due_date, default_tz)
        if iso is not None:
            payload["dueDate"] = iso
            payload["isAllDay"] = True
    return payload


def _strip_fenced(description: str | None) -> str:
    """Strip any legacy fence markers from a description before sending to TT."""
    return strip_legacy_fence(description)


# ─── Canonical hash for echo / loop prevention ───────────────────────────────


def canonical_hash(
    *,
    linear_title: str,
    rendered_description: str,
    state_type: str,
    priority: int,
    linear_due_date: str | None = None,
    tt_title: str,
    tt_content: str,
    tt_due_date: str | None,
    tt_column_id: str | None,
    tt_status: int,
    tt_priority: int,
    tt_items_signature: str,
) -> str:
    """Deterministic hash of the syncable joint state.

    `rendered_description` is the description that *should* be in Linear given
    the current TT state (i.e. `render_description(tt)`). When this drifts from
    the actual Linear description we trigger a re-sync. Linear-side description
    edits are intentionally not preserved — the description body lives in TT.
    """
    payload = "|".join(
        [
            linear_title.strip(),
            rendered_description.strip(),
            state_type,
            str(priority),
            (linear_due_date or "")[:10],
            tt_title.strip(),
            tt_content.strip(),
            (tt_due_date or "")[:10],
            tt_column_id or "",
            str(tt_status),
            str(tt_priority),
            tt_items_signature,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def items_signature(items: list[TTChecklistItem]) -> str:
    """Stable signature of TickTick checklist (sorted by title for reorder-tolerance)."""
    parts = sorted(f"{it.title}|{it.status}" for it in items)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ─── Column → label ──────────────────────────────────────────────────────────


def map_column_to_label(column_name: str | None) -> str | None:
    """TickTick column name → Linear label name. v1: only the "Delegated" column."""
    if column_name is None:
        return None
    if column_name.strip().startswith("📦"):
        return "Delegated"
    return None
