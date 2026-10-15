"""Command line interface: ``skillpulse <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from skillpulse.config import get_settings
from skillpulse.db import session_scope
from skillpulse.fetch_service import backfill, fetch_thread_by_id, find_hiring_threads
from skillpulse.logging_config import configure_logging
from skillpulse.repository import count_posts
from skillpulse.runtime import LiveFetchDisabledError, build_client, build_session_factory

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
