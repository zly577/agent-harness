# Exercise the entry point with a Docker stub; never start containers or mount disks.
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskFixture = Join-Path ([System.IO.Path]::GetTempPath()) ('harness-launcher-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path (Join-Path $taskFixture 'deploy') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $taskRepo 'deploy/start-windows.ps1') -Destination (Join-Path $taskFixture 'deploy/start-windows.ps1')
[System.IO.File]::WriteAllText((Join-Path $taskFixture '.env'), 'existing-config')
$taskEntry = Join-Path $taskFixture 'deploy/start-windows.ps1'
$global:taskDockerCalls = [System.Collections.Generic.List[object]]::new()
$global:taskDockerFail = $false

function global:docker {
    $global:taskDockerCalls.Add(@($args))
    $global:LASTEXITCODE = 0
    if ($global:taskDockerFail) { $global:LASTEXITCODE = 9; return }
    if ($args[0] -eq 'info') { 'linux' }
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
foreach ($taskVolume in $taskConfig.services.backend.volumes) {
    Assert-Task ($taskVolume.type -eq 'bind' -and $taskVolume.read_only -eq $true) 'Drive not read-only.'
    Assert-Task ($taskVolume.source -match '^[A-Z]:/$' -and $taskVolume.target -match '^/host/[a-z]$') 'Invalid drive mapping.'
    Assert-Task ($taskVolume.bind.create_host_path -eq $false) 'Missing drives could be silently created.'
}
Assert-Task ($taskConfig.services.backend.environment.HARNESS_ALLOW_FULL_ACCESS -eq 'false') 'full mode enabled.'
Assert-Task ($taskConfig.services.backend.environment.ALLOW_REGISTRATION -eq 'false') 'Registration enabled.'

& $taskEntry -Sandbox
$taskUp = $global:taskDockerCalls[$global:taskDockerCalls.Count - 1] -join ' '
Assert-Task ($taskUp -match 'docker-compose.sandbox.yml' -and $taskUp -match 'up -d --build --wait') 'Sandbox startup args incorrect.'
Assert-Task ((Get-Content -LiteralPath (Join-Path $taskFixture '.env') -Raw) -eq 'existing-config') 'Existing .env overwritten.'
Assert-Task (-not (@($global:taskDockerCalls | Where-Object { $_[0] -eq 'run' }).Count)) 'Environment regenerated despite existing .env.'

& $taskEntry -Action Down -Sandbox
$taskDown = $global:taskDockerCalls[$global:taskDockerCalls.Count - 1] -join ' '
Assert-Task ($taskDown -match 'docker-compose.sandbox.yml' -and $taskDown -match ' down$' -and $taskDown -notmatch ' -v') 'Shutdown does not preserve volumes.'
& $taskEntry -Action Status
Assert-Task (($global:taskDockerCalls[$global:taskDockerCalls.Count - 1] -join ' ') -match ' ps$') 'Status args incorrect.'

$global:taskDockerFail = $true
$taskCaught = $false
try { & $taskEntry -Action Status } catch { $taskCaught = $true }
Assert-Task $taskCaught 'Docker error swallowed.'
Write-Host 'Windows launcher checks passed: mappings, dry run, startup, env preservation, shutdown and errors.'
