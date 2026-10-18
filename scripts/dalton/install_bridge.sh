#!/usr/bin/env bash
# Install and start the host-side Dalton researchdata HTTP MCP bridge via launchd.
# Generates ~/.config/dalton/researchdata-bridge.env (chmod 600) and syncs
# DALTON_RESEARCHDATA_URL / DALTON_BRIDGE_TOKEN into repo .env without printing secrets.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_DIR="${HOME}/.config/dalton"
ENV_FILE="${CONFIG_DIR}/researchdata-bridge.env"
PLIST_TEMPLATE="${REPO_ROOT}/scripts/dalton/launchd/com.dalton.researchdata-bridge.plist.template"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${LAUNCH_AGENTS_DIR}/com.dalton.researchdata-bridge.plist"
OCW_ROOT="${DALTON_OCW_ROOT:-${HOME}/.openclaw/workspace}"
PYTHON_BIN="${DALTON_OCW_PYTHON:-/opt/homebrew/bin/python3}"
PORT="${DALTON_BRIDGE_PORT:-8960}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi

mkdir -p "${CONFIG_DIR}"
chmod 700 "${CONFIG_DIR}"

TOKEN=""
if [[ -f "${ENV_FILE}" ]]; then
    TOKEN="$(grep -E '^DALTON_BRIDGE_TOKEN=' "${ENV_FILE}" | head -n1 | cut -d'=' -f2- | tr -d '[:space:]"'\''' || true)"
fi

if [[ -z "${TOKEN}" ]]; then
    TOKEN="$(openssl rand -hex 32)"
fi

cat > "${ENV_FILE}" <<EOF
DALTON_BRIDGE_HOST=127.0.0.1
DALTON_BRIDGE_PORT=${PORT}
DALTON_BRIDGE_TOKEN=${TOKEN}
DALTON_OCW_ROOT=${OCW_ROOT}
DALTON_OCW_PYTHON=${PYTHON_BIN}
DALTON_BRIDGE_MAX_INFLIGHT=2
DALTON_BRIDGE_CALLER_BUDGET_S=625
EOF
chmod 600 "${ENV_FILE}"

# Update repo .env without echoing token
REPO_ENV="${REPO_ROOT}/.env"
touch "${REPO_ENV}"
"${PYTHON_BIN}" - "${REPO_ENV}" "${TOKEN}" "${PORT}" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1])
token = sys.argv[2]
port = sys.argv[3]
bridge_url = f"http://host.docker.internal:{port}/mcp/{token}"

lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
updated = {}
out_lines = []
for line in lines:
    stripped = line.strip()
    if stripped.startswith("DALTON_BRIDGE_TOKEN="):
        out_lines.append(f"DALTON_BRIDGE_TOKEN={token}")
        updated["DALTON_BRIDGE_TOKEN"] = True
    elif stripped.startswith("DALTON_RESEARCHDATA_URL="):
        out_lines.append(f"DALTON_RESEARCHDATA_URL={bridge_url}")
        updated["DALTON_RESEARCHDATA_URL"] = True
    elif stripped.startswith("SEC_USER_AGENT="):
        out_lines.append(line)
        updated["SEC_USER_AGENT"] = True
    else:
        out_lines.append(line)

if "DALTON_BRIDGE_TOKEN" not in updated:
    out_lines.append(f"DALTON_BRIDGE_TOKEN={token}")
if "DALTON_RESEARCHDATA_URL" not in updated:
    out_lines.append(f"DALTON_RESEARCHDATA_URL={bridge_url}")
if "SEC_USER_AGENT" not in updated:
    out_lines.append("SEC_USER_AGENT=DaltonResearch research@dalton.local")

env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
PY

mkdir -p "${LAUNCH_AGENTS_DIR}" "${HOME}/Library/Logs"
sed \
    -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
    -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__OCW_ROOT__|${OCW_ROOT}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${PLIST_TEMPLATE}" > "${PLIST_DEST}"

UID_NUM="$(id -u)"
launchctl bootout "gui/${UID_NUM}/com.dalton.researchdata-bridge" 2>/dev/null || true
if ! launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DEST}" 2>/dev/null; then
    launchctl unload "${PLIST_DEST}" 2>/dev/null || true
    launchctl load -w "${PLIST_DEST}"
fi
launchctl kickstart -k "gui/${UID_NUM}/com.dalton.researchdata-bridge" 2>/dev/null || true

# Wait up to 10s for /healthz
for _ in {1..20}; do
    if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
        echo "[install_bridge] researchdata_bridge is up on 127.0.0.1:${PORT}"
        curl -fsS "http://127.0.0.1:${PORT}/healthz"
        echo ""
        exit 0
    fi
    sleep 0.5
done

echo "[install_bridge] ERROR: bridge did not respond on http://127.0.0.1:${PORT}/healthz within 10s" >&2
exit 1
