# SkillPulse Agent — Specification

A job-market analyser: workers collect job posts from Hacker News' monthly "Who is
hiring?" threads, an LLM extracts the skills each post asks for, and a LangGraph agent
answers questions about skill demand and trends. The same tools are exposed through an
MCP server. Everything runs on Kubernetes, deployed with Helm, and checked by GitHub
Actions.

- **Owner:** Nikos Litharis
- **Status:** Draft v3 — 2026-10-16 (single source: Hacker News; Python 3.12)
- **Repo:** `github.com/NickLitharis/skillpulse-agent` (public, MIT licence for code)
- **Companion project:** MedLeaf RAG (Project 1) covers retrieval, evaluation and
  fine-tuning. This project covers agents, background processing, and the Kubernetes and
  Terraform deployment.

---

## 1. Problem

Job seekers read dozens of ads to work out which skills matter. Questions like *"Which
skills grew most in AI/ML posts over the last 12 months?"* or *"What share of remote
Python backend posts mention Kubernetes, and how has that changed since 2020?"* need
counts over many posts, not a single search.
Generic chatbots guess these numbers. Job boards show ads, not trends.

## 2. Goal

A self-hostable service that:
1. Collects the "Who is hiring?" threads on a schedule and keeps them up to date, without
   duplicates, over a bounded recent window.
2. Extracts a normalised skill list from each post, and measures the accuracy on a
   committed, reproducible dataset.
3. Answers natural-language questions with an agent whose every number comes from a
   database query, never from the model's memory.
4. Exposes the same read-only tools to any MCP client (Claude Desktop, IDEs, MCP Inspector).
5. Deploys to a local Kubernetes cluster with one command, the same way it would deploy to
   a real cluster.

Every quality number in the README must come from a command a reader can re-run.

## 3. Scope

### In scope
- One source: the official Hacker News API (§5).
- Celery workers + Celery beat on Redis for fetching, scheduled updates and extraction.
- Postgres for posts, skills and trend data; Alembic migrations.
- Skill extraction with a fixed taxonomy and a measured accuracy.
- LangGraph agent with tools, a step limit, and a grounding check (§9).
- MCP server with the same tools (§10).
- FastAPI service for the agent and the stats (§10).
- Helm chart, a k3d cluster and the Terraform setup, all installed by `make up`.
- GitHub Actions: lint, tests, chart checks, and a real deploy to k3d in CI.
- Prometheus metrics and optional Langfuse traces.

### Non-goals
- No scraping, and no second source in v1. Arbeitnow is "later" (§5).
- No web UI. FastAPI `/docs`, `curl` and MCP clients are enough.
- No accounts, auth, or multi-tenancy; the API is for `localhost` or a private cluster.
- No cloud cluster. Terraform targets any kube context, but only a local k3d cluster is
  actually deployed; a cloud deployment is "later".
- No fixed full historical back-fill; fetching is bounded on purpose (§8).
- No paid LLM in CI, and no real Hacker News text in any committed dataset.
- No storing personal data. CV text sent to `match_cv` is used in memory only (§9).
- No republishing of post text. Only aggregated stats and links leave the database.
- No code, data, prompts, or test cases from any employer or client project.

## 4. Success criteria

| # | Criterion | Target |
|---|-----------|--------|
| S1 | `make up` from a fresh clone: cluster, all pods ready, smoke test passes | < 20 min, no manual steps besides `.env` |
| S2 | Skill extraction on the committed labelled set (§11), one named model | micro-F1 ≥ 0.85; baseline reported next to it |
| S3 | Agent answer correctness on the committed agent set (§11), same model | ≥ 0.85 |
| S4 | Grounding: numbers in answers that match a tool result | ≥ 0.98; every miss logged as a defect |
| S5 | Fetching the same thread twice | 0 duplicate posts (integration test) |
| S6 | Hacker News API down or slow mid-run | Retried with backoff, run marked failed, next run resumes (test) |
| S7 | CI deploys the chart to k3d and runs the smoke test on every PR; green on `main` | Green on `main` |
| S8 | MCP server usable from MCP Inspector over stdio, with a recorded demo | README GIF |

S2–S4 run on datasets committed in the repository, because real Hacker News text is gated
(§5). Each result records the model, dataset and command. S2–S4 targets are starting
guesses: lock them after the first real run and note the change in `docs/decisions.md`.

## 5. Data sources

**Source: Hacker News "Ask HN: Who is hiring?" threads**, through the official API
(`https://hacker-news.firebaseio.com/v0/`, no key).

- The `whoishiring` account posts one thread on the first weekday of each month. Each
  top-level comment is one job post with a loose header, e.g.
  `Company | Role | Location | REMOTE | Salary`. Only titles starting with
  "Ask HN: Who is hiring?" are used; the account's other threads are ignored.
- **Volume (measured):** `user/whoishiring` returns 551 submitted items; the last 121 hold
  45 hiring threads, one per month, with 376–592 top-level comments each (~400–600 posts
  per month).
- **Terms:** the YC Terms of Use forbid copying, scraping or data-mining site content
  without express authorization, and the HN API README grants no reuse licence for post
  text. Authorization is therefore **unresolved**. Live access is gated (§8.8) and the
  record is in `docs/terms.md`; self-hosting without the gate is the supported path.
- **Attribution and limits:** every post carries
  `https://news.ycombinator.com/item?id=<id>`, and the README credits Hacker News. The
  data leans to US and remote roles, with few European on-site posts and almost no Greek
  ones: it reflects companies that post on Hacker News, not the whole market.
- **Privacy:** emails and phone numbers are stripped before storage, and the HN username
  (`by`) is not stored. Post text stays in the local database and is never exported.
- **Committed datasets are synthetic** — generated posts and metadata only — so they can
  be public, run in CI, and carry no personal data.
- **Later:** Arbeitnow as a Europe-focused second source (no key; must link back).

## 6. Architecture

```
                       Kubernetes namespace: skillpulse
 ┌───────────┐  scheduled  ┌──────────────┐  fetch   ┌──────────────┐
 │ celery    │────────────►│ worker:fetch │─────────►│ HN API       │
 │ beat      │             └──────┬───────┘          └──────────────┘
 └───────────┘                    │ upsert posts
                                  ▼
 ┌──────────────┐  queue  ┌──────────────┐  extract   ┌──────────────────┐
 │ Redis        │◄───────►│ worker:skills│───────────►│ LLM (OpenAI-      │
 └──────────────┘         └──────┬───────┘            │ compatible HTTP) │
                                 ▼                    └──────────────────┘
                          ┌──────────────┐ nightly refresh of trend views
                          │ Postgres     │
                          └──────┬───────┘
                  read-only SQL  │
           ┌─────────────────────┼─────────────────────┐
           ▼                                           ▼
  ┌─────────────────┐                        ┌──────────────────┐
  │ api (FastAPI)   │ ── LangGraph agent ──► │ tools module     │ ◄── mcp server
  └─────────────────┘                        └──────────────────┘
```

One Python package holds the tools, so the agent and the MCP server share one
implementation of each tool. Deployments: `api`, `mcp`, `worker-fetch`, `worker-skills`,
`beat` (exactly 1 replica), `redis`. Postgres runs through the CloudNativePG operator.

## 7. Tech stack

| Concern | Choice | Rejected alternative (why) |
|---------|--------|----------------------------|
| Agent | LangGraph | Plain LangChain agent (no explicit state or graph to show) |
| Tool protocol | MCP Python SDK (`mcp==2.3.0`), stdio + streamable HTTP | Custom REST tool API (not reusable by MCP clients) |
| API | FastAPI, async, Pydantic v2 | Flask |
| Database | Postgres 16+, SQLAlchemy 2, Alembic | SQLite (no concurrent workers) |
| Queue | Celery + Redis | RQ (fewer scheduling features), Kafka (too heavy) |
| LLM access | OpenAI-compatible `/chat/completions` over `httpx`, `response_format: json_object` | Direct provider SDK (lock-in). One adapter serves OpenAI, NVIDIA NIM, Ollama and a LiteLLM proxy |
| Container | Docker, multi-stage, non-root user | — |
| Cluster | k3d (k3s in Docker) | kind (similar; k3d has a built-in registry and load balancer) |
| Packaging | Helm 3 chart | Raw manifests (no templating or upgrades) |
| Postgres on k8s | CloudNativePG operator | Bitnami chart (image licensing changes in 2025), hand-written StatefulSet |
| Infra as code | Terraform (`kubernetes` + `helm` providers) | Pulumi (fewer job ads ask for it), a shell script (no plan and no state) |
| Metrics | `prometheus-client` `/metrics` | Full kube-prometheus-stack (too heavy for a laptop; optional) |
| Tracing | Langfuse (OTLP/HTTP, metadata only) | LangSmith (SaaS only) |
| Tests | pytest, testcontainers (Postgres) | Mocked DB (misses SQL bugs) |
| Lint / types | ruff, mypy (strict) | — |
| Packaging (Python) | uv | pip |
| CI | GitHub Actions | — |

## 8. Ingestion and skill extraction

### Fetching (`worker-fetch`)
1. **Find threads:** read the `submitted` list of user `whoishiring` and keep items whose
   title starts with "Ask HN: Who is hiring?". The month comes from the title.
2. **Bounded back-fill:** `skillpulse backfill` queues one task per thread, newest first,
   with a `--limit` so a run stays inside a chosen window.
3. **Updates (beat, every 6 hours):** re-fetch the current and previous month's threads.
   Posts arrive all month and authors edit or delete them; a changed text is
   re-extracted, a deleted or dead post is removed.
4. **Idempotent upsert** on the HN item id. The same company posting every month is *not*
   a duplicate: one post per company per month is the trend signal.
5. **Politeness:** at most 10 requests a second (token bucket in Redis), even though no
   limit is published. HTTP 429/5xx or timeouts → retry with exponential backoff, at most
   5 tries; then the run is marked `failed` in `fetch_runs`, and the next run resumes from
   the last fetched item.
6. A Redis lock with an expiry stops two runs on the same thread overlapping, and survives
   a crashed worker. A worker that cannot take the lock skips the thread.
7. Workers use a **sync** SQLAlchemy engine; only the API uses the async one. Async DB code
   inside Celery's sync tasks is a known source of event-loop bugs.
8. **Live access is gated:** `runtime.build_client` refuses unless
   `SKILLPULSE_HN_LIVE_FETCH_ENABLED=true` and a decision is recorded in `docs/terms.md`.
   The flag opens the gate; it is not a claim of permission.

### Skill extraction (`worker-skills`)
1. **Taxonomy:** `config/skills.yaml`, 336 skills in 13 categories with aliases, e.g.
   `kubernetes: {aliases: [k8s, kube], category: infra}`, plus 10 role families in
   `config/role_families.yaml`. The loader rejects duplicate ids and aliases. The LLM may
   only return skills from this list; anything else is dropped and counted as an
   "unknown term".
2. **Two extractors:**
   - *Baseline:* alias matching on Unicode word boundaries, with negation ("no
     Kubernetes") and nice-to-have rules. No LLM, free, runs in CI.
   - *LLM:* structured output with the taxonomy ids in the prompt:
     `{company, role_family, seniority, remote, countries[], skills: [{id, required}]}`.
     HN posts have no fixed fields, so the LLM also extracts role, location and remote
     status, and `required` separates "must have" from "nice to have".
   - Post text enters the prompt as untrusted data. The output must match the taxonomy
     schema and the extractor has no tools, so injected instructions cannot act.
3. Each post stores which extractor and taxonomy version produced its skills, so a new
   taxonomy can be re-run without mixing results.
4. LLM failures (timeout, invalid JSON after one retry) store the baseline result, flagged
   as a fallback; the post is retried in the next run.
5. **Cost guard:** real provider use requires a shared atomic Redis daily token budget. The
   builder fails closed when the gate is open but the base URL, model or Redis is missing,
   because a process-local counter is not a cost guard: N workers would each spend the full
   daily limit. Over budget, posts get the baseline until the next day. The baseline never
   needs a budget or a network.

### Trends
Materialised views refreshed nightly by a beat task (`REFRESH ... CONCURRENTLY`, which
needs a unique index on each view): skill counts per **thread month** × role family ×
remote/region, and the share of posts mentioning each skill. Shares, not raw counts, are
the main measure, because thread size changes a lot between years. `remote_state` has three
values (`true`/`false`/`unknown`), so `remote=false` cannot silently include posts whose
remote status was never extracted. Tools read the views, not the raw tables, so new posts
appear in the stats the next day. A share is withheld below 30 posts.

## 9. Agent

### Tools (read-only, typed inputs and outputs)
| Tool | Purpose |
|------|---------|
| `search_jobs(filters)` | Posts matching role, region, remote, skill, month range; max 20 rows |
| `skill_counts(filters, top_n)` | Most requested skills for a filter |
| `skill_trend(skill, filters, months)` | Monthly share of posts mentioning a skill |
| `compare_skills(skills[], filters)` | Side-by-side counts and trends |
| `match_cv(cv_text, filters)` | Skills in the CV vs. skills most asked for → gaps |

- Tools build SQL with SQLAlchemy and bound parameters; the LLM never writes SQL, and there
  is no raw-SQL parameter.
- Every query runs in a read-only session (`READ ONLY` on Postgres) with a statement timeout
  and a row cap. The tools' database role has `SELECT` rights only.
- Each result has a `result_id`, records the extractor and taxonomy version, and carries
  the eligible-post denominator and the coverage caveat.

### Graph (LangGraph)
```
question → plan → call tools (loop, max 6 tool calls) → draft answer → grounding check → answer
                                                                    └─ fail → one rewrite, then refuse
```
- **Plan:** the model picks tools and arguments. Filters are validated against known
  countries, role families and skill ids; unknown values make the agent ask back.
- **Grounding:** every number in the draft must appear in a tool result (allowing rounding
  and percent ↔ ratio) or in the question and filters (e.g. "last 12 months", "top 5"). A
  citation naming no real `result_id` fails closed. Only a typed `share`/`rate` field may
  prove a percentage, so a count of 1 can never prove 100%. A failure triggers one rewrite;
  if it still fails, the agent returns the tool rows as a table with no prose.
- **Answer format:** prose plus a `sources` list of `result_id`s and the filters used.
- **Limits:** 6 tool calls, 60 s overall, 20 s per LLM call. Hitting a limit returns what was
  found so far, marked partial.
- **Out-of-scope questions** → short refusal, no tool calls.
- **CV privacy:** `match_cv` finds CV skills with the baseline extractor (no LLM). CV text is
  never stored, never sent to Langfuse, and redacted from logs; the trace keeps only the
  extracted skill ids. The question itself still reaches the configured LLM, so the README
  points to the local Ollama option when pasting a real CV.

## 10. API and MCP

### FastAPI
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/ask` | Question → agent answer, streamed over SSE |
| `GET` | `/v1/skills/top` | Top skills for query-string filters |
| `GET` | `/v1/skills/{id}/trend` | Monthly trend for one skill |
| `GET` | `/v1/coverage` | Months covered, posts per month, last successful run, attribution |
| `GET` | `/healthz` | Process alive (liveness probe) |
| `GET` | `/readyz` | Postgres reachable (readiness probe) |
| `GET` | `/metrics` | Prometheus metrics |

SSE events for `/v1/ask`: `plan`, `tool_call` (name + args), `tool_result` (`result_id` +
row count), `token`, `done` (`trace_id`, `partial`, `grounded`), `error`.

Boundary errors: missing model config → `503`; LLM failure → `error` event; Postgres down →
`503` and `/readyz` fails; client disconnect → the graph run is cancelled. Internal errors
and credentials are never returned.

### MCP server
- The same five tools and schemas, with read-only access and no agent inside: the MCP
  client's own model reasons. There is no MCP-only query path, and no SQL, filesystem or
  shell tool.
- Built on the official SDK (`mcp==2.3.0`, `MCPServer`), not a hand-rolled protocol. Typed
  bounded arguments mirror the shared Pydantic models, and the `inputSchema` matches them.
- Transports: stdio (for Claude Desktop, via `skillpulse-mcp`) and streamable HTTP (the
  `mcp` Deployment). HTTP binds loopback with DNS-rebinding protection on (bad `Host` →
  421, bad `Origin` → 403) and refuses a non-loopback bind unless explicitly allowed.
- Errors are sanitised to safe tool errors; the database DSN is never returned.
- One resource: `skillpulse://taxonomy`, so clients can pick valid skill ids.

## 11. Evaluation

### Skill extraction set
- `eval/datasets/synthetic_extraction.json`: 40 synthetic posts written in HN style, each
  labelled by hand with taxonomy ids, `required` flags, role family and remote status.
- Synthetic, so it is public, needs no network, and carries no personal data. A model run
  reads the same file, so the number is reproducible.
- Metrics: micro and macro precision/recall/F1, per-category F1, `required` accuracy,
  unknown-term rate, cost per post — baseline and LLM in one table.
- The baseline is already committed at micro-F1 0.9910 / macro-F1 0.9783
  (`eval/results/baseline_synthetic.json`). The LLM F1 is the S2 number.

### Agent set
- `eval/datasets/agent_eval_cases.json`: 64 questions and 222 expected facts, generated by
  `src/skillpulse/eval/agent_dataset.py` and committed.
- Run against a **frozen fixture database**: 2019–2023, 60 months, 1800 metadata-only posts
  with a fixed fixture version and a SHA-256 semantic row hash, so expected answers never
  change. It holds no post text, so it can be public and run in CI.
- `expected_facts` come from SQL over the raw fixture tables, not from the tools under test,
  e.g. `{"skill": "kubernetes", "share": 0.31}`. Each fact names the tool whose result must
  carry it, so a case cannot pass by skipping that tool.
- Categories: top-N, trend, comparison, region/role/remote, CV gap, ambiguous (the agent
  should ask back), out of scope (should refuse), impossible (the data is unavailable, e.g.
  a country with too few posts — the agent must say the sample is too small instead of
  quoting a share), injection, and adversarial.
- The agent must not quote a share based on fewer than 30 posts; tools return the count
  behind every share.
- Metrics: answer correctness (all expected facts present, numbers within rounding), tool
  selection accuracy, grounding rate (S4), steps and tokens per question, p95 latency.
- A separate 9-case smoke set runs on a tiny fixture with the deterministic fake model, so
  CI stays fast and keyless.

### Runner
- `skillpulse eval extraction` and `skillpulse eval agent` write
  `eval/results/<run>.json` with the model, dataset, source and provenance.
- A real-model run needs `SKILLPULSE_LLM_*` / `SKILLPULSE_AGENT_LLM_*` plus Redis, and the
  result file names the model. PR checks use the fake model and need no credential.
- The README results table is filled from the committed JSON files.

## 12. Kubernetes, Helm and Terraform

### Chart (`deploy/helm/skillpulse`)
- One chart with values for `local` and `ci`.
- Every Deployment has resource requests/limits, liveness/readiness probes, a non-root
  security context and a read-only root filesystem where possible.
- `beat` is fixed at 1 replica with the `Recreate` strategy; two beats, even briefly during
  a rollout, would schedule every task twice.
- A `pre-install,pre-upgrade` hook Job runs `alembic upgrade head` before new pods start. It
  uses the owner role; the tools' read-only role comes from a migration, and the API and MCP
  logins are separate.
- A HorizontalPodAutoscaler on `worker-skills` by CPU (queue length would be the better
  signal, noted as later work).
- NetworkPolicy: only `api`, `mcp`, workers, beat and the migration Job reach Postgres and
  Redis. k3s enforces NetworkPolicy out of the box, and the CloudNativePG operator also
  needs access to its Postgres pods.

### Terraform (`infra/`)
- Providers: `kubernetes` and `helm`, pointed at a kube-context variable.
- Creates: the namespace, the CloudNativePG operator (Helm release), the Postgres
  `Cluster`, Redis, and the `skillpulse` Helm release.
- **Secrets stay out of Terraform state:** `make secrets` creates the Kubernetes Secret from
  `.env` with `kubectl`; Terraform and Helm only reference its name.
- State is local (`infra/terraform.tfstate`, gitignored). A remote backend is later work.

### Local flow
`make up` = create the k3d cluster with a local registry → build and push images →
`make secrets` → `terraform apply` → run the smoke test. `make down` deletes the cluster.

### Compose (`docker-compose.yml`)
- A single-host alternative to the cluster: the same image and the same container commands,
  with Postgres, Redis, a one-shot `alembic upgrade head`, the API and the MCP server.
- The Celery workers and beat sit behind the `workers` profile. They only do work once a
  gate is open, so they are not part of a default `docker compose up`.
- Application containers keep the chart's hardening: non-root, read-only root filesystem,
  no capabilities, no privilege escalation, `/tmp` the only writable path.
- Compose sets the in-network database and Redis URLs, so a host-oriented `.env`
  (`localhost:5433`, `localhost:6379`) cannot leak into a container. It opens no gate.
- It is not the evaluation path: the image ships `src/`, `alembic/` and `config/` only, and
  `make check` and the CLI evaluations run from the host. Kubernetes stays the path CI
  installs and smoke-tests.

## 13. CI (GitHub Actions)

| Workflow | Trigger | Steps | Secrets |
|----------|---------|-------|---------|
| `ci.yml` | every PR / push to `main` | ruff, mypy (strict), pytest (unit + testcontainers integration) | none |
| `deploy-check.yml` | every PR | `helm lint`, `helm template` + kubeconform, `terraform fmt -check` + `validate`, build images, create k3d, `terraform apply`, smoke test (`/readyz`, one tool call through MCP HTTP) | none (fake LLM, fixture DB) |
| `release.yml` | tag `v*` | build and push images to GHCR, package the chart | `GITHUB_TOKEN` |

PR checks need no paid API, so they are free and deterministic. `deploy-check.yml` enforces
a 20-minute step deadline and saves the timing and smoke evidence as artifacts.

## 14. Skills covered (from job-tracker gap analysis)

| Job-ad skill | Where |
|--------------|-------|
| Kubernetes, Helm | §12, deployed in CI |
| Terraform | §12, `infra/`, run by `make up` and by CI |
| CI/CD (GitHub Actions) | §13, including a real cluster deploy |
| LangGraph / agents | §9 |
| MCP | §10 |
| FastAPI / async Python | §10 |
| Postgres, SQL, migrations | §8, materialised views, Alembic |
| Redis / Celery | §8 |
| LLM evaluation | §11 |
| Observability | Prometheus metrics, Langfuse traces |
| Docker | Multi-stage images, non-root |

Not covered here, on purpose: vector databases, RAG and fine-tuning (Project 1).

## 15. Milestones

Each milestone is one or more small PRs with its acceptance check.

| M | Deliverable | Acceptance |
|---|-------------|------------|
| R1 | Wire the real model into both eval commands; add the missing `SKILLPULSE_LLM_*` block to `.env.example` | A named model produces F1 (S2), correctness (S3) and grounding (S4) on the committed datasets; results JSON committed and the model named in the README |
| R2 | Green `main`: `ci.yml` and `deploy-check.yml` pass on `main`; `make up` installs end to end on a clean cluster | S1 and S7 met; `release.yml` on tag `v1.0.0` |
| R3 | Trim the paperwork: one README, SPEC v3 as the only design document, no acceptance-audit docs, no PowerShell preflight harnesses | A reader can run the quick start and re-run every README number from the committed datasets |

## 16. Risks

| Risk | Mitigation |
|------|------------|
| HN terms restrict reuse | Live access stays behind the gate; committed datasets are synthetic, so the project is publishable without it |
| Real numbers need a paid API | One evaluation run with a named model; CI stays keyless on the fake model |
| HN API changes or disappears | Contract tests on recorded responses; stored data keeps the project working |
| Free-text posts vary in format | Schema-constrained LLM extraction, baseline fallback, a labelled set across role families |
| LLM invents numbers | Tools are the only source of numbers; grounding check; S4 |
| Scope creep (UI, cloud, auth) | Non-goals in §3 and the cut list in §18 |

## 17. CV line

> **SkillPulse Agent** — Job-market analysis agent over Hacker News hiring threads
> (LangGraph, MCP, FastAPI, Postgres, Celery/Redis, Kubernetes/Helm, Terraform,
> GitHub Actions).
> Scored 0.9552 micro-F1 on LLM skill extraction and 0.8594 answer correctness (55/64),
> with 51 of 64 runs producing a grounded draft (9 raw-table fallbacks and 4 failed runs
> counted as not grounded; all 51 real drafts are grounded), over a committed 64-question
> evaluation fixture; the deterministic alias baseline scores 0.991 on the same synthetic
> set, which is built from taxonomy aliases and therefore favours it. Every PR deploys the
> full stack to a Kubernetes cluster in CI.

## 18. Deliberately out of scope

| Cut | Why |
|-----|-----|
| Full back-fill since 2011 | Hours of runtime behind the terms gate. A bounded, repeatable window tells the same story. |
| A 150-post hand-labelled real-HN set | Days of labelling behind an unresolved permission gate. The synthetic set measures the same thing, publicly. |
| Claude Desktop acceptance | Needs the GUI and a global client-config edit. MCP Inspector over stdio proves the server works. |
| A weekly paid-key `eval-full.yml` | CI must stay free and deterministic. The real numbers come from one committed run. |
| HPA by queue length, remote Terraform state, cloud cluster | Named as later work, not promises. |
