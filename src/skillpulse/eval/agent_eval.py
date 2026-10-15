"""Agent evaluation on the frozen fixture database (SPEC section 11).

This runner proves the pipeline is honest, not that a real model is accurate. Every case
here runs :class:`~skillpulse.agent.models.FakeAgentModel` unless it names an adversarial
model, and the result records ``model="fake"`` so a fixture run can never be quoted as the
S3/S4 real-model acceptance number.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from skillpulse.agent.graph import AgentRun, run_agent
from skillpulse.agent.models import AgentModel, DraftResult, FakeAgentModel
from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.tools import ToolResult, ToolRuntime


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
    expected_tools: tuple[str, ...] = ()
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
    correct: bool
    grounded: bool
    used_tools: list[str] = field(default_factory=list)
    expected_tools: list[str] = field(default_factory=list)
    answer: str = ""
    error: str | None = None
    partial: bool = False
    unsupported: list[str] = field(default_factory=list)


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

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["results"] = [asdict(result) for result in self.results]
        return payload


def load_agent_cases(path: Path) -> list[AgentCase]:
    """Load the agent evaluation cases from JSON."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    cases: list[AgentCase] = []
    for entry in raw["cases"]:
        cases.append(
            AgentCase(
                id=str(entry["id"]),
                question=str(entry["question"]),
                expected_tools=tuple(entry.get("expected_tools") or ()),
                must_refuse=bool(entry.get("must_refuse", False)),
                must_ask_back=bool(entry.get("must_ask_back", False)),
                must_state_insufficient=bool(entry.get("must_state_insufficient", False)),
                forbidden_numbers=tuple(entry.get("forbidden_numbers") or ()),
                simulate=entry.get("simulate"),
                adversarial_model=entry.get("adversarial_model"),
            )
        )
    return cases


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


def _is_correct(case: AgentCase, run: AgentRun) -> bool:
    answer = run.answer.lower()
    if case.simulate == "cancel":
        return run.error == "cancelled"
    if case.simulate == "tool_failure":
        return run.error == "tool_failure" and run.partial
    if case.must_refuse:
        return run.refused
    if case.must_ask_back:
        return run.ask_back
    if case.must_state_insufficient:
        return "cannot" in answer or "sample is too small" in answer
    if any(number in run.answer for number in case.forbidden_numbers):
        return False
    if case.adversarial_model == "invents":
        return run.grounded and bool(case.forbidden_numbers) and all(
            number in run.unsupported for number in case.forbidden_numbers
        )
    if not run.grounded:
        return False
    return set(case.expected_tools) <= set(_used_tools(run))


def run_agent_eval(
    engine: Engine,
    taxonomy: Taxonomy,
    cases: list[AgentCase],
    *,
    model_label: str,
    dataset: str,
    source: str = "fixture",
) -> AgentEvalReport:
    """Run every case against the fixture database and score the outcomes."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    results: list[AgentCaseResult] = []
    correct = 0
    for case in cases:
        runtime: ToolRuntime = (
            FailingToolRuntime(factory, taxonomy)
            if case.simulate == "tool_failure"
            else ToolRuntime(factory, taxonomy)
        )
        cancel_check = (lambda: True) if case.simulate == "cancel" else None
        run = run_agent(
            case.question,
            runtime=runtime,
            model=_case_model(case),
            taxonomy=taxonomy,
            cancel_check=cancel_check,
        )
        correct_case = _is_correct(case, run)
        correct += int(correct_case)
        results.append(
            AgentCaseResult(
                id=case.id,
                question=case.question,
                correct=correct_case,
                grounded=run.grounded,
                used_tools=_used_tools(run),
                expected_tools=list(case.expected_tools),
                answer=run.answer,
                error=run.error,
                partial=run.partial,
                unsupported=run.unsupported,
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
    )
