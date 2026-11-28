#!/usr/bin/env bash
# Wrapper: run with the market-digest venv and explicit environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CODE="$(cd "${SCRIPT_DIR}/.." && pwd)"

WS="${DALTON_RESEARCH_CODE:-${DEFAULT_CODE}}"
RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
PY="${RESEARCH_HOME}/venvs/market-digest/bin/python"

if [[ ! -x "${PY}" ]]; then
    echo "ERROR: market-digest venv missing; run: bash ${WS}/skills/market-digest/run.sh self-test" >&2
    exit 1
fi
exec "${PY}" -W ignore "${WS}/scripts/tmtb_alphaengine_sync.py" "$@"
