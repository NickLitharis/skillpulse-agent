"""initial ingestion tables

Revision ID: 0001
Revises:
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("month", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("descendant_count", sa.Integer(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_threads_month", "threads", ["month"])

    op.create_table(
        "posts",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "thread_id",
            sa.BigInteger(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text_raw", sa.Text(), nullable=False),
        sa.Column("text_clean", sa.Text(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_dead", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_posts_month", "posts", ["month"])
    op.create_index("ix_posts_thread_id", "posts", ["thread_id"])

    op.create_table(
        "fetch_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "thread_id",
            sa.BigInteger(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="running"),
        sa.Column("posts_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posts_upserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_item_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('running', 'ok', 'failed')", name="ck_fetch_runs_status"),
    )
    op.create_index("ix_fetch_runs_thread_id", "fetch_runs", ["thread_id"])

    op.create_table(
        "skills",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.UniqueConstraint("name", name="uq_skills_name"),
    )


def downgrade() -> None:
    op.drop_table("skills")
    op.drop_index("ix_fetch_runs_thread_id", table_name="fetch_runs")
    op.drop_table("fetch_runs")
    op.drop_index("ix_posts_thread_id", table_name="posts")
    op.drop_index("ix_posts_month", table_name="posts")
    op.drop_table("posts")
    op.drop_index("ix_threads_month", table_name="threads")
    op.drop_table("threads")
