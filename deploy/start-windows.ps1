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
    # 自助注册开关改由根目录 .env 驱动（缺省 false）。本函数每次运行都会重写生成文件，
    # 直接改生成文件会被覆盖，所以把开关放在 .env 里统一管理。
    $taskAllowRegistration = 'false'
    $taskEnvFile = Join-Path $taskRoot '.env'
    if (Test-Path -LiteralPath $taskEnvFile) {
        foreach ($taskLine in [System.IO.File]::ReadAllLines($taskEnvFile)) {
            if ($taskLine -match '^\s*ALLOW_REGISTRATION\s*=\s*(\S+)\s*$') {
                if ($Matches[1] -match '^(?i:true)$') { $taskAllowRegistration = 'true' }
            }
        }
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
                    ALLOW_REGISTRATION = $taskAllowRegistration
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
    # docker compose 会把 "Image ... Building" / "Container ... Healthy" 这类**正常**
    # 状态行写到 stderr。Windows PowerShell 5.1 在 $ErrorActionPreference='Stop' 下会把
    # 原生命令的 stderr 当作终止性错误抛出（NativeCommandError），脚本遂在构建途中中断
    # ——容器可能仍被守护进程补齐，但退出码为 1、日志丢失，后续 --wait 也不再等待。
    # 这里只在调用 docker 期间放宽错误策略，成功与否一律以退出码为准。
    $taskPrevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker @DockerArgs
        $taskDockerExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $taskPrevEap
    }
    if ($taskDockerExit -ne 0) { throw "Docker command failed (exit $taskDockerExit)." }
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
    }
    $taskComposeArgs = @('compose', '-f', 'docker-compose.yml')
    if ($Sandbox) { $taskComposeArgs += @('-f', 'docker-compose.sandbox.yml') }
    $taskComposeArgs += @('-f', 'docker-compose.host.generated.yml')
    switch ($Action) {
        'Up' {
            Invoke-TaskDocker -DockerArgs ($taskComposeArgs + @('config', '--quiet'))
            Invoke-TaskDocker -DockerArgs ($taskComposeArgs + @('up', '-d', '--build', '--wait', '--wait-timeout', '300'))
            if (-not $SkipDesktopIntegration) {
                & (Join-Path $PSScriptRoot 'register-workspace.ps1') -WorkspaceRoot $taskWorkspace
            }
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
