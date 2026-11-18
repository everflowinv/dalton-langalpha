#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${DALTON_RESEARCH_HOME:-}" ]]; then
  RESEARCH_HOME="$DALTON_RESEARCH_HOME"
elif [[ -n "${DALTON_OCW_ROOT:-}" ]]; then
  RESEARCH_HOME="$DALTON_OCW_ROOT"
else
  RESEARCH_HOME="$HOME/Library/Application Support/Everflow/research"
fi

ENV_FILE="${GUIDEPOINT_ENV_FILE:-$RESEARCH_HOME/config/guidepoint_mcp.env}"
PYTHON_BIN="${GUIDEPOINT_PYTHON:-/opt/homebrew/bin/python3.12}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[guidepoint-proxy] Missing env file: $ENV_FILE" >&2
  exit 1
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[guidepoint-proxy] Python interpreter not executable: $PYTHON_BIN" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

exec "$PYTHON_BIN" "$SCRIPT_DIR/guidepoint_mcp_proxy.py"
