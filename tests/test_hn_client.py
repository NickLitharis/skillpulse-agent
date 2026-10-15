"""Tests for the HN client: retry/backoff, rate limit, and payload parsing."""

from __future__ import annotations

import httpx
import pytest
import respx

from skillpulse.hn_client import HackerNewsClient, HackerNewsError, RateLimiter

BASE = "https://hacker-news.firebaseio.com/v0"


@respx.mock
def test_get_item_parses_payload() -> None:
    respx.get(f"{BASE}/item/1000.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1000,
                "type": "story",
                "by": "whoishiring",
                "title": "Ask HN: Who is hiring? (October 2026)",
                "time": 1700000000,
                "kids": [2001, 2002],
                "descendants": 2,
            },
        )
    )
    with HackerNewsClient(base_url=BASE, sleep=lambda _: None) as client:
        item = client.get_item(1000)
    assert item is not None
    assert item.id == 1000
    assert item.kids == (2001, 2002)
    assert item.title is not None


@respx.mock
def test_get_item_returns_none_for_null_payload() -> None:
    respx.get(f"{BASE}/item/404.json").mock(return_value=httpx.Response(200, json=None))
    with HackerNewsClient(base_url=BASE, sleep=lambda _: None) as client:
        assert client.get_item(404) is None


@respx.mock
def test_retries_then_succeeds_on_500() -> None:
    route = respx.get(f"{BASE}/item/1.json")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(503),
        httpx.Response(200, json={"id": 1, "type": "story"}),
    ]
    sleeps: list[float] = []
    with HackerNewsClient(
        base_url=BASE, max_retries=5, sleep=sleeps.append, limiter=RateLimiter(1e6)
    ) as client:
        item = client.get_item(1)
    assert item is not None and item.id == 1
    assert route.call_count == 3
    # Exponential backoff, not a flat retry delay.
    assert sleeps == [1.0, 2.0]


@respx.mock
def test_retries_on_429() -> None:
    route = respx.get(f"{BASE}/item/2.json")
    route.side_effect = [
        httpx.Response(429),
        httpx.Response(200, json={"id": 2, "type": "comment"}),
    ]
    with HackerNewsClient(
        base_url=BASE, max_retries=5, sleep=lambda _: None, limiter=RateLimiter(1e6)
    ) as client:
        assert client.get_item(2) is not None
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_max_retries() -> None:
    route = respx.get(f"{BASE}/item/3.json")
    route.mock(return_value=httpx.Response(500))
    with (
        HackerNewsClient(
            base_url=BASE, max_retries=2, sleep=lambda _: None, limiter=RateLimiter(1e6)
        ) as client,
        pytest.raises(HackerNewsError),
    ):
        client.get_item(3)
    # One initial attempt plus two retries.
    assert route.call_count == 3


@respx.mock
def test_does_not_retry_plain_4xx() -> None:
    route = respx.get(f"{BASE}/item/4.json")
    route.mock(return_value=httpx.Response(404))
    with (
        HackerNewsClient(
            base_url=BASE, max_retries=5, sleep=lambda _: None, limiter=RateLimiter(1e6)
        ) as client,
        pytest.raises(HackerNewsError),
    ):
        client.get_item(4)
    assert route.call_count == 1


@respx.mock
def test_retries_on_transport_error() -> None:
    route = respx.get(f"{BASE}/item/5.json")
    route.side_effect = [
        httpx.ConnectTimeout("timed out"),
        httpx.Response(200, json={"id": 5, "type": "story"}),
    ]
    with HackerNewsClient(
        base_url=BASE, max_retries=3, sleep=lambda _: None, limiter=RateLimiter(1e6)
    ) as client:
        assert client.get_item(5) is not None
    assert route.call_count == 2


@respx.mock
def test_get_user_submitted_ids() -> None:
    respx.get(f"{BASE}/user/whoishiring.json").mock(
        return_value=httpx.Response(200, json={"id": "whoishiring", "submitted": [11, 22, 33]})
    )
    with HackerNewsClient(base_url=BASE, sleep=lambda _: None, limiter=RateLimiter(1e6)) as client:
        assert client.get_user_submitted_ids("whoishiring") == (11, 22, 33)


def test_rate_limiter_allows_burst_then_throttles() -> None:
    now = [0.0]
    slept: list[float] = []

    def clock() -> float:
        return now[0]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(rate=2.0, clock=clock, sleep=fake_sleep)
    for _ in range(6):
        limiter.acquire()
    # Capacity is max(1, rate) == 2, so the last 4 acquisitions must have waited.
    assert len(slept) == 4
    assert all(s > 0 for s in slept)


def test_rate_limiter_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(0)
