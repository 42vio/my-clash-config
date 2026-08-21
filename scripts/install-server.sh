#!/bin/sh
# Thin wrapper: validate python3, then exec the real installer with
# the original arguments.  No second implementation lives here.
set -eu

if ! command -v python3 >/dev/null 2>&1; then
    echo "install-server: python3 is required" >&2
    exit 1
fi

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "$here/install_server.py" "$@"
