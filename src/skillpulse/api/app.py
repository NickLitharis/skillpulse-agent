"""FastAPI stats service (SPEC section 10, M3 endpoints).

Only the read-only trend and coverage endpoints live here. ``/v1/ask`` (the agent) is
M4 and the MCP server is M5, so this module deliberately has no LLM dependency: every
number comes from :mod:`skillpulse.trends`, i.e. from SQL over the trend views.

The database session factory is injected through :func:`create_app`, which is what lets
the tests run the real endpoints against the test Postgres database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.extraction.taxonomy import Taxonomy, load_taxonomy
from skillpulse.runtime import build_session_factory
from skillpulse.trends import ELIGIBLE_POSTS_DEFINITION, coverage, skill_trend, top_skills

ALLOWED_EXTRACTORS = ("baseline", "llm")


def _as_dict(count: object) -> dict[str, object]:
    return {
        "skill_id": count.skill_id,  # type: ignore[attr-defined]
        "posts_with_skill": count.posts_with_skill,  # type: ignore[attr-defined]
        "total_posts": count.total_posts,  # type: ignore[attr-defined]
        "share": count.share,  # type: ignore[attr-defined]
        "sample_sufficient": count.sample_sufficient,  # type: ignore[attr-defined]
    }


def create_app(
    session_factory: sessionmaker[Session] | None = None,
    taxonomy: Taxonomy | None = None,
) -> FastAPI:
    """Build the app, optionally with an injected session factory and taxonomy."""
    factory = session_factory or build_session_factory()
    tax = taxonomy or load_taxonomy()

    def get_session() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI(title="SkillPulse Agent", version="0.1.0")
    app.state.taxonomy = tax

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
                {"month": month.month.isoformat(), "posts": month.posts}
                for month in report.months
            ],
        }

    return app


app = create_app()
