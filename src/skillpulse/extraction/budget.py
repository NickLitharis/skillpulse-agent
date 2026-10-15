"""Daily LLM token budget (SPEC section 8.2.5: cost guard).

When the day's budget is spent, extraction falls back to the baseline until the next
day instead of silently spending more. Two implementations share one protocol: the
in-memory one for tests, and a Redis one so every worker process sees the same counter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Protocol, cast

import redis

DEFAULT_DAILY_TOKEN_BUDGET = 2_000_000

# Atomic reserve: read, check and increment in one round trip, so two workers cannot
# both pass the ceiling. A UTC-day key expires after the day, so it resets on its own.
_BUDGET_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
local tokens = tonumber(ARGV[2])
if used + tokens > limit then return 0 end
redis.call('INCRBY', KEYS[1], tokens)
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""


class TokenBudget(Protocol):
    """The one operation the extractor needs: reserve tokens or refuse."""

    def try_consume(self, tokens: int) -> bool:
        """Reserve ``tokens`` and return ``True``, or return ``False`` if over budget."""

    @property
    def used(self) -> int:
        """Tokens consumed today so far."""

    @property
    def limit(self) -> int:
        """The daily ceiling."""


def _today_utc() -> date:
    return datetime.now(UTC).date()


class InMemoryDailyBudget:
    """Process-local daily budget with UTC rollover; for tests and a single CLI run.

    This is **not** a fleet cost guard: N worker processes each get their own counter.
    Real provider calls are wired to :class:`RedisDailyBudget` and fail closed without
    Redis (see ``skillpulse.extraction.providers.build_budget``).
    """

    def __init__(
        self, limit: int = DEFAULT_DAILY_TOKEN_BUDGET, today: Callable[[], date] = _today_utc
    ) -> None:
        if limit <= 0:
            raise ValueError("budget limit must be positive")
        self._limit = limit
        self._today = today
        self._day = today()
        self._used = 0

    def _rollover(self) -> None:
        current = self._today()
        if current != self._day:
            self._day = current
            self._used = 0

    def try_consume(self, tokens: int) -> bool:
        if tokens < 0:
            raise ValueError("tokens must not be negative")
        self._rollover()
        if self._used + tokens > self._limit:
            return False
        self._used += tokens
        return True

    @property
    def used(self) -> int:
        self._rollover()
        return self._used

    @property
    def limit(self) -> int:
        return self._limit


class RedisDailyBudget:
    """Fleet-wide daily budget in Redis, shared atomically by every worker process."""

    def __init__(
        self,
        client: redis.Redis,
        limit: int = DEFAULT_DAILY_TOKEN_BUDGET,
        *,
        key_prefix: str = "skillpulse:llm:tokens",
        ttl_seconds: int = 26 * 3600,
    ) -> None:
        if limit <= 0:
            raise ValueError("budget limit must be positive")
        self._client = client
        self._limit = limit
        self._key_prefix = key_prefix
        self._ttl = ttl_seconds
        self._script = client.register_script(_BUDGET_LUA)

    def _key(self) -> str:
        return f"{self._key_prefix}:{_today_utc().isoformat()}"

    def try_consume(self, tokens: int) -> bool:
        if tokens < 0:
            raise ValueError("tokens must not be negative")
        allowed = self._script(
            keys=[self._key()], args=[self._limit, tokens, self._ttl]
        )
        return bool(allowed)

    @property
    def used(self) -> int:
        raw = cast(bytes | str | None, self._client.get(self._key()))
        return int(raw) if raw is not None else 0

    @property
    def limit(self) -> int:
        return self._limit
