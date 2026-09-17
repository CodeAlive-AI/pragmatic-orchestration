#!/usr/bin/env python3
"""Connect the dedicated WireGuard tunnel and mount the Windows Work share."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import config as ra_config  # noqa: E402
import devlocal  # noqa: E402

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
if devlocal.DEV_OS != 'windows':
    # Keep network filesystems outside repositories and skill discovery trees.
    # (Windows reaches the share by UNC path; there is no mount directory.)
    _skill_root = Path(__file__).resolve().parents[1]
    try:
        _repo_root = Path(subprocess.check_output(
            ['git', '-C', str(_skill_root), 'rev-parse', '--show-toplevel'],
            text=True, stderr=subprocess.DEVNULL).strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        _repo_root = _skill_root
    if not MOUNT.is_absolute() or MOUNT.resolve().is_relative_to(_repo_root):
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
    if devlocal.smb_mounted(WINDOWS_IP, SHARE, MOUNT):
        print(devlocal.smb_access_path(WINDOWS_IP, SHARE, MOUNT))
        return
    if not devlocal.tcp_open(WINDOWS_IP, 445, timeout=5):
        raise RuntimeError(f'{WINDOWS_IP}:445 unreachable; is the tunnel up?')
    # Only this helper consumes its dedicated, locally generated credential.
    credential = json.loads((STATE / 'smb.json').read_text())
    if devlocal.DEV_OS != 'windows':
        if not MOUNT.is_dir() or MOUNT.is_symlink():
            raise RuntimeError(f'Provision a real local mount directory first: {MOUNT}')
        if any(MOUNT.iterdir()):
            raise RuntimeError('Refusing to mount over a nonempty directory')
    path = devlocal.smb_mount(WINDOWS_IP, SHARE, MOUNT, credential, STATE)
    print(path)


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
            devlocal.wg_service_restart(LABEL)
            for _ in range(30):
                if devlocal.tcp_open(WINDOWS_IP, 445, timeout=1):
                    break
                time.sleep(1)
            else:
                raise RuntimeError('System Work tunnel did not become ready; inspect its service log.')
        else:
            devlocal.wg_up(CONFIG)
        mount()
    elif action == 'mount':
        mount()
    elif action in ('unmount', 'disconnect'):
        devlocal.smb_unmount(WINDOWS_IP, SHARE, MOUNT)
        if action == 'disconnect':
            devlocal.smb_cred_cleanup(STATE, WINDOWS_IP)
            if (STATE / 'system-service.json').exists():
                devlocal.wg_service_stop(LABEL)
            else:
                devlocal.wg_down(CONFIG)
    elif action == 'status':
        reachable = devlocal.tcp_open(WINDOWS_IP, 445, timeout=3)
        print(json.dumps({'smb_reachable': reachable,
                          'mounted': devlocal.smb_mounted(WINDOWS_IP, SHARE, MOUNT),
                          'mount_path': devlocal.smb_access_path(WINDOWS_IP, SHARE, MOUNT)}))
    else:
        print(devlocal.smb_access_path(WINDOWS_IP, SHARE, MOUNT))


if __name__ == '__main__':
    main()
