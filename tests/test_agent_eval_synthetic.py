"""Contract and failure-sensitive checks for the canonical agent evaluation.

This runs the deterministic fake model/planner over the five-year synthetic fixture. It is
a **contract** check (the pipeline, the tools and the grounding gate behave as specified),
never a real-model S3/S4 accuracy measurement. The real-HN corpus and the real agent model
remain gated.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from skillpulse.agent.graph import run_agent
from skillpulse.agent.grounding import check_grounding
from skillpulse.agent.models import FakeAgentModel
from skillpulse.eval.agent_dataset import (
    CATEGORIES,
    DATASET_PATH,
    build_agent_dataset,
    dataset_json,
)
from skillpulse.eval.agent_eval import (
    AgentCase,
    check_expected_facts,
    load_agent_dataset,
    run_agent_eval,
)
from skillpulse.eval.synthetic_fixture import (
    SYNTHETIC_FIXTURE_VERSION,
    build_synthetic_fixture_engine,
    synthetic_manifest,
    synthetic_row_hash,
    synthetic_rows,
)
from skillpulse.extraction.taxonomy import REPO_ROOT, load_taxonomy
from skillpulse.tools import ToolRuntime
from skillpulse.tools.models import ToolResult

BASELINE = REPO_ROOT / "eval" / "baseline_agent.json"
RESULT = REPO_ROOT / "eval" / "results" / "agent_fixture_synthetic.json"
# Pinned on purpose: any change to the generated rows must be a deliberate, reviewed
# fixture-version bump, not an accidental edit that silently moves every expected count.
FROZEN_ROW_HASH = "c6c6edb67ea5bc4eb6a966aa606c56681bc12d98ba043ed707f515dfa9b56847"


@pytest.fixture(scope="module")
def synthetic_engine() -> Iterator[Engine]:
    engine = build_synthetic_fixture_engine()
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def synthetic_runtime(synthetic_engine: Engine) -> ToolRuntime:
    factory = sessionmaker(bind=synthetic_engine, expire_on_commit=False)
    return ToolRuntime(factory, load_taxonomy())


@pytest.fixture(scope="module")
def payload(synthetic_engine: Engine) -> dict[str, object]:
    return build_agent_dataset(synthetic_engine, load_taxonomy())


# ---------------------------------------------------------------------------
# Fixture provenance and frozen shape
# ---------------------------------------------------------------------------


def test_synthetic_fixture_is_five_years_of_metadata_only() -> None:
    rows = synthetic_rows()
    months = {str(row["month"])[:7] for row in rows}
    assert len(rows) == 1800
    assert len(months) == 60
    assert min(months) == "2019-01"
    assert max(months) == "2023-12"
    for row in rows:
        assert set(row) == {
            "post_id",
            "thread_id",
            "month",
            "company",
            "role_family",
            "remote",
            "countries",
            "skills",
        }
        assert "text" not in row and "text_raw" not in row and "text_clean" not in row
        assert str(row["company"]).startswith("Synth Labs")
        assert row["remote"] in (True, False, None)
    manifest = synthetic_manifest(rows)
    assert manifest["version"] == SYNTHETIC_FIXTURE_VERSION
    assert manifest["contains_post_text"] is False
    assert manifest["contains_real_hn_data"] is False
    assert manifest["years"] == [2019, 2023]


def test_row_hash_is_frozen_and_stable() -> None:
    assert synthetic_row_hash() == FROZEN_ROW_HASH


def test_unknown_remote_is_distinct_from_false_on_the_synthetic_fixture(
    synthetic_runtime: ToolRuntime,
) -> None:
    remote = synthetic_runtime.call("skill_counts", {"remote": True, "top_n": 50})
    non_remote = synthetic_runtime.call("skill_counts", {"remote": False, "top_n": 50})
    unknown_included = synthetic_runtime.call("skill_counts", {"top_n": 50})
    assert remote.rows and non_remote.rows and unknown_included.rows
    assert unknown_included.rows[0]["total_posts"] > non_remote.rows[0]["total_posts"]
    assert unknown_included.rows[0]["total_posts"] > remote.rows[0]["total_posts"]


def test_full_year_window_includes_december(synthetic_runtime: ToolRuntime) -> None:
    """Regression: a date-bound ``month_to`` used to drop December on SQLite."""
    result = synthetic_runtime.call(
        "skill_trend",
        {
            "skill_id": "python",
            "role_family": "backend",
            "month_from": "2021-01-01",
            "month_to": "2021-12-01",
        },
    )
    months = [point["month"] for point in result.rows]
    assert len(months) == 12
    assert months[-1] == "2021-12-01"


# ---------------------------------------------------------------------------
# Canonical dataset contract
# ---------------------------------------------------------------------------


def test_canonical_dataset_covers_categories_tools_and_size(payload: dict[str, object]) -> None:
    cases = payload["cases"]
    assert isinstance(cases, list)
    assert len(cases) >= 60
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    used_tools = {tool for case in cases for tool in case["expected_tools"]}
    assert used_tools == {
        "search_jobs",
        "skill_counts",
        "skill_trend",
        "compare_skills",
        "match_cv",
    }
    categories = {str(case["category"]) for case in cases}
    assert categories == set(CATEGORIES)
    assert sum(1 for case in cases if case["must_refuse"]) >= 3
    assert sum(1 for case in cases if case["must_ask_back"]) >= 3
    assert sum(1 for case in cases if case["must_state_insufficient"]) >= 4
    for case in cases:
        assert case["id"] and case["question"]
        assert isinstance(case["expected_tools"], list)
        assert isinstance(case["expected_facts"], list)
        assert "must_refuse" in case


def test_committed_dataset_matches_regeneration(payload: dict[str, object]) -> None:
    committed = DATASET_PATH.read_text(encoding="utf-8")
    assert committed == dataset_json(payload), (
        "eval/datasets/agent_eval_cases.json is stale; run `skillpulse eval build-agent-cases`"
    )


def test_every_case_reaches_its_expected_tool_and_facts(
    synthetic_engine: Engine, payload: dict[str, object]
) -> None:
    cases, fixture = load_agent_dataset(DATASET_PATH)
    report = run_agent_eval(
        synthetic_engine,
        load_taxonomy(),
        cases,
        model_label="fake",
        dataset=DATASET_PATH.name,
        source="synthetic_fixture",
        fixture=fixture,
    )
    assert report.cases == len(cases)
    assert report.facts_checked == report.facts_passed, [
        result.fact_failures for result in report.results if result.fact_failures
    ]
    # The two deliberately simulated failures (tool_failure, cancellation) are never
    # scored correct, and the one fallback run is not scored grounded.
    simulated = [result for result in report.results if result.error is not None]
    assert {result.id for result in simulated} == {"tools-failure", "client-cancellation"}
    assert report.correct == report.cases - len(simulated), [
        (result.id, result.fact_failures, result.answer)
        for result in report.results
        if not result.correct
    ]
    assert report.tool_selection_accuracy == 1.0
    assert report.grounding_rate == round(61 / 64, 4)
    assert report.draft_grounded_rate == 1.0
    assert report.usage["provider"] == "fake"
    assert report.usage["total_tokens"] is None
    assert report.usage["reason"]
    assert report.mean_steps > 0


def test_result_json_is_tagged_not_real_acceptance() -> None:
    recorded = json.loads(RESULT.read_text(encoding="utf-8"))
    assert recorded["model"] == "fake"
    assert recorded["source"] == "synthetic_fixture"
    assert recorded["acceptance"] == "not_real_acceptance"
    assert recorded["fixture"]["row_hash"] == FROZEN_ROW_HASH
    assert recorded["usage"]["total_tokens"] is None
    assert recorded["usage"]["cost_usd"] is None


def test_regression_gate_allows_at_most_two_dropped_questions(
    synthetic_engine: Engine,
) -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert baseline["fixture"]["row_hash"] == synthetic_row_hash()
    cases, fixture = load_agent_dataset(DATASET_PATH)
    report = run_agent_eval(
        synthetic_engine,
        load_taxonomy(),
        cases,
        model_label="fake",
        dataset=DATASET_PATH.name,
        source="synthetic_fixture",
        fixture=fixture,
    )
    tolerance = int(baseline["gate_tolerance_questions"])
    grounded = sum(1 for result in report.results if result.grounded)
    assert report.correct >= baseline["metrics"]["correct"] - tolerance, "correctness dropped"
    assert grounded >= baseline["metrics"]["grounded"] - tolerance, "grounding dropped"


# ---------------------------------------------------------------------------
# Failure-sensitive controls: prove the checks can fail, then restore
# ---------------------------------------------------------------------------


def _backend_case(cases: list[AgentCase]) -> AgentCase:
    return next(case for case in cases if case.id == "top-backend")


def test_negative_control_changed_tool_count_is_detected(
    synthetic_runtime: ToolRuntime,
) -> None:
    cases, _ = load_agent_dataset(DATASET_PATH)
    case = _backend_case(cases)
    run = run_agent(
        case.question, runtime=synthetic_runtime, model=FakeAgentModel(), taxonomy=load_taxonomy()
    )
    passed, failures = check_expected_facts(run, case)
    assert passed and failures == []
    # Change the expected count and the same run must now fail the check.
    tampered = replace(
        case,
        expected_facts=tuple(
            {**fact, "value": 10**9} for fact in case.expected_facts
        ),
    )
    passed_after, failures_after = check_expected_facts(run, tampered)
    assert not passed_after and failures_after
    # Restore: the original case still passes.
    assert check_expected_facts(run, case)[0] is True


def test_negative_control_forged_citation_fails_closed(
    synthetic_runtime: ToolRuntime,
) -> None:
    result = synthetic_runtime.call("skill_counts", {"role_family": "backend", "top_n": 3})
    report = check_grounding("The share is 50%.", [result], citations=["tr-forged"])
    assert report.grounded is False
    assert report.invalid_citations == ["tr-forged"]


def test_negative_control_count_cannot_prove_a_percentage() -> None:
    result = ToolResult(
        result_id="tr-unit",
        tool="skill_counts",
        args={},
        rows=[{"posts_with_skill": 1, "total_posts": 1}],
        values={},
        row_count=1,
        truncated=False,
        extractor="baseline",
        taxonomy_version="test",
    )
    confused = check_grounding("The share is 100%.", [result], citations=[result.result_id])
    assert confused.grounded is False
    assert "100%" in confused.unsupported
    # The same number as a plain count is fine.
    fine = check_grounding("The count is 1.", [result], citations=[result.result_id])
    assert fine.grounded is True


def test_negative_control_unsupported_number_fails_grounding() -> None:
    result = ToolResult(
        result_id="tr-count",
        tool="skill_counts",
        args={},
        rows=[{"posts_with_skill": 5, "total_posts": 10}],
        values={},
        row_count=1,
        truncated=False,
        extractor="baseline",
        taxonomy_version="test",
    )
    report = check_grounding("The count is 999.", [result], citations=[result.result_id])
    assert report.grounded is False
    assert any("999" in token for token in report.unsupported)


def test_negative_control_below_thirty_suppression(
    synthetic_runtime: ToolRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    args = {
        "role_family": "mobile",
        "remote": True,
        "month_from": "2019-01-01",
        "month_to": "2019-12-01",
        "top_n": 5,
    }
    suppressed = synthetic_runtime.call("skill_counts", args)
    assert suppressed.rows and suppressed.rows[0]["total_posts"] < 30
    assert suppressed.rows[0]["share"] is None
    assert suppressed.rows[0]["sample_sufficient"] is False
    # Removing the floor changes the answer, proving the floor is what suppresses it.
    monkeypatch.setattr("skillpulse.trends.MIN_SAMPLE_POSTS", 0)
    relaxed = synthetic_runtime.call("skill_counts", args)
    assert any(row["share"] is not None for row in relaxed.rows)
    monkeypatch.undo()
    restored = synthetic_runtime.call("skill_counts", args)
    assert restored.rows[0]["share"] is None


def test_dataset_path_exists() -> None:
    assert Path(DATASET_PATH).is_file()
