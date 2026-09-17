#!/usr/bin/env python3
"""Onboarding mode: verify a remote agent host end-to-end, phase by phase.

    onboard.py [--host <id>] check            readiness matrix (read-only)
    onboard.py [--host <id>] init-keys        dev-side WireGuard keypair +
                                              .bridge-state/<id>.conf skeleton
    onboard.py [--host <id>] set-peer <key>   fill the host's public key in

Each check maps to a phase in references/onboarding.md and prints
PASS/FAIL/SKIP/WARN plus a remediation hint. `check` never mutates anything.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import config as ra_config  # noqa: E402
import devlocal  # noqa: E402

if '--host' in sys.argv:
    _i = sys.argv.index('--host')
    if _i + 1 < len(sys.argv):
        os.environ['REMOTE_AGENTS_HOST'] = sys.argv[_i + 1]
CONFIG, CONFIG_PATH = ra_config.load_config()
HOST_ID, HOST = ra_config.resolve_host(CONFIG)
OS_KIND = HOST.get('os')
BRIDGE = HOST.get('bridge') or {}
DESKTOP = HOST.get('desktop') or {}
AWS = HOST.get('aws') or {}
STATE = Path(BRIDGE.get('stateDir') or (HERE.parent / '.bridge-state'))
SUBNET = BRIDGE.get('subnetPrefix', '10.99.0')
WIN_IP = f'{SUBNET}.1'
DEV_IP = f'{SUBNET}.2'
LISTEN_PORT = int(BRIDGE.get('listenPort', 51820))
SHARE = BRIDGE.get('shareName', 'Work')
ACCOUNT = BRIDGE.get('accountName', 'remote-agents-work')
LOCAL_PORT = int(DESKTOP.get('localPort', 13389))
ADDRESS = f'127.0.0.1:{LOCAL_PORT}'

PASS, FAIL, SKIP, WARN = 'PASS', 'FAIL', 'SKIP', 'WARN'
results = []


def report(status, name, detail=''):
    results.append((status, name, detail))
    print(f'  {status:4}  {name:22} {detail}')


def which(*names):
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


def run_out(argv, timeout=20, env=None):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)


def aws_cmd(*args):
    cmd = ['aws']
    if AWS.get('profile'):
        cmd += ['--profile', AWS['profile']]
    if AWS.get('region'):
        cmd += ['--region', AWS['region']]
    env = dict(os.environ)
    if AWS.get('configFile'):
        env['AWS_CONFIG_FILE'] = os.path.expanduser(AWS['configFile'])
    return cmd + list(args), env


def ssh(remote_cmd, timeout=60):
    return run_out(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                    HOST['sshAlias'], remote_cmd], timeout=timeout)


# ------------------------------------------------------------- dev checks


def check_dev():
    report(PASS if sys.version_info >= (3, 9) else FAIL, 'dev.python', sys.version.split()[0])

    missing = [n for n in ('aws', 'ssh', 'scp') if not which(n)]
    report(FAIL if missing else PASS, 'dev.tools',
           ('missing: ' + ', '.join(missing)) if missing else 'aws, ssh, scp on PATH')

    if not which('session-manager-plugin'):
        report(FAIL, 'dev.ssm-plugin', 'install AWS session-manager-plugin for SSH-over-SSM')
    else:
        report(PASS, 'dev.ssm-plugin', 'session-manager-plugin found')

    if devlocal.DEV_OS == 'windows' and not which('bash'):
        report(FAIL, 'dev.shell', 'host.sh needs Git Bash on Windows')
    else:
        report(PASS, 'dev.shell', devlocal.DEV_OS)

    cmd, env = aws_cmd('sts', 'get-caller-identity', '--query', 'Account', '--output', 'text')
    rc, out = run_out(cmd, env=env)
    report(PASS if rc == 0 else FAIL, 'dev.aws-creds',
           f'caller identity ok' if rc == 0 else f'aws auth failed: {out[:120]}')

    if devlocal.DEV_OS == 'windows':
        ok = which('wireguard', 'wg')
        report(PASS if ok else FAIL, 'dev.wireguard',
               'wireguard.exe found' if ok else 'install WireGuard for Windows')
    else:
        ok = which('wg', 'wg-quick')
        report(PASS if ok else FAIL, 'dev.wireguard',
               'wg/wg-quick found' if ok else 'install wireguard-tools')

    if OS_KIND == 'windows':
        try:
            binary = devlocal.rdp_client_binary(DESKTOP)
            report(PASS, 'dev.freerdp', binary)
        except RuntimeError as exc:
            report(FAIL, 'dev.freerdp', str(exc))
        _check_dev_credential()
    else:
        report(SKIP, 'dev.freerdp', 'linux host: no desktop plane')
        report(SKIP, 'dev.rdp-credential', 'linux host: no desktop plane')

    if BRIDGE:
        conf = STATE / f'{HOST_ID}.conf'
        if not conf.exists():
            report(FAIL, 'dev.bridge-keys', f'{conf} missing — run onboard.py init-keys')
        else:
            text = conf.read_text()
            pending = 'PENDING' in text
            report(WARN if pending else PASS, 'dev.bridge-keys',
                   'peer key not set — run setup-host-bridge.ps1 on the host, then set-peer'
                   if pending else f'{conf} ready')
        smb = STATE / 'smb.json'
        if smb.exists():
            try:
                cred = json.loads(smb.read_text())
                ok = bool(cred.get('username') and cred.get('password'))
                report(PASS if ok else FAIL, 'dev.smb-cred', 'smb.json present' if ok else 'smb.json lacks username/password')
            except json.JSONDecodeError:
                report(FAIL, 'dev.smb-cred', 'smb.json is not valid JSON')
        else:
            report(FAIL, 'dev.smb-cred',
                   'smb.json missing — save the smbJson printed by setup-host-bridge.ps1')
    else:
        report(SKIP, 'dev.bridge-keys', 'no bridge block configured')


def _check_dev_credential():
    if DESKTOP.get('passwordCommand') or DESKTOP.get('passwordEnv'):
        report(PASS, 'dev.rdp-credential', 'passwordCommand/passwordEnv configured')
        return
    if devlocal.DEV_OS == 'macos':
        # Presence check only: bookmark row, no Keychain decrypt.
        try:
            import sqlite3
            db = (Path.home() / 'Library/Containers/com.microsoft.rdc.macos/Data/Library/'
                  'Application Support/com.microsoft.rdc.macos/com.microsoft.rdc.application-data.sqlite')
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
                rows = conn.execute(
                    'select 1 from ZBOOKMARKENTITY where ZFRIENDLYNAME=? and ZHOSTNAME=?',
                    (DESKTOP.get('rdpBookmark'), ADDRESS)).fetchall()
            report(PASS if rows else FAIL, 'dev.rdp-credential',
                   f"bookmark '{DESKTOP.get('rdpBookmark')}' at {ADDRESS}" if rows else
                   'bookmark not found — connect once via Windows App and save the password')
        except Exception as exc:
            report(WARN, 'dev.rdp-credential', f'could not inspect bookmark db: {exc}')
        return
    if devlocal.DEV_OS == 'windows':
        rc, out = run_out(['cmdkey', '/list'])
        hit = f'TERMSRV/{ADDRESS}' in out or f'TERMSRV/{ADDRESS.split(":")[0]}' in out
        report(PASS if hit else FAIL, 'dev.rdp-credential',
               f'TERMSRV credential for {ADDRESS} saved' if hit else
               f'no TERMSRV/{ADDRESS} credential — connect once via mstsc with "remember me"')
        return
    report(FAIL, 'dev.rdp-credential',
           'set desktop.passwordCommand (e.g. "secret-tool lookup …" / "pass show …")')


# ------------------------------------------------------ reachability checks


def check_reachability():
    instance = AWS.get('instanceId')
    if instance:
        cmd, env = aws_cmd('ec2', 'describe-instances', '--instance-ids', instance,
                           '--query', 'Reservations[0].Instances[0].State.Name',
                           '--output', 'text')
        rc, out = run_out(cmd, env=env)
        state = out.splitlines()[-1] if rc == 0 else out[:120]
        report(PASS if rc == 0 and state == 'running' else (WARN if rc == 0 else FAIL),
               'host.instance', state)
        cmd, env = aws_cmd('ssm', 'get-connection-status', '--target', instance,
                           '--query', 'Status', '--output', 'text')
        rc, out = run_out(cmd, env=env)
        ssm = out.splitlines()[-1] if rc == 0 else out[:120]
        report(PASS if rc == 0 and ssm == 'connected' else FAIL, 'host.ssm', ssm)
    else:
        report(SKIP, 'host.instance', 'no aws.instanceId configured')
        report(SKIP, 'host.ssm', 'no aws.instanceId configured')

    rc, out = ssh('echo SSH_OK')
    report(PASS if rc == 0 and 'SSH_OK' in out else FAIL, 'host.ssh',
           'SSH-over-SSM answers' if rc == 0 else f'ssh failed: {out[:120]}')
    return rc == 0


# ----------------------------------------------------------- host checks


WINDOWS_PROBE = r"""
$ErrorActionPreference='SilentlyContinue'
$r=[ordered]@{}
$r.workRoot=(Test-Path 'WORKROOT')
$r.layout=@{}
foreach($d in 'workspaces','runs','artifacts','state'){ $r.layout[$d]=(Test-Path "WORKROOT/$d") }
$r.sshd=(Get-Service sshd).Status.ToString()
$shell=(Get-ItemProperty 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -ErrorAction SilentlyContinue).DefaultShell
$r.pwshShell=($shell -and $shell -match 'pwsh')
$r.wgTunnel=@(Get-Service 'WireGuardTunnel*').Name -join ','
$share=Get-SmbShare -Name 'SHARENAME'
$r.share=($null -ne $share); $r.shareEncrypted=($share -and $share.EncryptData)
$r.fwWg=($null -ne (Get-NetFirewallRule -DisplayName 'RemoteAgents-WorkBridge-WireGuard'))
$r.fwSmb=($null -ne (Get-NetFirewallRule -DisplayName 'RemoteAgents-WorkBridge-SMB'))
$r.smbUser=($null -ne (Get-LocalUser -Name 'ACCOUNTNAME'))
$r.remotePython=(Test-Path 'REMOTEPYTHON')
$r.porch=($null -ne (Get-Command porch -ErrorAction SilentlyContinue))
$r.agents=@('grok','claude','codex','opencode','gemini','devin') | Where-Object { Get-Command "$_.exe","$_" -ErrorAction SilentlyContinue }
$r | ConvertTo-Json -Compress -Depth 3
"""

LINUX_PROBE = r"""
echo "{"
echo "\"workRoot\": $(test -d 'WORKROOT' && echo true || echo false),"
echo "\"porch\": $(command -v porch >/dev/null && echo true || echo false),"
echo "\"tmux\": $(command -v tmux >/dev/null && echo true || echo false),"
echo "\"git\": $(command -v git >/dev/null && echo true || echo false),"
a=''; for c in grok claude codex opencode gemini devin; do command -v $c >/dev/null && a="$a$c "; done
echo "\"agents\": \"$a\"}"
"""


def _interp(name, ok, detail, warn=False):
    report(WARN if warn else (PASS if ok else FAIL), name, detail)


def check_host(ssh_ok):
    if not ssh_ok:
        report(SKIP, 'host.probe', 'SSH unreachable — start the host and re-run check')
        return
    if OS_KIND == 'windows':
        script = (WINDOWS_PROBE
                  .replace('WORKROOT', HOST.get('workRoot', 'C:/Work'))
                  .replace('SHARENAME', SHARE)
                  .replace('ACCOUNTNAME', ACCOUNT)
                  .replace('REMOTEPYTHON', DESKTOP.get('remotePython') or 'NUL'))
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        rc, out = ssh('powershell -NoProfile -NonInteractive -EncodedCommand ' + encoded)
        try:
            r = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            report(FAIL, 'host.probe', f'probe returned non-JSON: {out[:160]}')
            return
        _interp('host.workRoot', r['workRoot'], HOST.get('workRoot', ''))
        missing = [d for d, ok in r['layout'].items() if not ok]
        _interp('host.layout', not missing,
                'workspaces/runs/artifacts/state present' if not missing else 'missing: ' + ', '.join(missing))
        _interp('host.sshd', r['sshd'] == 'Running', f"sshd: {r['sshd']}")
        _interp('host.pwsh-shell', bool(r['pwshShell']),
                'pwsh is the OpenSSH DefaultShell' if r['pwshShell'] else
                'set pwsh 7 as HKLM:\\SOFTWARE\\OpenSSH DefaultShell (windows-host.md)', warn=True)
        if BRIDGE:
            _interp('host.wg-service', bool(r['wgTunnel']),
                    r['wgTunnel'] or 'run setup-host-bridge.ps1 on the host')
            _interp('host.share', r['share'] and r['shareEncrypted'],
                    f"share '{SHARE}' encrypted" if r['shareEncrypted'] else
                    f"share '{SHARE}' missing or unencrypted — setup-host-bridge.ps1")
            _interp('host.firewall', r['fwWg'] and r['fwSmb'],
                    'bridge rules present' if r['fwWg'] and r['fwSmb'] else
                    'RemoteAgents-WorkBridge-* firewall rules missing')
            _interp('host.smb-account', r['smbUser'], f'dedicated account {ACCOUNT}')
        _interp('host.remote-python', r['remotePython'],
                'remotePython present' if r['remotePython'] else
                'install pythonw + Pillow and set desktop.remotePython')
        _interp('host.porch', r['porch'], 'porch on PATH' if r['porch'] else 'install porch (porch-remote.md)')
        _interp('host.agents', bool(r['agents']), ', '.join(r['agents']) or 'no agent CLI found',
                warn=not r['agents'])
    else:
        encoded = base64.b64encode(
            LINUX_PROBE.replace('WORKROOT', HOST.get('workRoot', '/srv/agent-work'))
            .encode()).decode()
        rc, out = ssh('echo ' + encoded + ' | base64 -d | sh')
        try:
            r = json.loads(out)
        except (json.JSONDecodeError, ValueError):
            report(FAIL, 'host.probe', f'probe returned non-JSON: {out[:160]}')
            return
        _interp('host.workRoot', r['workRoot'], HOST.get('workRoot', ''))
        _interp('host.porch', r['porch'], 'porch on PATH' if r['porch'] else 'install porch (porch-remote.md)')
        _interp('host.tmux', r['tmux'], 'tmux present (durable sessions)' if r['tmux'] else 'install tmux', warn=not r['tmux'])
        _interp('host.git', r['git'], 'git present' if r['git'] else 'install git')
        agents = (r.get('agents') or '').split()
        _interp('host.agents', bool(agents), ' '.join(agents) or 'no agent CLI found', warn=not agents)


# -------------------------------------------------------------- mutations


def wg_binary():
    if devlocal.DEV_OS == 'windows':
        default = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'WireGuard' / 'wg.exe'
        return str(default) if default.exists() else shutil.which('wg')
    return shutil.which('wg')


def init_keys():
    if not BRIDGE:
        raise SystemExit(f"host '{HOST_ID}' has no bridge block; init-keys is only for bridged hosts")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    conf = STATE / f'{HOST_ID}.conf'
    if conf.exists():
        raise SystemExit(f'{conf} already exists; inspect before regenerating (set-peer updates the key)')
    wg = wg_binary()
    if not wg:
        raise SystemExit('wg not found; install wireguard-tools / WireGuard for Windows')
    private = subprocess.check_output([wg, 'genkey'], text=True).strip()
    public = subprocess.check_output([wg, 'pubkey'], input=private, text=True).strip()
    conf.write_text(f'''[Interface]
PrivateKey = {private}
Address = {DEV_IP}/32

[Peer]
PublicKey = PENDING
AllowedIPs = {WIN_IP}/32
Endpoint = 0.0.0.0:{LISTEN_PORT}
PersistentKeepalive = 25
''')
    os.chmod(conf, 0o600)
    print(f'Dev public key (paste into setup-host-bridge.ps1 -PeerPublicKey):\n\n  {public}\n')
    print(f'Wrote {conf} (mode 600, gitignored). Next:')
    print(f'  1. On the host (elevated): setup-host-bridge.ps1 -PeerPublicKey {public} '
          f'-ListenPort {LISTEN_PORT} -HostTunnelIp {WIN_IP} -DevTunnelIp {DEV_IP} '
          f'-WorkRoot {HOST.get("workRoot", "C:/Work").replace("/", chr(92))} -ShareName {SHARE} -AccountName {ACCOUNT}')
    print(f'  2. Save the printed smbJson as {STATE / "smb.json"} (mode 600)')
    print(f'  3. Run: onboard.py set-peer <hostPublicKey>  (printed by the ps1)')


def set_peer(key):
    if not re.fullmatch(r'[A-Za-z0-9+/]{43}=', key):
        raise SystemExit('Not a WireGuard public key (44-char base64 ending in =)')
    conf = STATE / f'{HOST_ID}.conf'
    if not conf.exists():
        raise SystemExit(f'{conf} missing; run init-keys first')
    text = conf.read_text()
    new, n = re.subn(r'^PublicKey = .*$', f'PublicKey = {key}', text, flags=re.M)
    if n != 1:
        raise SystemExit('conf has no single PublicKey line; inspect it')
    conf.write_text(new)
    os.chmod(conf, 0o600)
    print('Peer key recorded. Next: work-bridge.py connect')


# -------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host')
    parser.add_argument('command', choices=['check', 'init-keys', 'set-peer'])
    parser.add_argument('key', nargs='?')
    args = parser.parse_args()
    if args.command == 'init-keys':
        init_keys()
        return
    if args.command == 'set-peer':
        if not args.key:
            parser.error('set-peer requires the host public key')
        set_peer(args.key)
        return

    print(f'Onboarding check — host "{HOST_ID}" ({OS_KIND}), dev OS: {devlocal.DEV_OS}\n')
    print('== dev machine =='); check_dev()
    print('== reachability =='); ssh_ok = check_reachability()
    print('== host baseline =='); check_host(ssh_ok)
    print()
    counts = {s: sum(1 for st, _, _ in results if st == s) for s in (PASS, WARN, FAIL, SKIP)}
    print(f"\n{counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail, {counts[SKIP]} skip")
    if counts[FAIL]:
        print('Resolve FAILs top-down; each hint names the owning step in references/onboarding.md.')
        sys.exit(1)


if __name__ == '__main__':
    main()
