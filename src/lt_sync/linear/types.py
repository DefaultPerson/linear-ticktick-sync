"""Plain dataclasses for Linear domain objects (subset we use)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class LinearLabel:
    id: str
    name: str
    color: str | None = None


@dataclass(slots=True)
class LinearState:
    id: str
    name: str
    type: str  # backlog | unstarted | started | completed | canceled
    position: float = 0.0


@dataclass(slots=True)
class LinearProject:
    id: str
    name: str


@dataclass(slots=True)
class LinearIssue:
    id: str
    identifier: str  # HMC-N
    title: str
    description: str | None
    state_id: str
    state_name: str
    state_type: str
    priority: int
    project_id: str | None
    label_ids: list[str] = field(default_factory=list)
    label_names: list[str] = field(default_factory=list)
    due_date: str | None = None  # YYYY-MM-DD
    url: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class LinearTeam:
    id: str
    key: str
    name: str
    states: list[LinearState] = field(default_factory=list)
    labels: list[LinearLabel] = field(default_factory=list)
    projects: list[LinearProject] = field(default_factory=list)
