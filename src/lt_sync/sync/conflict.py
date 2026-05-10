"""Conflict resolution policy: last-writer-wins with safety nets.

A "winner" is computed from updatedAt timestamps on both sides + the canonical
hash of the previously-synced state. The canonical hash is the primary defence
against echo loops; updatedAt is the tie-breaker.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime

from lt_sync.linear.types import LinearIssue
from lt_sync.state.models import Link
from lt_sync.ticktick.types import TTTask


class Direction(enum.StrEnum):
    LINEAR_TO_TT = "linear_to_tt"
    TT_TO_LINEAR = "tt_to_linear"
    NOOP = "noop"


@dataclass(slots=True)
class Decision:
    direction: Direction
    reason: str


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def decide(
    *,
    link: Link,
    issue: LinearIssue,
    tt: TTTask,
    new_hash: str,
    inbound: Direction | None = None,
    now: datetime | None = None,
) -> Decision:
    """Decide which way to write, given current snapshots from both sides.

    Logic:
    1. If link.hash_canonical == new_hash → NOOP (already in sync).
    2. Echo: if inbound side is in its post-write window, drop the mirror event.
    3. Trust `inbound` — it's the side that observed the change first.
       TickTick OpenAPI v1 does not expose modifiedTime, so timestamp comparison
       is unreliable; the event source is the authoritative signal.
    4. Tie-break only when both sides have explicit timestamps and inbound is unknown.
    """
    now = now or datetime.now(tz=UTC)

    if link.hash_canonical and link.hash_canonical == new_hash:
        return Decision(Direction.NOOP, "hash_match")

    # Echo: post-write window on the inbound side suppresses self-loops.
    if (
        inbound is Direction.LINEAR_TO_TT
        and link.echo_until_l
        and now < _utc(link.echo_until_l)  # type: ignore[operator]
    ):
        return Decision(Direction.NOOP, "echo_l")
    if (
        inbound is Direction.TT_TO_LINEAR
        and link.echo_until_t
        and now < _utc(link.echo_until_t)  # type: ignore[operator]
    ):
        return Decision(Direction.NOOP, "echo_t")

    # Trust the inbound side — it observed the change.
    if inbound is Direction.LINEAR_TO_TT or inbound is Direction.TT_TO_LINEAR:
        return Decision(inbound, "inbound")

    # Cold-start / unknown inbound — fall back to timestamps if both sides have them.
    l_ts = _utc(issue.updated_at)
    t_ts = _utc(tt.modified_time)
    if l_ts and t_ts:
        if t_ts > l_ts:
            return Decision(Direction.TT_TO_LINEAR, f"tt_newer ({t_ts.isoformat()} > {l_ts.isoformat()})")
        if l_ts > t_ts:
            return Decision(Direction.LINEAR_TO_TT, f"linear_newer ({l_ts.isoformat()} > {t_ts.isoformat()})")
    return Decision(Direction.LINEAR_TO_TT, "fallback_default")
