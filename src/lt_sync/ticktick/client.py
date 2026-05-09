"""TickTick OpenAPI v1 async client (httpx) with rate-limit + retry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import httpx
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from lt_sync.ticktick.types import TTChecklistItem, TTColumn, TTProjectData, TTTask

API_BASE = "https://api.ticktick.com/open/v1"


class TickTickError(RuntimeError):
    pass


class TickTickClient:
    """Async TickTick OpenAPI v1 client.

    The token is obtained lazily through `token_provider` async-callable so the
    refresh-on-expiry flow is owned by the caller (see lt_sync.ticktick.oauth).
    """

    def __init__(
        self,
        token_provider: Callable[[], Awaitable[str]],
        *,
        timeout: float = 30.0,
    ) -> None:
        self._token_provider = token_provider
        self._client = httpx.AsyncClient(base_url=API_BASE, timeout=timeout)
        self._limiter = AsyncLimiter(max_rate=60, time_period=60)

    async def __aenter__(self) -> TickTickClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, json: Any = None, params: Any = None
    ) -> Any:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=32),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                token = await self._token_provider()
                headers = {"Authorization": f"Bearer {token}"}
                async with self._limiter:
                    resp = await self._client.request(
                        method, path, headers=headers, json=json, params=params
                    )
                if resp.status_code == 401:
                    raise TickTickError(f"401 Unauthorized: {resp.text}")
                if resp.status_code == 429:
                    raise httpx.HTTPError("TickTick 429 rate limit")
                if resp.status_code >= 400:
                    raise TickTickError(f"{resp.status_code} {resp.text}")
                if resp.content == b"" or resp.status_code == 204:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return resp.text
        raise TickTickError("unreachable")

    # ── Reads ─────────────────────────────────────────────────────────────

    async def get_project_data(self, project_id: str) -> TTProjectData:
        data = await self._request("GET", f"/project/{project_id}/data")
        proj = data["project"]
        columns = [
            TTColumn(id=c["id"], name=c["name"], sort_order=c.get("sortOrder", 0))
            for c in data.get("columns", [])
        ]
        tasks = [_parse_task(t) for t in data.get("tasks", [])]
        return TTProjectData(
            project_id=proj["id"],
            name=proj.get("name", ""),
            columns=columns,
            tasks=tasks,
        )

    async def get_task(self, project_id: str, task_id: str) -> TTTask | None:
        try:
            data = await self._request("GET", f"/project/{project_id}/task/{task_id}")
        except TickTickError as e:
            if "404" in str(e) or "Not Found" in str(e):
                return None
            raise
        return _parse_task(data)

    # ── Writes ────────────────────────────────────────────────────────────

    async def create_task(self, payload: dict[str, Any]) -> TTTask:
        data = await self._request("POST", "/task", json=payload)
        return _parse_task(data)

    async def update_task(self, task_id: str, payload: dict[str, Any]) -> TTTask:
        merged = {**payload, "id": task_id}
        data = await self._request("POST", f"/task/{task_id}", json=merged)
        return _parse_task(data)

    async def complete_task(self, project_id: str, task_id: str) -> None:
        await self._request("POST", f"/project/{project_id}/task/{task_id}/complete")

    async def delete_task(self, project_id: str, task_id: str) -> None:
        await self._request("DELETE", f"/project/{project_id}/task/{task_id}")


def _parse_task(t: dict[str, Any]) -> TTTask:
    items = [
        TTChecklistItem(
            id=str(it["id"]),
            title=it.get("title", ""),
            status=int(it.get("status", 0)),
            sort_order=int(it.get("sortOrder", 0)),
            start_date=it.get("startDate"),
            is_all_day=bool(it.get("isAllDay", False)),
            time_zone=it.get("timeZone", "UTC"),
            completed_time=it.get("completedTime"),
        )
        for it in t.get("items", []) or []
    ]
    modified = t.get("modifiedTime")
    modified_dt = None
    if isinstance(modified, str):
        try:
            modified_dt = datetime.fromisoformat(modified.replace("Z", "+00:00"))
        except ValueError:
            modified_dt = None
    return TTTask(
        id=t["id"],
        project_id=t["projectId"],
        title=t.get("title", ""),
        content=t.get("content"),
        desc=t.get("desc"),
        priority=int(t.get("priority", 0)),
        status=int(t.get("status", 0)),
        column_id=t.get("columnId"),
        sort_order=int(t.get("sortOrder", 0)),
        start_date=t.get("startDate"),
        due_date=t.get("dueDate"),
        is_all_day=bool(t.get("isAllDay", False)),
        time_zone=t.get("timeZone", "UTC"),
        reminders=list(t.get("reminders", []) or []),
        items=items,
        tags=list(t.get("tags", []) or []),
        modified_time=modified_dt,
    )
