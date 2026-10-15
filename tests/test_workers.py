"""Unit tests for the Celery worker wiring (no Redis, no network).

The task bodies wrap ``skillpulse.fetch_service``; these tests prove they do, that a
successful run writes the ``fetch_runs`` row, that a transient HN error is retried, that a
thread already held by another worker is skipped, and that discovery queues one fetch task
per thread.
"""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any, ClassVar

import pytest
import redis
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.config import get_settings
from skillpulse.hn_client import HackerNewsError
from skillpulse.models import Base, FetchRun
from skillpulse.repository import count_posts
from skillpulse.workers import redis_state
from skillpulse.workers import tasks as worker_tasks
from skillpulse.workers.app import (
    BEAT_REFRESH_RECENT,
    REFRESH_RECENT_TASK,
    app,
)
from skillpulse.workers.tasks import DISCOVER_THREADS_TASK, FETCH_THREAD_TASK
from tests.conftest import make_item
from tests.test_fetch_service import FakeHNClient

THREAD_ID = 1000
COMMENT_IDS = (2001, 2002)


class _FreeLock:
    """A stand-in for ``RedisThreadLock`` that is always acquired."""

    instances: ClassVar[list[_FreeLock]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.acquired = True
        self.refreshes = 0
        _FreeLock.instances.append(self)

    def __enter__(self) -> _FreeLock:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def refresh(self) -> None:
        self.refreshes += 1


class _BusyLock(_FreeLock):
    """A stand-in for a lock held by another worker."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self.acquired = False


class _LostLock(_FreeLock):
    """A stand-in for a lock whose renewal discovers the worker no longer owns it."""

    def refresh(self) -> None:
        raise redis_state.ThreadLockLostError("lock taken by another worker")


@pytest.fixture
def factory() -> sessionmaker[Session]:
    """A session factory on a fresh in-memory database."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def thread_items() -> dict[int, object]:
    """One hiring thread with two comments, as the fake client sees it."""
    return {
        THREAD_ID: make_item(
            item_id=THREAD_ID,
            title="Ask HN: Who is hiring? (October 2026)",
            kids=COMMENT_IDS,
        ),
        2001: make_item(item_id=2001, text="<p>Acme | Backend | Remote</p>"),
        2002: make_item(item_id=2002, text="<p>Beta | Frontend | Berlin</p>"),
    }


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    factory: sessionmaker[Session] | dict[int, object],
    client: object,
    lock: type = _FreeLock,
) -> None:
    """Point the tasks at an in-memory factory, a fake client and a fake lock."""
    monkeypatch.setattr(worker_tasks, "build_session_factory", lambda: factory)
    monkeypatch.setattr(worker_tasks, "build_client", lambda **_: nullcontext(client))
    monkeypatch.setattr(worker_tasks, "RedisThreadLock", lock)


def test_run_fetch_thread_summarises_and_is_idempotent(factory: sessionmaker[Session]) -> None:
    client = FakeHNClient(thread_items())

    first = worker_tasks.run_fetch_thread(factory, client, THREAD_ID)  # type: ignore[arg-type]
    assert first["status"] == "ok"
    assert first["created"] == 2
    assert first["children_found"] == 2

    second = worker_tasks.run_fetch_thread(factory, client, THREAD_ID)  # type: ignore[arg-type]
    assert second["created"] == 0
    assert second["unchanged"] == 2


def test_fetch_thread_task_writes_posts_and_ok_run(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _FreeLock.instances = []
    _patch_runtime(monkeypatch, factory, FakeHNClient(thread_items()))

    result = worker_tasks.fetch_thread(THREAD_ID)

    assert result["created"] == 2
    # The lock TTL is refreshed once per committed batch while the fetch runs.
    assert _FreeLock.instances[-1].refreshes == 1
    with factory() as session:
        assert count_posts(session, THREAD_ID) == 2
        run = session.scalars(select(FetchRun)).one()
        assert run.status == "ok"
        assert run.posts_upserted == 2


def test_fetch_task_skips_when_thread_is_locked(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runtime(monkeypatch, factory, FakeHNClient(thread_items()), lock=_BusyLock)

    result = worker_tasks.fetch_thread(THREAD_ID)

    assert result["status"] == "locked"
    assert result["created"] == 0
    with factory() as session:
        # A skipped fetch must not touch the database at all.
        assert count_posts(session, THREAD_ID) == 0
        assert session.scalars(select(FetchRun)).all() == []


def test_locked_fetch_aborts_when_lock_renewal_fails_closed(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_runtime(monkeypatch, factory, FakeHNClient(thread_items()), lock=_LostLock)

    # Losing the lock mid-fetch must not look like a success and must not keep fetching.
    with pytest.raises(redis_state.ThreadLockLostError):
        worker_tasks.fetch_thread(THREAD_ID)

    with factory() as session:
        run = session.scalars(select(FetchRun)).one()
        assert run.status == "failed"


def test_thread_lock_refresh_fails_closed_on_ownership_loss() -> None:
    class StubRedisLock:
        def extend(self, *args: Any, **kwargs: Any) -> None:
            raise redis.exceptions.LockNotOwnedError("lock expired")

        def release(self) -> None:
            return None

    class StubClient:
        def lock(self, *args: Any, **kwargs: Any) -> StubRedisLock:
            return StubRedisLock()

    lock = redis_state.RedisThreadLock(StubClient(), 1, ttl_seconds=5)  # type: ignore[arg-type]
    lock.acquired = True

    with pytest.raises(redis_state.ThreadLockLostError):
        lock.refresh()
    assert lock.acquired is False


def test_worker_client_uses_the_redis_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every worker HN client must draw from the one Redis token bucket (SPEC 8.5)."""
    captured: dict[str, Any] = {}
    sentinel = object()
    monkeypatch.setattr(worker_tasks, "RedisRateLimiter", lambda *a, **k: sentinel)

    def fake_build_client(**kwargs: Any):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return nullcontext(FakeHNClient({}))

    monkeypatch.setattr(worker_tasks, "build_client", fake_build_client)

    with worker_tasks.worker_client(object()):  # type: ignore[arg-type]
        pass

    assert captured["limiter"] is sentinel


def test_fetch_task_is_registered_with_retry_policy() -> None:
    assert FETCH_THREAD_TASK in app.tasks
    task = worker_tasks.fetch_thread
    # A transient HN failure must be retried, and the retry count comes from settings.
    assert HackerNewsError in task.autoretry_for
    assert task.retry_backoff is True
    assert task.max_retries == get_settings().celery_task_max_retries


def test_fetch_task_retries_then_propagates_hn_error(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"count": 0}

    class FailingClient(FakeHNClient):
        def get_item(self, item_id: int):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise HackerNewsError("simulated HN outage")

    _patch_runtime(monkeypatch, factory, FailingClient({}))
    monkeypatch.setattr(worker_tasks.app.conf, "task_always_eager", True, raising=False)
    monkeypatch.setattr(worker_tasks.app.conf, "task_eager_propagates", False, raising=False)

    task = worker_tasks.fetch_thread
    # apply() runs eagerly and re-invokes the task on each Retry, so the count below is
    # the real number of attempts Celery made.
    result = task.apply(args=[THREAD_ID])

    assert result.failed()
    assert isinstance(result.result, HackerNewsError)
    # One initial attempt plus one per allowed retry — a retry policy that stopped
    # retrying, or retried forever, would change this count.
    assert calls["count"] == task.max_retries + 1


def test_discover_threads_queues_one_task_per_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = {
        1: make_item(item_id=1, title="Ask HN: Who is hiring? (October 2026)"),
        2: make_item(item_id=2, title="Ask HN: Who is hiring? (September 2026)"),
        3: make_item(item_id=3, title="Ask HN: Who wants to be hired? (September 2026)"),
    }
    _patch_runtime(monkeypatch, {}, FakeHNClient(items, submitted=(1, 2, 3)))
    queued: list[int] = []
    monkeypatch.setattr(worker_tasks.fetch_thread, "delay", lambda tid: queued.append(tid))

    result = worker_tasks.discover_threads()

    assert queued == [1, 2]
    assert result == {"queued": 2, "thread_ids": [1, 2]}
    assert DISCOVER_THREADS_TASK in app.tasks


def test_refresh_recent_threads_queues_current_and_previous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    items = {
        1: make_item(item_id=1, title="Ask HN: Who is hiring? (October 2026)"),
        2: make_item(item_id=2, title="Ask HN: Who is hiring? (September 2026)"),
        3: make_item(item_id=3, title="Ask HN: Who is hiring? (August 2026)"),
    }
    _patch_runtime(monkeypatch, {}, FakeHNClient(items, submitted=(1, 2, 3)))
    queued: list[int] = []
    monkeypatch.setattr(worker_tasks.fetch_thread, "delay", lambda tid: queued.append(tid))

    result = worker_tasks.refresh_recent_threads()

    assert queued == [1, 2]
    assert result["queued"] == 2
    # The beat schedule must actually point at this task.
    assert app.conf.beat_schedule[BEAT_REFRESH_RECENT]["task"] == REFRESH_RECENT_TASK


def test_run_discover_threads_respects_limit() -> None:
    items = {
        1: make_item(item_id=1, title="Ask HN: Who is hiring? (October 2026)"),
        2: make_item(item_id=2, title="Ask HN: Who is hiring? (September 2026)"),
    }
    client = FakeHNClient(items, submitted=(1, 2))
    assert worker_tasks.run_discover_threads(client, limit=1) == [1]  # type: ignore[arg-type]


def test_seconds_until_token_is_the_refill_wait() -> None:
    assert redis_state.seconds_until_token(1.0, 10.0) == 0.0
    assert redis_state.seconds_until_token(0.0, 10.0) == pytest.approx(0.1)
    assert redis_state.seconds_until_token(0.5, 10.0) == pytest.approx(0.05)
    # Never busy-spin, even when the bucket is empty and the rate is very high.
    assert redis_state.seconds_until_token(0.0, 1_000_000.0) >= 0.001
