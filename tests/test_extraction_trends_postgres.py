"""M3 Postgres integration: extraction persistence, trend views and the stats API.

Runs against a real Postgres 16 with the real Alembic migrations (0001 + 0002), so the
materialised views, the unique indexes and the CONCURRENTLY refresh are exercised, not
mocked. All posts here are synthetic test rows inside the container database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from testcontainers.community.postgres import PostgresContainer

from skillpulse.api.app import create_app
from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.repository import get_extraction, sync_taxonomy
from skillpulse.extraction.service import extract_post
from skillpulse.extraction.taxonomy import load_taxonomy
from skillpulse.models import Post, PostSkillRow, Thread
from skillpulse.trends import MIN_SAMPLE_POSTS, refresh_trend_views, skill_trend, top_skills

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]
MONTH_A = datetime(2026, 10, 1, tzinfo=UTC)
MONTH_B = datetime(2026, 9, 1, tzinfo=UTC)
MONTH_C = datetime(2026, 8, 1, tzinfo=UTC)
POSTS_PER_MONTH = 30
PYTHON_IN_A = 15
POSTS_IN_B = 5
PYTHON_IN_B = 3
BACKEND_IN_A = 12  # 12 of the Python posts are labelled role_family=backend
REMOTE_FALSE_POSTS = 4  # month C: explicit "hybrid", so remote_state='false'
ELIGIBLE_TOTAL = POSTS_PER_MONTH + POSTS_IN_B + REMOTE_FALSE_POSTS


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with PostgresContainer("postgres:16-alpine") as container:
        url = container.get_connection_url()
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
        eng = create_engine(url, pool_pre_ping=True)
        try:
            yield eng
        finally:
            eng.dispose()


@pytest.fixture(scope="module")
def seeded(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """Seed synthetic posts and extractions, refresh the views, return the factory."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)
    with factory() as session:
        session.add(Thread(id=9001, title="A", month=MONTH_A, created_at=MONTH_A))
        session.add(Thread(id=9002, title="B", month=MONTH_B, created_at=MONTH_B))
        session.add(Thread(id=9003, title="C", month=MONTH_C, created_at=MONTH_C))
        session.flush()
        sync_taxonomy(session, taxonomy)

        def add(post_id: int, month: datetime, thread_id: int, text: str) -> Post:
            post = Post(
                id=post_id,
                thread_id=thread_id,
                month=month,
                time=month,
                text_raw=text,
                text_clean=text,
            )
            session.add(post)
            session.flush()
            return post

        for index in range(POSTS_PER_MONTH):
            text = "Python and Docker" if index < PYTHON_IN_A else "Docker and React"
            post = add(10001 + index, MONTH_A, 9001, text)
            extraction = baseline.extract(text)
            if index < BACKEND_IN_A:
                extraction = extraction.model_copy(update={"role_family": "backend"})
            extract_post(session, post, taxonomy=taxonomy, baseline=baseline)
            # Replace the baseline row with the role-labelled variant where needed.
            from skillpulse.extraction.repository import record_extraction

            record_extraction(session, post, extraction, taxonomy)
        for index in range(POSTS_IN_B):
            text = "Python and Docker" if index < PYTHON_IN_B else "Docker"
            post = add(20001 + index, MONTH_B, 9002, text)
            extract_post(session, post, taxonomy=taxonomy, baseline=baseline)
        # Explicit non-remote posts: "hybrid" makes remote=False, distinct from unknown.
        for index in range(REMOTE_FALSE_POSTS):
            post = add(30001 + index, MONTH_C, 9003, "Hybrid in Berlin, Docker required.")
            extract_post(session, post, taxonomy=taxonomy, baseline=baseline)
        session.commit()

    refresh_trend_views(engine, concurrently=True)
    yield factory


def test_migration_0002_adds_tables_and_materialised_views(engine: Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {"post_extractions", "post_skills"} <= tables
    with engine.connect() as connection:
        views = {
            row.matviewname
            for row in connection.execute(
                text("SELECT matviewname FROM pg_matviews")
            )
        }
    assert {"skill_trend_mv", "skill_segment_mv"} <= views


def test_extraction_is_stored_once(seeded: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy()
    with seeded() as session:
        stored = get_extraction(session, 10001, "baseline", taxonomy.version)
        assert stored is not None
        assert stored.skill_ids() == ["docker", "python"]
        assert stored.role_family == "backend"
        rows = session.scalars(
            select(PostSkillRow).where(PostSkillRow.post_id == 10001)
        ).all()
        assert len(rows) == 2  # no duplicate rows from the second write


def test_trend_share_has_exact_numerator_and_denominator(
    seeded: sessionmaker[Session],
) -> None:
    taxonomy = load_taxonomy()
    with seeded() as session:
        trend = skill_trend(session, "python", taxonomy_version=taxonomy.version)
        by_month = {point.month: point for point in trend.points}
        october = by_month[datetime(2026, 10, 1).date()]
        september = by_month[datetime(2026, 9, 1).date()]
        assert (october.posts_with_skill, october.total_posts) == (PYTHON_IN_A, POSTS_PER_MONTH)
        assert october.share == 0.5
        assert (september.posts_with_skill, september.total_posts) == (PYTHON_IN_B, POSTS_IN_B)
        assert trend.posts_with_skill == PYTHON_IN_A + PYTHON_IN_B
        assert trend.total_posts == ELIGIBLE_TOTAL


def test_share_is_withheld_below_the_sample_floor(seeded: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy()
    with seeded() as session:
        trend = skill_trend(session, "python", taxonomy_version=taxonomy.version)
        september = trend.points[0]
        assert september.month == datetime(2026, 9, 1).date()
        assert september.total_posts < MIN_SAMPLE_POSTS
        assert september.share is None
        assert september.sample_sufficient is False
        assert september.posts_with_skill == PYTHON_IN_B  # counts still exact


def test_top_skills_counts_and_sample_flag(seeded: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy()
    with seeded() as session:
        counts = {
            count.skill_id: count
            for count in top_skills(session, taxonomy_version=taxonomy.version)
        }
        assert counts["docker"].posts_with_skill == ELIGIBLE_TOTAL
        assert counts["docker"].total_posts == ELIGIBLE_TOTAL
        assert counts["docker"].share == 1.0
        assert counts["python"].share == round(
            (PYTHON_IN_A + PYTHON_IN_B) / ELIGIBLE_TOTAL, 4
        )
        assert counts["python"].sample_sufficient is True


def test_role_family_segment_uses_its_own_denominator(seeded: sessionmaker[Session]) -> None:
    taxonomy = load_taxonomy()
    with seeded() as session:
        counts = {
            count.skill_id: count
            for count in top_skills(
                session, taxonomy_version=taxonomy.version, role_family="backend"
            )
        }
        assert counts["python"].posts_with_skill == BACKEND_IN_A
        assert counts["python"].total_posts == BACKEND_IN_A
        assert counts["python"].sample_sufficient is False
        assert counts["python"].share is None


def test_remote_filter_excludes_unknown(seeded: sessionmaker[Session]) -> None:
    """remote=false must count only explicit non-remote posts, never unknown ones."""
    taxonomy = load_taxonomy()
    with seeded() as session:
        non_remote = {
            count.skill_id: count
            for count in top_skills(session, taxonomy_version=taxonomy.version, remote=False)
        }
        assert non_remote["docker"].posts_with_skill == REMOTE_FALSE_POSTS
        assert non_remote["docker"].total_posts == REMOTE_FALSE_POSTS  # unknown excluded
        # No post states it is remote, so the remote=true segment is empty, not unknown.
        assert top_skills(session, taxonomy_version=taxonomy.version, remote=True) == []
        # The unknown bucket holds everything else but is never reached by a bool filter.
        unknown = {
            count.skill_id: count
            for count in top_skills(session, taxonomy_version=taxonomy.version)
        }
        assert unknown["docker"].total_posts == ELIGIBLE_TOTAL


def test_stats_api_endpoints(seeded: sessionmaker[Session], engine: Engine) -> None:
    client = TestClient(create_app(seeded, load_taxonomy()))
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200

    top = client.get("/v1/skills/top", params={"top_n": 5}).json()
    assert top["taxonomy_version"] == load_taxonomy().version
    python = next(row for row in top["skills"] if row["skill_id"] == "python")
    assert python["posts_with_skill"] == PYTHON_IN_A + PYTHON_IN_B
    assert python["total_posts"] == ELIGIBLE_TOTAL
    assert "eligible" in top["total_posts_definition"]

    trend = client.get("/v1/skills/python/trend").json()
    assert trend["skill_id"] == "python"
    assert len(trend["points"]) == 2
    assert trend["points"][-1]["share"] == 0.5

    coverage = client.get("/v1/coverage").json()
    assert coverage["total_posts"] == ELIGIBLE_TOTAL
    assert {month["month"] for month in coverage["months"]} == {
        "2026-10-01",
        "2026-09-01",
        "2026-08-01",
    }
    assert "news.ycombinator.com" in coverage["attribution"]

    assert client.get("/v1/skills/not-a-skill/trend").status_code == 404
    assert client.get("/v1/skills/top", params={"extractor": "magic"}).status_code == 422
