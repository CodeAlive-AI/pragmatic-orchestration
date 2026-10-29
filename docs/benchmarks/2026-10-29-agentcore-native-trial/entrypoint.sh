#!/bin/bash
set -euo pipefail
trap 'printf "PORCH_BOOT failed at line %s\n" "$LINENO" >&2' ERR
printf 'PORCH_BOOT uid=%s gid=%s\n' "$(id -u)" "$(id -g)"
# AgentCore Instances has no runtime chown/setuid capabilities. Image ownership
# and USER are prepared at build time; create only declared user-owned paths.
test "$(id -u)" -eq 1000
mkdir -p /mnt/work/porch /mnt/work/codex /mnt/work/task
printf 'PORCH_BOOT volume initialized\n'
exec python3 -u /opt/porch/scripts/lib/agentcore_guest.py --bind 0.0.0.0
