#Requires -RunAsAdministrator
<#
.SYNOPSIS
One-time host-side setup of the WireGuard/SMB work bridge (run ON the Windows
agent host, elevated). Creates the tunnel service, a dedicated non-admin SMB
account, the encrypted work-root share, and the two firewall rules. Prints the
host's WireGuard public key and the smb.json payload for the dev machine.
#>
param(
    # Dev machine's WireGuard public key (onboard.py init-keys prints it)
    [Parameter(Mandatory = $true)][string]$PeerPublicKey,
    [string]$TunnelName = 'remote-agents-bridge',
    [int]$ListenPort = 51820,
    [string]$HostTunnelIp = '10.99.0.1',
    [string]$DevTunnelIp = '10.99.0.2',
    [string]$WorkRoot = 'C:\Work',
    [string]$ShareName = 'Work',
    [string]$AccountName = 'remote-agents-work'
)
$ErrorActionPreference = 'Stop'

$wgDir = "$env:ProgramFiles\WireGuard"
$wg = Join-Path $wgDir 'wg.exe'
$wireguard = Join-Path $wgDir 'wireguard.exe'
if (-not (Test-Path $wg)) { throw "WireGuard for Windows not installed ($wg missing)" }

# --- WireGuard host config -------------------------------------------------
$confDir = "$env:ProgramData\RemoteAgents-WorkBridge"
New-Item -ItemType Directory -Force -Path $confDir | Out-Null
$acl = Get-Acl $confDir
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    'Administrators', 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    'SYSTEM', 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
Set-Acl $confDir $acl

$keyFile = Join-Path $confDir 'host.private'
if (-not (Test-Path $keyFile)) {
    & $wg genkey | Out-File -Encoding ascii -NoNewline $keyFile
}
$hostPrivate = (Get-Content $keyFile -Raw).Trim()
$hostPublic = ($hostPrivate | & $wg pubkey).Trim()

$conf = @"
[Interface]
PrivateKey = $hostPrivate
Address = $HostTunnelIp/32
ListenPort = $ListenPort

[Peer]
PublicKey = $PeerPublicKey
AllowedIPs = $DevTunnelIp/32
"@
$confPath = Join-Path $confDir "$TunnelName.conf"
$conf | Out-File -Encoding ascii $confPath

& $wireguard /uninstalltunnelservice $TunnelName 2>$null
& $wireguard /installtunnelservice $confPath
if ($LASTEXITCODE) { throw "wireguard /installtunnelservice failed ($LASTEXITCODE)" }

# --- Dedicated SMB account --------------------------------------------------
$existing = Get-LocalUser -Name $AccountName -ErrorAction SilentlyContinue
if ($existing) {
    $password = $null
    Write-Host "Account $AccountName already exists; reusing it (password NOT re-shown)." -ForegroundColor Yellow
} else {
    $bytes = New-Object byte[] 18
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes)
    $secure = ConvertTo-SecureString $password -AsPlainText -Force
    New-LocalUser -Name $AccountName -Password $secure -PasswordNeverExpires `
        -AccountNeverExpires -Description 'remote-agents bridge SMB account' | Out-Null
}
# Never an admin; never in RDP.
$adminGroup = (Get-LocalGroup -SID 'S-1-5-32-544').Name
if (Get-LocalGroupMember -Group $adminGroup -Member $AccountName -ErrorAction SilentlyContinue) {
    Remove-LocalGroupMember -Group $adminGroup -Member $AccountName
}

# NTFS Modify on the work root (inheritance on).
& icacls $WorkRoot /grant "${AccountName}:(OI)(CI)M" /T | Out-Null

# Share: encrypted, Change-level.
$share = Get-SmbShare -Name $ShareName -ErrorAction SilentlyContinue
if (-not $share) {
    New-SmbShare -Name $ShareName -Path $WorkRoot -ChangeAccess $AccountName | Out-Null
}
Set-SmbShare -Name $ShareName -EncryptData $true -Force
Revoke-SmbShareAccess -Name $ShareName -AccountName 'Everyone' -Force -ErrorAction SilentlyContinue

# --- Firewall ---------------------------------------------------------------
foreach ($name in 'RemoteAgents-WorkBridge-WireGuard', 'RemoteAgents-WorkBridge-SMB') {
    Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
}
New-NetFirewallRule -DisplayName 'RemoteAgents-WorkBridge-WireGuard' -Direction Inbound `
    -Action Allow -Protocol UDP -LocalPort $ListenPort -Profile Any | Out-Null
New-NetFirewallRule -DisplayName 'RemoteAgents-WorkBridge-SMB' -Direction Inbound `
    -Action Allow -Protocol TCP -LocalPort 445 `
    -LocalAddress $HostTunnelIp -RemoteAddress $DevTunnelIp -Profile Any | Out-Null
# Broad built-in SMB-In rules stay disabled so only the tunnel-scoped rule applies.
Get-NetFirewallRule -DisplayGroup 'File and Printer Sharing' |
    Where-Object { $_.Name -match 'SMB-In' -and $_.Direction -eq 'Inbound' } |
    Disable-NetFirewallRule

# --- Output -----------------------------------------------------------------
$result = [ordered]@{
    hostPublicKey = $hostPublic
    next          = @(
        "dev: python3 scripts/onboard.py set-peer $hostPublic"
    )
    smbJson       = $null
}
if ($password) {
    $result.smbJson = "{`"username`": `"$AccountName`", `"password`": `"$password`"}"
    $result.next += 'dev: save smbJson as .bridge-state/smb.json (mode 0600)'
} else {
    $result.next += 'dev: reuse the existing .bridge-state/smb.json for this account'
}
$result.next += 'dev: python3 scripts/work-bridge.py connect'
$result | ConvertTo-Json -Depth 4
