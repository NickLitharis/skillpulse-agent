"""M5 Inspector CLI roundtrip (env-gated).

Runs the pinned ``@modelcontextprotocol/inspector`` CLI against the real server over
stdio and streamable HTTP and parses its JSON output. It needs Node/npx, so it is
excluded from the default suite and runs only with ``SKILLPULSE_RUN_INSPECTOR=1``.
The GUI (web) Inspector is never claimed here: this is the scriptable CLI only.

The package ships several binaries (``inspector``, ``mcp-inspector``, ``mcpdo``), so the
``inspector`` binary is named explicitly with ``--package=`` instead of relying on npx's
automatic package-bin selection, which is ambiguous on Windows.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests._process_tree import free_port, spawn_kwargs, stop_tree
from tests.test_m5_mcp_protocol import _start_http_server, _wait_http_ready

pytestmark = pytest.mark.inspector

REPO_ROOT = Path(__file__).resolve().parents[1]
INSPECTOR_VERSION = "2.11.0"
INSPECTOR_PACKAGE = f"@modelcontextprotocol/inspector@{INSPECTOR_VERSION}"
RUN_INSPECTOR = os.environ.get("SKILLPULSE_RUN_INSPECTOR") == "1"


def _npx() -> str:
    found = shutil.which("npx")
    if found is None:
        pytest.skip("npx is not installed")
    return found


def _inspector_argv(*args: str) -> list[str]:
    """Explicit ``--package=`` + ``inspector`` binary; npx never guesses the bin."""
    return [_npx(), "--yes", f"--package={INSPECTOR_PACKAGE}", "inspector", *args]


def _run_inspector(*args: str, timeout_s: float = 240.0) -> tuple[int, str, str]:
    """Run the CLI; return ``(returncode, stdout, stderr)`` so callers can show stderr."""
    process = subprocess.Popen(
        _inspector_argv("--cli", *args),
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **spawn_kwargs(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:  # pragma: no cover - operator timeout
        stop_tree(process)
        pytest.fail("MCP Inspector CLI timed out")
    return process.returncode, stdout, stderr


def _server_command() -> list[str]:
    return [sys.executable, "-m", "skillpulse.mcp_server", "--transport", "stdio", "--fixture"]


@pytest.mark.skipif(not RUN_INSPECTOR, reason="set SKILLPULSE_RUN_INSPECTOR=1 to run Inspector")
def test_inspector_version_is_pinned() -> None:
    process = subprocess.Popen(
        _inspector_argv("-V"),
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **spawn_kwargs(),
    )
    stdout, stderr = process.communicate(timeout=240)
    assert process.returncode == 0, stderr
    assert stdout.strip() == INSPECTOR_VERSION


@pytest.mark.skipif(not RUN_INSPECTOR, reason="set SKILLPULSE_RUN_INSPECTOR=1 to run Inspector")
def test_inspector_cli_lists_and_calls_over_stdio() -> None:
    code, stdout, stderr = _run_inspector(
        "--transport",
        "stdio",
        "--cwd",
        str(REPO_ROOT),
        "--method",
        "tools/list",
        "--",
        *_server_command(),
    )
    assert code == 0, stderr or stdout
    tools = json.loads(stdout)["tools"]
    assert sorted(tool["name"] for tool in tools) == [
        "compare_skills",
        "match_cv",
        "search_jobs",
        "skill_counts",
        "skill_trend",
    ]

    code, stdout, stderr = _run_inspector(
        "--transport",
        "stdio",
        "--cwd",
        str(REPO_ROOT),
        "--method",
        "tools/call",
        "--tool-name",
        "skill_counts",
        "--tool-arg",
        "top_n=2",
        "--",
        *_server_command(),
    )
    assert code == 0, stderr or stdout
    payload = json.loads(stdout)
    assert payload["isError"] is False
    assert payload["structuredContent"]["tool"] == "skill_counts"
    assert payload["structuredContent"]["row_count"] == 2
    assert payload["structuredContent"]["taxonomy_version"]


@pytest.mark.skipif(not RUN_INSPECTOR, reason="set SKILLPULSE_RUN_INSPECTOR=1 to run Inspector")
def test_inspector_cli_lists_over_streamable_http() -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"
    server = _start_http_server(port, "--fixture")
    try:
        _wait_http_ready(url, server)
        code, stdout, stderr = _run_inspector(
            "--transport",
            "http",
            "--server-url",
            url,
            "--method",
            "tools/list",
        )
        assert code == 0, stderr or stdout
        names = sorted(tool["name"] for tool in json.loads(stdout)["tools"])
        assert names == [
            "compare_skills",
            "match_cv",
            "search_jobs",
            "skill_counts",
            "skill_trend",
        ]
    finally:
        assert stop_tree(server) == set()
