"""The five read-only tools (SPEC section 9), shared by the agent and the MCP server.

Design rules enforced here, not by convention:

* **No model-written SQL.** Every statement is a fixed ``text()`` string with bound
  parameters; the only values that reach the database are validated by the Pydantic
  argument models. There is no ``raw_sql``/``query`` escape hatch.
* **No post text.** ``search_jobs`` selects ids, month, company, role, remote and skill
  ids only. No tool returns ``text_raw``/``text_clean``, so the unresolved HN content
  authorization cannot be bypassed with a ``get_examples``/``snippets`` tool. There is
  no such tool.
* **Provenance and honesty.** Every result carries the extractor, taxonomy version, the
  eligible-post denominator definition and the source-coverage caveat. ``remote`` is
  3-state and a share is ``None``/``sample_sufficient=false`` below 30 posts.
* **Read-only.** Callers pass a session from :func:`skillpulse.tools.readonly.readonly_session`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.tools.models import (
    CompareSkillsArgs,
    MatchCvArgs,
    SearchJobsArgs,
    SkillCountsArgs,
    SkillTrendArgs,
    ToolFilters,
    ToolResult,
    as_month,
    make_result_id,
)
from skillpulse.trends import (
    ELIGIBLE_POSTS_DEFINITION,
    MIN_SAMPLE_POSTS,
    months_back_cutoff,
    skill_trend,
    top_skills,
)

TOOL_NAMES = ("search_jobs", "skill_counts", "skill_trend", "compare_skills", "match_cv")

_SEARCH_BASE = """
SELECT p.id AS post_id, p.month AS month, e.company AS company,
       e.role_family AS role_family,
       CASE WHEN e.remote IS NULL THEN 'unknown'
            WHEN e.remote THEN 'true' ELSE 'false' END AS remote_state,
       e.is_fallback AS is_fallback, e.taxonomy_version AS taxonomy_version,
       e.extractor AS extractor
FROM posts p
JOIN post_extractions e ON e.post_id = p.id
WHERE p.is_deleted = false AND p.is_dead = false
  AND e.taxonomy_version = :taxonomy_version AND e.extractor = :extractor
"""


class ToolInputError(ValueError):
    """A tool was called with an unknown name, skill id or role family."""


def _filters(args: ToolFilters, conditions: list[str], params: dict[str, Any]) -> None:
    if args.role_family is not None:
        conditions.append("e.role_family = :role_family")
        params["role_family"] = args.role_family
    if args.remote is not None:
        conditions.append(
            "e.remote IS TRUE" if args.remote else "e.remote IS FALSE"
        )
    if args.month_from is not None:
        conditions.append("p.month >= :month_from")
        params["month_from"] = args.month_from
    if args.month_to is not None:
        conditions.append("p.month <= :month_to")
        params["month_to"] = args.month_to
    if args.months is not None:
        conditions.append("p.month >= :months_cutoff")
        params["months_cutoff"] = months_back_cutoff(date.today(), args.months)


def _validate(taxonomy: Taxonomy, args: ToolFilters, skill_ids: list[str] | None = None) -> None:
    if args.role_family is not None and args.role_family not in taxonomy.role_family_ids:
        raise ToolInputError(f"unknown role_family {args.role_family!r}")
    for skill_id in skill_ids or []:
        if taxonomy.get(skill_id) is None:
            raise ToolInputError(f"unknown skill id {skill_id!r}")


def _result(
    tool: str,
    args: dict[str, Any],
    rows: list[dict[str, Any]],
    values: dict[str, Any],
    taxonomy: Taxonomy,
    extractor: str,
    *,
    truncated: bool = False,
) -> ToolResult:
    return ToolResult(
        result_id=make_result_id(tool, args, rows, values),
        tool=tool,
        args=args,
        rows=rows,
        values=values,
        row_count=len(rows),
        truncated=truncated,
        extractor=extractor,
        taxonomy_version=taxonomy.version,
    )


def search_jobs(session: Session, taxonomy: Taxonomy, args: SearchJobsArgs) -> ToolResult:
    """Return up to 20 structured post rows matching the filters (no post text)."""
    _validate(taxonomy, args, [args.skill_id] if args.skill_id else [])
    conditions: list[str] = []
    params: dict[str, Any] = {
        "taxonomy_version": taxonomy.version,
        "extractor": args.extractor,
        "limit": args.limit,
    }
    _filters(args, conditions, params)
    if args.skill_id is not None:
        conditions.append(
            "EXISTS (SELECT 1 FROM post_skills ps WHERE ps.post_id = p.id "
            "AND ps.extractor = e.extractor AND ps.taxonomy_version = e.taxonomy_version "
            "AND ps.skill_id = :skill_id)"
        )
        params["skill_id"] = args.skill_id
    where = " AND ".join(conditions)
    query = _SEARCH_BASE if not where else f"{_SEARCH_BASE} AND {where}"
    sql = text(f"{query} ORDER BY p.month DESC, p.id DESC LIMIT :limit")
    raw = session.execute(sql, params).all()

    post_ids = [int(row.post_id) for row in raw]
    skills_by_post: dict[int, list[dict[str, Any]]] = {post_id: [] for post_id in post_ids}
    if post_ids:
        skill_rows = session.execute(
            text(
                "SELECT post_id, skill_id, required FROM post_skills "
                "WHERE post_id IN :post_ids AND extractor = :extractor "
                "AND taxonomy_version = :taxonomy_version ORDER BY skill_id"
            ).bindparams(bindparam("post_ids", expanding=True)),
            {"post_ids": post_ids, "extractor": args.extractor,
             "taxonomy_version": taxonomy.version},
        ).all()
        for skill_row in skill_rows:
            skills_by_post[int(skill_row.post_id)].append(
                {"skill_id": skill_row.skill_id, "required": bool(skill_row.required)}
            )
    rows = [
        {
            "post_id": int(row.post_id),
            "month": as_month(row.month),
            "company": row.company,
            "role_family": row.role_family,
            "remote_state": row.remote_state,
            "is_fallback": bool(row.is_fallback),
            "skills": skills_by_post[int(row.post_id)],
        }
        for row in raw
    ]
    values = {"returned": len(rows), "limit": args.limit, "contains_post_text": False}
    return _result(
        "search_jobs", args.model_dump(mode="json"), rows, values, taxonomy,
        args.extractor, truncated=len(rows) >= args.limit,
    )


def skill_counts(session: Session, taxonomy: Taxonomy, args: SkillCountsArgs) -> ToolResult:
    """Return the most requested skills for a filter, with the eligible denominator."""
    _validate(taxonomy, args)
    counts = top_skills(
        session,
        taxonomy_version=taxonomy.version,
        extractor=args.extractor,
        top_n=args.top_n,
        months=args.months,
        month_from=args.month_from,
        month_to=args.month_to,
        role_family=args.role_family,
        remote=args.remote,
    )
    rows = [
        {
            "skill_id": count.skill_id,
            "posts_with_skill": count.posts_with_skill,
            "total_posts": count.total_posts,
            "share": count.share,
            "sample_sufficient": count.sample_sufficient,
            "unit": "share of eligible posts",
        }
        for count in counts
    ]
    values = {
        "denominator_definition": ELIGIBLE_POSTS_DEFINITION,
        "sample_floor": MIN_SAMPLE_POSTS,
    }
    return _result("skill_counts", args.model_dump(mode="json"), rows, values, taxonomy,
                   args.extractor)


def skill_trend_tool(session: Session, taxonomy: Taxonomy, args: SkillTrendArgs) -> ToolResult:
    """Return one skill's monthly numerator/denominator series."""
    _validate(taxonomy, args, [args.skill_id])
    trend = skill_trend(
        session,
        args.skill_id,
        taxonomy_version=taxonomy.version,
        extractor=args.extractor,
        months=args.months,
        month_from=args.month_from,
        month_to=args.month_to,
        role_family=args.role_family,
        remote=args.remote,
    )
    definition = taxonomy.get(args.skill_id)
    rows = [
        {
            "month": as_month(point.month),
            "posts_with_skill": point.posts_with_skill,
            "total_posts": point.total_posts,
            "share": point.share,
            "sample_sufficient": point.sample_sufficient,
            "unit": "share of eligible posts",
        }
        for point in trend.points
    ]
    values = {
        "skill_id": args.skill_id,
        "name": definition.name if definition else args.skill_id,
        "posts_with_skill": trend.posts_with_skill,
        "total_posts": trend.total_posts,
        "share": trend.share,
        "sample_sufficient": trend.sample_sufficient,
        "denominator_definition": ELIGIBLE_POSTS_DEFINITION,
        "sample_floor": MIN_SAMPLE_POSTS,
    }
    return _result("skill_trend", args.model_dump(mode="json"), rows, values, taxonomy,
                   args.extractor)


def compare_skills(session: Session, taxonomy: Taxonomy, args: CompareSkillsArgs) -> ToolResult:
    """Return side-by-side trend aggregates for 1..5 skills."""
    _validate(taxonomy, args, args.skill_ids)
    rows: list[dict[str, Any]] = []
    for skill_id in args.skill_ids:
        single = SkillTrendArgs(**args.model_dump(exclude={"skill_ids"}), skill_id=skill_id)
        result = skill_trend_tool(session, taxonomy, single)
        rows.append(
            {
                "skill_id": skill_id,
                "posts_with_skill": result.values["posts_with_skill"],
                "total_posts": result.values["total_posts"],
                "share": result.values["share"],
                "sample_sufficient": result.values["sample_sufficient"],
                "points": result.rows,
            }
        )
    values = {"skills": args.skill_ids, "denominator_definition": ELIGIBLE_POSTS_DEFINITION}
    return _result("compare_skills", args.model_dump(mode="json"), rows, values, taxonomy,
                   args.extractor)


def match_cv(session: Session, taxonomy: Taxonomy, args: MatchCvArgs) -> ToolResult:
    """Compare CV skills to the most demanded skills; CV text is never stored or logged.

    CV skills come from the deterministic baseline, so this tool needs no LLM and never
    sends the CV anywhere (SPEC section 9, "CV privacy").
    """
    _validate(taxonomy, args)
    cv_skills = BaselineExtractor(taxonomy).extract(args.cv_text).skill_ids()
    counts = top_skills(
        session,
        taxonomy_version=taxonomy.version,
        extractor=args.extractor,
        top_n=args.top_n,
        months=args.months,
        month_from=args.month_from,
        month_to=args.month_to,
        role_family=args.role_family,
        remote=args.remote,
    )
    cv_set = set(cv_skills)
    rows = [
        {
            "skill_id": count.skill_id,
            "posts_with_skill": count.posts_with_skill,
            "total_posts": count.total_posts,
            "share": count.share,
            "sample_sufficient": count.sample_sufficient,
            "in_cv": count.skill_id in cv_set,
            "unit": "share of eligible posts",
        }
        for count in counts
    ]
    gaps = [count.skill_id for count in counts if count.skill_id not in cv_set]
    values = {
        "cv_skills": cv_skills,
        "gap_skills": gaps,
        "cv_text_stored": False,
        "denominator_definition": ELIGIBLE_POSTS_DEFINITION,
    }
    return _result("match_cv", args.model_dump(mode="json"), rows, values, taxonomy,
                   args.extractor)


_ARG_MODELS = {
    "search_jobs": SearchJobsArgs,
    "skill_counts": SkillCountsArgs,
    "skill_trend": SkillTrendArgs,
    "compare_skills": CompareSkillsArgs,
    "match_cv": MatchCvArgs,
}


def call_tool(
    session: Session, taxonomy: Taxonomy, tool: str, raw_args: dict[str, Any]
) -> ToolResult:
    """Validate ``raw_args`` with the tool's model and dispatch; never accepts SQL."""
    if tool not in _ARG_MODELS:
        raise ToolInputError(f"unknown tool {tool!r}")
    args = _ARG_MODELS[tool](**raw_args)
    if tool == "search_jobs":
        return search_jobs(session, taxonomy, args)
    if tool == "skill_counts":
        return skill_counts(session, taxonomy, args)
    if tool == "skill_trend":
        return skill_trend_tool(session, taxonomy, args)
    if tool == "compare_skills":
        return compare_skills(session, taxonomy, args)
    return match_cv(session, taxonomy, args)
