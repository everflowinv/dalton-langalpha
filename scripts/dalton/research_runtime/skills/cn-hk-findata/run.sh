#!/usr/bin/env bash
# run.sh - Auto-bootstrapping wrapper for cn-hk-findata (pinned Python 3.12)
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="${SKILL_DIR}/requirements.txt"
SCRIPT_FILE="${SKILL_DIR}/scripts/financemcp_adapter.py"

RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
VENV_DIR="${RESEARCH_HOME}/venvs/cn-hk-findata"

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

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
    BASE_PYTHON="$(_find_pinned_python 3.12)" || {
        echo "ERROR: python3.12 is required for cn-hk-findata venv" >&2
        exit 1
    }
    echo "Initializing virtual environment using: ${BASE_PYTHON}" >&2
    mkdir -p "$(dirname "${VENV_DIR}")"
    "${BASE_PYTHON}" -m venv "${VENV_DIR}"
fi

if [[ ! -f "${VENV_DIR}/.deps_ok" || "${REQ_FILE}" -nt "${VENV_DIR}/.deps_ok" ]]; then
    echo "Installing dependencies..." >&2
    "${VENV_DIR}/bin/pip" install --upgrade pip --quiet
    "${VENV_DIR}/bin/pip" install -r "${REQ_FILE}" --quiet
    touch "${VENV_DIR}/.deps_ok"
    echo "Environment ready." >&2
fi

exec "${VENV_DIR}/bin/python" "${SCRIPT_FILE}" "$@"
