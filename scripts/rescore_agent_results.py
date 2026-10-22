#!/usr/bin/env python3
"""Rescore committed agent-eval JSON without calling a model.

Two rules changed after the first real run, so stored results need the corrected
arithmetic:

* a run with ``error`` set is never correct, even when its fallback text says "cannot";
* a fallback (``partial``) run is not a grounded draft, so S4 does not count it.

The script updates the per-result flags and the headline gates in place. It is offline:
it reads only the committed JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEFAULT_PATHS = (
    Path("eval/results/agent_fixture_real.json"),
    Path("eval/results/agent_fixture_synthetic.json"),
    Path("eval/results/agent_fixture_fake.json"),
)


def rescore(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    report = payload.get("report", payload)
    results = report["results"]
    for result in results:
        if result.get("error") is not None:
            result["correct"] = False
        if result.get("error") is not None or result.get("partial"):
            result["grounded"] = False

    total = len(results)
    correct = sum(1 for result in results if result["correct"])
    grounded = sum(1 for result in results if result["grounded"])
    drafts = [
        result for result in results if result.get("error") is None and not result.get("partial")
    ]
    draft_grounded = sum(1 for result in drafts if result["grounded"])
    report["correct"] = correct
    report["correctness"] = round(correct / total, 4) if total else 0.0
    report["grounding_rate"] = round(grounded / total, 4) if total else 0.0
    report["draft_grounded_rate"] = (
        round(draft_grounded / len(drafts), 4) if drafts else 0.0
    )
    if "gates" in payload:
        payload["gates"]["answer_correctness"] = report["correctness"]
        payload["gates"]["grounding_rate"] = report["grounding_rate"]
        payload["gates"]["draft_grounded_rate"] = report["draft_grounded_rate"]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"{path}: {correct}/{total} correct ({report['correctness']}), "
        f"{grounded}/{total} grounded ({report['grounding_rate']}), "
        f"drafts {draft_grounded}/{len(drafts)} ({report['draft_grounded_rate']})"
    )
    return report


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv] or list(DEFAULT_PATHS)
    for path in paths:
        rescore(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
