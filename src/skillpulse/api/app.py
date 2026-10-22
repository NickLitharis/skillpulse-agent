"""FastAPI service: read-only stats (M3) and the streaming agent (M4).

Numbers come from :mod:`skillpulse.trends` and :mod:`skillpulse.tools` only; the model
never writes SQL. ``/v1/ask`` streams the agent run over SSE with exactly the SPEC
events: ``plan``, ``tool_call``, ``tool_result``, ``token``, ``done`` (``trace_id``,
``partial``, ``grounded``) and ``error``. A client disconnect cancels the run at the next
node boundary, Postgres being down returns ``503`` before streaming starts, and internal
errors are logged but never returned to the client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import date
from typing import Any

import redis
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from skillpulse.agent.graph import run_agent
from skillpulse.agent.models import AgentModel, build_agent_model
from skillpulse.config import Settings, get_settings
from skillpulse.extraction.taxonomy import Taxonomy, load_taxonomy
from skillpulse.observability import (
    metrics_content,
    observe_agent,
    observe_extractions,
    observe_request,
)
from skillpulse.runtime import build_session_factory
from skillpulse.tools import ToolRuntime
from skillpulse.tracing import record_agent_trace
from skillpulse.trends import ELIGIBLE_POSTS_DEFINITION, coverage, skill_trend, top_skills

ALLOWED_EXTRACTORS = ("baseline", "llm")
logger = logging.getLogger(__name__)


class AskRequest(BaseModel):
    """The only accepted ``/v1/ask`` input: a bounded natural-language question."""

    question: str = Field(min_length=1, max_length=2000)


def _as_dict(count: object) -> dict[str, object]:
    return {
        "skill_id": count.skill_id,  # type: ignore[attr-defined]
        "posts_with_skill": count.posts_with_skill,  # type: ignore[attr-defined]
        "total_posts": count.total_posts,  # type: ignore[attr-defined]
        "share": count.share,  # type: ignore[attr-defined]
        "sample_sufficient": count.sample_sufficient,  # type: ignore[attr-defined]
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    taxonomy: Taxonomy | None = None,
    *,
    agent_model: AgentModel | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the app with an injected factory/taxonomy/model (used by the tests)."""
    settings = settings or get_settings()
    factory = session_factory or build_session_factory()
    tax = taxonomy or load_taxonomy()
    # The real agent model needs the shared Redis token budget, so wire the client here.
    # With the gate closed build_agent_model returns None before it touches Redis.
    model = (
        agent_model
        if agent_model is not None
        else build_agent_model(
            settings, redis_client=redis.Redis.from_url(settings.redis_url)
        )
    )
    runtime = ToolRuntime(
        factory,
        tax,
        statement_timeout_ms=settings.tool_statement_timeout_ms,
        max_rows=settings.tool_max_rows,
    )

    def get_session() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI(title="SkillPulse Agent", version="0.1.0")
    app.state.taxonomy = tax

    @app.middleware("http")
    async def count_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            observe_request(request.method, route, status)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        observe_extractions(factory)
        content, content_type = metrics_content()
        return Response(content=content, headers={"Content-Type": content_type})

    def _validate(extractor: str, skill_id: str | None = None) -> None:
        if extractor not in ALLOWED_EXTRACTORS:
            raise HTTPException(status_code=422, detail=f"unknown extractor {extractor!r}")
        if skill_id is not None and tax.get(skill_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown skill id {skill_id!r}")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(session: Session = Depends(get_session)) -> dict[str, str]:
        session.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.get("/v1/skills/top")
    def skills_top(
        extractor: str = "baseline",
        top_n: int = Query(default=10, ge=1, le=100),
        months: int | None = Query(default=None, ge=1, le=240),
        role_family: str | None = None,
        remote: bool | None = None,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _validate(extractor)
        if role_family is not None and role_family not in tax.role_family_ids:
            raise HTTPException(status_code=422, detail=f"unknown role_family {role_family!r}")
        counts = top_skills(
            session,
            taxonomy_version=tax.version,
            extractor=extractor,
            top_n=top_n,
            months=months,
            role_family=role_family,
            remote=remote,
        )
        return {
            "taxonomy_version": tax.version,
            "extractor": extractor,
            "total_posts_definition": ELIGIBLE_POSTS_DEFINITION,
            "skills": [_as_dict(count) for count in counts],
        }

    @app.get("/v1/skills/{skill_id}/trend")
    def skills_trend(
        skill_id: str,
        extractor: str = "baseline",
        months: int | None = Query(default=None, ge=1, le=240),
        month_from: date | None = None,
        month_to: date | None = None,
        session: Session = Depends(get_session),
    ) -> dict[str, object]:
        _validate(extractor, skill_id)
        trend = skill_trend(
            session,
            skill_id,
            taxonomy_version=tax.version,
            extractor=extractor,
            months=months,
            month_from=month_from,
            month_to=month_to,
        )
        definition = tax.get(skill_id)
        return {
            "skill_id": trend.skill_id,
            "name": definition.name if definition else skill_id,
            "total_posts_definition": ELIGIBLE_POSTS_DEFINITION,
            "posts_with_skill": trend.posts_with_skill,
            "total_posts": trend.total_posts,
            "share": trend.share,
            "sample_sufficient": trend.sample_sufficient,
            "points": [
                {
                    "month": point.month.isoformat(),
                    "posts_with_skill": point.posts_with_skill,
                    "total_posts": point.total_posts,
                    "share": point.share,
                    "sample_sufficient": point.sample_sufficient,
                }
                for point in trend.points
            ],
        }

    @app.get("/v1/coverage")
    def coverage_endpoint(session: Session = Depends(get_session)) -> dict[str, object]:
        report = coverage(session)
        return {
            "total_posts": report.total_posts,
            "last_successful_run": report.last_successful_run,
            "extraction_versions": report.extraction_versions,
            "attribution": report.attribution,
            "months": [
                {"month": month.month.isoformat(), "posts": month.posts} for month in report.months
            ],
        }

    @app.post("/v1/ask")
    async def ask(request: Request, payload: AskRequest) -> StreamingResponse:
        if model is None:
            raise HTTPException(
                status_code=503,
                detail="agent model is not configured (SKILLPULSE_AGENT_LLM_ENABLED is false)",
            )
        try:
            await run_in_threadpool(_ping, factory)
        except (OperationalError, DBAPIError) as exc:
            logger.warning("database not ready for /v1/ask: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail="database unavailable") from None

        cancel_event = threading.Event()

        async def watch_disconnect() -> None:
            try:
                while not cancel_event.is_set():
                    if await request.is_disconnected():
                        cancel_event.set()
                        return
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:  # pragma: no cover - shutdown race
                return

        async def event_stream() -> AsyncIterator[str]:
            watcher = asyncio.create_task(watch_disconnect())
            run = None
            try:
                run = await run_in_threadpool(
                    _run_agent_sync, payload.question, cancel_event, runtime, model, tax, settings
                )
            except Exception:
                # Headers already say 200, so a provider failure must end the stream with
                # an explicit error/done pair instead of truncating it.
                logger.exception("agent run failed after the SSE response started")
            finally:
                watcher.cancel()
            if run is None:
                yield _sse(
                    "error",
                    {"code": "agent_error", "message": "the agent could not complete the request"},
                )
                yield _sse("done", {"trace_id": None, "partial": True, "grounded": False})
                return
            for event in run.events:
                if event["event"] == "done":
                    continue
                yield _sse(event["event"], event["data"])
            if run.error and run.error != "cancelled":
                yield _sse(
                    "error",
                    {"code": run.error, "message": "the agent could not complete the request"},
                )
            for chunk in _token_chunks(run.answer):
                yield _sse("token", {"text": chunk})
            yield _sse(
                "done",
                {
                    "trace_id": run.trace_id,
                    "partial": run.partial,
                    "grounded": run.grounded,
                },
            )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


def _ping(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        session.execute(text("SELECT 1"))


def _run_agent_sync(
    question: str,
    cancel_event: threading.Event,
    runtime: ToolRuntime,
    model: AgentModel,
    tax: Taxonomy,
    settings: Settings,
) -> Any:
    started = time.perf_counter()
    started_ns = time.time_ns()
    status = "error"
    try:
        run = run_agent(
            question,
            runtime=runtime,
            model=model,
            taxonomy=tax,
            max_tool_calls=settings.agent_max_tool_calls,
            overall_timeout_seconds=settings.agent_overall_timeout_seconds,
            cancel_check=cancel_event.is_set,
        )
        if run.error == "cancelled":
            status = "cancelled"
        elif run.error:
            status = "error"
        else:
            status = "partial" if run.partial else "ok"
        record_agent_trace(run, started_ns, settings=settings)
        return run
    finally:
        observe_agent(status, time.perf_counter() - started)


def _token_chunks(answer: str, size: int = 6) -> list[str]:
    words = answer.split(" ")
    return [" ".join(words[index : index + size]) for index in range(0, len(words), size)]


app = create_app()
