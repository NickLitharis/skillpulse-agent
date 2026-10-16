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
from kombu import Queue  # type: ignore[import-untyped]

from skillpulse.config import get_settings

TASK_MODULES = ["skillpulse.workers.tasks"]
REFRESH_RECENT_TASK = "skillpulse.workers.tasks.refresh_recent_threads"
EXTRACT_PENDING_TASK = "skillpulse.workers.tasks.extract_pending"
REFRESH_TREND_VIEWS_TASK = "skillpulse.workers.tasks.refresh_trend_views"
# M6: the fetch and skills workers run as separate Deployments, so tasks must land on
# separate queues (a worker only consumes the queues it is started with). Task names are
# spelled out here because importing them from tasks.py would be circular (tasks imports
# this module).
FETCH_THREAD_TASK = "skillpulse.workers.tasks.fetch_thread"
DISCOVER_THREADS_TASK = "skillpulse.workers.tasks.discover_threads"
BEAT_REFRESH_RECENT = "refresh-recent-threads"
BEAT_EXTRACT_PENDING = "extract-pending-posts"
BEAT_REFRESH_TRENDS = "refresh-trend-views"
FETCH_QUEUE = "fetch"
SKILLS_QUEUE = "skills"


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
        # The default local worker must consume the same queues the routes publish to.
        # Kubernetes workers still select their own queue explicitly with -Q.
        task_default_queue=FETCH_QUEUE,
        task_queues=(Queue(FETCH_QUEUE), Queue(SKILLS_QUEUE)),
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
        # M6: one queue per worker Deployment. A worker started with -Q fetch only
        # consumes fetching tasks, -Q skills only extraction/trend tasks; beat itself
        # publishes to both through these routes.
        task_routes={
            FETCH_THREAD_TASK: {"queue": FETCH_QUEUE},
            DISCOVER_THREADS_TASK: {"queue": FETCH_QUEUE},
            REFRESH_RECENT_TASK: {"queue": FETCH_QUEUE},
            EXTRACT_PENDING_TASK: {"queue": SKILLS_QUEUE},
            REFRESH_TREND_VIEWS_TASK: {"queue": SKILLS_QUEUE},
        },
    )
    return app


app = make_celery_app()
