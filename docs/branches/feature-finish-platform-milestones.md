# Platform milestone implementation review

Branch: `feature/finish-platform-milestones`
Date: 2026-10-15

## Changes and scope

Three bounded agents used `openrouter/openrouter/free` after a real tool-use probe and zero-price catalog check.
Parent review rejected placeholder infrastructure commands and incorrect observability tests.
Parent corrections replaced those results with actual Terraform resources, deployment commands, and endpoint tests.

The work adds local CloudNativePG/PostgreSQL and Redis resources, staged application installation, guarded secret bootstrap,
a synthetic PostgreSQL/MCP smoke test, deployment-check CI, API metrics, metadata-only tracing, and a release workflow.
Chart fixes cover quoted JSON environment values, Kubernetes service-link collisions, migration-owner credentials,
worker startup/liveness probes, and beat's writable schedule path/startup check.
Docker excludes private infrastructure files from build context.

Existing worker changes and the original Docker/Helm files were preserved as the starting point.
No HN fetch/backfill, real-model call, Claude Desktop configuration change, push, tag, or external image publication occurred.
Image pushes during validation target only the disposable local registry.

## Verification

- Locked dependency sync succeeds, including `prometheus-client` 0.26.0.
- Full non-integration suite: **158 passed, 3 skipped, 19 deselected**, 430 pre-existing SQLite adapter warnings.
- Opt-in Inspector CLI suite: **3 passed** over real stdio/HTTP subprocesses, offline fixture data.
- Final agent/observability subset: **26 passed**.
- Ruff `src tests` passes; strict mypy passes across all **47 source files**.
- Helm 3.17.3 lint and strict Kubernetes 1.32.3 schema checks pass for default/local/CI values: **11/10/10 resources**.
- A deliberately malformed rendered environment value makes kubeconform fail with exit 1.
- Terraform init, format check, and validation pass. The pinned CNPG chart resolves from its public Helm repository.
- Both new workflows pass actionlint 1.7.12; a deliberately invalid workflow fails validation.
- Shell syntax checks pass. Both secret and smoke scripts reject a nonlocal context with exit 1.
- A local deployment reached healthy API, MCP, workers, beat, PostgreSQL and Redis.
  The smoke script verified readiness, metrics and a real MCP `skill_counts` response from synthetic PostgreSQL data.
- Final fresh cluster, using the CI chart profile: **236 seconds (3m56s)** from `scripts/up.sh` start through smoke success, exit **0**.
  This is the same script called by `make up`. Tools and image/build caches already existed; it is not a cold-machine benchmark.
  All **eight pods** remained ready with **zero restarts** at a later check. All four external-access/export gates were false.
- Cleanup passed. The disposable cluster, registry, kubeconfig and Terraform state were removed.

Follow-up pod checks caught faults that an early rollout-only smoke could miss.
The final fresh run uses chart fixes, not diagnostic in-cluster patches.
Beat waits for writable schedule state; workers wait for their own broker ping before becoming ready.

## Acceptance limits

M1/M2 authorization and historical-data gates remain unchanged.
M3/M4 still need real-HN labels and real-model quality evidence.
M5/S8 still need Claude Desktop and GUI/demo evidence.
M6/S7 needs an approved push and a successful remote deployment-check run on the integration branch.
The first round left M7 telemetry and separate database logins open. The follow-up below closes those local implementation gaps.
M7 still needs live Langfuse ingestion, demo assets and remote release evidence.
No remote CI or release result is inferred from local validation.

## Hardening follow-up

The second free-agent round hit the router's daily quota. An alternate free route was also unavailable.
No credits were purchased and no paid fallback was used. Parent review rejected the database completion claim
(no claimed edits existed), replaced the acceptance scaffold/vacuous tests, and repaired partial telemetry.
All four second-round child panes were closed; the parent completed and checked the changes.

- CloudNativePG manages `skillpulse_api` and `skillpulse_mcp` logins and separate basic-auth Secrets.
  API/MCP use separate configuration Secrets without the owner's DSN; workers/migrations retain the owner.
  Existing migration 0003 supplies the NOLOGIN privilege group and SELECT grants.
  Post-migration SQL grants membership and sets read-only/timeout session defaults. Password bootstrap reuses existing Secrets.
- Keep membership out of the initial CloudNativePG role declarations: on a fresh database the group does not exist
  until migration 0003. Declaring `inRoles` too early delayed role creation. That approach was rejected and removed.
- MCP handler counters and durations use the actual five fixed tool names. Its SDK HTTP app serves `/metrics`;
  stdio remains protocol-only. Generic error logging omits exception contents.
- API extraction volume reads stored `post_extractions` rows for baseline/LLM across taxonomy versions.
  It observes separate writer connections, uses a one-second PostgreSQL query timeout, and reports NaN/health 0
  on failed count queries rather than a false zero. This is stored volume, not an extraction-attempt/rate counter.
- The offline PowerShell preflight checks seven prerequisites without network calls, GUI changes or flag activation.
  Process environment overrides `.env`, matching application Settings. HN flags never grant authorization.
  Fourteen fixture assertions cover configured/missing/placeholder settings, environment precedence,
  secret redaction, HN blocking and strict failure. The existing CI workflow now runs them.
  A disposable copy with the placeholder filter removed correctly fails its test; the real script remains intact.
- Full non-integration suite: **162 passed, 3 skipped, 19 deselected**; 430 existing SQLite adapter warnings.
  Observability/MCP subset: **11 passed**. Opt-in Inspector: **3 passed**.
  Ruff and strict mypy pass for all 47 source files. All three workflows pass actionlint.
- Actual PostgreSQL TCP connections as both reader logins prove SELECT succeeds and INSERT/UPDATE/DDL each
  fail with privilege SQLSTATE 42501, with session read-only deliberately disabled.
  Temporarily granting UPDATE to the API login makes smoke fail for the expected write-permission reason;
  revoking that grant restores a fully passing smoke. The probes roll back successful test writes.
- Synthetic Kubernetes smoke also validates the API's committed extraction-row gauge and a one-call increment
  on MCP's own metrics endpoint. Repeated smoke runs pass, rather than requiring a fresh counter of exactly one.
- All four effective runtime external gates were checked inside all five application deployments and were false.
  Checking only the Deployment's explicit `env` list is insufficient because these gates come from `envFrom` Secrets.

README, deployment runbook, milestone JSON and remaining-acceptance checklist now distinguish these local checks
from real-HN/model quality, live tracing, GUI, hosted endpoint, remote CI and release acceptance.
M6 and M7 remain partial. No push, tag, release or global Desktop configuration change occurred.

Final hardened clean-cluster run: **222 seconds (3m42s)**, exit **0**, `VALUES_FILE=values-ci.yaml`.
It used the reviewed files, not diagnostic patches. It verified real reader privileges, persisted extraction volume,
a one-call MCP counter increment and the synthetic tool result. Tools and build/image caches already existed;
this is not a cold-machine benchmark. Follow-up: **eight ready pods, zero restarts**, and all four effective
runtime external gates false across all five application deployments. CloudNativePG reported all three managed logins reconciled.
Cleanup was independently checked: no isolated cluster, registry, private kubeconfig or local Terraform state remains.
The pre-existing medleaf, SkillPulse Compose PostgreSQL, Hindsight and taxis database containers were retained.

## Native Cline follow-up

The native Cline CLI has a separate free-model catalog from the exhausted direct API routes.
Its live catalog listed `cline-free/nemotron-3.5-super-vl` as free. An isolated real file read/write probe passed,
with reported cost zero. Two native Cline agents then used that exact model with separate temporary configuration/state,
explicit model/provider flags, bounded runtimes and separate file ownership. No global provider selection was changed.

The M6 child completed the deployment-workflow work with reported cost zero. Parent review added the actual
20-minute step deadline, made the S1 result depend on successful deployment/smoke (not time alone), enforced the
SPEC's strict less-than-1200s boundary, handled not-started timing without an epoch-zero fiction, recorded revision/outcome,
and used the isolated cleanup script for the cluster, registry and state. Five deterministic policy cases passed;
a disposable inclusive-boundary mutation failed as required. All three workflows passed actionlint.
Timing/log artifacts remain evidence-producing configuration, not evidence of a remote CI run.

The broader M7 draft and a smaller `cline-free/solar-mini4` runner-repair attempt were not accepted
as completed tasks. Both were stopped before the parent took file ownership. Solar's isolated real
file read/write probe also passed with reported cost zero; that is not proof of coding-task success.
The parent replaced the draft with digest-pinned Langfuse v3 services, a guarded PowerShell runner
and explicit opt-in live tests. Seven agent-created scratch files were removed after confirming
that none existed in the initial file-hash baseline.

The final local Langfuse run exited **0** in **53.3 seconds**, including startup and cleanup with
cached images. **Two real tests passed**: exact stored trace/span read-back, private visibility,
typed metadata, no synthetic private prompt/CV/answer/error sentinel or input/output payload,
and disabled export making no HTTP send and leaving no stored trace. A deliberate enabled-export
mutation failed the disabled-send assertion; it cannot pass on an asynchronous-ingestion race.
An occupied-port check failed before service creation. JUnit guards rejected zero tests, all-skipped,
failures and missing test cases. Ordinary runs skip both live tests without network; explicit opt-in
rejects non-loopback URLs. The runner uses an empty Compose env file, keeps HN and both model flags
off, restores its scoped environment, and removes only its unique disposable project's resources.
Containers, named volumes and networks were independently confirmed removed afterward.

The full non-integration regression is **162 passed, 5 skipped, 19 deselected**; the five skips are
three opt-in Inspector tests and two opt-in live tracing tests. Live tracing's two tests pass separately.
README, deployment runbook, remaining-acceptance checklist and milestone JSON now distinguish real
local tracing evidence from hosted tracing, remote CI/release and GUI/demo evidence. M6/M7 remain
partial. No global provider selection, Desktop configuration, real-HN/model activation, push, tag or
external publication was changed or performed.

Final follow-up checks: Ruff and live-test formatting pass; strict mypy checks **47 source files**;
the dependency lock matches; actionlint checks **all three workflows**; PowerShell prerequisite tests
pass **14 assertions**. Inspector CLI integration passes **3 tests** separately. Both live-test fixtures
reject a deliberately non-loopback URL before HTTP. **23 relative document links** resolve.
The **141-file** pre-Cline hash baseline shows only six intended workflow/document changes and no
missing files; exactly the three reviewed Langfuse smoke files are new. The original uncommitted
application changes remain intact. The complete working tree has **53 changed/new paths**.
Temporary isolated Cline credential/state copies and operational scratch scripts are removed after
owned processes stop; execution logs remain in the user-local temporary evidence directory.

## Approved publication follow-up

The user approved feature-branch publication and PR creation. The verified destination is the private
`NickLitharis/skillpulse-agent` GitHub repository, default branch `main`; the authenticated account has
push permission. No paid model was enabled, and HN data-use authorization is not inferred from this
approval. The final merge/release source and tag must be identified before any release publication.

Pre-publication review preserves the original worker changes and checks the complete staged scope.
Gitleaks v8.30.1 was downloaded into a temporary tools directory and its published SHA256 verified.
The redacted staged scan covered about 138 KB and found no leaks. Private `.env`, kubeconfig,
Terraform state and provider credentials are excluded from the commit and Docker context.

The review caught Git's Windows CRLF conversion of new shell entrypoints. `.gitattributes` now pins
`*.sh` to LF. A disposable Git repository with `core.autocrlf=true` reproduced CRLF without this
policy and produced LF with the actual policy. This guards fresh Windows checkouts without changing
application behavior. The publication scope is now 54 paths. Remote CI and PR evidence will be read
from GitHub after publication; configuration and local test success do not count as a remote pass.

## PR #1: observed CI and compatibility fixes

Published commit `0f9c574e2d8e444213af45e086108beb4e2acb29` in
[PR #1](https://github.com/NickLitharis/skillpulse-agent/pull/1). The first
[deployment run](https://github.com/NickLitharis/skillpulse-agent/actions/runs/38870868739)
passed on a fresh GitHub-hosted runner in **166 seconds**, with no configured application cache.
Downloaded artifact `deploy-check-evidence` records successful deployment/smoke and the strict
1200-second budget. Its tested PR merge revision is
`e58972c3ee6a2ef603a1dfe1e1efe1f4d3e38229`, not a `main` deployment.

The first general CI run exposed two failures that were reproduced locally before fixing:

- The preflight's 14 assertions passed, but GitHub's PowerShell wrapper forwarded the deliberately
  failed strict child's exit code. The test script now exits zero only after all assertions and
  cleanup succeed. An intentionally wrong assertion still exits nonzero.
- Explicit task routes published to `fetch`/`skills`, but the documented default Celery worker only
  consumed `celery`. Declare both queues and default to `fetch`; workers using `-Q` remain isolated.
  A new queue-consumption unit test failed against the old configuration. The original real
  Redis/worker/local-HTTP-stub integration test now passes without weakening it.

Running unit and integration tests together also exposed Alembic disabling existing application
loggers. Its logging setup now preserves existing loggers; the tracing privacy/error assertion
passes after migrations instead of being silently disabled. No schema or migration SQL changed.

Validation after these fixes: **182 passed, 5 opt-in skipped**, including all 19 Docker integration
tests; Ruff passes for `src`, `tests` and the changed Alembic environment, and strict mypy passes
47 source files. The successful PowerShell run was checked through GitHub's actual exit-forwarding
wrapper. These fixes add one previously unchanged path (`alembic/env.py`), for a total publication
scope of 55 paths. The corrected revision still needs a fresh remote CI run before a green claim.
Main deployment, original Desktop acceptance and release publication are still separate gates.

## Corrected PR green; genuine Inspector browser demo

Code revision `956f597` passed the actual [CI run 38872397299](https://github.com/NickLitharis/skillpulse-agent/actions/runs/38872397299):
163 non-integration tests, 5 opt-in skips, all 19 Docker integration tests, strict mypy (47 files),
Ruff and the 14-assertion preflight. The corrected [deployment run 38872397352](https://github.com/NickLitharis/skillpulse-agent/actions/runs/38872397352)
also passed. Downloaded evidence records **171 seconds** on a fresh hosted runner, successful
smoke, and tested PR merge revision `ffa0f72e99c1ed01c18a946a16c6f9b8dd3c9017`.
Both runs completed successfully; PR #1 was open, mergeable and clean, not merged.

The real browser MCP Inspector 2.11.0 then connected over stdio to the deterministic
`skillpulse-mcp --fixture` server. All five tools appeared. Executing `skill_counts` rendered
eight structured rows and the expected 480-post eligible fixture denominator. All HN/model/
Langfuse gates stayed false in this isolated server configuration. No existing database,
provider credentials or Desktop settings were changed.

`docs/assets/inspector-demo.gif` is the actual browser recording of the form, invocation and
result, not a mockup. It is 1,344,755 bytes; decoding verifies 103 frames with 48 distinct
frame hashes. SHA256: `3EFFCF860F75DD94747C94D6FD41E631A0F6B6AAD36F3EC4F58869E3A7E83D7D`.
A separately captured result screenshot was visually reviewed: no credentials, private prompt,
CV, post text or personal browser content. README labels the data synthetic and limits the claim.
The owned browser session and Inspector process tree were stopped; neither temporary loopback
listener remains. Operational logs/recordings remain in the user-local temporary evidence folder.

Updated README, the deployment runbook, M5 acceptance and milestone/remaining-gate records to distinguish completed
Inspector GUI/demo evidence from original Desktop, hosted, real-data and release acceptance.
This adds the existing M5 record and one GIF to the reviewed scope, for 57 total paths from the
base commit. No application code changed after the green corrected CI revision. The documentation
and demo commit will still receive its own PR checks. No merge, tag or image publication occurred.
