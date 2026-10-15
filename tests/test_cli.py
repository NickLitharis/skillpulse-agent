"""Tests for the CLI argument parsing (no network, no database)."""

from __future__ import annotations

import pytest

from skillpulse.cli import build_parser


def test_parser_requires_a_command() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_threads_defaults() -> None:
    args = build_parser().parse_args(["threads"])
    assert args.command == "threads"
    assert args.max_scan == 600
    assert args.func.__name__ == "cmd_threads"


def test_fetch_requires_thread_id() -> None:
    args = build_parser().parse_args(["fetch", "49922569"])
    assert args.thread_id == 49922569
    assert args.func.__name__ == "cmd_fetch"


def test_backfill_limit_is_optional() -> None:
    parser = build_parser()
    assert parser.parse_args(["backfill"]).limit is None
    assert parser.parse_args(["backfill", "--limit", "3"]).limit == 3
