#!/usr/bin/env bash
# Launcher for the company-wiki skill (`skills/company-wiki/run.sh`).
# Scrubs ASR/TOS, Feishu, OpenClaw gateway, broker, and Bridge tokens after shell initialization
# and delegates to `bridge_subprocess_env.py exec-nonarchive` before invoking `company_wiki_cli.py`.
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
elif [[ -n "${DALTON_OCW_ROOT:-}" ]] && _resolve_cli_in_dir "${DALTON_OCW_ROOT}/skills/company-wiki" >/dev/null; then
    SKILL_DIR="${DALTON_OCW_ROOT}/skills/company-wiki"
elif _resolve_cli_in_dir "${SCRIPT_DIR}" >/dev/null; then
    SKILL_DIR="${SCRIPT_DIR}"
elif _resolve_cli_in_dir "${PWD}/skills/company-wiki" >/dev/null; then
    SKILL_DIR="${PWD}/skills/company-wiki"
else
    echo "[company-wiki] ERROR: unable to locate company_wiki_cli.py in supported skill directories" >&2
    exit 1
fi

_PRESERVED_GEMINI_API_KEY="${GEMINI_API_KEY:-}"
if [[ -n "${DALTON_WIKI_EXPLICIT_RUN_SH:-}" && -f "${DALTON_WIKI_EXPLICIT_RUN_SH}" && "${DALTON_WIKI_EXPLICIT_RUN_SH}" != "${SCRIPT_DIR}/run.sh" ]]; then
    set -a
    while IFS= read -r _cfg_line || [[ -n "${_cfg_line}" ]]; do
        case "${_cfg_line}" in
            *company_wiki_cli.py*|*wiki_cli.py*|*bridge_subprocess_env.py*|*"exec "*|*"python"*)
                continue
                ;;
            [[:space:]]*\#*|\#*|"")
                continue
                ;;
            *export\ *|[A-Za-z_]*=*|*source\ *|.\ *|*[[:space:]].\ *)
                eval "${_cfg_line}" 2>/dev/null || true
                ;;
        esac
    done < "${DALTON_WIKI_EXPLICIT_RUN_SH}"
    set +a
fi
unset DALTON_WIKI_EXPLICIT_RUN_SH _cfg_line

if [[ -n "${DALTON_SKILL_SHELL_RC:-}" && -f "${DALTON_SKILL_SHELL_RC}" ]]; then
    # shellcheck disable=SC1090
    source "${DALTON_SKILL_SHELL_RC}"
fi
for _env_cand in "${DALTON_OCW_ROOT:-}/.env" "$(dirname "$(dirname "${SKILL_DIR}")")/.env" "${SKILL_DIR}/.env"; do
    if [[ -n "${_env_cand}" && -f "${_env_cand}" ]]; then
        set -a
        # shellcheck disable=SC1090
        source "${_env_cand}"
        set +a
    fi
done
if [[ -n "${_PRESERVED_GEMINI_API_KEY}" ]]; then
    export GEMINI_API_KEY="${_PRESERVED_GEMINI_API_KEY}"
fi
unset _PRESERVED_GEMINI_API_KEY _env_cand

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

CLI_SCRIPT="$(_resolve_cli_in_dir "${SKILL_DIR}")"
if [[ -n "${DALTON_SKILL_PYTHON:-}" && -x "${DALTON_SKILL_PYTHON}" ]]; then
    PYTHON_BIN="${DALTON_SKILL_PYTHON}"
elif [[ -x "${SKILL_DIR}/venv/bin/python" ]]; then
    PYTHON_BIN="${SKILL_DIR}/venv/bin/python"
elif [[ -x "/opt/homebrew/bin/python3" ]]; then
    PYTHON_BIN="/opt/homebrew/bin/python3"
else
    PYTHON_BIN="$(command -v python3)"
fi

FILTER_PY="${DALTON_BRIDGE_ENV_FILTER_PY:-}"
if [[ -z "${FILTER_PY}" || ! -f "${FILTER_PY}" ]]; then
    if [[ -f "${SCRIPT_DIR}/../../../bridge_subprocess_env.py" ]]; then
        FILTER_PY="$(cd "${SCRIPT_DIR}/../../.." && pwd)/bridge_subprocess_env.py"
    fi
fi
unset DALTON_BRIDGE_ENV_FILTER_PY

if [[ -n "${FILTER_PY}" && -f "${FILTER_PY}" ]]; then
    exec "${PYTHON_BIN}" "${FILTER_PY}" exec-nonarchive --allow-env GEMINI_API_KEY -- "${PYTHON_BIN}" "${CLI_SCRIPT}" "$@"
fi
exec "${PYTHON_BIN}" "${CLI_SCRIPT}" "$@"
