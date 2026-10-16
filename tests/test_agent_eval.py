"""Agent evaluation runner on the frozen fixture DB (fake model only).

This proves the pipeline and the evaluation runner are honest; it is not a real-model
S3/S4 result.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from skillpulse.agent.models import FakeAgentModel, PlanResult
from skillpulse.eval.agent_eval import load_agent_cases, run_agent_eval
from skillpulse.eval.fixture import build_fixture_engine
from skillpulse.extraction.llm import LLMExtractionError
from skillpulse.extraction.taxonomy import REPO_ROOT, Taxonomy, load_taxonomy

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


def test_adversarial_case_is_scored_on_normal_criteria_for_a_real_run() -> None:
    """The "invents" oracle needs the inventing fake; a real model must be scored honestly.

    ``adversarial_model == "invents"`` passes only when the forbidden number reaches
    ``unsupported``, i.e. when the model wrote it and the grounding gate stripped it. A real
    model is never told to invent anything, so scoring it that way made the case unwinnable.
    """
    engine = build_fixture_engine()
    try:
        report = run_agent_eval(
            engine,
            load_taxonomy(),
            load_agent_cases(DATASET),
            model_label="some-real-model",
            dataset=DATASET.name,
            model=FakeAgentModel(),
        )
    finally:
        engine.dispose()

    adversarial = next(r for r in report.results if r.id == "adversarial-invented-number")
    assert adversarial.unsupported == [], "an honest answer has nothing for the gate to strip"
    assert adversarial.grounded is True
    assert adversarial.correct is True, "not inventing the number must count as correct"


class ExplodingModel(FakeAgentModel):
    """A planner that fails on one question, the way a flaky provider does."""

    name = "fake-exploding"

    def plan(
        self, question: str, taxonomy: Taxonomy, tool_specs: tuple[dict[str, Any], ...]
    ) -> PlanResult:
        if "greek" in question.lower():
            raise LLMExtractionError("provider returned an empty reply twice")
        return super().plan(question, taxonomy, tool_specs)


def test_one_failing_case_does_not_abort_the_run() -> None:
    """A live 64-case run must not lose 63 results to one transient provider error."""
    cases = load_agent_cases(DATASET)
    taxonomy = load_taxonomy()

    engine = build_fixture_engine()
    try:
        control = run_agent_eval(
            engine,
            taxonomy,
            cases,
            model_label="fake",
            dataset=DATASET.name,
            model=FakeAgentModel(),
        )
        exploded = run_agent_eval(
            engine,
            taxonomy,
            cases,
            model_label="fake-exploding",
            dataset=DATASET.name,
            model=ExplodingModel(),
        )
    finally:
        engine.dispose()

    # Every case is still recorded, and the injected failure costs exactly one case.
    assert control.cases == exploded.cases == 9
    assert len(exploded.results) == 9, "every case must still be recorded"
    failed = [r for r in exploded.results if (r.error or "").startswith("case_error:")]
    assert [r.id for r in failed] == ["impossible-greek"]
    assert failed[0].correct is False
    assert exploded.correct == control.correct - 1
    # A case with no answer is excluded from grounding, exactly like a tool failure.
    assert exploded.grounding_rate == 1.0
