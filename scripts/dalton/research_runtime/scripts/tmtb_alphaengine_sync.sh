#!/usr/bin/env bash
# Wrapper: run with the market-digest venv and explicit environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CODE="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_CODE:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]]; then
    WS="${DALTON_OCW_ROOT}"
    PY="${DALTON_OCW_ROOT}/skills/market-digest/venv/bin/python"
else
    WS="${DALTON_RESEARCH_CODE:-${DEFAULT_CODE}}"
    RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
    PY="${RESEARCH_HOME}/venvs/market-digest/bin/python"
fi

if [[ ! -x "${PY}" ]]; then
    echo "ERROR: market-digest venv missing; run: bash ${WS}/skills/market-digest/run.sh self-test" >&2
    exit 1
fi
exec "${PY}" -W ignore "${WS}/scripts/tmtb_alphaengine_sync.py" "$@"
