"""Token provider for TickTickClient — pulls current access_token from DB."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lt_sync.state import repo
from lt_sync.state.db import session_scope


class TokenError(RuntimeError):
    pass


class DbTokenProvider:
    """Async callable that returns the current TickTick access_token from DB.

    Raises TokenError if no token is stored or if the stored token has expired.
    """

    def __init__(self, sm: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sm

    async def __call__(self) -> str:
        async with session_scope(self._sm) as session:
            tok = await repo.get_token(session, "ticktick")
            if tok is None:
                raise TokenError(
                    "No TickTick access_token in DB. Run `lt-sync setup ticktick` first."
                )
            now = datetime.now(tz=UTC)
            expires_at = tok.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                raise TokenError(
                    f"TickTick access_token expired at {expires_at.isoformat()}. "
                    "Re-authorize via `lt-sync setup ticktick`."
                )
            return tok.access_token
