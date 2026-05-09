"""Typer-based CLI entry point: `lt-sync` script."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer
import uvicorn

from lt_sync.config import get_settings
from lt_sync.linear.client import LinearClient
from lt_sync.logging_setup import configure_logging
from lt_sync.state import repo
from lt_sync.state.db import init_db, make_engine, make_sessionmaker, session_scope
from lt_sync.sync import reconcile
from lt_sync.sync.setup import build_context
from lt_sync.ticktick.client import TickTickClient
from lt_sync.ticktick.oauth import build_authorize_url, make_state
from lt_sync.ticktick.token_provider import DbTokenProvider

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Linear ↔ TickTick sync CLI")
setup_app = typer.Typer(no_args_is_help=True, help="One-time setup commands")
match_app = typer.Typer(no_args_is_help=True, help="Initial reconciliation")
app.add_typer(setup_app, name="setup")
app.add_typer(match_app, name="match")


def _bootstrap():  # type: ignore[no-untyped-def]
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    return settings, engine, sm


# ── server ──────────────────────────────────────────────────────────────────


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int = typer.Option(8000, help="Bind port"),
    reload: bool = typer.Option(False, help="uvicorn --reload"),
) -> None:
    """Run the FastAPI service (webhook + scheduler)."""
    uvicorn.run("lt_sync.app:app", host=host, port=port, reload=reload, log_level="info")


# ── setup ticktick ──────────────────────────────────────────────────────────


@setup_app.command("ticktick")
def setup_ticktick(
    state: str = typer.Option(None, help="Optional override; default = random."),
) -> None:
    """Print the TickTick OAuth authorize URL.

    Open this URL in a browser, authorize your TickTick account, and the redirect
    will hit `<PUBLIC_BASE_URL>/oauth/ticktick/callback`. The service stores the token.
    """
    settings = get_settings()
    cstate = state or make_state()
    url = build_authorize_url(
        client_id=settings.ticktick_client_id.get_secret_value(),
        redirect_uri=settings.ticktick_redirect_uri,
        state=cstate,
    )
    typer.echo(f"State: {cstate}")
    typer.echo(f"Authorize URL:\n{url}")
    typer.echo("")
    typer.echo("Open the URL above, authorize, and TickTick will redirect to the callback.")


@app.command("token-status")
def token_status() -> None:
    """Print TickTick access_token expiry."""

    async def _run() -> None:
        _, engine, sm = _bootstrap()
        await init_db(engine)
        async with session_scope(sm) as session:
            tok = await repo.get_token(session, "ticktick")
        if tok is None:
            typer.echo("no token stored")
            raise typer.Exit(1)
        expires_at = tok.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        delta = expires_at - datetime.now(tz=UTC)
        typer.echo(f"expires_at: {expires_at.isoformat()} (in {delta})")
        await engine.dispose()

    asyncio.run(_run())


# ── match dry-run / confirm ─────────────────────────────────────────────────


@match_app.command("dry-run")
def match_dry_run(
    out: Path = typer.Option(Path("match-plan.tsv"), help="Output TSV path"),
) -> None:
    """Build initial title-similarity match plan; write TSV."""

    async def _run() -> None:
        settings, engine, sm = _bootstrap()
        await init_db(engine)
        linear = LinearClient(settings.linear_api_key.get_secret_value())
        ticktick = TickTickClient(DbTokenProvider(sm))
        try:
            await build_context(settings=settings, sm=sm, linear=linear, ticktick=ticktick)
            issues = await linear.list_team_issues(settings.linear_team_key, limit=250)
            tt_data = await ticktick.get_project_data(settings.ticktick_list_id)
            rows = reconcile.build_match_plan(
                issues=issues,
                tt_tasks=tt_data.tasks,
                threshold=settings.match_threshold,
                due_window=settings.match_due_window_days,
                sync_label_name=settings.sync_label_name,
            )
            reconcile.write_plan_tsv(rows, out)
            typer.echo(f"plan written: {out} ({len(rows)} rows)")
            for r in rows[:20]:
                typer.echo(f"  {r.action:18s}  score={r.score:3d}  {r.linear_ident or '—':>10s}  ↔  {r.ttid or '—'}  {r.tt_title[:60]}")
        finally:
            await linear.close()
            await ticktick.close()
            await engine.dispose()

    asyncio.run(_run())


@match_app.command("confirm")
def match_confirm(
    plan: Path = typer.Argument(Path("match-plan.tsv"), help="TSV from dry-run"),
) -> None:
    """Apply the dry-run plan: link existing pairs, create new Linear issues, tombstone orphans."""

    async def _run() -> None:
        settings, engine, sm = _bootstrap()
        await init_db(engine)
        linear = LinearClient(settings.linear_api_key.get_secret_value())
        ticktick = TickTickClient(DbTokenProvider(sm))
        try:
            ctx = await build_context(settings=settings, sm=sm, linear=linear, ticktick=ticktick)
            rows = reconcile.read_plan_tsv(plan)
            issues = await linear.list_team_issues(settings.linear_team_key, limit=250)
            tt_data = await ticktick.get_project_data(settings.ticktick_list_id)
            issues_by_ident = {i.identifier: i for i in issues}
            tasks_by_id = {t.id: t for t in tt_data.tasks}
            counts = await reconcile.apply_plan(
                ctx, rows, issues_by_ident=issues_by_ident, tasks_by_id=tasks_by_id
            )
            typer.echo(f"applied: {counts}")
        finally:
            await linear.close()
            await ticktick.close()
            await engine.dispose()

    asyncio.run(_run())


@app.command("poll-once")
def poll_once_cmd() -> None:
    """One-shot TickTick poll → reconcile → write changes (CLI replacement for scheduler)."""

    async def _run() -> None:
        from lt_sync.sync.poller import poll_once

        settings, engine, sm = _bootstrap()
        await init_db(engine)
        linear = LinearClient(settings.linear_api_key.get_secret_value())
        ticktick = TickTickClient(DbTokenProvider(sm))
        try:
            ctx = await build_context(settings=settings, sm=sm, linear=linear, ticktick=ticktick)
            counts = await poll_once(ctx)
            typer.echo(f"poll counts: {counts}")
        finally:
            await linear.close()
            await ticktick.close()
            await engine.dispose()

    asyncio.run(_run())


def main() -> None:
    app()


if __name__ == "__main__":
    main()
