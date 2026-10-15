"""extraction results and trend views (M3)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TREND_VIEW_SQL = """
CREATE MATERIALIZED VIEW skill_trend_mv AS
WITH denom AS (
    SELECT e.taxonomy_version, e.extractor,
           date_trunc('month', p.month)::date AS month,
           count(DISTINCT p.id) AS total_posts
    FROM post_extractions e
    JOIN posts p ON p.id = e.post_id
    WHERE p.is_deleted = false AND p.is_dead = false
    GROUP BY 1, 2, 3
),
num AS (
    SELECT s.taxonomy_version, s.extractor,
           date_trunc('month', p.month)::date AS month,
           s.skill_id,
           count(DISTINCT p.id) AS posts_with_skill
    FROM post_skills s
    JOIN posts p ON p.id = s.post_id
    WHERE p.is_deleted = false AND p.is_dead = false
    GROUP BY 1, 2, 3, 4
)
SELECT n.taxonomy_version, n.extractor, n.month, n.skill_id,
       n.posts_with_skill, d.total_posts
FROM num n
JOIN denom d USING (taxonomy_version, extractor, month)
"""

SEGMENT_VIEW_SQL = """
CREATE MATERIALIZED VIEW skill_segment_mv AS
WITH denom AS (
    SELECT e.taxonomy_version, e.extractor,
           date_trunc('month', p.month)::date AS month,
           coalesce(e.role_family, '') AS role_family,
           CASE WHEN e.remote IS NULL THEN 'unknown'
                WHEN e.remote THEN 'true' ELSE 'false' END AS remote_state,
           count(DISTINCT p.id) AS total_posts
    FROM post_extractions e
    JOIN posts p ON p.id = e.post_id
    WHERE p.is_deleted = false AND p.is_dead = false
    GROUP BY 1, 2, 3, 4, 5
),
num AS (
    SELECT s.taxonomy_version, s.extractor,
           date_trunc('month', p.month)::date AS month,
           coalesce(e.role_family, '') AS role_family,
           CASE WHEN e.remote IS NULL THEN 'unknown'
                WHEN e.remote THEN 'true' ELSE 'false' END AS remote_state,
           s.skill_id,
           count(DISTINCT p.id) AS posts_with_skill
    FROM post_skills s
    JOIN posts p ON p.id = s.post_id
    JOIN post_extractions e
      ON e.post_id = p.id
     AND e.extractor = s.extractor
     AND e.taxonomy_version = s.taxonomy_version
    WHERE p.is_deleted = false AND p.is_dead = false
    GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT n.taxonomy_version, n.extractor, n.month, n.role_family, n.remote_state,
       n.skill_id, n.posts_with_skill, d.total_posts
FROM num n
JOIN denom d USING (taxonomy_version, extractor, month, role_family, remote_state)
"""


def upgrade() -> None:
    op.create_table(
        "post_extractions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "post_id",
            sa.BigInteger(),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("extractor", sa.String(length=16), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=32), nullable=False),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("role_family", sa.String(length=64), nullable=True),
        sa.Column("seniority", sa.String(length=32), nullable=True),
        sa.Column("remote", sa.Boolean(), nullable=True),
        sa.Column("countries", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("unknown_terms", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "post_id", "extractor", "taxonomy_version", name="uq_post_extractions_identity"
        ),
        sa.CheckConstraint(
            "extractor IN ('baseline', 'llm') AND taxonomy_version <> ''",
            name="ck_post_extractions_identity",
        ),
    )
    op.create_index("ix_post_extractions_post_id", "post_extractions", ["post_id"])
    op.create_index(
        "ix_post_extractions_query", "post_extractions", ["taxonomy_version", "extractor"]
    )

    op.create_table(
        "post_skills",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "post_id",
            sa.BigInteger(),
            sa.ForeignKey("posts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("skill_id", sa.String(length=64), sa.ForeignKey("skills.id"), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("extractor", sa.String(length=16), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "post_id",
            "skill_id",
            "extractor",
            "taxonomy_version",
            name="uq_post_skills_identity",
        ),
    )
    op.create_index("ix_post_skills_post_id", "post_skills", ["post_id"])
    op.create_index("ix_post_skills_skill_id", "post_skills", ["skill_id"])

    op.execute(TREND_VIEW_SQL)
    op.execute(
        "CREATE UNIQUE INDEX uq_skill_trend_mv "
        "ON skill_trend_mv (taxonomy_version, extractor, month, skill_id)"
    )
    op.execute(SEGMENT_VIEW_SQL)
    op.execute(
        "CREATE UNIQUE INDEX uq_skill_segment_mv "
        "ON skill_segment_mv (taxonomy_version, extractor, month, role_family, "
        "remote_state, skill_id)"
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS skill_segment_mv")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS skill_trend_mv")
    op.drop_index("ix_post_skills_skill_id", table_name="post_skills")
    op.drop_index("ix_post_skills_post_id", table_name="post_skills")
    op.drop_table("post_skills")
    op.drop_index("ix_post_extractions_query", table_name="post_extractions")
    op.drop_index("ix_post_extractions_post_id", table_name="post_extractions")
    op.drop_table("post_extractions")
