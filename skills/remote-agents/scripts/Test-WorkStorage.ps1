<#
.SYNOPSIS
    Report storage-layout violations on the agent work root. Never deletes.
    Exit 0 = clean, 1 = violations found. See references/work-storage.md.
    Extend -AllowedDirectories/-AllowedFiles for project-specific roots.
#>
param(
    [string]$Root = 'C:\Work',
    [int]$StaleRunHours = 24,
    [string[]]$AllowedDirectories = @(
        'artifacts', 'desktop-helpers', 'runs', 'state', 'Temp', 'workspaces'
    ),
    [string[]]$AllowedFiles = @(
        'bootstrap.log', 'toolchain.json'
    ),
    [string[]]$ForbiddenPaths = @('C:\tmp')
)
$ErrorActionPreference = 'Stop'
$violations = New-Object System.Collections.Generic.List[string]

foreach ($entry in Get-ChildItem -LiteralPath $Root -Force) {
    $allowed = if ($entry.PSIsContainer) { $AllowedDirectories } else { $AllowedFiles }
    if ($allowed -notcontains $entry.Name) { $violations.Add("unknown root entry: $($entry.FullName)") }
}

$runs = Join-Path $Root 'runs'
if (Test-Path -LiteralPath $runs) {
    $cutoff = (Get-Date).AddHours(-$StaleRunHours)
    foreach ($run in Get-ChildItem -LiteralPath $runs -Force) {
        if ($run.LastWriteTime -lt $cutoff) { $violations.Add("stale run (> $StaleRunHours h): $($run.FullName)") }
    }
}

foreach ($path in $ForbiddenPaths) {
    if (Test-Path -LiteralPath $path) { $violations.Add("forbidden directory: $path") }
}

# A task whose executable or script argument no longer exists is an orphan.
foreach ($task in Get-ScheduledTask | Where-Object { $_.TaskPath -notlike '\Microsoft\*' }) {
    foreach ($action in @($task.Actions)) {
        $targets = @()
        if ($action.Execute) { $targets += [Environment]::ExpandEnvironmentVariables($action.Execute.Trim('"')) }
        if ($action.Arguments) {
            $targets += [regex]::Matches($action.Arguments, '[A-Za-z]:\\[^"]+?\.(?:ps1|py|cmd|bat|exe)') | ForEach-Object Value
        }
        foreach ($target in $targets) {
            if ([IO.Path]::IsPathRooted($target) -and !(Test-Path -LiteralPath $target)) {
                $violations.Add("orphan scheduled task: $($task.TaskPath)$($task.TaskName) -> $target")
            }
        }
    }
}

if ($violations.Count) {
    $violations | ForEach-Object { Write-Output $_ }
    exit 1
}
Write-Output 'storage audit: clean'
