<#
.SYNOPSIS
    Launch a GUI process in the unlocked interactive Windows session.
    Task Scheduler owns the application, not the SSH/agent terminal's job
    object, so ending the terminal or a QA worker never kills the app.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Executable,
    [string]$WorkingDirectory,
    [string]$Arguments,
    [string]$InteractiveUser = 'Administrator',
    [string]$TaskPrefix = 'RemoteAgents-Interactive-'
)
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $Executable -PathType Leaf)) { throw "Missing executable: $Executable" }
$executable = (Resolve-Path -LiteralPath $Executable).Path
if (!$WorkingDirectory) { $WorkingDirectory = Split-Path -Parent $executable }
if (!(Test-Path -LiteralPath $WorkingDirectory -PathType Container)) { throw "Missing working directory: $WorkingDirectory" }
$directory = (Resolve-Path -LiteralPath $WorkingDirectory).Path
if ($Arguments -and $Arguments.Contains('"')) { throw 'Arguments cannot contain quotes' }

# Match the actual path, not a historical product/process name.
$processName = [IO.Path]::GetFileNameWithoutExtension($executable)
$existing = @(Get-Process -Name $processName -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -eq $executable })
if ($existing.Count) {
    throw "Executable is already running (PID $($existing.Id -join ',')). Inspect/reuse it; do not launch duplicates."
}

$taskName = $TaskPrefix + [Guid]::NewGuid().ToString('N')
$actionParameters = @{ Execute = $executable; WorkingDirectory = $directory }
if ($Arguments) { $actionParameters.Argument = $Arguments }
$action = New-ScheduledTaskAction @actionParameters
$principal = New-ScheduledTaskPrincipal -UserId $InteractiveUser -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
try {
    Start-ScheduledTask -TaskName $taskName
    $process = $null
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $process = Get-Process -Name $processName -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $executable -and $_.SessionId -ne 0 } | Select-Object -First 1
        if ($process) { break }
        Start-Sleep -Milliseconds 500
    }
    if (!$process) {
        $result = (Get-ScheduledTaskInfo -TaskName $taskName).LastTaskResult
        throw "No interactive process observed; task result=$result. Verify the unlocked $InteractiveUser desktop."
    }
    $startedAt = $process.StartTime.ToUniversalTime().ToString('o')
} catch {
    throw "Interactive launch failed (task '$taskName'): $($_.Exception.Message)"
} finally {
    # Unregistering the task leaves its running application alive (verified on
    # the reference deployment). Report cleanup failures rather than leaking.
    try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop }
    catch { throw "Could not remove launch task '$taskName'; inspect it and any launched process: $($_.Exception.Message)" }
}
$process.Refresh()
if ($process.HasExited) { throw "Application exited after launch (PID $($process.Id), exit code $($process.ExitCode)); task removed." }
@{
    status = 'process_started'; task_name = $taskName; task_removed = $true; pid = $process.Id
    process_started_at = $startedAt
    session_id = $process.SessionId; executable = $executable
    executable_modified_at = (Get-Item -LiteralPath $executable).LastWriteTimeUtc.ToString('o')
    working_directory = $directory
    requires = 'Verify this PID and start time after caller exit, then inspect its window; process_started is not UI readiness.'
} | ConvertTo-Json -Compress
