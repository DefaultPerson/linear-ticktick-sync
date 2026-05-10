"""Tests for conflict resolver: hash-match short-circuit + inbound-trust."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from lt_sync.linear.types import LinearIssue
from lt_sync.state.models import Link
from lt_sync.sync.conflict import Direction, decide
from lt_sync.ticktick.types import TTTask


def _issue(updated_at: datetime | None = None) -> LinearIssue:
    return LinearIssue(
        id="lid",
        identifier="HMC-1",
        title="t",
        description=None,
        state_id="s",
        state_name="Todo",
        state_type="unstarted",
        priority=0,
        project_id=None,
        updated_at=updated_at,
    )


def _tt(modified_time: datetime | None = None) -> TTTask:
    return TTTask(id="ttid", project_id="proj", title="t", modified_time=modified_time)


def _link(hash_: str | None = None, **kw) -> Link:  # type: ignore[no-untyped-def]
    return Link(linear_id="lid", linear_ident="HMC-1", ttid="ttid", hash_canonical=hash_, **kw)


def test_noop_when_hash_matches():
    link = _link("matching")
    d = decide(link=link, issue=_issue(), tt=_tt(), new_hash="matching", inbound=Direction.LINEAR_TO_TT)
    assert d.direction is Direction.NOOP
    assert d.reason == "hash_match"


def test_inbound_linear_wins():
    d = decide(link=_link(), issue=_issue(), tt=_tt(), new_hash="x", inbound=Direction.LINEAR_TO_TT)
    assert d.direction is Direction.LINEAR_TO_TT
    assert d.reason == "inbound"


def test_inbound_tt_wins_even_without_modified_time():
    """TickTick OpenAPI v1 doesn't expose modifiedTime; inbound must be the signal."""
    d = decide(link=_link(), issue=_issue(updated_at=datetime.now(UTC)), tt=_tt(), new_hash="x", inbound=Direction.TT_TO_LINEAR)
    assert d.direction is Direction.TT_TO_LINEAR
    assert d.reason == "inbound"


def test_echo_drops_linear_inbound_during_echo_l():
    now = datetime.now(UTC)
    link = _link(echo_until_l=now + timedelta(seconds=10))
    d = decide(link=link, issue=_issue(), tt=_tt(), new_hash="x", inbound=Direction.LINEAR_TO_TT, now=now)
    assert d.direction is Direction.NOOP
    assert d.reason == "echo_l"


def test_echo_drops_tt_inbound_during_echo_t():
    now = datetime.now(UTC)
    link = _link(echo_until_t=now + timedelta(seconds=10))
    d = decide(link=link, issue=_issue(), tt=_tt(), new_hash="x", inbound=Direction.TT_TO_LINEAR, now=now)
    assert d.direction is Direction.NOOP
    assert d.reason == "echo_t"


def test_echo_does_not_block_opposite_direction():
    """echo_until_l should only suppress LINEAR_TO_TT inbound, not TT_TO_LINEAR."""
    now = datetime.now(UTC)
    link = _link(echo_until_l=now + timedelta(seconds=10))
    d = decide(link=link, issue=_issue(), tt=_tt(), new_hash="x", inbound=Direction.TT_TO_LINEAR, now=now)
    assert d.direction is Direction.TT_TO_LINEAR


def test_cold_start_timestamp_tiebreak_tt_newer():
    now = datetime.now(UTC)
    d = decide(
        link=_link(),
        issue=_issue(updated_at=now - timedelta(hours=1)),
        tt=_tt(modified_time=now),
        new_hash="x",
        inbound=None,
    )
    assert d.direction is Direction.TT_TO_LINEAR


def test_cold_start_timestamp_tiebreak_linear_newer():
    now = datetime.now(UTC)
    d = decide(
        link=_link(),
        issue=_issue(updated_at=now),
        tt=_tt(modified_time=now - timedelta(hours=1)),
        new_hash="x",
        inbound=None,
    )
    assert d.direction is Direction.LINEAR_TO_TT


def test_cold_start_no_timestamps_falls_back_default():
    d = decide(link=_link(), issue=_issue(), tt=_tt(), new_hash="x", inbound=None)
    assert d.direction is Direction.LINEAR_TO_TT
    assert d.reason == "fallback_default"
