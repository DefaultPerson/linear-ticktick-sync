"""Pure transformations between TickTick and Linear domain objects.

All functions are side-effect free so they can be unit-tested without I/O.
Edge cases per §6.3 of the plan are encoded here.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime

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


# ─── Description / fenced block ──────────────────────────────────────────────

_FENCE_START = "<!-- ticktick-sync:start"
_FENCE_END = "<!-- ticktick-sync:end -->"
_FENCE_RE = re.compile(
    r"<!-- ticktick-sync:start(?: ttid=(?P<ttid>[^ >]+))? -->\n?(?P<body>.*?)\n?<!-- ticktick-sync:end -->",
    re.DOTALL,
)


@dataclass(slots=True)
class FencedBlock:
    ttid: str | None
    body: str  # content between markers (excluding markers themselves)


def render_fenced_body(tt: TTTask) -> str:
    """Inner content of the fenced block — what we own."""
    parts: list[str] = []
    parts.append("> Source: TickTick · 🐍HM&Trade")
    if tt.due_date:
        parts.append(f"> Due: {tt.due_date}")
    if tt.column_id:
        parts.append(f"> Column: {tt.column_id}")
    parts.append("")
    if tt.content:
        parts.append(tt.content.strip())
    if tt.items:
        parts.append("")
        parts.append("## Subtasks")
        for it in tt.items:
            mark = "x" if it.status == 1 else " "
            parts.append(f"- [{mark}] {it.title}")
    return "\n".join(parts).rstrip()


def render_fenced_description(tt: TTTask, *, existing_outside: str = "") -> str:
    """Linear description = our fenced block + (optional) user-owned text after."""
    body = render_fenced_body(tt)
    block = f"{_FENCE_START} ttid={tt.id} -->\n{body}\n{_FENCE_END}"
    if existing_outside.strip():
        return f"{block}\n\n{existing_outside.strip()}"
    return block


def split_outside_fence(description: str | None) -> tuple[FencedBlock | None, str]:
    """Return (fenced_block, outside_text). Strips the fenced block from description."""
    if not description:
        return None, ""
    m = _FENCE_RE.search(description)
    if not m:
        return None, description
    block = FencedBlock(ttid=m.group("ttid"), body=m.group("body"))
    outside = (description[: m.start()] + description[m.end() :]).strip()
    return block, outside


def merge_with_existing_description(tt: TTTask, existing: str | None) -> str:
    _, outside = split_outside_fence(existing)
    return render_fenced_description(tt, existing_outside=outside)


# ─── Checklist round-trip (TT items → markdown, markdown → diff plan) ────────


_CHECK_RE = re.compile(r"^\s*-\s+\[(?P<m>[ xX])\]\s+(?P<title>.+?)\s*$")


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


# ─── TickTick title/content from Linear (when we create new TT task) ─────────


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
        # TickTick expects ISO-8601 with offset; convert YYYY-MM-DD to start of day UTC.
        try:
            d = date.fromisoformat(issue.due_date)
            iso = datetime(d.year, d.month, d.day, tzinfo=UTC).isoformat(
                timespec="milliseconds"
            )
            payload["dueDate"] = iso.replace("+00:00", "+0000")
            payload["isAllDay"] = True
        except ValueError:
            pass
    return payload


def _strip_fenced(description: str | None) -> str:
    """Return description with our fenced block removed (so we don't recursively inject it)."""
    _, outside = split_outside_fence(description)
    return outside.strip()


# ─── Canonical hash for echo / loop prevention ───────────────────────────────


def canonical_hash(
    *,
    title: str,
    description_inside_fence: str,
    state_type: str,
    priority: int,
    tt_status: int,
    tt_priority: int,
    tt_items_signature: str,
) -> str:
    """Deterministic hash combining the syncable joint state.

    `description_inside_fence` is the content between fence markers only (not the
    full Linear description), so user edits outside the fence don't trigger sync.
    """
    payload = "|".join(
        [
            title.strip(),
            description_inside_fence.strip(),
            state_type,
            str(priority),
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
