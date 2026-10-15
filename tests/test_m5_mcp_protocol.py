"""M5 MCP protocol tests: real SDK clients over stdio and streamable HTTP.

Every check here talks to the server over a real MCP transport (a spawned stdio
subprocess or a loopback HTTP subprocess), lists tools, calls representative tools
against the deterministic no-text SQLite fixture, and drives the negative controls:
unknown tools, out-of-range arguments, write-style tools, a database failure and the
DNS-rebinding Host/Origin defences. Nothing reaches the network or a real HN post.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import Client, StdioServerParameters
from mcp_types import CallToolResult
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from skillpulse.eval.fixture import build_fixture_engine
from skillpulse.mcp_server.server import build_server
from skillpulse.tools.models import (
    CompareSkillsArgs,
    MatchCvArgs,
    SearchJobsArgs,
    SkillCountsArgs,
    SkillTrendArgs,
)
from skillpulse.tools.readonly import readonly_session
from tests._process_tree import free_port, spawn_kwargs, stop_tree

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = ["compare_skills", "match_cv", "search_jobs", "skill_counts", "skill_trend"]
BAD_DB_URL = "postgresql+psycopg2://nobody:nopass@127.0.0.1:1/none"


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update(extra)
    return env


def _stdio_params(*extra: str, env_extra: dict[str, str] | None = None) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "skillpulse.mcp_server", "--transport", "stdio", *extra],
        env=_env(env_extra),
        cwd=str(REPO_ROOT),
    )


def _payload(result: CallToolResult) -> dict[str, Any]:
    """Return the structured tool payload, falling back to the text block."""
    if result.structured_content is not None:
        return dict(result.structured_content)
    blocks = result.content or []
    assert blocks, "tool result carried no content"
    return dict(json.loads(blocks[0].text))


def _text(result: CallToolResult) -> str:
    return " ".join(block.text for block in (result.content or []) if hasattr(block, "text"))


def _find_key(schema: Any, key: str) -> Any:
    """Find a keyword anywhere in a JSON schema (optional fields nest it under anyOf)."""
    if isinstance(schema, dict):
        if key in schema:
            return schema[key]
        for value in schema.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(schema, list):
        for value in schema:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def test_mcp_stdio_lists_five_tools_and_calls_one() -> None:
    async def scenario() -> None:
        async with Client(_stdio_params("--fixture")) as client:
            listed = await client.list_tools()
            assert sorted(tool.name for tool in listed.tools) == EXPECTED_TOOLS
            result = await client.call_tool(
                "skill_counts", {"role_family": "backend", "top_n": 3}
            )
            assert result.is_error is False
            payload = _payload(result)
            assert payload["tool"] == "skill_counts"
            assert payload["row_count"] == 3
            assert payload["taxonomy_version"]
            assert "eligible" in payload["denominator_definition"]
            assert payload["values"]["denominator_definition"].startswith("posts with")
            assert "text_clean" not in json.dumps(payload["rows"])
            assert payload["rows"][0]["total_posts"] >= payload["rows"][0]["posts_with_skill"]

    anyio.run(scenario)


def test_mcp_stdio_taxonomy_resource() -> None:
    async def scenario() -> None:
        async with Client(_stdio_params("--fixture")) as client:
            result = await client.read_resource("skillpulse://taxonomy")
            payload = json.loads(result.contents[0].text)
            assert payload["version"]
            assert any(skill["id"] == "python" for skill in payload["skills"])
            assert len(payload["role_families"]) >= 1

    anyio.run(scenario)


def test_mcp_stdio_rejects_unknown_tool_bad_args_and_write_tools() -> None:
    async def scenario() -> None:
        async with Client(_stdio_params("--fixture")) as client:
            for name, args in [
                ("run_sql", {"sql": "DROP TABLE posts"}),
                ("run_shell", {"cmd": "whoami"}),
                ("insert_post", {"text": "x"}),
            ]:
                result = await client.call_tool(name, args)
                assert result.is_error is True
                assert "unknown tool" in _text(result).lower()
            bad_range = await client.call_tool("skill_counts", {"top_n": 999})
            assert bad_range.is_error is True and "top_n" in _text(bad_range)
            too_long = await client.call_tool("match_cv", {"cv_text": "x" * 20_001})
            assert too_long.is_error is True
            unknown_skill = await client.call_tool(
                "skill_trend", {"skill_id": "definitely-not-a-skill"}
            )
            assert unknown_skill.is_error is True

    anyio.run(scenario)


def test_mcp_stdio_db_failure_is_a_safe_tool_error() -> None:
    async def scenario() -> None:
        params = _stdio_params(env_extra={"SKILLPULSE_DATABASE_URL": BAD_DB_URL})
        async with Client(params) as client:
            result = await client.call_tool("skill_counts", {"top_n": 3})
            assert result.is_error is True
            message = _text(result)
            assert message.endswith("database unavailable")
            for secret in ("127.0.0.1", "postgresql", "nopass", "psycopg2"):
                assert secret not in message

    anyio.run(scenario)


def test_mcp_tool_schemas_match_the_shared_models() -> None:
    engine = build_fixture_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    server = build_server(factory)
    models = {
        "search_jobs": SearchJobsArgs,
        "skill_counts": SkillCountsArgs,
        "skill_trend": SkillTrendArgs,
        "compare_skills": CompareSkillsArgs,
        "match_cv": MatchCvArgs,
    }

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            schemas = {tool.name: tool.input_schema for tool in listed.tools}
            assert sorted(schemas) == EXPECTED_TOOLS
            for name, model in models.items():
                assert set(schemas[name]["properties"]) == set(model.model_fields), name
            assert schemas["search_jobs"]["properties"]["limit"]["maximum"] == 20
            assert schemas["skill_counts"]["properties"]["top_n"]["maximum"] == 50
            assert _find_key(schemas["skill_trend"]["properties"]["months"], "maximum") == 240
            assert schemas["match_cv"]["properties"]["cv_text"]["maxLength"] == 20_000
            assert schemas["compare_skills"]["properties"]["skill_ids"]["maxItems"] == 5
            assert "skill_id" in schemas["skill_trend"].get("required", [])

    try:
        anyio.run(scenario)
    finally:
        engine.dispose()


def test_readonly_fixture_backend_rejects_a_raw_write() -> None:
    engine = build_fixture_engine()
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with readonly_session(factory) as session, pytest.raises(DBAPIError):
            session.execute(
                text(
                    "INSERT INTO threads (id, title, month, created_at) "
                    "VALUES (999999, 'x', '2026-01-01', '2026-01-01')"
                )
            )
    finally:
        engine.dispose()


def _start_http_server(port: int, *extra: str) -> subprocess.Popen[str]:
    command: Sequence[str] = [
        sys.executable,
        "-m",
        "skillpulse.mcp_server",
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        *extra,
    ]
    return subprocess.Popen(
        list(command),
        cwd=str(REPO_ROOT),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **spawn_kwargs(),
    )


def _wait_http_ready(url: str, process: subprocess.Popen[str], timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            pytest.fail(f"HTTP MCP server exited early ({process.returncode}): {stderr[-2000:]}")
        try:
            httpx.get(url, headers={"Accept": "text/event-stream"}, timeout=2.0)
            return
        except httpx.HTTPError:
            time.sleep(0.25)
    pytest.fail("HTTP MCP server did not become ready in time")


def test_mcp_streamable_http_roundtrip_and_two_sessions() -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    process = _start_http_server(port, "--fixture")
    try:
        _wait_http_ready(url, process)

        async def scenario() -> None:
            async with Client(url) as client:
                listed = await client.list_tools()
                assert sorted(tool.name for tool in listed.tools) == EXPECTED_TOOLS
                result = await client.call_tool(
                    "skill_counts", {"role_family": "backend", "top_n": 2}
                )
                assert result.is_error is False
                assert _payload(result)["row_count"] == 2
            # A second independent session must initialise and serve too.
            async with Client(url) as client2:
                jobs = await client2.call_tool("search_jobs", {"limit": 1})
                assert jobs.is_error is False
                assert _payload(jobs)["row_count"] == 1

        anyio.run(scenario)
    finally:
        assert stop_tree(process) == set()


def test_mcp_http_dns_rebinding_defences() -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    process = _start_http_server(port, "--fixture")
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    try:
        _wait_http_ready(url, process)
        bad_host = httpx.post(
            url,
            headers={"Host": "evil.example", "Content-Type": "application/json"},
            json=initialize,
            timeout=5.0,
        )
        assert bad_host.status_code == 421
        bad_origin = httpx.post(
            url,
            headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
            json=initialize,
            timeout=5.0,
        )
        assert bad_origin.status_code == 403
        bad_content_type = httpx.post(
            url, headers={"Content-Type": "text/plain"}, content=b"{}", timeout=5.0
        )
        assert bad_content_type.status_code == 400
    finally:
        assert stop_tree(process) == set()


def test_mcp_server_refuses_non_loopback_http_without_explicit_opt_in() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "skillpulse.mcp_server",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--fixture",
        ],
        cwd=str(REPO_ROOT),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **spawn_kwargs(),
    )
    try:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode != 0
        assert "refusing to bind non-loopback" in stderr
    finally:
        stop_tree(process)
