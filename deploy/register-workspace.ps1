# Register a current-user URL protocol. No administrator rights required.
$ErrorActionPreference = 'Stop'
$taskHandler = Join-Path $PSScriptRoot 'open-workspace.ps1'
if (-not (Test-Path -LiteralPath $taskHandler)) { throw 'Workspace protocol handler missing.' }
$taskKey = 'HKCU:\Software\Classes\agent-harness-workspace'
New-Item -Path $taskKey -Force | Out-Null
Set-Item -LiteralPath $taskKey -Value 'URL:Agent Harness workspace'
New-ItemProperty -Path $taskKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$taskKey\shell\open\command" -Force | Out-Null
$taskCommand = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "' + $taskHandler + '" -WorkspaceUri "%1"'
Set-Item -LiteralPath "$taskKey\shell\open\command" -Value $taskCommand
Write-Host 'Workspace folder opener registered for the current Windows user.'
