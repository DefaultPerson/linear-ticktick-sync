"""Unit tests for sync.mappers — covers the §6.3-6.5 decision tables."""

from __future__ import annotations

from lt_sync.linear.types import LinearState
from lt_sync.sync import mappers
from lt_sync.ticktick.types import TTChecklistItem, TTTask

# ─── Status ──────────────────────────────────────────────────────────────────


def _states() -> list[LinearState]:
    return [
        LinearState(id="bk-1", name="Soon", type="backlog"),
        LinearState(id="bk-2", name="New", type="backlog"),
        LinearState(id="bk-3", name="Later", type="backlog"),
        LinearState(id="us", name="Todo", type="unstarted"),
        LinearState(id="st-1", name="In Progress", type="started"),
        LinearState(id="st-2", name="In Review", type="started"),
        LinearState(id="st-3", name="Ready to deploy", type="started"),
        LinearState(id="cp-1", name="Done", type="completed"),
        LinearState(id="cp-2", name="Deployed", type="completed"),
        LinearState(id="cn", name="Noted", type="canceled"),
    ]


def test_linear_state_type_to_tt_status():
    assert mappers.linear_state_to_tt_status("backlog") == 0
    assert mappers.linear_state_to_tt_status("unstarted") == 0
    assert mappers.linear_state_to_tt_status("started") == 0
    assert mappers.linear_state_to_tt_status("completed") == 2
    assert mappers.linear_state_to_tt_status("canceled") == -1


def test_tt_completed_picks_done():
    s = mappers.pick_linear_state_from_tt(2, states=_states(), current=None)
    assert s.name == "Done" and s.type == "completed"


def test_tt_wontdo_picks_noted():
    s = mappers.pick_linear_state_from_tt(-1, states=_states(), current=None)
    assert s.name == "Noted" and s.type == "canceled"


def test_tt_open_keeps_in_progress_when_current_started():
    in_progress = next(s for s in _states() if s.name == "In Progress")
    s = mappers.pick_linear_state_from_tt(0, states=_states(), current=in_progress)
    assert s.name == "In Progress"


def test_tt_open_keeps_in_review_round_trip():
    in_review = next(s for s in _states() if s.name == "In Review")
    s = mappers.pick_linear_state_from_tt(0, states=_states(), current=in_review)
    assert s.name == "In Review"


def test_tt_open_resets_completed_to_todo_terminal_revert():
    done = next(s for s in _states() if s.name == "Done")
    s = mappers.pick_linear_state_from_tt(0, states=_states(), current=done)
    assert s.name == "Todo"  # terminal_revert


def test_tt_open_resets_canceled_to_todo():
    cn = next(s for s in _states() if s.name == "Noted")
    s = mappers.pick_linear_state_from_tt(0, states=_states(), current=cn)
    assert s.name == "Todo"


def test_tt_open_new_issue_picks_todo():
    s = mappers.pick_linear_state_from_tt(0, states=_states(), current=None)
    assert s.name == "Todo"


# ─── Priority ────────────────────────────────────────────────────────────────


def test_priority_basic_roundtrip():
    assert mappers.tt_priority_to_linear(0) == 0
    assert mappers.tt_priority_to_linear(1) == 4
    assert mappers.tt_priority_to_linear(3) == 3
    assert mappers.tt_priority_to_linear(5) == 2
    assert mappers.linear_priority_to_tt(0) == 0
    assert mappers.linear_priority_to_tt(2) == 5
    assert mappers.linear_priority_to_tt(3) == 3
    assert mappers.linear_priority_to_tt(4) == 1
    assert mappers.linear_priority_to_tt(1) == 5  # Urgent → high


def test_priority_preserves_urgent_when_tt_high():
    # Linear had "Urgent", TT is 5 → keep Urgent.
    assert mappers.tt_priority_to_linear(5, current_linear_priority=1) == 1


def test_priority_legacy_clamp():
    assert mappers.tt_priority_to_linear(2) in {3, 4}  # clamped to nearest bucket
    assert mappers.tt_priority_to_linear(7) == 2  # clamped to 5 → linear 2


# ─── Description / fenced block ──────────────────────────────────────────────


def _sample_tt() -> TTTask:
    return TTTask(
        id="abc123",
        project_id="proj1",
        title="Test",
        content="hello body",
        priority=3,
        status=0,
        due_date="2026-05-15T00:00:00.000+0000",
        column_id="colA",
        items=[
            TTChecklistItem(id="i1", title="step 1", status=1),
            TTChecklistItem(id="i2", title="step 2", status=0),
        ],
    )


def test_render_fenced_includes_marker_and_subtasks():
    tt = _sample_tt()
    rendered = mappers.render_fenced_description(tt)
    assert "<!-- ticktick-sync:start ttid=abc123 -->" in rendered
    assert "<!-- ticktick-sync:end -->" in rendered
    assert "## Subtasks" in rendered
    assert "- [x] step 1" in rendered
    assert "- [ ] step 2" in rendered


def test_split_outside_fence_returns_user_text():
    desc = "user text\n<!-- ticktick-sync:start ttid=x -->\nbody\n<!-- ticktick-sync:end -->\nmore user text"
    block, outside = mappers.split_outside_fence(desc)
    assert block is not None and block.ttid == "x"
    assert "user text" in outside and "more user text" in outside


def test_merge_preserves_outside_text():
    existing = "<!-- ticktick-sync:start ttid=abc123 -->\nold\n<!-- ticktick-sync:end -->\n\nMy private notes"
    tt = _sample_tt()
    merged = mappers.merge_with_existing_description(tt, existing)
    assert "My private notes" in merged
    assert "## Subtasks" in merged


def test_split_no_fence_returns_full_text():
    block, outside = mappers.split_outside_fence("just plain text")
    assert block is None
    assert outside == "just plain text"


# ─── Checklist parser ────────────────────────────────────────────────────────


def test_parse_checklist_lines():
    text = "## Subtasks\n- [ ] one\n- [x] two\n- [X] three\nnot a check"
    parsed = mappers.parse_checklist_lines(text)
    assert parsed == [(False, "one"), (True, "two"), (True, "three")]


# ─── Canonical hash ──────────────────────────────────────────────────────────


def test_canonical_hash_stable():
    h1 = mappers.canonical_hash(
        linear_title="t",
        description_inside_fence="d",
        state_type="started",
        priority=2,
        tt_title="t",
        tt_content="",
        tt_due_date=None,
        tt_column_id=None,
        tt_status=0,
        tt_priority=5,
        tt_items_signature="sig",
    )
    h2 = mappers.canonical_hash(
        linear_title="t",
        description_inside_fence="d",
        state_type="started",
        priority=2,
        tt_title="t",
        tt_content="",
        tt_due_date=None,
        tt_column_id=None,
        tt_status=0,
        tt_priority=5,
        tt_items_signature="sig",
    )
    assert h1 == h2 and len(h1) == 64


def test_canonical_hash_changes_with_title():
    h1 = mappers.canonical_hash(
        linear_title="t1",
        description_inside_fence="",
        state_type="unstarted",
        priority=0,
        tt_title="t1",
        tt_content="",
        tt_due_date=None,
        tt_column_id=None,
        tt_status=0,
        tt_priority=0,
        tt_items_signature="",
    )
    h2 = mappers.canonical_hash(
        linear_title="t2",
        description_inside_fence="",
        state_type="unstarted",
        priority=0,
        tt_title="t2",
        tt_content="",
        tt_due_date=None,
        tt_column_id=None,
        tt_status=0,
        tt_priority=0,
        tt_items_signature="",
    )
    assert h1 != h2


def test_items_signature_reorder_tolerant():
    a = [
        TTChecklistItem(id="a", title="alpha", status=0),
        TTChecklistItem(id="b", title="beta", status=1),
    ]
    b = list(reversed(a))
    assert mappers.items_signature(a) == mappers.items_signature(b)


def test_items_signature_status_sensitive():
    a = [TTChecklistItem(id="a", title="x", status=0)]
    b = [TTChecklistItem(id="a", title="x", status=1)]
    assert mappers.items_signature(a) != mappers.items_signature(b)


# ─── Column → label ──────────────────────────────────────────────────────────


def test_column_delegated_maps_to_delegated_label():
    assert mappers.map_column_to_label("📦 Delegated") == "Delegated"
    assert mappers.map_column_to_label("📦 Anything") == "Delegated"  # any 📦-prefixed column
    assert mappers.map_column_to_label("Rules") is None
    assert mappers.map_column_to_label(None) is None


# ─── linear_to_tt_payload ────────────────────────────────────────────────────


def test_linear_to_tt_payload_basic():
    from lt_sync.linear.types import LinearIssue

    issue = LinearIssue(
        id="x", identifier="HMC-1", title="Hi", description="some text", state_id="s1",
        state_name="Todo", state_type="unstarted", priority=2, project_id=None,
        due_date="2026-05-15",
    )
    payload = mappers.linear_to_tt_payload(issue, project_id="proj1")
    assert payload["title"] == "Hi"
    assert payload["priority"] == 5
    assert payload["isAllDay"] is True
    assert "+0000" in payload["dueDate"]
