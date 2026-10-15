"""Structured extraction schema (SPEC section 8.2).

Both extractors return the same validated shape, so the persistence and trend layers
never see a malformed result. ``PostExtraction.from_llm_payload`` is the only place an
LLM response becomes an extraction: it drops ids outside the taxonomy, deduplicates,
and records the dropped terms as ``unknown_terms`` for the cost/quality report.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from skillpulse.extraction.taxonomy import Taxonomy, normalize_term

Extractor = Literal["baseline", "llm"]
SENIORITIES = ("intern", "junior", "mid", "senior", "staff", "principal", "lead", "unspecified")


class SkillMatch(BaseModel):
    """One skill the post asks for, plus whether it is a hard requirement."""

    model_config = ConfigDict(frozen=True)

    id: str
    required: bool = True


class PostExtraction(BaseModel):
    """The normalised extraction stored for one post."""

    model_config = ConfigDict(frozen=True)

    skills: list[SkillMatch] = Field(default_factory=list)
    company: str | None = None
    role_family: str | None = None
    seniority: str | None = None
    remote: bool | None = None
    countries: list[str] = Field(default_factory=list)
    unknown_terms: list[str] = Field(default_factory=list)
    extractor: Extractor
    taxonomy_version: str
    fallback: bool = False

    @field_validator("seniority")
    @classmethod
    def _known_seniority(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value if value in SENIORITIES else "unspecified"

    @classmethod
    def from_llm_payload(
        cls,
        payload: object,
        taxonomy: Taxonomy,
        *,
        extractor: Extractor = "llm",
    ) -> PostExtraction:
        """Validate a decoded LLM payload against the taxonomy.

        Raises :class:`ValueError` when the payload is not an object or the skill list
        is malformed. Unknown skill ids and unknown role families are dropped and
        recorded instead of failing the whole post (SPEC section 8.2.1).
        """
        if not isinstance(payload, dict):
            raise ValueError("LLM payload must be a JSON object")
        raw_skills = payload.get("skills") or []
        if not isinstance(raw_skills, list):
            raise ValueError("LLM payload field 'skills' must be a list")

        valid_ids = taxonomy.ids
        skills: list[SkillMatch] = []
        unknown: list[str] = []
        seen: dict[str, bool] = {}
        for entry in raw_skills:
            skill_id: object = entry
            required = True
            if isinstance(entry, dict):
                skill_id = entry.get("id")
                required = bool(entry.get("required", True))
            if not isinstance(skill_id, str) or not skill_id.strip():
                unknown.append(repr(skill_id))
                continue
            skill_id = skill_id.strip()
            if skill_id not in valid_ids:
                unknown.append(skill_id)
                continue
            seen[skill_id] = seen.get(skill_id, False) or required
        for skill_id, required in seen.items():
            skills.append(SkillMatch(id=skill_id, required=required))

        role_family = payload.get("role_family")
        if not isinstance(role_family, str) or role_family not in taxonomy.role_family_ids:
            role_family = None

        countries_raw = payload.get("countries") or []
        countries = (
            [str(country).strip() for country in countries_raw if str(country).strip()]
            if isinstance(countries_raw, list)
            else []
        )

        remote = payload.get("remote")
        remote = remote if isinstance(remote, bool) else None

        company = payload.get("company")
        company = company.strip() if isinstance(company, str) and company.strip() else None

        seniority = payload.get("seniority")
        seniority = seniority if isinstance(seniority, str) else None

        unknown_terms = payload.get("unknown_terms") or []
        if isinstance(unknown_terms, list):
            unknown.extend(str(term) for term in unknown_terms if str(term).strip())

        ordered = sorted(skills, key=lambda match: match.id)
        return cls(
            skills=ordered,
            company=company,
            role_family=role_family,
            seniority=seniority,
            remote=remote,
            countries=sorted(set(countries)),
            unknown_terms=sorted(set(unknown)),
            extractor=extractor,
            taxonomy_version=taxonomy.version,
        )

    def skill_ids(self) -> list[str]:
        """Return the skill ids in this extraction."""
        return [match.id for match in self.skills]

    def countries_json(self) -> str:
        """Serialise countries for the ``post_extractions`` column."""
        return json.dumps(self.countries)

    def unknown_terms_json(self) -> str:
        """Serialise unknown terms for the ``post_extractions`` column."""
        return json.dumps(self.unknown_terms)


def role_family_hint(text: str, taxonomy: Taxonomy) -> str | None:
    """Return the first role family whose alias appears in ``text``, else ``None``.

    Deterministic helper used by the baseline (which does not do role extraction) and by
    the eval set builder; the LLM adapter returns the model's own value.
    """
    lowered = normalize_term(text)
    best: str | None = None
    best_len = 0
    for family in taxonomy.role_families:
        for alias in family.aliases:
            key = normalize_term(alias)
            if key and key in lowered and len(key) > best_len:
                best, best_len = family.id, len(key)
    return best
