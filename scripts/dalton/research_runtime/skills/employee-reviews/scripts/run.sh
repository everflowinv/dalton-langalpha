#!/usr/bin/env bash
# run.sh - Auto-bootstrap wrapper for employee-reviews (pinned Python 3.14)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_CODE:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]]; then
    VENV_DIR="${DALTON_OCW_ROOT}/skills/employee-reviews/venv"
else
    RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
    VENV_DIR="${RESEARCH_HOME}/venvs/employee-reviews"
fi
REQ_FILE="${SCRIPT_DIR}/requirements.txt"
REQ_HASH_FILE="${VENV_DIR}/.requirements.sha256"
SCRIPT_FILE="${SCRIPT_DIR}/employee_reviews_cli.py"

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
    if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
        local base_python
        base_python="$(_find_pinned_python 3.14)" || {
            echo "ERROR: python3.14 is required for employee-reviews venv" >&2
            exit 1
        }
        echo "[bootstrap] Initializing virtualenv using: ${base_python}" >&2
        mkdir -p "$(dirname "${VENV_DIR}")"
        "${base_python}" -m venv "${VENV_DIR}"
        "${VENV_DIR}/bin/pip" install --upgrade pip --quiet
    fi

    if [[ -f "${REQ_FILE}" ]]; then
        local current_hash old_hash
        current_hash="$(hash_file "${REQ_FILE}")"
        old_hash=""
        [[ -f "${REQ_HASH_FILE}" ]] && old_hash="$(cat "${REQ_HASH_FILE}")"

        if [[ "${current_hash}" != "${old_hash}" ]]; then
            echo "[bootstrap] Installing/updating dependencies..." >&2
            "${VENV_DIR}/bin/pip" install -r "${REQ_FILE}" --quiet
            echo "${current_hash}" > "${REQ_HASH_FILE}"
        fi
    fi
}

if [[ ! -f "${SCRIPT_FILE}" ]]; then
    echo "ERROR: CLI script not found: ${SCRIPT_FILE}" >&2
    echo "Hint: 期望路径 skills/employee-reviews/scripts/employee_reviews_cli.py" >&2
    exit 1
fi

ensure_env

PY_WARNINGS_FLAG=()
if [[ "${SKILL_WARNINGS:-default}" == "ignore" ]]; then
    PY_WARNINGS_FLAG=(-W ignore)
fi

exec "${VENV_DIR}/bin/python" ${PY_WARNINGS_FLAG[@]+"${PY_WARNINGS_FLAG[@]}"} "${SCRIPT_FILE}" "$@"
