# Source this file for config helpers used by host.sh and peers.
# Requires python3 and a resolved config (see lib/config.py --help).

RA_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RA_CONFIG_PY="$RA_SCRIPTS_DIR/lib/config.py"

ra_field() { python3 "$RA_CONFIG_PY" field "$@"; }
ra_host_json() { python3 "$RA_CONFIG_PY" host "$@"; }
