"""Agent evaluation runner on the frozen fixture DB (fake model only).

This proves the pipeline and the evaluation runner are honest; it is not a real-model
S3/S4 result.
"""

from __future__ import annotations

from pathlib import Path

from skillpulse.eval.agent_eval import load_agent_cases, run_agent_eval
from skillpulse.eval.fixture import build_fixture_engine
from skillpulse.extraction.taxonomy import REPO_ROOT, load_taxonomy

DATASET = REPO_ROOT / "eval" / "datasets" / "agent_cases.json"


def test_agent_dataset_covers_the_required_categories() -> None:
    cases = load_agent_cases(DATASET)
    ids = {case.id for case in cases}
    assert "ambiguous-hot-skills" in ids
    assert "impossible-greek" in ids
    assert "injection-direct-number" in ids
    assert "adversarial-invented-number" in ids
    assert "tools-failure" in ids
    assert "client-cancellation" in ids


def test_agent_eval_on_fixture_is_correct_and_grounded() -> None:
    engine = build_fixture_engine()
    try:
        report = run_agent_eval(
            engine,
            load_taxonomy(),
            load_agent_cases(DATASET),
            model_label="fake",
            dataset=DATASET.name,
        )
    finally:
        engine.dispose()
    assert report.model == "fake"
    assert report.source == "fixture"
    assert report.cases == 9
    assert report.correct == report.cases, [r.id for r in report.results if not r.correct]
    assert report.grounding_rate == 1.0
    assert report.tool_selection_accuracy == 1.0
    invented = next(r for r in report.results if r.id == "adversarial-invented-number")
    assert "999" in invented.unsupported


def test_dataset_path_exists() -> None:
    assert Path(DATASET).is_file()
