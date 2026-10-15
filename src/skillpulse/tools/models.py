"""Typed arguments and results for the shared read-only tools (SPEC section 9).

The agent (M4) and the MCP server (M5) call the same functions in
:mod:`skillpulse.tools.registry`, so every argument and every result shape is defined
once here. Arguments are Pydantic models with bounded ranges: an out-of-range
``top_n``/``months``/``limit`` is rejected before any SQL runs, and a tool never accepts
a SQL fragment from a caller or a model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Extractor = Literal["baseline", "llm"]
MIN_SAMPLE_POSTS = 30
# ``total_posts`` is the eligible-post denominator, not full source coverage. Tools
# return this text with every result so an answer can state what the denominator means
# and how far the stored data reaches (SPEC sections 8 and 9).
ELIGIBLE_POSTS_DEFINITION = (
    "posts that have an extraction for the requested extractor and taxonomy version "
    "(eligible), not all stored posts"
)
SOURCE_COVERAGE_CAVEAT = (
    "Counts cover eligible stored posts only; Hacker News threads the project has not "
    "fetched are not represented. Data leans US/remote with few European and almost no "
    "Greek posts."
)


class ToolFilters(BaseModel):
    """Filters shared by every tool; all bounds are enforced before SQL runs."""

    model_config = ConfigDict(frozen=True)

    extractor: Extractor = "baseline"
    role_family: str | None = None
    remote: bool | None = None
    months: int | None = Field(default=None, ge=1, le=240)
    month_from: date | None = None
    month_to: date | None = None


class SearchJobsArgs(ToolFilters):
    """``search_jobs``: up to 20 structured post rows, never post text."""

    skill_id: str | None = None
    limit: int = Field(default=20, ge=1, le=20)


class SkillCountsArgs(ToolFilters):
    """``skill_counts``: the most requested skills for a filter."""

    top_n: int = Field(default=10, ge=1, le=50)


class SkillTrendArgs(ToolFilters):
    """``skill_trend``: one skill's monthly numerator/denominator series."""

    skill_id: str


class CompareSkillsArgs(ToolFilters):
    """``compare_skills``: side-by-side trends for 1..5 skills."""

    skill_ids: list[str] = Field(min_length=1, max_length=5)


class MatchCvArgs(ToolFilters):
    """``match_cv``: CV skills versus the most demanded skills (no LLM, no storage)."""

    cv_text: str = Field(min_length=1, max_length=20_000)
    top_n: int = Field(default=10, ge=1, le=50)


class ToolResult(BaseModel):
    """One immutable tool output.

    ``result_id`` is a content hash of the tool, its validated arguments and the exact
    rows/values returned, so a citation points at a specific output rather than a tool
    name. ``row_count`` and ``truncated`` make a capped result visible instead of
    silently short.
    """

    model_config = ConfigDict(frozen=True)

    result_id: str
    tool: str
    args: dict[str, Any]
    rows: list[dict[str, Any]]
    values: dict[str, Any]
    row_count: int
    truncated: bool
    extractor: str
    taxonomy_version: str
    denominator_definition: str = ELIGIBLE_POSTS_DEFINITION
    coverage_caveat: str = SOURCE_COVERAGE_CAVEAT
    sample_floor: int = MIN_SAMPLE_POSTS


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def make_result_id(tool: str, args: dict[str, Any], rows: list[dict[str, Any]],
                   values: dict[str, Any]) -> str:
    """Content-address a tool output so the same query returns the same id."""
    digest = hashlib.sha256(
        _canonical({"tool": tool, "args": args, "rows": rows, "values": values}).encode("utf-8")
    ).hexdigest()
    return f"tr-{digest[:16]}"


def as_month(value: object) -> str:
    """Render a stored month as ``YYYY-MM-DD`` across Postgres and SQLite fixtures."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]
