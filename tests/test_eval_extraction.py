"""Extraction evaluation: scoring maths and the shipped labelled set."""

from __future__ import annotations

from pathlib import Path

from skillpulse.eval.extraction_eval import (
    LabeledPost,
    load_dataset,
    run_baseline_eval,
    score,
)
from skillpulse.extraction.schema import PostExtraction, SkillMatch
from skillpulse.extraction.taxonomy import REPO_ROOT, load_taxonomy

DATASET = REPO_ROOT / "eval" / "datasets" / "synthetic_extraction.json"


def test_shipped_dataset_is_well_formed() -> None:
    labeled = load_dataset(DATASET)
    assert len(labeled) == 40
    assert len({post.id for post in labeled}) == len(labeled)
    assert all(post.text for post in labeled)
    assert all(post.skills for post in labeled)


def test_baseline_f1_on_the_synthetic_set() -> None:
    taxonomy = load_taxonomy()
    report = run_baseline_eval(taxonomy, load_dataset(DATASET), dataset=DATASET.name)
    # The baseline is reported next to the LLM; the synthetic set must show that the
    # deterministic matcher is genuinely working, not that it is perfect.
    assert report.micro_f1 >= 0.85
    assert report.macro_f1 >= 0.85
    assert report.required_accuracy is not None and report.required_accuracy >= 0.9
    assert report.unknown_term_rate == 0.0
    assert report.posts == 40
    assert report.false_positives or report.false_negatives  # not artificially perfect


def test_micro_and_macro_scoring_maths() -> None:
    labeled = [
        LabeledPost(id="a", text="x", skills={"python": True, "docker": True}),
        LabeledPost(id="b", text="y", skills={"react": True}),
    ]
    predictions = {
        "a": PostExtraction(
            skills=[SkillMatch(id="python"), SkillMatch(id="rust")],
            extractor="baseline",
            taxonomy_version="t",
        ),
        "b": PostExtraction(
            skills=[SkillMatch(id="react")], extractor="baseline", taxonomy_version="t"
        ),
    }
    report = score(predictions, labeled, extractor="baseline", dataset="unit")
    # tp=2, fp=1, fn=1 -> precision 2/3, recall 2/3, micro F1 2/3.
    assert report.micro_precision == 0.6667
    assert report.micro_recall == 0.6667
    assert report.micro_f1 == 0.6667
    assert report.false_positives == ["a:rust"]
    assert report.false_negatives == ["a:docker"]


def test_missing_prediction_counts_as_recall_loss() -> None:
    labeled = [LabeledPost(id="a", text="x", skills={"python": True})]
    report = score({}, labeled, extractor="llm", dataset="unit")
    assert report.micro_recall == 0.0
    assert report.false_negatives == ["a:python"]


def test_dataset_resolves_under_repo_root() -> None:
    assert DATASET.exists() and Path(DATASET).is_file()
