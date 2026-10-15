"""Postgres integration test: idempotent ingestion and Alembic migration (SPEC S5).

The point is to prove the no-duplicate rule on the real dialect, with the real
migration, not on SQLite. Marked ``integration`` so it can be deselected where
Docker is unavailable.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer

from skillpulse.fetch_service import fetch_thread
from skillpulse.hn_client import HackerNewsClient, HackerNewsError, RateLimiter
from skillpulse.models import FetchRun, Post
from skillpulse.repository import count_posts, upsert_thread
from tests.conftest import make_item

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeHNClient:
    """In-memory HN client; the integration test only needs the DB to be real."""

    def __init__(self, items: dict[int, object]) -> None:
        self.items = items

    def get_item(self, item_id: int):
        return self.items.get(item_id)

    def get_user_submitted_ids(self, username: str) -> tuple[int, ...]:
        return ()


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """Start Postgres 16, run the Alembic migration, yield the admin SQLAlchemy URL."""
    with PostgresContainer("postgres:16-alpine") as container:
        url = container.get_connection_url()
        # testcontainers returns psycopg2 (sync) URL; the app uses the same driver.
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        yield url


@pytest.fixture
def factory(postgres_url: str) -> sessionmaker[Session]:
    """A session factory on the migrated container database."""
    engine = create_engine(postgres_url, pool_pre_ping=True)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_alembic_migration_creates_expected_tables(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"threads", "posts", "fetch_runs", "skills"} <= tables
    assert "alembic_version" in tables


def test_fetch_same_thread_twice_creates_no_duplicates(
    factory: sessionmaker[Session],
) -> None:
    thread_item = make_item(
        item_id=5000,
        title="Ask HN: Who is hiring? (October 2026)",
        kids=(6001, 6002, 6003),
    )
    items = {
        5000: thread_item,
        6001: make_item(item_id=6001, text="<p>Acme | Backend | Remote</p>"),
        6002: make_item(item_id=6002, text="<p>Beta | Frontend | Berlin</p>"),
        6003: make_item(item_id=6003, text="<p>Gamma | Data | London</p>"),
    }
    client = FakeHNClient(items)

    with factory() as session:
        first = fetch_thread(session, client, 5000)  # type: ignore[arg-type]
    with factory() as session:
        second = fetch_thread(session, client, 5000)  # type: ignore[arg-type]

    assert first.result.created == 3
    # S5: fetching the same thread twice yields zero new posts.
    assert second.result.created == 0
    assert second.result.updated == 0
    assert second.result.unchanged == 3

    with factory() as session:
        assert count_posts(session, 5000) == 3
        # The primary key on the HN item id is what enforces this.
        ids = session.scalars(select(Post.id).where(Post.thread_id == 5000)).all()
        assert sorted(ids) == [6001, 6002, 6003]


def test_postgres_unique_pk_rejects_manual_duplicate(
    factory: sessionmaker[Session],
) -> None:
    thread_item = make_item(item_id=7000, title="Ask HN: Who is hiring? (October 2026)")
    month = datetime(2026, 10, 1, tzinfo=UTC)

    def post(text: str) -> Post:
        return Post(
            id=8001,
            thread_id=7000,
            month=month,
            time=month,
            text_raw=text,
            text_clean=text,
        )

    with factory() as session:
        upsert_thread(session, thread_item)
        session.commit()
        session.add(post("a"))
        session.commit()
        # The same HN item id must not be insertable twice.
        session.add(post("b"))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


HN_TEST_BASE = "https://hn.test/v0"


def _comment_json(item_id: int) -> dict[str, object]:
    return {
        "id": item_id,
        "type": "comment",
        "by": "someone",
        "text": f"<p>post {item_id}</p>",
        "time": 1_700_000_000,
    }


@respx.mock
def test_forced_midrun_failure_then_resume_on_postgres(
    factory: sessionmaker[Session],
) -> None:
    """S6 on the real dialect with the real client and controlled fake HTTP.

    Deterministic and failure-sensitive: the first run commits one batch of two posts,
    then the API returns a permanent HTTP 500 on the third child. No retries are
    configured, so the failure lands exactly at the batch boundary. The run must be
    marked ``failed`` with the resume point, its committed posts must survive, and the
    second run must finish the thread with no skip and no duplicate.
    """
    thread_id = 9100
    kid_ids = (9101, 9102, 9103, 9104, 9105)

    respx.get(f"{HN_TEST_BASE}/item/{thread_id}.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": thread_id,
                "type": "story",
                "by": "whoishiring",
                "title": "Ask HN: Who is hiring? (October 2026)",
                "time": 1_700_000_000,
                "kids": list(kid_ids),
            },
        )
    )
    for kid in kid_ids:
        respx.get(f"{HN_TEST_BASE}/item/{kid}.json").mock(
            return_value=httpx.Response(200, json=_comment_json(kid))
        )
    outage = respx.get(f"{HN_TEST_BASE}/item/{kid_ids[2]}.json")
    outage.mock(return_value=httpx.Response(500))

    client = HackerNewsClient(
        base_url=HN_TEST_BASE,
        max_retries=0,
        limiter=RateLimiter(1e6),
        sleep=lambda _: None,
    )
    with factory() as session, pytest.raises(HackerNewsError):
        fetch_thread(session, client, thread_id, batch_size=2)

    with factory() as session:
        failed = session.scalars(
            select(FetchRun).where(FetchRun.thread_id == thread_id)
        ).one()
        assert failed.status == "failed"
        assert failed.last_item_id == kid_ids[1]  # last item of the committed batch
        assert failed.posts_upserted == 2
        assert count_posts(session, thread_id) == 2  # durable progress survives

    # The transient cause is gone; the next run resumes from the committed point.
    outage.mock(return_value=httpx.Response(200, json=_comment_json(kid_ids[2])))
    with factory() as session:
        resumed = fetch_thread(session, client, thread_id, batch_size=2)

    assert resumed.result.created == 3  # the three posts the failed run never stored
    assert resumed.result.unchanged == 2  # the two it did store
    with factory() as session:
        runs = session.scalars(
            select(FetchRun).where(FetchRun.thread_id == thread_id).order_by(FetchRun.id)
        ).all()
        assert [run.status for run in runs] == ["failed", "ok"]
        assert count_posts(session, thread_id) == 5
        ids = session.scalars(select(Post.id).where(Post.thread_id == thread_id)).all()
        assert sorted(ids) == sorted(kid_ids)
