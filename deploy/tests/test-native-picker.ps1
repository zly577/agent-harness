# Validate the native bridge without opening a window or changing registration.
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskPicker = Join-Path $taskRepo 'deploy/pick-workspace.ps1'
$taskTicket = 'a' * 64
function Assert-Task {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}
$taskActual = . $taskPicker -WorkspaceUri "agent-harness-workspace://pick/$taskTicket" -ValidateOnly
Assert-Task ($taskActual -eq $taskTicket) 'Picker ticket parsing failed.'
foreach ($taskBadUri in @('agent-harness-workspace://pick/abc', "agent-harness-workspace://pick/$taskTicket?cmd=bad", 'https://example.com')) {
    $taskRejected = $false
    try { & $taskPicker -WorkspaceUri $taskBadUri -ValidateOnly } catch { $taskRejected = $true }
    Assert-Task $taskRejected 'Unsafe picker URL accepted.'
}
$taskRejected = $false
try { & $taskPicker -WorkspaceUri "agent-harness-workspace://pick/$taskTicket" -WebOrigin 'http://example.com:8088' -ValidateOnly } catch { $taskRejected = $true }
Assert-Task $taskRejected 'Remote callback origin accepted.'
$taskMounts = @(
    @{ Type = 'bind'; Source = 'D:/'; Destination = '/host/d' },
    @{ Type = 'bind'; Source = 'D:/deployment/workspace'; Destination = '/app/workspace' }
)
Assert-Task ((ConvertTo-ContainerDirectory 'D:\project\中文' $taskMounts) -eq '/host/d/project/中文') 'Drive path conversion failed.'
Assert-Task ((ConvertTo-ContainerDirectory 'D:\deployment\workspace\user' $taskMounts) -eq '/app/workspace/user') 'Most specific mount not selected.'
Assert-Task ((ConvertTo-ContainerDirectory 'D:\' $taskMounts) -eq '/host/d') 'Drive root conversion failed.'
Assert-Task ((ConvertTo-ContainerDirectory 'D:\deployment\workspace-other' $taskMounts) -eq '/host/d/deployment/workspace-other') 'Mount boundary ignored.'
$taskRejected = $false
try { ConvertTo-ContainerDirectory 'C:\unmounted' $taskMounts } catch { $taskRejected = $true }
Assert-Task $taskRejected 'Unmounted drive accepted.'
Assert-Task ((ConvertTo-WindowsDirectory '/host/d/project/中文' $taskMounts) -eq 'D:\project\中文') 'Selected workspace reverse mapping failed.'
Assert-Task ((ConvertTo-WindowsDirectory '/app/workspace/user' $taskMounts) -eq 'D:\deployment\workspace\user') 'Default workspace reverse mapping failed.'
Assert-Task ((ConvertTo-WindowsDirectory '/host/d' $taskMounts) -eq 'D:\') 'Drive root reverse mapping failed.'
foreach ($taskBadPath in @('/host/d-other/project', '/host/d/../outside', '/unmounted/project')) {
    $taskRejected = $false
    try { ConvertTo-WindowsDirectory $taskBadPath $taskMounts } catch { $taskRejected = $true }
    Assert-Task $taskRejected 'Unsafe or unmounted container directory accepted.'
}
# Exercise the actual opener with stubbed HTTP and Docker: no Explorer launch.
function global:Invoke-WebRequest {
    $taskBytes = [System.Text.Encoding]::UTF8.GetBytes('{"directory":"/host/d/project/中文"}')
    @{ RawContentStream = [System.IO.MemoryStream]::new($taskBytes) }
}
function global:docker { $global:LASTEXITCODE = 0; ConvertTo-Json -InputObject $taskMounts -Compress }
$taskOpened = & (Join-Path $taskRepo 'deploy/open-workspace.ps1') -WorkspaceUri "agent-harness-workspace://open/$taskTicket" -ValidateOnly
Assert-Task ($taskOpened -eq 'D:\project\中文') 'Ticket-based opener did not resolve the selected directory.'
Remove-Item Function:\Invoke-WebRequest
# Real HTTP response deliberately omits charset, matching FastAPI. Run this
# test under Windows PowerShell 5.1 as well as PowerShell 7.
$taskSocket = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$taskSocket.Start()
$taskPort = $taskSocket.LocalEndpoint.Port
$taskSocket.Stop()
$taskOrigin = "http://127.0.0.1:$taskPort"
$taskServer = Start-Job -ArgumentList $taskOrigin -ScriptBlock {
    param($Origin)
    $listener = [System.Net.HttpListener]::new()
    $listener.Prefixes.Add("$Origin/")
    try {
        $listener.Start()
        'ready'
        $context = $listener.GetContext()
        $bytes = [System.Text.Encoding]::UTF8.GetBytes('{"directory":"/host/d/project/中文"}')
        $context.Response.ContentType = 'application/json'
        $context.Response.ContentLength64 = $bytes.Length
        $context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
        $context.Response.Close()
    } finally { $listener.Close() }
}
try {
    $taskReady = $false
    for ($taskAttempt = 0; $taskAttempt -lt 100; $taskAttempt++) {
        if ((Receive-Job $taskServer) -contains 'ready') { $taskReady = $true; break }
        if ($taskServer.State -eq 'Failed') { throw 'HTTP fixture failed.' }
        Start-Sleep -Milliseconds 100
    }
    Assert-Task $taskReady 'HTTP fixture did not start.'
    $taskOpened = & (Join-Path $taskRepo 'deploy/open-workspace.ps1') -WorkspaceUri "agent-harness-workspace://open/$taskTicket" -WebOrigin $taskOrigin -ValidateOnly
    Assert-Task ($taskOpened -eq 'D:\project\中文') 'UTF-8 HTTP workspace path was corrupted.'
} finally {
    Stop-Job $taskServer
    Remove-Job $taskServer
    Remove-Item Function:\docker
}
# Compile the exact embedded C# and check the COM interface is available on this
# Windows installation. No Show call is made during automated validation.
$taskSource = Get-Content -LiteralPath $taskPicker -Raw
$taskMatch = [regex]::Match($taskSource, '(?s)Add-Type -TypeDefinition @"\r?\n(.*?)\r?\n"@')
Assert-Task $taskMatch.Success 'Missing native picker implementation.'
Add-Type -TypeDefinition $taskMatch.Groups[1].Value
$taskClass = [HarnessFolderPicker].Assembly.GetType('HarnessFileOpenDialog')
$taskDialog = [Activator]::CreateInstance($taskClass)
$taskUnknown = [System.Runtime.InteropServices.Marshal]::GetIUnknownForObject($taskDialog)
$taskInterface = [IntPtr]::Zero
$taskGuid = [guid]'D57C7288-D4AD-4768-BE02-9D969532D960'
try {
    Assert-Task ([System.Runtime.InteropServices.Marshal]::QueryInterface($taskUnknown, [ref]$taskGuid, [ref]$taskInterface) -eq 0) 'Windows native folder dialog unavailable.'
} finally {
    if ($taskInterface -ne [IntPtr]::Zero) { [System.Runtime.InteropServices.Marshal]::Release($taskInterface) | Out-Null }
    [System.Runtime.InteropServices.Marshal]::Release($taskUnknown) | Out-Null
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($taskDialog) | Out-Null
}
Write-Host 'Native picker URL, callback, mount mapping and Windows COM checks passed.'
