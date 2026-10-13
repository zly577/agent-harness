[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$WorkspaceUri, [string]$WorkspaceRoot, [string]$WebOrigin = 'http://127.0.0.1:8088', [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
# New URLs carry a single-use ticket for the exact authorized directory.
if ($WorkspaceUri -match '^agent-harness-workspace://open/([0-9a-f]{64})/?$') {
    $taskTicket = $Matches[1]
    if ($WebOrigin -notmatch '^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$' -or ([uri]$WebOrigin).Port -le 0) {
        throw 'Workspace callback must use a local HTTP origin.'
    }
    $taskSelection = Invoke-RestMethod -Uri "$WebOrigin/api/v1/files/open-workspace/$taskTicket" -TimeoutSec 10
    $taskInspect = & docker inspect agent-harness-backend-1 --format '{{json .Mounts}}' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect backend directory mounts.' }
    . (Join-Path $PSScriptRoot 'workspace-paths.ps1')
    $taskMounts = @((($taskInspect -join '') | ConvertFrom-Json))
    $taskPath = ConvertTo-WindowsDirectory -ContainerPath $taskSelection.directory -Mounts $taskMounts
    if ($ValidateOnly) { Write-Output $taskPath; return }
    if (-not (Test-Path -LiteralPath $taskPath -PathType Container)) { throw 'Workspace directory no longer exists.' }
    Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $taskPath + '"') -WindowStyle Normal
    return
}
# Retain old default-workspace URLs for already-open browser pages.
# Validate the entire raw URL before interpreting it or starting any process.
if ($WorkspaceUri -notmatch '^agent-harness-workspace://open/([0-9a-f]{32})(?:/([0-9a-f]{32}))?/?$') {
    throw 'Invalid workspace URL.'
}
$taskUserId = $Matches[1]
$taskWorkspaceId = $Matches[2]
if (-not $WorkspaceRoot) { $WorkspaceRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'workspace' }
$taskBase = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\', '/')
$taskRelative = $taskUserId
if ($taskWorkspaceId) { $taskRelative = Join-Path (Join-Path $taskUserId "workspaces") $taskWorkspaceId }
$taskPath = [System.IO.Path]::GetFullPath((Join-Path $taskBase $taskRelative))
if (-not $taskPath.StartsWith($taskBase + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Workspace path outside allowed directory.'
}
if ($ValidateOnly) { Write-Output $taskPath; return }
if (-not (Test-Path -LiteralPath $taskPath -PathType Container)) { throw 'Workspace does not exist. Start the app and create files first.' }
if ((Get-Item -LiteralPath $taskBase).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Workspace root must not be a link.' }
if ((Get-Item -LiteralPath $taskPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Workspace must not be a link.' }
Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $taskPath + '"') -WindowStyle Normal
