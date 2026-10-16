"""Exercise the actual metrics endpoint and metadata-only trace export, offline."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from skillpulse.agent.graph import AgentRun
from skillpulse.agent.models import FakeAgentModel
from skillpulse.api.app import create_app
from skillpulse.config import Settings
from skillpulse.eval.fixture import build_fixture_engine
from skillpulse.models import Post, PostExtractionRow
from skillpulse.observability import REGISTRY
from skillpulse.tracing import record_agent_trace


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    engine = build_fixture_engine()
    try:
        app = create_app(
            sessionmaker(bind=engine),
            agent_model=FakeAgentModel(),
            settings=Settings(_env_file=None, langfuse_enabled=False),
        )
        with TestClient(app) as test_client:
            yield test_client
        # A second app must not register duplicate collectors.
        create_app(sessionmaker(bind=engine), settings=Settings(_env_file=None))
    finally:
        engine.dispose()


def test_metrics_endpoint_counts_real_requests(client: TestClient) -> None:
    before = (
        REGISTRY.get_sample_value(
            "skillpulse_requests_total", {"method": "GET", "route": "/healthz", "status": "200"}
        )
        or 0
    )
    assert client.get("/healthz").status_code == 200
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    after = REGISTRY.get_sample_value(
        "skillpulse_requests_total", {"method": "GET", "route": "/healthz", "status": "200"}
    )
    assert after == before + 1


def test_unmatched_paths_and_queries_never_become_labels(client: TestClient) -> None:
    secret = "private-cv-email@example.com"
    assert client.get(f"/missing/{secret}", params={"question": secret}).status_code == 404
    response = client.get("/metrics")
    assert secret not in response.text
    assert 'route="unmatched"' in response.text


def test_agent_metrics_are_wired_to_requests(client: TestClient) -> None:
    before = REGISTRY.get_sample_value("skillpulse_agent_runs_total", {"status": "ok"}) or 0
    response = client.post("/v1/ask", json={"question": "Top Python skills?"})
    assert response.status_code == 200
    assert "event: done" in response.text
    assert REGISTRY.get_sample_value("skillpulse_agent_runs_total", {"status": "ok"}) == before + 1
    assert (
        REGISTRY.get_sample_value("skillpulse_agent_duration_seconds_count", {"status": "ok"})
        is not None
    )


def test_extraction_metrics_see_separate_writer_connections(tmp_path: Path) -> None:
    path = tmp_path / "extractions.db"
    engine = build_fixture_engine(path)
    writer = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    factory = sessionmaker(bind=engine)
    try:
        with writer.connect() as connection:
            expected = connection.scalar(select(func.count()).select_from(PostExtractionRow))
        assert expected is not None and expected > 0
        app = create_app(factory, settings=Settings(_env_file=None))
        with TestClient(app) as test_client:
            assert test_client.get("/metrics").status_code == 200
            total = sum(
                REGISTRY.get_sample_value("skillpulse_extraction_records", {"extractor": kind}) or 0
                for kind in ("baseline", "llm")
            )
            assert total == expected
            with sessionmaker(bind=writer)() as session:
                post_id = session.scalar(select(Post.id).limit(1))
                assert post_id is not None
                session.add(
                    PostExtractionRow(
                        post_id=post_id,
                        extractor="llm",
                        taxonomy_version="metric-test",
                        company="private-extraction-sentinel",
                    )
                )
                session.commit()
            scrape = test_client.get("/metrics")
            assert "private-extraction-sentinel" not in scrape.text
            total_after = sum(
                REGISTRY.get_sample_value("skillpulse_extraction_records", {"extractor": kind}) or 0
                for kind in ("baseline", "llm")
            )
            assert total_after == expected + 1
            assert REGISTRY.get_sample_value("skillpulse_extraction_scrape_ok") == 1
    finally:
        writer.dispose()
        engine.dispose()


def test_failed_extraction_query_reports_unknown_not_zero() -> None:
    engine = create_engine("sqlite://")
    try:
        app = create_app(sessionmaker(bind=engine), settings=Settings(_env_file=None))
        with TestClient(app) as test_client:
            assert test_client.get("/metrics").status_code == 200
        assert REGISTRY.get_sample_value("skillpulse_extraction_scrape_ok") == 0
        count = REGISTRY.get_sample_value(
            "skillpulse_extraction_records", {"extractor": "baseline"}
        )
        assert count is not None and math.isnan(count)
    finally:
        engine.dispose()


def _trace_settings() -> Settings:
    return Settings(
        _env_file=None,
        langfuse_enabled=True,
        langfuse_base_url="https://traces.example",
        langfuse_public_key="public-test-key",
        langfuse_secret_key="secret-test-key",
    )


@respx.mock
def test_trace_export_contains_only_safe_summary() -> None:
    route = respx.post("https://traces.example/api/public/otel/v1/traces").mock(
        return_value=httpx.Response(200, json={})
    )
    run = AgentRun(
        answer="PRIVATE-CV-ANSWER",
        events=[{"question": "PRIVATE-CV-QUESTION"}],
        trace_id="a" * 32,
        grounded=True,
        tool_calls_used=2,
        error="PRIVATE-ERROR",
    )
    record_agent_trace(run, time.time_ns(), settings=_trace_settings())
    assert route.call_count == 1
    payload = json.loads(route.calls[0].request.content)
    encoded = json.dumps(payload)
    assert "PRIVATE" not in encoded
    span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span["traceId"] == "a" * 32
    assert span["status"]["code"] == 2
    assert "Basic " in route.calls[0].request.headers["authorization"]
    assert "langfuse.trace.metadata.tool_calls" in encoded


@respx.mock
def test_tracing_is_disabled_by_default() -> None:
    record_agent_trace(AgentRun(), time.time_ns(), settings=Settings(_env_file=None))
    assert len(respx.calls) == 0


@respx.mock
def test_export_failure_is_sanitized_and_nonfatal(caplog: pytest.LogCaptureFixture) -> None:
    respx.post("https://traces.example/api/public/otel/v1/traces").mock(
        side_effect=httpx.ConnectError("PRIVATE-CV-ERROR secret-test-key")
    )
    record_agent_trace(AgentRun(), time.time_ns(), settings=_trace_settings())
    assert "Langfuse export failed" in caplog.text
    assert "PRIVATE" not in caplog.text
    assert "secret-test-key" not in caplog.text


@respx.mock
def test_incomplete_configuration_never_calls_network() -> None:
    record_agent_trace(
        AgentRun(), time.time_ns(), settings=Settings(_env_file=None, langfuse_enabled=True)
    )
    assert len(respx.calls) == 0
