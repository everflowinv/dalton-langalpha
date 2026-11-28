#!/usr/bin/env bash
# run.sh - Unified launcher & auto-bootstrap wrapper for company-filings-alert
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
VENV_DIR="${RESEARCH_HOME}/venvs/company-filings-alert"
REQ_FILE="${SCRIPT_DIR}/requirements.txt"
REQ_HASH_FILE="${VENV_DIR}/.requirements.sha256"
SCRIPT_FILE="${SCRIPT_DIR}/scripts/company_filings_alert_cli.py"

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
        "${DALTON_FILINGS_LEGACY_PYVENV_CFG:-}" \
        "${RESEARCH_HOME:+${RESEARCH_HOME}/state/venvs/company-filings-alert.python_version}" \
        "${HOME}/.openclaw/workspace/skills/company-filings-alert/venv/pyvenv.cfg"; do # 兼容读取旧 venv Python 版本
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
        local target_mm base_python
        target_mm="$(_read_legacy_pyvenv_mm || true)"
        if [[ -z "${target_mm}" ]]; then
            target_mm="3.12"
        fi
        base_python="$(_find_pinned_python "${target_mm}" || true)"
        if [[ -z "${base_python}" ]]; then
            base_python="$(_find_pinned_python 3.12 || command -v python3 || true)"
        fi
        if [[ -z "${base_python}" ]]; then
            echo "ERROR: python3 is required for company-filings-alert venv" >&2
            exit 1
        fi
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
    exit 1
fi

ensure_env

if [[ "${SKILL_WARNINGS:-default}" == "ignore" ]]; then
    exec "${VENV_DIR}/bin/python" -W ignore "${SCRIPT_FILE}" "$@"
else
    exec "${VENV_DIR}/bin/python" "${SCRIPT_FILE}" "$@"
fi
