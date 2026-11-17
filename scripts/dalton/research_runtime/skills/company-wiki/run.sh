#!/usr/bin/env bash
# run.sh - Unified launcher & auto-bootstrap wrapper for company-wiki (pinned Python 3.14)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

_resolve_cli_in_dir() {
    local dir="$1"
    if [[ -f "${dir}/scripts/company_wiki_cli.py" ]]; then
        printf '%s\n' "${dir}/scripts/company_wiki_cli.py"
        return 0
    fi
    if [[ -f "${dir}/scripts/wiki_cli.py" ]]; then
        printf '%s\n' "${dir}/scripts/wiki_cli.py"
        return 0
    fi
    return 1
}

SKILL_DIR=""
if [[ -n "${DALTON_WIKI_SKILL_DIR:-}" ]]; then
    if ! _resolve_cli_in_dir "${DALTON_WIKI_SKILL_DIR}" >/dev/null; then
        echo "[company-wiki] ERROR: unsupported DALTON_WIKI_SKILL_DIR=${DALTON_WIKI_SKILL_DIR} (missing scripts/company_wiki_cli.py)" >&2
        exit 1
    fi
    SKILL_DIR="${DALTON_WIKI_SKILL_DIR}"
elif [[ -n "${DALTON_SKILL_DIR:-}" ]]; then
    if ! _resolve_cli_in_dir "${DALTON_SKILL_DIR}" >/dev/null; then
        echo "[company-wiki] ERROR: unsupported DALTON_SKILL_DIR=${DALTON_SKILL_DIR} (missing scripts/company_wiki_cli.py)" >&2
        exit 1
    fi
    SKILL_DIR="${DALTON_SKILL_DIR}"
elif [[ -n "${DALTON_RESEARCH_CODE:-}" ]] && _resolve_cli_in_dir "${DALTON_RESEARCH_CODE}/skills/company-wiki" >/dev/null; then
    SKILL_DIR="${DALTON_RESEARCH_CODE}/skills/company-wiki"
elif [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]] && _resolve_cli_in_dir "${DALTON_OCW_ROOT}/skills/company-wiki" >/dev/null; then
    SKILL_DIR="${DALTON_OCW_ROOT}/skills/company-wiki"
elif _resolve_cli_in_dir "${SCRIPT_DIR}" >/dev/null; then
    SKILL_DIR="${SCRIPT_DIR}"
else
    echo "ERROR: CLI script not found under ${SCRIPT_DIR}/scripts" >&2
    exit 1
fi

if [[ -n "${DALTON_OCW_ROOT:-}" && -z "${DALTON_RESEARCH_CODE:-}" && -z "${DALTON_RESEARCH_HOME:-}" ]]; then
    VENV_DIR="${DALTON_OCW_ROOT}/skills/company-wiki/venv"
else
    RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
    VENV_DIR="${RESEARCH_HOME}/venvs/company-wiki"
fi
REQ_FILE="${SKILL_DIR}/requirements.txt"
REQ_HASH_FILE="${VENV_DIR}/.requirements.sha256"
SCRIPT_FILE="$(_resolve_cli_in_dir "${SKILL_DIR}")"

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
            echo "ERROR: python3.14 is required for company-wiki venv" >&2
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

if [[ -n "${DALTON_SKILL_PYTHON:-}" && -x "${DALTON_SKILL_PYTHON}" ]]; then
    PYTHON_BIN="${DALTON_SKILL_PYTHON}"
else
    ensure_env
    PYTHON_BIN="${VENV_DIR}/bin/python"
fi

FILTER_PY="${DALTON_BRIDGE_ENV_FILTER_PY:-}"
if [[ -z "${FILTER_PY}" || ! -f "${FILTER_PY}" ]]; then
    if [[ -f "${SCRIPT_DIR}/../../../bridge_subprocess_env.py" ]]; then
        FILTER_PY="$(cd "${SCRIPT_DIR}/../../.." && pwd)/bridge_subprocess_env.py"
    fi
fi

unset DALTON_WIKI_EXPLICIT_RUN_SH DALTON_MARKET_DIGEST_EXPLICIT_RUN_SH DALTON_BRIDGE_ENV_FILTER_PY
unset BASH_ENV ENV ZDOTDIR PROMPT_COMMAND PYTHONSTARTUP PYTHONINSPECT PYTHONPATH PYTHONHOME PYTHONUSERBASE PYTHONEXECUTABLE CDPATH GLOBIGNORE
unset SEED_ASR_APP_ID SEED_ASR_ACCESS_KEY VOLC_TOS_AK VOLC_TOS_SK VOLC_TOS_BUCKET VOLC_TOS_REGION VOLC_TOS_ENDPOINT
unset DALTON_ASR_STORAGE_ENDPOINT_URL DALTON_ASR_STORAGE_BUCKET_NAME DALTON_ASR_STORAGE_ACCESS_KEY_ID DALTON_ASR_STORAGE_SECRET_ACCESS_KEY DALTON_ASR_STORAGE_REGION DALTON_ASR_STORAGE_ADDRESSING_STYLE DALTON_ASR_STORAGE_KEY_PREFIX
unset FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_DEFAULT_GRANT_CHAT_ID
unset DALTON_FEISHU_APP_ID DALTON_FEISHU_APP_SECRET DALTON_FEISHU_DEFAULT_GRANT_CHAT_ID
unset OPENCLAW_FEISHU_APP_ID OPENCLAW_FEISHU_APP_SECRET LARK_APP_ID LARK_APP_SECRET
unset OPENCLAW_GATEWAY_TOKEN DALTON_OPENCLAW_GATEWAY_TOKEN DALTON_BROKER_TOKEN DALTON_BROKER_TOKEN_PATH DALTON_WEB_SEARCH_BRIDGE_TOKEN
unset DALTON_BRIDGE_ADMIN_TOKEN DALTON_BRIDGE_TOKEN

while IFS='=' read -r _env_name _; do
    case "${_env_name}" in
        SEED_ASR_*|VOLC_TOS_*|DALTON_ASR_STORAGE_*|FEISHU_*|LARK_*|OPENCLAW_FEISHU_*|DALTON_FEISHU_*|BASH_FUNC_*)
            unset "${_env_name}" 2>/dev/null || true
            ;;
    esac
done < <(env)

PY_WARNINGS_FLAG=()
if [[ "${SKILL_WARNINGS:-default}" == "ignore" ]]; then
    PY_WARNINGS_FLAG=(-W ignore)
fi

if [[ -n "${FILTER_PY}" && -f "${FILTER_PY}" ]]; then
    exec "${PYTHON_BIN}" "${FILTER_PY}" exec-nonarchive --allow-env GEMINI_API_KEY -- "${PYTHON_BIN}" ${PY_WARNINGS_FLAG[@]+"${PY_WARNINGS_FLAG[@]}"} "${SCRIPT_FILE}" "$@"
fi
exec "${PYTHON_BIN}" ${PY_WARNINGS_FLAG[@]+"${PY_WARNINGS_FLAG[@]}"} "${SCRIPT_FILE}" "$@"
