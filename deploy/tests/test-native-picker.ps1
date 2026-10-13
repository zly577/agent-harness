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
