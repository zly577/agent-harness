' GUI-subsystem launcher: no console window is created when opening a folder.
Option Explicit
Dim taskUri, taskPattern, taskFiles, taskHandler, taskShell, taskCommand
If WScript.Arguments.Count <> 1 Then WScript.Quit 1
taskUri = WScript.Arguments(0)
Set taskPattern = New RegExp
taskPattern.Pattern = "^agent-harness-workspace://open/[0-9a-f]{32}/?$"
If Not taskPattern.Test(taskUri) Then WScript.Quit 1
Set taskFiles = CreateObject("Scripting.FileSystemObject")
taskHandler = taskFiles.BuildPath(taskFiles.GetParentFolderName(WScript.ScriptFullName), "open-workspace.ps1")
If Not taskFiles.FileExists(taskHandler) Then WScript.Quit 1
Set taskShell = CreateObject("WScript.Shell")
taskCommand = "powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & taskHandler & """ -WorkspaceUri """ & taskUri & """"
taskShell.Run taskCommand, 0, False
