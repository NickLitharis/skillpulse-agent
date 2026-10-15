"""Ingestion service: find hiring threads and fetch their posts.

This is the M1 slice of SPEC section 8 (fetching). It deliberately contains no Celery
wiring yet, so the logic is testable without Redis; the workers in M2 wrap these
functions in tasks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from skillpulse.db import session_scope
from skillpulse.hn_client import HackerNewsClient, HackerNewsError, HNItem
from skillpulse.repository import (
    UpsertResult,
    finish_fetch_run,
    start_fetch_run,
    upsert_comments,
    upsert_thread,
)
from skillpulse.text_cleaning import is_hiring_thread_title

logger = logging.getLogger(__name__)

WHOISHIRING_USER = "whoishiring"
# The API caps the submitted list, so scan a generous slice of the history.
MAX_SUBMITTED_TO_SCAN = 600


@dataclass(frozen=True)
class HiringThread:
    """A hiring thread found in the ``whoishiring`` submission history."""

    id: int
    title: str
    month_label: str


@dataclass
class ThreadFetchResult:
    """Outcome of fetching one thread."""

    thread_id: int
    status: str
    result: UpsertResult = field(default_factory=UpsertResult)
    children_found: int = 0
    error: str | None = None


def find_hiring_threads(
    client: HackerNewsClient,
    *,
    username: str = WHOISHIRING_USER,
    max_scan: int = MAX_SUBMITTED_TO_SCAN,
) -> list[HiringThread]:
    """Find every 'Ask HN: Who is hiring?' thread in a user's submissions.

    Only titles starting with 'Ask HN: Who is hiring?' are kept, so the sibling
    'Who wants to be hired?' and 'Freelancer?' threads are ignored (SPEC section 5).
    """
    submitted = client.get_user_submitted_ids(username)
    if not submitted:
        return []
    found: list[HiringThread] = []
    for item_id in submitted[:max_scan]:
        item = client.get_item(item_id)
        if item is None or not is_hiring_thread_title(item.title):
            continue
        found.append(
            HiringThread(
                id=item.id, title=item.title or "", month_label=_month_label(item.title or "")
            )
        )
        logger.debug("found hiring thread %s: %s", item.id, item.title)
    return found


def _month_label(title: str) -> str:
    """Extract '(October 2026)' as a compact '2026-10' label, for CLI output."""
    from skillpulse.text_cleaning import parse_thread_month

    month = parse_thread_month(title)
    return month.strftime("%Y-%m") if month else "unknown"


def fetch_thread(
    session: Session,
    client: HackerNewsClient,
    thread_id: int,
    *,
    batch_size: int = 100,
    on_batch: Callable[[UpsertResult], None] | None = None,
) -> ThreadFetchResult:
    """Fetch one thread's top-level comments and upsert them idempotently.

    Every comment is fetched through the rate-limited client and upserted on the HN
    item id, so running this twice on the same thread creates zero duplicate posts.
    A failed run is marked ``failed`` with its resume point recorded (SPEC S6).
    """
    thread_item = client.get_item(thread_id)
    if thread_item is None:
        raise HackerNewsError(f"thread {thread_id} does not exist")

    thread = upsert_thread(session, thread_item)
    run = start_fetch_run(session, kind="thread", thread_id=thread.id)
    session.commit()

    total = UpsertResult()
    pending: list[HNItem] = []
    # The resume point is the last item in a *committed* batch. Recording items that
    # were only read would point the next run past posts that were rolled back.
    committed_item_id: int | None = None
    try:
        for child_id in thread_item.kids:
            item = client.get_item(child_id)
            if item is None:
                continue
            pending.append(item)
            if len(pending) >= batch_size:
                total = _merge(total, upsert_comments(session, thread, pending), pending)
                session.commit()
                committed_item_id = pending[-1].id
                if on_batch is not None:
                    on_batch(total)
                pending = []
        if pending:
            total = _merge(total, upsert_comments(session, thread, pending), pending)
            session.commit()
            committed_item_id = pending[-1].id
            if on_batch is not None:
                on_batch(total)
    except Exception as exc:
        session.rollback()
        run = session.merge(run)
        finish_fetch_run(
            session,
            run,
            status="failed",
            result=total,
            last_item_id=committed_item_id,
            error=f"{type(exc).__name__}: {exc}"[:2000],
        )
        session.commit()
        logger.error("fetch of thread %s failed: %s", thread_id, exc)
        raise

    finish_fetch_run(session, run, status="ok", result=total, last_item_id=committed_item_id)
    session.commit()
    logger.info(
        "thread %s: %d comments seen, %d created, %d updated, %d unchanged",
        thread_id,
        total.seen,
        total.created,
        total.updated,
        total.unchanged,
    )
    return ThreadFetchResult(
        thread_id=thread.id,
        status="ok",
        result=total,
        children_found=len(thread_item.kids),
    )


def _merge(total: UpsertResult, batch: UpsertResult, items: Sequence[HNItem]) -> UpsertResult:
    """Accumulate batch counts into the run total."""
    total.seen += len(items)
    total.created += batch.created
    total.updated += batch.updated
    total.unchanged += batch.unchanged
    total.deleted += batch.deleted
    return total


def fetch_thread_by_id(
    factory: sessionmaker[Session],
    client: HackerNewsClient,
    thread_id: int,
    *,
    batch_size: int = 100,
    on_batch: Callable[[UpsertResult], None] | None = None,
) -> ThreadFetchResult:
    """Fetch a thread in its own transaction (the shape Celery tasks will call)."""
    with session_scope(factory) as session:
        return fetch_thread(session, client, thread_id, batch_size=batch_size, on_batch=on_batch)


def backfill(
    factory: sessionmaker[Session],
    client: HackerNewsClient,
    *,
    limit: int | None = None,
    max_scan: int = MAX_SUBMITTED_TO_SCAN,
) -> list[ThreadFetchResult]:
    """Fetch every hiring thread found, newest first.

    ``limit`` bounds the run so a first back-fill can be tried on a few threads.
    """
    threads = find_hiring_threads(client, max_scan=max_scan)
    if limit is not None:
        threads = threads[:limit]
    results: list[ThreadFetchResult] = []
    for thread in threads:
        logger.info("fetching thread %s (%s)", thread.id, thread.month_label)
        results.append(fetch_thread_by_id(factory, client, thread.id))
    return results
