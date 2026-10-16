"""The evaluation commands can run a real model, and fail closed when it is off.

The provider is never called: a fake extractor stands in, so these tests prove the
wiring (gate, recorded model, output path) without a credential or a network.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from skillpulse import cli
from skillpulse.config import Settings
from skillpulse.extraction.llm import LLMExtractor
from skillpulse.extraction.taxonomy import REPO_ROOT

EXTRACTION_DATASET = REPO_ROOT / "eval" / "datasets" / "synthetic_extraction.json"


class FixedClient:
    """Provider double that always returns one valid extraction."""

    def complete_json(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "company": "Acme",
                "role_family": "backend",
                "seniority": None,
                "remote": True,
                "countries": ["US"],
                "skills": [{"id": "python", "required": True}],
            }
        )


def extraction_args(out: Path) -> argparse.Namespace:
    return argparse.Namespace(
        extractor="llm",
        dataset=str(EXTRACTION_DATASET),
        out=str(out),
        source="synthetic",
        gate="unit",
    )


def test_extraction_llm_run_records_the_model(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "llm.json"
    settings = Settings(llm_enabled=True, llm_model="unit-model")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(
        cli,
        "build_extractor",
        lambda taxonomy, **kwargs: LLMExtractor(taxonomy, FixedClient()),
    )
    assert cli.cmd_eval_extraction(extraction_args(out)) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["extractor"] == "llm"
    assert payload["model"] == "unit-model"


def test_extraction_llm_run_fails_closed_when_the_gate_is_off(
    monkeypatch, tmp_path: Path
) -> None:
    out = tmp_path / "never.json"
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(llm_enabled=False))
    assert cli.cmd_eval_extraction(extraction_args(out)) == 2
    assert not out.exists()


def test_default_output_paths_keep_the_committed_files() -> None:
    assert cli._default_extraction_out("baseline").name == "baseline_synthetic.json"
    assert cli._default_extraction_out("llm").name == "llm_synthetic.json"
    assert cli._default_agent_out("fake").name == "agent_fixture_fake.json"
    assert cli._default_agent_out("any-real-model").name == "agent_fixture_real.json"


def test_agent_real_model_fails_closed_when_the_gate_is_off(monkeypatch, tmp_path: Path) -> None:
    out = tmp_path / "agent.json"
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(agent_llm_enabled=False))
    args = argparse.Namespace(
        dataset=str(cli.AGENT_EVAL_DATASET),
        out=str(out),
        model="fake",
        source="fixture",
        fixture_profile="tiny",
        gate="unit",
        real_model=True,
    )
    assert cli.cmd_eval_agent(args) == 2
    assert not out.exists()
