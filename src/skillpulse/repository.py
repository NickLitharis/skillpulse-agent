"""Database writes for ingestion.

The idempotency rule is the important part: the same HN item id must never create a
second post, so re-fetching a thread twice leaves the post count unchanged (SPEC S5).
A deleted or dead post is kept but flagged, so its skills stop counting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from skillpulse.hn_client import HNItem
from skillpulse.models import FetchRun, Post, Thread
from skillpulse.text_cleaning import (
    clean_comment_text,
    parse_thread_month,
    timestamp_to_datetime,
)


@dataclass
class UpsertResult:
    """Counts from one ingest batch, mirroring the ``fetch_runs`` columns."""

    seen: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0

    @property
    def upserted(self) -> int:
        """Posts written (created or updated); unchanged rows count as no-ops."""
        return self.created + self.updated


def upsert_thread(session: Session, item: HNItem) -> Thread:
    """Insert or update the thread row for a hiring-thread item.

    The month is parsed from the title and falls back to the item time, so a title
    change cannot silently move already-stored posts to a different month.
    """
    now = datetime.now(UTC)
    month = parse_thread_month(item.title or "")
    if month is None:
        month = timestamp_to_datetime(item.time) or now
    created_at = timestamp_to_datetime(item.time) or now

    thread = session.get(Thread, item.id)
    if thread is None:
        thread = Thread(
            id=item.id,
            title=item.title or "",
            month=month,
            created_at=created_at,
            descendant_count=item.descendants,
            fetched_at=now,
        )
        session.add(thread)
    else:
        thread.title = item.title or thread.title
        thread.month = month
        thread.created_at = created_at
        thread.descendant_count = item.descendants
        thread.fetched_at = now
    session.flush()
    return thread


def upsert_comment(session: Session, thread: Thread, item: HNItem) -> str:
    """Insert or update one top-level comment.

    Returns ``"created"``, ``"updated"`` or ``"unchanged"`` so callers can report
    real work rather than just row counts.
    """
    now = datetime.now(UTC)
    text_clean = clean_comment_text(item.text)
    commented_at = timestamp_to_datetime(item.time) or thread.month

    post = session.get(Post, item.id)
    if post is None:
        session.add(
            Post(
                id=item.id,
                thread_id=thread.id,
                month=thread.month,
                time=commented_at,
                text_raw=item.text or "",
                text_clean=text_clean,
                is_deleted=item.deleted,
                is_dead=item.dead,
                first_seen_at=now,
                updated_at=now,
            )
        )
        return "created"

    changed = (
        post.text_raw != (item.text or "")
        or post.is_deleted != item.deleted
        or post.is_dead != item.dead
    )
    if not changed:
        return "unchanged"

    post.text_raw = item.text or ""
    post.text_clean = text_clean
    post.is_deleted = item.deleted
    post.is_dead = item.dead
    post.thread_id = thread.id
    post.month = thread.month
    post.updated_at = now
    return "updated"


def upsert_comments(session: Session, thread: Thread, items: list[HNItem]) -> UpsertResult:
    """Upsert a batch of comments and return the counts."""
    result = UpsertResult()
    for item in items:
        result.seen += 1
        outcome = upsert_comment(session, thread, item)
        if outcome == "created":
            result.created += 1
        elif outcome == "updated":
            result.updated += 1
        else:
            result.unchanged += 1
    session.flush()
    return result


def start_fetch_run(session: Session, *, kind: str, thread_id: int | None = None) -> FetchRun:
    """Create a ``running`` fetch run row."""
    run = FetchRun(thread_id=thread_id, kind=kind, status="running")
    session.add(run)
    session.flush()
    return run


def finish_fetch_run(
    session: Session,
    run: FetchRun,
    *,
    status: str,
    result: UpsertResult | None = None,
    last_item_id: int | None = None,
    error: str | None = None,
) -> None:
    """Mark a fetch run finished, recording the resume point."""
    run.status = status
    run.finished_at = datetime.now(UTC)
    run.error = error
    if last_item_id is not None:
        run.last_item_id = last_item_id
    if result is not None:
        run.posts_seen = result.seen
        run.posts_upserted = result.upserted


def count_posts(session: Session, thread_id: int | None = None) -> int:
    """Count posts, optionally for one thread."""
    stmt = select(func.count()).select_from(Post)
    if thread_id is not None:
        stmt = stmt.where(Post.thread_id == thread_id)
    return int(session.scalar(stmt) or 0)
