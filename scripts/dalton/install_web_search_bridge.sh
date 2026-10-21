#!/usr/bin/env bash
# Install and start the host-side Dalton OpenClaw web search HTTP bridge via launchd.
# Generates ~/.config/dalton/web-search-bridge.env (chmod 600).
# Never passes the bridge token in process arguments and never modifies repo .env;
# the backend container receives DALTON_WEB_SEARCH_BRIDGE_TOKEN from
# ~/.config/dalton/web-search-bridge.env (or environment) via docker-compose.dalton.yml.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_DIR="${HOME}/.config/dalton"
ENV_FILE="${CONFIG_DIR}/web-search-bridge.env"
PLIST_TEMPLATE="${REPO_ROOT}/scripts/dalton/launchd/com.dalton.web-search-bridge.plist.template"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/com.dalton.web-search-bridge.plist"
PYTHON_BIN="/opt/homebrew/bin/python3"
PORT="${DALTON_WEB_SEARCH_BRIDGE_PORT:-8961}"
BROKER_SOCKET="${DALTON_WEB_SEARCH_BROKER_SOCKET:-${HOME}/.openclaw/dalton-web-search-broker.sock}"
BROKER_KEY="${DALTON_WEB_SEARCH_BROKER_KEY:-${HOME}/.openclaw/dalton-web-search-broker.sock.key}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "[install_web_search_bridge] ERROR: Homebrew Python not found at ${PYTHON_BIN}" >&2
    exit 1
fi

mkdir -p "${CONFIG_DIR}"
chmod 700 "${CONFIG_DIR}"

TOKEN=""
if [[ -f "${ENV_FILE}" ]]; then
    TOKEN="$(grep -E '^DALTON_WEB_SEARCH_BRIDGE_TOKEN=' "${ENV_FILE}" | head -n1 | cut -d'=' -f2- | tr -d '[:space:]"'\''' || true)"
fi

if [[ -z "${TOKEN}" ]]; then
    TOKEN="$(openssl rand -hex 32)"
fi

(
    umask 077
    cat > "${ENV_FILE}" <<EOF
DALTON_WEB_SEARCH_BRIDGE_HOST=127.0.0.1
DALTON_WEB_SEARCH_BRIDGE_PORT=${PORT}
DALTON_WEB_SEARCH_BRIDGE_TOKEN=${TOKEN}
DALTON_WEB_SEARCH_BROKER_SOCKET=${BROKER_SOCKET}
DALTON_WEB_SEARCH_BROKER_KEY=${BROKER_KEY}
DALTON_WEB_SEARCH_BROKER_CLIENT_ID=client:dalton-core
DALTON_WEB_SEARCH_BROKER_PROFILE_ID=profile:web-search
DALTON_WEB_SEARCH_BRIDGE_MAX_INFLIGHT=4
DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S=30
EOF
)
chmod 600 "${ENV_FILE}"

mkdir -p "${LAUNCH_AGENTS_DIR}" "${HOME}/Library/Logs"
sed \
    -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
    -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_DEST}"

UID_NUM="$(id -u)"
launchctl bootout "gui/${UID_NUM}/com.dalton.web-search-bridge" 2>/dev/null || true
if ! launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DEST}" 2>/dev/null; then
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
    launchctl load -w "${PLIST_DEST}"
fi
launchctl kickstart -k "gui/${UID_NUM}/com.dalton.web-search-bridge" 2>/dev/null || true

# Wait up to 10s for /healthz
for _ in {1..20}; do
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
        echo "[install_web_search_bridge] web_search_bridge is up on 127.0.0.1:${PORT}"
        echo "[install_web_search_bridge] Token stored in ${ENV_FILE} (0600); container reads DALTON_WEB_SEARCH_BRIDGE_TOKEN via docker-compose.dalton.yml env_file."
        curl -fsS "http://127.0.0.1:${PORT}/healthz"
        echo ""
        exit 0
    fi
    sleep 0.5
done

echo "[install_web_search_bridge] ERROR: bridge did not respond on http://127.0.0.1:${PORT}/healthz within 10s" >&2
exit 1
