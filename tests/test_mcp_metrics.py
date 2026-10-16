"""Actual MCP HTTP calls change the metrics served by the same transport."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from skillpulse.config import Settings
from skillpulse.eval.fixture import build_fixture_engine
from skillpulse.mcp_server.server import build_server
from skillpulse.observability import REGISTRY, observe_mcp_tool


def test_mcp_http_calls_and_errors_are_scrapeable() -> None:
    engine = build_fixture_engine()
    try:
        server = build_server(sessionmaker(bind=engine), settings=Settings(_env_file=None))
        app = server.streamable_http_app(json_response=True, stateless_http=True)
        labels = {"tool": "skill_counts", "outcome": "ok"}
        before = REGISTRY.get_sample_value("skillpulse_mcp_tool_calls_total", labels) or 0
        error_labels = {"tool": "skill_trend", "outcome": "error"}
        errors = REGISTRY.get_sample_value("skillpulse_mcp_tool_calls_total", error_labels) or 0
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            headers = {"Accept": "application/json, text/event-stream"}
            result = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "skill_counts", "arguments": {"top_n": 1}},
                },
            )
            assert result.status_code == 200, result.text
            assert result.json()["result"]["isError"] is False
            private = "private-mcp-sentinel"
            result = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "skill_trend", "arguments": {"skill_id": private}},
                },
            )
            assert result.status_code == 200
            assert result.json()["result"]["isError"] is True
            scrape = client.get("/metrics")
            assert scrape.status_code == 200
            assert private not in scrape.text
            assert 'tool="skill_counts"' in scrape.text
            assert "skillpulse_mcp_tool_duration_seconds_count" in scrape.text
        assert REGISTRY.get_sample_value("skillpulse_mcp_tool_calls_total", labels) == before + 1
        assert (
            REGISTRY.get_sample_value("skillpulse_mcp_tool_calls_total", error_labels) == errors + 1
        )
    finally:
        engine.dispose()


def test_unknown_metric_labels_are_bounded() -> None:
    observe_mcp_tool("private-tool-sentinel", "private-outcome-sentinel", 0.01)
    assert (
        REGISTRY.get_sample_value(
            "skillpulse_mcp_tool_calls_total", {"tool": "other", "outcome": "error"}
        )
        is not None
    )
    for sample in REGISTRY.collect():
        for value in sample.samples:
            assert "sentinel" not in str(value.labels)
