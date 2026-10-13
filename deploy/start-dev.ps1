[CmdletBinding()]
param([string]$PythonPath, [int]$BackendPort = 8000, [int]$FrontendPort = 5173)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskLogs = Join-Path $taskRoot 'logs'
New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
if (-not $PythonPath) {
    $taskConda = Join-Path $env:USERPROFILE 'anaconda3/envs/agent_env/python.exe'
    $taskVenv = Join-Path $taskRoot 'backend/.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $taskVenv) { $PythonPath = $taskVenv }
    elseif (Test-Path -LiteralPath $taskConda) { $PythonPath = $taskConda }
    else { throw 'Specify -PythonPath with a Python 3.12 environment containing backend dependencies.' }
}
function Get-TaskFreePort([int]$Start) {
    for ($taskPort = $Start; $taskPort -lt ($Start + 100); $taskPort++) {
        $taskListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $taskPort)
        try { $taskListener.Start(); return $taskPort }
        catch [System.Net.Sockets.SocketException] { }
        finally { $taskListener.Stop() }
    }
    throw "No available port near $Start."
}
$BackendPort = Get-TaskFreePort $BackendPort
$FrontendPort = Get-TaskFreePort $FrontendPort
$taskBackend = Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--reload', '--reload-dir', 'app', '--timeout-graceful-shutdown', '5', '--host', '127.0.0.1', '--port', "$BackendPort") -WorkingDirectory (Join-Path $taskRoot 'backend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogs "dev-backend-$BackendPort.out.log") -RedirectStandardError (Join-Path $taskLogs "dev-backend-$BackendPort.err.log") -PassThru
$taskOldApiUrl = $env:HARNESS_DEV_API_URL
try {
    $env:HARNESS_DEV_API_URL = "http://127.0.0.1:$BackendPort"
    $taskNode = (Get-Command node.exe).Source
    $taskFrontend = Start-Process -FilePath $taskNode -ArgumentList @('node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', "$FrontendPort", '--strictPort') -WorkingDirectory (Join-Path $taskRoot 'frontend') -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogs "dev-frontend-$FrontendPort.out.log") -RedirectStandardError (Join-Path $taskLogs "dev-frontend-$FrontendPort.err.log") -PassThru
} finally { $env:HARNESS_DEV_API_URL = $taskOldApiUrl }
$taskState = [ordered]@{ backendUrl = "http://127.0.0.1:$BackendPort"; frontendUrl = "http://127.0.0.1:$FrontendPort"; backendPid = $taskBackend.Id; frontendPid = $taskFrontend.Id }
$taskState | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskLogs 'dev-session.json') -Encoding utf8
$taskState | ConvertTo-Json
