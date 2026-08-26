#!/bin/sh
# clash-sub integration installer bootstrap. Run as root on Debian 12 after
# 3x-ui is installed with one Reality inbound on port 10443.
set -eu
[ "$(id -u)" = 0 ] || { echo "install.sh must run as root" >&2; exit 1; }
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update
    apt-get install -y --no-install-recommends python3 python3-venv curl
fi
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install --quiet -r requirements.txt
exec bin/clash-sub install
