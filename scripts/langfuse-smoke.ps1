#Requires -Version 7.2
# Explicit opt-in: only synthetic data, loopback HTTP, and a unique disposable Docker project.
[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)][int]$Port = 33007,
    [ValidateRange(30, 600)][int]$StartupTimeoutSeconds = 240
)
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false
$repository = Split-Path $PSScriptRoot -Parent
$composeFile = Join-Path $PSScriptRoot 'langfuse-smoke.compose.yml'
$project = 'skillpulse-langfuse-smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$originalLocation = Get-Location
$names = @(
    'LANGFUSE_SMOKE_PORT', 'SKILLPULSE_RUN_LANGFUSE_SMOKE',
    'SKILLPULSE_LANGFUSE_ENABLED', 'SKILLPULSE_LANGFUSE_BASE_URL',
    'SKILLPULSE_LANGFUSE_PUBLIC_KEY', 'SKILLPULSE_LANGFUSE_SECRET_KEY',
    'SKILLPULSE_HN_LIVE_FETCH_ENABLED', 'SKILLPULSE_LLM_ENABLED', 'SKILLPULSE_AGENT_LLM_ENABLED'
)
$saved = @{}
foreach ($name in $names) { $saved[$name] = [Environment]::GetEnvironmentVariable($name) }
$work = $null
$attemptedStart = $false
$result = 1

function Assert-ExecutedTests([xml]$Report) {
    $total = 0; $skips = 0; $failures = 0; $errors = 0
    foreach ($suite in $Report.SelectNodes('//testsuite')) {
        $total += [int]$suite.GetAttribute('tests')
        $skips += [int]$suite.GetAttribute('skipped')
        $failures += [int]$suite.GetAttribute('failures')
        $errors += [int]$suite.GetAttribute('errors')
    }
    if ($total -lt 1 -or $Report.SelectNodes('//testcase').Count -ne $total -or $skips -ne 0 -or $failures -ne 0 -or $errors -ne 0) {
        throw 'Live smoke requires executed passing tests, with no skips, failures or errors.'
    }
    Write-Host "Verified $total executed live tests with no skips."
}

try {
    foreach ($command in 'docker', 'uv') {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "Required CLI missing: $command" }
    }
    docker compose version > $null
    if ($LASTEXITCODE -ne 0) { throw 'Docker Compose is unavailable.' }
    $dockerHost = [Environment]::GetEnvironmentVariable('DOCKER_HOST')
    if ([string]::IsNullOrEmpty($dockerHost)) {
        $context = docker context inspect | ConvertFrom-Json
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect Docker context.' }
        $dockerHost = $context[0].Endpoints.docker.Host
    }
    if ($dockerHost -notmatch '^(npipe|unix)://') { throw 'Smoke refuses a remote Docker endpoint.' }
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try { $listener.Start() } catch { throw "Loopback port $Port is occupied; no services started." } finally { $listener.Stop() }
    $existing = @(docker ps -aq --filter "label=com.docker.compose.project=$project")
    if ($LASTEXITCODE -ne 0 -or $existing.Count) { throw 'Cannot acquire an unused disposable Docker project.' }
    $work = Join-Path ([IO.Path]::GetTempPath()) $project
    New-Item -ItemType Directory -Path $work | Out-Null
    # An explicit empty file prevents Compose from loading the repository's .env.
    $envFile = Join-Path $work 'compose.env'
    Set-Content -LiteralPath $envFile -Value ''
    $composeArgs = @('compose', '--env-file', $envFile, '-p', $project, '-f', $composeFile)
    [Environment]::SetEnvironmentVariable('LANGFUSE_SMOKE_PORT', [string]$Port)
    [Environment]::SetEnvironmentVariable('SKILLPULSE_RUN_LANGFUSE_SMOKE', '1')
    [Environment]::SetEnvironmentVariable('SKILLPULSE_LANGFUSE_ENABLED', 'true')
    [Environment]::SetEnvironmentVariable('SKILLPULSE_LANGFUSE_BASE_URL', "http://127.0.0.1:$Port")
    [Environment]::SetEnvironmentVariable('SKILLPULSE_LANGFUSE_PUBLIC_KEY', 'pk-lf-skillpulse-smoke')
    [Environment]::SetEnvironmentVariable('SKILLPULSE_LANGFUSE_SECRET_KEY', 'sk-lf-skillpulse-smoke')
    foreach ($name in 'SKILLPULSE_HN_LIVE_FETCH_ENABLED', 'SKILLPULSE_LLM_ENABLED', 'SKILLPULSE_AGENT_LLM_ENABLED') {
        [Environment]::SetEnvironmentVariable($name, 'false')
    }
    Set-Location $repository
    Write-Host "Starting disposable local Langfuse project $project."
    $attemptedStart = $true
    & docker @composeArgs up -d --wait --wait-timeout $StartupTimeoutSeconds
    if ($LASTEXITCODE -ne 0) { throw 'Disposable Langfuse stack startup failed.' }
    $basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('pk-lf-skillpulse-smoke:sk-lf-skillpulse-smoke'))
    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/public/projects" -Headers @{ Authorization = "Basic $basic" } -TimeoutSec 3 -MaximumRedirection 0
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw 'Local Langfuse project API did not become ready within the deadline.' }
    $junit = Join-Path $work 'live-results.xml'
    & uv run pytest tests/test_langfuse_live.py -q --junitxml $junit
    if ($LASTEXITCODE -ne 0) { throw 'Live pytest did not pass (including zero selected tests).' }
    if (-not (Test-Path -LiteralPath $junit)) { throw 'Live pytest produced no result report.' }
    Assert-ExecutedTests ([xml](Get-Content -LiteralPath $junit -Raw))
    $result = 0
} catch {
    Write-Host ('Local Langfuse smoke failed: ' + $_.Exception.Message)
} finally {
    try {
        if ($attemptedStart) {
            & docker @composeArgs down --volumes --remove-orphans --timeout 10
            if ($LASTEXITCODE -ne 0) { $result = 1; Write-Warning 'Disposable project cleanup failed.' }
        }
    } catch { $result = 1; Write-Warning 'Disposable project cleanup failed.' }
    finally {
        foreach ($name in $names) {
            if ($null -eq $saved[$name]) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
        }
        Set-Location $originalLocation
        if ($work -and (Test-Path -LiteralPath $work)) { Remove-Item -LiteralPath $work -Recurse -Force }
    }
}
if ($result -eq 0) { Write-Host 'Live Langfuse ingestion, privacy, disabled-export and cleanup checks passed.' }
exit $result
