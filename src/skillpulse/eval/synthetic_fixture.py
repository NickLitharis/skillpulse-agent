"""Deterministic five-year synthetic metadata fixture for the agent evaluation.

SPEC section 11 asks the agent set to run against a *frozen* fixture database of about
five years of posts with fixed extracted fields, so the expected answers never change.
The database built here holds **no post text and no Hacker News data**: every row is
generated from the deterministic seed below and carries only a synthetic item id, month,
company, role family, 3-state remote flag, countries and skills.

Provenance is explicit and machine-checkable:

* :data:`SYNTHETIC_FIXTURE_VERSION` names this profile; the tiny fixture in
  :mod:`skillpulse.eval.fixture` keeps its own profile for the fast smoke tests so the
  canonical counts cannot drift when the smoke fixture changes;
* :func:`synthetic_row_hash` is a SHA-256 over the canonical JSON rows, so any change to
  the data changes the hash, and the hash is the same on Windows, Linux and any SQLite
  version because it is computed before SQLite ever sees a row;
* the ids are synthetic and cannot be mistaken for real HN item ids.

Nothing here is fetched, and nothing here is written to the shared Postgres or any
production database: the fixture is built in memory (or in a caller-supplied local
SQLite file) per process.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from skillpulse.eval.fixture import _build_views
from skillpulse.extraction.repository import sync_taxonomy
from skillpulse.extraction.taxonomy import Taxonomy, load_taxonomy
from skillpulse.models import (
    Base,
    Post,
    PostExtractionRow,
    PostSkillRow,
    Thread,
)

SYNTHETIC_FIXTURE_VERSION = "synth-2019-2023-v1"
SYNTHETIC_START = (2019, 1)
SYNTHETIC_MONTHS = 60
SYNTHETIC_POSTS_PER_MONTH = 30
POST_ID_BASE = 900_000
THREAD_ID_BASE = 700_000
COMPANY_PREFIX = "Synth Labs"
SOURCE = "synthetic_fixture"

ROLE_CYCLE: tuple[str, ...] = (
    "backend",
    "frontend",
    "data",
    "infra",
    "ml",
    "mobile",
    "security",
    "fullstack",
)
_BASE_SKILLS: dict[str, tuple[str, ...]] = {
    "backend": ("python", "postgres", "docker"),
    "frontend": ("react", "typescript", "docker"),
    "data": ("python", "postgres", "aws"),
    "infra": ("kubernetes", "terraform", "aws"),
    "ml": ("python", "kubernetes"),
    "mobile": ("typescript", "react"),
    "security": ("python", "docker"),
    "fullstack": ("python", "react", "docker"),
}
# A weighted country pool (this is fixture data, not a claim about the real market). The
# three Greece rows are deliberately far below the 30-post floor so the "rare country"
# case has a genuinely undersampled segment.
_COUNTRY_POOL: tuple[str, ...] = (
    ("United States",) * 10
    + ("Germany",) * 4
    + ("Netherlands",) * 3
    + ("United Kingdom",) * 2
    + ("Canada",) * 1
)
_GREECE_MONTHS = (4, 19, 33)
_GREECE_INDEX = 7
# From this month on, more backend-side roles ask for Kubernetes: a deterministic uptrend.
_GROWTH_START = 30
_KUBERNETES_ROLES = frozenset({"backend", "data", "infra", "ml"})
# Frontend drops Docker from this month on: a deterministic downtrend.
_DECLINE_START = 24


def _month(year: int, month: int) -> datetime:
    return datetime(year, month, 1, tzinfo=UTC)


def _months() -> list[datetime]:
    out: list[datetime] = []
    year, month = SYNTHETIC_START
    for _ in range(SYNTHETIC_MONTHS):
        out.append(_month(year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1
    return out


def _skills(month_index: int, index: int, role: str) -> list[str]:
    skills = list(_BASE_SKILLS[role])
    # Deterministic variation so shares are not all 1.0: drop one base skill.
    if index % 7 == 3 and len(skills) > 1:
        skills = skills[:-1]
    # A deterministic uptrend: about half the backend-side posts add Kubernetes.
    if month_index >= _GROWTH_START and role in _KUBERNETES_ROLES and index % 2 == 0:
        skills.append("kubernetes")
    # A deterministic downtrend: frontend drops Docker for half its posts.
    if month_index >= _DECLINE_START and role == "frontend" and index % 2 == 0:
        skills = [skill for skill in skills if skill != "docker"]
    return sorted(set(skills))


def _country(month_index: int, index: int) -> str:
    if month_index in _GREECE_MONTHS and index == _GREECE_INDEX:
        return "Greece"
    return _COUNTRY_POOL[(month_index * 7 + index) % len(_COUNTRY_POOL)]


def synthetic_rows() -> list[dict[str, Any]]:
    """Return every synthetic post row in a fixed order (the source of truth)."""
    rows: list[dict[str, Any]] = []
    for month_index, month in enumerate(_months()):
        for index in range(SYNTHETIC_POSTS_PER_MONTH):
            post_id = POST_ID_BASE + month_index * 100 + index
            role = ROLE_CYCLE[(month_index + index) % len(ROLE_CYCLE)]
            remote: bool | None = [True, False, None][index % 3]
            rows.append(
                {
                    "post_id": post_id,
                    "thread_id": THREAD_ID_BASE + month_index,
                    "month": month.date().isoformat(),
                    "company": f"{COMPANY_PREFIX} {post_id:07d}",
                    "role_family": role,
                    "remote": remote,
                    "countries": [_country(month_index, index)],
                    "skills": _skills(month_index, index, role),
                }
            )
    return rows


def canonical_rows_json(rows: list[dict[str, Any]] | None = None) -> str:
    """Serialise rows deterministically so the hash is stable across platforms."""
    return json.dumps(
        synthetic_rows() if rows is None else rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def synthetic_row_hash(rows: list[dict[str, Any]] | None = None) -> str:
    """SHA-256 of the canonical synthetic rows (stable across OS and SQLite)."""
    return hashlib.sha256(canonical_rows_json(rows).encode("utf-8")).hexdigest()


def synthetic_manifest(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Describe this frozen fixture profile for the dataset and the results JSON."""
    data = synthetic_rows() if rows is None else rows
    last_year = SYNTHETIC_START[0] + (SYNTHETIC_MONTHS - 1) // 12
    return {
        "version": SYNTHETIC_FIXTURE_VERSION,
        "row_hash": synthetic_row_hash(data),
        "source": SOURCE,
        "years": [SYNTHETIC_START[0], last_year],
        "months": SYNTHETIC_MONTHS,
        "posts": len(data),
        "threads": SYNTHETIC_MONTHS,
        "contains_post_text": False,
        "contains_real_hn_data": False,
        "note": (
            "deterministic synthetic metadata only; ids, companies and rows are generated "
            "and are not Hacker News data"
        ),
    }


def _seed(engine: Engine, taxonomy: Taxonomy) -> None:
    rows = synthetic_rows()
    known = {skill for role in _BASE_SKILLS.values() for skill in role}
    for skill_id in known:
        if taxonomy.get(skill_id) is None:
            raise ValueError(f"synthetic fixture skill {skill_id!r} is not in the taxonomy")
    with Session(engine) as session:
        sync_taxonomy(session, taxonomy)
        for month_index, month in enumerate(_months()):
            session.add(
                Thread(
                    id=THREAD_ID_BASE + month_index,
                    title=f"Ask HN: Who is hiring? (synthetic {month.date()})",
                    month=month,
                    created_at=month,
                )
            )
        session.flush()
        for row in rows:
            month = datetime.fromisoformat(str(row["month"])).replace(tzinfo=UTC)
            session.add(
                Post(
                    id=int(row["post_id"]),
                    thread_id=int(row["thread_id"]),
                    month=month,
                    time=month,
                    text_raw="",  # the fixture holds no post text
                    text_clean="",
                )
            )
            session.add(
                PostExtractionRow(
                    post_id=int(row["post_id"]),
                    extractor="baseline",
                    taxonomy_version=taxonomy.version,
                    company=str(row["company"]),
                    role_family=str(row["role_family"]),
                    remote=row["remote"],
                    countries=json.dumps(sorted(row["countries"])),
                    unknown_terms="[]",
                    is_fallback=False,
                )
            )
            for skill_id in row["skills"]:
                session.add(
                    PostSkillRow(
                        post_id=int(row["post_id"]),
                        skill_id=str(skill_id),
                        required=True,
                        extractor="baseline",
                        taxonomy_version=taxonomy.version,
                    )
                )
        session.commit()


def build_synthetic_fixture_engine(path: str | Path | None = None) -> Engine:
    """Build and return the five-year synthetic fixture database."""
    if path:
        url = f"sqlite+pysqlite:///{Path(path).as_posix()}"
        engine = create_engine(url, future=True)
    else:
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
