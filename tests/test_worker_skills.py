"""Offline tests for the ``worker-skills`` extraction task and the nightly refresh.

No Redis, broker or network: the extraction helpers run against an isolated in-memory
SQLite database, which proves the batch loop, idempotency and fallback behaviour without
touching the shared Postgres/Redis services.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from skillpulse.config import get_settings
from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.llm import LLMExtractionError, LLMExtractor
from skillpulse.extraction.taxonomy import load_taxonomy
from skillpulse.models import Base, Post, PostExtractionRow, PostSkillRow, Thread
from skillpulse.workers import tasks as worker_tasks
from skillpulse.workers.app import (
    BEAT_EXTRACT_PENDING,
    BEAT_REFRESH_TRENDS,
    EXTRACT_PENDING_TASK,
    REFRESH_TREND_VIEWS_TASK,
    app,
)
from skillpulse.workers.skills import run_extract_pending, select_pending_post_ids

MONTH = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Thread(id=1, title="t", month=MONTH, created_at=MONTH))
        session.flush()
        for post_id in range(1, 4):
            session.add(
                Post(
                    id=post_id,
                    thread_id=1,
                    month=MONTH,
                    time=MONTH,
                    text_raw="Python and Docker required",
                    text_clean="Python and Docker required",
                )
            )
        session.commit()
    return sessionmaker(bind=engine, expire_on_commit=False)


class _FailingClient:
    def complete_json(self, *, system: str, prompt: str) -> str:
        raise LLMExtractionError("provider down")


def test_baseline_extraction_runs_offline_and_is_idempotent(
    factory: sessionmaker[Session],
) -> None:
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)

    first = run_extract_pending(factory, taxonomy=taxonomy, baseline=baseline, limit=10,
                                batch_size=2)
    assert first.processed == 3
    assert first.fallback == 0
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(PostExtractionRow)) == 3
        assert session.scalar(select(func.count()).select_from(PostSkillRow)) == 6

    second = run_extract_pending(factory, taxonomy=taxonomy, baseline=baseline, limit=10,
                                 batch_size=2)
    assert second.processed == 0
    with factory() as session:
        # Re-running creates no duplicate skill rows (replace-by-identity).
        assert session.scalar(select(func.count()).select_from(PostSkillRow)) == 6


def test_extraction_run_is_bounded_by_limit(factory: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)
    first = run_extract_pending(factory, taxonomy=taxonomy, baseline=baseline, limit=2,
                                batch_size=1)
    assert first.processed == 2
    assert first.pending_remaining == 0  # pending count was bounded to the limit
    with factory() as session:
        remaining = select_pending_post_ids(
            session, taxonomy_version=taxonomy.version, extractor="baseline", limit=10
        )
    assert remaining == [3]


def test_llm_failure_stores_flagged_baseline_and_retries_next_run(
    factory: sessionmaker[Session],
) -> None:
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)
    llm = LLMExtractor(taxonomy, _FailingClient())  # type: ignore[arg-type]

    result = run_extract_pending(factory, taxonomy=taxonomy, baseline=baseline, llm=llm,
                                 limit=10, batch_size=3)
    assert result.processed == 3
    assert result.llm == 0
    assert result.fallback == 3
    with factory() as session:
        flags = session.scalars(select(PostExtractionRow.is_fallback)).all()
        assert all(flags)
        pending = select_pending_post_ids(
            session, taxonomy_version=taxonomy.version, extractor="llm", limit=10
        )
    assert pending == [1, 2, 3]  # the failed posts are retried next run


@pytest.mark.parametrize("poison", [False, True])
def test_pending_extraction_reaches_untouched_posts_after_provider_failures(
    factory: sessionmaker[Session], poison: bool,
) -> None:
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)
    with factory.begin() as session:
        for post in session.scalars(select(Post).where(Post.id < 3)):
            post.text_clean = "POISON Python and Docker required"

    class Client:
        def complete_json(self, *, system: str, prompt: str) -> str:
            if poison and "POISON" in prompt:
                raise LLMExtractionError("permanent per-post failure")
            return '{"skills": []}'

    llm = LLMExtractor(taxonomy, Client())  # type: ignore[arg-type]
    for _ in range(2):
        run_extract_pending(factory, taxonomy=taxonomy, baseline=baseline, llm=llm,
                            limit=2, batch_size=2)

    with factory() as session:
        healthy_result = session.scalar(
            select(PostExtractionRow).where(
                PostExtractionRow.post_id == 3,
                PostExtractionRow.extractor == "llm",
                PostExtractionRow.taxonomy_version == taxonomy.version,
            )
        )
        assert healthy_result is not None
        assert healthy_result.is_fallback is False


def test_pending_extraction_rotates_fallback_retries(factory: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)
    llm = LLMExtractor(taxonomy, _FailingClient())  # type: ignore[arg-type]
    run_extract_pending(factory, taxonomy=taxonomy, baseline=baseline, llm=llm,
                        limit=3, batch_size=3)
    run_extract_pending(factory, taxonomy=taxonomy, baseline=baseline, llm=llm,
                        limit=1, batch_size=1)
    with factory() as session:
        pending = select_pending_post_ids(
            session, taxonomy_version=taxonomy.version, extractor="llm", limit=3
        )
    assert pending == [2, 3, 1]


def test_extract_task_registered_with_bounded_retries() -> None:
    assert EXTRACT_PENDING_TASK in app.tasks
    task = worker_tasks.extract_pending
    assert task.max_retries == get_settings().celery_task_max_retries
    assert task.retry_backoff is True


def test_beat_schedule_includes_extraction_and_nightly_refresh() -> None:
    schedule = app.conf.beat_schedule
    assert schedule[BEAT_EXTRACT_PENDING]["task"] == EXTRACT_PENDING_TASK
    assert schedule[BEAT_REFRESH_TRENDS]["task"] == REFRESH_TREND_VIEWS_TASK
    assert schedule[BEAT_REFRESH_TRENDS]["schedule"].hour == {
        get_settings().trend_refresh_hour_utc
    }


def test_refresh_task_uses_autocommit_helper_and_disposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, Any] = {}

    class FakeEngine:
        def dispose(self) -> None:
            calls["disposed"] = True

    engine = FakeEngine()
    monkeypatch.setattr(worker_tasks, "make_engine", lambda url: engine)
    monkeypatch.setattr(
        worker_tasks, "run_refresh_trend_views", lambda eng: calls.setdefault("engine", eng)
    )

    result = worker_tasks.refresh_trend_views_task()

    assert result["status"] == "ok"
    assert calls["engine"] is engine
    assert calls["disposed"] is True
