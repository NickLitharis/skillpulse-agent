# Checks local prerequisites only. Does not run collection, providers, CI, or a GUI.
[CmdletBinding()]
param(
    [string]$RepositoryRoot = (Split-Path $PSScriptRoot -Parent),
    [string]$EnvFile,
    [string]$DesktopExecutable,
    [switch]$Strict
)
$ErrorActionPreference = 'Stop'
if (-not $EnvFile) { $EnvFile = Join-Path $RepositoryRoot '.env' }
$config = @{}
if (Test-Path -LiteralPath $EnvFile -PathType Leaf) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*?)\s*$') {
            $config[$matches[1]] = $matches[2].Trim().Trim('"').Trim("'")
        }
    }
}
function Test-Configured([string]$Name) {
    # Match Settings precedence: the process environment overrides .env.
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($null -eq $value -and $config.ContainsKey($Name)) { $value = $config[$Name] }
    return -not [string]::IsNullOrWhiteSpace($value) -and $value -notmatch '^(changeme|your[-_ ].*|<.*>)$'
}
function Test-Settings([string[]]$Names) {
    return @($Names | Where-Object { -not (Test-Configured $_) }).Count -eq 0
}
$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check([string]$Gate, [bool]$Ready, [string]$Detail) {
    $checks.Add([pscustomobject]@{ Gate = $Gate; Status = $(if ($Ready) { 'READY' } else { 'NOT_CONFIGURED' }); Detail = $Detail })
}
$checks.Add([pscustomobject]@{ Gate = 'HN authorization and labels'; Status = 'BLOCKED'; Detail = 'Needs reviewed read/storage/reuse permission and labelled real data; a feature flag is not permission. See docs/terms.md.' })
foreach ($prefix in 'LLM', 'AGENT_LLM') {
    $names = @('BASE_URL', 'MODEL', 'API_KEY') | ForEach-Object { "SKILLPULSE_${prefix}_$_" }
    Add-Check "$prefix configuration" (Test-Settings $names) 'Configuration presence only. Provider access, Redis budget, model pricing and evaluation results are NOT verified.'
}
Add-Check 'Langfuse configuration' (Test-Settings @('SKILLPULSE_LANGFUSE_BASE_URL', 'SKILLPULSE_LANGFUSE_PUBLIC_KEY', 'SKILLPULSE_LANGFUSE_SECRET_KEY')) 'Keys/URL present only; no network request made. Ingestion and trace read-back are NOT verified.'
Add-Check 'Inspector CLI prerequisite' ([bool](Get-Command npx -ErrorAction SilentlyContinue)) 'npx availability only; run the opt-in Inspector tests separately.'
Add-Check 'Desktop executable' ([bool]($DesktopExecutable -and (Test-Path -LiteralPath $DesktopExecutable -PathType Leaf))) 'Pass -DesktopExecutable explicitly. Global Desktop config is not read or changed; GUI/demo acceptance remains manual.'
Add-Check 'CI workflow files' ((Test-Path (Join-Path $RepositoryRoot '.github/workflows/deploy-check.yml')) -and (Test-Path (Join-Path $RepositoryRoot '.github/workflows/release.yml'))) 'Files exist only. Remote project, permissions and successful PR/main runs are NOT verified; no push performed.'
$checks.ToArray()
if ($Strict -and @($checks | Where-Object Status -ne 'READY').Count -gt 0) { exit 1 }
