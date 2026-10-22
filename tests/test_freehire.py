"""Tests for the freehire integration: request shaping, parsing, and the tool result."""

from __future__ import annotations

import httpx
import pytest
import respx

from skillpulse.freehire import (
    FreehireClient,
    FreehireError,
    FreehireSearchArgs,
    FreehireToolRuntime,
)
from skillpulse.freehire.client import SearchPage, normalize_country, normalize_region
from skillpulse.freehire.tool import (
    FREEHIRE_TOOL_NAME,
    FREEHIRE_TOOL_SPEC,
    search_result,
)

BASE = "https://freehire.me"
SEARCH_URL = f"{BASE}/api/v1/jobs/search"

JOB = {
    "public_slug": "junior-ai-engineer-acme",
    "title": "Junior AI Engineer",
    "company": "Acme",
    "location": "Athens",
    "work_mode": "remote",
    "url": "https://example.invalid/job",
    "posted_at": "2026-10-20T00:00:00Z",
    "skills": "['python', 'llm']",
    "description": "x" * 2000,
}


@respx.mock
def test_search_sends_filters_and_parses_rows() -> None:
    route = respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(200, json={"data": [JOB], "meta": {"total": 18}})
    )

    page = FreehireClient().search(
        q="llm", skills=["python"], countries=["GR"], remote=True, limit=5
    )

    assert route.called
    params = route.calls.last.request.url.params
    assert params["q"] == "llm"
    assert params["skills"] == "python"
    assert params["countries"] == "GR"
    assert params["work_mode"] == "remote"
    assert params["limit"] == "5"

    assert page.total == 18
    assert page.ignored == []
    assert page.rows == [
        {
            "title": "Junior AI Engineer",
            "company": "Acme",
            "location": "Athens",
            "work_mode": "remote",
            "url": "https://example.invalid/job",
            "posted_at": "2026-10-20T00:00:00Z",
            "slug": "junior-ai-engineer-acme",
            "skills": ["python", "llm"],
        }
    ]


@respx.mock
def test_ignored_params_are_reported_not_silently_dropped() -> None:
    respx.get(SEARCH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [],
                "meta": {
                    "total": 0,
                    "ignored_params": [{"param": "query"}, {"param": "work_modes"}],
                },
            },
        )
    )

    page = FreehireClient().search(q="llm")

    assert page.ignored == ["query", "work_modes"]


@respx.mock
def test_http_failure_raises_freehire_error() -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(503))

    with pytest.raises(FreehireError):
        FreehireClient().search(q="llm")


@respx.mock
def test_non_json_body_raises_freehire_error() -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="<html>nope"))

    with pytest.raises(FreehireError):
        FreehireClient().search(q="llm")


def test_limit_is_clamped_to_the_api_maximum() -> None:
    client = FreehireClient()
    with respx.mock:
        route = respx.get(SEARCH_URL).mock(
            return_value=httpx.Response(200, json={"data": [], "meta": {"total": 0}})
        )
        client.search(limit=500)
    assert route.calls.last.request.url.params["limit"] == "20"


def test_tool_result_marks_the_source_as_external() -> None:
    args = FreehireSearchArgs(q="llm", countries=["GR"], limit=2)
    page = SearchPage(rows=[{"title": "a"}, {"title": "b"}], total=9, ignored=["query"])

    result = search_result(args, page)

    assert result.tool == FREEHIRE_TOOL_NAME
    assert result.extractor == "freehire-api"
    assert result.values["total"] == 9
    assert result.values["ignored_params"] == ["query"]
    assert result.truncated is True
    assert "external feed" in result.coverage_caveat.lower()
    assert result.row_count == 2


def test_args_reject_unknown_and_out_of_range_values() -> None:
    with pytest.raises(ValueError):
        FreehireSearchArgs(unknown_filter="GR")
    with pytest.raises(ValueError):
        FreehireSearchArgs(limit=99)


def test_tool_spec_is_namespaced_and_bounded() -> None:
    assert FREEHIRE_TOOL_SPEC["name"].startswith("freehire_")
    schema = FREEHIRE_TOOL_SPEC["args_schema"]
    assert schema["properties"]["limit"]["maximum"] == 20


class _FakeClient:
    def __init__(self, page: SearchPage) -> None:
        self._page = page
        self.calls: list[dict[str, object]] = []

    def search(self, **kwargs: object) -> SearchPage:
        self.calls.append(kwargs)
        return self._page


def test_runtime_routes_freehire_calls_to_the_client() -> None:
    fake = _FakeClient(SearchPage(rows=[{"title": "a"}], total=1))
    runtime = FreehireToolRuntime(
        None,  # type: ignore[arg-type]  # SQL is never touched on this path
        None,  # type: ignore[arg-type]
        client=fake,  # type: ignore[arg-type]
    )

    result = runtime.call(FREEHIRE_TOOL_NAME, {"q": "llm", "countries": ["GR"], "limit": 1})

    assert result.rows == [{"title": "a"}]
    assert fake.calls[0]["q"] == "llm"
    assert FREEHIRE_TOOL_NAME in {spec["name"] for spec in runtime.tool_specs}


def test_country_names_are_normalized_to_iso_codes() -> None:
    assert normalize_country("Greece") == "GR"
    assert normalize_country("greece") == "GR"
    assert normalize_country("gr") == "GR"
    assert normalize_country("United Kingdom") == "GB"
    assert normalize_country("Narnia") == "Narnia"


def test_region_names_are_normalized() -> None:
    assert normalize_region("Europe") == "eu"
    assert normalize_region("EU") == "eu"
    assert normalize_region("North America") == "north_america"
