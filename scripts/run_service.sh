#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
exec python -m archive_extractor.cli service --port "${PORT:-5001}"
