#!/usr/bin/env bash
# Shared logging, env loading, key management, and alert helpers for Dalton offsite backups.
# Compatible with macOS Bash 3.2 under `set -euo pipefail`.

set -euo pipefail
set -E
umask 077

BACKUP_ALERT_EMITTED=0
BACKUP_COMPLETED_OK=0
BACKUP_CLEANUP_FN=""
_DALTON_TRAP_INSTALLED=0
_DALTON_LAST_ERR_LINE=""
_DALTON_LAST_ERR_CMD=""

export PATH="${PATH:+${PATH}:}/opt/homebrew/bin:/usr/local/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin"

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_DEFAULT="$(cd "${OPS_DIR}/../../.." && pwd)"
BACKUP_LIB="${OPS_DIR}/backup_lib.py"

resolve_python_bin() {
    if [[ -n "${DALTON_REPO_PYTHON:-}" ]] && [[ -x "${DALTON_REPO_PYTHON}" ]]; then
        printf '%s\n' "${DALTON_REPO_PYTHON}"
    elif [[ -x "${REPO_ROOT_DEFAULT}/.venv/bin/python" ]]; then
        printf '%s\n' "${REPO_ROOT_DEFAULT}/.venv/bin/python"
    elif [[ -x "/opt/homebrew/bin/python3" ]]; then
        printf '%s\n' "/opt/homebrew/bin/python3"
    else
        command -v python3 || echo python3
    fi
}

init_default_alert_paths() {
    DALTON_OFFSITE_BACKUP_LOG_FILE="${DALTON_OFFSITE_BACKUP_LOG_FILE:-${HOME}/Library/Logs/dalton-offsite-backup.log}"
    DALTON_BACKUP_ALERTS_FILE="${DALTON_BACKUP_ALERTS_FILE:-}"
    DALTON_CHANNEL_GATEWAY_ENV_FILE="${DALTON_CHANNEL_GATEWAY_ENV_FILE:-${HOME}/.config/dalton/channel-gateway.env}"
    DALTON_CHANNEL_GATEWAY_URL="${DALTON_CHANNEL_GATEWAY_URL:-http://127.0.0.1:8962}"
    INTERNAL_SERVICE_TOKEN="${INTERNAL_SERVICE_TOKEN:-}"
    DALTON_BACKUP_KEY_FILE="${DALTON_BACKUP_KEY_FILE:-${HOME}/.config/dalton/backup.key}"
}

init_default_alert_paths

load_offsite_backup_env() {
    init_default_alert_paths
    local env_file="${DALTON_OFFSITE_BACKUP_ENV_FILE:-${HOME}/.config/dalton/offsite-backup.env}"
    if [[ -L "${env_file}" ]]; then
        fail_with_alert "INVALID_ENV_FILE" "offsite-backup env file must not be a symlink: ${env_file}"
    fi
    if [[ -e "${env_file}" ]] && [[ ! -f "${env_file}" ]]; then
        fail_with_alert "INVALID_ENV_FILE" "offsite-backup env path is not a regular file: ${env_file}"
    fi
    if [[ -f "${env_file}" ]]; then
        if [[ ! -r "${env_file}" ]]; then
            fail_with_alert "ENV_LOAD_FAILED" "offsite-backup env file is not readable: ${env_file}"
        fi
        if ! bash -n "${env_file}" >/dev/null 2>&1; then
            fail_with_alert "ENV_SYNTAX_ERROR" "offsite-backup env file has shell syntax error: ${env_file}"
        fi
        set -a
        # shellcheck disable=SC1090
        source "${env_file}" || fail_with_alert "ENV_LOAD_FAILED" "Failed to load offsite-backup env file: ${env_file}"
        set +a
    fi
    init_default_alert_paths
}

# B8b: Google Drive uploads bypass proxy env vars because proxy bandwidth quota is limited
# and direct connectivity to googleapis.com works on this host.
rclone_noproxy() {
    env \
        -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
        -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY \
        rclone "$@"
}

log_msg() {
    local level="$1"
    shift
    local msg="$*"
    local log_file="${DALTON_OFFSITE_BACKUP_LOG_FILE:-${HOME}/Library/Logs/dalton-offsite-backup.log}"
    local line
    line="$(date '+%Y-%m-%d %H:%M:%S %z') [${level}] [${SCRIPT_NAME:-dalton-backup}] ${msg}"
    if [[ -n "${log_file}" ]]; then
        mkdir -p "$(dirname "${log_file}")" 2>/dev/null || true
        printf '%s\n' "${line}" >> "${log_file}" 2>/dev/null || true
    fi
    printf '%s\n' "${line}" >&2 || true
}

emit_alert() {
    local level="$1"
    local code="$2"
    shift 2
    local message="$*"
    local py_bin
    py_bin="$(resolve_python_bin)"

    # Send failure notification via dalton-channel-gateway first (best-effort; never alters caller exit flow or prints token).
    if [[ "${level}" == "ERROR" ]] && [[ -n "${INTERNAL_SERVICE_TOKEN:-}" ]] && [[ -n "${DALTON_CHANNEL_GATEWAY_URL:-}" ]] && command -v curl >/dev/null 2>&1; then
        local gw_payload=""
        gw_payload="$("${py_bin}" "${BACKUP_LIB}" gateway-payload \
            --source "${SCRIPT_NAME:-dalton-backup}" \
            --code "${code}" \
            --message "${message}" 2>/dev/null || true)"
        if [[ -n "${gw_payload}" ]]; then
            local gw_url="${DALTON_CHANNEL_GATEWAY_URL%/}/internal/ops-alerts"
            printf 'header = "X-Service-Token: %s"\n' "${INTERNAL_SERVICE_TOKEN}" \
                | curl -sS -f --noproxy '*' --max-time 5 -X POST \
                    -H "Content-Type: application/json" \
                    -K - \
                    -d "${gw_payload}" \
                    "${gw_url}" >/dev/null 2>&1 || true
        fi
    fi

    # Write local alerts file before main log write so log I/O errors cannot block alerts.
    local alert_out=""
    alert_out="$("${py_bin}" "${BACKUP_LIB}" emit-alert \
        --level "${level}" \
        --source "${SCRIPT_NAME:-dalton-backup}" \
        --code "${code}" \
        --message "${message}" \
        --backup-alerts-file "${DALTON_BACKUP_ALERTS_FILE:-}" \
        --ops-alerts-file "${DALTON_OPS_ALERTS_FILE:-}" \
        --gateway-env-file "${DALTON_CHANNEL_GATEWAY_ENV_FILE:-}" \
        --default-file "${HOME}/Library/Logs/dalton-offsite-backup-alerts.log" 2>/dev/null)" || true

    if [[ -n "${alert_out}" ]]; then
        log_msg "${level}" "ALERT(${code}) -> ${alert_out}: ${message}" || true
    else
        log_msg "${level}" "ALERT(${code}): ${message}" || true
    fi
}

fail_with_alert() {
    local code="$1"
    shift
    local msg="$*"
    BACKUP_ALERT_EMITTED=1
    emit_alert "ERROR" "${code}" "${msg}" || true
    log_msg "ERROR" "${msg}" || true
    exit 1
}

_dalton_on_err() {
    _DALTON_LAST_ERR_LINE="${1:-}"
    _DALTON_LAST_ERR_CMD="${2:-}"
}

_dalton_on_exit() {
    local rc=$?
    set +e
    if [[ "${rc}" -eq 0 ]] && [[ "${BACKUP_COMPLETED_OK:-0}" != "1" ]]; then
        # Bash 3.2 enters EXIT trap with $?=0 when aborted by `set -u` unbound variable.
        rc=1
    fi
    if [[ "${rc}" -ne 0 ]] && [[ "${BACKUP_ALERT_EMITTED:-0}" != "1" ]]; then
        BACKUP_ALERT_EMITTED=1
        local detail="Unexpected failure (exit=${rc})"
        if [[ -n "${_DALTON_LAST_ERR_LINE:-}" ]]; then
            detail="Unexpected failure at line ${_DALTON_LAST_ERR_LINE} (${_DALTON_LAST_ERR_CMD:-unknown}, exit=${rc})"
        fi
        emit_alert "ERROR" "UNEXPECTED_FAILURE" "${detail}" || true
        log_msg "ERROR" "${detail}" || true
    fi
    if [[ -n "${BACKUP_CLEANUP_FN:-}" ]] && declare -F "${BACKUP_CLEANUP_FN}" >/dev/null 2>&1; then
        "${BACKUP_CLEANUP_FN}" "${rc}" || true
    fi
    exit "${rc}"
}

install_failure_trap() {
    BACKUP_CLEANUP_FN="${1:-}"
    _DALTON_TRAP_INSTALLED=1
    trap '_dalton_on_err "${LINENO}" "${BASH_COMMAND:-unknown}"' ERR
    trap _dalton_on_exit EXIT
}

suspend_err_trap() {
    trap - ERR
}

restore_err_trap() {
    if [[ "${_DALTON_TRAP_INSTALLED:-0}" == "1" ]]; then
        trap '_dalton_on_err "${LINENO}" "${BASH_COMMAND:-unknown}"' ERR
    fi
}

finish_backup() {
    BACKUP_COMPLETED_OK=1
    exit 0
}

ensure_backup_key() {
    local key_file="${DALTON_BACKUP_KEY_FILE:-${HOME}/.config/dalton/backup.key}"
    if [[ -L "${key_file}" ]]; then
        BACKUP_ALERT_EMITTED=1
        emit_alert "ERROR" "INVALID_KEY_FILE" "Backup key path is a symlink: ${key_file}" || true
        log_msg "ERROR" "Backup key path must not be a symlink: ${key_file}" || true
        return 1
    fi
    if [[ ! -f "${key_file}" ]]; then
        local key_dir
        key_dir="$(dirname "${key_file}")"
        mkdir -p "${key_dir}"
        chmod 700 "${key_dir}" 2>/dev/null || true
        local tmp_key="${key_file}.tmp.$$"
        (
            umask 077
            openssl rand -base64 32 > "${tmp_key}"
        )
        chmod 600 "${tmp_key}"
        mv "${tmp_key}" "${key_file}"
        log_msg "WARN" "NEW_KEY generated at ${key_file} — copy it to your password manager immediately; encrypted backups cannot be recovered without it"
    fi
    chmod 600 "${key_file}" 2>/dev/null || true
    if [[ ! -s "${key_file}" ]]; then
        BACKUP_ALERT_EMITTED=1
        emit_alert "ERROR" "EMPTY_KEY_FILE" "Backup key file is empty: ${key_file}" || true
        log_msg "ERROR" "Backup key file is empty: ${key_file}" || true
        return 1
    fi
    return 0
}
