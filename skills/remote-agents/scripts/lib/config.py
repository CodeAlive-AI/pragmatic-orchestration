#!/usr/bin/env python3
"""Resolve the remote-agents config and answer field queries.

Config file resolution order:
  1. $REMOTE_AGENTS_CONFIG
  2. <skill-dir>/config.json   (gitignored, user-local)

Host resolution order:
  1. --host <id>
  2. $REMOTE_AGENTS_HOST
  3. config's defaultHost
  4. the only defined host, if exactly one exists

Usage:
  config.py config-path
  config.py resolve [--host <id>]        # resolved host id
  config.py hosts
  config.py host <id>            # print host object as JSON
  config.py field <path> [--host <id>]   # e.g. field desktop.localPort
  config.py aws-env [--host <id>]        # shell env lines for the aws block
"""
import json
import os
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[2]


def die(msg):
    print(f"config.py: {msg}", file=sys.stderr)
    sys.exit(2)


def load_config():
    override = os.environ.get('REMOTE_AGENTS_CONFIG')
    path = Path(override).expanduser() if override else SKILL_DIR / 'config.json'
    if not path.is_file():
        die(f"no config at {path}; copy config.example.json to config.json and fill it in")
    try:
        return json.loads(path.read_text(encoding='utf-8')), path
    except json.JSONDecodeError as exc:
        die(f"{path}: invalid JSON: {exc}")


def resolve_host(config, host_id=None):
    host_id = host_id or os.environ.get('REMOTE_AGENTS_HOST') or config.get('defaultHost')
    hosts = config.get('hosts') or {}
    if host_id:
        if host_id not in hosts:
            die(f"unknown host '{host_id}'; defined: {', '.join(sorted(hosts)) or '(none)'}")
        return host_id, hosts[host_id]
    if len(hosts) == 1:
        return next(iter(hosts.items()))
    die("multiple hosts defined; pass --host <id> or set defaultHost")


def get_field(obj, dotted):
    node = obj
    for part in dotted.split('.'):
        if not isinstance(node, dict) or part not in node:
            die(f"field '{dotted}' not present (missing '{part}')")
        node = node[part]
    return node


def main():
    argv = sys.argv[1:]
    host_id = None
    if '--host' in argv:
        i = argv.index('--host')
        host_id = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
    if not argv:
        die(__doc__)
    cmd, rest = argv[0], argv[1:]
    config, path = load_config()

    if cmd == 'config-path':
        print(path)
    elif cmd == 'resolve':
        hid, _ = resolve_host(config, host_id)
        print(hid)
    elif cmd == 'hosts':
        print('\n'.join(sorted(config.get('hosts', {}))))
    elif cmd == 'host':
        hid = rest[0] if rest else None
        _, host = resolve_host(config, hid)
        print(json.dumps(host, indent=2))
    elif cmd == 'field':
        if not rest:
            die('field requires a dotted path, e.g. field desktop.localPort')
        _, host = resolve_host(config, host_id)
        value = get_field(host, rest[0])
        print(json.dumps(value) if isinstance(value, (dict, list)) else value)
    elif cmd == 'aws-env':
        _, host = resolve_host(config, host_id)
        aws = host.get('aws') or {}
        profile = aws.get('profile')
        region = aws.get('region')
        config_file = aws.get('configFile')
        if config_file:
            print(f"export AWS_CONFIG_FILE={os.path.expanduser(config_file)}")
        if profile:
            print(f"export AWS_PROFILE={profile}")
        if region:
            print(f"export AWS_DEFAULT_REGION={region}")
    else:
        die(f"unknown command '{cmd}'")


if __name__ == '__main__':
    main()
