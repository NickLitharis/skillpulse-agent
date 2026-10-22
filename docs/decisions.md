# Decisions

Short records of decisions that change the design. Newest last.

## D001 — Source: Hacker News "Who is hiring?" only (2026-10-15)

**Decision.** v1 uses one source: the official Hacker News API, reading the monthly
"Ask HN: Who is hiring?" threads posted by the `whoishiring` account.

**Why.** No API key, no published rate limit or terms against counting posts, and threads
go back to 2011, which gives 10+ years of trends on day one. Adzuna was rejected because its
terms forbid aggregated counts without written consent; Remotive was rejected for its
4-fetches-a-day limit. Arbeitnow (Europe) stays on the "later" list so v1 keeps one source
and one client.

**Verified (M1, 2026-10-15).** `user/whoishiring` returns 551 submitted items; the last 121
contain 45 "Ask HN: Who is hiring?" threads, one per month, with 376–592 top-level comments
each. So ~400–600 posts/month and ~60k+ posts in total, matching the spec's estimate.

**Consequence.** The README must state the bias of the data (US/remote-heavy, few European
on-site posts, almost no Greek ones) and credit Hacker News on every post link.

## D002 — Terms of use for the HN data (M1 check, 2026-10-15)

Status: D006 supersedes this earlier position. The notes below are a historical record,
not permission to resume live ingestion. Keeping only extracted fields does not itself
resolve the restriction on automated extraction.

**What was checked.** The official HN API README (`https://github.com/HackerNews/API`)
documents the endpoints and requires no key, but it does not publish a usage policy for
bulk reading; the site sits under Y Combinator's terms of service, and each post belongs
to its author.

**Decision.** Keep the spec's cautious position rather than assume a permissive licence:

- fetch at most 10 requests/second, well below anything that would look like abuse;
- store post text locally, keep it unexported, and use it only to re-run extraction;
- strip e-mails and phone numbers before storage; never store the HN username;
- publish only counts, metadata and `news.ycombinator.com/item?id=` links;
- credit Hacker News in the README as the source.

**Open question for the user.** If SkillPulse is ever made public with a live back-fill,
confirm whether storing full post text long-term is acceptable, or switch to storing only
extracted fields plus the post link. This needs the owner's call, not the agent's.

## D003 — Sync SQLAlchemy in workers, async only in the API (2026-10-15)

Matches SPEC §8.7. Running async DB code inside Celery's sync tasks is a known source of
event-loop bugs, so `db.py` exposes a sync engine and the API will add an async one in a
later milestone.

## D004 — Celery task bodies stay thin and share the CLI wiring (2026-10-15)

**Decision.** `workers/tasks.py` contains no fetching logic. Each task calls the same
`fetch_service` function the CLI calls, and both build their HN client and session factory
from the shared `runtime.py`. Discovery (`discover_threads`) only queues one `fetch_thread`
task per thread; it does not re-implement the sequential `backfill` loop.

**Why.** One implementation of the ingestion rules means the S5 (no duplicates) and S6
(resume after failure) behaviour proven in M1 holds for the workers too. Task bodies that
take their client and session factory as parameters are testable without Redis, and the
single real-worker smoke test then only has to prove the broker/worker path.

**Consequences.** Beat refreshes the current and previous month every 6 hours
(`SKILLPULSE_CELERY_UPDATE_INTERVAL_SECONDS`). A transient `HackerNewsError` is retried at
the task level with backoff, on top of the client's own HTTP retries; each retry resumes
from the last committed batch recorded in `fetch_runs`.

## D005 — Redis-wide politeness and an expiring per-thread lock (2026-10-15)

**Decision.** During a fetch a worker draws tokens from one Redis token bucket
(`skillpulse:hn:rate`, a Lua script using Redis `TIME` so worker processes agree on the
clock) instead of a per-process limiter, and holds an expiring Redis lock
(`skillpulse:lock:thread:<id>`, TTL `SKILLPULSE_THREAD_LOCK_TTL_SECONDS`, refreshed after
each committed batch). A worker that cannot take the lock skips the thread and returns
`status: "locked"` rather than waiting behind the holder.

**Why.** SPEC §8.5 and §8.6: the ceiling is 10 requests/second for the whole fleet, and two
runs on one thread must not overlap. A per-process limiter would allow N workers on N
processes to reach N×10 requests/second, and the lock has to outlive a crashed worker, so
it carries a TTL. Refreshing it per batch keeps a long fetch from losing a lock it still
needs while still freeing it if the worker dies. Renewal fails closed: if the worker no
longer owns the lock, `refresh()` raises `ThreadLockLostError`, the fetch stops and its
`fetch_runs` row is marked failed rather than continuing unprotected.

**Consequence.** Every worker HN client — the fetch task and both discovery tasks
(`discover_threads`, `refresh_recent_threads`) — goes through `tasks.worker_client()`, so
discovery and fetching share the one bucket. Ad-hoc CLI commands
(`skillpulse threads|fetch|backfill`) keep the in-process limiter so they still work
without Redis; that standalone path is intentional and not a worker.

## D006 — Live HN fetch gate: unresolved authorization (2026-10-15)

**Decision.** Live Hacker News access is gated. `runtime.build_client`, the single place
the CLI and the workers build their HN client, refuses unless
`SKILLPULSE_HN_LIVE_FETCH_ENABLED=true` (default `false`). The CLI exits 2 with the
reason; a Celery task raises `LiveFetchDisabledError` and is not retried.

**Why.** The official YC Terms of Use forbid copying, scraping or data-mining Site
Content without express authorization, and the HN API README states no explicit content
reuse licence; the YC User Content licence is not a grant to API consumers. So
authorization is unresolved and no live ingestion, back-fill or discovery runs. Record:
[docs/terms.md](terms.md).

**Not a clearance toggle.** Enabling the flag does not assert permission. It only opens
the gate after an explicit authorization decision is recorded in `docs/terms.md`. Tests
use in-process fakes or a local HTTP stub and set the variable explicitly where the real
worker path is exercised (`tests/test_workers_redis_smoke.py`); the stub is localhost,
not Hacker News.

## D007 — M3: taxonomy in YAML, baseline fallback, trend views, no real LLM yet (2026-10-15)

**Decision.** The taxonomy lives in `config/skills.yaml` (plus `role_families.yaml`) and
is validated on load; both extractors return the same Pydantic shape. The deterministic
baseline is the fallback, not a debug path: if the LLM is unconfigured, over budget, or
returns invalid JSON twice, the baseline result is stored with `is_fallback=true` so the
post still counts in trends and is retried next run. Trend numbers come from two
materialised views (`skill_trend_mv`, `skill_segment_mv`) that keep the numerator
(posts mentioning a skill) and denominator (posts with an extraction) side by side, and
a share is suppressed below 30 posts. The LLM provider is behind a `StructuredLLMClient`
protocol with prompt-injection isolation, wired to a real OpenAI-compatible adapter
(`providers.py`) and a daily token budget.

**Fleet budget (safety).** A process-local token counter is not a cost guard: N workers
would each spend the full daily limit. Real provider calls therefore use
`RedisDailyBudget` — an atomic `INCRBY`-in-Lua reserve on a UTC-day key — and
`build_extractor` **fails closed** (raises `LLMUnavailableError`) when the gate is open
but Redis or the base URL/model are missing. The gate is off by default and
`SKILLPULSE_LLM_API_KEY` is the only credential source. The offline baseline never needs
a budget or a network, so extraction stays usable with the gate closed.

**3-state remote and the denominator.** `skill_segment_mv` stores `remote_state` as
`'true'`/`'false'`/`'unknown'` rather than coalescing a null to `false`, so a
`remote=false` filter cannot silently include posts whose remote status was never
extracted. In every trend result `total_posts` means *eligible* posts — those with an
extraction for the requested extractor and taxonomy version — which is deliberately not
full-source coverage; `/v1/coverage` reports stored posts separately.

**Why.** SPEC sections 8.2 and 8.2.5. A fallback that is the same code as the baseline
means one tested path; storing the fallback flagged keeps extraction coverage visible
instead of dropping posts. Exact numerator/denominator in the views makes the "share of
posts" claim auditable and lets the API withhold small-sample shares without losing the
counts. No real LLM call is made in this round: there is no authorized provider
credential, so the LLM is exercised only through contract tests with a fake client.

**Consequence.** M3 is partial. The real-LLM F1 (S2) and the real-HN labelled set stay
blocked until M1 authorization and a provider credential exist. The synthetic baseline
F1 in `eval/results/baseline_synthetic.json` is explicitly synthetic and is not a
real-data S2 result.

## D008 — M4: read-only tools, a bounded LangGraph agent, fail-closed grounding (2026-10-15)

**Decision.** The five tools live once in `skillpulse.tools` and are shared by the agent
(and M5). Every tool takes a typed bounded Pydantic argument model, runs in a read-only
session, and uses fixed parameterized SQL; there is no raw-SQL parameter. Each result
carries a content-addressed `result_id`, the extractor and taxonomy version, the
eligible-post denominator definition and the coverage caveat. Migration 0003 creates a
`NOLOGIN` `skillpulse_readonly` role with SELECT only and no embedded credential.

**Agent.** A native `langgraph` `StateGraph` runs plan → tools → draft → grounding, with
explicit bounds: 6 tool calls, one grounding rewrite, a 60 s deadline and a cancellation
callback checked at every node. The model path reuses the M3 OpenAI-compatible client
and the shared `RedisDailyBudget`, and is off by default (`SKILLPULSE_AGENT_LLM_ENABLED`).

**Grounding.** A citation that names no real `result_id` fails closed. Only fields named
`share`/`rate`/`ratio` may prove a percent, so a count of `1` can never prove `100%`.
Structured claims (`result_id` + field path + value + unit) are validated against the
exact field and rendered from it; unsupported numbers are logged and blocked, then the
answer falls back to verified claims or raw tool rows.

**Why.** SPEC sections 9-11 and S4. A number may only come from a query, and a reviewer
must be able to trace it to an immutable tool output. Falling back to "the number appears
somewhere in the results" would let a fabricated citation or an unrelated count pass;
failing closed is the only honest option.

**Consequence.** M4 is partial: the fake-model fixture baseline is recorded, but the
real-model S3/S4 numbers need an explicit credential and shared budget and remain
blocked, so `eval/results/agent_fixture_fake.json` is never quoted as a real result.

## D009 — Real-model choice, chosen by screening rather than reputation (2026-10-16)

**Decision.** The v1 real-model results are produced with
`nvidia/nemotron-3-super-120b-a12b` over the NVIDIA NIM OpenAI-compatible endpoint.

**How it was chosen.** The key can see 81 models. Fourteen general-instruction candidates
(embedding, rerank, guard, code-only and vision models excluded) were probed once each
through the production `build_extractor`, and survivors were scored on an 8-post subset.
Only two were usable: `openai/gpt-oss-20b` and `nvidia/nemotron-3-super-120b-a12b`. The
rest timed out at 60 s and again at 120 s (`z-ai/glm-5.3`, `google/gemma-4-31b-it`,
`deepseek-ai/deepseek-v4.1-flash`, `nemotron-3.5-lightning-30b-a3b`), returned `503`
(`nemotron-3-nano-omni-30b-a3b-reasoning`), or returned `404` for a model the catalogue
still lists (`nemotron-3-ultra-550b`, `nemotron-nano-3-30b`, `kimi-k2.6`, every
`llama-3.1-nemotron-*`). `z-ai/glm-5.3-flash` answered correctly but took 70–81 s per
call. Screening numbers, which are **not** the official S2/S3/S4 results: extraction
micro-F1 0.9667 (gpt-oss-20b) versus 0.9492 (nemotron-3-super); agent correctness on the
8-case slice 0.75 versus 0.875. Nemotron was picked because the agent side is the harder
target (≥ 0.85) and both models clear extraction (≥ 0.85) comfortably.

## D010 — The planner must be shown its own argument schemas (2026-10-16)

**Decision.** `OpenAIAgentModel.plan()` sends each tool's `args_schema` to the model, along
with the exact role-family ids, the exact skill ids, the `month_from`/`month_to`
`YYYY-MM-DD` convention, and an instruction to ask back rather than guess a filter.

**The defect.** Until now `plan()` built its prompt from tool *names and descriptions* and
discarded the `args_schema` that `TOOL_SPECS` already provides. A real model therefore had
to guess argument names, produced `skill`/`year`/`location`/`filter`, and — because
`ToolFilters` allowed extra fields by default — the tool layer **silently dropped the
filter and returned rows for the wrong question**. Both candidate models scored exactly
`0.25` correctness, with the same `tool_failure`, which is what exposed it.

**Three further corrections came with it.** `ToolFilters` is now `extra="forbid"`, so an
unknown argument fails loudly instead of answering a different question. The graph logs the
real exception where it previously swallowed every failure into "A data tool failed" with no
cause. And a plan or draft that comes back empty or non-JSON is retried once before the run
is lost.

**Why this is not tuning.** The dataset, the fixture, the ground truth and the thresholds are
unchanged. The fake CI model carries a private `_year_window()` helper that converts a year
into a date window; the real model was never shown that contract. Correcting the interface is
what moves the number (`0.25` → `0.875` on the same 8 cases); whatever remains is reported as
it falls. `skill_trend` and `compare_skills` now also state which one a "compare" question
wants, because both models chose `skill_trend` for a single-skill comparison.

**Consequence.** The plan prompt is larger, so token cost per call rises; the shared
`RedisDailyBudget` still bounds it. `TOOL_SPECS` serves the MCP server as well, so the MCP
tool descriptions changed with it.

## D011 — Two evaluation defects fixed, five shortfalls kept (2026-10-16)

**Decision.** The committed real-model agent result is `correctness 0.9219` (59/64),
`grounding 1.0`, `tool-selection 0.9796`, `facts 214/222`. It follows two fixes to defects
in this project's own evaluation code and planner guidance. Nothing in the dataset, the
fixture, the ground truth or the pass thresholds changed, and the remaining failures
are reported as they fall rather than edited away.

**Correction (2026-10-22) — the scorer was too generous.** The 0.9219 above counted four
runs that had `error` set (three `tool_failure`, one `cancelled`) as correct because their
fallback text contains "cannot", and it excluded those four from the grounding denominator
while counting nine raw-table fallbacks as grounded. A run with `error` is now never correct
and a fallback is not a grounded draft. Rescored from the same committed JSON, the honest
headline is `correctness 0.8594` (55/64) and `grounding 0.7969` (51/64; 9 fallbacks and 4
failed runs are not grounded). The 51 real drafts are all grounded, so the draft-only rate
is 1.0 (51/51). S3 still clears its 0.85 target; S4's 0.85 is below its 0.98 target and the
README says so. No model call was repeated.

**Fix 1 — the adversarial case was unwinnable for a real model.** `adversarial_model ==
"invents"` passed only when the forbidden number reached the run's `unsupported` list, that
is, only when the model wrote the number and the grounding gate stripped it. Only the
deterministic inventing fake is ever told to invent anything, so a real model that answered
correctly without fabricating the number was scored wrong. A real run is now scored on the
normal criteria: the forbidden number must not appear in the answer, and the answer must
still be grounded and pass its tools and facts. The fake oracle is unchanged.

**Fix 2 — the clarify rule rejected answerable questions.** The planner was told to ask back
when the question named no role family, skill, region or time window. That rejected "most
requested skills in remote posts" (remote is a filter) and "my resume lists Python and SQL,
what are the gaps for data roles?" (data is a role family), and no rule said when to refuse,
so off-topic questions also got a clarifying question. The guidance now answers whenever at
least one filter or a time window is present, clarifies only when there is neither, and
refuses explicitly off-topic questions. Over the same 64 cases the two fixes move correctness
from 0.8125 to 0.9219.

**Kept shortfalls, not suppressed.** `region-nl-data` drops the `role_family` filter that the
question states. `cv-data` reaches for `skill_counts` where the question is about matching a
CV. `imp-2010` and `imp-2010-kubernetes` quote a zero (or answer "0") for a year the fixture
does not cover: the fixture spans 2019–2023, so the honest answer is that the window is
unsupported, and reporting zero implies a measurement that was never made. `imp-1999` answers
"insufficient sample (less than 30 posts)", which is correct in substance but does not match
the wording the check accepts. That check was deliberately left alone: four of the seven
insufficient-sample cases pass, so its vocabulary is reachable, and widening it further would
be editing the test to fit the answer.

**Consequence.** The README states the model, the endpoint and the agent timeout the run used,
so a reader can reproduce the number — or reproduce the failures. The timeout gap is recorded
rather than smoothed over: the run used 60 s while `.env.example` documented 20 s, and a 9-case
spot check at the documented 20 s passed 9/9 with no timeout, so the documented default is not
demonstrably wrong — but the committed numbers came from 60 s, and that is what the README says.

## D012 — A Compose stack runs the same image without a cluster (2026-10-16)

**Decision.** `docker-compose.yml` starts the whole application on one host: Postgres,
Redis, a one-shot `alembic upgrade head`, the API and the MCP server, with the Celery workers
and beat behind a `workers` profile. It runs the same image and the same container commands
as the Helm chart, so the two paths cannot drift in what they execute, and the API and MCP
wait on `service_completed_successfully` for the migration exactly as the chart's hook Job
orders them. The dependency-only services (`postgres`, `redis`) keep the host ports
(`5433`, `6379`) the rest of the developer workflow already uses.

**Why.** Kubernetes stays the deployment that is probed, smoke-tested and documented
(docs/deployment.md), but it needs k3d, kubectl, Helm and Terraform installed and running
before the first request is served. A single `docker compose up -d --build` makes the project
inspectable in one container runtime, which matters for a reader who wants to see the API and
the MCP tools answer a question rather than read about them.

**Consequences.** The workers are a profile, not a default: with every gate closed they do no
work, and idle Celery processes are a real cost on a laptop. The compose file opens no gate by
itself, so a containerised stack cannot start fetching Hacker News or calling a model that
`.env` did not already allow. `.env` is read for provider keys only — compose sets the
in-network URLs, which is what keeps a host-oriented `localhost:5433` value out of a
container. The image stays a runtime image (`src/`, `alembic/`, `config/`), so `make check`
and the CLI evaluations still run from the host.