"""Extraction orchestration: pick the extractor, store the result, fall back.

Order per SPEC section 8.2.4: try the LLM when one is configured; on timeout, invalid
JSON, provider failure or an exhausted budget, store the baseline result flagged as a
fallback so the post is visible in trends and retried on the next run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.llm import LLMExtractionError, LLMExtractor
from skillpulse.extraction.repository import record_extraction
from skillpulse.extraction.schema import PostExtraction
from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.models import Post

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtractionOutcome:
    """What happened for one post: the stored extraction and whether it fell back."""

    extraction: PostExtraction
    fallback: bool
    error: str | None = None


def extract_post(
    session: Session,
    post: Post,
    *,
    taxonomy: Taxonomy,
    baseline: BaselineExtractor,
    llm: LLMExtractor | None = None,
) -> ExtractionOutcome:
    """Extract one post with the LLM when available, else the baseline, and store it."""
    if llm is not None:
        try:
            extraction = llm.extract(post.text_clean)
            record_extraction(session, post, extraction, taxonomy)
            return ExtractionOutcome(extraction=extraction, fallback=False)
        except LLMExtractionError as exc:
            logger.warning("LLM extraction failed for post %s; using baseline: %s", post.id, exc)
            fallback = baseline.extract(post.text_clean).model_copy(update={"fallback": True})
            record_extraction(session, post, fallback, taxonomy)
            return ExtractionOutcome(extraction=fallback, fallback=True, error=str(exc))

    extraction = baseline.extract(post.text_clean)
    record_extraction(session, post, extraction, taxonomy)
    return ExtractionOutcome(extraction=extraction, fallback=False)
