"""Provider adapter for the LLM extractor (SPEC section 7: LiteLLM, structured output).

This is the real, configurable provider path — not just the :class:`StructuredLLMClient`
protocol. It speaks the OpenAI-compatible ``/chat/completions`` surface, which OpenAI,
LiteLLM (as a proxy) and local Ollama all expose, so switching provider is config, not
code. Two gates keep it safe:

* **No call without explicit configuration.** ``build_extractor`` returns ``None`` while
  ``SKILLPULSE_LLM_ENABLED`` is false, and raises :class:`LLMUnavailableError` when it is
  enabled but incomplete. Calibration in ``skillpulse.config`` also **fails closed**: a
  real provider may only be used with a shared, fleet-wide Redis token budget, because a
  process-local counter is not a cost guard. Without Redis the caller stays on the
  offline baseline, which never needs a budget or a network.
* **No ambient credentials.** The API key is read only from ``SKILLPULSE_LLM_API_KEY``.
  A local Ollama endpoint needs no key, so the key may stay empty.
"""

from __future__ import annotations

from typing import Any

import httpx
import redis

from skillpulse.config import Settings, get_settings
from skillpulse.extraction.budget import RedisDailyBudget, TokenBudget
from skillpulse.extraction.llm import (
    LLMExtractionError,
    LLMExtractor,
    LLMUnavailableError,
    StructuredLLMClient,
)
from skillpulse.extraction.taxonomy import Taxonomy


class OpenAICompatibleClient:
    """Structured-output client for OpenAI-compatible chat completions endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: float = 60.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not base_url.strip():
            raise LLMUnavailableError("LLM base URL is not configured")
        if not model.strip():
            raise LLMUnavailableError("LLM model is not configured")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(timeout=timeout_seconds)

    def complete_json(self, *, system: str, prompt: str) -> str:
        """POST a chat completion requesting a JSON object and return its content."""
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = self._client.post(self._endpoint, headers=headers, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise LLMExtractionError(f"LLM provider request failed: {exc}") from exc
        try:
            data = response.json()
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMExtractionError(f"unexpected LLM provider response: {exc}") from exc

    def close(self) -> None:
        """Close the underlying HTTP client when this adapter owns it."""
        if self._owns_client:
            self._client.close()


def build_llm_client(settings: Settings | None = None) -> StructuredLLMClient | None:
    """Build the provider client, or ``None`` while the LLM gate is closed.

    Raises :class:`LLMUnavailableError` when the gate is on but the configuration is
    incomplete, so a half-configured deployment fails loudly instead of silently
    pretending to extract.
    """
    settings = settings or get_settings()
    if not settings.llm_enabled:
        return None
    if not settings.llm_base_url or not settings.llm_model:
        raise LLMUnavailableError(
            "SKILLPULSE_LLM_ENABLED is true but SKILLPULSE_LLM_BASE_URL/LLM_MODEL are unset"
        )
    return OpenAICompatibleClient(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        timeout_seconds=settings.llm_timeout_seconds,
    )


def build_budget(
    redis_client: redis.Redis | None, settings: Settings | None = None
) -> TokenBudget:
    """Build the *shared* daily budget, or fail closed for real provider use.

    A process-local budget is fine for tests and a single-process CLI run, but it is not
    a fleet cost guard: N worker processes would each spend the full daily limit. Real
    provider calls therefore require Redis, and a missing Redis raises instead of quietly
    falling back to the in-process counter.
    """
    settings = settings or get_settings()
    if redis_client is None:
        raise LLMUnavailableError(
            "real LLM provider use requires a shared Redis token budget; set "
            "SKILLPULSE_REDIS_URL (fleet cost guard fails closed without it)"
        )
    return RedisDailyBudget(redis_client, settings.llm_daily_token_budget)


def build_extractor(
    taxonomy: Taxonomy,
    *,
    redis_client: redis.Redis | None = None,
    settings: Settings | None = None,
) -> LLMExtractor | None:
    """Build the LLM extractor through both gates, or ``None`` for baseline-only mode.

    ``None`` means "use the offline baseline" and is the default: the LLM gate is closed.
    When the gate is open, an incomplete provider config or a missing shared budget
    raises :class:`LLMUnavailableError`, so a misconfigured deployment cannot spend
    money with a guard it does not have.
    """
    settings = settings or get_settings()
    client = build_llm_client(settings)
    if client is None:
        return None
    budget = build_budget(redis_client, settings)
    return LLMExtractor(
        taxonomy,
        client,
        budget=budget,
        max_output_tokens=settings.llm_max_output_tokens,
    )
