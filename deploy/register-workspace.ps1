# Register a current-user URL protocol. No administrator rights required.
[CmdletBinding()]
param([string]$WorkspaceRoot, [string]$WebOrigin)
$ErrorActionPreference = 'Stop'
$taskHandler = Join-Path $PSScriptRoot 'open-workspace.ps1'
if (-not (Test-Path -LiteralPath $taskHandler)) { throw 'Workspace protocol handler missing.' }
$taskHiddenLauncher = Join-Path $PSScriptRoot 'open-workspace-hidden.vbs'
$taskScriptHost = Join-Path $env:WINDIR 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $taskHiddenLauncher)) { throw 'Hidden workspace launcher missing.' }
if (-not (Test-Path -LiteralPath $taskScriptHost)) { throw 'Windows Script Host is unavailable.' }
if (-not $WorkspaceRoot -and (Get-Command docker -ErrorAction SilentlyContinue)) {
    $taskMounts = @()
    try {
        # JSON avoids native argument quote stripping in Windows PowerShell 5.1.
        $taskInspect = & docker inspect agent-harness-backend-1 --format '{{json .Mounts}}' 2>$null
        if ($LASTEXITCODE -eq 0) { $taskMounts = @((($taskInspect -join '') | ConvertFrom-Json)) }
    } catch { Write-Verbose 'No running backend was available for workspace detection.' }
    $taskWorkspaceMount = $taskMounts | Where-Object Destination -eq '/app/workspace' | Select-Object -First 1
    if ($taskWorkspaceMount) {
        if ($taskWorkspaceMount.Source -notmatch '^[A-Za-z]:[/\\]') {
            throw 'The running backend uses a Docker volume, not a Windows folder. Start with start-windows.ps1 first.'
        }
        $WorkspaceRoot = $taskWorkspaceMount.Source
    }
}
if (-not $WorkspaceRoot) {
    $WorkspaceRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'workspace'
}
$WorkspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
if (-not (Test-Path -LiteralPath $WorkspaceRoot -PathType Container)) {
    throw 'Workspace folder missing. Start the Windows deployment first or pass -WorkspaceRoot.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'pick-workspace.ps1'))) {
    throw 'Native directory picker handler missing.'
}
if (-not $WebOrigin) {
    $taskPort = '8088'
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        try {
            $taskWebInspect = & docker inspect agent-harness-web-1 --format '{{json .HostConfig.PortBindings}}' 2>$null
            if ($LASTEXITCODE -eq 0) {
                $taskBindings = ($taskWebInspect -join '') | ConvertFrom-Json
                $taskPort = @($taskBindings.'80/tcp')[0].HostPort
            }
        } catch { Write-Verbose 'Using the default local callback port.' }
    }
    $WebOrigin = "http://127.0.0.1:$taskPort"
}
if ($WebOrigin -notmatch '^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$' -or ([uri]$WebOrigin).Port -le 0) {
    throw 'WebOrigin must be a local HTTP origin.'
}
$taskKey = 'HKCU:\Software\Classes\agent-harness-workspace'
New-Item -Path $taskKey -Force | Out-Null
Set-Item -LiteralPath $taskKey -Value 'URL:Agent Harness workspace'
New-ItemProperty -Path $taskKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$taskKey\shell\open\command" -Force | Out-Null
$taskCommand = '"' + $taskScriptHost + '" //B //Nologo "' + $taskHiddenLauncher + '" "' + $WorkspaceRoot + '" "' + $WebOrigin + '" "%1"'
Set-Item -LiteralPath "$taskKey\shell\open\command" -Value $taskCommand
Write-Host "Workspace opener and native folder picker registered: $WorkspaceRoot"
