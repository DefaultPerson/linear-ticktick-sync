"""POST /webhook/linear handler with HMAC verification + idempotency."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from lt_sync.config import get_settings
from lt_sync.linear.webhook_verify import (
    WebhookVerifyError,
    make_delivery_id,
    reject_replay,
    verify_signature,
)
from lt_sync.logging_setup import log
from lt_sync.state import repo
from lt_sync.state.db import session_scope
from lt_sync.state.models import EventSource
from lt_sync.sync.conflict import Direction
from lt_sync.sync.engine import SyncContext, ctx_for_issue, sync_pair

router = APIRouter()


@router.post("/webhook/linear")
async def linear_webhook(
    request: Request,
    linear_signature: str | None = Header(default=None, alias="Linear-Signature"),
) -> dict[str, Any]:
    settings = get_settings()
    body = await request.body()
    try:
        verify_signature(
            body=body,
            signature_header=linear_signature,
            secret=settings.linear_webhook_secret.get_secret_value(),
        )
    except WebhookVerifyError as e:
        log.warning("webhook signature failed", reason=str(e))
        raise HTTPException(status_code=401, detail=str(e)) from e

    payload: dict[str, Any] = await request.json()
    try:
        reject_replay(payload.get("webhookTimestamp"))
    except WebhookVerifyError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    delivery_id = make_delivery_id(payload)
    ctxs: list[SyncContext] = request.app.state.sync_ctxs
    if not ctxs:
        log.error("sync_ctxs not initialised on webhook")
        raise HTTPException(status_code=503, detail="sync engine not ready")

    action = payload.get("action")
    data = payload.get("data") or {}
    type_ = payload.get("type")
    log.info("webhook received", action=action, type=type_, delivery_id=delivery_id)

    if type_ != "Issue":
        return {"ok": True, "skipped": "non_issue_event"}

    issue_id = data.get("id")
    if not isinstance(issue_id, str):
        return {"ok": True, "skipped": "missing_id"}

    sm = ctxs[0].sm
    linear = ctxs[0].linear

    # idempotency (single shared DB)
    async with session_scope(sm) as session:
        if await repo.event_seen(session, EventSource.LINEAR_WEBHOOK, delivery_id):
            return {"ok": True, "duplicate": True}

    if action == "remove":
        await _handle_remove(ctxs, issue_id, delivery_id)
        return {"ok": True, "removed": True}

    issue = await linear.find_issue_by_id(issue_id)
    if issue is None:
        return {"ok": True, "skipped": "issue_gone"}

    ctx = ctx_for_issue(ctxs, issue)
    if ctx is None:
        return {"ok": True, "skipped": "untracked_team"}

    async with session_scope(ctx.sm) as session:
        link = await repo.get_link_by_linear(session, issue_id)
    if link is None:
        # New (or moved-in) Linear issue in a tracked team — mirror to TickTick.
        from lt_sync.sync.linear_to_tt import create_tt_for_linear

        ttid = await create_tt_for_linear(ctx, issue)
        return {"ok": True, "created_tt": ttid, "team": ctx.team.key}

    tt = await ctx.ticktick.get_task(ctx.ticktick_list_id, link.ttid)
    if tt is None:
        return {"ok": True, "skipped": "tt_task_missing"}

    decision = await sync_pair(
        ctx,
        issue=issue,
        tt=tt,
        inbound=Direction.LINEAR_TO_TT,
        delivery_id=delivery_id,
        source=EventSource.LINEAR_WEBHOOK,
    )
    return {"ok": True, "direction": decision.direction.value, "reason": decision.reason}


async def _handle_remove(
    ctxs: list[SyncContext], issue_id: str, delivery_id: str
) -> None:
    """Linear issue deleted → mark TickTick task wontDo (status=-1).

    Routes by the link's own `ticktick_list_id` (the issue is gone, so we can't
    fetch its team) — works regardless of which pair owned it.
    """
    from lt_sync.state.models import Side
    from lt_sync.sync import mappers

    ticktick = ctxs[0].ticktick
    sm = ctxs[0].sm
    async with session_scope(sm) as session:
        link = await repo.get_link_by_linear(session, issue_id)
        if link is None:
            return
        list_id = link.ticktick_list_id or ctxs[0].settings.ticktick_list_id
        tt = await ticktick.get_task(list_id, link.ttid)
        if tt is not None:
            await ticktick.update_task(
                link.ttid, {"id": link.ttid, "projectId": tt.project_id, "status": mappers.TT_WONTDO}
            )
        await repo.add_tombstone(session, side=Side.LINEAR, linear_id=issue_id, ttid=link.ttid, note="linear_remove")
        await repo.mark_tombstoned(session, link)
        await repo.record_event(
            session,
            source=EventSource.LINEAR_WEBHOOK,
            delivery_id=delivery_id,
            payload_hash=None,
            action="remove",
            link_id=link.id,
        )
