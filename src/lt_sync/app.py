"""FastAPI application factory + lifespan wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from lt_sync.config import get_settings
from lt_sync.linear.client import LinearClient
from lt_sync.logging_setup import configure_logging, log
from lt_sync.scheduler import make_scheduler
from lt_sync.state import repo
from lt_sync.state.db import init_db, make_engine, make_sessionmaker, session_scope
from lt_sync.sync.setup import build_contexts
from lt_sync.ticktick.client import TickTickClient
from lt_sync.ticktick.oauth import build_authorize_url, exchange_code, make_state
from lt_sync.ticktick.token_provider import DbTokenProvider, TokenError
from lt_sync.webhook import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    await init_db(engine)

    linear = LinearClient(settings.linear_api_key.get_secret_value())
    ticktick = TickTickClient(DbTokenProvider(sm))

    app.state.settings = settings
    app.state.engine = engine
    app.state.sm = sm
    app.state.linear = linear
    app.state.ticktick = ticktick
    app.state.sync_ctxs = []
    app.state.scheduler = None

    try:
        ctxs = await build_contexts(settings=settings, sm=sm, linear=linear, ticktick=ticktick)
        app.state.sync_ctxs = ctxs
        log.info("contexts built", pairs=[(c.team.key, c.ticktick_list_id) for c in ctxs])
        try:
            await ticktick._token_provider()  # type: ignore[attr-defined]
            scheduler = make_scheduler(ctxs)
            scheduler.start()
            app.state.scheduler = scheduler
            log.info("scheduler started")
        except TokenError as e:
            log.warning("scheduler not started — authorize TickTick first", reason=str(e))
    except Exception as exc:
        log.error("startup failed; running in degraded mode", error=str(exc))

    try:
        yield
    finally:
        sched = app.state.scheduler
        if sched is not None:
            sched.shutdown(wait=False)
        await linear.close()
        await ticktick.close()
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="linear-ticktick-sync", lifespan=lifespan)
    app.include_router(webhook_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        ctxs = getattr(app.state, "sync_ctxs", [])
        return {
            "ok": True,
            "ctx_ready": bool(ctxs),
            "pairs": [{"team": c.team.key, "list": c.ticktick_list_id} for c in ctxs],
            "scheduler_running": app.state.scheduler is not None,
        }

    @app.get("/oauth/ticktick/start")
    async def oauth_start(request: Request) -> dict[str, str]:
        s = request.app.state.settings
        state = make_state()
        url = build_authorize_url(
            client_id=s.ticktick_client_id.get_secret_value(),
            redirect_uri=s.ticktick_redirect_uri,
            state=state,
        )
        request.app.state.oauth_state = state
        return {"authorize_url": url, "state": state}

    @app.get("/oauth/ticktick/callback")
    async def oauth_callback(code: str, state: str, request: Request) -> dict[str, str]:
        s = request.app.state.settings
        expected = getattr(request.app.state, "oauth_state", None)
        if expected and state != expected:
            return {"ok": "false", "error": "state_mismatch"}
        result = await exchange_code(
            code=code,
            client_id=s.ticktick_client_id.get_secret_value(),
            client_secret=s.ticktick_client_secret.get_secret_value(),
            redirect_uri=s.ticktick_redirect_uri,
        )
        async with session_scope(request.app.state.sm) as session:
            await repo.save_token(
                session,
                provider="ticktick",
                access_token=str(result["access_token"]),
                expires_at=result["expires_at"],  # type: ignore[arg-type]
                refresh_token=result.get("refresh_token") and str(result.get("refresh_token")),
                scope=result.get("scope") and str(result.get("scope")),
            )
        log.info("ticktick token saved", expires_at=str(result["expires_at"]))
        return {"ok": "true", "expires_at": str(result["expires_at"])}

    return app


app = create_app()
