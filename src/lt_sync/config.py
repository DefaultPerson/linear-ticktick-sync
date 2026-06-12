"""Application configuration loaded from environment variables (.env supported)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SyncPair(BaseModel):
    """One Linear-team → TickTick-list sync pair.

    `project_name=None` means "sync the whole team" (no project filter).
    """

    team_key: str
    ticktick_list_id: str
    project_name: str | None = None


class Settings(BaseSettings):
    """All runtime configuration in one place. Validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Linear
    linear_api_key: SecretStr = Field(..., description="Linear PAT (lin_api_…)")
    linear_team_key: str = Field(default="HMC", description="Linear team key")
    linear_project_name: str = Field(default="hm", description="Project name to host synced issues")
    linear_webhook_secret: SecretStr = Field(
        ..., description="Shared secret for Linear webhook HMAC verification"
    )

    # TickTick
    ticktick_client_id: SecretStr = Field(..., description="TickTick OAuth client ID")
    ticktick_client_secret: SecretStr = Field(..., description="TickTick OAuth client secret")
    ticktick_redirect_uri: str = Field(
        ..., description="Public redirect URI registered in TickTick app"
    )
    ticktick_list_id: str = Field(
        default="69cd04eb8f088eaeff7fb755", description="TickTick project (list) id to sync"
    )
    ticktick_default_tz: str = Field(default="Europe/Moscow", description="Default TZ for new tasks")

    # Multi-pair sync. JSON list of {team_key, ticktick_list_id, project_name?}.
    # When unset, a single pair is synthesised from the legacy scalar fields below.
    sync_pairs: list[SyncPair] | None = Field(
        default=None, description="JSON list of Linear-team → TickTick-list sync pairs"
    )

    @field_validator("sync_pairs", mode="before")
    @classmethod
    def _blank_sync_pairs_to_none(cls, v: object) -> object:
        # An unset `SYNC_PAIRS=${SYNC_PAIRS:-}` arrives as "" — treat as None so we
        # fall back to the legacy single pair instead of failing JSON decode.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # Pushover (optional notifications)
    pushover_token: SecretStr | None = Field(default=None)
    pushover_user: SecretStr | None = Field(default=None)

    # Service / DB
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/state.db",
        description="SQLAlchemy async URL",
    )
    poll_interval_sec: int = Field(default=180, ge=10, le=3600)
    linear_backfill_interval_sec: int = Field(default=3600, ge=60)
    token_check_interval_sec: int = Field(default=86400, ge=3600)
    echo_window_sec: int = Field(default=30, ge=5, le=600)
    backfill_on_start: bool = Field(default=True)
    log_level: str = Field(default="INFO")
    public_base_url: str | None = Field(default=None, description="Public HTTPS URL of the service")

    # Markers
    sync_label_name: str = Field(default="ticktick-sync")
    delegated_label_name: str = Field(default="Delegated")
    tombstoned_label_name: str = Field(default="tombstoned-from-ticktick")

    # Match
    match_threshold: int = Field(default=85, ge=50, le=100)
    match_due_window_days: int = Field(default=3, ge=0, le=30)

    @property
    def effective_pairs(self) -> list[SyncPair]:
        """Resolved sync pairs: explicit `sync_pairs`, else one legacy pair."""
        if self.sync_pairs:
            return self.sync_pairs
        return [
            SyncPair(
                team_key=self.linear_team_key,
                ticktick_list_id=self.ticktick_list_id,
                project_name=self.linear_project_name,
            )
        ]

    @property
    def database_path(self) -> Path | None:
        """Returns local Path for sqlite URLs, else None."""
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            return Path(self.database_url[len(prefix) :]).resolve()
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
