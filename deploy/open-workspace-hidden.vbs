' GUI-subsystem launcher: no console window is created when opening a folder.
Option Explicit
Dim taskUri, taskRoot, taskPattern, taskFiles, taskHandler, taskShell, taskCommand, taskOrigin
If WScript.Arguments.Count <> 3 Then WScript.Quit 1
taskRoot = WScript.Arguments(0)
taskOrigin = WScript.Arguments(1)
taskUri = WScript.Arguments(2)
Set taskPattern = New RegExp
taskPattern.Pattern = "^agent-harness-workspace://(open/[0-9a-f]{64}|open/[0-9a-f]{32}(/[0-9a-f]{32})?|pick/[0-9a-f]{64})/?$"
If Not taskPattern.Test(taskUri) Then WScript.Quit 1
Set taskFiles = CreateObject("Scripting.FileSystemObject")
If InStr(taskUri, "://pick/") > 0 Then
    taskHandler = taskFiles.BuildPath(taskFiles.GetParentFolderName(WScript.ScriptFullName), "pick-workspace.ps1")
Else
    taskHandler = taskFiles.BuildPath(taskFiles.GetParentFolderName(WScript.ScriptFullName), "open-workspace.ps1")
End If
If Not taskFiles.FileExists(taskHandler) Then WScript.Quit 1
If Not taskFiles.FolderExists(taskRoot) Then WScript.Quit 1
If InStr(taskRoot, Chr(34)) > 0 Then WScript.Quit 1
taskPattern.Pattern = "^http://(127\.0\.0\.1|localhost):[0-9]{1,5}$"
If Not taskPattern.Test(taskOrigin) Then WScript.Quit 1
Set taskShell = CreateObject("WScript.Shell")
taskCommand = "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & taskHandler & """ -WorkspaceRoot """ & taskRoot & """ -WebOrigin """ & taskOrigin & """ -WorkspaceUri """ & taskUri & """"
If InStr(taskUri, "://pick/") > 0 Then
    taskCommand = "powershell.exe -NoLogo -NoProfile -STA -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & taskHandler & """ -WebOrigin """ & taskOrigin & """ -WorkspaceUri """ & taskUri & """"
End If
taskShell.Run taskCommand, 0, False
