param([string]$Work = (Join-Path $env:TEMP 'remote-agents-bootstrap'))
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$release = Invoke-RestMethod -Headers @{ 'User-Agent' = 'remote-agents-bootstrap' } -Uri 'https://api.github.com/repos/ip7z/7zip/releases/latest'
$asset = $release.assets | Where-Object { $_.name -match '^7z[0-9]+-x64\.exe$' } | Select-Object -First 1
if (-not $asset -or $asset.digest -notmatch '^sha256:(.+)$') { throw 'Could not resolve a checksummed 7-Zip x64 installer.' }
$expected = $Matches[1].ToLowerInvariant()
$installer = Join-Path $Work $asset.name
Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $installer
$actual = (Get-FileHash -Algorithm SHA256 -Path $installer).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw '7-Zip installer SHA-256 mismatch.' }
$process = Start-Process -FilePath $installer -ArgumentList '/S' -Wait -PassThru
if ($process.ExitCode -ne 0) { throw "7-Zip installer failed with $($process.ExitCode)." }
& 'C:\Program Files\7-Zip\7z.exe' | Select-Object -First 2
