"""HTML-to-text cleaning, PII stripping and thread-title parsing.

All rules here come from SPEC section 5 and section 8:
* posts are HTML; store the text with entities and tags resolved;
* strip e-mail addresses and phone numbers before storing;
* the thread month comes from the title.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAGS_RE = re.compile(r"</(p|div|li|blockquote|pre)>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_SPACE_RE = re.compile(r"[ \t]{2,}")

# Use an anchored local part and an optional scheme-less domain to avoid matching
# version strings; SPEC only needs recruiter contact details removed.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<![\w.])(?:\+?\d{1,3}[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?![\w.])"
)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_THREAD_TITLE_PREFIX = "Ask HN: Who is hiring?"
_TITLE_MONTH_RE = re.compile(r"\(([A-Za-z]+)\s+(\d{4})\)\s*$")


def strip_html(raw_html: str | None) -> str:
    """Convert an HN comment body to plain text, resolving HTML entities."""
    if not raw_html:
        return ""
    text = _BR_RE.sub("\n", raw_html)
    text = _BLOCK_TAGS_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _SPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _MULTI_BLANK_RE.sub("\n\n", text).strip()


def strip_pii(text: str) -> str:
    """Remove e-mail addresses and phone numbers from already-cleaned text."""
    return _PHONE_RE.sub("[redacted]", _EMAIL_RE.sub("[redacted]", text))


def clean_comment_text(raw_html: str | None) -> str:
    """Full pipeline used before storing a post: HTML out, then PII out."""
    return strip_pii(strip_html(raw_html))


def parse_thread_month(title: str) -> datetime | None:
    """Return the first day of the thread's month, or ``None`` if unparseable.

    Titles look like ``Ask HN: Who is hiring? (October 2026)``; the API also carries
    ``time``, but the title is the authoritative month for back-fill (SPEC section 8.1).
    """
    match = _TITLE_MONTH_RE.search(title.strip())
    if match is None:
        return None
    month_name, year_str = match.group(1).lower(), match.group(2)
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    return datetime(int(year_str), month, 1, tzinfo=UTC)


def is_hiring_thread_title(title: str | None) -> bool:
    """True only for 'Who is hiring?' threads, not 'Who wants to be hired?'."""
    if not title:
        return False
    return title.strip().startswith(_THREAD_TITLE_PREFIX)


def timestamp_to_datetime(unix_seconds: int | None) -> datetime | None:
    """Convert an HN unix timestamp to an aware UTC datetime."""
    if unix_seconds is None:
        return None
    return datetime.fromtimestamp(int(unix_seconds), tz=UTC)
