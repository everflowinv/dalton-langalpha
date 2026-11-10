#!/usr/bin/env bash
# Launcher for the company-wiki skill (`skills/company-wiki/run.sh`).
# Scrubs ASR/TOS, Feishu, OpenClaw gateway, broker, and Bridge tokens after shell initialization
# and delegates to `bridge_subprocess_env.py exec-nonarchive` before invoking `company_wiki_cli.py`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -n "${DALTON_SKILL_SHELL_RC:-}" && -f "${DALTON_SKILL_SHELL_RC}" ]]; then
    # shellcheck disable=SC1090
    source "${DALTON_SKILL_SHELL_RC}"
fi

unset BASH_ENV ENV
unset SEED_ASR_APP_ID SEED_ASR_ACCESS_KEY VOLC_TOS_AK VOLC_TOS_SK VOLC_TOS_BUCKET VOLC_TOS_REGION VOLC_TOS_ENDPOINT
unset DALTON_ASR_STORAGE_ENDPOINT_URL DALTON_ASR_STORAGE_BUCKET_NAME DALTON_ASR_STORAGE_ACCESS_KEY_ID DALTON_ASR_STORAGE_SECRET_ACCESS_KEY DALTON_ASR_STORAGE_REGION DALTON_ASR_STORAGE_ADDRESSING_STYLE DALTON_ASR_STORAGE_KEY_PREFIX
unset FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_DEFAULT_GRANT_CHAT_ID
unset DALTON_FEISHU_APP_ID DALTON_FEISHU_APP_SECRET DALTON_FEISHU_DEFAULT_GRANT_CHAT_ID
unset OPENCLAW_FEISHU_APP_ID OPENCLAW_FEISHU_APP_SECRET LARK_APP_ID LARK_APP_SECRET
unset OPENCLAW_GATEWAY_TOKEN DALTON_OPENCLAW_GATEWAY_TOKEN DALTON_BROKER_TOKEN DALTON_BROKER_TOKEN_PATH DALTON_WEB_SEARCH_BRIDGE_TOKEN
unset DALTON_BRIDGE_ADMIN_TOKEN DALTON_BRIDGE_TOKEN

while IFS='=' read -r _env_name _; do
    case "${_env_name}" in
        SEED_ASR_*|VOLC_TOS_*|DALTON_ASR_STORAGE_*|FEISHU_*|LARK_*|OPENCLAW_FEISHU_*|DALTON_FEISHU_*)
            unset "${_env_name}" 2>/dev/null || true
            ;;
    esac
done < <(env)

SKILL_DIR="${SCRIPT_DIR}"
if [[ ! -f "${SKILL_DIR}/scripts/company_wiki_cli.py" ]]; then
    if [[ -n "${DALTON_OCW_ROOT:-}" && -f "${DALTON_OCW_ROOT}/skills/company-wiki/scripts/company_wiki_cli.py" ]]; then
        SKILL_DIR="${DALTON_OCW_ROOT}/skills/company-wiki"
    elif [[ -f "${PWD}/skills/company-wiki/scripts/company_wiki_cli.py" ]]; then
        SKILL_DIR="${PWD}/skills/company-wiki"
    fi
fi

CLI_SCRIPT="${SKILL_DIR}/scripts/company_wiki_cli.py"
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
    exec "${PYTHON_BIN}" "${FILTER_PY}" exec-nonarchive -- "${PYTHON_BIN}" "${CLI_SCRIPT}" "$@"
fi
exec "${PYTHON_BIN}" "${CLI_SCRIPT}" "$@"
