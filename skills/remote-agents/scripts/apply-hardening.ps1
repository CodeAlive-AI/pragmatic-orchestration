<#
.SYNOPSIS
    Baseline hardening for a dedicated Windows agent host whose only network
    entry path is SSH over AWS SSM. Idempotent. See references/provisioning.md.
.PARAMETER RdpRuleName
    Firewall rule name for the loopback-only RDP exception.
.PARAMETER AllowWorkShare
    Keep LanmanServer running and scope SMB to the WireGuard tunnel address
    instead of disabling the SMB server entirely (needed by the work bridge).
.PARAMETER TunnelAddress
    The Windows side of the WireGuard tunnel (e.g. 10.99.0.1).
#>
param(
    [string]$RdpRuleName = 'RemoteAgents RDP via SSM only',
    [switch]$AllowWorkShare,
    [string]$TunnelAddress
)
$ErrorActionPreference = 'Stop'
if ($AllowWorkShare -and !$TunnelAddress) { throw '-AllowWorkShare requires -TunnelAddress' }

# SSM is the only network entry path. RDP is reachable only from the local SSM
# agent through loopback; security-group ingress remains empty.
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections -Type DWord -Value 0
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name UserAuthentication -Type DWord -Value 1
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name SecurityLayer -Type DWord -Value 2
Set-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp' -Name MinEncryptionLevel -Type DWord -Value 3
Set-Service TermService -StartupType Manual
Start-Service TermService
Set-Service WinRM -StartupType Disabled
Stop-Service WinRM -Force -ErrorAction SilentlyContinue
Set-Service RemoteRegistry -StartupType Disabled
Stop-Service RemoteRegistry -Force -ErrorAction SilentlyContinue

Get-NetFirewallProfile | Set-NetFirewallProfile -Enabled True -DefaultInboundAction Block -DefaultOutboundAction Allow -NotifyOnListen False
Disable-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue
Get-NetFirewallRule -DisplayName $RdpRuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RdpRuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 3389 -RemoteAddress 127.0.0.1 -Profile Any | Out-Null
Disable-NetFirewallRule -DisplayGroup 'Windows Remote Management' -ErrorAction SilentlyContinue
Disable-NetFirewallRule -DisplayGroup 'File and Printer Sharing' -ErrorAction SilentlyContinue

Set-SmbServerConfiguration -EnableSMB1Protocol $false -RequireSecuritySignature $true -RejectUnencryptedAccess $true -Force
if ($AllowWorkShare) {
    # The Work share is reachable only over the WireGuard tunnel address.
    Set-Service LanmanServer -StartupType Automatic
    Start-Service LanmanServer
    Get-NetFirewallRule -DisplayName 'RemoteAgents Work share via tunnel only' -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName 'RemoteAgents Work share via tunnel only' -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort 445 -LocalAddress $TunnelAddress -Profile Any | Out-Null
} else {
    Stop-Service LanmanServer -Force -ErrorAction SilentlyContinue
    Set-Service LanmanServer -StartupType Disabled
}
Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=True' |
    ForEach-Object { [void](Invoke-CimMethod -InputObject $_ -MethodName SetTcpipNetbios -Arguments @{ TcpipNetbiosOptions = 2 }) }

# Defender stays active during builds. Cloud-delivered protection and PUA
# blocking improve coverage without excluding source/build paths.
Set-MpPreference -DisableRealtimeMonitoring $false -DisableBehaviorMonitoring $false -DisableIOAVProtection $false -MAPSReporting Advanced -SubmitSamplesConsent SendSafeSamples -PUAProtection Enabled
Update-MpSignature

# Preserve only public-key SSH. Administrator keys use the special ACL-protected file.
$sshdConfig = 'C:\ProgramData\ssh\sshd_config'
$content = Get-Content $sshdConfig
$required = @{
    PubkeyAuthentication = 'yes'
    PasswordAuthentication = 'no'
    PermitEmptyPasswords = 'no'
}
foreach ($setting in $required.GetEnumerator()) {
    $pattern = "^\s*#?\s*$($setting.Key)\s+.*$"
    if ($content -match $pattern) {
        $content = $content -replace $pattern, "$($setting.Key) $($setting.Value)"
    } else {
        $content += "$($setting.Key) $($setting.Value)"
    }
}
$content | Set-Content -Encoding ascii $sshdConfig
& "$env:WINDIR\System32\OpenSSH\sshd.exe" -t
if ($LASTEXITCODE -ne 0) { throw 'sshd_config validation failed.' }
Restart-Service sshd
Set-Service sshd -StartupType Automatic

$keys = 'C:\ProgramData\ssh\administrators_authorized_keys'
$acl = Get-Acl $keys
$acl.SetAccessRuleProtection($true, $false)
$acl.Access | ForEach-Object { [void]$acl.RemoveAccessRule($_) }
$system = New-Object System.Security.AccessControl.FileSystemAccessRule('SYSTEM', 'FullControl', 'Allow')
$admins = New-Object System.Security.AccessControl.FileSystemAccessRule('BUILTIN\Administrators', 'FullControl', 'Allow')
$acl.AddAccessRule($system)
$acl.AddAccessRule($admins)
Set-Acl -Path $keys -AclObject $acl

# Guest stays disabled; the built-in Administrator remains because SSH uses it.
Disable-LocalUser -Name Guest -ErrorAction SilentlyContinue
net.exe accounts /minpwlen:14 /uniquepw:24 /maxpwage:90 | Out-Null

# Record process creation and logon events useful for incident investigation.
auditpol.exe /set /subcategory:'Process Creation' /success:enable /failure:enable | Out-Null
auditpol.exe /set /subcategory:'Logon' /success:enable /failure:enable | Out-Null
auditpol.exe /set /subcategory:'Account Lockout' /success:enable /failure:enable | Out-Null

Write-Host 'Hardening applied. Run audit-hardening.ps1 to verify.'
