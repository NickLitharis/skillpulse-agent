"""Typed application settings (SPEC section 12: env vars, names from .env.example)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment variables prefixed with ``SKILLPULSE_``."""

    model_config = SettingsConfigDict(env_prefix="SKILLPULSE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://skillpulse:skillpulse@localhost:5433/skillpulse"

    hn_base_url: str = "https://hacker-news.firebaseio.com/v0"
    hn_max_requests_per_second: float = Field(default=10.0, gt=0)
    hn_max_retries: int = Field(default=5, ge=0)
    hn_timeout_seconds: float = Field(default=20.0, gt=0)
    # Live HN access is gated: authorization to read and store HN post text is
    # unresolved (docs/terms.md), so the gate is closed by default. The CLI and the
    # workers build their client through runtime.build_client, which refuses while this
    # is false. Tests use in-process fakes or a local HTTP stub.
    hn_live_fetch_enabled: bool = False

    # Celery workers (SPEC section 8): Redis broker/result backend, 6-hour refresh beat.
    redis_url: str = "redis://localhost:6379/0"
    celery_task_max_retries: int = Field(default=3, ge=0)
    celery_update_interval_seconds: int = Field(default=6 * 3600, gt=0)
    thread_lock_ttl_seconds: int = Field(default=900, gt=0)

    log_level: str = "INFO"


def get_settings() -> Settings:
    """Return settings built from the current environment."""
    return Settings()
