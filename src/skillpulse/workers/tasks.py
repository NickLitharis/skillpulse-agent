"""Celery tasks wrapping the ingestion service (SPEC section 8).

Nothing here re-implements fetching. Each task body calls the same
``skillpulse.fetch_service`` function the CLI uses, so the two paths cannot drift. The
``run_*`` helpers take their collaborators as arguments, which is what makes them
testable without Redis or a live network.

Coordination is Redis-wide (SPEC sections 8.5 and 8.6): the fetch client draws tokens
from one shared bucket, and one expiring lock per thread stops two runs overlapping.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import redis
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.config import get_settings
from skillpulse.db import make_engine
from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.llm import LLMExtractor
from skillpulse.extraction.providers import build_extractor
from skillpulse.extraction.taxonomy import Taxonomy, load_taxonomy
from skillpulse.fetch_service import (
    MAX_SUBMITTED_TO_SCAN,
    ThreadFetchResult,
    fetch_thread_by_id,
    find_hiring_threads,
)
from skillpulse.hn_client import HackerNewsClient, HackerNewsError
from skillpulse.repository import UpsertResult
from skillpulse.runtime import build_client, build_session_factory
from skillpulse.trends import refresh_trend_views
from skillpulse.workers.app import app
from skillpulse.workers.redis_state import RedisRateLimiter, RedisThreadLock
from skillpulse.workers.skills import ExtractionBatchResult, run_extract_pending

logger = logging.getLogger(__name__)

FETCH_THREAD_TASK = "skillpulse.workers.tasks.fetch_thread"
DISCOVER_THREADS_TASK = "skillpulse.workers.tasks.discover_threads"
REFRESH_RECENT_THREADS_TASK = "skillpulse.workers.tasks.refresh_recent_threads"
EXTRACT_PENDING_TASK = "skillpulse.workers.tasks.extract_pending"
REFRESH_TREND_VIEWS_TASK = "skillpulse.workers.tasks.refresh_trend_views"

# Current + previous month (SPEC section 8.3).
RECENT_THREADS = 2

_settings = get_settings()


def result_summary(result: ThreadFetchResult) -> dict[str, Any]:
    """Render a fetch result as a JSON-serialisable task return value."""
    return {
        "thread_id": result.thread_id,
        "status": result.status,
        "seen": result.result.seen,
        "created": result.result.created,
        "updated": result.result.updated,
        "unchanged": result.result.unchanged,
        "children_found": result.children_found,
    }


def locked_summary(thread_id: int) -> dict[str, Any]:
    """A fetch that was skipped because another worker held the thread lock."""
    return {
        "thread_id": thread_id,
        "status": "locked",
        "seen": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "children_found": 0,
    }


def worker_client(redis_client: redis.Redis) -> HackerNewsClient:
    """Worker HN client drawing from the fleet-wide Redis token bucket (SPEC 8.5).

    Every worker task (fetch and discovery) must go through this, otherwise discovery
    and fetch could together reach more than the polite 10 requests/second ceiling.
    """
    settings = get_settings()
    limiter = RedisRateLimiter(redis_client, rate=settings.hn_max_requests_per_second)
    return build_client(limiter=limiter)


def run_fetch_thread(
    factory: sessionmaker[Session],
    client: HackerNewsClient,
    thread_id: int,
    *,
    on_batch: Callable[[UpsertResult], None] | None = None,
) -> dict[str, Any]:
    """Fetch one thread with a caller-supplied client and session factory."""
    return result_summary(fetch_thread_by_id(factory, client, thread_id, on_batch=on_batch))


def run_locked_fetch(
    factory: sessionmaker[Session],
    client: HackerNewsClient,
    redis_client: redis.Redis,
    thread_id: int,
    *,
    ttl_seconds: int,
) -> dict[str, Any]:
    """Fetch a thread while holding its Redis lock (SPEC section 8.6).

    If another worker holds the lock the fetch is skipped rather than queued behind it:
    the holder is already doing exactly this work. The lock TTL is refreshed after each
    committed batch, so a long fetch keeps its lock and a crashed one lets it expire.
    """
    with RedisThreadLock(redis_client, thread_id, ttl_seconds=ttl_seconds) as lock:
        if not lock.acquired:
            logger.info("thread %s is already being fetched; skipping", thread_id)
            return locked_summary(thread_id)
        return run_fetch_thread(
            factory,
            client,
            thread_id,
            on_batch=lambda _batch: lock.refresh(),
        )


def run_discover_threads(
    client: HackerNewsClient,
    *,
    limit: int | None = None,
    max_scan: int = MAX_SUBMITTED_TO_SCAN,
) -> list[int]:
    """Return the ids of the hiring threads to queue, newest first."""
    threads = find_hiring_threads(client, max_scan=max_scan)
    if limit is not None:
        threads = threads[:limit]
    return [thread.id for thread in threads]


@app.task(
    name=FETCH_THREAD_TASK,
    autoretry_for=(HackerNewsError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_settings.celery_task_max_retries,
)
def fetch_thread(thread_id: int) -> dict[str, Any]:
    """Fetch and upsert one hiring thread's posts.

    A transient ``HackerNewsError`` is retried with backoff; each retry resumes from the
    last committed batch recorded in ``fetch_runs`` (SPEC S6).
    """
    settings = get_settings()
    redis_client = redis.Redis.from_url(settings.redis_url)
    with worker_client(redis_client) as client:
        return run_locked_fetch(
            build_session_factory(),
            client,
            redis_client,
            thread_id,
            ttl_seconds=settings.thread_lock_ttl_seconds,
        )


def queue_thread_tasks(thread_ids: list[int]) -> dict[str, Any]:
    """Queue one ``fetch_thread`` task per thread id."""
    for thread_id in thread_ids:
        fetch_thread.delay(thread_id)
    return {"queued": len(thread_ids), "thread_ids": thread_ids}


@app.task(name=DISCOVER_THREADS_TASK)
def discover_threads(
    limit: int | None = None, max_scan: int = MAX_SUBMITTED_TO_SCAN
) -> dict[str, Any]:
    """Queue one fetch task per hiring thread (the ``backfill`` fan-out)."""
    redis_client = redis.Redis.from_url(get_settings().redis_url)
    with worker_client(redis_client) as client:
        thread_ids = run_discover_threads(client, limit=limit, max_scan=max_scan)
    logger.info("discovered %d hiring threads to fetch", len(thread_ids))
    return queue_thread_tasks(thread_ids)


@app.task(name=REFRESH_RECENT_THREADS_TASK)
def refresh_recent_threads() -> dict[str, Any]:
    """Beat entry point: queue the current and previous month's threads (SPEC 8.3)."""
    redis_client = redis.Redis.from_url(get_settings().redis_url)
    with worker_client(redis_client) as client:
        thread_ids = run_discover_threads(client, limit=RECENT_THREADS)
    return queue_thread_tasks(thread_ids)


def run_refresh_trend_views(engine: Engine) -> None:
    """Refresh both materialised views; ``REFRESH ... CONCURRENTLY`` needs AUTOCOMMIT.

    The AUTOCOMMIT connection lives in :func:`skillpulse.trends.refresh_trend_views`, so
    this wrapper never opens its own transaction around the refresh (which Postgres
    rejects for ``CONCURRENTLY``).
    """
    refresh_trend_views(engine, concurrently=True)


@app.task(name=REFRESH_TREND_VIEWS_TASK)
def refresh_trend_views_task() -> dict[str, Any]:
    """Nightly beat task: refresh the trend views after extraction (SPEC section 8)."""
    engine = make_engine(get_settings().database_url)
    try:
        run_refresh_trend_views(engine)
    finally:
        engine.dispose()
    return {"status": "ok", "views": ["skill_trend_mv", "skill_segment_mv"]}


def build_worker_extractor(taxonomy: Taxonomy, redis_client: redis.Redis) -> LLMExtractor | None:
    """Build the LLM extractor through the default-closed provider gate (D007).

    The shared Redis budget the provider factory needs is the same Redis the workers
    already use for politeness and locks. While ``SKILLPULSE_LLM_ENABLED`` is false this
    returns ``None`` and extraction stays on the offline baseline.
    """
    return build_extractor(taxonomy, redis_client=redis_client)


@app.task(
    name=EXTRACT_PENDING_TASK,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=_settings.celery_task_max_retries,
)
def extract_pending(
    limit: int | None = None, batch_size: int | None = None
) -> dict[str, Any]:
    """Extract pending posts in bounded batches (``worker-skills``).

    A transient database ``OperationalError`` is retried at most
    ``SKILLPULSE_CELERY_TASK_MAX_RETRIES`` times, matching the fetch task. The extractor
    gate is checked here: a provider failure inside a post falls back to the baseline
    (stored flagged) rather than failing the whole run.
    """
    settings = get_settings()
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)
    redis_client = redis.Redis.from_url(settings.redis_url)
    try:
        llm = build_worker_extractor(taxonomy, redis_client)
    except Exception:
        redis_client.close()
        raise
    try:
        result: ExtractionBatchResult = run_extract_pending(
            build_session_factory(),
            taxonomy=taxonomy,
            baseline=baseline,
            llm=llm,
            limit=limit if limit is not None else settings.skill_extract_max_per_run,
            batch_size=batch_size if batch_size is not None else settings.skill_extract_batch_size,
        )
    finally:
        redis_client.close()
    return {
        "status": "ok",
        "extractor": "llm" if llm is not None else "baseline",
        "processed": result.processed,
        "llm": result.llm,
        "fallback": result.fallback,
    }
