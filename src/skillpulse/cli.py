"""Command line interface: ``skillpulse <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

import redis
from sqlalchemy import select

from skillpulse.agent.models import AgentModel, build_agent_model
from skillpulse.config import get_settings
from skillpulse.db import session_scope
from skillpulse.eval.agent_dataset import (
    DATASET_PATH as AGENT_EVAL_DATASET,
)
from skillpulse.eval.agent_dataset import (
    write_agent_dataset,
)
from skillpulse.eval.agent_eval import load_agent_dataset, run_agent_eval
from skillpulse.eval.extraction_eval import load_dataset, run_baseline_eval, run_llm_eval
from skillpulse.eval.fixture import build_fixture_engine
from skillpulse.eval.synthetic_fixture import build_synthetic_fixture_engine
from skillpulse.extraction.baseline import BaselineExtractor
from skillpulse.extraction.llm import LLMUnavailableError
from skillpulse.extraction.providers import build_extractor
from skillpulse.extraction.repository import sync_taxonomy
from skillpulse.extraction.service import extract_post
from skillpulse.extraction.taxonomy import REPO_ROOT, load_taxonomy
from skillpulse.fetch_service import backfill, fetch_thread_by_id, find_hiring_threads
from skillpulse.logging_config import configure_logging
from skillpulse.models import Post
from skillpulse.repository import count_posts
from skillpulse.runtime import LiveFetchDisabledError, build_client, build_session_factory
from skillpulse.trends import refresh_trend_views

logger = logging.getLogger(__name__)


def cmd_threads(args: argparse.Namespace) -> int:
    """List the hiring threads found in the whoishiring submission history."""
    with build_client() as client:
        threads = find_hiring_threads(client, max_scan=args.max_scan)
    print(f"found {len(threads)} hiring threads")
    for thread in threads:
        print(f"  {thread.id}  {thread.month_label}  {thread.title}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """Fetch one thread by id."""
    factory = build_session_factory()
    with build_client() as client:
        result = fetch_thread_by_id(factory, client, args.thread_id)
    print(
        f"thread {result.thread_id}: status={result.status} seen={result.result.seen} "
        f"created={result.result.created} updated={result.result.updated} "
        f"unchanged={result.result.unchanged}"
    )
    with session_scope(factory) as session:
        print(f"posts stored for thread: {count_posts(session, result.thread_id)}")
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """Back-fill hiring threads, newest first."""
    factory = build_session_factory()
    with build_client() as client:
        results = backfill(factory, client, limit=args.limit, max_scan=args.max_scan)
    for result in results:
        print(
            f"thread {result.thread_id}: {result.status} "
            f"seen={result.result.seen} created={result.result.created} "
            f"unchanged={result.result.unchanged}"
        )
    with session_scope(factory) as session:
        print(f"total posts stored: {count_posts(session)}")
    return 0


def cmd_taxonomy(args: argparse.Namespace) -> int:
    """Print the loaded taxonomy summary."""
    taxonomy = load_taxonomy()
    categories: dict[str, int] = {}
    for skill in taxonomy.skills:
        categories[skill.category] = categories.get(skill.category, 0) + 1
    print(
        f"taxonomy {taxonomy.version}: {len(taxonomy.skills)} skills, "
        f"{len(taxonomy.role_families)} role families"
    )
    for category, count in sorted(categories.items()):
        print(f"  {category}: {count}")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    """Run the deterministic baseline over one thread's stored posts (no LLM)."""
    taxonomy = load_taxonomy()
    baseline = BaselineExtractor(taxonomy)
    factory = build_session_factory()
    with session_scope(factory) as session:
        posts = list(session.scalars(select(Post).where(Post.thread_id == args.thread_id)))
        if not posts:
            print(f"no posts stored for thread {args.thread_id}", file=sys.stderr)
            return 1
        sync_taxonomy(session, taxonomy)
        for post in posts:
            extract_post(session, post, taxonomy=taxonomy, baseline=baseline)
    print(f"extracted baseline skills for {len(posts)} posts")
    return 0


def _default_extraction_out(extractor: str) -> Path:
    """Derive the results path, so an LLM run cannot overwrite the baseline file."""
    return REPO_ROOT / "eval" / "results" / f"{extractor}_synthetic.json"


def cmd_eval_extraction(args: argparse.Namespace) -> int:
    """Score one extractor on a labelled set and write a results JSON."""
    settings = get_settings()
    taxonomy = load_taxonomy()
    dataset_path = Path(args.dataset)
    labeled = load_dataset(dataset_path)
    model = ""
    if args.extractor == "llm":
        try:
            # The same builder the workers use, so the provider gate and the shared
            # Redis token budget apply to an evaluation run too.
            extractor = build_extractor(
                taxonomy,
                redis_client=redis.Redis.from_url(settings.redis_url),
                settings=settings,
            )
        except LLMUnavailableError as exc:
            print(f"LLM extractor unavailable: {exc}", file=sys.stderr)
            return 2
        if extractor is None:
            print(
                "SKILLPULSE_LLM_ENABLED is false, so there is no LLM extractor to score; "
                "use --extractor baseline or configure the provider",
                file=sys.stderr,
            )
            return 2
        report = run_llm_eval(labeled, extractor, dataset=dataset_path.name)
        model = settings.llm_model
    else:
        report = run_baseline_eval(taxonomy, labeled, dataset=dataset_path.name)
    payload = {
        "extractor": report.extractor,
        "dataset": report.dataset,
        "source": args.source,
        "model": model,
        "gate": args.gate,
        "report": report.to_json(),
    }
    out_path = Path(args.out) if args.out else _default_extraction_out(report.extractor)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"{report.extractor} on {report.dataset}: micro-F1={report.micro_f1} "
        f"macro-F1={report.macro_f1} required-accuracy={report.required_accuracy} "
        f"unknown-rate={report.unknown_term_rate} -> {out_path}"
    )
    return 0


def _default_agent_out(model_label: str) -> Path:
    """Derive the results path, so a real run cannot overwrite the fake baseline file."""
    suffix = "fake" if model_label == "fake" else "real"
    return REPO_ROOT / "eval" / "results" / f"agent_fixture_{suffix}.json"


def cmd_eval_agent(args: argparse.Namespace) -> int:
    """Score the agent on a frozen fixture database and write a results JSON."""
    settings = get_settings()
    taxonomy = load_taxonomy()
    dataset_path = Path(args.dataset)
    cases, fixture = load_agent_dataset(dataset_path)
    model_label = args.model
    model: AgentModel | None = None
    if args.real_model:
        try:
            model = build_agent_model(
                settings, redis_client=redis.Redis.from_url(settings.redis_url)
            )
        except LLMUnavailableError as exc:
            print(f"agent model unavailable: {exc}", file=sys.stderr)
            return 2
        if model is None:
            print(
                "SKILLPULSE_AGENT_LLM_ENABLED is false, so there is no real model to run",
                file=sys.stderr,
            )
            return 2
        model_label = settings.agent_llm_model
    if args.fixture_profile == "synthetic":
        engine = build_synthetic_fixture_engine()
    else:
        engine = build_fixture_engine()
    try:
        report = run_agent_eval(
            engine,
            taxonomy,
            cases,
            model_label=model_label,
            dataset=dataset_path.name,
            source=args.source,
            fixture=fixture,
            model=model,
        )
    finally:
        engine.dispose()
    if model is None:
        acceptance = "not_real_acceptance" if model_label == "fake" else "unverified"
    else:
        acceptance = "real_model_run"
    payload = {
        "model": report.model,
        "dataset": report.dataset,
        "source": report.source,
        "acceptance": acceptance,
        "fixture_profile": args.fixture_profile,
        "fixture": report.fixture,
        "usage": report.usage,
        "gates": {
            "answer_correctness": report.correctness,
            "grounding_rate": report.grounding_rate,
            "tool_selection_accuracy": report.tool_selection_accuracy,
            "facts_passed": report.facts_passed,
            "facts_checked": report.facts_checked,
            "mean_steps": report.mean_steps,
            "p95_latency_ms": report.p95_latency_ms,
        },
        "gate": args.gate,
        "report": report.to_json(),
    }
    out_path = Path(args.out) if args.out else _default_agent_out(model_label)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"agent fixture [{report.model}/{report.source}]: correctness={report.correctness} "
        f"grounding={report.grounding_rate} tool-selection={report.tool_selection_accuracy} "
        f"facts={report.facts_passed}/{report.facts_checked} ({report.correct}/{report.cases}) "
        f"-> {out_path}"
    )
    return 0


def cmd_build_agent_cases(args: argparse.Namespace) -> int:
    """Regenerate the canonical agent dataset from the synthetic fixture."""
    taxonomy = load_taxonomy()
    engine = build_synthetic_fixture_engine()
    try:
        out = write_agent_dataset(engine, taxonomy, Path(args.out) if args.out else None)
    finally:
        engine.dispose()
    print(f"wrote canonical agent cases -> {out}")
    return 0


def cmd_refresh_trends(args: argparse.Namespace) -> int:
    """Refresh the trend materialised views."""
    factory = build_session_factory()
    refresh_trend_views(factory.kw["bind"], concurrently=not args.no_concurrent)
    print("trend views refreshed")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with every M1 subcommand."""
    parser = argparse.ArgumentParser(prog="skillpulse", description="SkillPulse Agent")
    sub = parser.add_subparsers(dest="command", required=True)

    threads = sub.add_parser("threads", help="list hiring threads found on Hacker News")
    threads.add_argument("--max-scan", type=int, default=600)
    threads.set_defaults(func=cmd_threads)

    fetch = sub.add_parser("fetch", help="fetch one hiring thread by item id")
    fetch.add_argument("thread_id", type=int)
    fetch.set_defaults(func=cmd_fetch)

    backfill_p = sub.add_parser("backfill", help="fetch hiring threads, newest first")
    backfill_p.add_argument("--limit", type=int, default=None, help="max threads to fetch")
    backfill_p.add_argument("--max-scan", type=int, default=600)
    backfill_p.set_defaults(func=cmd_backfill)

    taxonomy_p = sub.add_parser("taxonomy", help="show the loaded skill taxonomy")
    taxonomy_p.set_defaults(func=cmd_taxonomy)

    extract_p = sub.add_parser("extract", help="baseline-extract one thread's stored posts")
    extract_p.add_argument("--thread", dest="thread_id", type=int, required=True)
    extract_p.set_defaults(func=cmd_extract)

    eval_p = sub.add_parser("eval", help="run an evaluation")
    eval_sub = eval_p.add_subparsers(dest="eval_command", required=True)
    eval_extraction = eval_sub.add_parser("extraction", help="score extractors on a labelled set")
    eval_extraction.add_argument(
        "--dataset", default=str(REPO_ROOT / "eval" / "datasets" / "synthetic_extraction.json")
    )
    eval_extraction.add_argument("--extractor", choices=["baseline", "llm"], default="baseline")
    # No default output path: it is derived from the extractor, so an LLM run cannot
    # overwrite the committed baseline result.
    eval_extraction.add_argument("--out", default=None)
    eval_extraction.add_argument("--source", default="synthetic")
    eval_extraction.add_argument(
        "--gate",
        default="real HN post text and a real LLM provider are gated (docs/terms.md, D006)",
    )
    eval_extraction.set_defaults(func=cmd_eval_extraction)

    eval_agent = eval_sub.add_parser("agent", help="score the agent on the fixture database")
    eval_agent.add_argument(
        "--dataset", default=str(REPO_ROOT / "eval" / "datasets" / "agent_cases.json")
    )
    eval_agent.add_argument(
        "--out",
        default=None,
        help="defaults to eval/results/agent_fixture_<fake|real>.json",
    )
    eval_agent.add_argument("--model", default="fake")
    eval_agent.add_argument(
        "--real-model",
        action="store_true",
        help="use the configured provider model instead of the deterministic fake",
    )
    eval_agent.add_argument("--source", default="fixture")
    eval_agent.add_argument(
        "--fixture-profile", choices=["tiny", "synthetic"], default="tiny"
    )
    eval_agent.add_argument(
        "--gate",
        default="fixture DB + fake model only; a real agent model needs "
        "SKILLPULSE_AGENT_LLM_ENABLED and a shared Redis budget (not supplied)",
    )
    eval_agent.set_defaults(func=cmd_eval_agent)

    build_cases = eval_sub.add_parser(
        "build-agent-cases", help="regenerate the canonical agent dataset JSON"
    )
    build_cases.add_argument("--out", default=str(AGENT_EVAL_DATASET))
    build_cases.set_defaults(func=cmd_build_agent_cases)

    refresh_p = sub.add_parser("refresh-trends", help="refresh the trend materialised views")
    refresh_p.add_argument("--no-concurrent", action="store_true")
    refresh_p.set_defaults(func=cmd_refresh_trends)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point used by the console script."""
    settings = get_settings()
    configure_logging(settings.log_level)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except LiveFetchDisabledError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
