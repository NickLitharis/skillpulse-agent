"""Shared read-only tools (SPEC section 9) used by the M4 agent and, later, M5 MCP.

Import from here rather than from the submodules: :data:`TOOL_SPECS`,
:func:`call_tool` and :class:`ToolRuntime` are the stable surface the agent and the MCP
server both use.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.tools.models import (
    ELIGIBLE_POSTS_DEFINITION,
    MIN_SAMPLE_POSTS,
    SOURCE_COVERAGE_CAVEAT,
    CompareSkillsArgs,
    MatchCvArgs,
    SearchJobsArgs,
    SkillCountsArgs,
    SkillTrendArgs,
    ToolFilters,
    ToolResult,
    make_result_id,
)
from skillpulse.tools.readonly import ReadOnlyViolation, readonly_session
from skillpulse.tools.registry import (
    TOOL_NAMES,
    ToolInputError,
    call_tool,
    compare_skills,
    match_cv,
    search_jobs,
    skill_counts,
    skill_trend_tool,
)

# The model sees name + description + the JSON schema of the bounded argument model.
TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_jobs",
        "description": "Structured post rows (id, month, company, role, remote, skill ids) "
        "for filters; max 20 rows; never returns post text.",
        "args_schema": SearchJobsArgs.model_json_schema(),
    },
    {
        "name": "skill_counts",
        "description": "Most requested skills for a filter, with posts_with_skill, "
        "eligible total_posts, share (null below 30 posts) and sample_sufficient.",
        "args_schema": SkillCountsArgs.model_json_schema(),
    },
    {
        "name": "skill_trend",
        "description": "Monthly share of posts mentioning one skill, with numerator and "
        "eligible denominator per month.",
        "args_schema": SkillTrendArgs.model_json_schema(),
    },
    {
        "name": "compare_skills",
        "description": "Side-by-side counts and monthly trends for 1 to 5 skills.",
        "args_schema": CompareSkillsArgs.model_json_schema(),
    },
    {
        "name": "match_cv",
        "description": "Skills found in CV text versus the most demanded skills (gap list). "
        "Uses the deterministic baseline, never an LLM; CV text is never stored.",
        "args_schema": MatchCvArgs.model_json_schema(),
    },
)

TOOL_SPECS_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}


class ToolRuntime:
    """Executes validated tools inside read-only, timed, row-capped sessions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        taxonomy: Taxonomy,
        *,
        statement_timeout_ms: int = 5000,
        max_rows: int = 200,
    ) -> None:
        self._factory = session_factory
        self._taxonomy = taxonomy
        self._statement_timeout_ms = statement_timeout_ms
        self._max_rows = max_rows

    @property
    def taxonomy(self) -> Taxonomy:
        """The taxonomy the tools validate skill ids and role families against."""
        return self._taxonomy

    @property
    def tool_specs(self) -> tuple[dict[str, Any], ...]:
        """The tool descriptions and argument schemas shown to the planner."""
        return TOOL_SPECS

    def call(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Validate and run one tool call."""
        with readonly_session(
            self._factory,
            statement_timeout_ms=self._statement_timeout_ms,
            max_rows=self._max_rows,
        ) as session:
            return call_tool(session, self._taxonomy, tool, args)


__all__ = [
    "ELIGIBLE_POSTS_DEFINITION",
    "MIN_SAMPLE_POSTS",
    "SOURCE_COVERAGE_CAVEAT",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "TOOL_SPECS_BY_NAME",
    "ReadOnlyViolation",
    "ToolFilters",
    "ToolInputError",
    "ToolResult",
    "ToolRuntime",
    "call_tool",
    "compare_skills",
    "make_result_id",
    "match_cv",
    "search_jobs",
    "skill_counts",
    "skill_trend_tool",
]
