"""Taxonomy loading and normalisation (SPEC section 8.1).

The taxonomy is the single source of truth for the skills and role families the
extractors may return. It is loaded from ``config/skills.yaml`` and
``config/role_families.yaml``, validated once, and shared by both extractors.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SKILLS_PATH = REPO_ROOT / "config" / "skills.yaml"
DEFAULT_ROLE_FAMILIES_PATH = REPO_ROOT / "config" / "role_families.yaml"

_WHITESPACE = re.compile(r"\s+")


class TaxonomyError(ValueError):
    """The taxonomy file is missing, malformed, or internally inconsistent."""


def normalize_term(value: str) -> str:
    """Normalise a skill term for matching: NFKC, casefold, collapse whitespace.

    Casefold and NFKC keep multilingual text (accented French, German, CJK) matching
    the same way everywhere, so an alias matches whatever the post's language is.
    """
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


@dataclass(frozen=True)
class SkillDef:
    """One taxonomy entry."""

    id: str
    name: str
    category: str
    aliases: tuple[str, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        """Every surface form that identifies this skill, longest first."""
        return tuple(sorted({self.name, *self.aliases}, key=len, reverse=True))


@dataclass(frozen=True)
class RoleFamily:
    """One role family entry."""

    id: str
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Taxonomy:
    """A validated taxonomy: skills, role families and lookup indexes."""

    version: str
    skills: tuple[SkillDef, ...]
    role_families: tuple[RoleFamily, ...]
    role_family_version: str

    def get(self, skill_id: str) -> SkillDef | None:
        """Return the skill definition for ``skill_id`` or ``None``."""
        return self._by_id.get(skill_id)

    @property
    def ids(self) -> frozenset[str]:
        """Every valid skill id."""
        return frozenset(self._by_id)

    @property
    def role_family_ids(self) -> frozenset[str]:
        """Every valid role-family id."""
        return frozenset(rf.id for rf in self.role_families)

    @property
    def _by_id(self) -> Mapping[str, SkillDef]:
        return {skill.id: skill for skill in self.skills}

    def alias_to_id(self) -> dict[str, str]:
        """Map every normalised alias (not the primary name) to a skill id."""
        index: dict[str, str] = {}
        for skill in self.skills:
            for alias in skill.aliases:
                key = normalize_term(alias)
                if key and key not in index:
                    index[key] = skill.id
        return index

    def matching_terms(self) -> list[tuple[str, str]]:
        """Return ``(surface_term, skill_id)`` pairs, longest term first."""
        pairs: list[tuple[str, str]] = []
        for skill in self.skills:
            for term in skill.terms:
                pairs.append((term, skill.id))
        pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
        return pairs


def _load_yaml(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError as exc:  # pragma: no cover - configuration error path
        raise TaxonomyError(f"taxonomy file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise TaxonomyError(f"invalid YAML in {path}: {exc}") from exc


def _require_mapping(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TaxonomyError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _require_list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        raise TaxonomyError(f"{what} must be a list, got {type(value).__name__}")
    return value


def _require_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaxonomyError(f"{what} must be a non-empty string")
    return value.strip()


def load_taxonomy(
    skills_path: Path | None = None,
    role_families_path: Path | None = None,
) -> Taxonomy:
    """Load and validate the skill taxonomy and role families.

    Raises :class:`TaxonomyError` on a missing file, a malformed entry, or duplicate
    ids/aliases. Validation is deterministic, so a bad edit fails at load time rather
    than at the first post.
    """
    skills_path = skills_path or DEFAULT_SKILLS_PATH
    role_families_path = role_families_path or DEFAULT_ROLE_FAMILIES_PATH

    raw_skills = _require_mapping(_load_yaml(skills_path), str(skills_path))
    version = _require_id(raw_skills.get("version"), "skills.version")
    skills: list[SkillDef] = []
    seen_ids: set[str] = set()
    seen_aliases: dict[str, str] = {}
    for index, entry in enumerate(_require_list(raw_skills.get("skills"), "skills.skills")):
        item = _require_mapping(entry, f"skills[{index}]")
        skill_id = _require_id(item.get("id"), f"skills[{index}].id")
        name = _require_id(item.get("name"), f"skills[{index}].name")
        category = _require_id(item.get("category"), f"skills[{index}].category")
        aliases_raw = item.get("aliases") or []
        alias_list = _require_list(aliases_raw, f"skills[{index}].aliases")
        aliases = tuple(_require_id(alias, f"skills[{index}].aliases") for alias in alias_list)
        if skill_id in seen_ids:
            raise TaxonomyError(f"duplicate skill id: {skill_id}")
        seen_ids.add(skill_id)
        for alias in (name, *aliases):
            key = normalize_term(alias)
            owner = seen_aliases.get(key)
            if owner is not None and owner != skill_id:
                raise TaxonomyError(f"alias {alias!r} is claimed by both {owner} and {skill_id}")
            seen_aliases[key] = skill_id
        skills.append(SkillDef(id=skill_id, name=name, category=category, aliases=aliases))

    raw_roles = _require_mapping(_load_yaml(role_families_path), str(role_families_path))
    role_version = _require_id(raw_roles.get("version"), "role_families.version")
    role_families: list[RoleFamily] = []
    seen_role_ids: set[str] = set()
    raw_role_list = _require_list(
        raw_roles.get("role_families"), "role_families.role_families"
    )
    for index, entry in enumerate(raw_role_list):
        item = _require_mapping(entry, f"role_families[{index}]")
        role_id = _require_id(item.get("id"), f"role_families[{index}].id")
        name = _require_id(item.get("name"), f"role_families[{index}].name")
        role_aliases = _require_list(
            item.get("aliases") or [], f"role_families[{index}].aliases"
        )
        aliases = tuple(
            _require_id(alias, f"role_families[{index}].aliases") for alias in role_aliases
        )
        if role_id in seen_role_ids:
            raise TaxonomyError(f"duplicate role family id: {role_id}")
        seen_role_ids.add(role_id)
        role_families.append(RoleFamily(id=role_id, name=name, aliases=aliases))

    if not skills:
        raise TaxonomyError("taxonomy defines no skills")
    if not role_families:
        raise TaxonomyError("taxonomy defines no role families")
    return Taxonomy(
        version=version,
        skills=tuple(skills),
        role_families=tuple(role_families),
        role_family_version=role_version,
    )


def iter_categories(skills: Iterable[SkillDef]) -> list[str]:
    """Return the sorted distinct categories."""
    return sorted({skill.category for skill in skills})
