"""Tests for last-writer-wins conflict resolver."""

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
    d = decide(link=link, issue=_issue(), tt=_tt(), new_hash="matching")
    assert d.direction is Direction.NOOP


def test_tt_newer_wins():
    now = datetime.now(UTC)
    d = decide(
        link=_link(),
        issue=_issue(updated_at=now - timedelta(hours=1)),
        tt=_tt(modified_time=now),
        new_hash="x",
    )
    assert d.direction is Direction.TT_TO_LINEAR


def test_linear_newer_wins():
    now = datetime.now(UTC)
    d = decide(
        link=_link(),
        issue=_issue(updated_at=now),
        tt=_tt(modified_time=now - timedelta(hours=1)),
        new_hash="x",
    )
    assert d.direction is Direction.LINEAR_TO_TT


def test_tie_prefers_linear():
    now = datetime.now(UTC)
    d = decide(
        link=_link(),
        issue=_issue(updated_at=now),
        tt=_tt(modified_time=now),
        new_hash="x",
    )
    assert d.direction is Direction.LINEAR_TO_TT


def test_only_tt_ts():
    now = datetime.now(UTC)
    d = decide(link=_link(), issue=_issue(), tt=_tt(modified_time=now), new_hash="x")
    assert d.direction is Direction.TT_TO_LINEAR


def test_only_linear_ts():
    now = datetime.now(UTC)
    d = decide(link=_link(), issue=_issue(updated_at=now), tt=_tt(), new_hash="x")
    assert d.direction is Direction.LINEAR_TO_TT


def test_echo_drops_repeat():
    now = datetime.now(UTC)
    link = _link(
        hash_="h",
        echo_until_l=now + timedelta(seconds=10),
    )
    # Inbound from TT, but the observed hash matches our last-written hash.
    d = decide(link=link, issue=_issue(), tt=_tt(), new_hash="h", inbound=Direction.TT_TO_LINEAR)
    # echo applies only when inbound side matches the stored hash; check explicit match path
    assert d.direction is Direction.NOOP


def test_no_timestamps_falls_back_to_inbound():
    d = decide(link=_link(), issue=_issue(), tt=_tt(), new_hash="x", inbound=Direction.TT_TO_LINEAR)
    assert d.direction is Direction.TT_TO_LINEAR
