# M4 acceptance evidence (2026-10-15 UTC)

Scope: the shared read-only tools, the LangGraph agent, the grounding check, `POST
/v1/ask`, the deterministic fixture database and the agent evaluation, plus the small
unblocked M3 follow-up (the `worker-skills` task and the nightly trend-view refresh).

This round did **not** fetch Hacker News and did **not** call a remote LLM or agent model.
No number here is a real-data S2 result and no number here is a real-model S3/S4 result.

## Delivered and verified

| Part | Where | Evidence |
| --- | --- | --- |
| Five read-only tools | `src/skillpulse/tools/` | typed bounded Pydantic args; fixed parameterized SQL; content-addressed `result_id`; eligible-post denominator; 3-state `remote`; share withheld below 30 posts; no tool returns post text and there is no `get_examples`/`snippets` tool |
| Read-only enforcement | `src/skillpulse/tools/readonly.py`, `alembic/versions/0003_readonly_role.py` | `SET TRANSACTION READ ONLY` / `PRAGMA query_only`, `statement_timeout` and row cap; ORM `before_flush` rejects writes; migration 0003 adds `skillpulse_readonly` (NOLOGIN, SELECT only, no embedded credential) |
| LangGraph agent | `src/skillpulse/agent/graph.py`, `models.py` | native `StateGraph`: plan → tools → draft → grounding, max 6 tool calls, one rewrite, wall-clock deadline, cancellation callback; real OpenAI-compatible model path reuses the M3 client + shared Redis budget and is off by default |
| Grounding | `src/skillpulse/agent/grounding.py` | fabricated citations fail closed; only typed share/rate fields prove a percent; structured claims validated against the exact field path and rendered from it |
| `/v1/ask` SSE | `src/skillpulse/api/app.py` | events `plan`, `tool_call`, `tool_result`, `token`, `done` (trace_id/partial/grounded), `error`; disconnect cancels the run; Postgres down → 503; internal errors/credentials never returned |
| Fixture DB | `src/skillpulse/eval/fixture.py` | deterministic SQLite database, 60 months × 8 posts, **no post text**, tools read `skill_trend_mv`/`skill_segment_mv` |
| Agent eval | `src/skillpulse/eval/agent_eval.py`, `eval/datasets/agent_cases.json`, `eval/results/agent_fixture_fake.json` | 9 cases covering grounded statistics, ambiguity, impossible Greek, prompt injection, invented numbers, tool failure and cancellation |
| M3 worker follow-up | `src/skillpulse/workers/skills.py`, `tasks.py`, `app.py` | bounded idempotent extraction batches, default-closed provider factory, shared Redis budget, nightly `refresh_trend_views` in AUTOCOMMIT |

## Fake model vs real model (read this before quoting a number)

| Result | Status | Why |
| --- | --- | --- |
| Fixture run, `model="fake"`: correctness 1.0, grounding 1.0, tool-selection 1.0 over 9 cases | recorded in `eval/results/agent_fixture_fake.json` | deterministic pipeline check; the JSON records `model="fake"`, `source="fixture"` and the gate string |
| Real-model S3 (answer correctness ≥ 0.85) and S4 (grounding ≥ 0.98) | **BLOCKED** | `SKILLPULSE_AGENT_LLM_ENABLED=false` and no OpenAI-compatible credential/base URL or shared Redis budget was supplied |
| `/v1/ask` against a hosted deployment | **not run** | needs M5/M6; the endpoint is covered by TestClient against the fixture DB |

The nine fixture cases assert behaviour (grounded, ask-back, refuse, insufficient,
blocked invented number, safe tool failure, cancellation) — they are not an accuracy
measurement of a real model.

## Commands and results

| Check | Command | Result |
| --- | --- | --- |
| Lint | `uv run ruff check src tests` | clean |
| Types | `uv run mypy` | clean (strict, **40** source files) |
| Full suite | `.venv/Scripts/python.exe -m pytest -q` | **144 passed**, exit 0, wall 78 s (includes real Postgres + Redis integration) |
| Agent eval | `uv run skillpulse eval agent` | 9/9 correct, grounding 1.0, tool-selection 1.0 → `eval/results/agent_fixture_fake.json` |
| Negative control | weaken the fail-closed guard in `check_grounding`, run `tests/test_grounding.py::test_fabricated_citation_fails_closed` | **failed as required**; guard restored, `tests/test_grounding.py` 8 passed |

## Limits

- No real-HN text, no remote LLM and no real agent-model call. The agent's number
  grounding is proven on the fixture; the S4 real-data rate stays unmeasured.
- The read-only role's credential provisioning is an operator step outside this repo;
  migration 0003 creates the `NOLOGIN` role and grants, and no password is committed.
- Postgres is still reached through the sync engine; the SPEC's async API engine is not
  introduced in M4 (the endpoints are synchronous and bounded). Recorded as future work.
