#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CODE="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_CODE:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]]; then
    FUNDA_CODE="${DALTON_OCW_ROOT}"
    FUNDA_PY="${DALTON_OCW_ROOT}/skills/market-digest/venv/bin/python"
else
    FUNDA_CODE="${DALTON_RESEARCH_CODE:-${DEFAULT_CODE}}"
    FUNDA_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
    FUNDA_PY="${FUNDA_HOME}/venvs/market-digest/bin/python"
fi

if [[ ! -x "${FUNDA_PY}" ]]; then
    echo '{"ok":false,"error":"market-digest Python missing","hint":"恢复现有 market-digest 环境后重试。"}'
    exit 1
fi
exec "${FUNDA_PY}" -W ignore "${FUNDA_CODE}/scripts/funda_ai.py" "$@"
