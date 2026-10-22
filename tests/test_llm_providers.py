"""Provider wiring: configurable adapter, gates, and fail-closed shared budget."""

from __future__ import annotations

import httpx
import pytest
import respx

from skillpulse.config import Settings
from skillpulse.extraction.budget import RedisDailyBudget
from skillpulse.extraction.llm import LLMUnavailableError
from skillpulse.extraction.providers import (
    OpenAICompatibleClient,
    build_budget,
    build_extractor,
    build_llm_client,
)
from skillpulse.extraction.taxonomy import load_taxonomy

ENDPOINT = "http://llm.test/v1/chat/completions"


class FakeRedis:
    """Minimal Redis stand-in that emulates the atomic reserve script."""

    def __init__(self) -> None:
        self.used = 0

    def register_script(self, script: str):
        def call(*, keys: list[str], args: list[object]) -> int:
            limit = int(str(args[0]))
            tokens = int(str(args[1]))
            if self.used + tokens > limit:
                return 0
            self.used += tokens
            return 1

        return call

    def get(self, key: str) -> int:
        return self.used


def _configured(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "llm_enabled": True,
        "llm_base_url": "http://llm.test/v1",
        "llm_model": "test-model",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_gate_is_closed_by_default() -> None:
    assert Settings(_env_file=None).llm_enabled is False  # type: ignore[call-arg]
    assert build_llm_client(Settings(_env_file=None)) is None  # type: ignore[call-arg]


def test_enabled_but_unconfigured_fails_closed() -> None:
    settings = Settings(llm_enabled=True, llm_base_url="", llm_model="")
    with pytest.raises(LLMUnavailableError):
        build_llm_client(settings)


def test_real_provider_without_shared_redis_budget_fails_closed() -> None:
    settings = _configured()
    # The gate is open and the endpoint is set, but there is no fleet budget.
    with pytest.raises(LLMUnavailableError, match="shared Redis token budget"):
        build_budget(None, settings)


def test_extractor_uses_the_shared_redis_budget_when_configured() -> None:
    taxonomy = load_taxonomy()
    extractor = build_extractor(taxonomy, redis_client=FakeRedis(), settings=_configured())
    assert extractor is not None
    assert isinstance(extractor.budget, RedisDailyBudget)


def test_extractor_is_none_when_the_gate_is_closed() -> None:
    taxonomy = load_taxonomy()
    extractor = build_extractor(
        taxonomy, redis_client=FakeRedis(), settings=Settings(llm_enabled=False)
    )
    assert extractor is None


@respx.mock
def test_openai_compatible_client_posts_and_parses() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": '{"skills": []}'}}]}
        )
    )
    client = OpenAICompatibleClient(
        base_url="http://llm.test/v1", model="test-model", api_key="secret-key"
    )
    assert client.complete_json(system="s", prompt="p") == '{"skills": []}'
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer secret-key"
    assert b'"temperature":0' in request.content
    assert b'"max_tokens":512' in request.content
    assert b"json_object" in request.content


@respx.mock
def test_provider_sends_the_configured_output_token_limit() -> None:
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": '{"skills": []}'}}]}
        )
    )
    client = OpenAICompatibleClient(
        base_url="http://llm.test/v1", model="test-model", max_output_tokens=64
    )
    client.complete_json(system="s", prompt="p")
    assert b'"max_tokens":64' in route.calls.last.request.content


@respx.mock
def test_provider_error_is_wrapped() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(503))
    client = OpenAICompatibleClient(base_url="http://llm.test/v1", model="test-model")
    from skillpulse.extraction.llm import LLMExtractionError

    with pytest.raises(LLMExtractionError):
        client.complete_json(system="s", prompt="p")


@respx.mock
def test_malformed_provider_response_is_wrapped() -> None:
    respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json={"nope": True}))
    client = OpenAICompatibleClient(base_url="http://llm.test/v1", model="test-model")
    from skillpulse.extraction.llm import LLMExtractionError

    with pytest.raises(LLMExtractionError):
        client.complete_json(system="s", prompt="p")
