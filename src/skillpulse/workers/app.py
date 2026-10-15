"""The Celery application: Redis broker, Redis result backend and the beat schedule.

Task bodies live in :mod:`skillpulse.workers.tasks`; they only wrap the same
``skillpulse.fetch_service`` functions the CLI calls, so there is one implementation of
the fetching logic (SPEC sections 8 and 15).

Run a worker (Windows and all platforms support the ``solo`` pool):

    uv run celery -A skillpulse.workers.app worker --pool=solo --loglevel=info

Run the scheduler that refreshes the current and previous month every 6 hours:

    uv run celery -A skillpulse.workers.app beat --loglevel=info
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from skillpulse.config import get_settings

TASK_MODULES = ["skillpulse.workers.tasks"]
REFRESH_RECENT_TASK = "skillpulse.workers.tasks.refresh_recent_threads"
EXTRACT_PENDING_TASK = "skillpulse.workers.tasks.extract_pending"
REFRESH_TREND_VIEWS_TASK = "skillpulse.workers.tasks.refresh_trend_views"
BEAT_REFRESH_RECENT = "refresh-recent-threads"
BEAT_EXTRACT_PENDING = "extract-pending-posts"
BEAT_REFRESH_TRENDS = "refresh-trend-views"


def make_celery_app(redis_url: str | None = None) -> Celery:
    """Build the Celery app.

    ``redis_url`` overrides the broker and result backend, which the smoke test uses to
    point at an ephemeral Redis container instead of the one in ``.env``.
    """
    settings = get_settings()
    broker = redis_url or settings.redis_url
    app = Celery("skillpulse", broker=broker, backend=broker, include=TASK_MODULES)
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        # One fetch at a time per worker: a task is long-running and holds a DB session.
        worker_prefetch_multiplier=1,
        task_acks_late=True,
        task_track_started=True,
        broker_connection_retry_on_startup=True,
        beat_schedule={
            # SPEC section 8.3: re-fetch the current and previous month every 6 hours.
            BEAT_REFRESH_RECENT: {
                "task": REFRESH_RECENT_TASK,
                "schedule": float(settings.celery_update_interval_seconds),
            },
            # SPEC section 8.2.4: keep extracting newly fetched posts in bounded runs.
            BEAT_EXTRACT_PENDING: {
                "task": EXTRACT_PENDING_TASK,
                "schedule": float(settings.skill_extract_interval_seconds),
                "kwargs": {
                    "limit": settings.skill_extract_max_per_run,
                    "batch_size": settings.skill_extract_batch_size,
                },
            },
            # SPEC section 8, "Trends": refresh the trend views nightly.
            BEAT_REFRESH_TRENDS: {
                "task": REFRESH_TREND_VIEWS_TASK,
                "schedule": crontab(
                    hour=settings.trend_refresh_hour_utc,
                    minute=settings.trend_refresh_minute_utc,
                ),
            },
        },
    )
    return app


app = make_celery_app()
