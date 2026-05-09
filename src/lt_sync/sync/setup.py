"""Bootstrap helpers — assemble SyncContext, ensure Linear project & labels exist."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lt_sync.config import Settings
from lt_sync.linear.client import LinearClient
from lt_sync.linear.types import LinearLabel
from lt_sync.logging_setup import log
from lt_sync.sync.engine import SyncContext
from lt_sync.ticktick.client import TickTickClient


async def build_context(
    *,
    settings: Settings,
    sm: async_sessionmaker[AsyncSession],
    linear: LinearClient,
    ticktick: TickTickClient,
) -> SyncContext:
    team = await linear.get_team(settings.linear_team_key)
    project = await linear.get_or_create_project(name=settings.linear_project_name, team_id=team.id)
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
        project=project.name,
        labels=[lab.name for lab in team.labels],
    )
    return SyncContext(
        settings=settings,
        sm=sm,
        linear=linear,
        ticktick=ticktick,
        team=team,
        project=project,
        sync_label=sync_label,
        delegated_label=delegated_label,
        tombstoned_label=tombstoned_label,
    )


async def ensure_label(
    *, linear: LinearClient, name: str, team_id: str
) -> LinearLabel:
    return await linear.get_or_create_label(name=name, team_id=team_id)
