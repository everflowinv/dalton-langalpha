#!/usr/bin/env bash
set -euo pipefail

# Bootstrap runner for podcast-weekly-digest inside Dalton host data tasks.
# Resolves workspace from OPENCLAW_WORKSPACE / DALTON_DATA_ROOT without ~/.openclaw.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WS_DIR="${OPENCLAW_WORKSPACE:-${DALTON_DATA_ROOT:-$(cd "$SKILL_DIR/../.." && pwd)}}"
export OPENCLAW_WORKSPACE="$WS_DIR"

VENV="$SKILL_DIR/.venv"
REQ="$SCRIPT_DIR/requirements.txt"
BASE_PY="${DALTON_PYTHON_BIN:-python3}"

if [ ! -x "$VENV/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV" --python "$BASE_PY" >/dev/null
  else
    "$BASE_PY" -m venv "$VENV"
  fi
fi

if [ -f "$REQ" ] && ! "$VENV/bin/python" -c "import tos" >/dev/null 2>&1; then
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$VENV/bin/python" -r "$REQ" >/dev/null
  else
    "$VENV/bin/pip" install -q -r "$REQ" >/dev/null
  fi
fi

exec "$VENV/bin/python" "$SCRIPT_DIR/podcast_weekly_cli.py" "$@"
