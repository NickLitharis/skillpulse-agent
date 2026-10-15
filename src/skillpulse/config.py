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
    # ``worker-skills`` (SPEC 8.2.4): extract in bounded batches so one task cannot run
    # for hours, and refresh the trend views nightly.
    skill_extract_interval_seconds: int = Field(default=6 * 3600, gt=0)
    skill_extract_batch_size: int = Field(default=200, gt=0)
    skill_extract_max_per_run: int = Field(default=2000, gt=0)
    trend_refresh_hour_utc: int = Field(default=2, ge=0, le=23)
    trend_refresh_minute_utc: int = Field(default=30, ge=0, le=59)

    log_level: str = "INFO"

    # LLM extraction (SPEC section 8.2). Off by default, exactly like the live-fetch
    # gate: no provider call is made unless SKILLPULSE_LLM_ENABLED is true AND a base
    # URL, a model and a shared (Redis) token budget are configured. llm_api_key is read
    # only from this explicit setting, never from ambient agent credentials. When the
    # gate is off or unconfigured, extraction stays on the offline baseline.
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_output_tokens: int = Field(default=512, gt=0)
    llm_daily_token_budget: int = Field(default=2_000_000, gt=0)

    # M4 agent (SPEC sections 9-10). Off by default, exactly like the extraction LLM:
    # no agent model call is made unless the gate is on AND a base URL and model are set.
    # The agent never reads ambient credentials; the key comes only from this setting.
    agent_llm_enabled: bool = False
    agent_llm_base_url: str = ""
    agent_llm_model: str = ""
    agent_llm_api_key: str = ""
    agent_llm_timeout_seconds: float = Field(default=20.0, gt=0)
    agent_max_tool_calls: int = Field(default=6, ge=1, le=20)
    agent_overall_timeout_seconds: float = Field(default=60.0, gt=0)
    agent_daily_token_budget: int = Field(default=2_000_000, gt=0)
    # Read-only tool sessions fail closed: every tool query runs in a READ ONLY
    # transaction with a statement timeout and a hard row cap (SPEC section 9).
    tool_statement_timeout_ms: int = Field(default=5000, gt=0)
    tool_max_rows: int = Field(default=200, ge=1, le=1000)


def get_settings() -> Settings:
    """Return settings built from the current environment."""
    return Settings()
