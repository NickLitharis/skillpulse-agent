"""Bounded metrics without user content; extraction volume comes from stored rows."""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.models import PostExtractionRow
from skillpulse.tools.readonly import readonly_session

REGISTRY = CollectorRegistry()
REQUEST_COUNT = Counter(
    "skillpulse_requests_total", "HTTP requests", ["method", "route", "status"], registry=REGISTRY
)
AGENT_COUNT = Counter("skillpulse_agent_runs_total", "Agent runs", ["status"], registry=REGISTRY)
AGENT_DURATION = Histogram(
    "skillpulse_agent_duration_seconds", "Agent duration", ["status"], registry=REGISTRY
)
_MCP_TOOLS = frozenset({"search_jobs", "skill_counts", "skill_trend", "compare_skills", "match_cv"})
MCP_TOOL_CALLS = Counter(
    "skillpulse_mcp_tool_calls_total", "MCP handler calls", ["tool", "outcome"], registry=REGISTRY
)
MCP_TOOL_DURATION = Histogram(
    "skillpulse_mcp_tool_duration_seconds",
    "MCP handler duration",
    ["tool", "outcome"],
    registry=REGISTRY,
)
EXTRACTION_RECORDS = Gauge(
    "skillpulse_extraction_records",
    "Stored extraction rows across taxonomy versions, not unique posts",
    ["extractor"],
    registry=REGISTRY,
)
EXTRACTION_SCRAPE_OK = Gauge(
    "skillpulse_extraction_scrape_ok",
    "Whether the last bounded database count succeeded",
    registry=REGISTRY,
)


def observe_mcp_tool(tool: str, outcome: str, duration: float) -> None:
    tool = tool if tool in _MCP_TOOLS else "other"
    outcome = outcome if outcome in {"ok", "error"} else "error"
    MCP_TOOL_CALLS.labels(tool=tool, outcome=outcome).inc()
    MCP_TOOL_DURATION.labels(tool=tool, outcome=outcome).observe(duration)


def observe_extractions(factory: sessionmaker[Session]) -> None:
    """Count committed rows, including work done in other processes; fail as unknown, not zero."""
    extractors = ("baseline", "llm")
    try:
        with readonly_session(factory, statement_timeout_ms=1000) as session:
            counts = dict(
                session.execute(
                    select(PostExtractionRow.extractor, func.count())
                    .where(PostExtractionRow.extractor.in_(extractors))
                    .group_by(PostExtractionRow.extractor)
                ).all()
            )
    except SQLAlchemyError:
        for extractor in extractors:
            EXTRACTION_RECORDS.labels(extractor=extractor).set(float("nan"))
        EXTRACTION_SCRAPE_OK.set(0)
        return
    for extractor in extractors:
        EXTRACTION_RECORDS.labels(extractor=extractor).set(counts.get(extractor, 0))
    EXTRACTION_SCRAPE_OK.set(1)


def observe_request(method: str, route: str, status: int) -> None:
    """Call with a route template, never a raw request path or query string."""
    method = (
        method
        if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        else "OTHER"
    )
    REQUEST_COUNT.labels(method=method, route=route, status=str(status)).inc()


def observe_agent(status: str, duration: float) -> None:
    status = status if status in {"ok", "error", "cancelled", "partial"} else "error"
    AGENT_COUNT.labels(status=status).inc()
    AGENT_DURATION.labels(status=status).observe(duration)


def metrics_content() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
