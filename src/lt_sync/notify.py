"""Pushover alerts (optional)."""

from __future__ import annotations

import httpx

from lt_sync.config import Settings
from lt_sync.logging_setup import log


def maybe_pushover(settings: Settings, *, title: str, message: str) -> None:
    if not settings.pushover_token or not settings.pushover_user:
        return
    try:
        resp = httpx.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": settings.pushover_token.get_secret_value(),
                "user": settings.pushover_user.get_secret_value(),
                "title": title,
                "message": message,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("pushover failed", error=str(exc))
