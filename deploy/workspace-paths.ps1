function ConvertTo-ContainerDirectory {
    param([string]$SelectedPath, [object[]]$Mounts)
    $taskSelected = [System.IO.Path]::GetFullPath($SelectedPath).TrimEnd('\', '/')
    foreach ($taskMount in ($Mounts | Sort-Object { $_.Source.Length } -Descending)) {
        if ($taskMount.Type -ne 'bind' -or $taskMount.Source -notmatch '^[A-Za-z]:[/\\]') { continue }
        $taskSource = [System.IO.Path]::GetFullPath($taskMount.Source).TrimEnd('\', '/')
        if ($taskSelected.Equals($taskSource, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $taskMount.Destination
        }
        if ($taskSelected.StartsWith($taskSource + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $taskMount.Destination.TrimEnd('/') + $taskSelected.Substring($taskSource.Length).Replace('\', '/')
        }
    }
    throw 'Selected folder is not mounted in the backend container.'
}

function ConvertTo-WindowsDirectory {
    param([string]$ContainerPath, [object[]]$Mounts)
    if ($ContainerPath -notmatch '^/' -or $ContainerPath -match '\\|(^|/)\.\.(/|$)' -or $ContainerPath.Contains('"')) {
        throw 'Invalid container workspace path.'
    }
    $taskContainer = $ContainerPath.TrimEnd('/')
    foreach ($taskMount in ($Mounts | Sort-Object { $_.Destination.Length } -Descending)) {
        if ($taskMount.Type -ne 'bind' -or $taskMount.Source -notmatch '^[A-Za-z]:[/\\]') { continue }
        $taskDestination = $taskMount.Destination.TrimEnd('/')
        if ($taskContainer -ne $taskDestination -and -not $taskContainer.StartsWith($taskDestination + '/', [System.StringComparison]::Ordinal)) { continue }
        $taskSource = [System.IO.Path]::GetFullPath($taskMount.Source).TrimEnd('\', '/')
        $taskSuffix = $taskContainer.Substring($taskDestination.Length).Replace('/', '\')
        $taskPath = [System.IO.Path]::GetFullPath($taskSource + $taskSuffix + $(if (-not $taskSuffix -and $taskSource -match '^[A-Za-z]:$') { '\' } else { '' }))
        if ($taskPath.TrimEnd('\') -ne $taskSource -and -not $taskPath.StartsWith($taskSource + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            throw 'Workspace path outside the mounted folder.'
        }
        return $taskPath
    }
    throw 'Workspace folder is not mounted from Windows.'
}
