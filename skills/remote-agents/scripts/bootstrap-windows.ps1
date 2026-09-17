<#
.SYNOPSIS
    Provision a Windows agent host toolchain: Visual Studio workloads, Git,
    Python, PowerShell 7, 7-Zip, and work-root environment variables.
    Every installer is signature- or checksum-verified. See
    references/provisioning.md for the surrounding host setup.
.PARAMETER WorkRoot
    Closed work root (default C:\Work); see references/work-storage.md.
.PARAMETER VsInstallPath
    Expected Visual Studio install path; verified after install.
.PARAMETER VsWorkloads / VsComponents
    VS setup --add arguments; override for the host's actual workload.
.PARAMETER SkipVisualStudio
    Skip the VS install/verify phase (existing or VS-less hosts).
#>
[CmdletBinding()]
param(
    [string]$WorkRoot = 'C:\Work',
    [switch]$SkipVisualStudio,
    [string]$VsInstallPath = 'C:\Program Files\Microsoft Visual Studio\18\Community',
    [string[]]$VsWorkloads = @(
        'Microsoft.VisualStudio.Workload.NativeDesktop',
        'Microsoft.VisualStudio.Workload.ManagedDesktop'
    ),
    [string[]]$VsComponents = @(
        'Microsoft.VisualStudio.Component.VC.CLI.Support',
        'Microsoft.Net.Component.4.8.1.TargetingPack',
        'Microsoft.Net.Component.4.8.1.SDK'
    )
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$work = Join-Path $WorkRoot 'bootstrap'
$log = Join-Path $WorkRoot 'bootstrap.log'
New-Item -ItemType Directory -Force -Path $work | Out-Null
Start-Transcript -Path $log -Append

function Get-SignedFile {
    param(
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][string]$OutFile,
        [Parameter(Mandatory)][string]$SignerPattern
    )

    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $OutFile
    $signature = Get-AuthenticodeSignature -FilePath $OutFile
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch $SignerPattern) {
        throw "Invalid signature for ${OutFile}: status=$($signature.Status), signer=$($signature.SignerCertificate.Subject)"
    }
}

function Invoke-Installer {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [int[]]$AllowedExitCodes = @(0, 3010)
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -Wait -PassThru
    if ($process.ExitCode -notin $AllowedExitCodes) {
        throw "Installer $FilePath exited with $($process.ExitCode)"
    }
}

if (-not $SkipVisualStudio) {
    Write-Host 'Installing Visual Studio Community and build workloads...'
    $vsBootstrapper = Join-Path $work 'vs_community.exe'
    Get-SignedFile -Uri 'https://aka.ms/vs/stable/vs_community.exe' -OutFile $vsBootstrapper -SignerPattern 'Microsoft Corporation'
    $vsArguments = @(
        '--quiet', '--wait', '--norestart', '--nocache',
        '--installPath', ('"{0}"' -f $VsInstallPath),
        '--includeRecommended'
    )
    foreach ($component in $VsWorkloads + $VsComponents) { $vsArguments += @('--add', $component) }
    Invoke-Installer -FilePath $vsBootstrapper -ArgumentList $vsArguments
}

Write-Host 'Installing Git for Windows from its latest signed GitHub release...'
$gitRelease = Invoke-RestMethod -Headers @{ 'User-Agent' = 'remote-agents-bootstrap' } -Uri 'https://api.github.com/repos/git-for-windows/git/releases/latest'
$gitAsset = $gitRelease.assets | Where-Object { $_.name -match '^Git-[0-9].*-64-bit\.exe$' } | Select-Object -First 1
if (-not $gitAsset) { throw 'Could not resolve the latest Git for Windows x64 installer.' }
$gitInstaller = Join-Path $work $gitAsset.name
Invoke-WebRequest -UseBasicParsing -Uri $gitAsset.browser_download_url -OutFile $gitInstaller
if ($gitAsset.digest -match '^sha256:(.+)$') {
    $gitExpected = $Matches[1].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -Path $gitInstaller).Hash.ToLowerInvariant()
    if ($actual -ne $gitExpected) { throw 'Git installer SHA-256 mismatch.' }
}
$gitSignature = Get-AuthenticodeSignature -FilePath $gitInstaller
if ($gitSignature.Status -ne 'Valid') { throw "Invalid Git installer signature: $($gitSignature.Status)" }
Invoke-Installer -FilePath $gitInstaller -ArgumentList @('/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-') -AllowedExitCodes @(0)

Write-Host 'Installing Python 3.13 for repository automation...'
$pythonInstaller = Join-Path $work 'python-3.13.15-amd64.exe'
Get-SignedFile -Uri 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe' -OutFile $pythonInstaller -SignerPattern 'Python Software Foundation'
Invoke-Installer -FilePath $pythonInstaller -ArgumentList @('/quiet', 'InstallAllUsers=1', 'PrependPath=1', 'Include_test=0', 'Include_launcher=1')

Write-Host 'Installing current stable PowerShell from the official GitHub release...'
$pwshRelease = Invoke-RestMethod -Headers @{ 'User-Agent' = 'remote-agents-bootstrap' } -Uri 'https://api.github.com/repos/PowerShell/PowerShell/releases/latest'
$pwshAsset = $pwshRelease.assets | Where-Object { $_.name -match '^PowerShell-[0-9.]+-win-x64\.msi$' } | Select-Object -First 1
if (-not $pwshAsset) { throw 'Could not resolve the latest PowerShell x64 MSI.' }
$pwshInstaller = Join-Path $work $pwshAsset.name
Invoke-WebRequest -UseBasicParsing -Uri $pwshAsset.browser_download_url -OutFile $pwshInstaller
if ($pwshAsset.digest -match '^sha256:(.+)$') {
    $pwshExpected = $Matches[1].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -Path $pwshInstaller).Hash.ToLowerInvariant()
    if ($actual -ne $pwshExpected) { throw 'PowerShell installer SHA-256 mismatch.' }
}
$pwshSignature = Get-AuthenticodeSignature -FilePath $pwshInstaller
if ($pwshSignature.Status -ne 'Valid' -or $pwshSignature.SignerCertificate.Subject -notmatch 'Microsoft Corporation') {
    throw "Invalid PowerShell installer signature: $($pwshSignature.Status)"
}
Invoke-Installer -FilePath 'msiexec.exe' -ArgumentList @('/i', $pwshInstaller, '/qn', '/norestart', 'ADD_EXPLORER_CONTEXT_MENU_OPENPOWERSHELL=0', 'ENABLE_PSREMOTING=0', 'USE_MU=1', 'ENABLE_MU=1')

Write-Host 'Installing 7-Zip (checksummed GitHub release)...'
$sevenZipRelease = Invoke-RestMethod -Headers @{ 'User-Agent' = 'remote-agents-bootstrap' } -Uri 'https://api.github.com/repos/ip7z/7zip/releases/latest'
$sevenZipAsset = $sevenZipRelease.assets | Where-Object { $_.name -match '^7z[0-9]+-x64\.exe$' } | Select-Object -First 1
if (-not $sevenZipAsset -or $sevenZipAsset.digest -notmatch '^sha256:(.+)$') { throw 'Could not resolve a checksummed 7-Zip x64 installer.' }
$sevenZipExpected = $Matches[1].ToLowerInvariant()
$sevenZipInstaller = Join-Path $work $sevenZipAsset.name
Invoke-WebRequest -UseBasicParsing -Uri $sevenZipAsset.browser_download_url -OutFile $sevenZipInstaller
$sevenZipActual = (Get-FileHash -Algorithm SHA256 -Path $sevenZipInstaller).Hash.ToLowerInvariant()
if ($sevenZipActual -ne $sevenZipExpected) { throw '7-Zip installer SHA-256 mismatch.' }
Invoke-Installer -FilePath $sevenZipInstaller -ArgumentList @('/S') -AllowedExitCodes @(0)

$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
if ($machinePath -notlike '*C:\Program Files\Git\cmd*') {
    $machinePath += ';C:\Program Files\Git\cmd'
}
[Environment]::SetEnvironmentVariable('Path', $machinePath, 'Machine')
[Environment]::SetEnvironmentVariable('NUGET_PACKAGES', (Join-Path $WorkRoot '.nuget\packages'), 'Machine')
[Environment]::SetEnvironmentVariable('DOTNET_CLI_HOME', (Join-Path $WorkRoot '.dotnet'), 'Machine')
[Environment]::SetEnvironmentVariable('DOTNET_CLI_TELEMETRY_OPTOUT', '1', 'Machine')
$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')

& 'C:\Program Files\Git\cmd\git.exe' config --system core.longpaths true
& 'C:\Program Files\Git\cmd\git.exe' config --system core.autocrlf false

$summary = [ordered]@{
    Git = (& 'C:\Program Files\Git\cmd\git.exe' --version)
    Python = (& 'C:\Program Files\Python313\python.exe' --version)
    PowerShell = (& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -NoProfile -Command '$PSVersionTable.PSVersion.ToString()')
    SevenZip = (& 'C:\Program Files\7-Zip\7z.exe' | Select-Object -First 2 | Select-Object -Last 1)
}
if (-not $SkipVisualStudio) {
    $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
    $msbuild = Join-Path $VsInstallPath 'MSBuild\Current\Bin\MSBuild.exe'
    if (-not (Test-Path $vswhere)) { throw 'vswhere.exe was not installed.' }
    if (-not (Test-Path $msbuild)) { throw "Expected MSBuild at $msbuild is missing." }
    $installation = & $vswhere -latest -products Microsoft.VisualStudio.Product.Community -requires Microsoft.VisualStudio.Component.VC.CLI.Support -property installationPath
    if ($installation -ne $VsInstallPath) {
        throw "C++/CLI component verification failed; vswhere returned: $installation"
    }
    $summary.VisualStudio = (& $vswhere -latest -products Microsoft.VisualStudio.Product.Community -property catalog_productDisplayVersion)
    $summary.MSBuild = (& $msbuild -version -nologo | Select-Object -Last 1)
    $summary.DotNetSDKs = @(& 'C:\Program Files\dotnet\dotnet.exe' --list-sdks)
}
$toolchainPath = Join-Path $WorkRoot 'toolchain.json'
$summary | ConvertTo-Json -Depth 3 | Set-Content -Encoding UTF8 $toolchainPath
$summary | ConvertTo-Json -Depth 3
Stop-Transcript
