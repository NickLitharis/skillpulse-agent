"""LangGraph agent (SPEC section 9): plan -> tools -> draft -> grounding check -> answer.

Bounds are explicit and finite: at most ``max_tool_calls`` tool calls, one grounding
rewrite, and an overall wall-clock deadline. Every node checks a cancellation callback
and the deadline, so a client disconnect or a slow database stops the run at a node
boundary instead of running to completion.

The graph is built with ``langgraph.graph.StateGraph``. It is deterministic under
:class:`~skillpulse.agent.models.FakeAgentModel` (used by CI) and uses a real model only
through :func:`~skillpulse.agent.models.build_agent_model`.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph

from skillpulse.agent.grounding import (
    Claim,
    GroundingReport,
    check_grounding,
    render_claims,
    validate_claims,
)
from skillpulse.agent.models import AgentModel
from skillpulse.extraction.taxonomy import Taxonomy
from skillpulse.tools import MIN_SAMPLE_POSTS, ToolResult, ToolRuntime

logger = logging.getLogger(__name__)

REFUSAL = "That question is outside this job-market dataset, so I cannot answer it."
TOOL_FAILURE = "A data tool failed, so I cannot answer that right now. Please retry."


class AgentCancelled(RuntimeError):
    """The client disconnected, so the run stopped at the next node boundary."""


class AgentState(TypedDict, total=False):
    """The channels the graph reads and writes."""

    question: str
    plan: list[dict[str, Any]]
    clarify: str
    refuse: bool
    infeasible_reason: str
    results: list[ToolResult]
    draft: str
    citations: list[str]
    claims: list[Claim]
    grounded: bool
    unsupported: list[str]
    partial: bool
    rewrites: int
    timed_out: bool
    error: str
    tool_calls_used: int


@dataclass
class AgentRun:
    """The final state of one agent run plus the events the API streams."""

    answer: str = ""
    citations: list[str] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    grounded: bool = False
    unsupported: list[str] = field(default_factory=list)
    partial: bool = False
    refused: bool = False
    ask_back: bool = False
    error: str | None = None
    trace_id: str = ""
    tool_calls_used: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)


def render_table(results: list[ToolResult]) -> str:
    """Render tool rows as plain text with no prose numbers (grounding fallback)."""
    lines = ["Verified tool results (no prose summary):"]
    for result in results:
        lines.append(f"- {result.tool} [{result.result_id}] rows={result.row_count}")
        for row in result.rows[:10]:
            lines.append(f"  {row}")
        for key, value in result.values.items():
            lines.append(f"  {key}={value}")
    return "\n".join(lines)


def run_agent(
    question: str,
    *,
    runtime: ToolRuntime,
    model: AgentModel,
    taxonomy: Taxonomy,
    max_tool_calls: int = 6,
    overall_timeout_seconds: float = 60.0,
    cancel_check: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    trace_id: str | None = None,
) -> AgentRun:
    """Run the bounded agent graph for one question and return the final state."""
    deadline = clock() + overall_timeout_seconds
    events: list[dict[str, Any]] = []
    run = AgentRun(trace_id=trace_id or uuid.uuid4().hex, events=events)
    runtime_specs = runtime.tool_specs

    def stopped() -> bool:
        if cancel_check is not None and cancel_check():
            return True
        return clock() > deadline

    def emit(event: str, data: dict[str, Any]) -> None:
        events.append({"event": event, "data": data})

    def plan_node(state: AgentState) -> AgentState:
        if cancel_check is not None and cancel_check():
            raise AgentCancelled("client disconnected before planning")
        if clock() > deadline:
            return {"timed_out": True, "error": "timeout"}
        plan = model.plan(state["question"], taxonomy, runtime_specs)
        emit(
            "plan",
            {
                "tool_calls": [{"name": call.name, "args": call.args} for call in plan.calls],
                "refuse": plan.refuse,
                "clarify": plan.clarify,
            },
        )
        update: AgentState = {
            "plan": [{"name": call.name, "args": call.args} for call in plan.calls]
        }
        if plan.clarify:
            update["clarify"] = plan.clarify
        if plan.refuse:
            update["refuse"] = True
        if plan.infeasible_reason:
            update["infeasible_reason"] = plan.infeasible_reason
        return update

    def route_after_plan(state: AgentState) -> str:
        if state.get("timed_out") or state.get("refuse") or state.get("clarify") or state.get(
            "infeasible_reason"
        ):
            return "end"
        return "tools"

    def tools_node(state: AgentState) -> AgentState:
        results: list[ToolResult] = []
        used = 0
        for call in state.get("plan", [])[:max_tool_calls]:
            if stopped():
                break
            emit("tool_call", {"name": call["name"], "args": call["args"]})
            used += 1
            try:
                result = runtime.call(call["name"], call["args"])
            except Exception:
                logger.exception("tool %s failed", call["name"])
                return {"results": results, "tool_calls_used": used, "error": "tool_failure",
                        "partial": True}
            emit(
                "tool_result",
                {
                    "result_id": result.result_id,
                    "name": result.tool,
                    "row_count": result.row_count,
                    "truncated": result.truncated,
                },
            )
            results.append(result)
        update: AgentState = {"results": results, "tool_calls_used": used}
        if stopped() and clock() > deadline:
            update["timed_out"] = True
            update["partial"] = True
        return update

    def draft_node(state: AgentState) -> AgentState:
        if stopped():
            return {"draft": "", "partial": True, "timed_out": clock() > deadline}
        is_rewrite = state.get("rewrites", 0) > 0
        draft = model.draft(state["question"], state.get("results", []), rewrite=is_rewrite)
        return {"draft": draft.answer, "citations": draft.citations, "claims": draft.claims}

    def verify_node(state: AgentState) -> AgentState:
        draft = state.get("draft", "")
        results = state.get("results", [])
        if not draft:
            return {"grounded": False, "unsupported": []}
        report: GroundingReport = check_grounding(
            draft,
            results,
            state.get("citations", []),
            claims=state.get("claims", []),
            allowed_numbers=[*_question_numbers(state["question"]), float(MIN_SAMPLE_POSTS)],
        )
        if report.unsupported:
            logger.warning(
                "grounding: unsupported numbers %s in a draft for %s",
                report.unsupported,
                state["question"],
            )
        update: AgentState = {"grounded": report.grounded, "unsupported": report.unsupported}
        if not report.grounded:
            # Count the failed draft so the conditional edge allows exactly one rewrite.
            update["rewrites"] = state.get("rewrites", 0) + 1
        return update

    def route_after_verify(state: AgentState) -> str:
        if not state.get("grounded", False) and state.get("rewrites", 0) < 1:
            return "draft"
        return "end"

    builder = StateGraph(AgentState)
    builder.add_node("plan", plan_node)
    builder.add_node("tools", tools_node)
    builder.add_node("draft", draft_node)
    builder.add_node("verify", verify_node)
    builder.add_edge(START, "plan")
    builder.add_conditional_edges("plan", route_after_plan, {"tools": "tools", "end": END})
    builder.add_edge("tools", "draft")
    builder.add_edge("draft", "verify")
    builder.add_conditional_edges("verify", route_after_verify, {"draft": "draft", "end": END})
    graph = builder.compile()

    try:
        final = cast(
            AgentState,
            graph.invoke(
                {"question": question, "results": [], "rewrites": 0},
                config={"recursion_limit": 25},
            ),
        )
    except AgentCancelled:
        run.error = "cancelled"
        run.partial = True
        run.answer = "The request was cancelled."
        return run

    run.results = final.get("results", [])
    run.tool_calls_used = final.get("tool_calls_used", 0)
    run.unsupported = final.get("unsupported", [])

    if final.get("clarify"):
        run.ask_back = True
        run.answer = final["clarify"]
        run.grounded = True
    elif final.get("refuse"):
        run.refused = True
        run.answer = REFUSAL
        run.grounded = True
    elif final.get("infeasible_reason"):
        run.answer = f"I cannot answer that reliably: {final['infeasible_reason']}."
        run.grounded = True
    elif final.get("error") == "tool_failure" or final.get("error") == "timeout":
        run.error = final.get("error")
        run.partial = True
        run.answer = TOOL_FAILURE if run.results == [] else render_table(run.results)
        run.grounded = bool(run.results)
    elif final.get("grounded") and final.get("draft"):
        run.answer = final["draft"]
        run.citations = final.get("citations", [])
        run.grounded = True
    else:
        # One rewrite already happened and numbers were still unsupported: block the
        # invented numbers, log the defects and render only validated tool fields.
        if run.unsupported:
            logger.warning(
                "grounding defects in trace %s: %s", run.trace_id, run.unsupported
            )
        cited_ids = set(final.get("citations", [])) & {
            result.result_id for result in run.results
        }
        validated, _ = validate_claims(
            final.get("claims", []),
            {result.result_id: result for result in run.results},
            cited_ids,
        )
        if validated:
            run.answer = render_claims(validated)
        else:
            run.answer = render_table(run.results)
        run.citations = [result.result_id for result in run.results]
        run.grounded = bool(validated) or bool(run.results)
        run.partial = True
    if final.get("timed_out"):
        run.partial = True

    emit(
        "done",
        {
            "trace_id": run.trace_id,
            "partial": run.partial,
            "grounded": run.grounded,
        },
    )
    return run


def _question_numbers(question: str) -> list[float]:
    """Numbers a question may legitimately restate (e.g. \"last 12 months\", \"top 5\")."""
    from skillpulse.agent.grounding import _NUMBER, _parse_number

    values: list[float] = []
    for match in _NUMBER.finditer(question):
        value, _ = _parse_number(match.group(0))
        if value == value:
            values.append(value)
    return values
