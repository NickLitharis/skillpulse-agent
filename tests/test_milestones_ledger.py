"""The milestone ledger stays machine-readable and honest about gates."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER = REPO_ROOT / "docs" / "milestones.json"
ALLOWED = {"complete", "partial", "blocked", "not_started"}


def _ledger() -> dict[str, object]:
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def test_ledger_covers_every_milestone() -> None:
    data = _ledger()
    ids = [milestone["id"] for milestone in data["milestones"]]
    assert ids == [f"M{n}" for n in range(1, 8)]


def test_every_status_is_valid_and_every_milestone_names_its_gates() -> None:
    data = _ledger()
    for milestone in data["milestones"]:
        assert milestone["status"] in ALLOWED, milestone["id"]
        assert milestone["evidence"], milestone["id"]
        assert milestone["remaining_gates"], milestone["id"]
        assert milestone["spec_acceptance"], milestone["id"]


def test_m3_is_honest_about_synthetic_vs_real() -> None:
    data = _ledger()
    m3 = next(milestone for milestone in data["milestones"] if milestone["id"] == "M3")
    assert m3["status"] == "partial"
    gates = " ".join(m3["remaining_gates"]).lower()
    assert "real" in gates
    evidence = " ".join(m3["evidence"]).lower()
    assert "synthetic" in evidence
    assert "336 skills" in evidence
