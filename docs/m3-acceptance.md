# M3 acceptance evidence (2026-10-15 UTC)

Scope: taxonomy, baseline and LLM extraction, trend views and stats endpoints. This
round did **not** fetch Hacker News (the gate in [terms.md](terms.md) is still closed)
and did **not** call any remote LLM. No test result here is legal permission and no
result here is a real-data S2 number.

## Delivered and verified

| Part | Where | Evidence |
| --- | --- | --- |
| Taxonomy + role families | `config/skills.yaml` (336 skills, 10 role families), `config/role_families.yaml` | loader validates ids/aliases; duplicate alias/id and malformed YAML tests fail loudly |
| Deterministic baseline | `src/skillpulse/extraction/baseline.py` | word-boundary aliases, negation (leading "no/kein/pas", trailing "not needed"), nice-to-have `required=false`, NFKC+casefold so accent and CJK posts match |
| LLM extractor | `src/skillpulse/extraction/llm.py`, `schema.py` | taxonomy-only validation, unknown ids dropped and counted, JSON retried once, daily token budget, prompt-injection isolation |
| Provider adapter + cost guard | `src/skillpulse/extraction/providers.py`, `budget.py` | real OpenAI-compatible adapter; off unless `SKILLPULSE_LLM_ENABLED=true`; fails closed without a shared atomic Redis daily budget |
| Fallback | `src/skillpulse/extraction/service.py` | provider failure stores the baseline flagged `is_fallback=true` |
| Persistence | `alembic/versions/0002_extraction_trends.py` | `post_extractions`, `post_skills`, uniqueness on `(post_id, extractor, taxonomy_version)`; re-record is idempotent |
| Trend views | migration 0002 | `skill_trend_mv`, `skill_segment_mv` with unique indexes; `remote_state` is 3-state (`true`/`false`/`unknown`) so `remote=false` never counts unclassified posts; `REFRESH ... CONCURRENTLY` runs on real Postgres |
| Trends + safeguards | `src/skillpulse/trends.py` | exact numerator/denominator; `total_posts` is **eligible** posts (with an extraction for the requested extractor/taxonomy), not full-source coverage; `share=None` and `sample_sufficient=false` below 30 posts |
| Stats API | `src/skillpulse/api/app.py` | `/healthz`, `/readyz`, `/v1/skills/top`, `/v1/skills/{id}/trend`, `/v1/coverage` |
| Labelled set + runner | `eval/datasets/synthetic_extraction.json`, `src/skillpulse/eval/extraction_eval.py` | micro/macro P/R/F1, required accuracy, unknown-term rate |

## Synthetic vs real (read this before quoting a number)

| Result | Status | Why |
| --- | --- | --- |
| Baseline micro-F1 0.9910 / macro-F1 0.9783 on the 40-post **synthetic** set | recorded in `eval/results/baseline_synthetic.json` | text is written for this repo; no HN text involved; 1 false positive and 2 false negatives keep it honest |
| Baseline/LLM F1 on the **real** ~150-post set (S2) | **BLOCKED** | needs authorized HN post text (M1) and a real LLM provider credential |
| LLM F1 from the fake provider | contract test only | a fake result is never recorded as a real F1 |

The synthetic set is deliberately small and in-distribution, so its baseline F1 is an
upper bound on real data, not a real-data claim. The runner records `source` and a
`gate` string in the results JSON so a synthetic run cannot be mistaken for a real one.

## Commands and results

| Check | Command | Result |
| --- | --- | --- |
| Lint | `uv run ruff check src tests` | clean |
| Types | `uv run mypy` | clean (strict, 29 source files) |
| Unit + integration | `.venv/Scripts/python.exe -m pytest -q` | **106 passed**, exit 0, wall 31 s (whole-process cap 180 s) |
| Taxonomy | `uv run skillpulse taxonomy` | 336 skills, 10 role families |
| Baseline eval | `uv run skillpulse eval extraction` | writes `eval/results/baseline_synthetic.json` |
| Refresh views | `uv run skillpulse refresh-trends` | refreshes both materialised views |

## Limits

- No real-data extraction, no remote LLM call, no training, and no export of stored post
  text. Real LLM extraction needs a configured provider plus a recorded authorization
  decision; until then M3 is **partial**.
- The `worker-skills` Celery task and the nightly refresh schedule are not wired; the
  service functions and views they will call exist and are tested.
- The taxonomy is 336 skills, near the spec's ~300, and the labelled set is 40 posts, near the spec's ~40. Both are real and validated, not filler.
- A real provider call is impossible without a recorded authorization decision **and** a shared Redis budget: `build_extractor` returns `None` (baseline only) while the gate is closed and raises when it is open but incomplete. No remote call is made in this round.
- Shares below 30 posts are withheld (`share=None`); counts are still exact. `total_posts` means eligible posts (extracted by the requested extractor/taxonomy), not stored-post coverage; `/v1/coverage` reports stored posts separately.
