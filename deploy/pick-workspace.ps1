# Invoked by the registered Windows URL protocol in a hidden STA process.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WorkspaceUri,
    [string]$WebOrigin = 'http://127.0.0.1:8088',
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
if ($WorkspaceUri -notmatch '^agent-harness-workspace://pick/([0-9a-f]{64})/?$') {
    throw 'Invalid directory picker URL.'
}
$taskTicket = $Matches[1]
if ($WebOrigin -notmatch '^http://(?:127\.0\.0\.1|localhost):[0-9]{1,5}$' -or ([uri]$WebOrigin).Port -le 0) {
    throw 'Directory picker callback must use a local HTTP origin.'
}

. (Join-Path $PSScriptRoot 'workspace-paths.ps1')

if ($ValidateOnly) { Write-Output $taskTicket; return }
$taskCallback = "$WebOrigin/api/v1/workspaces/picker/$taskTicket"
# Check the short-lived ticket before showing any window.
Invoke-RestMethod -Uri "$taskCallback/ready" -TimeoutSec 10 | Out-Null
$taskResult = @{ cancelled = $true }
try {
    # IFileOpenDialog uses the modern Windows Explorer folder picker, including
    # the address bar, navigation pane and New folder command.
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
internal class HarnessFileOpenDialog { }
[ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface HarnessShellItem {
    void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
    void GetParent(out HarnessShellItem parent);
    void GetDisplayName(uint sigdn, out IntPtr name);
    void GetAttributes(uint mask, out uint attributes);
    void Compare(HarnessShellItem other, uint hint, out int order);
}
[ComImport, Guid("D57C7288-D4AD-4768-BE02-9D969532D960"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
internal interface HarnessOpenDialog {
    [PreserveSig] int Show(IntPtr owner);
    void SetFileTypes(uint count, IntPtr filters);
    void SetFileTypeIndex(uint index);
    void GetFileTypeIndex(out uint index);
    void Advise(IntPtr sink, out uint cookie);
    void Unadvise(uint cookie);
    void SetOptions(uint options);
    void GetOptions(out uint options);
    void SetDefaultFolder(HarnessShellItem folder);
    void SetFolder(HarnessShellItem folder);
    void GetFolder(out HarnessShellItem folder);
    void GetCurrentSelection(out HarnessShellItem item);
    void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string name);
    void GetFileName(out IntPtr name);
    void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
    void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
    void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string label);
    void GetResult(out HarnessShellItem item);
    void AddPlace(HarnessShellItem item, uint placement);
    void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string extension);
    void Close(int result);
    void SetClientGuid(ref Guid guid);
    void ClearClientData();
    void SetFilter(IntPtr filter);
    void GetResults(out IntPtr items);
    void GetSelectedItems(out IntPtr items);
}
public static class HarnessFolderPicker {
    public static string Pick() {
        var dialog = (HarnessOpenDialog)new HarnessFileOpenDialog();
        HarnessShellItem item = null;
        IntPtr name = IntPtr.Zero;
        try {
            dialog.SetOptions(0x20 | 0x40 | 0x800 | 0x02000000);
            dialog.SetTitle("选择工作区目录");
            dialog.SetOkButtonLabel("选择文件夹");
            int result = dialog.Show(IntPtr.Zero);
            if (result == unchecked((int)0x800704C7)) return null;
            Marshal.ThrowExceptionForHR(result);
            dialog.GetResult(out item);
            item.GetDisplayName(0x80058000, out name);
            return Marshal.PtrToStringUni(name);
        } finally {
            if (name != IntPtr.Zero) Marshal.FreeCoTaskMem(name);
            if (item != null) Marshal.ReleaseComObject(item);
            Marshal.ReleaseComObject(dialog);
        }
    }
}
"@
    $taskSelected = [HarnessFolderPicker]::Pick()
    if ($taskSelected) {
        $taskInspect = & docker inspect agent-harness-backend-1 --format '{{json .Mounts}}' 2>$null
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect backend directory mounts.' }
        $taskMounts = @((($taskInspect -join '') | ConvertFrom-Json))
        $taskDirectory = ConvertTo-ContainerDirectory -SelectedPath $taskSelected -Mounts $taskMounts
        $taskResult = @{ directory = $taskDirectory }
    }
} catch {
    $taskResult = @{ error = '无法选择该目录。请确认 Docker 正在运行，并且所选目录已挂载到后端。' }
}
$taskBody = [System.Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $taskResult -Compress))
Invoke-RestMethod -Uri "$taskCallback/result" -Method Post -ContentType 'application/json; charset=utf-8' -Body $taskBody -TimeoutSec 10 | Out-Null
