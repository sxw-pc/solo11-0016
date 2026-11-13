#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
PY=python
command -v python >/dev/null 2>&1 || PY=python3
exec "$PY" scripts/install_offline.py "$@"
