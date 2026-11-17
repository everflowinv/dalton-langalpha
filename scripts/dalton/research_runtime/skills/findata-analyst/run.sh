#!/usr/bin/env bash
# run.sh - Auto-bootstrapping wrapper for findata-analyst (pinned Python 3.12)
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="${SKILL_DIR}/requirements.txt"
SCRIPT_FILE="${SKILL_DIR}/scripts/findata_cli.py"

if [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_CODE:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]]; then
    VENV_DIR="${DALTON_OCW_ROOT}/skills/findata-analyst/venv"
else
    RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
    VENV_DIR="${RESEARCH_HOME}/venvs/findata-analyst"
fi

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
        echo "ERROR: python3.12 is required for findata-analyst venv" >&2
        exit 1
    }
    echo "Initializing virtual environment using: ${BASE_PYTHON}" >&2
    mkdir -p "$(dirname "${VENV_DIR}")"
    "${BASE_PYTHON}" -m venv "${VENV_DIR}"

    echo "Installing dependencies..." >&2
    "${VENV_DIR}/bin/pip" install --upgrade pip --quiet
    "${VENV_DIR}/bin/pip" install -r "${REQ_FILE}" --quiet
    echo "Environment ready." >&2
fi

# 已有环境只同步 SEC 解析器的固定版本，保留可选 Yahoo 回退的现有安装状态。
EDGAR_PIN="$(sed -n 's/^edgartools==//p' "${REQ_FILE}")"
EDGAR_INSTALLED="$("${VENV_DIR}/bin/python" -c 'from importlib.metadata import version; print(version("edgartools"))')"
if [[ "${EDGAR_INSTALLED}" != "${EDGAR_PIN}" ]]; then
    "${VENV_DIR}/bin/pip" install "edgartools==${EDGAR_PIN}" --quiet >&2
fi

exec "${VENV_DIR}/bin/python" -W ignore "${SCRIPT_FILE}" "$@"
