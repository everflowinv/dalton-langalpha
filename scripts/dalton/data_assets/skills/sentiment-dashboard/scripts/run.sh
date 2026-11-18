#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ -n "${DALTON_PYTHON_BIN:-}" ]]; then
  exec "$DALTON_PYTHON_BIN" "$SCRIPT_DIR/sentiment_dashboard_cli.py" "$@"
fi
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SKILL_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="$(find_python || true)"
if [[ -z "$PYTHON_BIN" ]]; then
  echo '{"ok":false,"error":"Python 3 not found","hint":"Install Python 3.11+ and rerun."}' >&2
  exit 2
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR" || {
    echo '{"ok":false,"error":"Failed to create virtualenv","hint":"Check Python venv support and write permissions."}' >&2
    exit 2
  }
fi

STAMP="$VENV_DIR/.requirements.sha256"
REQ_HASH="$(shasum -a 256 "$REQ_FILE" | awk '{print $1}')"
OLD_HASH=""
if [[ -f "$STAMP" ]]; then OLD_HASH="$(tr -d '\n' < "$STAMP")"; fi
if [[ "$REQ_HASH" != "$OLD_HASH" ]]; then
  "$VENV_DIR/bin/python" -m pip install -q --upgrade pip
  "$VENV_DIR/bin/python" -m pip install -q -r "$REQ_FILE" || {
    echo '{"ok":false,"error":"Dependency installation failed","hint":"Check network access, then rerun scripts/run.sh self-test --json."}' >&2
    exit 2
  }
  printf '%s' "$REQ_HASH" > "$STAMP"
fi

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/sentiment_dashboard_cli.py" "$@"
