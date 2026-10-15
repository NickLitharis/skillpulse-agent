"""M4 Postgres integration: read-only role migration and the nightly refresh task.

Runs against a real Postgres 16 with the real Alembic migrations, so migration 0003 (the
``skillpulse_readonly`` role), the ``READ ONLY`` transaction and the ``CONCURRENTLY``
refresh are exercised, not mocked. It uses an ephemeral container and never touches the
shared database.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

from skillpulse.models import Thread
from skillpulse.tools.readonly import readonly_session
from skillpulse.workers import tasks as worker_tasks

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[1]


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


def test_migration_0003_creates_readonly_role_with_select_only(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.scalar(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'skillpulse_readonly'")
        ) == 1
        can_select = connection.scalar(
            text(
                "SELECT has_table_privilege('skillpulse_readonly', 'posts', 'SELECT')"
            )
        )
        can_insert = connection.scalar(
            text(
                "SELECT has_table_privilege('skillpulse_readonly', 'posts', 'INSERT')"
            )
        )
        can_select_mv = connection.scalar(
            text(
                "SELECT has_table_privilege('skillpulse_readonly', 'skill_trend_mv', 'SELECT')"
            )
        )
    assert can_select is True
    assert can_insert is False
    assert can_select_mv is True


def test_readonly_transaction_rejects_writes_on_real_postgres(engine: Engine) -> None:
    from datetime import UTC, datetime

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            Thread(id=70001, title="t", month=datetime(2026, 9, 1, tzinfo=UTC),
                   created_at=datetime(2026, 9, 1, tzinfo=UTC))
        )
        session.commit()
    # The read-only transaction must reject a write even for the owner role.
    with readonly_session(factory) as session, pytest.raises(DBAPIError):
        session.execute(
            text(
                "INSERT INTO posts (id, thread_id, month, time, text_raw, text_clean, "
                "is_deleted, is_dead) VALUES (70002, 70001, now(), now(), '', '', "
                "false, false)"
            )
        )
        session.commit()


def test_refresh_trend_views_task_runs_concurrently(engine: Engine) -> None:
    """The beat task must refresh both views outside a transaction block."""
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM post_skills"))
        connection.execute(text("DELETE FROM post_extractions"))
        connection.execute(text("DELETE FROM posts"))
        connection.execute(text("DELETE FROM threads"))
        connection.execute(
            text("INSERT INTO threads (id, title, month, created_at) "
                 "VALUES (71001, 't', now(), now())")
        )
        connection.execute(
            text("INSERT INTO posts (id, thread_id, month, time, text_raw, text_clean, "
                 "is_deleted, is_dead) VALUES (71002, 71001, now(), now(), '', '', "
                 "false, false)")
        )
        connection.execute(
            text("INSERT INTO post_extractions (post_id, extractor, taxonomy_version, "
                 "countries, unknown_terms, is_fallback) "
                 "VALUES (71002, 'baseline', 'x', '[]', '[]', false)")
        )
        connection.execute(
            text("INSERT INTO skills (id, name, category, aliases) "
                 "VALUES ('python', 'Python', 'language', '[]')")
        )
        connection.execute(
            text("INSERT INTO post_skills (post_id, skill_id, required, extractor, "
                 "taxonomy_version) VALUES (71002, 'python', true, 'baseline', 'x')")
        )
    worker_tasks.run_refresh_trend_views(engine)
    with engine.connect() as connection:
        total = connection.scalar(
            text("SELECT total_posts FROM skill_trend_mv WHERE skill_id = 'python'")
        )
    assert total == 1
