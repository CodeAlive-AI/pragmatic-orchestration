#!/usr/bin/env python3
"""Connect the dedicated WireGuard tunnel and mount the Windows Work share."""
import argparse
import fcntl
import termios
import json
import os
from pathlib import Path
import pty
import re
import select
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import config as ra_config  # noqa: E402

_config, _ = ra_config.load_config()
HOST_ID, HOST = ra_config.resolve_host(_config)
BRIDGE = HOST.get('bridge') or {}
WORK_ROOT = HOST.get('workRoot', 'C:/Work')

STATE = Path(BRIDGE.get('stateDir') or (Path(__file__).resolve().parents[1] / '.bridge-state'))
CONFIG = STATE / f'{HOST_ID}.conf'
SUBNET = BRIDGE.get('subnetPrefix', '10.99.0')
WINDOWS_IP = f'{SUBNET}.1'
MAC_IP = f'{SUBNET}.2'
LISTEN_PORT = int(BRIDGE.get('listenPort', 51820))
SHARE = BRIDGE.get('shareName', 'Work')
LABEL = BRIDGE.get('launchdLabel', 'remote-agents.work-bridge')
APP_SUPPORT = Path(BRIDGE.get('appSupportDir', '/Library/Application Support/RemoteAgents-WorkBridge'))

MOUNT = Path(os.path.expanduser(
    BRIDGE.get('mountRoot', '~/.remote-agents'))) / HOST_ID
# Keep network filesystems outside repositories and skill discovery trees.
_skill_root = Path(__file__).resolve().parents[1]
if not MOUNT.is_absolute() or MOUNT.resolve().is_relative_to(_skill_root):
    raise RuntimeError('Work mount must be an absolute path outside the repository')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def aws_env():
    aws = HOST.get('aws') or {}
    env = dict(os.environ)
    if aws.get('configFile'):
        env['AWS_CONFIG_FILE'] = os.path.expanduser(aws['configFile'])
    cmd = ['aws']
    if aws.get('profile'):
        cmd += ['--profile', aws['profile']]
    if aws.get('region'):
        cmd += ['--region', aws['region']]
    return cmd, env


def public_endpoint():
    instance = (HOST.get('aws') or {}).get('instanceId')
    if not instance:
        raise RuntimeError(f"host '{HOST_ID}' has no aws.instanceId; cannot resolve the tunnel endpoint")
    cmd, env = aws_env()
    endpoint = subprocess.check_output(
        cmd + ['ec2', 'describe-instances', '--instance-ids', instance,
               '--query', 'Reservations[0].Instances[0].PublicIpAddress', '--output', 'text'],
        env=env, text=True).strip()
    if not re.fullmatch(r'(?:\d{1,3}\.){3}\d{1,3}', endpoint):
        raise RuntimeError('Windows host has no public endpoint; check host status.')
    return endpoint


def mount():
    if os.path.ismount(MOUNT):
        print(MOUNT)
        return
    run('nc', '-z', '-G', '5', '-w', '5', WINDOWS_IP, '445')
    # Only this helper consumes its dedicated, locally generated credential.
    credential = json.loads((STATE / 'smb.json').read_text())
    if not MOUNT.is_dir() or MOUNT.is_symlink():
        raise RuntimeError(f'Provision a real local mount directory first: {MOUNT}')
    if any(MOUNT.iterdir()):
        raise RuntimeError('Refusing to mount over a nonempty directory')
    master, slave = pty.openpty()
    def controlling_terminal():
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

    process = subprocess.Popen(['/sbin/mount_smbfs',
        f'//{credential["username"]}@{WINDOWS_IP}/{SHARE}', str(MOUNT)],
        stdin=slave, stdout=slave, stderr=slave, preexec_fn=controlling_terminal)
    os.close(slave)
    pending = b''
    sent = False
    deadline = time.monotonic() + 30
    try:
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    pending += os.read(master, 4096)
                except OSError:
                    break
                if b'password' in pending.lower() and not sent:
                    os.write(master, (credential['password'] + '\n').encode())
                    sent = True
        if process.poll() is None:
            process.wait(timeout=2)
        if process.returncode != 0 or not os.path.ismount(MOUNT):
            detail = pending.decode(errors='replace').replace(credential['password'], '[redacted]')
            raise RuntimeError(f'SMB mount failed (no retry): {detail}')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    print(MOUNT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['connect', 'mount', 'unmount', 'disconnect', 'status', 'path'])
    action = parser.parse_args().action
    if action == 'connect':
        # Query on every connection: the public address changes after stop/start.
        endpoint = public_endpoint()
        config = CONFIG.read_text()
        config, count = re.subn(r'^Endpoint = .*$', f'Endpoint = {endpoint}:{LISTEN_PORT}', config, flags=re.M)
        if count != 1:
            raise RuntimeError('Invalid tunnel configuration')
        CONFIG.write_text(config)
        if (STATE / 'system-service.json').exists():
            request = APP_SUPPORT / 'endpoint'
            request.write_text(endpoint + '\n')
            run('sudo', '-n', '/bin/launchctl', 'kickstart', '-k', f'system/{LABEL}')
            for _ in range(30):
                probe = subprocess.run(['nc', '-z', '-G', '1', '-w', '1', WINDOWS_IP, '445'], capture_output=True)
                if probe.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('System Work tunnel did not become ready; inspect its service log.')
        else:
            run('sudo', 'wg-quick', 'up', str(CONFIG))
        mount()
    elif action == 'mount':
        mount()
    elif action in ('unmount', 'disconnect'):
        if os.path.ismount(MOUNT):
            run('/sbin/umount', str(MOUNT))
        if action == 'disconnect':
            if (STATE / 'system-service.json').exists():
                run('sudo', '-n', '/bin/launchctl', 'kill', 'SIGTERM', f'system/{LABEL}')
            else:
                run('sudo', 'wg-quick', 'down', str(CONFIG))
    elif action == 'status':
        reachable = subprocess.run(['nc', '-z', '-G', '3', '-w', '3', WINDOWS_IP, '445'], capture_output=True).returncode == 0
        print(json.dumps({'smb_reachable': reachable, 'mounted': os.path.ismount(MOUNT), 'mount_path': str(MOUNT)}))
    else:
        print(MOUNT)


if __name__ == '__main__':
    main()
