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
[docs/terms.md](terms.md); acceptance evidence:
[docs/m1-m2-acceptance.md](m1-m2-acceptance.md).

**Not a clearance toggle.** Enabling the flag does not assert permission. It only opens
the gate after an explicit authorization decision is recorded in `docs/terms.md`. Tests
use in-process fakes or a local HTTP stub and set the variable explicitly where the real
worker path is exercised (`tests/test_workers_redis_smoke.py`); the stub is localhost,
not Hacker News.