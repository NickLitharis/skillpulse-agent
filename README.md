# SkillPulse Agent

A job-market analyser over Hacker News' monthly **"Ask HN: Who is hiring?"** threads.
Background workers collect the posts, an LLM extracts the skills each post asks for, and
a LangGraph agent answers questions about skill demand and trends using only database
results. The same read-only tools are exposed through an MCP server.

Full design: [SPEC.md](SPEC.md). Architecture decisions: [docs/decisions.md](docs/decisions.md).

## Status

Milestone **M5 — MCP server (stdio + HTTP)** (partial):

- `skillpulse.mcp_server` exposes the **same five read-only tools** over **stdio** and
  **streamable HTTP** using the official `mcp` SDK (`MCPServer`), not a hand-rolled
  protocol; every call goes through the same `skillpulse.tools.ToolRuntime` registry,
  read-only session, bounds and `result_id` — there is no MCP-only query path and no
  SQL/filesystem/shell tool,
- the MCP `inputSchema` mirrors the shared typed bounded arguments, `skillpulse://taxonomy`
  is a read-only resource, and errors are sanitised to safe tool errors (the DSN is never
  returned),
- HTTP binds **loopback** by default with DNS-rebinding protection on (bad `Host` → 421,
  bad `Origin` → 403); a non-loopback bind is refused unless
  `SKILLPULSE_MCP_ALLOW_NON_LOOPBACK=true`; stdio keeps stdout protocol-only and logs to
  stderr,
- real SDK protocol tests cover stdio and an HTTP subprocess, plus a real MCP Inspector
  CLI roundtrip (`@modelcontextprotocol/inspector@2.11.0`).

**M5 is partial, not complete.** "Works in Claude Desktop" (S8) is **blocked**: it needs
the Desktop GUI and a global client-config edit, which this round did not perform. A
ready example config and the manual check are in
[docs/m5-acceptance.md](docs/m5-acceptance.md).

Milestone **M4 — tools, LangGraph agent, `/v1/ask`, fixture DB and agent eval** (partial):

- `skillpulse.tools` holds the five read-only tools (`search_jobs`, `skill_counts`,
  `skill_trend`, `compare_skills`, `match_cv`) with typed bounded arguments and fixed
  parameterized SQL — the model never writes SQL, and a tool result carries a
  content-addressed `result_id`, the extractor/taxonomy provenance, the eligible-post
  denominator and the coverage caveat,
- every tool query runs in a read-only session (`READ ONLY` on Postgres, `query_only` on
  SQLite) with a statement timeout and a row cap; migration `0003` adds a `NOLOGIN`
  `skillpulse_readonly` role with `SELECT` only and no embedded credential,
- a native **LangGraph** agent runs plan → tools → draft → grounding with finite bounds
  (6 tool calls, one rewrite, a deadline, disconnect cancellation); the real model path
  reuses the M3 OpenAI-compatible client and the shared Redis budget and is **off by
  default**,
- the **grounding check** fails closed on a fabricated citation and only lets a typed
  `share`/`rate` field prove a percentage; invented numbers are logged and blocked,
- `POST /v1/ask` streams SSE (`plan`, `tool_call`, `tool_result`, `token`, `done` with
  `trace_id`/`partial`/`grounded`, `error`), returns `503` when Postgres is down and
  cancels on disconnect,
- a deterministic **fixture database** (no post text) and 9 agent smoke cases run in CI; the
  fake-model result is `eval/results/agent_fixture_fake.json`,
- a canonical **five-year synthetic evaluation fixture** (2019-2023, 1800 metadata-only
  posts, versioned SHA-256 row hash) and a **64-question, 222-fact agent set** across all
  five tools and every SPEC §11 category, with independently SQL-computed expected facts, a
  2-question regression gate and failure-sensitive controls; the run is tagged
  `model=fake` / `source=synthetic_fixture` / `not_real_acceptance` in
  `eval/results/agent_fixture_synthetic.json` — it is a contract check, never a real-model
  S3/S4 number.

**M4 is partial, not complete.** The real-model S3/S4 numbers need
`SKILLPULSE_AGENT_LLM_ENABLED` plus an OpenAI-compatible credential and shared budget,
which were not supplied; see [docs/m4-acceptance.md](docs/m4-acceptance.md).

Milestone **M3 — taxonomy, extraction and trends** (partial). What exists now:

- `config/skills.yaml` holds 336 skills in 13 categories plus 10 role families; the
  loader validates ids and aliases and rejects duplicates,
- the deterministic **baseline** extractor matches aliases on Unicode word boundaries
  with negation ("no Kubernetes", "ohne Docker") and nice-to-have rules
  (`required=false`),
- the **LLM** extractor uses structured output validated against the taxonomy, retries
  invalid JSON once and isolates post text as untrusted data (prompt-injection guard);
  the real OpenAI-compatible provider adapter is off by default, reads its key only from
  `SKILLPULSE_LLM_API_KEY`, and **fails closed without a shared atomic Redis daily
  budget**; a failure stores the baseline flagged as a fallback,
- migration `0002` adds `post_extractions` and `post_skills`, plus the materialised views
  `skill_trend_mv` and `skill_segment_mv` (refreshed with `REFRESH ... CONCURRENTLY`),
  with a 3-state `remote_state` so `remote=false` excludes unclassified posts,
- `/v1/skills/top`, `/v1/skills/{id}/trend` and `/v1/coverage` serve exact
  numerator/denominator counts; `total_posts` means eligible posts (for the requested
  extractor/taxonomy), and a share is withheld below 30 posts,
- `skillpulse taxonomy | extract | eval extraction | refresh-trends` wrap the same code.

**M3 is partial, not complete.** The real-LLM F1 (S2) and the ~150-post real-HN labelled
set need the M1 authorization plus an LLM provider credential, so no real-LLM number is
claimed. `eval/results/baseline_synthetic.json` is a **synthetic** baseline result; see
[docs/m3-acceptance.md](docs/m3-acceptance.md) and
[docs/milestones.json](docs/milestones.json) for the honest status per milestone.

Milestone **M2 — Celery/Redis workers** (partial: full back-fill blocked). The
asynchronous slice of SPEC §8 now runs behind Redis:

- `skillpulse.workers.app` is the Celery app; `skillpulse.workers.tasks` wraps the
  already-tested `fetch_service` functions, so the CLI and the workers run the same code,
- `discover_threads` queues one `fetch_thread` task per hiring thread (the back-fill
  fan-out), and `refresh_recent_threads` queues the current and previous month,
- Celery beat schedules that refresh every 6 hours, and a transient HN error is retried
  with backoff, resuming from the last committed batch,
- politeness and overlap control are Redis-wide: one shared 10 requests/second token bucket
  (SPEC §8.5) and an expiring per-thread lock that makes a second worker skip a thread it
  does not own (SPEC §8.6),
- the same idempotent upsert means re-running a task creates **zero duplicates** (S5), and
  a failed run is still recorded in `fetch_runs` with its resume point (S6).

Milestone **M1 — ingestion skeleton** remains the fetching slice: find every
"Who is hiring?" thread, fetch its top-level comments at <= 10 requests/second, clean the
HTML, strip recruiter e-mails and phone numbers, and upsert on the HN item id.

Not built yet: the MCP server (`stdio` + HTTP), Helm/Terraform, and the Kubernetes
deployment. The full historical back-fill remains blocked on the authorization gate below,
and a real-model agent/extraction acceptance run needs a provider credential and budget.

## Quick start

```bash
cp .env.example .env          # then edit if needed
make install                  # uv sync
make db-up                    # Postgres 16 on localhost:5433
make redis-up                 # Redis 7 on localhost:6379
make migrate                  # alembic upgrade head

make threads                  # list the hiring threads found on HN
make fetch THREAD=49922569    # fetch one thread's posts
```

Or run a bounded back-fill (newest threads first):

```bash
uv run skillpulse backfill --limit 3
```

## Workers

Start a worker and the scheduler in two terminals (Redis and Postgres must be up):

```bash
make worker                   # celery -A skillpulse.workers.app worker --pool=solo
make beat                     # celery -A skillpulse.workers.app beat
```

The beat schedule refreshes the current and previous month's threads every 6 hours
(`SKILLPULSE_CELERY_UPDATE_INTERVAL_SECONDS`, default 21600). To fan out a bounded
back-fill by hand, queue one `fetch_thread` task:

```bash
uv run celery -A skillpulse.workers.app call skillpulse.workers.tasks.fetch_thread --args='[49922569]'
```

## MCP server

The MCP server reuses the same read-only tools as the agent, so it needs no separate
SQL or credentials beyond the read-only database session:

```bash
# stdio (for MCP clients such as Claude Desktop)
.venv/Scripts/python.exe -m skillpulse.mcp_server --transport stdio
.venv/Scripts/python.exe -m skillpulse.mcp_server --transport stdio --fixture   # fixture DB, no Postgres

# streamable HTTP (loopback only, DNS-rebinding protection on)
.venv/Scripts/python.exe -m skillpulse.mcp_server --transport streamable-http --port 8765
```

`skillpulse-mcp` is installed as an alias of the same entry point. A ready Claude
Desktop config is at [docs/claude-desktop-mcp.example.json](docs/claude-desktop-mcp.example.json);
the transport, security and blocked-gate details are in
[docs/m5-acceptance.md](docs/m5-acceptance.md).

## Layout

```
src/skillpulse/
  hn_client.py        HN API client: typed items, token-bucket rate limit, retries
  text_cleaning.py    HTML -> text, PII stripping, thread-title month parsing
  models.py           SQLAlchemy models: threads, posts, fetch_runs, skills
  repository.py       idempotent upserts and fetch-run bookkeeping
  fetch_service.py    find threads, fetch a thread, back-fill
  extraction/         taxonomy loader, baseline + LLM extractors, persistence, service
  trends.py           trend statistics over the materialised views
  tools/              the five read-only tools (shared by the agent and the MCP server)
  mcp_server/         MCP server: stdio + streamable HTTP over the shared tools
  agent/              LangGraph graph, planner/drafter models, grounding check
  api/app.py          FastAPI stats endpoints (/v1/skills, /v1/coverage) and /v1/ask SSE
  eval/               labelled-set extraction scoring, fixture DB and agent evaluation
  runtime.py          shared client/session wiring for the CLI and the workers
  workers/
    app.py            Celery app: Redis broker/backend, beat schedule
    tasks.py          fetch_thread, discover_threads, refresh_recent_threads,
                      extract_pending, refresh_trend_views
    skills.py         bounded, idempotent extraction batches (worker-skills)
  cli.py              skillpulse threads | fetch | backfill | extract | eval | refresh-trends
alembic/              migrations (0001 ingestion, 0002 extraction/trends, 0003 read-only role)
tests/                unit tests + Postgres/Redis testcontainers integration tests
```

## Tests

```bash
make check             # ruff + mypy (strict) + unit tests, no Docker needed
make test-integration  # real Postgres 16 and Redis 7 via testcontainers (needs Docker)

uv run skillpulse taxonomy            # print the loaded taxonomy
uv run skillpulse eval extraction     # synthetic baseline F1 -> eval/results/
uv run skillpulse eval agent          # tiny fixture + fake model -> eval/results/

# canonical 64-question agent set on the frozen five-year synthetic fixture (fake model,
# synthetic provenance; regenerate the dataset with `eval build-agent-cases`)
uv run skillpulse eval build-agent-cases
uv run skillpulse eval agent --dataset eval/datasets/agent_eval_cases.json \
  --fixture-profile synthetic --source synthetic_fixture \
  --out eval/results/agent_fixture_synthetic.json

uv run skillpulse refresh-trends      # refresh the trend materialised views
uv run uvicorn skillpulse.api.app:app # stats API on localhost:8000

# real MCP Inspector CLI roundtrip (needs Node/npx; pinned inspector 2.11.0)
SKILLPULSE_RUN_INSPECTOR=1 .venv/Scripts/python.exe -m pytest tests/test_m5_inspector.py
```

`POST /v1/ask` needs a configured agent model; without `SKILLPULSE_AGENT_LLM_ENABLED` it
returns `503` rather than inventing an answer. The tools connect with a read-only role
(migration `0003`); the operator provisions its login and password outside the repository.

## Data source, terms and attribution

**Live fetching is gated.** Authorization to read and store Hacker News post text is
unresolved, so `skillpulse threads|fetch|backfill` and the workers refuse unless
`SKILLPULSE_HN_LIVE_FETCH_ENABLED=true` is set after a decision is recorded in
[docs/terms.md](docs/terms.md). Acceptance evidence and the honest coverage gaps are in
[docs/m1-m2-acceptance.md](docs/m1-m2-acceptance.md). The flag is a gate, not a claim of
permission.

Data comes only from the official Hacker News API (`https://hacker-news.firebaseio.com/v0/`,
no key). Post text stays in the local database; only counts, metadata and links leave it.
E-mails and phone numbers are stripped before storage, and the HN username (`by`) is not
stored.

Every post is attributable to `https://news.ycombinator.com/item?id=<id>`.
Posts belong to their authors; Hacker News is credited here as the source.

**Limits:** the data leans toward US and remote roles, has few European on-site posts, and
almost no Greek ones. It reflects companies that post on Hacker News, not the whole market.

## Licence

MIT for the code. Post text is not republished.