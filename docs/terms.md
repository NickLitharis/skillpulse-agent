# Terms check: Hacker News / Y Combinator (M1, SPEC section 5)

Status: **UNRESOLVED — live raw-post ingestion and back-fill are gated.** This is a
record of what the official sources say, not legal advice and not a permission.

## Sources read

| Source | URL | Retrieved (UTC) |
| --- | --- | --- |
| Hacker News API README | <https://github.com/HackerNews/API> | 2026-10-15 |
| Hacker News site footer (links "Legal", "API") | <https://news.ycombinator.com/> | 2026-10-15 |
| Y Combinator Terms of Use (the site legal page the footer links to) | <https://www.ycombinator.com/legal/> | 2026-10-15 |

## What the sources say

- **HN API README.** The data is "the public Hacker News data ... available in near real
  time"; "There is currently no rate limit." The README documents the endpoints only.
  It states no explicit licence for the reuse of post text.
- **HN site footer.** Links `Legal` to the YC legal page and `API` to the API GitHub
  repository, so the site and the comments it holds sit under YC's Terms of Use.
- **YC Terms of Use — "Site Content, Software and Trademarks".** Except as expressly
  authorized by YC, users agree not to modify, copy, scrape, distribute, or create
  derivative works from the site or its content. The clause excludes the user's own
  legally uploaded content from that restriction. It also prohibits data mining, robots,
  scraping, and similar gathering methods. Users must not bypass access blocks.
  "Any use ... other than as specifically authorized herein is strictly prohibited."
- **Ownership.** Each post belongs to its author. The YC User Content clause is a licence
  a *user* grants to YC; it is **not** a grant of rights to third-party API consumers.

## Conclusion

- Authorization to bulk-read and store Hacker News **comment text** is unresolved. No
  explicit official permission covering this use was found. The public-data wording of
  the API README does not by itself resolve the scope.
- **Consequence:** live raw-post ingestion is gated. `runtime.build_client`, the single
  choke point every live path uses, refuses unless
  `SKILLPULSE_HN_LIVE_FETCH_ENABLED=true` — see [D006](decisions.md#d006--live-hn-fetch-gate-unresolved-authorization).
  The CLI exits 2 with the reason; **no** `threads`, `fetch`, `backfill` or worker fetch
  runs against Hacker News by default.
- **Allowed without the gate:** the local database, unit tests with in-process fakes, and
  integration tests against a local HTTP stub (`tests/`).
- **Never published:** full post text. Only counts, metadata and
  `news.ycombinator.com/item?id=<id>` links leave the database.
- **PII:** e-mails and phone numbers are stripped before storage; the HN username (`by`)
  is not stored. Post text stays in the local database and is never exported.

## Owner action needed

Obtain authorization that covers the intended access, storage, and reuse, and record its
scope here. Alternatively, select a data source and access method that explicitly permit
the intended use. Storing only extracted fields and links does not itself resolve the
restriction on automated extraction. The owner must resolve this before live ingestion
resumes.
