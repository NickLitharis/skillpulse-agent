# Local deployment and telemetry

## Scope

This deployment uses a disposable local Kubernetes cluster. It is not a production deployment.
It never enables live Hacker News access or real model calls.
The smoke test inserts synthetic data. It does not measure model quality or historical coverage.

## Requirements

Install Docker, Bash, k3d, kubectl, Helm 3, Terraform 1.x, OpenSSL, curl, jq, and Node.js with npx.
On Windows, run these commands from Git Bash. The Makefile is optional; its targets call Bash scripts.
Docker must be running. Ports 5001, 18000, and 18765 must be available.

The workflow pins k3d 5.8.3, k3s 1.32.3, Helm 3.17.3, Terraform 1.11.4, and Inspector 2.11.0.
Terraform pins the CloudNativePG chart at 0.23.2 and uses PostgreSQL 17.4 and Redis 7.4.
Review these versions and container security updates before nonlocal use.

## Start

If `.env` does not exist, copy `.env.example` to `.env`. Do not overwrite an existing configuration.

```bash
make up
# Without Make:
bash scripts/up.sh
```

The script creates only `skillpulse` and `skillpulse-registry` in k3d.
It writes a private, gitignored kubeconfig under `infra/`; it does not switch your default context.
It builds the application image and pushes it to the local registry on port 5001.
A fresh local image tag forces workload updates. CI uses the commit identifier instead.

Terraform first installs the database operator, then its database resource and Redis, then the application chart.
This order is necessary: the database custom resource definition must exist before Terraform can plan that resource.
The final full apply reconciles all resources after the targeted bootstrap steps.
The database must be ready before Helm runs migrations with the database-owner connection.

Secret bootstrap treats `.env` as data, not executable shell code.
It keeps secret values outside Terraform state and reuses local passwords on subsequent runs.
It overrides database/Redis URLs to target the local cluster.
It forces all ingestion/model gates and Langfuse export off.
Workers and migrations keep the database-owner connection. API and MCP each use a separate login,
`skillpulse_api` and `skillpulse_mcp`, and separate configuration Secrets without the owner's DSN.
CloudNativePG manages login/password state. Migration 0003 creates `skillpulse_readonly`,
a NOLOGIN role, and its current/future table SELECT grants.
After migration, `scripts/db-readers.sql` grants membership and sets reader session defaults.
Membership is not declared before migration: on a fresh database the group does not exist yet.
Readers have no superuser, create-role, or create-database rights. Read-only tool transactions remain an extra guard.
Rerunning bootstrap reuses each role's basic-auth Secret; it does not rotate passwords.
For a reviewed local password rotation, update that Secret securely and wait for CloudNativePG to reconcile it,
then rerun `make up` to rebuild configuration Secrets and restart workloads with a new local image tag.
Never put passwords in Terraform variables/state or log them.

## Smoke test

```bash
make smoke
# Without Make:
bash scripts/smoke.sh
```

The test waits for API, MCP, both workers, and beat deployments.
It inserts a synthetic Python skill fixture into PostgreSQL, then refreshes the trend views.
It connects as both real reader logins over TCP and proves SELECT succeeds, while INSERT, UPDATE and
DDL each fail with permission-denied SQLSTATE `42501`. It deliberately disables session read-only mode
for these probes, so authentication failures or transaction guards cannot pass as privilege isolation.
It checks `/readyz`, which currently checks PostgreSQL, and the API's stored-extraction gauge.
It calls the actual MCP `skill_counts` tool through Inspector over HTTP, validates the returned row,
and checks that the counter on MCP's own `/metrics` endpoint increased by one.
Temporary port-forwards and temporary files are removed when the script exits.
The fixture remains in the disposable database until cluster deletion. Never run this test against real data.
The fixed context guard rejects other context names.

Use `SMOKE_API_PORT` and `SMOKE_MCP_PORT` to change the local forwarding ports.
Use `VALUES_FILE=values-ci.yaml` for the CI chart profile (base resources, single replicas, HPA disabled).

## Stop

```bash
make down
# Without Make:
bash scripts/down.sh
```

This deletes the isolated cluster, its registry, local kubeconfig, and associated local Terraform state.
It deletes all data in that cluster. It does not touch Docker Compose services or other Kubernetes clusters.
Terraform state and `.kubeconfig` must never be committed or included in image build context.

## Metrics

Each API application exposes `/metrics` in Prometheus text format.
`skillpulse_requests_total` labels contain only normalized methods, route templates, and HTTP status codes.
Unknown routes use `unmatched`; raw paths and query strings never become labels.
`skillpulse_agent_runs_total` and `skillpulse_agent_duration_seconds` measure API agent runs.
Agent status is one of `ok`, `partial`, `cancelled`, or `error`.
MCP's HTTP transport also exposes `/metrics`. `skillpulse_mcp_tool_calls_total` and
`skillpulse_mcp_tool_duration_seconds` measure handler calls for the five fixed tools, with `ok`/`error` outcomes.
Arguments rejected by the SDK before handler dispatch are not handler calls. Stdio stays protocol-only;
it has no HTTP scrape endpoint. Scrape each HTTP process separately.

The API's `skillpulse_extraction_records{extractor="baseline"|"llm"}` gauge counts stored extraction rows
across taxonomy versions, not unique posts or extraction attempts. Each scrape reads committed database
state, including worker writes from other processes. Its SELECT has a one-second PostgreSQL statement timeout.
A failed count sets `skillpulse_extraction_scrape_ok` to 0 and volume to NaN (unknown), never a false zero.
This gauge is volume, not a measured extraction-rate counter.

Request, agent and MCP counters/durations are process-local. Deploy a scraper separately; processes do not share counters.
Protect both HTTP metrics endpoints with the deployment network boundary; they have no separate authentication.
There are no prompt, answer, CV, tool-argument, or tool-result labels.
Dashboards and alerts are not installed.

## Optional Langfuse export

For a separately reviewed non-smoke environment, configure these settings:

```dotenv
SKILLPULSE_LANGFUSE_ENABLED=true
SKILLPULSE_LANGFUSE_BASE_URL=https://your-langfuse-host
SKILLPULSE_LANGFUSE_PUBLIC_KEY=your-project-public-key
SKILLPULSE_LANGFUSE_SECRET_KEY=your-project-secret-key
```

The API sends an OTLP/HTTP JSON span to `/api/public/otel/v1/traces` using HTTP Basic authentication.
It sends only trace identifiers, timestamps, duration, status, tool-call count, and grounding/partial/error flags.
It never sends prompts, answers, CVs, tool arguments/results, or exception text.
The exporter times out after one second. Export failures do not fail the answer.
Missing settings or the default disabled flag cause no export request.
No framework-wide or model auto-instrumentation is installed.
Local bootstrap forces export off, even when `.env` enables it.
The isolated local backend roundtrip below is verified. It does not verify your hosted endpoint.
Mocked exporter tests alone are not backend-ingestion evidence.

## Disposable live Langfuse check

With PowerShell 7.2+, Docker Compose and uv installed:

```powershell
pwsh -NoProfile -File scripts/langfuse-smoke.ps1
# If the default loopback port is occupied:
pwsh -NoProfile -File scripts/langfuse-smoke.ps1 -Port 33008
```

This explicit command starts six isolated local services: Langfuse web/worker, PostgreSQL,
ClickHouse, Redis and MinIO. The tested images are pinned by digest in
`scripts/langfuse-smoke.compose.yml`; review those pins before updating them.
Only the web port is published, on `127.0.0.1:33007` by default. The script rejects an occupied
port and remote Docker endpoints. It uses a unique `skillpulse-langfuse-smoke-<random>` project.
Its fixed credentials are synthetic, local-test values. Never use them for a real deployment.

The runner passes an empty Compose env file, so it does not load the repository's `.env`.
It sets the test/export opt-in only in its process and disables HN and both model flags there.
It restores changed environment values and the caller's directory. It removes only its own
containers, volumes and network in `finally`; cleanup failure makes the run fail.

`tests/test_langfuse_live.py` calls the existing exporter, then reads the exact trace ID back
from the real backend. It checks one stored span, private visibility, typed metadata and the
absence of synthetic prompt/CV/answer/error sentinels and input/output payloads.
The disabled-export test checks that no HTTP send is attempted and no trace exists.
Ordinary pytest runs skip these tests without making a readiness/network request.
When explicitly opted in, tests reject non-loopback URLs before sending anything.
The runner checks JUnit results and rejects zero tests, skipped tests and failures.

Final local run: **2 live tests passed**, total startup/test/cleanup **53.3 seconds**, exit **0**.
Images were already cached. This is not a fresh-machine benchmark or a hosted tracing result.
A deliberate enabled-export mutation failed the disabled-send assertion, confirming that the guard
cannot pass merely because asynchronous storage has not appeared yet. Occupied-port and JUnit
negative controls also failed as expected. Cleanup was checked independently.

## Verification on 2026-10-15

A fresh isolated cluster with `VALUES_FILE=values-ci.yaml` completed deployment and smoke in **222 seconds (3m42s)** with the hardened chart and scripts.
The check used preinstalled tools and existing image/build caches, not a clean machine.
All eight pods remained ready with zero restarts at the follow-up check.
The check confirmed all four ingestion/model/export gates were false.
The disposable cluster, registry, kubeconfig, and local state were then removed.

The full non-integration suite passed: **162 passed, 5 skipped, 19 deselected**.
The five skips are three opt-in Inspector tests and two opt-in live Langfuse tests.
The three opt-in Inspector tests and fourteen PowerShell prerequisite assertions passed separately.
The observability/MCP subset passed eleven tests. Ruff, strict mypy, Terraform validation, and all three workflows' actionlint checks passed.
Both reader logins passed actual PostgreSQL permission probes. Giving the API reader temporary UPDATE permission
made smoke fail as expected; revoking it restored a passing run. The final fresh cluster needed no diagnostic patches.
Removing the preflight placeholder filter in a disposable copy also made its tests fail as expected.
All three chart profiles passed lint and strict Kubernetes schema validation.
A deliberately malformed environment value failed schema validation, confirming that the validator rejects that fault.
See [the branch verification record](branches/feature-finish-platform-milestones.md) for scope and limitations.

## Workflows and acceptance limits

`deploy-check.yml` validates all chart variants, Terraform, and shell syntax before a real isolated deployment and smoke test.
It runs for pull requests and pushes to `main`. A hard 20-minute deployment-step deadline and
an outcome-aware check enforce S1's strict less-than-1200s budget. The job uploads revision,
timing, deployment outcome and smoke logs as `deploy-check-evidence`; a quick failed deploy
cannot pass the budget check. Five deterministic policy cases and an inclusive-boundary negative
control were checked locally. Local success does not prove that remote CI passed.
`release.yml` validates `v*` tags, runs Ruff/mypy/unit tests, packages the chart, and publishes images to GHCR.
Do not create a tag or push without approval. No release or remote publication was performed for this work.

Real-HN authorization, historical backfill, real-model quality thresholds, Claude Desktop GUI evidence,
a demo GIF, remote CI/release and any hosted-endpoint tracing verification remain separate gates.
A possible CV line, after deployment review: “Built a read-only MCP job-trends agent with Kubernetes deployment and privacy-safe telemetry.”
Do not attach unmeasured accuracy or production-use claims to it.
