# Register a current-user URL protocol. No administrator rights required.
[CmdletBinding()]
param([string]$WorkspaceRoot)
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
$taskKey = 'HKCU:\Software\Classes\agent-harness-workspace'
New-Item -Path $taskKey -Force | Out-Null
Set-Item -LiteralPath $taskKey -Value 'URL:Agent Harness workspace'
New-ItemProperty -Path $taskKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$taskKey\shell\open\command" -Force | Out-Null
$taskCommand = '"' + $taskScriptHost + '" //B //Nologo "' + $taskHiddenLauncher + '" "' + $WorkspaceRoot + '" "%1"'
Set-Item -LiteralPath "$taskKey\shell\open\command" -Value $taskCommand
Write-Host "Workspace folder opener registered: $WorkspaceRoot"
