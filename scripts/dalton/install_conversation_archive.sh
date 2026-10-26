#!/usr/bin/env bash
# Install the host-side Dalton daily conversation archive launchd job (21:15).
# Renders scripts/dalton/launchd/com.dalton.conversation-archive.plist.template
# into ~/Library/LaunchAgents/com.dalton.conversation-archive.plist and initializes
# the bridge-managed state file (~/.config/dalton/conversation-archive-state.json).
# Refuses to overwrite an existing plist unless --force is explicitly passed.
set -euo pipefail

FORCE=0
for arg in "$@"; do
    case "${arg}" in
        --force)
            FORCE=1
            ;;
        -h|--help)
            echo "Usage: $(basename "$0") [--force]"
            exit 0
            ;;
        *)
            echo "[install_conversation_archive] Unknown argument: ${arg} (expected --force)" >&2
            exit 2
            ;;
    esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_DIR="${HOME}/.config/dalton"
CACHE_DIR="${HOME}/.cache/dalton"
STATE_FILE="${CONFIG_DIR}/conversation-archive-state.json"
PLIST_TEMPLATE="${REPO_ROOT}/scripts/dalton/launchd/com.dalton.conversation-archive.plist.template"
LAUNCH_AGENTS_DIR="${HOME}/Library/LaunchAgents"
PLIST_DEST="${DALTON_ARCHIVE_PLIST_DEST:-${LAUNCH_AGENTS_DIR}/com.dalton.conversation-archive.plist}"
ARCHIVE_SH="${REPO_ROOT}/scripts/dalton/launchd/archive_daily.sh"

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
elif [[ -x "${DALTON_OCW_PYTHON:-/opt/homebrew/bin/python3}" ]]; then
    PYTHON_BIN="${DALTON_OCW_PYTHON:-/opt/homebrew/bin/python3}"
else
    PYTHON_BIN="$(command -v python3)"
fi

[[ -x "${ARCHIVE_SH}" ]] || chmod +x "${ARCHIVE_SH}" 2>/dev/null || true
mkdir -p "${CONFIG_DIR}" "${CACHE_DIR}" "$(dirname "${PLIST_DEST}")" "${HOME}/Library/Logs"
chmod 700 "${CONFIG_DIR}"

if [[ ! -f "${STATE_FILE}" ]]; then
    cat > "${STATE_FILE}" <<EOF
{
  "enabled": true
}
EOF
    chmod 600 "${STATE_FILE}"
fi

TMP_PLIST="$(mktemp "$(dirname "${PLIST_DEST}")/.com.dalton.conversation-archive.plist.XXXXXX")"
cleanup_tmp() {
    rm -f "${TMP_PLIST}" 2>/dev/null || true
}
trap cleanup_tmp EXIT

sed \
    -e "s|__PYTHON_BIN__|${PYTHON_BIN}|g" \
    -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__HOME__|${HOME}|g" \
    "${PLIST_TEMPLATE}" > "${TMP_PLIST}"

# launchd jobs do not inherit the login shell's environment. On a host whose outbound
# traffic goes through a local proxy, the archive's model calls need it, so carry the
# installing shell's proxy settings into the job.
for proxy_var in http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY; do
    proxy_val="${!proxy_var:-}"
    if [[ -n "${proxy_val}" ]]; then
        plutil -replace "EnvironmentVariables.${proxy_var}" -string "${proxy_val}" "${TMP_PLIST}"
    fi
done

if [[ -f "${PLIST_DEST}" ]] && [[ "${FORCE}" != "1" ]]; then
    echo "[install_conversation_archive] Target plist already exists: ${PLIST_DEST}" >&2
    diff -u "${PLIST_DEST}" "${TMP_PLIST}" || true
    echo "[install_conversation_archive] Refusing to overwrite existing plist without --force." >&2
    exit 1
fi

mv -f "${TMP_PLIST}" "${PLIST_DEST}"

if [[ "${DALTON_SKIP_LAUNCHCTL:-0}" != "1" ]]; then
    UID_NUM="$(id -u)"
    launchctl bootout "gui/${UID_NUM}/com.dalton.conversation-archive" 2>/dev/null || true
    if ! launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DEST}" 2>/dev/null; then
        launchctl unload "${PLIST_DEST}" 2>/dev/null || true
        launchctl load -w "${PLIST_DEST}"
    fi
fi

echo "[install_conversation_archive] Installed ${PLIST_DEST} (daily 21:15, state file ${STATE_FILE})."
