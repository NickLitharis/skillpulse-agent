# SkillPulse Agent

A job-market analyser over Hacker News' monthly **"Ask HN: Who is hiring?"** threads.
Background workers collect the posts, an LLM extracts the skills each post asks for, and
a LangGraph agent answers questions about skill demand and trends using only database
results. The same read-only tools are exposed through an MCP server.

Full design: [SPEC.md](SPEC.md). Architecture decisions: [docs/decisions.md](docs/decisions.md).

## Status

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

Not built yet: the LLM-backed extraction worker task and its nightly refresh schedule,
the LangGraph agent (`/v1/ask`), the MCP server, Helm/Terraform, and k8s deployment. The
full historical back-fill is blocked on the authorization gate below.

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
  api/app.py          FastAPI stats endpoints (/v1/skills, /v1/coverage)
  eval/               labelled-set extraction scoring
  runtime.py          shared client/session wiring for the CLI and the workers
  workers/
    app.py            Celery app: Redis broker/backend, beat schedule
    tasks.py          fetch_thread, discover_threads, refresh_recent_threads
  cli.py              skillpulse threads | fetch | backfill
alembic/              migrations (0001 initial ingestion tables)
tests/                unit tests + Postgres/Redis testcontainers integration tests
```

## Tests

```bash
make check             # ruff + mypy (strict) + unit tests, no Docker needed
make test-integration  # real Postgres 16 and Redis 7 via testcontainers (needs Docker)

uv run skillpulse taxonomy            # print the loaded taxonomy
uv run skillpulse eval extraction     # synthetic baseline F1 -> eval/results/
uv run skillpulse refresh-trends      # refresh the trend materialised views
uv run uvicorn skillpulse.api.app:app # stats API on localhost:8000
```

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