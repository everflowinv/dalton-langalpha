#!/usr/bin/env bash
# Read-only library adapter; uses explicit environment and managed market-digest venv.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CODE="$(cd "${SCRIPT_DIR}/.." && pwd)"

LIBRARY_CODE="${DALTON_RESEARCH_CODE:-${DEFAULT_CODE}}"
LIBRARY_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
LIBRARY_PY="${LIBRARY_HOME}/venvs/market-digest/bin/python"

if [[ ! -x "${LIBRARY_PY}" ]]; then
    echo '{"ok":false,"status":"error","error":"market-digest Python missing","hint":"Restore the existing market-digest environment; no database was created."}'
    exit 1
fi
exec "${LIBRARY_PY}" -W ignore "${LIBRARY_CODE}/scripts/research_library.py" "$@"
