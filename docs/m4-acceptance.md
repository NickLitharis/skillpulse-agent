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
| Fixture DB | `src/skillpulse/eval/fixture.py`, `src/skillpulse/eval/synthetic_fixture.py` | tiny smoke fixture (60 months × 8 posts) plus the canonical five-year **synthetic** metadata fixture (2019-2023, 1800 posts, no post text/PII, versioned row hash); tools read `skill_trend_mv`/`skill_segment_mv` |
| Agent eval | `src/skillpulse/eval/agent_eval.py`, `agent_dataset.py`, `eval/datasets/agent_cases.json`, `eval/datasets/agent_eval_cases.json`, `eval/results/agent_fixture_fake.json`, `eval/results/agent_fixture_synthetic.json` | 9 smoke cases plus a canonical 64-question, 222-fact set covering all five tools and every SPEC §11 category |
| M3 worker follow-up | `src/skillpulse/workers/skills.py`, `tasks.py`, `app.py` | bounded idempotent extraction batches, default-closed provider factory, shared Redis budget, nightly `refresh_trend_views` in AUTOCOMMIT |

## Fake model vs real model (read this before quoting a number)

| Result | Status | Why |
| --- | --- | --- |
| Fixture run, `model="fake"`: correctness 1.0, grounding 1.0, tool-selection 1.0 over 9 cases | recorded in `eval/results/agent_fixture_fake.json` | deterministic pipeline check; the JSON records `model="fake"`, `source="fixture"` and the gate string |
| Canonical fixture run, `model="fake"` / `source="synthetic_fixture"`: correctness 1.0, grounding 1.0, tool-selection 1.0, facts 222/222 over 64 cases | recorded in `eval/results/agent_fixture_synthetic.json` (`acceptance="not_real_acceptance"`) | deterministic contract check over the frozen five-year synthetic fixture; usage is null with a reason because the fake model produces none |
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

## Canonical agent set on the five-year synthetic fixture (2026-10-15)

SPEC §11 asks for ~60 questions against a frozen ~5-year fixture with fixed extracted
fields and no post text. That dataset and harness now exist and run offline in CI.

| Item | Value |
| --- | --- |
| Fixture | `synth-2019-2023-v1`, 60 months (2019-01…2023-12), **1800** metadata-only posts, 60 threads |
| Fixture row hash | `c6c6edb67ea5bc4eb6a966aa606c56681bc12d98ba043ed707f515dfa9b56847` (SHA-256 over canonical rows; stable across Windows/Linux/SQLite) |
| Dataset | `eval/datasets/agent_eval_cases.json` — **64** questions, **222** expected facts, 14 categories |
| Categories (count) | top_n 15, trend 10, comparison 6, region 5, cv_gap 4, ambiguous 4, out_of_scope 4, impossible 5, injection 2, role_filter 2, remote_filter 2, undersampled 2, adversarial 1, robustness 2 |
| Tools covered | `search_jobs`, `skill_counts`, `skill_trend`, `compare_skills`, `match_cv` |
| Provenance | every case is synthetic; ids/companies are generated and are **not** Hacker News data; the result is tagged `model=fake`, `source=synthetic_fixture`, `acceptance=not_real_acceptance` |

`expected_facts` are computed by independent SQL over the raw fixture tables in
`agent_dataset.py` (not by the tool functions under test), and the committed JSON is
regenerated and compared byte-for-byte in tests, so the counts cannot silently drift. The
fake model/planner is a deterministic **contract** double, not a measured real agent.

Region is a real filter: `search_jobs` takes a bounded `country` argument matched against
the extracted country list and returns it per row. A latent bug was fixed on the way — a
`date`-typed `month_to` bound dropped December on SQLite; a full-year window now returns
12 months (`test_full_year_window_includes_december`). The `remote=false` filter still
excludes `unknown`, verified on both fixtures.

| Check | Command | Result |
| --- | --- | --- |
| Regenerate dataset | `uv run skillpulse eval build-agent-cases` | writes `eval/datasets/agent_eval_cases.json` |
| Canonical contract run | `uv run skillpulse eval agent --dataset eval/datasets/agent_eval_cases.json --fixture-profile synthetic --source synthetic_fixture --out eval/results/agent_fixture_synthetic.json` | 64/64 correct, grounding 1.0, tool-selection 1.0, facts 222/222 |
| Regression gate | `tests/test_agent_eval_synthetic.py::test_regression_gate_allows_at_most_two_dropped_questions` | passes; tolerance 2 questions (`eval/baseline_agent.json`, SPEC §13) |
| Failure-sensitive controls | `tests/test_agent_eval_synthetic.py` | changed tool count, forged citation, count-cannot-prove-percent, unsupported number and below-30 suppression all fail as required, then are restored |

Limits of this round: nothing here is a real-model S3/S4 number. The harness supports a
real configured model later (off by default; only `SKILLPULSE_AGENT_*` settings and the
shared Redis budget, no key access or provider calls now) and records provider usage when
one is available — otherwise `null` with a reason. A real-model run with
temperature 0 is what the 2-question gate is meant to guard.
