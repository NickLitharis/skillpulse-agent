"""Persistence for taxonomy and extraction results.

Writing an extraction is replace-by-identity: the rows for
``(post, extractor, taxonomy_version)`` are removed and rewritten, so re-running the
extractor creates no duplicates and a changed result replaces the old one.
"""

from __future__ import annotations

import json

from sqlalchemy import and_, delete, select
from sqlalchemy.orm import Session

from skillpulse.extraction.schema import PostExtraction, SkillMatch
from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.models import Post, PostExtractionRow, PostSkillRow, Skill


def sync_taxonomy(session: Session, taxonomy: Taxonomy) -> int:
    """Upsert every taxonomy entry into the ``skills`` table; return the count."""
    for definition in taxonomy.skills:
        row = session.get(Skill, definition.id)
        aliases = json.dumps(list(definition.aliases))
        if row is None:
            session.add(
                Skill(
                    id=definition.id,
                    name=definition.name,
                    category=definition.category,
                    aliases=aliases,
                )
            )
        else:
            row.name = definition.name
            row.category = definition.category
            row.aliases = aliases
    session.flush()
    return len(taxonomy.skills)


def _ensure_skill(session: Session, taxonomy: Taxonomy, skill_id: str) -> None:
    if session.get(Skill, skill_id) is not None:
        return
    definition = taxonomy.get(skill_id)
    if definition is None:  # pragma: no cover - callers validate first
        raise KeyError(f"skill {skill_id!r} is not in taxonomy {taxonomy.version}")
    session.add(
        Skill(
            id=definition.id,
            name=definition.name,
            category=definition.category,
            aliases=json.dumps(list(definition.aliases)),
        )
    )
    session.flush()


def record_extraction(
    session: Session, post: Post, extraction: PostExtraction, taxonomy: Taxonomy
) -> None:
    """Store one extraction and its skills, replacing any previous result of the same identity."""
    if extraction.taxonomy_version != taxonomy.version:  # pragma: no cover - guard rail
        raise ValueError(
            f"extraction taxonomy {extraction.taxonomy_version} != {taxonomy.version}"
        )
    skill_identity = and_(
        PostSkillRow.post_id == post.id,
        PostSkillRow.extractor == extraction.extractor,
        PostSkillRow.taxonomy_version == extraction.taxonomy_version,
    )
    extraction_identity = and_(
        PostExtractionRow.post_id == post.id,
        PostExtractionRow.extractor == extraction.extractor,
        PostExtractionRow.taxonomy_version == extraction.taxonomy_version,
    )
    session.execute(delete(PostSkillRow).where(skill_identity))
    session.execute(delete(PostExtractionRow).where(extraction_identity))
    session.flush()

    session.add(
        PostExtractionRow(
            post_id=post.id,
            extractor=extraction.extractor,
            taxonomy_version=extraction.taxonomy_version,
            company=extraction.company,
            role_family=extraction.role_family,
            seniority=extraction.seniority,
            remote=extraction.remote,
            countries=extraction.countries_json(),
            unknown_terms=extraction.unknown_terms_json(),
            is_fallback=extraction.fallback,
        )
    )
    for match in extraction.skills:
        _ensure_skill(session, taxonomy, match.id)
        session.add(
            PostSkillRow(
                post_id=post.id,
                skill_id=match.id,
                required=match.required,
                extractor=extraction.extractor,
                taxonomy_version=extraction.taxonomy_version,
            )
        )
    session.flush()


def get_extraction(
    session: Session, post_id: int, extractor: str, taxonomy_version: str
) -> PostExtraction | None:
    """Read back one stored extraction, or ``None`` when it is not there."""
    row = session.scalars(
        select(PostExtractionRow).where(
            PostExtractionRow.post_id == post_id,
            PostExtractionRow.extractor == extractor,
            PostExtractionRow.taxonomy_version == taxonomy_version,
        )
    ).one_or_none()
    if row is None:
        return None
    skill_rows = session.scalars(
        select(PostSkillRow)
        .where(
            PostSkillRow.post_id == post_id,
            PostSkillRow.extractor == extractor,
            PostSkillRow.taxonomy_version == taxonomy_version,
        )
        .order_by(PostSkillRow.skill_id)
    ).all()
    return PostExtraction(
        skills=[SkillMatch(id=skill.skill_id, required=skill.required) for skill in skill_rows],
        company=row.company,
        role_family=row.role_family,
        seniority=row.seniority,
        remote=row.remote,
        countries=json.loads(row.countries),
        unknown_terms=json.loads(row.unknown_terms),
        extractor=row.extractor,  # type: ignore[arg-type]
        taxonomy_version=row.taxonomy_version,
        fallback=row.is_fallback,
    )
