"""TickTick OpenAPI v1 domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class TTChecklistItem:
    id: str
    title: str
    status: int  # 0=unchecked, 1=checked
    sort_order: int = 0
    start_date: str | None = None
    is_all_day: bool = False
    time_zone: str = "UTC"
    completed_time: int | None = None  # ms epoch


@dataclass(slots=True)
class TTColumn:
    id: str
    name: str
    sort_order: int


@dataclass(slots=True)
class TTTask:
    id: str
    project_id: str
    title: str
    content: str | None = None
    desc: str | None = None
    priority: int = 0  # 0/1/3/5
    status: int = 0  # 0=open, 2=completed, -1=wontDo
    column_id: str | None = None
    sort_order: int = 0
    start_date: str | None = None
    due_date: str | None = None
    is_all_day: bool = False
    time_zone: str = "UTC"
    reminders: list[str] = field(default_factory=list)
    items: list[TTChecklistItem] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    modified_time: datetime | None = None  # last server update


@dataclass(slots=True)
class TTProjectData:
    project_id: str
    name: str
    columns: list[TTColumn]
    tasks: list[TTTask]
