"""TickTick OAuth 2.0 helpers — authorize URL builder + code exchange.

TickTick OpenAPI v1: https://developer.ticktick.com/api#/openapi
- authorize: https://ticktick.com/oauth/authorize
- token:     https://ticktick.com/oauth/token
- scope:     tasks:read tasks:write
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

AUTHORIZE_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
DEFAULT_SCOPE = "tasks:read tasks:write"


def make_state() -> str:
    return secrets.token_urlsafe(16)


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str = DEFAULT_SCOPE,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "response_type": "code",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


async def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scope: str = DEFAULT_SCOPE,
) -> dict[str, object]:
    """Exchange authorization code for access token.

    Returns dict with keys: access_token, expires_in (seconds), scope, token_type,
    optionally refresh_token. Computes expires_at as a datetime in UTC.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TOKEN_URL,
            auth=(client_id, client_secret),
            data={
                "code": code,
                "grant_type": "authorization_code",
                "scope": scope,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
    expires_in = int(data.get("expires_in") or 0)
    expires_at = datetime.now(tz=UTC) + timedelta(seconds=expires_in or 86400)
    data["expires_at"] = expires_at
    return data
