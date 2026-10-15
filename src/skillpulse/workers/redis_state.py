"""Redis-backed coordination shared by every worker (SPEC sections 8.5 and 8.6).

Two pieces:

* :class:`RedisRateLimiter` — one token bucket in Redis, so the whole worker fleet stays
  under the polite 10 requests/second ceiling, not 10 per process.
* :class:`RedisThreadLock` — an expiring lock, so two runs on the same thread cannot
  overlap and a crashed worker does not hold it forever.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import redis

RATE_LIMIT_KEY = "skillpulse:hn:rate"
THREAD_LOCK_PREFIX = "skillpulse:lock:thread:"


class ThreadLockLostError(RuntimeError):
    """The worker no longer owns a thread lock it is still fetching under.

    Raised on a failed renewal so the fetch stops instead of continuing without the lock
    (two workers on one thread is exactly what SPEC section 8.6 forbids).
    """

# Atomic token bucket: refill by elapsed time, then consume one token if one is free.
# Redis TIME is the single clock, so separate worker processes cannot disagree.
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity end
if ts == nil then ts = now end
tokens = math.min(capacity, tokens + (now - ts) * rate)
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity / rate * 1000) + 1000)
return {allowed, tostring(tokens)}
"""


def seconds_until_token(tokens: float, rate: float) -> float:
    """Seconds to wait for the next token when ``tokens`` are left at ``rate``/second."""
    if tokens >= 1.0:
        return 0.0
    return max((1.0 - tokens) / rate, 0.001)


class RedisRateLimiter:
    """A token bucket stored in Redis and shared by all workers (SPEC section 8.5).

    Drop-in replacement for :class:`skillpulse.hn_client.RateLimiter`: it has the same
    ``acquire()`` method, so ``HackerNewsClient`` does not know the difference.
    """

    def __init__(
        self,
        client: redis.Redis,
        *,
        rate: float,
        capacity: float | None = None,
        key: str = RATE_LIMIT_KEY,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self._key = key
        self._sleep = sleep
        self._script = client.register_script(_TOKEN_BUCKET_LUA)

    def acquire(self) -> None:
        """Block until one token is available across the whole fleet."""
        while True:
            allowed, tokens = self._script(keys=[self._key], args=[self.rate, self.capacity])
            if int(allowed) == 1:
                return
            self._sleep(seconds_until_token(float(tokens), self.rate))


class RedisThreadLock:
    """An expiring per-thread lock (SPEC section 8.6).

    Usage::

        with RedisThreadLock(redis_client, thread_id, ttl_seconds=900) as lock:
            if not lock.acquired:
                ...  # another worker already holds this thread
            else:
                lock.refresh()  # extend the TTL after each committed batch
    """

    def __init__(
        self,
        client: redis.Redis,
        thread_id: int,
        *,
        ttl_seconds: int,
        key_prefix: str = THREAD_LOCK_PREFIX,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._lock = client.lock(f"{key_prefix}{thread_id}", timeout=self._ttl_seconds)
        self.acquired = False

    def __enter__(self) -> RedisThreadLock:
        self.acquired = bool(self._lock.acquire(blocking=False))
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self.acquired:
            # The TTL may have expired while we were working; then there is nothing to
            # release and redis raises rather than silently doing nothing.
            with suppress(redis.exceptions.LockError):
                self._lock.release()
            self.acquired = False

    def refresh(self) -> None:
        """Reset the TTL so a long fetch does not outlive its lock.

        Fails closed: if the lock is gone (expired, or taken by another worker) this
        raises :class:`ThreadLockLostError` rather than returning and letting the fetch
        continue without protection.
        """
        if not self.acquired:
            return
        try:
            self._lock.extend(self._ttl_seconds, replace_ttl=True)
        except redis.exceptions.LockError as exc:
            self.acquired = False
            raise ThreadLockLostError(f"lost the thread lock while refreshing: {exc}") from exc
