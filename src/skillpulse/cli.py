"""Command line interface: ``skillpulse <command>``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select

from skillpulse.config import get_settings
from skillpulse.db import session_scope
from skillpulse.eval.extraction_eval import load_dataset, run_baseline_eval
from skillpulse.extraction.baseline import BaselineExtractor
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


def cmd_eval_extraction(args: argparse.Namespace) -> int:
    """Score the baseline on a labelled set and write a results JSON."""
    taxonomy = load_taxonomy()
    dataset_path = Path(args.dataset)
    labeled = load_dataset(dataset_path)
    report = run_baseline_eval(taxonomy, labeled, dataset=dataset_path.name)
    payload = {
        "extractor": report.extractor,
        "dataset": report.dataset,
        "source": args.source,
        "gate": args.gate,
        "report": report.to_json(),
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        f"{report.extractor} on {report.dataset}: micro-F1={report.micro_f1} "
        f"macro-F1={report.macro_f1} required-accuracy={report.required_accuracy} "
        f"unknown-rate={report.unknown_term_rate} -> {out_path}"
    )
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
    eval_extraction.add_argument(
        "--out", default=str(REPO_ROOT / "eval" / "results" / "baseline_synthetic.json")
    )
    eval_extraction.add_argument("--source", default="synthetic")
    eval_extraction.add_argument(
        "--gate",
        default="real HN post text and a real LLM provider are gated (docs/terms.md, D006)",
    )
    eval_extraction.set_defaults(func=cmd_eval_extraction)

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
