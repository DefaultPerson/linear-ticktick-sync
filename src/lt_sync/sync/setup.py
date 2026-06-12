"""Bootstrap helpers — assemble SyncContext, ensure Linear project & labels exist."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lt_sync.config import Settings, SyncPair
from lt_sync.linear.client import LinearClient
from lt_sync.linear.types import LinearLabel
from lt_sync.logging_setup import log
from lt_sync.sync.engine import SyncContext
from lt_sync.ticktick.client import TickTickClient


async def build_contexts(
    *,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    linear: LinearClient,
    ticktick: TickTickClient,
) -> list[SyncContext]:
    """One SyncContext per configured (team → list) pair.

    A pair that fails to build (e.g. unknown team) is skipped with a logged
    error rather than taking down every other pair's sync.
    """
    ctxs: list[SyncContext] = []
    for pair in settings.effective_pairs:
        try:
            ctxs.append(
                await _build_one(pair, settings=settings, sm=sm, linear=linear, ticktick=ticktick)
            )
        except Exception as exc:
            log.error("failed to build sync pair; skipping", team=pair.team_key, error=str(exc))
    if not ctxs:
        raise RuntimeError("no sync pairs could be built")
    return ctxs


async def _build_one(
    pair: SyncPair,
    *,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    linear: LinearClient,
    ticktick: TickTickClient,
) -> SyncContext:
    team = await linear.get_team(pair.team_key)
    project = (
        await linear.get_or_create_project(name=pair.project_name, team_id=team.id)
        if pair.project_name
        else None
    )
    sync_label = await linear.get_or_create_label(name=settings.sync_label_name, team_id=team.id)
    delegated_label = await linear.get_or_create_label(
        name=settings.delegated_label_name, team_id=team.id
    )
    tombstoned_label = await linear.get_or_create_label(
        name=settings.tombstoned_label_name, team_id=team.id
    )
    # refresh team labels list to include newly-created ones
    team.labels = await linear.get_team_labels(team.id)
    log.info(
        "context ready",
        team=team.key,
        project=project.name if project else None,
        ticktick_list_id=pair.ticktick_list_id,
        labels=[lab.name for lab in team.labels],
    )
    return SyncContext(
        settings=settings,
        sm=sm,
        linear=linear,
        ticktick=ticktick,
        team=team,
        project=project,
        ticktick_list_id=pair.ticktick_list_id,
        sync_label=sync_label,
        delegated_label=delegated_label,
        tombstoned_label=tombstoned_label,
    )


async def build_context(
    *,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    linear: LinearClient,
    ticktick: TickTickClient,
) -> SyncContext:
    """Single-pair shim for the CLI — returns the first configured pair's context."""
    ctxs = await build_contexts(settings=settings, sm=sm, linear=linear, ticktick=ticktick)
    return ctxs[0]


async def ensure_label(
    *, linear: LinearClient, name: str, team_id: str
) -> LinearLabel:
    return await linear.get_or_create_label(name=name, team_id=team_id)
