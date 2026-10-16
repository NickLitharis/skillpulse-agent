# M5 acceptance evidence (updated 2026-10-16 UTC)

Scope: the MCP server that exposes the **same five M4 read-only tools** over **stdio**
and **streamable HTTP**, with real protocol/client integration tests and a real MCP
Inspector CLI roundtrip.

This round did **not** fetch Hacker News, did **not** call a remote LLM or agent model
and did **not** send any stored post text anywhere. Every protocol test runs against the
deterministic SQLite fixture DB (no post text) or a temporary local Postgres-free
session. The initial 2026-10-15 check used the Inspector CLI only. The browser GUI was
verified on 2026-10-16 as described below; Claude Desktop remains untested. See
[Blocked gates](#blocked-gates-not-claimed).

## Delivered and verified

| Part | Where | Evidence |
| --- | --- | --- |
| MCP server | `src/skillpulse/mcp_server/server.py` | official `mcp==2.3.0` SDK (`MCPServer`, not a hand-rolled protocol); a thin shell over `skillpulse.tools.ToolRuntime` — no MCP-only query path, no SQL/fs/shell tool |
| Five tools | same registry as the agent | `search_jobs`, `skill_counts`, `skill_trend`, `compare_skills`, `match_cv`, each with the M4 typed bounded `Annotated`/`Field` arguments; the Inspector-listed `inputSchema` matches the shared Pydantic models |
| Taxonomy resource | `skillpulse://taxonomy` | read-only resource returning the loaded taxonomy version + skills; no post text |
| Transports | `server.run(transport=...)` | `--transport stdio` (stdout is protocol-only, logs to stderr) and `--transport streamable-http` |
| CLI | `python -m skillpulse.mcp_server` | flags `--transport`, `--host`, `--port`, `--json-response`, `--stateless`, `--fixture`; entry point `skillpulse-mcp` |
| Error sanitisation | `_run()` in `server.py` | `ToolInputError` → `ToolError(str)`, Pydantic `ValidationError` → safe field message, DB/operational errors → `database unavailable`, anything else → `the tool could not complete the request`; the server logs the exception type only, never the DSN or values |
| HTTP hardening | `build_transport_security()` | `TransportSecuritySettings(enable_dns_rebinding_protection=True)`; loopback bind by default; a non-loopback `--host` is refused (`refusing to bind non-loopback`) unless `SKILLPULSE_MCP_ALLOW_NON_LOOPBACK=true` |
| Lifecycle | `AppContext` lifespan | the engine + `ToolRuntime` are built once at startup and disposed in `finally`; `KeyboardInterrupt` → exit 130; the stdio child is killed as a process tree on test teardown |
| Read-only stays intact | `skillpulse.tools.readonly` | MCP calls go through the same `readonly_session`; a raw `INSERT` over the MCP fixture DB raises `DBAPIError` and is returned as a safe tool error |

## Transports and commands

```bash
# stdio (stdout is the MCP protocol; logs go to stderr)
.venv/Scripts/python.exe -m skillpulse.mcp_server --transport stdio                 # local Postgres
.venv/Scripts/python.exe -m skillpulse.mcp_server --transport stdio --fixture       # deterministic fixture DB

# streamable HTTP (loopback only, DNS-rebinding protection on)
.venv/Scripts/python.exe -m skillpulse.mcp_server --transport streamable-http --port 8765
```

## Initial tests and results (2026-10-15)

| Check | Command | Result |
| --- | --- | --- |
| Lint | `uv run ruff check src tests` | clean |
| Types | `uv run mypy` | clean (strict, **43** source files) |
| Full suite | `.venv/Scripts/python.exe -m pytest -q` | **154 passed, 3 skipped**, exit 0, wall 62 s (skips are the env-gated Inspector CLI tests; includes the real Postgres + Redis integration tests) |
| Real MCP SDK, stdio | `pytest tests/test_m5_mcp_protocol.py` | `tools/list` returns the 5 tools; a real `tools/call` roundtrip returns the shared payload; `skillpulse://taxonomy` reads back |
| Real MCP SDK, HTTP | same file | subprocess `streamable-http` server; two sequential client sessions both succeed; bad `Host` → 421, bad `Origin` → 403, bad content-type → 400 |
| Inspector CLI | `SKILLPULSE_RUN_INSPECTOR=1 uv run pytest tests/test_m5_inspector.py` | **3 passed**, real `@modelcontextprotocol/inspector@2.11.0` on Node v24.11.1 (npx 11.6.2): `tools/list` over stdio and over HTTP, and `tools/call skill_counts top_n=2` over stdio. Invoked as `npx --yes --package=@modelcontextprotocol/inspector@2.11.0 inspector …` (explicit binary: the package ships `inspector`/`mcp-inspector`/`mcpdo`, so npx must not guess), and the helper keeps stderr so a CLI failure is never silent. |
| Negative control | remove the SQLite `PRAGMA query_only` guard in `readonly.py`, run `test_readonly_fixture_backend_rejects_a_raw_write` | **failed as required** (`DID NOT RAISE DBAPIError`); guard restored → the test and `test_readonly_session_rejects_orm_writes` both pass |
| Latent M4 bug fixed | `search_jobs` with **no** filters built `... AND  ORDER BY` (invalid SQL) | fixed in `src/skillpulse/tools/registry.py`; regression test `tests/test_m4_tools_agent.py::test_search_jobs_without_filters_returns_rows` added |

Saved Inspector evidence (bounded): `docs/evidence/m5-inspector-version.txt`,
`docs/evidence/m5-inspector-tools-list.json` (~12 KB),
`docs/evidence/m5-inspector-tools-call.json` (~3 KB), each produced by the explicit
`--package=` + `inspector` invocation above. The `tools/call` output contains
the exact shared tool payload (`result_id`, `extractor`, `taxonomy_version`,
`denominator_definition`, `coverage_caveat`, `sample_floor=30`).

## Browser Inspector and GIF (2026-10-16)

The real MCP Inspector **2.11.0** browser UI connected over stdio to
`skillpulse-mcp --fixture`. It listed all five tools and executed `skill_counts`.
The rendered structured result contains eight rows with **480 eligible fixture posts**.
No HN access, model call, private post text or Desktop configuration was involved.

The [README demo](../README.md#inspector-demo-synthetic-data) embeds the actual recording
at [assets/inspector-demo.gif](assets/inspector-demo.gif), not a mockup. The GIF was decoded
successfully: 103 frames, 48 distinct frame hashes. A screenshot was independently reviewed
for unintended private content. The browser session and owned Inspector process tree were
closed; the two temporary loopback listeners were removed.

This closes the Inspector GUI and demo-asset gaps, not the separate Desktop or hosting gaps.
Latest full regression: **182 passed, 5 opt-in skipped**, including 19 Docker integration tests.
PR code revision `956f597` also passed real GitHub CI and the hosted deployment check.

## Blocked gates (not claimed)

| Gate | Status | Why / what would close it |
| --- | --- | --- |
| Claude Desktop end-to-end (S8 "works in Claude Desktop") | **BLOCKED — not validated** | Validating it needs the Claude Desktop GUI and an edit to the **global** client config. This round did not touch global config and did not claim GUI validation. A ready config is at `docs/claude-desktop-mcp.example.json`; the manual check is below. |
| Hosted / public MCP endpoint | **not deployed** | Local M6 Kubernetes HTTP smoke is verified; no public/TLS endpoint is claimed. |

### Manual check for the blocked Claude Desktop gate

1. Merge the `mcpServers` block from `docs/claude-desktop-mcp.example.json` into the
   Claude Desktop MCP config, replacing the absolute paths with this checkout's.
2. Fully restart Claude Desktop.
3. Open a chat and ask it to list its tools: `search_jobs`, `skill_counts`,
   `skill_trend`, `compare_skills`, `match_cv` must appear.
4. Ask a statistics question (for example "top skills in backend roles") and confirm the
   answer carries the eligible-post denominator and the coverage caveat, and that no post
   text is returned.

Do not record this as done until steps 1-4 have run on a machine with a real Claude
Desktop installed.

## Limits

- The M5 evidence is **fixture/local only**: no real HN text is fetched or transmitted,
  and the LLM/agent/provider gates stay off (D006 and the M3/M4 gates).
- The HTTP transport is validated on **loopback**; no reverse proxy, TLS or public bind
  was configured and none is claimed.
- M6 now manages separate API/MCP login/password Secrets through CloudNativePG and grants
  read-only membership after migration `0003` creates the `NOLOGIN` group. No credential
  is committed. See the [deployment runbook](deployment.md).
