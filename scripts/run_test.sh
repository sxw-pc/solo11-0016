#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
exec python -m pytest --cov=archive_extractor --cov-branch \
  --cov-report=term-missing --cov-report=html --cov-fail-under=80
