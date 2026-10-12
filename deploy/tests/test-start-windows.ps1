# Exercise the entry point with a Docker stub; never start containers or mount disks.
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskFixture = Join-Path ([System.IO.Path]::GetTempPath()) ('harness-launcher-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $taskFixture 'deploy') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $taskRepo 'deploy/start-windows.ps1') -Destination (Join-Path $taskFixture 'deploy/start-windows.ps1')
Copy-Item -LiteralPath (Join-Path $taskRepo 'deploy/open-workspace.ps1') -Destination (Join-Path $taskFixture 'deploy/open-workspace.ps1')
[System.IO.File]::WriteAllText((Join-Path $taskFixture '.env'), 'existing-config')
$taskEntry = Join-Path $taskFixture 'deploy/start-windows.ps1'
$global:taskDockerCalls = [System.Collections.Generic.List[object]]::new()
$global:taskDockerFail = $false
$global:taskLegacyVolumeExists = $false

function global:docker {
    $global:taskDockerCalls.Add(@($args))
    $global:LASTEXITCODE = 0
    if ($global:taskDockerFail) { $global:LASTEXITCODE = 9; return }
    if ($args[0] -eq 'info') { 'linux' }
    if ($args[0] -eq 'volume' -and $global:taskLegacyVolumeExists) { 'agent-harness_workspace_data' }
}

function Assert-Task {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

& $taskEntry -ConfigureOnly
Assert-Task ($global:taskDockerCalls.Count -eq 0) 'ConfigureOnly invoked Docker.'
$taskConfigPath = Join-Path $taskFixture 'docker-compose.host.generated.yml'
$taskConfig = Get-Content -LiteralPath $taskConfigPath -Raw | ConvertFrom-Json
Assert-Task (@($taskConfig.services.backend.volumes).Count -gt 0) 'No drives found.'
foreach ($taskVolume in ($taskConfig.services.backend.volumes | Where-Object { $_.target -like '/host/*' })) {
    Assert-Task ($taskVolume.type -eq 'bind' -and $taskVolume.read_only -eq $false) 'Drive cannot support full-mode writes.'
    Assert-Task ($taskVolume.source -match '^[A-Z]:/$' -and $taskVolume.target -match '^/host/[a-z]$') 'Invalid drive mapping.'
    Assert-Task ($taskVolume.bind.create_host_path -eq $false) 'Missing drives could be silently created.'
}
Assert-Task ($taskConfig.services.backend.environment.HARNESS_ALLOW_FULL_ACCESS -eq 'true') 'full mode unavailable.'
Assert-Task ($taskConfig.services.backend.environment.HARNESS_ACCESS_MODE -eq 'sandbox') 'Startup default is not sandbox.'
Assert-Task ($taskConfig.services.backend.environment.MCP_ENABLED -eq 'true') 'MCP disabled.'
Assert-Task (@($taskConfig.services.backend.volumes | Where-Object { $_.target -eq '/app/workspace' }).Count -eq 1) 'Host workspace bind missing.'
Assert-Task ($taskConfig.services.backend.environment.ALLOW_REGISTRATION -eq 'false') 'Registration enabled.'

& $taskEntry -Sandbox -SkipDesktopIntegration
$taskUp = $global:taskDockerCalls[$global:taskDockerCalls.Count - 1] -join ' '
Assert-Task ($taskUp -match 'docker-compose.sandbox.yml' -and $taskUp -match 'up -d --build --wait') 'Sandbox startup args incorrect.'
Assert-Task ((Get-Content -LiteralPath (Join-Path $taskFixture '.env') -Raw) -eq 'existing-config') 'Existing .env overwritten.'
Assert-Task (-not (@($global:taskDockerCalls | Where-Object { $_[0] -eq 'run' }).Count)) 'Environment regenerated despite existing .env.'

# Exercise an upgrade without copying any real files or starting containers.
Remove-Item -LiteralPath (Join-Path (Join-Path $taskFixture 'workspace') '.docker-import-complete')
$global:taskLegacyVolumeExists = $true
$taskBeforeMigration = $global:taskDockerCalls.Count
& $taskEntry -SkipDesktopIntegration
$taskMigrationCommands = @($global:taskDockerCalls | Select-Object -Skip $taskBeforeMigration | ForEach-Object { $_ -join ' ' })
$taskStopIndex = -1
$taskCopyIndex = -1
for ($taskIndex = 0; $taskIndex -lt $taskMigrationCommands.Count; $taskIndex++) {
    if ($taskMigrationCommands[$taskIndex] -match 'stop backend$') { $taskStopIndex = $taskIndex }
    if ($taskMigrationCommands[$taskIndex] -match 'cp -an /old/\. /new/') { $taskCopyIndex = $taskIndex }
}
Assert-Task ($taskStopIndex -ge 0 -and $taskCopyIndex -gt $taskStopIndex) 'Upgrade must stop writers before preserving-copy import.'
$taskBeforeRepeat = $global:taskDockerCalls.Count
& $taskEntry -SkipDesktopIntegration
Assert-Task (-not @($global:taskDockerCalls | Select-Object -Skip $taskBeforeRepeat | Where-Object { $_[0] -eq 'run' }).Count) 'Workspace imported repeatedly.'

$taskOpener = Join-Path $taskFixture 'deploy/open-workspace.ps1'
$taskUserId = 'a' * 32
$taskOpenPath = & $taskOpener -WorkspaceUri "agent-harness-workspace://open/$taskUserId" -ValidateOnly
Assert-Task ($taskOpenPath -eq (Join-Path (Join-Path $taskFixture 'workspace') $taskUserId)) 'Workspace URL maps to the wrong directory.'
foreach ($taskBadUri in @('https://example.com', 'agent-harness-workspace://open/../outside', "agent-harness-workspace://open/$taskUserId?command=bad")) {
    $taskRejected = $false
    try { & $taskOpener -WorkspaceUri $taskBadUri -ValidateOnly } catch { $taskRejected = $true }
    Assert-Task $taskRejected 'Unsafe workspace URL accepted.'
}

& $taskEntry -Action Down -Sandbox
$taskDown = $global:taskDockerCalls[$global:taskDockerCalls.Count - 1] -join ' '
Assert-Task ($taskDown -match 'docker-compose.sandbox.yml' -and $taskDown -match ' down$' -and $taskDown -notmatch ' -v') 'Shutdown does not preserve volumes.'
& $taskEntry -Action Status
Assert-Task (($global:taskDockerCalls[$global:taskDockerCalls.Count - 1] -join ' ') -match ' ps$') 'Status args incorrect.'

$global:taskDockerFail = $true
$taskCaught = $false
try { & $taskEntry -Action Status } catch { $taskCaught = $true }
Assert-Task $taskCaught 'Docker error swallowed.'
# The final failure was intentional. GitHub's PowerShell wrapper propagates
# LASTEXITCODE even after a caught exception, so clear the stub's exit status.
$global:LASTEXITCODE = 0
Write-Host 'Windows launcher checks passed: mappings, dry run, startup, env preservation, shutdown and errors.'
