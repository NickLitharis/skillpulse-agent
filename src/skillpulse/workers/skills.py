"""``worker-skills``: run extraction over stored posts in bounded, idempotent batches.

SPEC section 8.2 and the M3 follow-up. The task bodies in
:mod:`skillpulse.workers.tasks` call these helpers, and the tests call them directly, so
the batch logic is provable without Redis or a broker.

Rules that make the worker safe to re-run:

* A post is pending when it has no extraction for the requested extractor and taxonomy
  version. The LLM extractor additionally retries posts whose stored result is a
  fallback, so a provider that failed yesterday is retried today.
* The pending ids are chosen once, then processed in bounded batches, each batch in its
  own transaction. A post that fell back within this run is not re-picked in the same
  run, so a single task run is finite.
* Persistence is replace-by-identity (:func:`skillpulse.extraction.repository.record_extraction`),
  so re-running a batch never creates duplicate ``post_skills`` rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.db import session_scope
from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.llm import LLMExtractor
from skillpulse.extraction.repository import sync_taxonomy
from skillpulse.extraction.service import extract_post
from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.models import Post, PostExtractionRow


@dataclass(frozen=True)
class ExtractionBatchResult:
    """Counts from one bounded extraction run."""

    processed: int = 0
    llm: int = 0
    fallback: int = 0
    pending_remaining: int = 0


def _pending_condition(*, extractor: str, taxonomy_version: str) -> object:
    """Return the SQLAlchemy predicate for a post that still needs an extraction."""
    if extractor == "llm":
        # A successful LLM row counts as done; a fallback row does not, so the post is
        # retried on the next run (D007).
        return ~exists().where(
            PostExtractionRow.post_id == Post.id,
            PostExtractionRow.extractor == "llm",
            PostExtractionRow.taxonomy_version == taxonomy_version,
            PostExtractionRow.is_fallback.is_(False),
        )
    return ~exists().where(
        PostExtractionRow.post_id == Post.id,
        PostExtractionRow.extractor == extractor,
        PostExtractionRow.taxonomy_version == taxonomy_version,
    )


def select_pending_post_ids(
    session: Session,
    *,
    taxonomy_version: str,
    extractor: str,
    limit: int,
) -> list[int]:
    """Select pending posts, prioritizing untouched LLM work over oldest retries."""
    condition = _pending_condition(extractor=extractor, taxonomy_version=taxonomy_version)
    stmt = (
        select(Post.id)
        .where(Post.is_deleted.is_(False), Post.is_dead.is_(False))
        .where(condition)  # type: ignore[arg-type]
    )
    if extractor == "llm":
        # Fallback rows are replaced on every failed attempt. Their IDs order retries
        # without timestamp ties; posts with no fallback for this taxonomy come first.
        latest_fallback = (
            select(func.max(PostExtractionRow.id))
            .where(
                PostExtractionRow.post_id == Post.id,
                PostExtractionRow.taxonomy_version == taxonomy_version,
                PostExtractionRow.is_fallback.is_(True),
            )
            .scalar_subquery()
        )
        stmt = stmt.order_by(latest_fallback.asc().nulls_first())
    stmt = stmt.order_by(Post.id).limit(limit)
    return [int(post_id) for post_id in session.scalars(stmt)]


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def run_extract_pending(
    factory: sessionmaker[Session],
    *,
    taxonomy: Taxonomy,
    baseline: BaselineExtractor,
    llm: LLMExtractor | None = None,
    limit: int,
    batch_size: int,
) -> ExtractionBatchResult:
    """Extract every pending post up to ``limit``, in committed batches.

    ``llm`` is the already-built extractor (``None`` while the provider gate is closed,
    which is the default), so this function makes no provider decision and no network
    call of its own.
    """
    if limit <= 0 or batch_size <= 0:
        raise ValueError("limit and batch_size must be positive")
    extractor = "llm" if llm is not None else "baseline"

    with session_scope(factory) as session:
        sync_taxonomy(session, taxonomy)
        pending = select_pending_post_ids(
            session, taxonomy_version=taxonomy.version, extractor=extractor, limit=limit
        )

    processed = 0
    used_llm = 0
    fallback = 0
    for chunk in _chunks(pending, batch_size):
        with session_scope(factory) as session:
            posts = {
                post.id: post
                for post in session.scalars(select(Post).where(Post.id.in_(chunk)))
            }
            for post_id in chunk:
                post = posts.get(post_id)
                if post is None:  # deleted between selection and this batch
                    continue
                outcome = extract_post(
                    session, post, taxonomy=taxonomy, baseline=baseline, llm=llm
                )
                processed += 1
                if outcome.fallback:
                    fallback += 1
                elif llm is not None:
                    used_llm += 1
    return ExtractionBatchResult(
        processed=processed,
        llm=used_llm,
        fallback=fallback,
        pending_remaining=max(0, len(pending) - processed),
    )
