<#
.SYNOPSIS
    Report the effective hardening state of a Windows agent host. Read-only.
    Companion to apply-hardening.ps1; see references/provisioning.md.
#>
param(
    [string]$RdpRuleName = 'RemoteAgents RDP via SSM only',
    [string]$OutFile
)
$ErrorActionPreference = 'Stop'

$sshdConfig = 'C:\ProgramData\ssh\sshd_config'
$authorizedKeys = 'C:\ProgramData\ssh\administrators_authorized_keys'
$rdpDenied = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections).fDenyTSConnections
$rdpTcp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\WinStations\RDP-Tcp'
$rdpStandardRules = @(Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue | Where-Object Enabled -eq True)
$rdpSsmRule = Get-NetFirewallRule -DisplayName $RdpRuleName -ErrorAction SilentlyContinue
$rdpSsmAddress = @($rdpSsmRule | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue).RemoteAddress
$sshConfigLines = @(Get-Content $sshdConfig | Where-Object { $_ -match '^\s*(PubkeyAuthentication|PasswordAuthentication|PermitEmptyPasswords)\s+' } | ForEach-Object { [string]$_ })
$sshAcl = (Get-Acl $authorizedKeys).Access | Select-Object IdentityReference, FileSystemRights, AccessControlType, IsInherited
$localUsers = Get-LocalUser | Select-Object Name, Enabled, PasswordRequired, PasswordExpires, LastLogon
$services = Get-Service sshd, WinRM, RemoteRegistry, TermService, LanmanServer | Select-Object Name, Status, StartType
$firewall = Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction
$defender = Get-MpComputerStatus | Select-Object AntivirusEnabled, AntispywareEnabled, RealTimeProtectionEnabled, BehaviorMonitorEnabled, IoavProtectionEnabled, NISEnabled, AntivirusSignatureLastUpdated
try {
    $smb = Get-SmbServerConfiguration | Select-Object EnableSMB1Protocol, EnableSMB2Protocol, EncryptData, RejectUnencryptedAccess, RequireSecuritySignature
} catch {
    $smb = [ordered]@{
        QueryFailed = $true
        Error = $_.Exception.Message
        LanmanServerStatus = (Get-Service LanmanServer).Status.ToString()
        LanmanServerStartType = (Get-Service LanmanServer).StartType.ToString()
    }
}
$netbios = Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=True' | Select-Object Description, TcpipNetbiosOptions
$smbListeners = @(Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 139, 445 | Select-Object LocalAddress, LocalPort, OwningProcess)
$passwordPolicy = net accounts
$auditPolicy = auditpol.exe /get /category:*

$report = [ordered]@{
    GeneratedAt = (Get-Date).ToString('o')
    RdpSsmOnly = [ordered]@{
        Enabled = ($rdpDenied -eq 0)
        NlaRequired = ($rdpTcp.UserAuthentication -eq 1)
        TlsSecurityLayer = ($rdpTcp.SecurityLayer -eq 2)
        HighEncryption = ($rdpTcp.MinEncryptionLevel -eq 3)
        StandardFirewallRulesEnabled = $rdpStandardRules.Count
        LoopbackRuleEnabled = ($null -ne $rdpSsmRule -and $rdpSsmRule.Enabled)
        AllowedRemoteAddresses = $rdpSsmAddress
    }
    Firewall = $firewall
    Defender = $defender
    Services = $services
    SshConfig = $sshConfigLines
    AuthorizedKeysAcl = $sshAcl
    LocalUsers = $localUsers
    Smb = $smb
    NetBios = $netbios
    SmbListeners = $smbListeners
    PasswordPolicy = $passwordPolicy
    AuditPolicy = $auditPolicy
}

$json = $report | ConvertTo-Json -Depth 6
if ($OutFile) { $json | Set-Content -Encoding UTF8 $OutFile }
$json
