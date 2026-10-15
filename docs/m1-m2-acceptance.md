# M1/M2 acceptance evidence (2026-10-15 UTC)

Scope: ingestion, recovery and CLI acceptance for the existing code. No taxonomy, LLM
extraction, trends or API work. This round did **not** fetch Hacker News: see
[terms.md](terms.md) — authorization is unresolved, so live raw-post ingestion and
back-fill are gated. The numbers below are read-only counts from the local database that
already existed, plus deterministic recovery tests on synthetic fixtures.

## Accepted this round

- **Recovery / resume (M2, SPEC S6).** Forced mid-run failure after one committed batch
  marks the run `failed` with its resume point; the next run finishes the thread with no
  skipped or duplicated post. Proven on real Postgres with the real client and controlled
  fake HTTP.
- **Idempotence (M1, SPEC S5).** Replaying a thread creates zero new posts.
- **Live-fetch gate.** Live HN access is closed by default and refuses with a clear
  reason; it is not a permission toggle, and enabling it does not constitute clearance.

## Not accepted this round (honest gaps)

- **M2 full back-fill since 2011: NOT delivered.** Blocked by unresolved authorization.
- **M1 "last 12 months" fetch: NOT delivered.** Blocked for the same reason.
- **Discovery coverage: not re-measured this round** (gated). A prior record (D001,
  2026-10-15) says `user/whoishiring` returned 551 submitted items and the last 121 items
  held 45 "Who is hiring?" threads at 376–592 comments each. That is a past record, not a
  new measurement, and listing threads is **not** fetching their months.

## 12-month window (explicit)

Run date 2026-10-15 (UTC). "Last 12 months" = the twelve monthly hiring threads with
title months **2025-11 through 2026-10 inclusive** (rolling window start
2025-10-15T13:08Z; the 2025-10 thread predates it).

### Stored coverage per month (read-only, existing DB)

| Month | Threads stored | Posts stored | Status |
| --- | --- | --- | --- |
| 2025-11 … 2026-09 (11 months) | 0 | 0 | **MISSING — blocked (not fetched)** |
| 2026-10 | 1 (id 49922569) | 318 | present |

Thread 49922569 ("Ask HN: Who is hiring? (October 2026)", `descendant_count` 392 at fetch
time) holds 318 top-level posts: 33 flagged deleted, 22 flagged dead, ids 49922573…
50095921. **11 of 12 months have no stored posts.**

### Fetch runs (existing DB)

| Run | Kind | Status | posts_seen | posts_upserted | last_item_id |
| --- | --- | --- | --- | --- | --- |
| 1 | thread | ok | 318 | 318 | 49928997 |
| 2 | thread | ok | 318 | 0 | 49928997 |
| 3 | thread | ok | 318 | 0 | 49928997 |

Runs 2 and 3 replay the same thread and write nothing: repeat stability on real data.
This is the only live evidence available; it is not 12-month coverage.

## Bounded historical back-fill: BLOCKED

Planned scope (not executed): **at most two** threads outside the 12-month window, chosen
as the two oldest "Who is hiring?" threads in the `whoishiring` submission scan, fetched
at the polite ≤10 requests/second with bounded work. **Explicit thread ids are not
recorded because resolving them requires the gated discovery read.** No ids are invented,
and the full-history (since 2011) task is untouched. Once authorized, resolve ids with:

```bash
uv run skillpulse threads        # lists id, month label and title (gated today)
```

## Commands and results

| Check | Command | Result |
| --- | --- | --- |
| Lint | `uv run ruff check src tests` | clean |
| Types | `uv run mypy` | clean, 15 files (strict) |
| Full suite | `.venv/Scripts/python.exe -m pytest -q` | **52 passed** in 18.24s; whole process (wall clock, exit code checked, not tail-masked) 20s |
| Recovery test | `uv run pytest tests/test_ingest_postgres.py::test_forced_midrun_failure_then_resume_on_postgres` | pass |

Failure-sensitivity of the recovery test (deliberate mutation, then restored): running
the first fetch with `batch_size=100` so no batch commits before the forced HTTP 500 makes
the test fail on `last_item_id`/durable-progress assertions; with `batch_size=2` it passes.

## Independent review

The parent reviewer reran `uv run pytest -q`: 52 tests passed in 15.43 seconds.
The whole process took 18 seconds and exited with code 0. Ruff and strict mypy also
passed. A read-only query independently confirmed one stored month, 318 posts, and three
successful runs with 318 total upserts.

To repeat the count against the existing local Docker database, run:

```bash
docker exec skillpulse-agent-postgres-1 psql -U skillpulse -d skillpulse -v ON_ERROR_STOP=1 -c "BEGIN TRANSACTION READ ONLY; SELECT t.month, t.id, count(p.id) AS stored_posts FROM threads t LEFT JOIN posts p ON p.thread_id=t.id GROUP BY t.month,t.id ORDER BY t.month; SELECT status, count(*), sum(posts_upserted) FROM fetch_runs GROUP BY status; COMMIT;"
```

Keeping extracted fields instead of full text does not itself resolve the restriction
on automated extraction. D006 supersedes the earlier storage suggestion in D002.

## What the reviewer should not read into this

No test result here is legal permission. No result here shows 12-month or full-history
coverage. The gate is an operational safety control, not a clearance record.
