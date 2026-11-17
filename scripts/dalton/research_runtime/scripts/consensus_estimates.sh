#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_CODE="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_CODE:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]]; then
    CODE_ROOT="${DALTON_OCW_ROOT}"
    RESEARCH_HOME=""
    VENV="${DALTON_OCW_ROOT}/state/venvs/consensus-estimates"
else
    CODE_ROOT="${DALTON_RESEARCH_CODE:-${DEFAULT_CODE}}"
    RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
    VENV="${RESEARCH_HOME}/venvs/consensus-estimates"
fi
REQ="${CODE_ROOT}/scripts/consensus_estimates_requirements.txt"

_find_pinned_python() {
    local ver="$1"
    local cand
    for cand in \
        "/opt/homebrew/bin/python${ver}" \
        "/opt/homebrew/opt/python@${ver}/bin/python${ver}" \
        "/usr/local/bin/python${ver}" \
        "/usr/bin/python${ver}" \
        "${HOME}/.local/bin/python${ver}"; do
        if [[ -x "${cand}" ]]; then
            printf '%s\n' "${cand}"
            return 0
        fi
    done
    if command -v "python${ver}" >/dev/null 2>&1; then
        command -v "python${ver}"
        return 0
    fi
    return 1
}

_read_legacy_pyvenv_mm() {
    local cfg
    for cfg in \
        "${DALTON_CONSENSUS_LEGACY_PYVENV_CFG:-}" \
        "${RESEARCH_HOME:+${RESEARCH_HOME}/state/venvs/consensus-estimates.python_version}" \
        "${DALTON_OCW_ROOT:+${DALTON_OCW_ROOT}/state/venvs/consensus-estimates/pyvenv.cfg}" \
        "${HOME}/.openclaw/workspace/state/venvs/consensus-estimates/pyvenv.cfg"; do # 兼容读取旧 venv Python 版本
        if [[ -n "${cfg}" && -f "${cfg}" ]]; then
            local ver=""
            if [[ "${cfg}" == *.python_version ]]; then
                ver="$(tr -d '[:space:]' < "${cfg}" 2>/dev/null || true)"
            else
                ver="$(sed -n -E 's/^[[:space:]]*version(_info)?[[:space:]]*=[[:space:]]*([0-9]+\.[0-9]+).*$/\2/p' "${cfg}" 2>/dev/null | head -n 1 || true)"
            fi
            if [[ "${ver}" =~ ^[0-9]+\.[0-9]+$ ]]; then
                printf '%s\n' "${ver}"
                return 0
            fi
        fi
    done
    return 1
}

if [[ ! -x "${VENV}/bin/python" ]]; then
    LEGACY_MM="$(_read_legacy_pyvenv_mm || true)"
    BASE_PYTHON=""
    if [[ -n "${LEGACY_MM}" ]]; then
        BASE_PYTHON="$(_find_pinned_python "${LEGACY_MM}" || true)"
    fi
    if [[ -z "${BASE_PYTHON}" ]]; then
        if [[ -x "/opt/homebrew/bin/python3" ]]; then
            BASE_PYTHON="/opt/homebrew/bin/python3"
        else
            BASE_PYTHON="$(command -v python3 || true)"
        fi
    fi
    if [[ -z "${BASE_PYTHON}" ]]; then
        echo "ERROR: python3 is required for consensus-estimates venv" >&2
        exit 1
    fi
    mkdir -p "$(dirname "${VENV}")"
    "${BASE_PYTHON}" -m venv "${VENV}" >&2
fi
if [[ ! -f "${VENV}/.deps_ok" || "${REQ}" -nt "${VENV}/.deps_ok" ]]; then
    "${VENV}/bin/python" -m pip install -r "${REQ}" >&2
    touch "${VENV}/.deps_ok"
fi
exec "${VENV}/bin/python" "${CODE_ROOT}/scripts/consensus_estimates.py" "$@"
