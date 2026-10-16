$ErrorActionPreference = 'Stop'
# The strict-mode child deliberately exits 1; assert that exit instead of throwing early.
$PSNativeCommandUseErrorActionPreference = $false
$script = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts/acceptance-preflight.ps1'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('skillpulse-preflight-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null
$names = @('SKILLPULSE_LLM_BASE_URL', 'SKILLPULSE_LLM_MODEL', 'SKILLPULSE_LLM_API_KEY', 'SKILLPULSE_AGENT_LLM_BASE_URL', 'SKILLPULSE_AGENT_LLM_MODEL', 'SKILLPULSE_AGENT_LLM_API_KEY', 'SKILLPULSE_LANGFUSE_BASE_URL', 'SKILLPULSE_LANGFUSE_PUBLIC_KEY', 'SKILLPULSE_LANGFUSE_SECRET_KEY')
$saved = @{}
foreach ($name in $names) {
    $saved[$name] = [Environment]::GetEnvironmentVariable($name)
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}
$assertions = 0
function Assert([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:assertions++
}
try {
    $missing = @(& $script -RepositoryRoot $temp)
    Assert ($missing.Count -eq 7) 'All seven checks must execute.'
    Assert (($missing | Where-Object Gate -eq 'Langfuse configuration').Status -eq 'NOT_CONFIGURED') 'Absent credentials must fail.'
    Assert (($missing | Where-Object Gate -eq 'CI workflow files').Status -eq 'NOT_CONFIGURED') 'Absent workflows must fail.'
    $secret = 'private-preflight-sentinel'
    $lines = @($names | ForEach-Object { "$_=`"$secret`"" })
    $lines += 'SKILLPULSE_HN_LIVE_FETCH_ENABLED=true'
    Set-Content (Join-Path $temp '.env') $lines
    New-Item -ItemType Directory (Join-Path $temp '.github/workflows') -Force | Out-Null
    Set-Content (Join-Path $temp '.github/workflows/deploy-check.yml') '# fixture'
    Set-Content (Join-Path $temp '.github/workflows/release.yml') '# fixture'
    $desktop = Join-Path $temp 'desktop.exe'
    Set-Content $desktop '# fixture only; never executed'
    $ready = @(& $script -RepositoryRoot $temp -DesktopExecutable $desktop)
    foreach ($gate in 'LLM configuration', 'AGENT_LLM configuration', 'Langfuse configuration', 'Desktop executable', 'CI workflow files') {
        Assert (($ready | Where-Object Gate -eq $gate).Status -eq 'READY') "Configured prerequisite should be ready: $gate"
    }
    Assert (($ready | Where-Object Gate -eq 'HN authorization and labels').Status -eq 'BLOCKED') 'An enabled flag must never grant HN permission.'
    Assert (($ready | ConvertTo-Json -Depth 3) -notmatch $secret) 'Secrets must not enter output.'
    $strictOutput = & pwsh -NoProfile -File $script -RepositoryRoot $temp -Strict 2>&1
    Assert ($LASTEXITCODE -eq 1) 'Strict mode must fail for unresolved external evidence.'
    Set-Content (Join-Path $temp '.env') ($lines -replace "SKILLPULSE_LANGFUSE_SECRET_KEY=.*", 'SKILLPULSE_LANGFUSE_SECRET_KEY=changeme')
    $placeholder = @(& $script -RepositoryRoot $temp)
    Assert (($placeholder | Where-Object Gate -eq 'Langfuse configuration').Status -eq 'NOT_CONFIGURED') 'A placeholder must fail even when every other key exists.'
    Set-Content (Join-Path $temp '.env') $lines
    [Environment]::SetEnvironmentVariable('SKILLPULSE_LANGFUSE_SECRET_KEY', 'changeme')
    $override = @(& $script -RepositoryRoot $temp)
    Assert (($override | Where-Object Gate -eq 'Langfuse configuration').Status -eq 'NOT_CONFIGURED') 'Process environment must override .env, just as application Settings do.'
    Remove-Item Env:SKILLPULSE_LANGFUSE_SECRET_KEY
    Set-Content (Join-Path $temp '.env') 'SKILLPULSE_LANGFUSE_SECRET_KEY=present-but-incomplete'
    $partial = @(& $script -RepositoryRoot $temp)
    Assert (($partial | Where-Object Gate -eq 'Langfuse configuration').Status -eq 'NOT_CONFIGURED') 'Partial settings must fail.'
    "PASS: $assertions assertions; missing and configured fixtures, HN authorization guard, secret redaction and strict failure."
} finally {
    foreach ($name in $names) {
        if ($null -eq $saved[$name]) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
        else { [Environment]::SetEnvironmentVariable($name, $saved[$name]) }
    }
    Remove-Item -LiteralPath $temp -Recurse -Force
}
# GitHub's pwsh wrapper forwards LASTEXITCODE. Clear the expected strict-child failure
# only after every assertion and cleanup succeeded; exceptions still exit nonzero.
exit 0
