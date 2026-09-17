<#
.SYNOPSIS
    Remove a Visual Studio instance that was registered under the broken
    'C:\Program' path (missing space) so a correct reinstall can proceed.
    Recovery helper for bootstrap failures; see references/troubleshooting.md.
#>
$ErrorActionPreference = 'Stop'

$targets = Get-Process setup, vs_community -ErrorAction SilentlyContinue
if ($targets) {
    $targets | Stop-Process -Force
    Start-Sleep -Seconds 3
}

$installer = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\setup.exe'
if (-not (Test-Path $installer)) {
    throw 'Visual Studio Installer is missing.'
}

$uninstallArguments = @(
    'uninstall',
    '--installPath', '"C:\Program"',
    '--quiet', '--norestart', '--force'
)
$uninstall = Start-Process -FilePath $installer -ArgumentList $uninstallArguments -Wait -PassThru
if ($uninstall.ExitCode -notin @(0, 3010)) {
    throw "Visual Studio uninstall failed with exit code $($uninstall.ExitCode)."
}

$vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$remaining = if (Test-Path $vswhere) { & $vswhere -all -property installationPath } else { @() }
if ($remaining -contains 'C:\Program') {
    throw 'The incorrect C:\Program Visual Studio instance is still registered.'
}

Write-Host 'Incorrect Visual Studio instance removed.'
