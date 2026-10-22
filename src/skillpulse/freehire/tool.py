"""The freehire tool: live vacancies as a bounded, read-only ``ToolResult``.

The agent's other tools read the local Hacker News corpus. This one reads an external
feed, so it says so in the result: a different denominator, a different coverage
caveat, and its own extractor name. The tool is namespaced (``freehire_*``) so an
answer can never quietly mix the two sources.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.freehire.client import FreehireClient, SearchPage
from skillpulse.tools import ToolRuntime
from skillpulse.tools.models import ToolResult, make_result_id

FREEHIRE_TOOL_NAME = "freehire_search_jobs"
FREEHIRE_EXTRACTOR = "freehire-api"
FREEHIRE_TAXONOMY_VERSION = "freehire-live"
FREEHIRE_DENOMINATOR = (
    "Live open vacancies reported by freehire.me at query time; not the HN corpus."
)
FREEHIRE_CAVEAT = (
    "External feed: counts change as company boards change, and coverage is freehire.me's, "
    "not the Hacker News hiring threads."
)


class FreehireSearchArgs(BaseModel):
    """Bounded arguments for the freehire search tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    q: str | None = Field(default=None, max_length=120)
    skills: list[str] = Field(default_factory=list, max_length=10)
    countries: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Country names or ISO codes; both are accepted (Greece or GR).",
    )
    regions: list[str] = Field(default_factory=list, max_length=5)
    remote: bool = True
    limit: int = Field(default=10, ge=1, le=20)


FREEHIRE_TOOL_SPEC: dict[str, Any] = {
    "name": FREEHIRE_TOOL_NAME,
    "description": (
        "Search live open vacancies on freehire.me (an external job feed, not the HN "
        "posts). Returns title, company, location, work mode, skills and the posting "
        "URL. Use it when the question asks for real jobs to apply to, and say that the "
        "result comes from an external feed. Country filters accept a name or an ISO code."
    ),
    "args_schema": FreehireSearchArgs.model_json_schema(),
}


def search_result(args: FreehireSearchArgs, page: SearchPage) -> ToolResult:
    """Build the immutable tool result the grounding check will see."""
    values: dict[str, Any] = {
        "total": page.total,
        "returned": len(page.rows),
        "source": "freehire.me",
    }
    if page.ignored:
        values["ignored_params"] = page.ignored
    return ToolResult(
        result_id=make_result_id(FREEHIRE_TOOL_NAME, args.model_dump(), page.rows, values),
        tool=FREEHIRE_TOOL_NAME,
        args=args.model_dump(),
        rows=page.rows,
        values=values,
        row_count=len(page.rows),
        truncated=page.total > len(page.rows),
        extractor=FREEHIRE_EXTRACTOR,
        taxonomy_version=FREEHIRE_TAXONOMY_VERSION,
        denominator_definition=FREEHIRE_DENOMINATOR,
        coverage_caveat=FREEHIRE_CAVEAT,
        sample_floor=0,
    )


class FreehireToolRuntime(ToolRuntime):
    """The SQL runtime plus one external tool.

    ``run_agent`` only needs ``tool_specs`` and ``call``, so extending the existing
    runtime keeps the agent signature unchanged: SQL tools still run inside the
    read-only, timed session, and only ``freehire_*`` leaves the process.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        taxonomy: Taxonomy,
        *,
        client: FreehireClient,
        statement_timeout_ms: int = 5000,
        max_rows: int = 200,
    ) -> None:
        super().__init__(
            session_factory,
            taxonomy,
            statement_timeout_ms=statement_timeout_ms,
            max_rows=max_rows,
        )
        self._freehire = client

    @property
    def tool_specs(self) -> tuple[dict[str, Any], ...]:
        """SQL tool specs followed by the external one."""
        return (*super().tool_specs, FREEHIRE_TOOL_SPEC)

    def call(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Route freehire calls to the network and everything else to SQL."""
        if tool == FREEHIRE_TOOL_NAME:
            validated = FreehireSearchArgs.model_validate(args)
            page = self._freehire.search(
                q=validated.q,
                skills=validated.skills,
                countries=validated.countries,
                regions=validated.regions,
                remote=validated.remote,
                limit=validated.limit,
            )
            return search_result(validated, page)
        return super().call(tool, args)
