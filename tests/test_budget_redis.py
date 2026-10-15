"""Fleet budget on real Redis: atomic reserve and UTC-day rollover (SPEC 8.2.5)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
import redis
from testcontainers.community.redis import RedisContainer

from skillpulse.extraction import budget as budget_module
from skillpulse.extraction.budget import RedisDailyBudget

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def redis_client() -> Iterator[redis.Redis]:
    with RedisContainer("redis:7-alpine") as container:
        url = (
            f"redis://{container.get_container_host_ip()}:"
            f"{container.get_exposed_port(6379)}/0"
        )
        client = redis.Redis.from_url(url)
        try:
            yield client
        finally:
            client.close()


def test_reserve_is_atomic_and_refuses_over_budget(redis_client: redis.Redis) -> None:
    redis_client.flushall()
    budget = RedisDailyBudget(redis_client, limit=100)
    assert budget.try_consume(60) is True
    assert budget.used == 60
    # A refused reserve must not consume anything.
    assert budget.try_consume(50) is False
    assert budget.used == 60
    assert budget.try_consume(40) is True
    assert budget.used == 100
    assert budget.try_consume(1) is False


def test_reserved_key_expires(redis_client: redis.Redis) -> None:
    redis_client.flushall()
    budget = RedisDailyBudget(redis_client, limit=10)
    budget.try_consume(1)
    assert redis_client.ttl(budget._key()) > 0


def test_utc_day_rollover_uses_a_fresh_counter(
    redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis_client.flushall()
    monkeypatch.setattr(budget_module, "_today_utc", lambda: date(2026, 10, 15))
    budget = RedisDailyBudget(redis_client, limit=10)
    assert budget.try_consume(10) is True
    assert budget.try_consume(1) is False
    monkeypatch.setattr(budget_module, "_today_utc", lambda: date(2026, 10, 16))
    assert budget.try_consume(1) is True
