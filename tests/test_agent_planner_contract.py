"""The planner must be shown its own argument contract.

Regression guard: ``OpenAIAgentModel.plan`` used to build its prompt from the tool *names
and descriptions* only, dropping the ``args_schema`` the runtime supplies. A real model
therefore had to guess argument names and produced ``skill``/``year``/``location`` — which
the tool layer either rejected or, worse, silently ignored while returning wrong rows.

The provider is never called: a recording double stands in.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from skillpulse.agent.models import OpenAIAgentModel
from skillpulse.extraction.llm import LLMExtractionError
from skillpulse.extraction.taxonomy import load_taxonomy
from skillpulse.tools import TOOL_SPECS
from skillpulse.tools.models import SkillCountsArgs


class RecordingClient:
    """Provider double that records prompts and replays canned replies."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete_json(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_plan_prompt_carries_the_argument_schemas() -> None:
    client = RecordingClient([json.dumps({"tool_calls": []})])
    model = OpenAIAgentModel(client, max_output_tokens=64)

    model.plan("Which skills do backend posts mention in 2020?", load_taxonomy(), TOOL_SPECS)

    prompt = client.prompts[0]
    assert "args_schema" in prompt
    assert '"month_from"' in prompt, "the date window argument must be visible"
    assert "YYYY-MM-DD" in prompt, "the date format must be stated"
    assert '"skill_id"' in prompt, "argument names must be visible, not just descriptions"
    assert "backend" in prompt, "valid role family ids must be listed"


def test_plan_prompt_states_when_to_clarify_and_when_to_refuse() -> None:
    """A question with one usable filter is answerable; only a filterless one is vague."""
    client = RecordingClient([json.dumps({"tool_calls": []})])
    model = OpenAIAgentModel(client, max_output_tokens=64)

    model.plan(
        "Which skills are most requested in remote posts?", load_taxonomy(), TOOL_SPECS
    )

    prompt = client.prompts[0]
    assert "Call a tool whenever the question supplies at least one filter" in prompt
    assert "only when the question supplies no filter" in prompt
    assert '"refuse":true' in prompt, "off-topic questions must refuse, not ask back"


def test_plan_retries_an_empty_reply_once() -> None:
    client = RecordingClient(["", json.dumps({"clarify": "Which role family?"})])
    model = OpenAIAgentModel(client, max_output_tokens=64)

    plan = model.plan("Which skills are hot?", load_taxonomy(), TOOL_SPECS)

    assert plan.clarify == "Which role family?"
    assert len(client.prompts) == 2


def test_plan_raises_after_two_invalid_replies() -> None:
    model = OpenAIAgentModel(RecordingClient(["", "not json"]), max_output_tokens=64)

    with pytest.raises(LLMExtractionError):
        model.plan("Which skills are hot?", load_taxonomy(), TOOL_SPECS)


def test_invented_arguments_are_rejected_rather_than_ignored() -> None:
    with pytest.raises(ValidationError):
        SkillCountsArgs(**{"year": 2021, "role_family": "backend"})

    args = SkillCountsArgs(role_family="backend", month_from="2021-01-01")
    assert args.role_family == "backend"


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_calls": [{"args": {}}]},
        {"tool_calls": [{"name": 7}]},
        {"tool_calls": ["skill_counts"]},
        {"tool_calls": {"name": "skill_counts"}},
        {"tool_calls": [{"name": "skill_counts", "args": []}]},
    ],
)
def test_malformed_planned_tool_calls_are_rejected(payload: dict[str, object]) -> None:
    client = RecordingClient([json.dumps(payload)])
    model = OpenAIAgentModel(client, max_output_tokens=64)
    with pytest.raises(LLMExtractionError):
        model.plan("Which skills are hot?", load_taxonomy(), TOOL_SPECS)
