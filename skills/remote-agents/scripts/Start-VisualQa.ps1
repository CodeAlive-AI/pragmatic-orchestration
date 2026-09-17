<#
.SYNOPSIS
    Dispatch a bounded visual-QA worker into the interactive Windows session.
    See references/visual-qa.md for the full contract.
#>
param(
    [Parameter(Mandatory=$true)][string]$PromptFile,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$WorkingDirectory = (Join-Path $env:SystemDrive 'Work/workspaces/default'),
    [string]$Python,
    [int]$TimeoutSeconds = 1800,
    [int]$MaxTurns = 120,
    [string]$ResumeSession,
    [string]$InteractiveUser = 'Administrator',
    [string]$TaskPrefix = 'RemoteAgents-VisualQa-'
)
$ErrorActionPreference = 'Stop'
if (!$Python) {
    $Python = (Get-Command python.exe -ErrorAction Stop).Source
}
$runner = Join-Path $PSScriptRoot 'run-visual-qa.py'
foreach ($path in @($PromptFile, $runner, $Python, $WorkingDirectory)) {
    if (!(Test-Path -LiteralPath $path)) { throw "Missing path: $path" }
    if ($path.Contains('"')) { throw 'Paths cannot contain quotes' }
}
if ($OutputDirectory.Contains('"')) { throw 'Output path cannot contain quotes' }
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Output directory already exists; choose a fresh run directory' }
if ($TimeoutSeconds -lt 30 -or $TimeoutSeconds -gt 3600) { throw 'Timeout must be between 30 and 3600 seconds' }
if ($MaxTurns -lt 1 -or $MaxTurns -gt 500) { throw 'MaxTurns must be between 1 and 500' }
$taskName = $TaskPrefix + [Guid]::NewGuid().ToString('N')
$arguments = '"{0}" --prompt-file "{1}" --output-dir "{2}" --cwd "{3}" --timeout {4} --max-turns {5}' -f $runner,$PromptFile,$OutputDirectory,$WorkingDirectory,$TimeoutSeconds,$MaxTurns
if ($ResumeSession) {
    $parsedSession = [Guid]::Empty
    if (![Guid]::TryParse($ResumeSession, [ref]$parsedSession)) { throw 'ResumeSession must be a session UUID' }
    $arguments += ' --resume-session ' + $parsedSession.ToString()
}
$action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $WorkingDirectory
$principal = New-ScheduledTaskPrincipal -UserId $InteractiveUser -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Seconds ($TimeoutSeconds + 60))
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
@{task_name=$taskName;output_directory=$OutputDirectory;status='dispatched';max_turns=$MaxTurns;timeout_seconds=$TimeoutSeconds;requires="active unlocked $InteractiveUser desktop"} | ConvertTo-Json -Compress
