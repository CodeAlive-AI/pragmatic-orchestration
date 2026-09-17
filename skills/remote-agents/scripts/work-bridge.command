#!/bin/zsh
set -eu
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd -- "${0:A:h}"
python3 ./work-bridge.py connect
