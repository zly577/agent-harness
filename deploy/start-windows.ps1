# Windows PowerShell 5.1 / PowerShell 7. No Python or .bat required.
[CmdletBinding()]
param(
    [switch]$Sandbox,
    [switch]$ConfigureOnly,
    [ValidateSet('Up', 'Down', 'Status', 'Logs')]
    [string]$Action = 'Up'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskOverride = Join-Path $taskRoot 'docker-compose.host.generated.yml'

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
                read_only = $true
                bind = @{ create_host_path = $false }
            }
            $taskMounts += "$taskLetter=/host/$taskLetter"
            Write-Host "Read-only drive: $taskSource -> /mnt/$taskLetter"
        } catch {
            Write-Warning "Skipping an unavailable drive: $($_.Exception.Message)"
        }
    }
    if ($taskVolumes.Count -eq 0) {
        throw 'No ready local fixed/removable drives found. Run this entry point on Windows.'
    }
    $taskConfig = [ordered]@{
        services = @{
            backend = @{
                volumes = $taskVolumes
                environment = @{
                    HARNESS_MACHINE_ACCESS = 'true'
                    HARNESS_MACHINE_MOUNTS = ($taskMounts -join ',')
                    HARNESS_ALLOW_FULL_ACCESS = 'false'
                    SANDBOX_MOUNT_MACHINE_DIRS = 'false'
                    ALLOW_REGISTRATION = 'false'
                }
            }
        }
    }
    # JSON is valid YAML; avoid path quoting differences across PowerShell versions.
    $taskText = ConvertTo-Json -InputObject $taskConfig -Depth 8
    [System.IO.File]::WriteAllText($taskOverride, $taskText + "`n", [System.Text.UTF8Encoding]::new($false))
    Write-Host 'Host disks are shared across existing application accounts. Use this mode for personal deployment.'
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
