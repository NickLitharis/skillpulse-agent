"""Keyless client for the freehire job API (https://freehire.me/docs/api).

The public search endpoint needs no credential, unlike the freehire MCP server, which
refuses every data tool until ``FREEHIRE_TOKEN`` is set. Only the read-only search is
used here: this project never writes to another service.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://freehire.me"
SEARCH_PATH = "/api/v1/jobs/search"
MAX_LIMIT = 20
USER_AGENT = "skillpulse/0.1 (+https://github.com/NickLitharis/skillpulse-agent)"


COUNTRY_CODES = {
    "greece": "GR", "greek": "GR", "uk": "GB", "united kingdom": "GB", "england": "GB",
    "germany": "DE", "netherlands": "NL", "holland": "NL", "france": "FR",
    "spain": "ES", "portugal": "PT", "italy": "IT", "ireland": "IE", "poland": "PL",
    "romania": "RO", "bulgaria": "BG", "cyprus": "CY", "sweden": "SE", "denmark": "DK",
    "finland": "FI", "norway": "NO", "austria": "AT", "belgium": "BE",
    "switzerland": "CH", "czechia": "CZ", "czech republic": "CZ", "hungary": "HU",
    "usa": "US", "united states": "US", "canada": "CA", "australia": "AU",
}


def normalize_country(value: str) -> str:
    """The API wants ISO codes; the model sends names like "Greece"."""
    cleaned = value.strip()
    if len(cleaned) == 2:
        return cleaned.upper()
    return COUNTRY_CODES.get(cleaned.lower(), cleaned)


class FreehireError(RuntimeError):
    """The freehire API could not be reached or returned an unusable response."""


@dataclass(frozen=True)
class SearchPage:
    """One page of live vacancies plus the metadata needed to read it honestly."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    total: int = 0
    ignored: list[str] = field(default_factory=list)


def _string_list(value: Any) -> list[str]:
    """The API sends list fields as a stringified Python list; parse it safely."""
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _posting(job: dict[str, Any]) -> dict[str, Any]:
    """Keep the fields a reader needs, and drop the full description (often 2 KB)."""
    return {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "work_mode": job.get("work_mode"),
        "url": job.get("url"),
        "posted_at": job.get("posted_at"),
        "slug": job.get("public_slug"),
        "skills": _string_list(job.get("skills")),
    }


class FreehireClient:
    """Search live vacancies. Read-only and keyless."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout_seconds: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    def search(
        self,
        *,
        q: str | None = None,
        skills: list[str] | None = None,
        countries: list[str] | None = None,
        regions: list[str] | None = None,
        remote: bool = True,
        limit: int = 10,
    ) -> SearchPage:
        """Return up to ``limit`` vacancies matching the filters."""
        params: dict[str, str] = {"limit": str(max(1, min(limit, MAX_LIMIT)))}
        if q:
            params["q"] = q
        if skills:
            params["skills"] = ",".join(skills)
        if countries:
            params["countries"] = ",".join(normalize_country(c) for c in countries)
        if regions:
            params["regions"] = ",".join(regions)
        if remote:
            params["work_mode"] = "remote"
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        try:
            with httpx.Client(
                timeout=self._timeout, transport=self._transport, headers=headers
            ) as client:
                response = client.get(self._base_url + SEARCH_PATH, params=params)
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as error:
            raise FreehireError(f"freehire request failed: {error}") from error
        except ValueError as error:
            raise FreehireError("freehire returned a non-JSON response") from error

        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise FreehireError("freehire response did not contain a data list")
        meta_raw = payload.get("meta")
        meta: dict[str, Any] = meta_raw if isinstance(meta_raw, dict) else {}
        ignored = [
            str(item.get("param"))
            for item in (meta.get("ignored_params") or [])
            if isinstance(item, dict) and item.get("param")
        ]
        raw_total = meta.get("total")
        total = raw_total if isinstance(raw_total, int) else len(payload["data"])
        rows = [_posting(job) for job in payload["data"] if isinstance(job, dict)]
        return SearchPage(rows=rows, total=total, ignored=ignored)
