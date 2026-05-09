"""Application configuration loaded from environment variables (.env supported)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Pushover (optional notifications)
    pushover_token: SecretStr | None = Field(default=None)
    pushover_user: SecretStr | None = Field(default=None)

    # Service / DB
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/state.db",
        description="SQLAlchemy async URL",
    )
    poll_interval_sec: int = Field(default=180, ge=30, le=3600)
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
    def database_path(self) -> Path | None:
        """Returns local Path for sqlite URLs, else None."""
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            return Path(self.database_url[len(prefix) :]).resolve()
        return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
