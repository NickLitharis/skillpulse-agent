"""Opt-in, real local Langfuse ingestion/read-back; ordinary collection makes no requests."""

from __future__ import annotations

import json
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx
import pytest

from skillpulse.agent.graph import AgentRun
from skillpulse.config import Settings
from skillpulse.tracing import record_agent_trace

pytestmark = pytest.mark.skipif(
    os.environ.get("SKILLPULSE_RUN_LANGFUSE_SMOKE") != "1",
    reason="opt-in local Langfuse test; run scripts/langfuse-smoke.ps1",
)


@pytest.fixture
def smoke_settings() -> Settings:
    base_url = os.environ.get("SKILLPULSE_LANGFUSE_BASE_URL", "").rstrip("/")
    parsed = urlsplit(base_url)
    assert (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and parsed.port is not None
        and not parsed.username
        and not parsed.password
        and parsed.path in ("", "/")
        and not parsed.query
        and not parsed.fragment
    ), "Live smoke requires an explicit loopback-only URL from the isolated runner."
    public = os.environ.get("SKILLPULSE_LANGFUSE_PUBLIC_KEY", "")
    secret = os.environ.get("SKILLPULSE_LANGFUSE_SECRET_KEY", "")
    assert public and secret, "Run the isolated smoke runner to configure disposable keys."
    return Settings(
        _env_file=None,
        langfuse_enabled=True,
        langfuse_base_url=base_url,
        langfuse_public_key=public,
        langfuse_secret_key=secret,
    )


def test_real_trace_is_stored_with_only_safe_metadata(smoke_settings: Settings) -> None:
    trace_id = uuid.uuid4().hex
    private = f"PRIVATE-SYNTHETIC-{uuid.uuid4().hex}"
    run = AgentRun(
        trace_id=trace_id,
        answer=private,
        events=[{"question": private, "cv_text": private, "tool_result": private}],
        grounded=True,
        partial=True,
        tool_calls_used=2,
        error=private,
    )
    record_agent_trace(run, time.time_ns(), settings=smoke_settings)
    deadline = time.monotonic() + 90
    last_status = 0
    trace: dict[str, object] = {}
    with httpx.Client(
        base_url=smoke_settings.langfuse_base_url or "",
        auth=(smoke_settings.langfuse_public_key or "", smoke_settings.langfuse_secret_key or ""),
        timeout=3,
        follow_redirects=False,
    ) as client:
        while time.monotonic() < deadline:
            response = client.get(f"/api/public/traces/{trace_id}")
            last_status = response.status_code
            assert last_status not in (401, 403), "Disposable project authentication failed."
            if last_status == 200:
                trace = response.json()
                if trace.get("observations"):
                    break
            time.sleep(1)
        else:
            pytest.fail(f"No stored trace/observation after 90s; last HTTP status {last_status}.")
    assert trace["id"] == trace_id
    assert trace["name"] == "skillpulse-agent"
    assert trace["public"] is False
    assert trace.get("input") is None
    assert trace.get("output") is None
    assert private not in json.dumps(trace)
    metadata = trace["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["grounded"] is True
    assert metadata["partial"] is True
    assert metadata["failed"] is True
    assert metadata["tool_calls"] == 2
    observations = trace["observations"]
    assert isinstance(observations, list)
    spans = [item for item in observations if item.get("name") == "skillpulse-agent"]
    assert len(spans) == 1
    assert spans[0]["traceId"] == trace_id
    assert spans[0]["startTime"] and spans[0]["endTime"]
    assert spans[0].get("input") is None and spans[0].get("output") is None


def test_disabled_export_does_not_create_a_trace(
    smoke_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    send = httpx.HTTPTransport.handle_request

    def counted_send(transport: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        attempts.append(request.method)
        return send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", counted_send)
    trace_id = uuid.uuid4().hex
    disabled = smoke_settings.model_copy(update={"langfuse_enabled": False})
    record_agent_trace(
        AgentRun(trace_id=trace_id, answer="PRIVATE-DISABLED"), time.time_ns(), settings=disabled
    )
    # A mere immediate 404 could race asynchronous ingestion; reject even an attempted send.
    assert not attempts, "Disabled export attempted an HTTP request."
    response = httpx.get(
        f"{smoke_settings.langfuse_base_url}/api/public/traces/{trace_id}",
        auth=(smoke_settings.langfuse_public_key or "", smoke_settings.langfuse_secret_key or ""),
        timeout=3,
        follow_redirects=False,
    )
    assert response.status_code == 404, "Disabled export must not create a persisted trace."
