"""Async Linear GraphQL client (httpx) with rate-limit + retry."""

from __future__ import annotations

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

from lt_sync.linear.types import (
    LinearIssue,
    LinearLabel,
    LinearProject,
    LinearState,
    LinearTeam,
)
from lt_sync.logging_setup import log

LINEAR_GRAPHQL = "https://api.linear.app/graphql"


class LinearError(RuntimeError):
    pass


class LinearClient:
    """High-level async client. Holds httpx.AsyncClient lifecycle."""

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            base_url=LINEAR_GRAPHQL,
            timeout=timeout,
            headers={
                "Authorization": api_key,
                "Content-Type": "application/json",
            },
        )
        # Linear public limit: 2500 req/h ≈ 41/min sustained. Stay safely below.
        self._limiter = AsyncLimiter(max_rate=30, time_period=60)

    async def __aenter__(self) -> LinearClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _post(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=32),
            retry=retry_if_exception_type((httpx.HTTPError,)),
            reraise=True,
        ):
            with attempt:
                async with self._limiter:
                    resp = await self._client.post(
                        "", json={"query": query, "variables": variables or {}}
                    )
                if resp.status_code == 429:
                    raise httpx.HTTPError("Linear 429 rate limit")
                resp.raise_for_status()
                payload = resp.json()
                if payload.get("errors"):
                    raise LinearError(str(payload["errors"]))
                return payload["data"]
        raise LinearError("unreachable")

    # ── Reads ─────────────────────────────────────────────────────────────

    async def viewer(self) -> dict[str, Any]:
        data = await self._post("query { viewer { id name email } }")
        return data["viewer"]

    async def get_team(self, team_key: str) -> LinearTeam:
        # Resolve by key OR name (case-insensitive), so a pair may be configured
        # with either the team key ("HMC") or its display name ("w3a").
        q = """
        query($q: String!) {
          teams(
            filter: {or: [{key: {eqIgnoreCase: $q}}, {name: {eqIgnoreCase: $q}}]}
            first: 1
          ) {
            nodes {
              id key name
              states(first: 100) { nodes { id name type position } }
              labels(first: 250) { nodes { id name color } }
              projects(first: 100) { nodes { id name state } }
            }
          }
        }
        """
        data = await self._post(q, {"q": team_key})
        nodes = data["teams"]["nodes"]
        if not nodes:
            raise LinearError(f"Team {team_key} not found")
        n = nodes[0]
        return LinearTeam(
            id=n["id"],
            key=n["key"],
            name=n["name"],
            states=[LinearState(**s) for s in n["states"]["nodes"]],
            labels=[LinearLabel(id=lab["id"], name=lab["name"], color=lab.get("color")) for lab in n["labels"]["nodes"]],
            projects=[LinearProject(id=p["id"], name=p["name"]) for p in n["projects"]["nodes"]],
        )

    async def find_issue_by_id(self, issue_id: str) -> LinearIssue | None:
        q = """
        query($id: String!) {
          issue(id: $id) { ...IssueFields }
        }
        """ + _ISSUE_FRAGMENT
        try:
            data = await self._post(q, {"id": issue_id})
        except LinearError:
            return None
        n = data.get("issue")
        return _parse_issue(n) if n else None

    async def list_team_issues(
        self,
        team_key: str,
        *,
        project_id: str | None = None,
        only_no_project: bool = False,
        label: str | None = None,
        limit: int = 250,
    ) -> list[LinearIssue]:
        filter_obj: dict[str, Any] = {"team": {"key": {"eq": team_key}}}
        if project_id is not None:
            filter_obj["project"] = {"id": {"eq": project_id}}
        elif only_no_project:
            filter_obj["project"] = {"null": True}
        if label is not None:
            filter_obj["labels"] = {"name": {"eq": label}}
        q = """
        query($filter: IssueFilter!, $limit: Int!) {
          issues(first: $limit, filter: $filter) {
            nodes { ...IssueFields }
          }
        }
        """ + _ISSUE_FRAGMENT
        data = await self._post(q, {"filter": filter_obj, "limit": limit})
        return [_parse_issue(n) for n in data["issues"]["nodes"]]

    # ── Mutations ─────────────────────────────────────────────────────────

    async def create_project(
        self, *, name: str, team_id: str, state: str = "started"
    ) -> LinearProject:
        q = """
        mutation($input: ProjectCreateInput!) {
          projectCreate(input: $input) {
            success
            project { id name }
          }
        }
        """
        data = await self._post(
            q, {"input": {"name": name, "teamIds": [team_id], "state": state}}
        )
        if not data["projectCreate"]["success"]:
            raise LinearError("projectCreate failed")
        p = data["projectCreate"]["project"]
        return LinearProject(id=p["id"], name=p["name"])

    async def create_label(self, *, name: str, team_id: str, color: str | None = None) -> LinearLabel:
        q = """
        mutation($input: IssueLabelCreateInput!) {
          issueLabelCreate(input: $input) {
            success
            issueLabel { id name color }
          }
        }
        """
        inp: dict[str, Any] = {"name": name, "teamId": team_id}
        if color:
            inp["color"] = color
        data = await self._post(q, {"input": inp})
        if not data["issueLabelCreate"]["success"]:
            raise LinearError("issueLabelCreate failed")
        lab = data["issueLabelCreate"]["issueLabel"]
        return LinearLabel(id=lab["id"], name=lab["name"], color=lab.get("color"))

    async def create_issue(
        self,
        *,
        team_id: str,
        title: str,
        description: str,
        state_id: str,
        priority: int = 0,
        project_id: str | None = None,
        label_ids: list[str] | None = None,
        due_date: str | None = None,
    ) -> LinearIssue:
        q = """
        mutation($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue { ...IssueFields }
          }
        }
        """ + _ISSUE_FRAGMENT
        inp: dict[str, Any] = {
            "teamId": team_id,
            "title": title,
            "description": description,
            "stateId": state_id,
            "priority": priority,
        }
        if project_id:
            inp["projectId"] = project_id
        if label_ids:
            inp["labelIds"] = label_ids
        if due_date:
            inp["dueDate"] = due_date
        data = await self._post(q, {"input": inp})
        if not data["issueCreate"]["success"]:
            raise LinearError("issueCreate failed")
        return _parse_issue(data["issueCreate"]["issue"])

    async def update_issue(
        self,
        issue_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        state_id: str | None = None,
        priority: int | None = None,
        project_id: str | None = None,
        team_id: str | None = None,
        label_ids: list[str] | None = None,
        due_date: str | None = None,
    ) -> LinearIssue:
        q = """
        mutation($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
            issue { ...IssueFields }
          }
        }
        """ + _ISSUE_FRAGMENT
        inp: dict[str, Any] = {}
        if title is not None:
            inp["title"] = title
        if description is not None:
            inp["description"] = description
        if state_id is not None:
            inp["stateId"] = state_id
        if priority is not None:
            inp["priority"] = priority
        if project_id is not None:
            inp["projectId"] = project_id
        if team_id is not None:
            inp["teamId"] = team_id
        if label_ids is not None:
            inp["labelIds"] = label_ids
        if due_date is not None:
            inp["dueDate"] = due_date
        if not inp:
            existing = await self.find_issue_by_id(issue_id)
            if existing is None:
                raise LinearError(f"issue {issue_id} not found")
            return existing
        data = await self._post(q, {"id": issue_id, "input": inp})
        if not data["issueUpdate"]["success"]:
            raise LinearError("issueUpdate failed")
        return _parse_issue(data["issueUpdate"]["issue"])

    async def add_label_to_issue(self, issue_id: str, label_id: str) -> None:
        existing = await self.find_issue_by_id(issue_id)
        if existing is None:
            raise LinearError(f"issue {issue_id} not found")
        if label_id in existing.label_ids:
            return
        new_ids = [*existing.label_ids, label_id]
        await self.update_issue(issue_id, label_ids=new_ids)

    async def get_or_create_project(self, *, name: str, team_id: str) -> LinearProject:
        team = await self._post(
            """query($id: String!) {
              team(id: $id) { projects(first: 100) { nodes { id name } } }
            }""",
            {"id": team_id},
        )
        for p in team["team"]["projects"]["nodes"]:
            if p["name"].strip().lower() == name.strip().lower():
                return LinearProject(id=p["id"], name=p["name"])
        log.info("creating linear project", name=name)
        return await self.create_project(name=name, team_id=team_id)

    async def get_or_create_label(
        self, *, name: str, team_id: str, color: str | None = None
    ) -> LinearLabel:
        team = await self.get_team_labels(team_id)
        for lab in team:
            if lab.name.strip().lower() == name.strip().lower():
                return lab
        log.info("creating linear label", name=name)
        return await self.create_label(name=name, team_id=team_id, color=color)

    async def get_team_labels(self, team_id: str) -> list[LinearLabel]:
        q = """
        query($id: String!) {
          team(id: $id) { labels(first: 250) { nodes { id name color } } }
        }
        """
        data = await self._post(q, {"id": team_id})
        return [
            LinearLabel(id=lab["id"], name=lab["name"], color=lab.get("color"))
            for lab in data["team"]["labels"]["nodes"]
        ]


_ISSUE_FRAGMENT = """
fragment IssueFields on Issue {
  id identifier title description priority dueDate url
  createdAt updatedAt
  state { id name type }
  team { id key }
  project { id }
  labels { nodes { id name } }
}
"""


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_issue(n: dict[str, Any]) -> LinearIssue:
    return LinearIssue(
        id=n["id"],
        identifier=n["identifier"],
        title=n["title"],
        description=n.get("description"),
        state_id=n["state"]["id"],
        state_name=n["state"]["name"],
        state_type=n["state"]["type"],
        priority=n.get("priority") or 0,
        project_id=(n.get("project") or {}).get("id"),
        team_key=(n.get("team") or {}).get("key"),
        team_id=(n.get("team") or {}).get("id"),
        label_ids=[lab["id"] for lab in n["labels"]["nodes"]],
        label_names=[lab["name"] for lab in n["labels"]["nodes"]],
        due_date=n.get("dueDate"),
        url=n.get("url", ""),
        created_at=_parse_dt(n.get("createdAt")),
        updated_at=_parse_dt(n.get("updatedAt")),
    )
