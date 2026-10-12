[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$WorkspaceUri, [string]$WorkspaceRoot, [switch]$ValidateOnly)
$ErrorActionPreference = 'Stop'
# Validate the entire raw URL before interpreting it or starting any process.
if ($WorkspaceUri -notmatch '^agent-harness-workspace://open/([0-9a-f]{32})/?$') {
    throw 'Invalid workspace URL.'
}
$taskUserId = $Matches[1]
if (-not $WorkspaceRoot) { $WorkspaceRoot = Join-Path (Split-Path -Parent $PSScriptRoot) 'workspace' }
$taskBase = [System.IO.Path]::GetFullPath($WorkspaceRoot).TrimEnd('\', '/')
$taskPath = [System.IO.Path]::GetFullPath((Join-Path $taskBase $taskUserId))
if (-not $taskPath.StartsWith($taskBase + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Workspace path outside allowed directory.'
}
if ($ValidateOnly) { Write-Output $taskPath; return }
if (-not (Test-Path -LiteralPath $taskPath -PathType Container)) { throw 'Workspace does not exist. Start the app and create files first.' }
if ((Get-Item -LiteralPath $taskBase).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Workspace root must not be a link.' }
if ((Get-Item -LiteralPath $taskPath).Attributes -band [System.IO.FileAttributes]::ReparsePoint) { throw 'Workspace must not be a link.' }
Start-Process -FilePath 'explorer.exe' -ArgumentList ('"' + $taskPath + '"') -WindowStyle Normal
