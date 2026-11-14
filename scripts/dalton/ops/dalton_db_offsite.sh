#!/usr/bin/env bash
# B3b: Daily Postgres offsite upload -> streaming openssl enc -> rclone rcat (.partial -> moveto)
# to Google Drive (<remote>:DaltonBackup/db/<date>/, default 7 copies retained).
# Never creates local copies on internal or external disk and never falls back to internal /tmp.
# B8b: rclone calls unset all proxy env vars because proxy bandwidth quota is limited and googleapis.com is directly reachable.

SCRIPT_NAME="dalton_db_offsite"
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dalton/ops/_common.sh
source "${OPS_DIR}/_common.sh"

RUN_TMP_DIR=""
CURRENT_PARTIAL_OBJ=""
RCLONE_LOG_FILE=""
RCLONE_CACHE_DIR=""
EXT_TMP_ROOT=""

delete_partial_remote() {
    local partial_obj="$1"
    [[ -n "${partial_obj}" ]] || return 0
    local -a extra_args=()
    if [[ -n "${RCLONE_CACHE_DIR:-}" ]]; then
        extra_args+=(--cache-dir "${RCLONE_CACHE_DIR}")
    fi
    if [[ -n "${RUN_TMP_DIR:-${EXT_TMP_ROOT:-}}" ]]; then
        extra_args+=(--temp-dir "${RUN_TMP_DIR:-${EXT_TMP_ROOT:-}}")
    fi
    if [[ -n "${RCLONE_LOG_FILE:-}" ]]; then
        extra_args+=(--log-file "${RCLONE_LOG_FILE}")
    fi
    if ! rclone_noproxy deletefile "${partial_obj}" \
        ${extra_args[@]+"${extra_args[@]}"} >/dev/null 2>&1; then
        log_msg "WARN" "Failed to delete remote partial object (best-effort): ${partial_obj}" || true
    fi
}

cleanup() {
    if [[ -n "${CURRENT_PARTIAL_OBJ:-}" ]]; then
        delete_partial_remote "${CURRENT_PARTIAL_OBJ}"
        CURRENT_PARTIAL_OBJ=""
    fi
    if [[ -n "${RUN_TMP_DIR:-}" ]] && [[ -d "${RUN_TMP_DIR}" ]]; then
        rm -rf "${RUN_TMP_DIR}" 2>/dev/null || true
    fi
}
install_failure_trap cleanup

load_offsite_backup_env

PG_BACKUP_DIR="${DALTON_PG_BACKUP_DIR:-${HOME}/Backups/dalton}"
EXT_MOUNT_ROOT="${DALTON_EXTERNAL_MOUNT_ROOT:-/Volumes/EveSSD}"
EXT_TMP_ROOT="${DALTON_EXTERNAL_TMP_ROOT:-${EXT_MOUNT_ROOT}/Dalton/backups/.tmp}"

RCLONE_REMOTE="${DALTON_RCLONE_REMOTE:-gdrive}"
RCLONE_DRIVE_DIR="${DALTON_RCLONE_DRIVE_DIR:-DaltonBackup}"
DB_DRIVE_RETAIN="${DALTON_DB_RETAIN_COUNT:-${DALTON_DB_DRIVE_RETAIN:-7}}"
RCLONE_CACHE_DIR="${DALTON_RCLONE_CACHE_DIR:-${EXT_MOUNT_ROOT}/Dalton/backups/.rclone-cache}"
RCLONE_LOG_DIR="${DALTON_RCLONE_LOG_DIR:-${EXT_MOUNT_ROOT}/Dalton/backups/.rclone-logs}"

PY_BIN="$(resolve_python_bin)"

# Hard check: external drive must be mounted and ALL writable runtime/cache/log dirs must reside inside it.
if ! MOUNT_ERR="$("${PY_BIN}" "${BACKUP_LIB}" verify-mount \
    --mount-root "${EXT_MOUNT_ROOT}" \
    --target-path "${EXT_TMP_ROOT}" "${RCLONE_CACHE_DIR}" "${RCLONE_LOG_DIR}" 2>&1)"; then
    fail_with_alert "EXTERNAL_DRIVE_UNMOUNTED" "${MOUNT_ERR}"
fi

mkdir -p "${EXT_TMP_ROOT}" "${RCLONE_CACHE_DIR}" "${RCLONE_LOG_DIR}" \
    || fail_with_alert "EXTERNAL_DRIVE_NOT_WRITABLE" "Cannot create backup runtime directories under ${EXT_MOUNT_ROOT}"

DATE_STAMP="$(date '+%Y-%m-%d')"
RUN_TMP_DIR="$(mktemp -d "${EXT_TMP_ROOT}/db-run-${DATE_STAMP}.XXXXXX")"

if ! MOUNT_ERR="$("${PY_BIN}" "${BACKUP_LIB}" verify-mount \
    --mount-root "${EXT_MOUNT_ROOT}" \
    --target-path "${RUN_TMP_DIR}" 2>&1)"; then
    fail_with_alert "EXTERNAL_DRIVE_UNMOUNTED" "${MOUNT_ERR}"
fi

export TMPDIR="${RUN_TMP_DIR}"
export TEMP="${RUN_TMP_DIR}"
export TMP="${RUN_TMP_DIR}"
export RCLONE_TEMP_DIR="${RUN_TMP_DIR}"
export RCLONE_CACHE_DIR="${RCLONE_CACHE_DIR}"

if ! LATEST_DUMP="$("${PY_BIN}" "${BACKUP_LIB}" latest-pg-dump --pg-dir "${PG_BACKUP_DIR}" 2>"${RUN_TMP_DIR}/pg.err")"; then
    PG_ERR="$(cat "${RUN_TMP_DIR}/pg.err" 2>/dev/null || true)"
    fail_with_alert "PG_DUMP_MISSING" "No complete daily Postgres dump available in ${PG_BACKUP_DIR}: ${PG_ERR}"
fi

if ! RCLONE_STATUS="$("${PY_BIN}" "${BACKUP_LIB}" check-rclone --remote "${RCLONE_REMOTE}" 2>&1)"; then
    fail_with_alert "RCLONE_NOT_CONFIGURED" "rclone remote unavailable for daily DB upload: ${RCLONE_STATUS}"
fi

ensure_backup_key || exit 1

PLAIN_SIZE="$(wc -c < "${LATEST_DUMP}" | tr -d ' ')"
EXPECTED_ENC_SIZE="$("${PY_BIN}" "${BACKUP_LIB}" expected-enc-size --plain-size "${PLAIN_SIZE}")"

RCLONE_LOG_FILE="${RCLONE_LOG_DIR}/rclone-db-${DATE_STAMP}.log"
DUMP_BASE="$(basename "${LATEST_DUMP}")"
REMOTE_DB_BASE="${RCLONE_REMOTE}:${RCLONE_DRIVE_DIR}/db"
REMOTE_DAY_DIR="${REMOTE_DB_BASE}/${DATE_STAMP}"
REMOTE_OBJ="${REMOTE_DAY_DIR}/${DUMP_BASE}.enc"
RAND_DB="$(openssl rand -hex 6)"
PARTIAL_OBJ="${REMOTE_OBJ}.partial-${RAND_DB}"
PARTIAL_NAME="$(basename "${PARTIAL_OBJ}")"
HASH_META="${RUN_TMP_DIR}/db.meta.json"

# Check Drive free space when reported by `rclone about`; skip + alert if below 2x backup size.
ABOUT_JSON="$(rclone_noproxy about "${RCLONE_REMOTE}:" --json \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" 2>/dev/null || true)"

if [[ -n "${ABOUT_JSON}" ]]; then
    if ! SPACE_MSG="$(printf '%s' "${ABOUT_JSON}" | "${PY_BIN}" "${BACKUP_LIB}" check-drive-space --file-size "${EXPECTED_ENC_SIZE}" --multiplier 2 2>&1)"; then
        fail_with_alert "DRIVE_LOW_SPACE" "Skipped daily DB upload for ${DUMP_BASE}: ${SPACE_MSG}"
    fi
    log_msg "INFO" "${SPACE_MSG}"
fi

log_msg "INFO" "Streaming encrypted Postgres dump ${LATEST_DUMP} (${PLAIN_SIZE} bytes -> expected ${EXPECTED_ENC_SIZE} enc bytes) to ${PARTIAL_OBJ}"

CURRENT_PARTIAL_OBJ="${PARTIAL_OBJ}"
suspend_err_trap
set +e
openssl enc -aes-256-cbc -pbkdf2 -salt -in "${LATEST_DUMP}" -pass "file:${DALTON_BACKUP_KEY_FILE}" 2>"${RUN_TMP_DIR}/openssl.err" \
    | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${HASH_META}" 2>"${RUN_TMP_DIR}/hash.err" \
    | rclone_noproxy rcat "${PARTIAL_OBJ}" \
        --size "${EXPECTED_ENC_SIZE}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" \
        --log-file "${RCLONE_LOG_FILE}" \
        --log-level INFO 2>"${RUN_TMP_DIR}/rcat.err"
PIPE_STATUS=("${PIPESTATUS[@]}")
set -e
restore_err_trap

ENC_RC="${PIPE_STATUS[0]:-1}"
HASH_RC="${PIPE_STATUS[1]:-1}"
RCAT_RC="${PIPE_STATUS[2]:-1}"
if [[ "${ENC_RC}" -ne 0 ]] || [[ "${HASH_RC}" -ne 0 ]] || [[ "${RCAT_RC}" -ne 0 ]]; then
    PIPE_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/openssl.err" "${RUN_TMP_DIR}/hash.err" "${RUN_TMP_DIR}/rcat.err" 2>/dev/null || true)"
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "DB_UPLOAD_FAILED" "Streaming DB upload failed (openssl=${ENC_RC}, hash=${HASH_RC}, rcat=${RCAT_RC}): ${PIPE_ERR}"
fi

# Verify uploaded .partial object size AND MD5 hash match the exact ciphertext stream bytes before moveto.
if [[ ! -s "${HASH_META}" ]]; then
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "STREAM_META_MISSING" "Missing ciphertext stream metadata for ${PARTIAL_OBJ}"
fi

LSJSON_OUT=""
if ! LSJSON_OUT="$(rclone_noproxy lsjson "${PARTIAL_OBJ}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsjson.err")"; then
    LS_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/lsjson.err" 2>/dev/null || true)"
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "DB_VERIFY_FAILED" "rclone lsjson failed for ${PARTIAL_OBJ}: ${LS_ERR}"
fi

if ! SIZE_MSG="$(printf '%s' "${LSJSON_OUT}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-size --filename "${PARTIAL_NAME}" --expected-size "${EXPECTED_ENC_SIZE}" 2>&1)"; then
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "DB_SIZE_MISMATCH" "${SIZE_MSG}"
fi

if ! META_SIZE_MSG="$(printf '%s' "${LSJSON_OUT}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-size --filename "${PARTIAL_NAME}" --meta-file "${HASH_META}" 2>&1)"; then
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "DB_SIZE_MISMATCH" "${META_SIZE_MSG}"
fi

HASHSUM_OUT=""
if ! HASHSUM_OUT="$(rclone_noproxy hashsum md5 "${PARTIAL_OBJ}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/hashsum.err")"; then
    HS_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/hashsum.err" 2>/dev/null || true)"
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "DB_HASHSUM_FAILED" "rclone hashsum md5 failed for ${PARTIAL_OBJ}: ${HS_ERR}"
fi

if ! HASH_MSG="$(printf '%s\n' "${HASHSUM_OUT}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-hash --filename "${PARTIAL_NAME}" --meta-file "${HASH_META}" 2>&1)"; then
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "DB_HASH_MISMATCH" "${HASH_MSG}"
fi

if ! rclone_noproxy moveto "${PARTIAL_OBJ}" "${REMOTE_OBJ}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" \
    --log-file "${RCLONE_LOG_FILE}" \
    --log-level INFO 2>"${RUN_TMP_DIR}/moveto.err"; then
    MV_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/moveto.err" 2>/dev/null || true)"
    delete_partial_remote "${PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "DB_MOVETO_FAILED" "rclone moveto failed (${PARTIAL_OBJ} -> ${REMOTE_OBJ}): ${MV_ERR}"
fi

CURRENT_PARTIAL_OBJ=""
rm -f "${HASH_META}"

# Prune remote db/<YYYY-MM-DD> directories keeping DB_DRIVE_RETAIN days.
REMOTE_DIRS="$(rclone_noproxy lsf --dirs-only "${REMOTE_DB_BASE}/" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" 2>/dev/null || true)"

if [[ -n "${REMOTE_DIRS}" ]]; then
    PRUNE_NAMES="$(printf '%s\n' "${REMOTE_DIRS}" | "${PY_BIN}" "${BACKUP_LIB}" select-prune-names --retain "${DB_DRIVE_RETAIN}")"
    if [[ -n "${PRUNE_NAMES}" ]]; then
        while IFS= read -r dname; do
            [[ -n "${dname}" ]] || continue
            if rclone_noproxy purge "${REMOTE_DB_BASE}/${dname}" \
                --cache-dir "${RCLONE_CACHE_DIR}" \
                --temp-dir "${RUN_TMP_DIR}" \
                --log-file "${RCLONE_LOG_FILE}"; then
                log_msg "INFO" "PRUNE_DB_DRIVE purged ${REMOTE_DB_BASE}/${dname}"
            else
                log_msg "WARN" "PRUNE_DB_DRIVE failed to purge ${REMOTE_DB_BASE}/${dname}"
            fi
        done <<EOF
${PRUNE_NAMES}
EOF
    fi
fi

log_msg "INFO" "OK daily DB offsite backup complete: ${REMOTE_OBJ} (${SIZE_MSG}; ${HASH_MSG})"
finish_backup
