"""Agent evaluation on the frozen fixture databases (SPEC section 11).

This runner proves the pipeline is honest, not that a real model is accurate. Every case
here runs :class:`~skillpulse.agent.models.FakeAgentModel` unless it names an adversarial
model, and the result records ``model="fake"`` (and the dataset ``source``) so a fixture
run can never be quoted as the S3/S4 real-model acceptance number.

Two datasets are supported:

* the tiny smoke set in ``eval/datasets/agent_cases.json`` (behaviour only, no facts),
* the canonical set in ``eval/datasets/agent_eval_cases.json`` built by
  :mod:`skillpulse.eval.agent_dataset`, whose ``expected_facts`` were computed by
  independent SQL and are checked against the tool results here.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from skillpulse.agent.graph import AgentRun, run_agent
from skillpulse.agent.models import AgentModel, DraftResult, FakeAgentModel
from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.tools import ToolResult, ToolRuntime

logger = logging.getLogger(__name__)


class InventingAgentModel(FakeAgentModel):
    """A model that always adds an unsupported number, to test the grounding gate."""

    name = "fake-inventing"

    def draft(
        self, question: str, results: list[ToolResult], *, rewrite: bool = False
    ) -> DraftResult:
        base = super().draft(question, results, rewrite=rewrite)
        return DraftResult(
            answer=f"{base.answer} Also, 999 posts used it.",
            citations=base.citations,
            claims=base.claims,
        )


class FailingToolRuntime(ToolRuntime):
    """A runtime whose tools always fail, to test the safe-failure path."""

    def call(self, tool: str, args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("simulated database failure")


@dataclass(frozen=True)
class AgentCase:
    """One fixture question and what a correct, grounded answer must do."""

    id: str
    question: str
    category: str = ""
    expected_tools: tuple[str, ...] = ()
    expected_facts: tuple[dict[str, Any], ...] = ()
    must_refuse: bool = False
    must_ask_back: bool = False
    must_state_insufficient: bool = False
    forbidden_numbers: tuple[str, ...] = ()
    simulate: str | None = None
    adversarial_model: str | None = None


@dataclass(frozen=True)
class AgentCaseResult:
    """The outcome of one case."""

    id: str
    question: str
    category: str
    correct: bool
    grounded: bool
    used_tools: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    answer: str = ""
    error: str | None = None
    partial: bool = False
    unsupported: list[str] = field(default_factory=list)
    facts_checked: int = 0
    facts_passed: int = 0
    fact_failures: list[str] = field(default_factory=list)
    steps: int = 0
    latency_ms: float = 0.0


@dataclass(frozen=True)
class AgentEvalReport:
    """Aggregate scores for one model over one dataset."""

    model: str
    dataset: str
    source: str
    cases: int
    correct: int
    correctness: float
    grounding_rate: float
    tool_selection_accuracy: float
    results: list[AgentCaseResult]
    fixture: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    facts_checked: int = 0
    facts_passed: int = 0
    mean_steps: float = 0.0
    p95_latency_ms: float = 0.0

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["results"] = [asdict(result) for result in self.results]
        return payload


def _as_dicts(raw: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(entry for entry in raw if isinstance(entry, dict))


def load_agent_cases(path: Path) -> list[AgentCase]:
    """Load the agent evaluation cases from JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[AgentCase] = []
    for entry in raw["cases"]:
        cases.append(
            AgentCase(
                id=str(entry["id"]),
                question=str(entry["question"]),
                category=str(entry.get("category", "")),
                expected_tools=tuple(entry.get("expected_tools") or ()),
                expected_facts=_as_dicts(entry.get("expected_facts")),
                must_refuse=bool(entry.get("must_refuse", False)),
                must_ask_back=bool(entry.get("must_ask_back", False)),
                must_state_insufficient=bool(entry.get("must_state_insufficient", False)),
                forbidden_numbers=tuple(entry.get("forbidden_numbers") or ()),
                simulate=entry.get("simulate"),
                adversarial_model=entry.get("adversarial_model"),
            )
        )
    return cases


def load_agent_dataset(path: Path) -> tuple[list[AgentCase], dict[str, Any]]:
    """Load cases plus the frozen-fixture metadata a canonical dataset carries."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    fixture = raw.get("fixture")
    return load_agent_cases(path), fixture if isinstance(fixture, dict) else {}


def _used_tools(run: AgentRun) -> list[str]:
    return [
        str(event["data"]["name"])
        for event in run.events
        if event["event"] == "tool_call"
    ]


def _case_model(case: AgentCase) -> AgentModel:
    if case.adversarial_model == "invents":
        return InventingAgentModel()
    return FakeAgentModel()


def _resolve_path(result: ToolResult, path: str) -> Any:
    node: Any = result.model_dump(mode="json")
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return None
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (IndexError, ValueError):
                return None
        else:
            return None
    return node


def _check_fact(result: ToolResult, fact: dict[str, Any]) -> bool:
    """Check one expected fact against the exact field of the named tool result."""
    kind = fact.get("kind")
    if kind == "field":
        value = _resolve_path(result, str(fact.get("path", "")))
        expected = fact.get("value")
        if expected is None or value is None:
            return value is None and expected is None
        if isinstance(expected, bool) or isinstance(value, bool):
            return bool(value) is bool(expected)
        if isinstance(expected, int | float) and isinstance(value, int | float):
            tolerance = max(float(fact.get("tolerance", 0.0)), 1e-9)
            return abs(float(value) - float(expected)) <= tolerance
        return bool(value == expected)
    if kind == "all_rows":
        field_name = str(fact.get("field", ""))
        return bool(result.rows) and all(
            row.get(field_name) == fact.get("equals") for row in result.rows
        )
    if kind == "min_rows":
        return result.row_count >= int(fact.get("value", 0))
    if kind == "nonempty":
        return bool(_resolve_path(result, str(fact.get("path", ""))))
    return False


def check_expected_facts(run: AgentRun, case: AgentCase) -> tuple[bool, list[str]]:
    """Return ``(all_passed, failures)`` for the case's expected facts.

    Each fact names the tool whose result must carry it; a fact with no matching tool
    result fails, so a case cannot pass by skipping the tool it is meant to exercise.
    """
    failures: list[str] = []
    results_by_tool = {result.tool: result for result in run.results}
    for fact in case.expected_facts:
        tool = str(fact.get("tool", ""))
        result = results_by_tool.get(tool)
        if result is None:
            failures.append(f"{case.id}: no {tool} result")
            continue
        if not _check_fact(result, fact):
            failures.append(f"{case.id}: {tool}:{fact.get('path') or fact.get('kind')}")
    return not failures, failures


def _insufficient_answer(answer: str) -> bool:
    lowered = answer.lower()
    return (
        "cannot" in lowered
        or "too small" in lowered
        or "below 30" in lowered
        or "sample is below" in lowered
    )


def _is_correct(case: AgentCase, run: AgentRun, *, adversarial_fake: bool = False) -> bool:
    if case.simulate == "cancel":
        return run.error == "cancelled"
    if case.simulate == "tool_failure":
        return run.error == "tool_failure" and run.partial
    if case.must_refuse:
        return run.refused
    if case.must_ask_back:
        return run.ask_back
    if case.must_state_insufficient:
        return _insufficient_answer(run.answer)
    if any(number in run.answer for number in case.forbidden_numbers):
        return False
    # The "invents" oracle needs the deterministic inventing model: it passes only when the
    # forbidden number was produced and then stripped by the grounding gate. A real model is
    # not told to invent anything, so for it the honest test is the normal one above (the
    # number must not appear in the answer) plus the usual tool and fact checks below.
    if case.adversarial_model == "invents" and adversarial_fake:
        return run.grounded and bool(case.forbidden_numbers) and all(
            number in run.unsupported for number in case.forbidden_numbers
        )
    if not run.grounded:
        return False
    if not set(case.expected_tools) <= set(_used_tools(run)):
        return False
    if case.expected_facts:
        passed, _ = check_expected_facts(run, case)
        if not passed:
            return False
    return True


def _usage_for(model_label: str) -> dict[str, Any]:
    """Provider usage when it exists, otherwise null with a reason (never invented)."""
    if model_label == "fake":
        reason = "deterministic fake model; no provider usage is produced"
    else:
        reason = "this runner does not capture provider usage; record it from the provider"
    return {
        "provider": model_label,
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "reason": reason,
    }


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[index], 3)


def run_agent_eval(
    engine: Engine,
    taxonomy: Taxonomy,
    cases: list[AgentCase],
    *,
    model_label: str,
    dataset: str,
    source: str = "fixture",
    fixture: dict[str, Any] | None = None,
    model: AgentModel | None = None,
) -> AgentEvalReport:
    """Run every case against the fixture database and score the outcomes.

    ``model`` runs the real agent model (``build_agent_model``). When it is omitted the
    deterministic fake model is used per case, which is what CI does. A real model cannot
    be told to invent a number, so for a real run the adversarial "invents" cases are scored
    on the normal criteria — the forbidden number must not appear in the answer, and the
    answer must still be grounded, use the expected tools and pass its facts — instead of
    through :class:`InventingAgentModel`.
    """
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    results: list[AgentCaseResult] = []
    correct = 0
    facts_checked = 0
    facts_passed = 0
    latencies: list[float] = []
    for case in cases:
        runtime: ToolRuntime = (
            FailingToolRuntime(factory, taxonomy)
            if case.simulate == "tool_failure"
            else ToolRuntime(factory, taxonomy)
        )
        cancel_check = (lambda: True) if case.simulate == "cancel" else None
        started = time.perf_counter()
        try:
            run = run_agent(
                case.question,
                runtime=runtime,
                model=model if model is not None else _case_model(case),
                taxonomy=taxonomy,
                cancel_check=cancel_check,
            )
        except Exception as exc:
            # A live model can fail on one question: a provider error, or an empty reply
            # that survives the retry. Record the case as incorrect and keep going. A
            # 64-case run must not lose 63 results to one transient failure.
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            latencies.append(latency_ms)
            logger.warning("agent case %s raised %s: %s", case.id, type(exc).__name__, exc)
            facts_checked += len(case.expected_facts)
            results.append(
                AgentCaseResult(
                    id=case.id,
                    question=case.question,
                    category=case.category,
                    correct=False,
                    grounded=False,
                    expected_tools=list(case.expected_tools),
                    error=f"case_error:{type(exc).__name__}",
                    facts_checked=len(case.expected_facts),
                    fact_failures=[f"{case.id}: raised {type(exc).__name__}"],
                    latency_ms=latency_ms,
                )
            )
            continue
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        latencies.append(latency_ms)
        _, failures = (
            check_expected_facts(run, case) if case.expected_facts else (True, [])
        )
        facts_checked += len(case.expected_facts)
        facts_passed += len(case.expected_facts) - len(failures)
        correct_case = _is_correct(case, run, adversarial_fake=model is None)
        correct += int(correct_case)
        results.append(
            AgentCaseResult(
                id=case.id,
                question=case.question,
                category=case.category,
                correct=correct_case,
                grounded=run.grounded,
                used_tools=_used_tools(run),
                expected_tools=list(case.expected_tools),
                answer=run.answer,
                error=run.error,
                partial=run.partial,
                unsupported=run.unsupported,
                facts_checked=len(case.expected_facts),
                facts_passed=len(case.expected_facts) - len(failures),
                fact_failures=failures,
                steps=run.tool_calls_used,
                latency_ms=latency_ms,
            )
        )
    tool_cases = [case for case in cases if case.expected_tools]
    tool_hits = sum(
        1
        for case, result in zip(cases, results, strict=True)
        if case.expected_tools and set(case.expected_tools) <= set(result.used_tools)
    )
    total = len(cases)
    # Grounding is only defined for runs that produced an answer; cancellation and a
    # tool failure have no answer to ground and are scored by correctness instead.
    answer_results = [result for result in results if result.error is None]
    return AgentEvalReport(
        model=model_label,
        dataset=dataset,
        source=source,
        cases=total,
        correct=correct,
        correctness=round(correct / total, 4) if total else 0.0,
        grounding_rate=round(
            sum(1 for result in answer_results if result.grounded) / len(answer_results), 4
        )
        if answer_results
        else 0.0,
        tool_selection_accuracy=round(tool_hits / len(tool_cases), 4) if tool_cases else 0.0,
        results=results,
        fixture=fixture or {},
        usage=_usage_for(model_label),
        facts_checked=facts_checked,
        facts_passed=facts_passed,
        mean_steps=round(sum(result.steps for result in results) / total, 3) if total else 0.0,
        p95_latency_ms=_p95(latencies),
    )
