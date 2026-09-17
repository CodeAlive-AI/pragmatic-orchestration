#!/usr/bin/env python3
"""One-time root installation of a fixed, password-free Work tunnel service."""
import configparser
import json
import os
from pathlib import Path
import plistlib
import pwd
import shlex
import shutil
import subprocess as sp
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import config as ra_config  # noqa: E402

if os.geteuid() != 0:
    raise SystemExit('Run this installer once with sudo.')
# The installer lives in the skill's scripts directory.
user = os.environ.get('SUDO_USER')
if not user or user == 'root':
    raise SystemExit('Run with sudo from the owning Mac account.')
uid = pwd.getpwnam(user).pw_uid

# Host identity for the sudo-run installer comes from the environment: the
# owning user's config file resolves the host and its bridge block.
_config, _ = ra_config.load_config()
HOST_ID, HOST = ra_config.resolve_host(_config)
BRIDGE = HOST.get('bridge') or {}
SUBNET = BRIDGE.get('subnetPrefix', '10.99.0')
WINDOWS_IP = f'{SUBNET}.1'
MAC_IP = f'{SUBNET}.2'
LISTEN_PORT = int(BRIDGE.get('listenPort', 51820))
LABEL = BRIDGE.get('launchdLabel', 'remote-agents.work-bridge')
APP_SUPPORT = Path(BRIDGE.get('appSupportDir', '/Library/Application Support/RemoteAgents-WorkBridge'))
SUDOERS_NAME = BRIDGE.get('sudoersFile', 'remote-agents-work-bridge')
LOG = Path(BRIDGE.get('logFile', '/var/log/remote-agents-work-bridge.log'))

source = Path(BRIDGE.get('stateDir') or (Path(__file__).resolve().parents[1] / '.bridge-state'))
config = configparser.ConfigParser(interpolation=None)
config.read(source / f'{HOST_ID}.conf')
if config.sections() != ['Interface', 'Peer'] \
        or config['Interface']['Address'] != f'{MAC_IP}/32' \
        or config['Peer']['AllowedIPs'] != f'{WINDOWS_IP}/32':
    raise SystemExit('Unexpected tunnel configuration; nothing installed.')

base = APP_SUPPORT
label = LABEL
plist = Path('/Library/LaunchDaemons') / (label + '.plist')
sudoers = Path('/etc/sudoers.d') / SUDOERS_NAME
if plist.exists() or sudoers.exists():
    raise SystemExit('Service already installed; inspect before changing it.')
base.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chmod(base, 0o711)
for binary in ('wg', 'wireguard-go'):
    origin = shutil.which(binary, path='/opt/homebrew/bin:/usr/local/bin')
    if not origin:
        raise SystemExit('Missing ' + binary)
    shutil.copyfile(origin, base / binary)
    os.chmod(base / binary, 0o700)
allowed = {'Interface': ['PrivateKey'], 'Peer': ['PublicKey', 'AllowedIPs', 'Endpoint', 'PersistentKeepalive']}
text = ''
for section, keys in allowed.items():
    text += '[' + section + ']\n'
    for key in keys:
        text += key + ' = ' + config[section][key] + '\n'
(base / 'wg.conf').write_text(text)
os.chmod(base / 'wg.conf', 0o600)
# The only user-controlled input is a validated IPv4 endpoint, never executable text.
request = base / 'endpoint'
request.write_text(config['Peer']['Endpoint'].split(':')[0] + '\n')
os.chown(request, uid, -1)
os.chmod(request, 0o600)
script = r'''#!/bin/sh
set -eu
base=BASEDIR
endpoint=$(/usr/bin/head -n 1 REQUEST)
case "$endpoint" in ''|*[!0-9.]*) echo 'Invalid endpoint' >&2; exit 1;; esac
/usr/bin/awk -v ip="$endpoint" 'BEGIN {n=split(ip,a,"."); if(n!=4)exit 1; for(i=1;i<=4;i++)if(a[i]=="" || a[i]>255)exit 1}'
rm -f "$base/interface.name"
export WG_TUN_NAME_FILE="$base/interface.name"
"$base/wireguard-go" -f utun &
pid=$!
cleanup() { kill "$pid" 2>/dev/null || :; rm -f "$base/interface.name"; }
trap cleanup EXIT
trap 'exit 0' TERM INT
n=0
while [ ! -f "$base/interface.name" ]; do
 n=$((n+1)); [ "$n" -le 30 ] || exit 1
 kill -0 "$pid"
 sleep 1
done
iface=$(cat "$base/interface.name")
case "$iface" in utun[0-9]*) ;; *) exit 1;; esac
"$base/wg" setconf "$iface" "$base/wg.conf"
peer=$(/usr/bin/awk '/^PublicKey = / {print $3}' "$base/wg.conf")
"$base/wg" set "$iface" peer "$peer" endpoint "$endpoint:PORT"
/sbin/ifconfig "$iface" inet MACIP/32 MACIP alias
/sbin/ifconfig "$iface" mtu 1380 up
/sbin/route -q -n add -inet WINIP/32 -interface "$iface"
wait "$pid"
'''
script = (script
          .replace('BASEDIR', shlex.quote(str(base)))
          .replace('REQUEST', shlex.quote(str(request)))
          .replace('PORT', str(LISTEN_PORT))
          .replace('MACIP', MAC_IP)
          .replace('WINIP', WINDOWS_IP))
(base / 'run.sh').write_text(script)
os.chmod(base / 'run.sh', 0o700)
plist.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': ['/bin/sh', str(base / 'run.sh')],
    'RunAtLoad': True, 'StandardOutPath': str(LOG),
    'StandardErrorPath': str(LOG),
    'EnvironmentVariables': {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}}))
os.chmod(plist, 0o644)
rule = f'{user} ALL=(root) NOPASSWD: /bin/launchctl kickstart -k system/{label}, /bin/launchctl kill SIGTERM system/{label}\n'
temp = base / 'sudoers-check'
temp.write_text(rule)
os.chmod(temp, 0o440)
sp.run(['/usr/sbin/visudo', '-cf', str(temp)], check=True)
sudoers.parent.mkdir(mode=0o750, exist_ok=True)
sudoers.write_text(rule)
os.chmod(sudoers, 0o440)
temp.unlink()
# Retire only the old, explicitly named tunnel; other VPNs are untouched.
if Path(f'/var/run/wireguard/{HOST_ID}.name').exists():
    sp.run(['/opt/homebrew/bin/bash', '/opt/homebrew/bin/wg-quick', 'down', str(source / f'{HOST_ID}.conf')],
           check=True, env=dict(os.environ, PATH='/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin'))
sp.run(['/usr/sbin/visudo', '-c'], check=True)
sp.run(['/bin/launchctl', 'bootstrap', 'system', str(plist)], check=True)
marker = source / 'system-service.json'
marker.write_text(json.dumps({'label': label}) + '\n')
os.chown(marker, uid, -1)
os.chmod(marker, 0o600)
print('Installed fixed Work tunnel service. Future connect/disconnect does not prompt for sudo.')
