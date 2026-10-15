"""Read-only sessions for the tools (SPEC section 9).

Every tool query runs inside a session that is read-only at two levels:

* the transaction is ``READ ONLY`` (Postgres) or ``PRAGMA query_only`` (SQLite), so the
  database itself rejects a write even if the code below tries one;
* a ``before_flush`` listener raises :class:`ReadOnlyViolation` if an ORM write is
  attempted on the session.

The statement timeout and the row cap are applied here too, so a tool cannot run an
unbounded or slow query. The dedicated ``skillpulse_readonly`` Postgres role is created
by migration 0003; this module is what enforces read-only behaviour on every dialect,
including the SQLite fixture database used by the agent evaluation.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

READONLY_KEY = "skillpulse_readonly"
MAX_ROWS_KEY = "skillpulse_max_rows"


class ReadOnlyViolation(RuntimeError):
    """An ORM write was attempted inside a read-only tool session."""


@event.listens_for(Session, "before_flush")
def _reject_writes_on_readonly_session(
    session: Session, flush_context: object, instances: object
) -> None:
    if not session.info.get(READONLY_KEY):
        return
    if session.new or session.dirty or session.deleted:
        raise ReadOnlyViolation("a read-only tool session cannot flush ORM changes")


@contextmanager
def readonly_session(
    factory: sessionmaker[Session],
    *,
    statement_timeout_ms: int = 5000,
    max_rows: int = 200,
) -> Iterator[Session]:
    """Yield a session whose transaction cannot write and whose queries are bounded."""
    session = factory()
    session.info[READONLY_KEY] = True
    session.info[MAX_ROWS_KEY] = max_rows
    try:
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            # Both must run before any other statement opens the transaction.
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text(f"SET LOCAL statement_timeout = {int(statement_timeout_ms)}"))
        elif dialect == "sqlite":
            session.execute(text("PRAGMA query_only = ON"))
        yield session
    finally:
        session.close()
