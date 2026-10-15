"""Taxonomy loading and validation (SPEC section 8.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from skillpulse.extraction.taxonomy import (
    TaxonomyError,
    load_taxonomy,
    normalize_term,
)


def test_loads_the_shipped_taxonomy() -> None:
    taxonomy = load_taxonomy()
    assert taxonomy.version
    assert {"python", "kubernetes", "postgres", "langgraph"} <= taxonomy.ids
    assert "backend" in taxonomy.role_family_ids
    assert taxonomy.get("k8s") is None
    assert taxonomy.get("kubernetes") is not None


def test_alias_index_is_unique_and_normalised() -> None:
    taxonomy = load_taxonomy()
    index = taxonomy.alias_to_id()
    assert index["k8s"] == "kubernetes"
    assert index["postgresql"] == "postgres"
    assert index["js"] == "javascript"


def test_matching_terms_are_longest_first() -> None:
    taxonomy = load_taxonomy()
    terms = [term for term, _ in taxonomy.matching_terms()]
    lengths = [len(term) for term in terms]
    assert lengths == sorted(lengths, reverse=True)


def test_normalize_term_casefolds_and_collapses_space() -> None:
    assert normalize_term("  KUBERNETES\t k8s  ") == "kubernetes k8s"
    assert normalize_term("TÉLÉTRAVAIL") == "télétravail"


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_duplicate_alias_between_skills_is_rejected(tmp_path: Path) -> None:
    skills = _write(
        tmp_path,
        "skills.yaml",
        """
version: "t1"
skills:
  - {id: a, name: Alpha, category: x, aliases: [shared]}
  - {id: b, name: Beta, category: x, aliases: [shared]}
""",
    )
    roles = _write(
        tmp_path,
        "roles.yaml",
        'version: "t1"\nrole_families:\n  - {id: other, name: Other}\n',
    )
    with pytest.raises(TaxonomyError, match="claimed by both"):
        load_taxonomy(skills, roles)


def test_duplicate_skill_id_is_rejected(tmp_path: Path) -> None:
    skills = _write(
        tmp_path,
        "skills.yaml",
        """
version: "t1"
skills:
  - {id: a, name: Alpha, category: x}
  - {id: a, name: Beta, category: x}
""",
    )
    roles = _write(
        tmp_path, "roles.yaml", 'version: "t1"\nrole_families:\n  - {id: other, name: Other}\n'
    )
    with pytest.raises(TaxonomyError, match="duplicate skill id"):
        load_taxonomy(skills, roles)


def test_malformed_yaml_is_rejected(tmp_path: Path) -> None:
    skills = _write(tmp_path, "skills.yaml", "version: [not a string\n")
    roles = _write(tmp_path, "roles.yaml", 'version: "t1"\nrole_families: []\n')
    with pytest.raises(TaxonomyError):
        load_taxonomy(skills, roles)


def test_missing_skills_key_is_rejected(tmp_path: Path) -> None:
    skills = _write(tmp_path, "skills.yaml", 'version: "t1"\n')
    roles = _write(
        tmp_path, "roles.yaml", 'version: "t1"\nrole_families:\n  - {id: other, name: Other}\n'
    )
    with pytest.raises(TaxonomyError):
        load_taxonomy(skills, roles)
