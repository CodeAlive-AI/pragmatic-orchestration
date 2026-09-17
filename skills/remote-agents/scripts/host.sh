#!/usr/bin/env bash
set -euo pipefail

# Remote agent host lifecycle and transport entrypoint.
# Host identity (SSH alias, AWS instance, work root) comes from config.json;
# run `scripts/lib/config.py hosts` to see defined hosts.

usage() {
  cat >&2 <<'EOF'
usage: host.sh [--host <id>] <command>

  status              instance state + SSM reachability
  ssh                 open an interactive SSH-over-SSM session
  run <cmd...>        run a command over SSH (batch-friendly)
  start|stop|restart  control the EC2 instance (waits for SSM)
  logs                tail <workRoot>/bootstrap.log
  audit               run Test-WorkStorage.ps1 on the host (Windows hosts)
  desktop-tunnel      SSM loopback port-forward for RDP (Windows hosts)
  desktop-open        open the configured RDP client (Windows hosts)
  desktop-start|desktop-status|desktop-probe|desktop-viewer
  desktop-viewer-stop|desktop-stop|desktop-clear-auth
                      headless desktop controller (scripts/desktop.py)

Host selection: --host flag, $REMOTE_AGENTS_HOST, config defaultHost, or the
sole defined host. Copy config.example.json to config.json first.
EOF
  exit 2
}

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PY="$HERE/lib/config.py"

# python3 on Unix; `python`/`py` on Windows (Git Bash).
PYBIN=""
for _p in python3 python py; do
  if command -v "$_p" >/dev/null 2>&1; then PYBIN="$_p"; break; fi
done
[ -n "$PYBIN" ] || { echo "host.sh: python is required (python3 or python)" >&2; exit 2; }

HOST_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST_ID="$2"; shift 2 ;;
    *) break ;;
  esac
done
[ $# -ge 1 ] || usage
CMD="$1"; shift

export REMOTE_AGENTS_HOST="${HOST_ID:-${REMOTE_AGENTS_HOST:-}}"
HOST_ID="$("$PYBIN" "$CONFIG_PY" resolve)" || exit 2
HOST_JSON="$("$PYBIN" "$CONFIG_PY" host "$HOST_ID")" || exit 2

jfield() { "$PYBIN" -c 'import json,sys
node=json.loads(sys.argv[1])
for p in sys.argv[2].split("."):
    node = node.get(p) if isinstance(node, dict) else None
    if node is None: break
print("" if node is None else (json.dumps(node) if isinstance(node,(dict,list)) else node))' "$HOST_JSON" "$1"; }

ALIAS="$(jfield sshAlias)"
OS="$(jfield os)"
WORK_ROOT="$(jfield workRoot)"
PROFILE="$(jfield aws.profile)"
REGION="$(jfield aws.region)"
INSTANCE="$(jfield aws.instanceId)"
AWS_CFG="$(jfield aws.configFile)"
RDP_LOCAL_PORT="$(jfield desktop.localPort)"; RDP_LOCAL_PORT="${RDP_LOCAL_PORT:-13389}"
RDP_REMOTE_PORT="$(jfield desktop.remotePort)"; RDP_REMOTE_PORT="${RDP_REMOTE_PORT:-3389}"

if [ -n "$AWS_CFG" ]; then export AWS_CONFIG_FILE="${AWS_CFG/#\~/$HOME}"; fi
AWS=(aws)
[ -n "$PROFILE" ] && AWS+=(--profile "$PROFILE")
[ -n "$REGION" ] && AWS+=(--region "$REGION")

require_instance() {
  if [ -z "$INSTANCE" ] || [ "$INSTANCE" = "null" ]; then
    echo "host.sh: host '${HOST_ID}' has no aws.instanceId in config.json" >&2; exit 2
  fi
}

wait_for_ssm() {
  local connection
  for _ in {1..30}; do
    connection=$("${AWS[@]}" ssm get-connection-status --target "$INSTANCE" --query Status --output text 2>/dev/null || true)
    if [[ "$connection" == 'connected' ]]; then
      return 0
    fi
    sleep 5
  done
  echo 'SSM did not become connected within 150 seconds.' >&2
  return 1
}

require_windows() {
  if [ "$OS" != "windows" ]; then
    echo "host.sh: $CMD applies only to Windows hosts" >&2; exit 2
  fi
}

case "$CMD" in
  status)
    if [ -n "$INSTANCE" ] && [ "$INSTANCE" != "null" ]; then
      "${AWS[@]}" ec2 describe-instances \
        --instance-ids "$INSTANCE" \
        --query 'Reservations[0].Instances[0].{State:State.Name,Type:InstanceType,AZ:Placement.AvailabilityZone,PrivateIP:PrivateIpAddress,PublicIP:PublicIpAddress,LaunchTime:LaunchTime}' \
        --output table
      "${AWS[@]}" ssm get-connection-status --target "$INSTANCE" --output table || true
    else
      echo "no aws.instanceId configured; checking SSH alias only" >&2
      ssh -o ConnectTimeout=10 -o BatchMode=yes "$ALIAS" 'echo SSH_OK' 2>/dev/null || echo "SSH unreachable"
    fi
    ;;
  ssh)
    exec ssh "$ALIAS" "$@"
    ;;
  run)
    [ $# -ge 1 ] || { echo "usage: host.sh run <cmd...>" >&2; exit 2; }
    exec ssh "$ALIAS" "$*"
    ;;
  start)
    require_instance
    "${AWS[@]}" ec2 start-instances --instance-ids "$INSTANCE" --output table
    "${AWS[@]}" ec2 wait instance-running --instance-ids "$INSTANCE"
    wait_for_ssm
    echo "host '${HOST_ID}' is running and reachable through SSM."
    ;;
  stop)
    require_instance
    "${AWS[@]}" ec2 stop-instances --instance-ids "$INSTANCE" --output table
    "${AWS[@]}" ec2 wait instance-stopped --instance-ids "$INSTANCE"
    echo "host '${HOST_ID}' is stopped."
    ;;
  restart)
    require_instance
    "${AWS[@]}" ec2 reboot-instances --instance-ids "$INSTANCE"
    wait_for_ssm
    echo "host '${HOST_ID}' is reachable through SSM."
    ;;
  logs)
    if [ "$OS" = "windows" ]; then
      exec ssh "$ALIAS" "powershell -NoProfile -Command \"Get-Content -Tail 120 '${WORK_ROOT}/bootstrap.log'\""
    else
      exec ssh "$ALIAS" "tail -120 '${WORK_ROOT}/bootstrap.log' 2>/dev/null || journalctl --user -n 120 --no-pager"
    fi
    ;;
  audit)
    require_windows
    # Run the local, reviewed script over stdin: the host's copy may lag or omit it.
    exec ssh "$ALIAS" 'powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create([Console]::In.ReadToEnd()))"' < "$HERE/Test-WorkStorage.ps1"
    ;;
  desktop-tunnel)
    require_windows
    require_instance
    "${AWS[@]}" ssm start-session \
      --target "$INSTANCE" \
      --document-name AWS-StartPortForwardingSession \
      --parameters "{\"portNumber\":[\"${RDP_REMOTE_PORT}\"],\"localPortNumber\":[\"${RDP_LOCAL_PORT}\"]}"
    ;;
  desktop-start|desktop-status|desktop-probe|desktop-viewer|desktop-viewer-stop|desktop-open|desktop-stop|desktop-clear-auth)
    require_windows
    desktop_command="${CMD#desktop-}"
    exec "$PYBIN" "$HERE/desktop.py" "$desktop_command" "$@"
    ;;
  *)
    echo "host.sh: unknown command '$CMD'" >&2; usage ;;
esac
