"""read-only role for the M4 tools

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15

Creates the dedicated ``skillpulse_readonly`` Postgres role the agent/MCP tools connect
with (SPEC section 9). The role is ``NOLOGIN`` and has no password: how it authenticates
is an operator decision stored outside the repository, so no credential is embedded here.
It gets ``SELECT`` only, on the tables plus the two materialised views, and its session
defaults are read-only with a statement timeout so even a code bug cannot write.

The application still enforces ``SET TRANSACTION READ ONLY`` per tool session
(``skillpulse.tools.readonly``), so this role is defence in depth, not the only guard.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

ROLE = "skillpulse_readonly"


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                CREATE ROLE {ROLE} NOLOGIN;
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {ROLE}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {ROLE}")
    op.execute(
        f"GRANT SELECT ON skill_trend_mv, skill_segment_mv TO {ROLE}"
    )
    op.execute(f"ALTER ROLE {ROLE} SET default_transaction_read_only = on")
    op.execute(f"ALTER ROLE {ROLE} SET statement_timeout = '5s'")
    # Future tables created by the migration owner are readable without another grant.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {ROLE}"
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                DROP OWNED BY {ROLE};
                DROP ROLE {ROLE};
            END IF;
        END
        $$;
        """
    )
