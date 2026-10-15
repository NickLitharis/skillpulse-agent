"""Hacker News API client: typed items, token-bucket rate limit, retry with backoff.

Sync HTTP on purpose: the workers are Celery sync tasks (SPEC section 8.7).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class HackerNewsError(RuntimeError):
    """Raised when the Hacker News API cannot be read after all retries."""


class TokenLimiter(Protocol):
    """Blocks until the next request is allowed.

    Implemented by the in-process :class:`RateLimiter` and by the Redis-wide limiter the
    workers use (SPEC section 8.5), so the client does not care which one it gets.
    """

    def acquire(self) -> None: ...


class RateLimiter:
    """Simple token bucket: at most ``rate`` acquisitions per second.

    The spec asks for a polite ceiling of 10 requests/second even though the API
    publishes no limit (SPEC section 8.5).
    """

    def __init__(
        self,
        rate: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = max(1.0, rate)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def acquire(self) -> None:
        """Block until one token is available."""
        while True:
            now = self._clock()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            self._sleep((1.0 - self._tokens) / self.rate)


@dataclass(frozen=True)
class HNItem:
    """The fields SkillPulse reads from an HN item."""

    id: int
    type: str | None
    by: str | None
    title: str | None
    text: str | None
    time: int | None
    kids: tuple[int, ...]
    deleted: bool
    dead: bool
    descendants: int | None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> HNItem:
        """Build an item from the raw API payload, tolerating missing fields."""
        kids = payload.get("kids") or []
        return cls(
            id=int(payload["id"]) if "id" in payload else 0,
            type=payload.get("type"),
            by=payload.get("by"),
            title=payload.get("title"),
            text=payload.get("text"),
            time=payload.get("time"),
            kids=tuple(int(k) for k in kids),
            deleted=bool(payload.get("deleted", False)),
            dead=bool(payload.get("dead", False)),
            descendants=payload.get("descendants"),
        )


class HackerNewsClient:
    """Read-only client for the official Hacker News API (no key needed)."""

    def __init__(
        self,
        base_url: str = "https://hacker-news.firebaseio.com/v0",
        *,
        max_requests_per_second: float = 10.0,
        max_retries: int = 5,
        timeout_seconds: float = 20.0,
        client: httpx.Client | None = None,
        limiter: TokenLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self._sleep = sleep
        self._limiter = limiter if limiter is not None else RateLimiter(max_requests_per_second)
        self._client = client if client is not None else httpx.Client(timeout=timeout_seconds)
        self._owns_client = client is None

    def close(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HackerNewsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _get(self, path: str) -> Any:
        """GET a JSON resource with rate limiting and exponential backoff.

        Retries 429/5xx and transport errors up to ``max_retries`` times; then raises
        ``HackerNewsError`` so the caller can mark the run failed (SPEC section 8.5).
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempts = self.max_retries + 1
        last_error: str = "unknown error"
        for attempt in range(attempts):
            self._limiter.acquire()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    # A deleted item comes back as an empty body, not valid JSON.
                    if not response.content:
                        return None
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise HackerNewsError(f"{url} -> invalid JSON: {exc}") from exc
                last_error = f"HTTP {response.status_code}"
                if response.status_code != 429 and response.status_code < 500:
                    # A 4xx that is not a throttle will not succeed on retry.
                    raise HackerNewsError(f"{url} -> {last_error}")
            if attempt < self.max_retries:
                backoff = min(2.0**attempt, 30.0)
                logger.warning(
                    "HN request failed (%s), retry %d/%d in %.1fs: %s",
                    last_error,
                    attempt + 1,
                    self.max_retries,
                    backoff,
                    url,
                )
                self._sleep(backoff)
        raise HackerNewsError(f"{url} failed after {attempts} attempts: {last_error}")

    def get_item(self, item_id: int) -> HNItem | None:
        """Fetch one item; ``None`` when the API returns ``null``."""
        payload = self._get(f"item/{item_id}.json")
        if payload is None:
            return None
        return HNItem.from_json(payload)

    def get_user_submitted_ids(self, username: str) -> tuple[int, ...]:
        """Return the submitted-item ids of a user, newest first."""
        payload = self._get(f"user/{username}.json")
        if not payload:
            return ()
        submitted = payload.get("submitted") or []
        return tuple(int(i) for i in submitted)
