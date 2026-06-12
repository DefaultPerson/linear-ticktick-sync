"""Unit tests for multi-team (multi-pair) sync: config, routing, re-home payload, migration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from lt_sync.config import Settings, SyncPair
from lt_sync.linear.types import LinearIssue
from lt_sync.state.db import init_db, make_engine
from lt_sync.sync import poller
from lt_sync.sync.engine import ctx_for_issue
from lt_sync.ticktick.types import TTChecklistItem, TTTask

_SECRETS = {
    "linear_api_key": "lin",
    "linear_webhook_secret": "sec",
    "ticktick_client_id": "cid",
    "ticktick_client_secret": "csec",
    "ticktick_redirect_uri": "https://x/cb",
}


def _settings(**kw: object) -> Settings:
    return Settings(**{**_SECRETS, **kw})  # type: ignore[arg-type]


# ─── config: effective_pairs ──────────────────────────────────────────────────


def test_effective_pairs_legacy_fallback():
    s = _settings(linear_team_key="HMC", ticktick_list_id="LHM", linear_project_name="hm")
    pairs = s.effective_pairs
    assert len(pairs) == 1
    assert pairs[0] == SyncPair(team_key="HMC", ticktick_list_id="LHM", project_name="hm")


def test_effective_pairs_explicit():
    s = _settings(
        sync_pairs=[
            SyncPair(team_key="HMC", ticktick_list_id="L1", project_name="hm"),
            SyncPair(team_key="w3a", ticktick_list_id="L2"),
        ]
    )
    pairs = s.effective_pairs
    assert [p.team_key for p in pairs] == ["HMC", "w3a"]
    assert pairs[1].project_name is None
    assert pairs[1].ticktick_list_id == "L2"


def test_sync_pairs_parsed_from_json_env(monkeypatch: pytest.MonkeyPatch):
    for k, v in _SECRETS.items():
        monkeypatch.setenv(k.upper(), str(v))
    monkeypatch.setenv(
        "SYNC_PAIRS",
        '[{"team_key":"HMC","ticktick_list_id":"L1","project_name":"hm"},'
        '{"team_key":"w3a","ticktick_list_id":"L2"}]',
    )
    s = Settings()  # type: ignore[call-arg]
    pairs = s.effective_pairs
    assert len(pairs) == 2
    assert pairs[0].project_name == "hm"
    assert pairs[1].team_key == "w3a" and pairs[1].project_name is None


# ─── routing: ctx_for_issue ───────────────────────────────────────────────────


def _ctx(team_key: str, project_id: str | None):
    project = SimpleNamespace(id=project_id) if project_id else None
    return SimpleNamespace(
        team=SimpleNamespace(key=team_key), project=project, ticktick_list_id=f"L-{team_key}"
    )


def _issue(team_key: str | None, project_id: str | None):
    return LinearIssue(
        id="u", identifier="X-1", title="t", description=None, state_id="s",
        state_name="S", state_type="unstarted", priority=0, project_id=project_id,
        team_key=team_key,
    )


def test_routing_matches_team_and_project():
    ctxs = [_ctx("HMC", "projhm"), _ctx("w3a", None)]
    assert ctx_for_issue(ctxs, _issue("HMC", "projhm")).team.key == "HMC"
    # whole-team pair (project None) matches any project in that team
    assert ctx_for_issue(ctxs, _issue("w3a", "anything")).team.key == "w3a"


def test_routing_project_filter_excludes_other_project():
    ctxs = [_ctx("HMC", "projhm"), _ctx("w3a", None)]
    # HMC issue outside project hm belongs to no pair
    assert ctx_for_issue(ctxs, _issue("HMC", "other")) is None


def test_routing_untracked_team_and_missing_team_key():
    ctxs = [_ctx("HMC", "projhm"), _ctx("w3a", None)]
    assert ctx_for_issue(ctxs, _issue("zzz", None)) is None
    assert ctx_for_issue(ctxs, _issue(None, None)) is None


# ─── re-home payload ──────────────────────────────────────────────────────────


def test_recreate_payload_carries_fields_and_items():
    tt = TTTask(
        id="t1", project_id="old", title="Hi", content="body", priority=3, status=0,
        due_date="2026-05-15T00:00:00.000+0300", start_date="2026-05-15T00:00:00.000+0300",
        is_all_day=True, time_zone="Europe/Moscow",
        items=[TTChecklistItem(id="i1", title="a", status=1)],
    )
    ctx_to = SimpleNamespace(settings=SimpleNamespace(ticktick_default_tz="UTC"))
    p = poller._recreate_payload(tt, "newlist", ctx_to)
    assert p["projectId"] == "newlist"
    assert p["title"] == "Hi" and p["content"] == "body"
    assert p["priority"] == 3 and p["status"] == 0
    assert p["dueDate"] == "2026-05-15T00:00:00.000+0300" and p["isAllDay"] is True
    assert p["timeZone"] == "Europe/Moscow"
    assert p["items"] == [{"title": "a", "status": 1}]


def test_recreate_payload_defaults_tz_when_missing():
    tt = TTTask(id="t1", project_id="old", title="Hi", time_zone="")
    ctx_to = SimpleNamespace(settings=SimpleNamespace(ticktick_default_tz="Europe/Moscow"))
    p = poller._recreate_payload(tt, "newlist", ctx_to)
    assert p["timeZone"] == "Europe/Moscow"
    assert "dueDate" not in p and "items" not in p and "content" not in p


# ─── runtime migration: ALTER + backfill ──────────────────────────────────────


async def test_runtime_migrate_adds_column_and_backfills(tmp_path):
    db = tmp_path / "legacy.db"
    settings = _settings(
        database_url=f"sqlite+aiosqlite:///{db}", ticktick_list_id="LEGACY_LIST"
    )
    engine = make_engine(settings)
    # Simulate a legacy `link` table without ticktick_list_id + an existing row.
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            "CREATE TABLE link (id INTEGER PRIMARY KEY, linear_id VARCHAR(64), "
            "linear_ident VARCHAR(32), ttid VARCHAR(64), tombstoned BOOLEAN DEFAULT 0)"
        )
        await conn.exec_driver_sql(
            "INSERT INTO link (linear_id, linear_ident, ttid) VALUES ('u1','HMC-1','t1')"
        )

    await init_db(engine, settings)

    async with engine.connect() as conn:
        cols = {r[1] for r in (await conn.exec_driver_sql("PRAGMA table_info(link)")).all()}
        assert "ticktick_list_id" in cols
        val = (
            await conn.exec_driver_sql("SELECT ticktick_list_id FROM link WHERE ttid='t1'")
        ).scalar()
        assert val == "LEGACY_LIST"
    # Idempotent: running again must not fail or double-apply.
    await init_db(engine, settings)
    await engine.dispose()
