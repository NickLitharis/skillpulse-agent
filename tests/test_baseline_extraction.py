"""Baseline extractor: aliases, boundaries, negation, multilingual, remote."""

from __future__ import annotations

from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.taxonomy import load_taxonomy


def _skills(text: str) -> dict[str, bool]:
    extraction = BaselineExtractor(load_taxonomy()).extract(text)
    return {match.id: match.required for match in extraction.skills}


def test_alias_matching_and_word_boundaries() -> None:
    found = _skills("We use JavaScript and PostgreSQL, plus k8s in production.")
    assert set(found) == {"javascript", "postgres", "kubernetes"}
    # "java" must not fire inside "javascript".
    assert "java" not in found


def test_short_alias_does_not_match_inside_a_word() -> None:
    assert "go" not in _skills("We are going to market with a gopher mascot.")
    assert "r" not in _skills("Our roadmaps are ready.")


def test_negation_drops_the_skill() -> None:
    found = _skills("No Kubernetes here. Docker required and Python.")
    assert "kubernetes" not in found
    assert found["docker"] is True
    assert found["python"] is True


def test_trailing_negation_drops_the_skill() -> None:
    found = _skills("Kubernetes not needed. Terraform required.")
    assert "kubernetes" not in found
    assert found["terraform"] is True


def test_multilingual_negation() -> None:
    assert "kubernetes" not in _skills("Kenntnisse in Docker. Kein Kubernetes nötig.")
    assert "kafka" not in _skills("Nous ne cherchons pas Kafka.")


def test_nice_to_have_sets_required_false() -> None:
    found = _skills("Python required. Kubernetes is a plus.")
    assert found["python"] is True
    assert found["kubernetes"] is False


def test_required_wins_when_a_skill_appears_in_a_hard_clause() -> None:
    found = _skills("Kubernetes a plus, but Python and Kubernetes required.")
    assert found["kubernetes"] is True


def test_multilingual_posts_still_match_ascii_aliases() -> None:
    french = _skills("Nous recherchons un développeur Python et PostgreSQL.")
    german = _skills("Wir suchen Rust und Docker Kenntnisse.")
    spanish = _skills("Buscamos ingeniero con Spark y Airflow.")
    japanese = _skills("Python エンジニア募集。AWS と Kubernetes の経験が必要です。")
    assert "python" in french and "postgres" in french
    assert "rust" in german and "docker" in german
    assert "spark" in spanish and "airflow" in spanish
    assert {"python", "aws", "kubernetes"} <= set(japanese)


def test_remote_detection() -> None:
    extractor = BaselineExtractor(load_taxonomy())
    assert extractor.extract("Fully remote role.").remote is True
    assert extractor.extract("Hybrid in Berlin.").remote is False
    assert extractor.extract("Remote, télétravail possible.").remote is True
    assert extractor.extract("No remote work; on-site only.").remote is False
    assert extractor.extract("Join our team.").remote is None


def test_baseline_does_not_guess_role_or_country() -> None:
    extraction = BaselineExtractor(load_taxonomy()).extract("Python developer in Berlin.")
    assert extraction.role_family is None
    assert extraction.countries == []
    assert extraction.extractor == "baseline"
