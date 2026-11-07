#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Reuse the EXISTING working environment from the earlier TFLite package.
if [[ -x "${HERE}/.venv/bin/python" ]]; then
  PYTHON="${HERE}/.venv/bin/python"
elif [[ -x "${HOME}/TwirlyTunes_Pi_Deployment/.venv/bin/python" ]]; then
  PYTHON="${HOME}/TwirlyTunes_Pi_Deployment/.venv/bin/python"
else
  echo 'No virtual environment found.' >&2
  echo 'Install dependencies: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt' >&2
  exit 1
fi
cd "$HERE"
exec "$PYTHON" -u src/main.py "$@"
