"""Segmented trend denominators on the frozen synthetic fixture (offline, SQLite)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.eval.synthetic_fixture import build_synthetic_fixture_engine
from skillpulse.extraction.taxonomy import load_taxonomy
from skillpulse.trends import skill_trend, top_skills


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    engine = build_synthetic_fixture_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_segmented_trend_denominator_includes_posts_without_the_skill(
    session_factory: sessionmaker[Session],
) -> None:
    """Kubernetes appears in only 15 of 60 backend months; the denominator must still
    count every eligible backend post in the period, not just the months it appears."""
    taxonomy = load_taxonomy()
    with session_factory() as session:
        counts = {
            count.skill_id: count
            for count in top_skills(
                session, taxonomy_version=taxonomy.version, role_family="backend", top_n=50
            )
        }
        trend = skill_trend(
            session,
            "kubernetes",
            taxonomy_version=taxonomy.version,
            role_family="backend",
        )
        per_month_denominator = sum(point.total_posts for point in trend.points)

    assert counts["python"].total_posts == 224  # the whole backend segment
    assert trend.total_posts == 224
    assert trend.posts_with_skill == 56
    assert trend.share == 0.25
    # Per-month rows only exist where the skill appears; that sum is not the denominator.
    assert per_month_denominator == 56
    assert len(trend.points) == 15
