"""Trend views and statistics (SPEC section 8, "Trends").

The numbers come from the materialised views ``skill_trend_mv`` and
``skill_segment_mv``, which carry the numerator (posts mentioning a skill) and the
denominator (posts with an extraction for that month) side by side. Shares are the main
measure, but a share is never quoted on fewer than :data:`MIN_SAMPLE_POSTS` posts: those
rows still return the exact counts, with ``share=None`` and ``sample_sufficient=False``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

MIN_SAMPLE_POSTS = 30
ATTRIBUTION = "Hacker News, https://news.ycombinator.com/item?id=<id>"
# ``total_posts`` is the denominator and means *eligible* posts: posts that have an
# extraction for the requested extractor and taxonomy version and are neither deleted
# nor dead. It is NOT total stored posts or full source coverage; ``/v1/coverage``
# reports the stored-post total separately.
ELIGIBLE_POSTS_DEFINITION = (
    "posts with an extraction for the requested extractor and taxonomy version "
    "(eligible), not all stored posts"
)


@dataclass(frozen=True)
class SkillCount:
    """One skill's count over a period, with its eligible-post denominator."""

    skill_id: str
    posts_with_skill: int
    total_posts: int
    share: float | None
    sample_sufficient: bool


@dataclass(frozen=True)
class TrendPoint:
    """One month of a skill's trend."""

    month: date
    posts_with_skill: int
    total_posts: int
    share: float | None
    sample_sufficient: bool


@dataclass(frozen=True)
class SkillTrend:
    """A skill's monthly series plus eligible-post totals for the period."""

    skill_id: str
    points: list[TrendPoint] = field(default_factory=list)
    posts_with_skill: int = 0
    total_posts: int = 0
    share: float | None = None
    sample_sufficient: bool = False


@dataclass(frozen=True)
class MonthCoverage:
    """Posts stored for one thread month."""

    month: date
    posts: int


@dataclass(frozen=True)
class CoverageReport:
    """Months covered, posts per month and the last successful ingestion run."""

    months: list[MonthCoverage]
    total_posts: int
    last_successful_run: str | None
    extraction_versions: list[str]
    attribution: str = ATTRIBUTION


def _share(posts_with_skill: int, total_posts: int) -> tuple[float | None, bool]:
    sufficient = total_posts >= MIN_SAMPLE_POSTS
    if not sufficient or total_posts == 0:
        return None, sufficient
    return round(posts_with_skill / total_posts, 4), True


def months_back_cutoff(today: date, months: int) -> date:
    """Return the first day of the oldest month in a ``months``-long window.

    Computed in Python rather than with Postgres' ``make_interval`` so the same query
    runs on the frozen SQLite fixture database and on Postgres, and so "last 12 months"
    can be tested with an injected ``today`` instead of the wall clock.
    """
    index = (today.year * 12 + today.month - 1) - (months - 1)
    year, month0 = divmod(index, 12)
    return date(year, month0 + 1, 1)


def _segment_filters(
    *,
    months_back: int | None,
    month_from: date | None,
    month_to: date | None,
    role_family: str | None,
    remote: bool | None,
    conditions: list[str],
    params: dict[str, Any],
    today: date,
) -> None:
    if month_from is not None:
        conditions.append("month >= :month_from")
        params["month_from"] = month_from
    if month_to is not None:
        conditions.append("month <= :month_to")
        params["month_to"] = month_to
    if months_back is not None:
        conditions.append("month >= :months_cutoff")
        params["months_cutoff"] = months_back_cutoff(today, months_back)
    if role_family is not None:
        conditions.append("role_family = :role_family")
        params["role_family"] = role_family
    if remote is not None:
        # 3-state: 'true'/'false'/'unknown'. Filtering false must not include posts whose
        # remote status was never extracted (unknown).
        conditions.append("remote_state = :remote_state")
        params["remote_state"] = "true" if remote else "false"


def _period_denominator(
    session: Session,
    *,
    view: str,
    segmented: bool,
    conditions: list[str],
    params: dict[str, Any],
) -> int:
    """Sum the distinct (month, segment) eligible-post counts for the period.

    Summing one skill's per-month denominators would drop months where that skill is
    absent and inflate its share; the period denominator must be every eligible post in
    the window, including months with zero mentions of the skill.
    """
    keys = "month, role_family, remote_state, total_posts" if segmented else "month, total_posts"
    where = " AND ".join(conditions)
    sql = text(
        f"""
        SELECT coalesce(sum(total_posts), 0) AS total_posts
        FROM (SELECT DISTINCT {keys} FROM {view} WHERE {where}) AS segments
        """
    )
    return int(session.scalar(sql, params) or 0)


def top_skills(
    session: Session,
    *,
    taxonomy_version: str,
    extractor: str = "baseline",
    top_n: int = 10,
    months: int | None = None,
    month_from: date | None = None,
    month_to: date | None = None,
    role_family: str | None = None,
    remote: bool | None = None,
    today: date | None = None,
) -> list[SkillCount]:
    """Return the most-mentioned skills for the filter, with exact counts.

    ``months`` keeps the window relative to today (e.g. ``12`` for the last year), which
    is the shape the agent's questions use.
    """
    segmented = role_family is not None or remote is not None
    view = "skill_segment_mv" if segmented else "skill_trend_mv"
    conditions = ["taxonomy_version = :taxonomy_version", "extractor = :extractor"]
    params: dict[str, Any] = {
        "taxonomy_version": taxonomy_version,
        "extractor": extractor,
        "top_n": top_n,
    }
    _segment_filters(
        months_back=months,
        month_from=month_from,
        month_to=month_to,
        role_family=role_family,
        remote=remote,
        conditions=conditions,
        params=params,
        today=today or date.today(),
    )
    where = " AND ".join(conditions)
    numerator_sql = text(
        f"""
        SELECT skill_id, sum(posts_with_skill) AS posts_with_skill
        FROM {view}
        WHERE {where}
        GROUP BY skill_id
        ORDER BY posts_with_skill DESC, skill_id
        LIMIT :top_n
        """
    )
    rows = session.execute(numerator_sql, params).all()
    denominator = _period_denominator(
        session, view=view, segmented=segmented, conditions=conditions, params=params
    )
    counts: list[SkillCount] = []
    for row in rows:
        numerator = int(row.posts_with_skill)
        share, sufficient = _share(numerator, denominator)
        counts.append(
            SkillCount(
                skill_id=row.skill_id,
                posts_with_skill=numerator,
                total_posts=denominator,
                share=share,
                sample_sufficient=sufficient,
            )
        )
    return counts


def skill_trend(
    session: Session,
    skill_id: str,
    *,
    taxonomy_version: str,
    extractor: str = "baseline",
    months: int | None = None,
    month_from: date | None = None,
    month_to: date | None = None,
    role_family: str | None = None,
    remote: bool | None = None,
    today: date | None = None,
) -> SkillTrend:
    """Return the monthly numerator/denominator series for one skill."""
    segmented = role_family is not None or remote is not None
    view = "skill_segment_mv" if segmented else "skill_trend_mv"
    conditions = ["taxonomy_version = :taxonomy_version", "extractor = :extractor"]
    params: dict[str, Any] = {
        "taxonomy_version": taxonomy_version,
        "extractor": extractor,
        "skill_id": skill_id,
    }
    _segment_filters(
        months_back=months,
        month_from=month_from,
        month_to=month_to,
        role_family=role_family,
        remote=remote,
        conditions=conditions,
        params=params,
        today=today or date.today(),
    )
    skill_where = " AND ".join([*conditions, "skill_id = :skill_id"])
    if segmented:
        sql = text(
            f"""
            SELECT month, sum(posts_with_skill) AS posts_with_skill, sum(total_posts) AS total_posts
            FROM {view}
            WHERE {skill_where}
            GROUP BY month
            ORDER BY month
            """
        )
    else:
        sql = text(
            f"""
            SELECT month, posts_with_skill, total_posts
            FROM {view}
            WHERE {skill_where}
            ORDER BY month
            """
        )
    rows = session.execute(sql, params).all()
    points: list[TrendPoint] = []
    numerator = 0
    for row in rows:
        point_numerator = int(row.posts_with_skill)
        point_denominator = int(row.total_posts)
        numerator += point_numerator
        share, sufficient = _share(point_numerator, point_denominator)
        points.append(
            TrendPoint(
                month=row.month,
                posts_with_skill=point_numerator,
                total_posts=point_denominator,
                share=share,
                sample_sufficient=sufficient,
            )
        )
    # Period denominator: every eligible post in the window, including months where this
    # skill has no mention; summing the per-month rows would silently drop them.
    period_denominator = _period_denominator(
        session, view=view, segmented=segmented, conditions=conditions, params=params
    )
    share, sufficient = _share(numerator, period_denominator)
    return SkillTrend(
        skill_id=skill_id,
        points=points,
        posts_with_skill=numerator,
        total_posts=period_denominator,
        share=share,
        sample_sufficient=sufficient,
    )


def coverage(session: Session) -> CoverageReport:
    """Return months covered, posts per month and the last successful run."""
    month_rows = session.execute(
        text(
            """
            SELECT date_trunc('month', month)::date AS month, count(*) AS posts
            FROM posts
            WHERE is_deleted = false AND is_dead = false
            GROUP BY 1
            ORDER BY 1
            """
        )
    ).all()
    months = [MonthCoverage(month=row.month, posts=int(row.posts)) for row in month_rows]

    last_ok = session.scalar(
        text("SELECT max(finished_at) FROM fetch_runs WHERE status = 'ok'")
    )
    version_rows = session.execute(
        text(
            """
            SELECT DISTINCT taxonomy_version || '/' || extractor AS version
            FROM post_extractions
            ORDER BY 1
            """
        )
    ).all()
    return CoverageReport(
        months=months,
        total_posts=sum(month.posts for month in months),
        last_successful_run=last_ok.isoformat() if last_ok is not None else None,
        extraction_versions=[row.version for row in version_rows],
    )


def refresh_trend_views(engine: Engine, *, concurrently: bool = True) -> None:
    """Refresh both trend views (SPEC section 8, nightly beat task).

    ``CONCURRENTLY`` keeps reads working during the refresh and needs the unique indexes
    created in migration 0002. It cannot run inside a transaction block, so this takes
    an :class:`~sqlalchemy.engine.Engine` and uses an AUTOCOMMIT connection.
    """
    mode = "CONCURRENTLY" if concurrently else ""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f"REFRESH MATERIALIZED VIEW {mode} skill_trend_mv"))
        connection.execute(text(f"REFRESH MATERIALIZED VIEW {mode} skill_segment_mv"))
