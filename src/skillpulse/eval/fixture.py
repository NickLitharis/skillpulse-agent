"""Frozen fixture database for the agent evaluation (SPEC section 11).

The agent evaluation must not depend on Hacker News, a live LLM or the wall clock, so it
runs on a deterministic database built here. The fixture holds **no post text**: each
post stores an empty text and only the structured fields (month, company, role family,
remote state, skill ids), exactly the shape the public agent set is allowed to expose.

The two materialised views the tools read are created as plain tables on SQLite, so the
same tool code path (``skill_trend_mv``/``skill_segment_mv``) is exercised without Docker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from skillpulse.extraction.repository import sync_taxonomy
from skillpulse.extraction.taxonomy import Taxonomy, load_taxonomy
from skillpulse.models import (
    Base,
    Post,
    PostExtractionRow,
    PostSkillRow,
    Thread,
)

FIXTURE_START = (2021, 10)
FIXTURE_MONTHS = 60
POSTS_PER_MONTH = 8
FIXTURE_COMPANY_PREFIX = "Fixture Co"
ROLE_CYCLE = ("backend", "frontend", "data", "infra", "ml", "mobile", "security", "fullstack")
SKILLS_BY_ROLE: dict[str, tuple[str, ...]] = {
    "backend": ("python", "postgres", "docker"),
    "frontend": ("react", "typescript", "docker"),
    "data": ("python", "postgres", "aws"),
    "infra": ("kubernetes", "terraform", "aws"),
    "ml": ("python", "kubernetes"),
    "mobile": ("typescript", "react"),
    "security": ("python", "docker"),
    "fullstack": ("python", "react", "docker"),
}


def _month(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def _months() -> list[datetime]:
    out: list[datetime] = []
    year, month = FIXTURE_START
    for _ in range(FIXTURE_MONTHS):
        out.append(_month(year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def _skills_for(index: int, role: str) -> list[str]:
    skills = list(SKILLS_BY_ROLE[role])
    if index % 4 == 0 and "kubernetes" not in skills:
        skills.append("kubernetes")
    if index % 5 == 0 and "aws" not in skills:
        skills.append("aws")
    return sorted(set(skills))


def build_fixture_engine(path: str | Path | None = None) -> Engine:
    """Build and return the frozen fixture database (in memory unless ``path`` is given)."""
    if path:
        url = f"sqlite+pysqlite:///{Path(path).as_posix()}"
        engine = create_engine(url, future=True)
    else:
        # StaticPool keeps one shared in-memory database across sessions and threads, so
        # the fixture works under the SSE endpoint's thread-pool execution too.
        engine = create_engine(
            "sqlite+pysqlite://",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
            future=True,
        )
    Base.metadata.create_all(engine)
    taxonomy = load_taxonomy()
    _seed(engine, taxonomy)
    _build_views(engine)
    return engine


def _seed(engine: Engine, taxonomy: Taxonomy) -> None:
    for skill_id in {skill for role in SKILLS_BY_ROLE.values() for skill in role} | {
        "kubernetes",
        "aws",
    }:
        if taxonomy.get(skill_id) is None:
            raise ValueError(f"fixture skill {skill_id!r} is not in the taxonomy")
    with Session(engine) as session:
        sync_taxonomy(session, taxonomy)
        months = _months()
        for month_index, month in enumerate(months):
            thread_id = 800_000 + month_index
            session.add(
                Thread(
                    id=thread_id,
                    title=f"Ask HN: Who is hiring? (fixture {month.date()})",
                    month=month,
                    created_at=month,
                )
            )
            session.flush()
            for index in range(POSTS_PER_MONTH):
                post_id = month_index * 100 + index
                role = ROLE_CYCLE[(month_index + index) % len(ROLE_CYCLE)]
                remote: bool | None = [True, False, None][index % 3]
                skills = _skills_for(index, role)
                session.add(
                    Post(
                        id=post_id,
                        thread_id=thread_id,
                        month=month,
                        time=month,
                        text_raw="",  # the fixture holds no post text
                        text_clean="",
                    )
                )
                session.add(
                    PostExtractionRow(
                        post_id=post_id,
                        extractor="baseline",
                        taxonomy_version=taxonomy.version,
                        company=f"{FIXTURE_COMPANY_PREFIX} {post_id}",
                        role_family=role,
                        remote=remote,
                        countries="[]",
                        unknown_terms="[]",
                        is_fallback=False,
                    )
                )
                for skill_id in skills:
                    session.add(
                        PostSkillRow(
                            post_id=post_id,
                            skill_id=skill_id,
                            required=True,
                            extractor="baseline",
                            taxonomy_version=taxonomy.version,
                        )
                    )
        session.commit()


_VIEW_SQL = {
    "skill_trend_mv": """
        CREATE TABLE skill_trend_mv AS
        SELECT n.taxonomy_version, n.extractor, n.month, n.skill_id,
               n.posts_with_skill, d.total_posts
        FROM (
            SELECT s.taxonomy_version, s.extractor, p.month AS month, s.skill_id,
                   count(DISTINCT p.id) AS posts_with_skill
            FROM post_skills s JOIN posts p ON p.id = s.post_id
            WHERE NOT p.is_deleted AND NOT p.is_dead
            GROUP BY 1, 2, 3, 4
        ) n
        JOIN (
            SELECT e.taxonomy_version, e.extractor, p.month AS month,
                   count(DISTINCT p.id) AS total_posts
            FROM post_extractions e JOIN posts p ON p.id = e.post_id
            WHERE NOT p.is_deleted AND NOT p.is_dead
            GROUP BY 1, 2, 3
        ) d
        ON n.taxonomy_version = d.taxonomy_version
       AND n.extractor = d.extractor AND n.month = d.month
    """,
    "skill_segment_mv": """
        CREATE TABLE skill_segment_mv AS
        SELECT n.taxonomy_version, n.extractor, n.month, n.role_family, n.remote_state,
               n.skill_id, n.posts_with_skill, d.total_posts
        FROM (
            SELECT s.taxonomy_version, s.extractor, p.month AS month,
                   coalesce(e.role_family, '') AS role_family,
                   CASE WHEN e.remote IS NULL THEN 'unknown'
                        WHEN e.remote THEN 'true' ELSE 'false' END AS remote_state,
                   s.skill_id, count(DISTINCT p.id) AS posts_with_skill
            FROM post_skills s
            JOIN posts p ON p.id = s.post_id
            JOIN post_extractions e ON e.post_id = p.id
             AND e.extractor = s.extractor
             AND e.taxonomy_version = s.taxonomy_version
            WHERE NOT p.is_deleted AND NOT p.is_dead
            GROUP BY 1, 2, 3, 4, 5, 6
        ) n
        JOIN (
            SELECT e.taxonomy_version, e.extractor, p.month AS month,
                   coalesce(e.role_family, '') AS role_family,
                   CASE WHEN e.remote IS NULL THEN 'unknown'
                        WHEN e.remote THEN 'true' ELSE 'false' END AS remote_state,
                   count(DISTINCT p.id) AS total_posts
            FROM post_extractions e JOIN posts p ON p.id = e.post_id
            WHERE NOT p.is_deleted AND NOT p.is_dead
            GROUP BY 1, 2, 3, 4, 5
        ) d
        ON n.taxonomy_version = d.taxonomy_version AND n.extractor = d.extractor
       AND n.month = d.month AND n.role_family = d.role_family
       AND n.remote_state = d.remote_state
    """,
}


def _build_views(engine: Engine) -> None:
    with engine.begin() as connection:
        for name, sql in _VIEW_SQL.items():
            connection.execute(text(f"DROP TABLE IF EXISTS {name}"))
            connection.execute(text(sql))
