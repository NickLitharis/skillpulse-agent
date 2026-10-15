"""Tests for HTML cleaning, PII stripping and thread-title parsing."""

from __future__ import annotations

from datetime import UTC, datetime

from skillpulse.text_cleaning import (
    clean_comment_text,
    is_hiring_thread_title,
    parse_thread_month,
    strip_html,
    strip_pii,
)


def test_strip_html_resolves_entities_and_paragraphs() -> None:
    raw = "<p>Acme Corp | Backend Engineer | Remote</p><p>Python &amp; Go</p>"
    assert strip_html(raw) == "Acme Corp | Backend Engineer | Remote\nPython & Go"


def test_strip_html_turns_br_into_newline() -> None:
    assert strip_html("Line one<br/>Line two") == "Line one\nLine two"


def test_clean_comment_text_strips_email_and_phone() -> None:
    raw = "<p>Contact jane.doe@acme.com or call 415-555-0199 for details.</p>"
    cleaned = clean_comment_text(raw)
    assert "jane.doe@acme.com" not in cleaned
    assert "415-555-0199" not in cleaned
    assert cleaned.count("[redacted]") == 2


def test_strip_pii_keeps_ordinary_numbers() -> None:
    # Version-like and metric-like numbers must survive.
    assert strip_pii("We use Python 3.12 and raised $10M in 2024.") == (
        "We use Python 3.12 and raised $10M in 2024."
    )


def test_clean_comment_text_handles_none() -> None:
    assert clean_comment_text(None) == ""


def test_parse_thread_month_from_title() -> None:
    month = parse_thread_month("Ask HN: Who is hiring? (October 2026)")
    assert month == datetime(2026, 10, 1, tzinfo=UTC)


def test_parse_thread_month_case_insensitive() -> None:
    assert parse_thread_month("Ask HN: Who is hiring? (JANUARY 2015)") == datetime(
        2015, 1, 1, tzinfo=UTC
    )


def test_parse_thread_month_returns_none_for_unknown_month() -> None:
    assert parse_thread_month("Ask HN: Who is hiring? (Smarch 2020)") is None


def test_is_hiring_thread_title_excludes_sibling_threads() -> None:
    assert is_hiring_thread_title("Ask HN: Who is hiring? (October 2026)")
    # The spec's source rule: only 'Who is hiring?', not these two.
    assert not is_hiring_thread_title("Ask HN: Who wants to be hired? (October 2026)")
    assert not is_hiring_thread_title("Ask HN: Freelancer? Seeking freelancer? (October 2026)")
    assert not is_hiring_thread_title(None)
