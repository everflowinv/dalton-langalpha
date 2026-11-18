#!/usr/bin/env bash
# Launch wrapper for Dalton usage_collector.py.
# Picks the skill's venv python when present, else Homebrew Python 3.12 (never /usr/bin/python3).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../.." && pwd)}"
SKILL_DIR="${DALTON_USAGE_SKILL_DIR:-${REPO_ROOT}/scripts/dalton/openclaw_overlays/skills/model-usage-cost}"
COLLECTOR_PY="${SCRIPT_DIR}/usage_collector.py"
HOMEBREW_PY312="${DALTON_HOMEBREW_PYTHON_312:-/opt/homebrew/bin/python3.12}"
SYSTEM_PY="/usr/bin/python3"

VENV_PY="${SKILL_DIR}/venv/bin/python"
PYTHON_BIN=""

if [[ -n "${DALTON_USAGE_PYTHON:-}" ]]; then
  if [[ ! -x "${DALTON_USAGE_PYTHON}" ]]; then
    echo "ERROR: Configured DALTON_USAGE_PYTHON is not executable: ${DALTON_USAGE_PYTHON}" >&2
    exit 1
  fi
  PYTHON_BIN="${DALTON_USAGE_PYTHON}"
elif [[ -x "${VENV_PY}" ]]; then
  PYTHON_BIN="${VENV_PY}"
elif [[ -x "${HOMEBREW_PY312}" ]]; then
  PYTHON_BIN="${HOMEBREW_PY312}"
else
  echo "ERROR: No usable Python found (checked ${VENV_PY} and ${HOMEBREW_PY312}; refusing ${SYSTEM_PY})" >&2
  exit 1
fi

if [[ "${PYTHON_BIN}" == "${SYSTEM_PY}" ]]; then
  echo "ERROR: Refusing system Python ${SYSTEM_PY}" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${COLLECTOR_PY}" "$@"
