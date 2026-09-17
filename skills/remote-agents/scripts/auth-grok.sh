#!/usr/bin/env bash
set -euo pipefail
if ! command -v grok >/dev/null 2>&1; then
  echo "grok CLI is not installed." >&2
  exit 1
fi
exec grok login --device-auth
