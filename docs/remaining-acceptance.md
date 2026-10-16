# External acceptance checks

The local deployment and synthetic tests are not proof of real-data accuracy, a successful remote CI run, or a public release.

## Check local prerequisites

From the repository root:

```powershell
pwsh -NoProfile -File scripts/acceptance-preflight.ps1
pwsh -NoProfile -File scripts/acceptance-preflight.ps1 -Strict
pwsh -NoProfile -File tests/test_acceptance_preflight.ps1
```

`READY` means that the named **prerequisite** exists. It does not mean the acceptance test passed.
The script reads configuration without executing `.env`. It never prints configuration values.
It does not contact providers, HN, Langfuse, CI, or Desktop. It does not change feature flags.
Strict mode returns exit code 1 while any prerequisite is missing or an evidence gate is blocked.
HN permission always requires human review; setting an environment flag does not grant permission.
Pass `-DesktopExecutable <path>` to check an explicitly selected executable. The script does not read global Desktop configuration or start that executable.

## Required evidence

| Gate | What remains | Existing procedure |
|---|---|---|
| HN data and labels | Written permission covering read, storage and reuse; permitted collection and reviewed labels | [Terms](terms.md), [M1/M2](m1-m2-acceptance.md) |
| Extraction quality, S2 | Real-data labels and an approved provider/model/budget; compare baseline and LLM F1 | [M3 acceptance](m3-acceptance.md) |
| Answer quality, S3/S4 | Approved real-model configuration and shared Redis budget; report model identity, dataset identity and measured correctness/grounding | [M4 acceptance](m4-acceptance.md) |
| Langfuse | Isolated local ingestion/read-back is verified with two real tests. A hosted deployment, if needed, still needs its own protected configuration and read-back check | [Disposable live test](deployment.md#disposable-live-langfuse-check) |
| Desktop and GUI Inspector, S8 | Run the actual clients and record the demo; installation/configuration presence is not acceptance | [M5 acceptance](m5-acceptance.md) |
| Remote CI, S7 | Explicit push permission, reviewed diff, then successful PR and `main` deployment-check runs | [Deployment workflow](../.github/workflows/deploy-check.yml) |
| Release, M7 | Explicit version/tag and publication approval, successful image publication and demo/CV evidence | [Release workflow](../.github/workflows/release.yml), [SPEC](../SPEC.md) |

No live-provider test should run merely because credentials exist. Review provider pricing and budget first.
The free agent router quota is separate from the application's extraction/agent budget.
A synthetic/fake-model evaluation remains a contract check even if it reports perfect scores.

## Safe checks now

```powershell
uv run pytest -m 'not integration'
$env:SKILLPULSE_RUN_INSPECTOR = '1'
try { uv run pytest tests/test_m5_inspector.py } finally { Remove-Item Env:SKILLPULSE_RUN_INSPECTOR }
```

The Inspector command exercises the CLI, not Desktop or the Inspector GUI. It can download the pinned Inspector package through `npx`.
For local deployment and the database permission/MCP metrics smoke, use [the deployment runbook](deployment.md).
For the opt-in local Langfuse stack, run `pwsh -NoProfile -File scripts/langfuse-smoke.ps1`.
It needs Docker Compose, uses only synthetic data and removes its own isolated stack afterward.
