#!/usr/bin/env bash
# Install and start the host-side Dalton researchdata HTTP MCP bridge via launchd.
# Incrementally merges defaults into ~/.config/dalton/researchdata-bridge.env (chmod 600)
# and repo .env (chmod 600) via atomic temp-file replacement without rotating existing
# tokens, overwriting Feishu credentials, or printing secrets.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${DALTON_BRIDGE_ENV_FILE:-${HOME}/.config/dalton/researchdata-bridge.env}"
REPO_ENV="${DALTON_REPO_ENV_FILE:-${REPO_ROOT}/.env}"
PLIST_TEMPLATE="${DALTON_PLIST_TEMPLATE:-${REPO_ROOT}/scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template}"
LAUNCH_AGENTS_DIR="${DALTON_LAUNCH_AGENTS_DIR:-${HOME}/Library/LaunchAgents}"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/com.dalton.researchdata-bridge.plist"
LOGS_DIR="${DALTON_BRIDGE_LOGS_DIR:-${HOME}/Library/Logs}"
OCW_ROOT="${DALTON_OCW_ROOT:-${HOME}/.openclaw/workspace}"
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
HELPER_PY="${REPO_ROOT}/scripts/dalton/bridge_subprocess_env.py"

EFFECTIVE_PORT="$("${HELPER_PYTHON}" "${HELPER_PY}" merge-bridge-env \
    --bridge-env "${ENV_FILE}" \
    --repo-env "${REPO_ENV}" \
    --default-port "${PORT}" \
    --default-ocw-root "${OCW_ROOT}" \
    --default-python "${PYTHON_BIN}" \
    --print-port-only)"

"${HELPER_PYTHON}" "${HELPER_PY}" render-plist \
    --template "${PLIST_TEMPLATE}" \
    --dest "${PLIST_DEST}" \
    --bridge-env "${ENV_FILE}" \
    --repo-root "${REPO_ROOT}" \
    --home "${HOME}" \
    --logs-dir "${LOGS_DIR}" >/dev/null

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
