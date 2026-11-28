#!/usr/bin/env bash
# Install and start the host-side Dalton researchdata HTTP MCP bridge via launchd.
# Incrementally merges defaults into ~/.config/dalton/researchdata-bridge.env (chmod 600)
# and repo .env (chmod 600) via atomic temp-file replacement without rotating existing
# tokens, overwriting Feishu credentials, or printing secrets.
set -euo pipefail

RESEARCH_RUNTIME_MODE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --research-runtime=*)
            RESEARCH_RUNTIME_MODE="${1#--research-runtime=}"
            shift
            ;;
        --research-runtime)
            RESEARCH_RUNTIME_MODE="${2:?missing --research-runtime value}"
            shift 2
            ;;
        -h|--help)
            echo "Usage: install_bridge.sh [--research-runtime=auto|new]"
            exit 0
            ;;
        *)
            echo "[install_bridge] ERROR: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ -n "${RESEARCH_RUNTIME_MODE}" && "${RESEARCH_RUNTIME_MODE}" != "auto" && "${RESEARCH_RUNTIME_MODE}" != "new" ]]; then
    echo "[install_bridge] ERROR: invalid --research-runtime=${RESEARCH_RUNTIME_MODE} (expected auto|new)" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
ENV_FILE="${DALTON_BRIDGE_ENV_FILE:-${HOME}/.config/dalton/researchdata-bridge.env}"
REPO_ENV="${DALTON_REPO_ENV_FILE:-${REPO_ROOT}/.env}"
OWNERSHIP_FILE="${DALTON_GUIDEPOINT_OWNERSHIP_FILE:-${REPO_ROOT}/var/dalton/cutover-snapshots/research-host/handoff-journal.json}"
PLIST_TEMPLATE="${DALTON_PLIST_TEMPLATE:-${REPO_ROOT}/scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template}"
LAUNCH_AGENTS_DIR="${DALTON_LAUNCH_AGENTS_DIR:-${HOME}/Library/LaunchAgents}"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/com.dalton.researchdata-bridge.plist"
LOGS_DIR="${DALTON_BRIDGE_LOGS_DIR:-${HOME}/Library/Logs}"
RESEARCH_CODE="${DALTON_RESEARCH_CODE:-${REPO_ROOT}/scripts/dalton/research_runtime}"
RESEARCH_HOME="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
RESEARCH_RUNTIME_MODE="${RESEARCH_RUNTIME_MODE:-new}"
DEFAULT_PYTHON_BIN="/opt/homebrew/bin/python3"
if [[ ! -x "${DEFAULT_PYTHON_BIN}" ]]; then
    DEFAULT_PYTHON_BIN="$(command -v python3 || true)"
fi
PYTHON_BIN="${DALTON_OCW_PYTHON:-${DEFAULT_PYTHON_BIN}}"
HELPER_PYTHON="${DEFAULT_PYTHON_BIN:-$(command -v python3)}"
PORT="${DALTON_BRIDGE_PORT:-8960}"
LAUNCHCTL_CMD="${LAUNCHCTL_BIN:-launchctl}"
CURL_CMD="${CURL_BIN:-curl}"
HEALTH_RETRIES="${DALTON_BRIDGE_HEALTH_RETRIES:-20}"
HEALTH_SLEEP_S="${DALTON_BRIDGE_HEALTH_SLEEP_S:-0.5}"
HELPER_PY="${SCRIPT_DIR}/bridge_subprocess_env.py"

# Fail before writing any env files when repository venv Python is missing.
REPO_VENV_PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${REPO_VENV_PYTHON}" ]]; then
    echo "[install_bridge] ERROR: repository virtualenv Python not found or not executable: ${REPO_VENV_PYTHON} (run 'uv sync --frozen' first)" >&2
    exit 1
fi

EFFECTIVE_PORT="$("${HELPER_PYTHON}" "${HELPER_PY}" merge-bridge-env \
    --bridge-env "${ENV_FILE}" \
    --repo-env "${REPO_ENV}" \
    --default-port "${PORT}" \
    --default-research-code "${RESEARCH_CODE}" \
    --default-research-home "${RESEARCH_HOME}" \
    --default-python "${PYTHON_BIN}" \
    --existing-plist "${PLIST_DEST}" \
    --research-runtime "${RESEARCH_RUNTIME_MODE}" \
    --print-port-only)"

mkdir -p "${LOGS_DIR}"
"${HELPER_PYTHON}" "${HELPER_PY}" render-plist \
    --template "${PLIST_TEMPLATE}" \
    --dest "${PLIST_DEST}" \
    --bridge-env "${ENV_FILE}" \
    --repo-root "${REPO_ROOT}" \
    --repo-env "${REPO_ENV}" \
    --ownership-file "${OWNERSHIP_FILE}" \
    --home "${HOME}" \
    --logs-dir "${LOGS_DIR}" \
    --research-runtime "${RESEARCH_RUNTIME_MODE}" >/dev/null

UID_NUM="$(id -u)"
"${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/com.dalton.researchdata-bridge" 2>/dev/null || true
if ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${PLIST_DEST}" 2>/dev/null; then
    "${LAUNCHCTL_CMD}" unload "${PLIST_DEST}" 2>/dev/null || true
    "${LAUNCHCTL_CMD}" load -w "${PLIST_DEST}"
fi
"${LAUNCHCTL_CMD}" kickstart -k "gui/${UID_NUM}/com.dalton.researchdata-bridge" 2>/dev/null || true

# Wait for /healthz
i=1
while [[ "${i}" -le "${HEALTH_RETRIES}" ]]; do
    if "${CURL_CMD}" -fsS "http://127.0.0.1:${EFFECTIVE_PORT}/healthz" >/dev/null 2>&1; then
        echo "[install_bridge] researchdata_bridge is up on 127.0.0.1:${EFFECTIVE_PORT}"
        "${CURL_CMD}" -fsS "http://127.0.0.1:${EFFECTIVE_PORT}/healthz"
        echo ""
        exit 0
    fi
    sleep "${HEALTH_SLEEP_S}"
    i=$((i + 1))
done

echo "[install_bridge] ERROR: bridge did not respond on http://127.0.0.1:${EFFECTIVE_PORT}/healthz" >&2
exit 1
