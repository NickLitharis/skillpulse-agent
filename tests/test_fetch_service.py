"""Unit tests for ingestion logic using an in-memory SQLite DB and a fake HN client.

These cover the idempotency rule at the repository level. The Postgres-backed
integration test in ``test_ingest_postgres.py`` proves it on the real dialect.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.fetch_service import fetch_thread, find_hiring_threads
from skillpulse.hn_client import HackerNewsError
from skillpulse.models import Base, FetchRun, Post
from skillpulse.repository import count_posts, upsert_comments
from tests.conftest import make_item


class FakeHNClient:
    """A stand-in for ``HackerNewsClient`` backed by an in-memory item table."""

    def __init__(self, items: dict[int, object], submitted: tuple[int, ...] = ()) -> None:
        self.items = items
        self.submitted = submitted
        self.item_calls: list[int] = []

    def get_item(self, item_id: int):
        self.item_calls.append(item_id)
        return self.items.get(item_id)

    def get_user_submitted_ids(self, username: str) -> tuple[int, ...]:
        return self.submitted


@pytest.fixture
def factory() -> sessionmaker[Session]:
    """A session factory on a fresh in-memory database."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_find_hiring_threads_filters_siblings() -> None:
    items = {
        1: make_item(item_id=1, title="Ask HN: Who is hiring? (October 2026)"),
        2: make_item(item_id=2, title="Ask HN: Who wants to be hired? (October 2026)"),
        3: make_item(item_id=3, title="Ask HN: Freelancer? Seeking freelancer? (October 2026)"),
        4: make_item(item_id=4, title="Some other post"),
    }
    client = FakeHNClient(items, submitted=(1, 2, 3, 4))
    found = find_hiring_threads(client)  # type: ignore[arg-type]
    assert [t.id for t in found] == [1]
    assert found[0].month_label == "2026-10"


def test_upsert_is_idempotent_across_two_runs(factory: sessionmaker[Session]) -> None:
    thread_item = make_item(
        item_id=1000, title="Ask HN: Who is hiring? (October 2026)", kids=(2001, 2002)
    )
    comment = make_item(item_id=2001, text="<p>Acme | Backend | Remote<br>Python and k8s</p>")
    comment2 = make_item(item_id=2002, text="<p>Beta | Frontend | Berlin</p>")
    client = FakeHNClient({1000: thread_item, 2001: comment, 2002: comment2})

    with factory() as session:
        first = fetch_thread(session, client, 1000)  # type: ignore[arg-type]
    with factory() as session:
        second = fetch_thread(session, client, 1000)  # type: ignore[arg-type]

    assert first.result.created == 2
    assert second.result.created == 0
    assert second.result.updated == 0
    assert second.result.unchanged == 2
    with factory() as session:
        assert count_posts(session, 1000) == 2


def test_changed_comment_text_is_updated_not_duplicated(factory: sessionmaker[Session]) -> None:
    thread_item = make_item(
        item_id=1000, title="Ask HN: Who is hiring? (October 2026)", kids=(2001,)
    )
    original = make_item(item_id=2001, text="<p>Acme | Backend</p>")
    client = FakeHNClient({1000: thread_item, 2001: original})
    with factory() as session:
        fetch_thread(session, client, 1000)  # type: ignore[arg-type]

    edited = make_item(item_id=2001, text="<p>Acme | Backend | Now remote</p>")
    client.items[2001] = edited
    with factory() as session:
        second = fetch_thread(session, client, 1000)  # type: ignore[arg-type]

    assert second.result.updated == 1
    assert second.result.created == 0
    with factory() as session:
        posts = session.query(Post).all()
        assert len(posts) == 1
        assert "Now remote" in posts[0].text_clean


def test_deleted_comment_is_flagged_not_removed(factory: sessionmaker[Session]) -> None:
    thread_item = make_item(
        item_id=1000, title="Ask HN: Who is hiring? (October 2026)", kids=(2001,)
    )
    client = FakeHNClient({1000: thread_item, 2001: make_item(item_id=2001, text="<p>Acme</p>")})
    with factory() as session:
        fetch_thread(session, client, 1000)  # type: ignore[arg-type]

    client.items[2001] = make_item(item_id=2001, text="<p>Acme</p>", deleted=True)
    with factory() as session:
        fetch_thread(session, client, 1000)  # type: ignore[arg-type]

    with factory() as session:
        post = session.get(Post, 2001)
        assert post is not None
        assert post.is_deleted is True
        assert count_posts(session, 1000) == 1


def test_missing_child_item_is_skipped(factory: sessionmaker[Session]) -> None:
    thread_item = make_item(
        item_id=1000, title="Ask HN: Who is hiring? (October 2026)", kids=(9, 2001)
    )
    client = FakeHNClient({1000: thread_item, 2001: make_item(item_id=2001, text="<p>Acme</p>")})
    with factory() as session:
        result = fetch_thread(session, client, 1000)  # type: ignore[arg-type]
    assert result.result.created == 1


def test_upsert_comments_counts_created(factory: sessionmaker[Session]) -> None:
    thread_item = make_item(item_id=1000, title="Ask HN: Who is hiring? (October 2026)")
    from skillpulse.repository import upsert_thread

    with factory() as session:
        thread = upsert_thread(session, thread_item)
        items = [make_item(item_id=i, text=f"<p>post {i}</p>") for i in (1, 2, 3)]
        result = upsert_comments(session, thread, items)
        session.commit()
    assert result.seen == 3
    assert result.created == 3
    assert result.upserted == 3


class FailingHNClient(FakeHNClient):
    """Fails once a given item id is requested, to exercise the failure path."""

    def __init__(self, items: dict[int, object], fail_on: int) -> None:
        super().__init__(items)
        self.fail_on = fail_on

    def get_item(self, item_id: int):
        if item_id == self.fail_on:
            raise HackerNewsError(f"simulated HN outage on {item_id}")
        return super().get_item(item_id)


def test_failed_run_is_marked_with_resume_point(factory: sessionmaker[Session]) -> None:
    """S6: a mid-run failure marks the run failed and records where to resume."""
    thread_item = make_item(
        item_id=1000,
        title="Ask HN: Who is hiring? (October 2026)",
        kids=(2001, 2002, 2003),
    )
    client = FailingHNClient(
        {
            1000: thread_item,
            2001: make_item(item_id=2001, text="<p>one</p>"),
            2002: make_item(item_id=2002, text="<p>two</p>"),
            2003: make_item(item_id=2003, text="<p>three</p>"),
        },
        fail_on=2003,
    )

    with factory() as session, pytest.raises(HackerNewsError):
        # batch_size=2 commits 2001+2002 before the failure on 2003.
        fetch_thread(session, client, 1000, batch_size=2)  # type: ignore[arg-type]

    with factory() as session:
        run = session.scalars(select(FetchRun)).one()
        assert run.status == "failed"
        assert run.error is not None and "HackerNewsError" in run.error
        # The resume point is the last item in a committed batch.
        assert run.last_item_id == 2002
        # Posts in that committed batch survive the failure, so a retry resumes
        # rather than restarting from scratch.
        assert count_posts(session, 1000) == 2


def test_retry_after_failure_resumes_without_duplicates(
    factory: sessionmaker[Session],
) -> None:
    """S6: the next run after a failure creates no duplicates of already-stored posts."""
    thread_item = make_item(
        item_id=1000, title="Ask HN: Who is hiring? (October 2026)", kids=(2001, 2002)
    )
    items = {
        1000: thread_item,
        2001: make_item(item_id=2001, text="<p>one</p>"),
        2002: make_item(item_id=2002, text="<p>two</p>"),
    }
    flaky = FailingHNClient(items, fail_on=2002)
    with factory() as session, pytest.raises(HackerNewsError):
        # batch_size=1 commits 2001 before the failure on 2002.
        fetch_thread(session, flaky, 1000, batch_size=1)  # type: ignore[arg-type]

    healthy = FakeHNClient(items)
    with factory() as session:
        retry = fetch_thread(session, healthy, 1000)  # type: ignore[arg-type]

    assert retry.result.created == 1  # only the post missed in the failed run
    assert retry.result.unchanged == 1  # the post stored before the failure
    with factory() as session:
        assert count_posts(session, 1000) == 2


def test_missing_thread_raises(factory: sessionmaker[Session]) -> None:
    client = FakeHNClient({})
    with factory() as session, pytest.raises(HackerNewsError):
        fetch_thread(session, client, 999999)  # type: ignore[arg-type]
