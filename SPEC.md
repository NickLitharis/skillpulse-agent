# SkillPulse Agent — Specification

A job-market analyser: background workers collect the job posts from Hacker News'
monthly "Who is hiring?" threads, an LLM extracts the
skills each post asks for, and a LangGraph agent answers questions about skill demand and
trends. The same tools are exposed through an MCP server. Everything runs on Kubernetes,
deployed with Helm and Terraform, and checked by GitHub Actions.

- **Owner:** Nikos Litharis
- **Status:** Draft v2 — 2026-10-15 (single source: Hacker News)
- **Repo:** `github.com/NickLitharis/skillpulse` (public, MIT licence for code)
- **Companion project:** MedLeaf RAG (Project 1) covers retrieval, evaluation and
  fine-tuning. This project covers agents, background processing and infrastructure.

---

## 1. Problem

Job seekers read dozens of ads to work out which skills matter. Questions like *"Which
skills grew most in AI/ML posts over the last 12 months?"* or *"What share of remote
Python backend posts mention Kubernetes, and how has that changed since 2020?"* need
counts over many posts, not a single search.
Generic chatbots guess these numbers. Job boards show ads, not trends.

## 2. Goal

A self-hostable service that:
1. Back-fills every "Who is hiring?" thread since 2011, then keeps the current threads
   up to date on a schedule, without duplicates.
2. Extracts a normalised skill list from each post and measures how accurate that is.
3. Answers natural-language questions with an agent whose every number comes from a
   database query, never from the model's memory.
4. Exposes the same read-only tools to any MCP client (Claude Desktop, IDEs, MCP Inspector).
5. Deploys to a local Kubernetes cluster with one command, the same way it would deploy
   to a real cluster.

Portfolio goal: publicly demonstrate the infrastructure and agent skills most requested
in job ads (see §14), which Project 1 leaves out on purpose.

## 3. Scope

### In scope
- One source: the official Hacker News API (§5).
- Celery workers + Celery beat on Redis for back-fill, scheduled updates and skill
  extraction.
- Postgres for posts, skills and trend data; Alembic migrations.
- Skill extraction with a fixed skill taxonomy and a measured accuracy.
- LangGraph agent with tools, a step limit, and a grounding check (§9).
- MCP server with the same tools (§10).
- FastAPI service for the agent and the stats (§10).
- Helm chart, k3d local cluster, Terraform for the cluster-level setup.
- GitHub Actions: lint, tests, chart checks, and a real deploy to k3d in CI.
- Prometheus-format metrics and Langfuse traces.

### Non-goals
- No scraping. Only the documented Hacker News API (§5).
- No second job source in v1. Arbeitnow (Europe) is on the "later" list (§5).
- No web UI. FastAPI `/docs`, `curl` and MCP clients are enough.
- No accounts, auth, or multi-tenancy. The API is meant for `localhost` or a private
  cluster; the README says so.
- No paid cloud deployment. Terraform targets any kube context; a cloud cluster is a
  "later" item.
- No storing personal data. CV text sent to `match_cv` is used in memory only (§9).
- No republishing of post text. Only aggregated stats and links leave the database.
- No code, data, prompts, or test cases from any employer or client project.

## 4. Success criteria

| # | Criterion | Target |
|---|-----------|--------|
| S1 | `make up` from a fresh clone: k3d cluster, all pods ready, smoke test passes | < 20 min, no manual steps besides `.env` |
| S2 | Skill extraction on the labelled set (§11) | micro-F1 ≥ 0.85; regex baseline reported next to it |
| S3 | Agent answer correctness on the agent set (§11) | ≥ 0.85 |
| S4 | Grounding: numbers in answers that match a tool result | ≥ 0.98; every miss logged as a defect |
| S5 | Fetching the same thread twice | 0 duplicate posts (integration test) |
| S6 | Hacker News API down or slow mid-run | Retried with backoff, run marked failed, next run resumes where it stopped (test) |
| S7 | CI deploys the Helm chart to k3d and runs the smoke test on every PR | Green on `main` |
| S8 | MCP server usable from MCP Inspector and Claude Desktop | Shown in README with a GIF |

S2–S4 targets are starting guesses. Lock them after the first baseline run (M4) and
record the change in `docs/decisions.md`.

## 5. Data sources

**Source: Hacker News "Ask HN: Who is hiring?" threads**, read through the official
Hacker News API (`https://hacker-news.firebaseio.com/v0/`, no key).

- **How it works:** the `whoishiring` account posts one thread on the first weekday of
  each month. Each top-level comment is one job post, usually starting with a loose
  header like `Company | Role | Location | REMOTE | Salary`. The same account also
  posts "Who wants to be hired?" and "Freelancer?" threads; only threads whose title
  starts with "Ask HN: Who is hiring?" are used.
- **Why this source:**
  - One source, no key, and no published rate limit or terms against counting posts
    (unlike Adzuna, rejected because its terms forbid aggregated counts without
    written consent, and Remotive, rejected for its 4-fetches-a-day limit).
  - Monthly threads since 2011 give 10+ years of trends on day one.
  - Tech-focused posts, i.e. exactly the target job market.
- **Volume:** roughly 300–500 posts a month, so ~5k a year and ~60k+ in total
  (measure in M1).
- **Terms (verify in M1):** the API README sets no usage terms, but the site falls under
  Y Combinator's terms and each post belongs to its author. So post text stays in the
  local database; only counts, metadata and links leave it.
- **Attribution:** every post returned by the API, MCP tools or agent answers carries its
  link, `https://news.ycombinator.com/item?id=<id>`; the README credits Hacker News.
- **Personal data:** posts often hold a recruiter's name, email or phone. Emails and phone
  numbers are stripped before the text is stored; the HN username (`by`) is not stored.
- **Limits, stated in the README:** the data leans toward the US and remote roles, has
  few European on-site posts, and almost no Greek ones. It reflects companies that
  post on Hacker News, not the whole market.
- **Storage:** stored fields are post id, thread month, time, cleaned text, and the
  extracted fields (§8). Text is kept to allow re-extraction and never exported.
- **Later:** Arbeitnow as a second, Europe-focused source (no key; must link back to
  arbeitnow.com). Not in v1, to keep one source and one client.

## 6. Architecture

```
                       Kubernetes namespace: skillpulse
 ┌───────────┐  scheduled  ┌──────────────┐  fetch   ┌──────────────┐
 │ celery    │────────────►│ worker:fetch │─────────►│ HN API       │
 │ beat      │             └──────┬───────┘          └──────────────┘
 └───────────┘                    │ upsert posts
                                  ▼
 ┌──────────────┐  queue  ┌──────────────┐  extract   ┌─────────┐
 │ Redis        │◄───────►│ worker:skills│───────────►│ LLM     │ (LiteLLM)
 └──────────────┘         └──────┬───────┘            └─────────┘
                                 ▼
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

One Python package holds the tools. The agent and the MCP server both call that same
module, so there is one implementation of each tool.

Deployments: `api`, `mcp`, `worker-fetch`, `worker-skills`, `beat` (exactly 1 replica),
`redis`. Postgres runs through the CloudNativePG operator.

## 7. Tech stack

| Concern | Choice | Rejected alternative (why) |
|---------|--------|----------------------------|
| Agent | LangGraph | Plain LangChain agent (no explicit state or graph to show) |
| Tool protocol | MCP Python SDK (`FastMCP`), stdio + streamable HTTP | Custom REST tool API (not reusable by MCP clients) |
| API | FastAPI, async, Pydantic v2 | Flask |
| Database | Postgres 16, SQLAlchemy 2 (async), Alembic | SQLite (no concurrent workers) |
| Queue | Celery + Redis | RQ (fewer scheduling features), Kafka (too heavy) |
| LLM access | LiteLLM, structured output (JSON schema) | Direct SDK (provider lock-in) |
| Container | Docker, multi-stage, non-root user | — |
| Cluster | k3d (k3s in Docker) | kind (similar; k3d has a built-in registry and load balancer) |
| Packaging | Helm 3 chart | Raw manifests (no templating or upgrades) |
| Postgres on k8s | CloudNativePG operator | Bitnami chart (image licensing changes in 2025), hand-written StatefulSet |
| Infra as code | Terraform with `kubernetes` + `helm` providers | Pulumi (fewer job ads ask for it) |
| Metrics | `prometheus-client` `/metrics` | Full kube-prometheus-stack (too heavy for a laptop; optional) |
| Tracing | Langfuse | LangSmith (SaaS only) |
| Tests | pytest, testcontainers (Postgres, Redis) | Mocked DB (misses SQL bugs) |
| Lint / types | ruff, mypy (strict) | — |
| Packaging (Python) | uv | pip |
| CI | GitHub Actions | — |

## 8. Ingestion and skill extraction

### Fetching (`worker-fetch`)
1. **Find threads:** read the `submitted` list of user `whoishiring` and keep items
   whose title starts with "Ask HN: Who is hiring?". The month comes from the title.
2. **Back-fill (once, `skillpulse backfill`):** queue one task per thread since 2011;
   each task fetches the thread's top-level comments (`kids`). About 60k+ item requests
   in total, so it runs over a few hours at a polite rate.
3. **Updates (beat, every 6 hours):** re-fetch the current and previous month's threads.
   Posts arrive all month and authors edit or delete them; a changed text is
   re-extracted, a deleted or dead post is removed.
4. **Idempotent upsert** on the HN item id. The same company posting every month is
   *not* a duplicate: one post per company per month is the trend signal. Two posts
   by the same company in one thread are both kept.
5. **Politeness:** at most 10 requests a second (token bucket in Redis), even though no
   limit is published. HTTP 429/5xx or timeouts → retry with exponential backoff, at
   most 5 tries; then the run is marked `failed` in `fetch_runs`, and the next run
   resumes from the last fetched item.
6. A lock in Redis (with an expiry, so a crashed worker does not hold it forever)
   stops two runs on the same thread overlapping.
7. Workers use a **sync** SQLAlchemy engine; only the API uses the async one. Running
   async database code inside Celery's sync tasks is a known source of event-loop bugs.

### Skill extraction (`worker-skills`)
1. **Taxonomy:** `config/skills.yaml`, ~300 skills with aliases and a category, e.g.
   `kubernetes: {aliases: [k8s, kube], category: infra}`. The LLM may only return
   skills from this list; anything else is dropped and counted as "unknown term".
2. **Two extractors:**
   - *Baseline:* alias matching with word boundaries. No LLM, free, runs in CI.
   - *LLM:* structured output with the taxonomy ids in the prompt:
     `{company, role_family, seniority, remote, countries[], skills: [{id, required}]}`.
     HN posts have no fixed fields, so the LLM also extracts role, location and remote
     status; `required` separates "must have" from "nice to have". The baseline extracts
     skills and the `REMOTE` flag only.
3. Each post stores which extractor and taxonomy version produced its skills, so a new
   taxonomy can be re-run without mixing results.
4. LLM failures (timeout, invalid JSON after 1 retry) → baseline result is stored and
   flagged; the post is retried in the next run.
5. **Cost guard:** a daily LLM token budget in config for ongoing updates; when it is
   reached, posts get the baseline only until the next day. The 60k-post back-fill is a
   one-off job with its own budget (`skillpulse extract --backfill --max-cost <USD>`):
   estimate the cost on 500 posts in M3, then run it all or limit it to recent years.

### Trends
Materialised views refreshed nightly by a beat task (`REFRESH ... CONCURRENTLY`, which
needs a unique index on each view): skill counts per **thread month** × role family ×
remote/region, and share of posts mentioning each skill. Shares, not raw counts, are the
main measure, because thread size changes a lot between years. Tools read the views,
not the raw tables, so new posts show up in stats the next day.

## 9. Agent

### Tools (read-only, typed inputs and outputs)
| Tool | Purpose |
|------|---------|
| `search_jobs(filters)` | Posts matching role, region, remote, skill, month range; max 20 rows |
| `skill_counts(filters, top_n)` | Most requested skills for a filter |
| `skill_trend(skill, filters, months)` | Monthly share of posts mentioning a skill |
| `compare_skills(skills[], filters)` | Side-by-side counts and trends |
| `match_cv(cv_text, filters)` | Skills in the CV vs. skills most asked for → gaps |

- Tools build SQL with SQLAlchemy and bound parameters; the LLM never writes SQL.
- The database role used by the tools has `SELECT` rights only.
- Each tool result has a `result_id` and every number in it is recorded.

### Graph (LangGraph)
```
question → plan → call tools (loop, max 6 tool calls) → draft answer → grounding check → answer
                                                                   └─ fail → one rewrite, then refuse
```
- **Plan:** the model picks tools and arguments; filters are validated against known
  countries, role families and skill ids. Unknown values → the agent asks back.
- **Grounding check:** every number in the draft must appear in a tool result
  (allowing rounding and percent ↔ ratio) or in the question and filters (e.g. "last
  12 months", "since 2020", "top 5"). Failing numbers trigger one rewrite; if it
  still fails, the agent returns the tool results as a table with no prose.
- **Answer format:** prose plus a `sources` list of `result_id`s and the filters used.
- **Limits:** 6 tool calls, 60 s overall timeout, 20 s per LLM call. Hitting a limit
  returns what was found so far, marked partial.
- **Out-of-scope questions** (not about the job market) → short refusal, no tool calls.
- **CV privacy:** `match_cv` finds CV skills with the baseline extractor (no LLM). CV
  text is never stored, never sent to Langfuse, and redacted from logs; the trace keeps
  only the extracted skill ids. The question itself still goes to the configured LLM,
  so the README says to use the local Ollama option when pasting a real CV.

## 10. API and MCP

### FastAPI
| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/ask` | Question → agent answer, streamed over SSE |
| `GET` | `/v1/skills/top` | Top skills for query-string filters |
| `GET` | `/v1/skills/{id}/trend` | Monthly trend for one skill |
| `GET` | `/v1/coverage` | Months covered, posts per month, last successful run, attribution |
| `GET` | `/healthz` | Process alive (liveness probe) |
| `GET` | `/readyz` | Postgres + Redis reachable (readiness probe) |
| `GET` | `/metrics` | Prometheus metrics |

SSE events for `/v1/ask`: `plan`, `tool_call` (name + args), `tool_result`
(`result_id` + row count), `token`, `done` (`trace_id`, `partial`, `grounded`), `error`.

Errors at the boundary: LLM failure → `error` event; Postgres down → `503` and
`/readyz` fails; client disconnect → the graph run is cancelled.

### MCP server
- Same five tools, same schemas, same code (§6). Read-only, no agent inside: the MCP
  client's own model does the reasoning.
- Transports: stdio (for Claude Desktop, via `uvx skillpulse-mcp`) and streamable HTTP
  (the `mcp` Deployment).
- One MCP resource: `skillpulse://taxonomy` (the skill list), so clients can pick valid ids.

## 11. Evaluation

### Skill extraction set
- ~150 posts sampled across years and role families, labelled by hand with taxonomy
  ids, `required` flags, role family and remote status.
- The public repo stores **HN item ids + labels only**, no post text. A script fetches
  the text from the HN API, so anyone can rebuild the set. Posts later edited or
  deleted are detected (text hash stored with the labels) and left out of scoring.
- CI does not depend on the HN API: it uses a second, public set of ~40 synthetic posts
  written by an LLM in HN style and checked by hand. README numbers come from the real
  set, run locally, with the results JSON committed.
- Metrics: micro and macro precision/recall/F1, per-category F1, `required`
  accuracy, unknown-term rate, cost per post. Baseline vs LLM in one table.

### Agent set
- ~60 questions, run against a **frozen fixture database** (~5 years of posts with
  fixed extracted fields), so expected answers never change. It holds only item id,
  month, company, role, remote/region and skills, no post text, so it can be public
  and used in CI.
- LLM calls use temperature 0; non-determinism that remains is why the gate allows a
  2-question drop (§13).
- Each record: `id, question, expected_tools[], expected_facts[], must_refuse`.
  `expected_facts` are values computed by SQL on the fixture DB, e.g.
  `{"skill": "kubernetes", "share": 0.31}`.
- Categories: top-N, trend direction, comparison, filter by region/role/remote, CV gap,
  ambiguous (agent should ask back), out of scope (should refuse), impossible
  (data not available, e.g. before 2011, or a country with too few posts — the agent
  must say the sample is too small instead of quoting a share).
- Tools return the post count behind every share, and the agent must not quote a share
  based on fewer than 30 posts.
- Metrics: answer correctness (all expected facts present, numbers within rounding),
  tool selection accuracy, grounding rate (S4), steps and tokens per question, p95
  latency.

### Runner
`skillpulse eval extraction|agent --config configs/eval.yaml` writes
`eval/results/<run>.json`; a second command renders the README results table.

## 12. Kubernetes, Helm and Terraform

### Helm chart (`deploy/helm/skillpulse`)
- One chart, values for `local` and `ci`.
- Every Deployment has resource requests/limits, liveness/readiness probes, a
  non-root security context and a read-only root filesystem where possible.
- `beat` is fixed at 1 replica with the `Recreate` rollout strategy (two beats, even
  briefly during a rollout, would schedule every task twice).
- A `pre-install,pre-upgrade` Helm hook Job runs `alembic upgrade head` before new pods
  start. It uses the owner database role; the tools' read-only role is created by a
  migration.
- HorizontalPodAutoscaler on `worker-skills` by CPU (shows autoscaling; the queue
  length would be the better signal, noted as later work).
- NetworkPolicy: only `api`, `mcp`, workers, beat and the migration Job may reach
  Postgres and Redis (k3s enforces NetworkPolicy out of the box; the CloudNativePG
  operator also needs access to its Postgres pods).

### Terraform (`infra/`)
- Providers: `kubernetes` and `helm`, pointed at a kube context variable.
- Creates: namespace, CloudNativePG operator (Helm release), Postgres `Cluster`
  resource, Redis, and the `skillpulse` Helm release.
- **Secrets stay out of Terraform state:** `make secrets` creates the Kubernetes
  Secret from `.env` with `kubectl`; Terraform and Helm only reference its name.
- State is local (`terraform.tfstate`, gitignored). A remote backend is later work.

### Local flow
`make up` = create k3d cluster with a local registry → build and push images →
`make secrets` → `terraform apply` → run smoke test. `make down` deletes the cluster.

## 13. CI (GitHub Actions)

| Workflow | Trigger | Steps | Secrets |
|----------|---------|-------|---------|
| `ci.yml` | every PR / push | ruff, mypy, pytest (unit + testcontainers integration), baseline extraction eval on the public synthetic set, agent tests with a fake LLM | none |
| `deploy-check.yml` | every PR | `helm lint`, `helm template` + kubeconform, `terraform fmt -check` + `validate`, build images, create k3d, `terraform apply`, smoke test (`/readyz`, one tool call through MCP HTTP) | none (fake LLM, fixture DB) |
| `eval-full.yml` | manual + weekly | LLM extraction eval + agent eval on the fixture DB; **fails if correctness or grounding drops by more than 2 questions vs `eval/baseline.json`** | `OPENAI_API_KEY` |
| `release.yml` | tag `v*` | build and push images to GHCR, package the chart | `GITHUB_TOKEN` |

- PR checks need no paid API, so they are free and deterministic.
- Updating `eval/baseline.json` is its own PR.

## 14. Skills covered (from job-tracker gap analysis)

| Job-ad skill | Where in this project |
|--------------|-----------------------|
| Kubernetes, Helm | §12, deployed in CI |
| Terraform | §12 |
| CI/CD (GitHub Actions) | §13, including a real cluster deploy |
| LangGraph / agents | §9 |
| MCP | §10 |
| FastAPI / async Python | §10 |
| Postgres, SQL, migrations | §8, materialised views, Alembic |
| Redis / Celery | §8 |
| LLM evaluation | §11 |
| Observability | Prometheus metrics, Langfuse traces |
| Docker | Multi-stage images, non-root |

Not covered here, on purpose: vector databases, RAG, fine-tuning → Project 1.

## 15. Repository layout

```
skillpulse/
├── src/skillpulse/
│   ├── hn/            # Hacker News client, thread finder, text cleaning
│   ├── workers/       # Celery app, beat schedule, fetch + extract tasks
│   ├── extraction/    # taxonomy loader, baseline and LLM extractors
│   ├── db/            # SQLAlchemy models, views, read-only session
│   ├── tools/         # the five tools (shared by agent and MCP)
│   ├── agent/         # LangGraph graph, grounding check
│   ├── api/           # FastAPI app, SSE
│   ├── mcp/           # MCP server
│   ├── eval/          # scorers, runner, report
│   └── config.py      # pydantic-settings, env vars only
├── config/            # skills.yaml, role_families.yaml
├── migrations/        # Alembic
├── eval/
│   ├── datasets/      # HN item ids + labels, agent questions (no post text)
│   ├── fixtures/      # fixture DB build script
│   ├── baseline.json
│   └── results/       # gitignored except the latest summary
├── deploy/helm/skillpulse/
├── infra/             # Terraform
├── tests/
├── docs/decisions.md
├── Dockerfile
├── Makefile
├── docker-compose.yml # quick local run without Kubernetes
├── pyproject.toml
├── .env.example       # variable names only
└── README.md          # source attribution, data limits, "localhost only" note
```

## 16. Milestones

Each milestone is one or more small PRs with its acceptance check.

| M | Deliverable | Acceptance |
|---|-------------|------------|
| M1 | Repo skeleton, `ci.yml`, HN client, thread finder, Postgres schema, Docker Compose; fetch the last 12 months | Y Combinator terms read and recorded; posts per month measured; fetch twice → no duplicates (S5) |
| M2 | Celery + beat, full back-fill since 2011, updates with edits/deletes, retries, `fetch_runs` | API forced to fail mid-run → next run resumes (S6) |
| M3 | Taxonomy, baseline + LLM extraction, trend views, stats endpoints | Extraction set labelled; baseline vs LLM F1 recorded (S2) |
| M4 | Tools module, LangGraph agent, grounding check, `/v1/ask`, fixture DB, agent eval | Baseline recorded; S3 and S4 measured |
| M5 | MCP server (stdio + HTTP) | Works in MCP Inspector and Claude Desktop (S8) |
| M6 | Helm chart, Terraform, k3d `make up`, `deploy-check.yml` | S1 on a clean machine; S7 green |
| M7 | Metrics, Langfuse, `release.yml`, README polish, demo GIF, CV line | Release `v1.0.0` with images on GHCR |

## 17. Risks

| Risk | Mitigation |
|------|------------|
| Single source: HN API changes or disappears | Contract tests on recorded responses; the stored back-fill keeps the project working; Arbeitnow is the planned second source |
| Y Combinator terms restrict reuse | Verify in M1; post text never leaves the local DB; public datasets hold item ids only |
| HN posts are not the whole market (US/remote/startup bias) | Stated in the README and in answers about regions; tools return post counts; no share from < 30 posts |
| Free-text posts vary a lot in format | LLM extraction with schema; baseline as fallback; extraction set spans 2011–now |
| Recruiter contact details in posts | Strip emails and phone numbers before storage; usernames not stored |
| Few Greek posts | Stated in scope; agent reports "sample too small" (eval category "impossible") |
| LLM invents numbers | Tools are the only source of numbers; grounding check; S4 |
| Prompt injection inside post text | Post text only reaches the extraction prompt; output must match the taxonomy schema; extraction has no tools |
| LLM cost | Daily token budget; baseline extractor as fallback; PR checks use a fake LLM |
| k3d too heavy for the laptop | Docker Compose path for daily development; Kubernetes for deploy checks |
| Scope creep (UI, cloud, auth) | Non-goals in §3; "later" list in the README |

## 18. CV line (fill in after M7)

> **SkillPulse Agent** — Job-market analysis agent over [N]k Hacker News job posts
> spanning 2011–2026 (LangGraph,
> MCP, FastAPI, Postgres, Celery/Redis, Kubernetes/Helm, Terraform, GitHub Actions).
> Reached [X] skill-extraction F1 (vs [Y] keyword baseline) and [Z] answer correctness
> with [W]% of numbers grounded in SQL results; every PR deploys the full stack to a
> Kubernetes cluster in CI.
