#!/usr/bin/env bash
# run.sh - Auto-bootstrap wrapper (stdlib only, works offline in Dalton host tasks)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WS_DIR="${OPENCLAW_WORKSPACE:-${DALTON_DATA_ROOT:-$(cd "$SKILL_DIR/../.." && pwd)}}"
export OPENCLAW_WORKSPACE="$WS_DIR"

VENV_DIR="$SKILL_DIR/venv"
REQ_FILE="$SKILL_DIR/scripts/requirements.txt"
REQ_HASH_FILE="$VENV_DIR/.requirements.sha256"
SCRIPT_FILE="$SKILL_DIR/scripts/model_usage_cost_cli.py"

get_best_python() {
  if [ -n "${DALTON_PYTHON_BIN:-}" ] && [ -x "${DALTON_PYTHON_BIN}" ]; then
    echo "$DALTON_PYTHON_BIN"
    return 0
  fi
  for prefix in "/opt/homebrew/bin" "/usr/local/bin" "/usr/bin" "$HOME/.local/bin"; do
    if [ -d "$prefix" ]; then
      local best_py
      best_py=$(ls -1 "$prefix"/python3.* 2>/dev/null | grep -E "^$prefix/python3\.[0-9]+$" | sort -V | tail -n 1 || true)
      if [ -n "$best_py" ] && [ -x "$best_py" ]; then
        echo "$best_py"
        return 0
      fi
    fi
  done
  echo "python3"
}

hash_file() {
  local f="$1"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$f" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" | awk '{print $1}'
  else
    stat -f "%m:%z" "$f" 2>/dev/null || stat -c "%Y:%s" "$f"
  fi
}

ensure_env() {
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    local base_python
    base_python=$(get_best_python)
    echo "[bootstrap] Initializing virtualenv using: $base_python" >&2
    if command -v uv >/dev/null 2>&1; then
      uv venv "$VENV_DIR" --python "$base_python" >/dev/null
    else
      "$base_python" -m venv "$VENV_DIR"
    fi
  fi

  if [ -f "$REQ_FILE" ] && grep -qvE '^\s*(#|$)' "$REQ_FILE"; then
    local current_hash old_hash
    current_hash=$(hash_file "$REQ_FILE")
    old_hash=""
    [ -f "$REQ_HASH_FILE" ] && old_hash="$(cat "$REQ_HASH_FILE")"

    if [ "$current_hash" != "$old_hash" ]; then
      echo "[bootstrap] Installing/updating dependencies..." >&2
      if command -v uv >/dev/null 2>&1; then
        uv pip install --python "$VENV_DIR/bin/python" -r "$REQ_FILE" >/dev/null
      else
        "$VENV_DIR/bin/pip" install -r "$REQ_FILE" --quiet
      fi
      echo "$current_hash" > "$REQ_HASH_FILE"
    fi
  fi
}

ensure_env

PY_WARNINGS_FLAG=()
if [ "${SKILL_WARNINGS:-default}" = "ignore" ]; then
  PY_WARNINGS_FLAG=(-W ignore)
fi

TARGET_SCRIPT="$SCRIPT_FILE"
if [ "${1:-}" = "--script" ] && [ $# -ge 2 ]; then
  TARGET_ARG="$2"
  shift 2
  if [ -f "$TARGET_ARG" ]; then
    TARGET_SCRIPT="$TARGET_ARG"
  elif [ -f "$WS_DIR/$TARGET_ARG" ]; then
    TARGET_SCRIPT="$WS_DIR/$TARGET_ARG"
  else
    echo "ERROR: target script not found: $TARGET_ARG" >&2
    exit 1
  fi
elif [ "${1:-}" != "" ] && [[ "$1" == *.py ]]; then
  TARGET_ARG="$1"
  shift 1
  if [ -f "$TARGET_ARG" ]; then
    TARGET_SCRIPT="$TARGET_ARG"
  elif [ -f "$WS_DIR/$TARGET_ARG" ]; then
    TARGET_SCRIPT="$WS_DIR/$TARGET_ARG"
  else
    echo "ERROR: target script not found: $TARGET_ARG" >&2
    exit 1
  fi
fi

if [ ! -f "$TARGET_SCRIPT" ]; then
  echo "ERROR: CLI script not found: $TARGET_SCRIPT" >&2
  exit 1
fi

if [ ${#PY_WARNINGS_FLAG[@]} -gt 0 ]; then
  exec "$VENV_DIR/bin/python" "${PY_WARNINGS_FLAG[@]}" "$TARGET_SCRIPT" "$@"
else
  exec "$VENV_DIR/bin/python" "$TARGET_SCRIPT" "$@"
fi
