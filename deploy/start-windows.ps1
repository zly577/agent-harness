# Windows PowerShell 5.1 / PowerShell 7. No Python or .bat required.
[CmdletBinding()]
param(
    [switch]$Sandbox,
    [switch]$ConfigureOnly,
    [switch]$SkipDesktopIntegration,
    [ValidateSet('Up', 'Down', 'Status', 'Logs')]
    [string]$Action = 'Up'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskOverride = Join-Path $taskRoot 'docker-compose.host.generated.yml'
$taskWorkspace = Join-Path $taskRoot 'workspace'

function Write-HostDriveConfig {
    $taskVolumes = @()
    $taskMounts = @()
    foreach ($taskDrive in [System.IO.DriveInfo]::GetDrives()) {
        try {
            if (-not $taskDrive.IsReady) { continue }
            if ($taskDrive.DriveType.ToString() -notin @('Fixed', 'Removable')) { continue }
            if ($taskDrive.Name -notmatch '^[A-Za-z]:\\$') { continue }
            $taskLetter = $taskDrive.Name.Substring(0, 1).ToLowerInvariant()
            $taskSource = $taskDrive.Name.Replace('\', '/')
            $taskVolumes += [ordered]@{
                type = 'bind'
                source = $taskSource
                target = "/host/$taskLetter"
                read_only = $false
                bind = @{ create_host_path = $false }
            }
            $taskMounts += "$taskLetter=/host/$taskLetter"
            Write-Host "Mode-controlled drive: $taskSource -> /mnt/$taskLetter"
        } catch {
            Write-Warning "Skipping an unavailable drive: $($_.Exception.Message)"
        }
    }
    if ($taskVolumes.Count -eq 0) {
        throw 'No ready local fixed/removable drives found. Run this entry point on Windows.'
    }
    $taskVolumes += [ordered]@{
        type = 'bind'
        source = $taskWorkspace.Replace('\', '/')
        target = '/app/workspace'
        bind = @{ create_host_path = $false }
    }
    $taskConfig = [ordered]@{
        services = @{
            backend = @{
                volumes = $taskVolumes
                environment = @{
                    HARNESS_MACHINE_ACCESS = 'true'
                    HARNESS_MACHINE_MOUNTS = ($taskMounts -join ',')
                    HARNESS_ALLOW_FULL_ACCESS = 'true'
                    HARNESS_ACCESS_MODE = 'sandbox'
                    WORKSPACE_OPEN_PROTOCOL = $(if ($SkipDesktopIntegration) { 'false' } else { 'true' })
                    MCP_ENABLED = 'true'
                    SANDBOX_MOUNT_MACHINE_DIRS = 'false'
                    ALLOW_REGISTRATION = 'false'
                }
            }
        }
    }
    # JSON is valid YAML; avoid path quoting differences across PowerShell versions.
    $taskText = ConvertTo-Json -InputObject $taskConfig -Depth 8
    [System.IO.File]::WriteAllText($taskOverride, $taskText + "`n", [System.Text.UTF8Encoding]::new($false))
    Write-Host 'Default mode reads disks; full mode can modify disks and run LocalShell. Personal deployment only.'
}

function Invoke-TaskDocker {
    param([string[]]$DockerArgs)
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE)." }
}

Push-Location $taskRoot
try {
    if ($Action -eq 'Up' -or $ConfigureOnly) {
        Write-HostDriveConfig
    } elseif (-not (Test-Path -LiteralPath $taskOverride)) {
        throw 'Generated host config not found. Run the Up action first.'
    }
    if ($ConfigureOnly) {
        Write-Host 'Configuration generated. No containers were started.'
        return
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Install Docker Desktop with Linux containers and start it first.'
    }
    if ($Action -eq 'Up') {
        $taskDockerOs = Invoke-TaskDocker -DockerArgs @('info', '--format', '{{.OSType}}')
        if (($taskDockerOs -join '').Trim() -ne 'linux') {
            throw 'Switch Docker Desktop to Linux containers first.'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $taskRoot '.env'))) {
            Invoke-TaskDocker -DockerArgs @(
                'run', '--rm', '--mount', "type=bind,source=$taskRoot,target=/project",
                '-w', '/project', 'python:3.12-slim', 'python', 'deploy/init-env.py'
            )
        }
        New-Item -ItemType Directory -Path $taskWorkspace -Force | Out-Null
        # One-time import of the previous named workspace volume. Stop writers
        # and preserve any existing host files; never delete the old volume.
        $taskMigrationMarker = Join-Path $taskWorkspace '.docker-import-complete'
        if (-not (Test-Path -LiteralPath $taskMigrationMarker)) {
            $taskLegacyVolume = & docker volume ls --filter 'name=^agent-harness_workspace_data$' --format '{{.Name}}'
            if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect existing workspace volume.' }
            if ($taskLegacyVolume -contains 'agent-harness_workspace_data') {
                Invoke-TaskDocker -DockerArgs @('compose', '-f', 'docker-compose.yml', 'stop', 'backend')
                Invoke-TaskDocker -DockerArgs @(
                    'run', '--rm', '--mount', 'type=volume,source=agent-harness_workspace_data,target=/old,readonly',
                    '--mount', "type=bind,source=$taskWorkspace,target=/new", 'alpine:3.22',
                    'sh', '-c', 'cp -an /old/. /new/'
                )
            }
            [System.IO.File]::WriteAllText($taskMigrationMarker, 'Imported without replacing existing files.')
        }
        if (-not $SkipDesktopIntegration) {
            & (Join-Path $PSScriptRoot 'register-workspace.ps1')
        }
    }
    $taskComposeArgs = @('compose', '-f', 'docker-compose.yml')
    if ($Sandbox) { $taskComposeArgs += @('-f', 'docker-compose.sandbox.yml') }
    $taskComposeArgs += @('-f', 'docker-compose.host.generated.yml')
    switch ($Action) {
        'Up' {
            Invoke-TaskDocker -DockerArgs ($taskComposeArgs + @('config', '--quiet'))
            Invoke-TaskDocker -DockerArgs ($taskComposeArgs + @('up', '-d', '--build', '--wait', '--wait-timeout', '300'))
            Write-Host 'Ready. Open the web address configured by WEB_PORT (default http://localhost:8088).'
            Write-Host 'Find initial administrator credentials in the root .env file.'
        }
        'Down' { Invoke-TaskDocker -DockerArgs ($taskComposeArgs + @('down')) }
        'Status' { Invoke-TaskDocker -DockerArgs ($taskComposeArgs + @('ps')) }
        'Logs' { Invoke-TaskDocker -DockerArgs ($taskComposeArgs + @('logs', '-f', '--tail', '100', 'backend')) }
    }
} finally {
    Pop-Location
}
