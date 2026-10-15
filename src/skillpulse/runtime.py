"""Shared wiring for the CLI and the Celery workers.

Both entry points build the HN client and the sync session factory from settings, so a
worker task and a CLI command run the exact same ingestion code (SPEC sections 8 and 15).
Keep the wiring here rather than in either entry point to avoid two copies drifting.
"""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from skillpulse.config import get_settings
from skillpulse.db import make_engine, make_session_factory
from skillpulse.hn_client import HackerNewsClient, TokenLimiter


class LiveFetchDisabledError(RuntimeError):
    """Live Hacker News access was attempted while the authorization gate is closed."""


_LIVE_FETCH_DISABLED_MESSAGE = (
    "Live Hacker News fetching is gated: authorization to read and store HN post text "
    "is unresolved (see docs/terms.md). No live ingestion, back-fill or discovery run "
    "while SKILLPULSE_HN_LIVE_FETCH_ENABLED is false. Enable it only after an explicit "
    "authorization decision for this use has been recorded in docs/terms.md."
)


def build_client(*, limiter: TokenLimiter | None = None) -> HackerNewsClient:
    """Create the HN client from the current settings.

    ``limiter`` lets the workers pass in the Redis-wide token bucket; the CLI keeps the
    default in-process limiter so one-off commands do not need Redis. The live-fetch
    gate is checked here, the single choke point every live path goes through.
    """
    settings = get_settings()
    if not settings.hn_live_fetch_enabled:
        raise LiveFetchDisabledError(_LIVE_FETCH_DISABLED_MESSAGE)
    return HackerNewsClient(
        settings.hn_base_url,
        max_requests_per_second=settings.hn_max_requests_per_second,
        max_retries=settings.hn_max_retries,
        timeout_seconds=settings.hn_timeout_seconds,
        limiter=limiter,
    )


def build_session_factory() -> sessionmaker[Session]:
    """Create the sync session factory the workers use (SPEC section 8.7)."""
    settings = get_settings()
    return make_session_factory(make_engine(settings.database_url))
