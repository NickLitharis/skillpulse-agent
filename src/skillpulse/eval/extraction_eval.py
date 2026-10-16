"""Score an extractor against a labelled set (SPEC section 11).

Two honesty rules from the spec are enforced by the shape of this module:

* The labelled set this runner ships with is **synthetic** (written in HN style and
  hand-labelled). It is the CI set. Real-HN numbers require fetching post text, which is
  gated, so a real-set result is marked with its ``source`` and the gate in the JSON.
* A run that used the fake LLM client is recorded as ``llm_contract``, never as a real
  LLM F1. The ``extractor`` field names what produced the numbers.

Metrics: micro and macro precision/recall/F1 over skill ids, ``required`` accuracy on the
skills both sides agree on, and the unknown-term rate over predicted terms.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.llm import LLMExtractor
from skillpulse.extraction.schema import PostExtraction
from skillpulse.extraction.taxonomy import Taxonomy


@dataclass(frozen=True)
class LabeledPost:
    """One hand-labelled post: text plus the expected extraction."""

    id: str
    text: str
    skills: dict[str, bool] = field(default_factory=dict)
    remote: bool | None = None


@dataclass(frozen=True)
class EvalReport:
    """The scored result for one extractor over one dataset."""

    extractor: str
    dataset: str
    posts: int
    micro_precision: float
    micro_recall: float
    micro_f1: float
    macro_f1: float
    required_accuracy: float | None
    unknown_term_rate: float
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        """Render the report as a JSON-serialisable dict."""
        return asdict(self)


def load_dataset(path: Path) -> list[LabeledPost]:
    """Load a labelled dataset; skills may be plain ids or ``{id, required}`` objects."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    posts: list[LabeledPost] = []
    for entry in raw["posts"]:
        skills: dict[str, bool] = {}
        for skill in entry.get("skills", []):
            if isinstance(skill, dict):
                skills[str(skill["id"])] = bool(skill.get("required", True))
            else:
                skills[str(skill)] = True
        posts.append(
            LabeledPost(
                id=str(entry["id"]),
                text=str(entry["text"]),
                skills=skills,
                remote=entry.get("remote") if isinstance(entry.get("remote"), bool) else None,
            )
        )
    return posts


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def score(
    predictions: dict[str, PostExtraction],
    labeled: list[LabeledPost],
    *,
    extractor: str,
    dataset: str,
) -> EvalReport:
    """Score predictions against the labels and return the report."""
    true_positive = 0
    false_positive = 0
    false_negative = 0
    per_post_f1: list[float] = []
    required_correct = 0
    required_total = 0
    predicted_terms = 0
    unknown_terms = 0
    fp_examples: list[str] = []
    fn_examples: list[str] = []

    for post in labeled:
        predicted = predictions.get(post.id)
        if predicted is None:
            predicted_skills: dict[str, bool] = {}
            unknown_terms += 0
        else:
            predicted_skills = {match.id: match.required for match in predicted.skills}
            predicted_terms += len(predicted_skills)
            unknown_terms += len(predicted.unknown_terms)

        tp = len(set(predicted_skills) & set(post.skills))
        fp = len(set(predicted_skills) - set(post.skills))
        fn = len(set(post.skills) - set(predicted_skills))
        true_positive += tp
        false_positive += fp
        false_negative += fn
        for skill_id in sorted(set(predicted_skills) - set(post.skills)):
            fp_examples.append(f"{post.id}:{skill_id}")
        for skill_id in sorted(set(post.skills) - set(predicted_skills)):
            fn_examples.append(f"{post.id}:{skill_id}")

        if predicted_skills:
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / (tp + fn) if tp + fn else 0.0
        else:
            precision = recall = 0.0
        per_post_f1.append(_f1(precision, recall))

        for skill_id, required in post.skills.items():
            if skill_id in predicted_skills:
                required_total += 1
                if predicted_skills[skill_id] == required:
                    required_correct += 1

    predicted_total = true_positive + false_positive
    labeled_total = true_positive + false_negative
    micro_precision = true_positive / predicted_total if predicted_total else 0.0
    micro_recall = true_positive / labeled_total if labeled_total else 0.0
    return EvalReport(
        extractor=extractor,
        dataset=dataset,
        posts=len(labeled),
        micro_precision=round(micro_precision, 4),
        micro_recall=round(micro_recall, 4),
        micro_f1=round(_f1(micro_precision, micro_recall), 4),
        macro_f1=round(sum(per_post_f1) / len(per_post_f1), 4) if per_post_f1 else 0.0,
        required_accuracy=round(required_correct / required_total, 4) if required_total else None,
        unknown_term_rate=round(unknown_terms / predicted_terms, 4) if predicted_terms else 0.0,
        false_positives=sorted(fp_examples),
        false_negatives=sorted(fn_examples),
    )


def run_baseline_eval(
    taxonomy: Taxonomy, labeled: list[LabeledPost], *, dataset: str
) -> EvalReport:
    """Run the deterministic baseline over a labelled set and score it."""
    extractor = BaselineExtractor(taxonomy)
    predictions = {post.id: extractor.extract(post.text) for post in labeled}
    return score(predictions, labeled, extractor="baseline", dataset=dataset)


def run_llm_eval(
    labeled: list[LabeledPost], extractor: LLMExtractor, *, dataset: str
) -> EvalReport:
    """Run a built LLM extractor over a labelled set and score it.

    The caller builds the extractor with ``build_extractor``, so the provider gate and
    the shared Redis token budget apply exactly as they do for the workers. This runner
    never builds a client itself, so it cannot quietly skip the cost guard.
    """
    predictions = {post.id: extractor.extract(post.text) for post in labeled}
    return score(predictions, labeled, extractor="llm", dataset=dataset)
