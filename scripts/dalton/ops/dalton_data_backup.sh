#!/usr/bin/env bash
# B3: Weekly Dalton data backup -> pure streaming pipeline (<producer> | openssl enc | rclone rcat)
# to Google Drive (encrypted, 4w retention). Never writes large archives or .enc copies to internal or external disk.
# Postgres is backed up daily by dalton_db_offsite.sh (B3b) and intentionally omitted here.
# B8b: rclone calls unset all proxy env vars because proxy bandwidth quota is limited and googleapis.com is directly reachable.

SCRIPT_NAME="dalton_data_backup"
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

EXT_MOUNT_ROOT="${DALTON_EXTERNAL_MOUNT_ROOT:-/Volumes/EveSSD}"
WEEKLY_ROOT="${DALTON_WEEKLY_BACKUP_ROOT:-${EXT_MOUNT_ROOT}/Dalton/backups/weekly}"
EXT_TMP_ROOT="${DALTON_EXTERNAL_TMP_ROOT:-${EXT_MOUNT_ROOT}/Dalton/backups/.tmp}"

MINIO_VOLUME="${DALTON_MINIO_VOLUME:-dalton-langalpha_dalton-minio-data}"
DOCKER_HELPER_IMAGE="${DALTON_DOCKER_HELPER_IMAGE:-alpine:3.20}"
SANDBOX_PREFIX="${DALTON_SANDBOX_CONTAINER_PREFIX:-langalpha-sandbox-}"
SANDBOX_MAX_GB="${DALTON_SANDBOX_MAX_SIZE_GB:-5}"
REDIS_CONTAINER="${DALTON_REDIS_CONTAINER:-dalton-langalpha-redis-1}"
PROD_ENV_FILE="${DALTON_PROD_ENV_FILE:-${HOME}/Projects/dalton-langalpha/.env}"

RCLONE_REMOTE="${DALTON_RCLONE_REMOTE:-gdrive}"
RCLONE_DRIVE_DIR="${DALTON_RCLONE_DRIVE_DIR:-DaltonBackup}"
DRIVE_RETAIN_WEEKS="${DALTON_DRIVE_RETAIN_WEEKS:-4}"
RCLONE_CACHE_DIR="${DALTON_RCLONE_CACHE_DIR:-${EXT_MOUNT_ROOT}/Dalton/backups/.rclone-cache}"
RCLONE_LOG_DIR="${DALTON_RCLONE_LOG_DIR:-${EXT_MOUNT_ROOT}/Dalton/backups/.rclone-logs}"

PY_BIN="$(resolve_python_bin)"

# Hard check: external drive must be mounted and EVERY writable dir must live inside it.
if ! MOUNT_ERR="$("${PY_BIN}" "${BACKUP_LIB}" verify-mount \
    --mount-root "${EXT_MOUNT_ROOT}" \
    --target-path "${WEEKLY_ROOT}" "${EXT_TMP_ROOT}" "${RCLONE_CACHE_DIR}" "${RCLONE_LOG_DIR}" 2>&1)"; then
    fail_with_alert "EXTERNAL_DRIVE_UNMOUNTED" "${MOUNT_ERR}"
fi

mkdir -p "${EXT_TMP_ROOT}" "${RCLONE_CACHE_DIR}" "${RCLONE_LOG_DIR}" \
    || fail_with_alert "EXTERNAL_DRIVE_NOT_WRITABLE" "Cannot create backup runtime directories under ${EXT_MOUNT_ROOT}"

DATE_STAMP="$(date '+%Y-%m-%d')"
RUN_TMP_DIR="$(mktemp -d "${EXT_TMP_ROOT}/run-${DATE_STAMP}.XXXXXX")"

# Double-check created temp dir itself is inside EXT_MOUNT_ROOT and force all temp/cache env vars there.
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

if ! RCLONE_STATUS="$("${PY_BIN}" "${BACKUP_LIB}" check-rclone --remote "${RCLONE_REMOTE}" 2>&1)"; then
    fail_with_alert "RCLONE_NOT_CONFIGURED" "Weekly streaming backup requires configured rclone remote: ${RCLONE_STATUS}"
fi

ensure_backup_key || exit 1

if ! docker info >/dev/null 2>&1; then
    fail_with_alert "DOCKER_UNAVAILABLE" "Docker daemon is not reachable"
fi

if ! docker volume inspect "${MINIO_VOLUME}" >/dev/null 2>&1; then
    fail_with_alert "MINIO_VOLUME_MISSING" "Docker volume not found: ${MINIO_VOLUME}"
fi

RCLONE_LOG_FILE="${RCLONE_LOG_DIR}/rclone-${DATE_STAMP}.log"
REMOTE_WEEK_DEST="${RCLONE_REMOTE}:${RCLONE_DRIVE_DIR}/weekly/${DATE_STAMP}"

verify_and_promote_partial() {
    local partial_obj="$1"
    local final_obj="$2"
    local hash_meta="$3"
    local label="$4"
    local partial_name
    partial_name="$(basename "${partial_obj}")"

    if [[ ! -s "${hash_meta}" ]]; then
        delete_partial_remote "${partial_obj}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "STREAM_META_MISSING" "Missing ciphertext stream metadata for ${label}"
    fi

    local lsjson_err="${RUN_TMP_DIR}/lsjson-${label}.err"
    local lsjson_out=""
    if ! lsjson_out="$(rclone_noproxy lsjson "${partial_obj}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${lsjson_err}")"; then
        local ls_err
        ls_err="$(tr '\n' ' ' < "${lsjson_err}" 2>/dev/null || true)"
        delete_partial_remote "${partial_obj}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "RCLONE_VERIFY_FAILED" "rclone lsjson failed for ${partial_obj}: ${ls_err}"
    fi

    local size_msg=""
    if ! size_msg="$(printf '%s' "${lsjson_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-size \
        --filename "${partial_name}" \
        --meta-file "${hash_meta}" 2>&1)"; then
        delete_partial_remote "${partial_obj}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "RCLONE_SIZE_MISMATCH" "Remote size check failed for ${partial_obj}: ${size_msg}"
    fi

    local hash_err="${RUN_TMP_DIR}/hashsum-${label}.err"
    local hashsum_out=""
    if ! hashsum_out="$(rclone_noproxy hashsum md5 "${partial_obj}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${hash_err}")"; then
        local hs_err
        hs_err="$(tr '\n' ' ' < "${hash_err}" 2>/dev/null || true)"
        delete_partial_remote "${partial_obj}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "RCLONE_HASHSUM_FAILED" "rclone hashsum md5 failed for ${partial_obj}: ${hs_err}"
    fi

    local hash_msg=""
    if ! hash_msg="$(printf '%s\n' "${hashsum_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-hash \
        --filename "${partial_name}" \
        --meta-file "${hash_meta}" 2>&1)"; then
        delete_partial_remote "${partial_obj}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "RCLONE_HASH_MISMATCH" "Remote MD5 check failed for ${partial_obj}: ${hash_msg}"
    fi

    if ! rclone_noproxy moveto "${partial_obj}" "${final_obj}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" \
        --log-file "${RCLONE_LOG_FILE}" \
        --log-level INFO 2>"${RUN_TMP_DIR}/moveto-${label}.err"; then
        local mv_err
        mv_err="$(tr '\n' ' ' < "${RUN_TMP_DIR}/moveto-${label}.err" 2>/dev/null || true)"
        delete_partial_remote "${partial_obj}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "RCLONE_MOVETO_FAILED" "rclone moveto failed (${partial_obj} -> ${final_obj}): ${mv_err}"
    fi

    CURRENT_PARTIAL_OBJ=""
    rm -f "${hash_meta}"
    log_msg "INFO" "Verified & promoted ${final_obj} (${size_msg}; ${hash_msg})"
}

# 1. MinIO volume streaming backup -> openssl enc -> stream-hash-meta -> rclone rcat (.partial -> moveto)
RAND_MINIO="$(openssl rand -hex 6)"
MINIO_FINAL_OBJ="${REMOTE_WEEK_DEST}/minio-${DATE_STAMP}.tar.gz.enc"
MINIO_PARTIAL_OBJ="${MINIO_FINAL_OBJ}.partial-${RAND_MINIO}"
MINIO_META="${RUN_TMP_DIR}/minio.meta.json"
CURRENT_PARTIAL_OBJ="${MINIO_PARTIAL_OBJ}"

log_msg "INFO" "Streaming MinIO volume ${MINIO_VOLUME} -> ${MINIO_PARTIAL_OBJ}"
suspend_err_trap
set +e
docker run --rm \
    -v "${MINIO_VOLUME}:/data:ro" \
    "${DOCKER_HELPER_IMAGE}" \
    tar czf - -C /data . 2>"${RUN_TMP_DIR}/minio.err" \
    | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}" 2>"${RUN_TMP_DIR}/minio-enc.err" \
    | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${MINIO_META}" 2>"${RUN_TMP_DIR}/minio-hash.err" \
    | rclone_noproxy rcat "${MINIO_PARTIAL_OBJ}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" \
        --log-file "${RCLONE_LOG_FILE}" \
        --log-level INFO 2>"${RUN_TMP_DIR}/minio-rcat.err"
PIPE_STATUS=("${PIPESTATUS[@]}")
set -e
restore_err_trap

if [[ "${PIPE_STATUS[0]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[1]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[2]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[3]:-1}" -ne 0 ]]; then
    MINIO_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/minio.err" "${RUN_TMP_DIR}/minio-enc.err" "${RUN_TMP_DIR}/minio-rcat.err" 2>/dev/null || true)"
    delete_partial_remote "${MINIO_PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "MINIO_BACKUP_FAILED" "MinIO stream failed (pipe=${PIPE_STATUS[*]}): ${MINIO_ERR}"
fi
verify_and_promote_partial "${MINIO_PARTIAL_OBJ}" "${MINIO_FINAL_OBJ}" "${MINIO_META}" "minio"

# 2. User sandbox containers (/home/workspace), including stopped containers (strict exit check on docker ps -a)
MAX_SANDBOX_BYTES="$((SANDBOX_MAX_GB * 1024 * 1024 * 1024))"
SANDBOX_LIST_ERR="${RUN_TMP_DIR}/docker-ps.err"
if ! RAW_CONTAINERS="$(docker ps -a --filter "name=${SANDBOX_PREFIX}" --format '{{.Names}}' 2>"${SANDBOX_LIST_ERR}")"; then
    PS_ERR="$(tr '\n' ' ' < "${SANDBOX_LIST_ERR}" 2>/dev/null || true)"
    fail_with_alert "DOCKER_LIST_FAILED" "docker ps -a failed while listing sandboxes: ${PS_ERR}"
fi
SANDBOX_CONTAINERS="$(printf '%s\n' "${RAW_CONTAINERS}" | sed '/^$/d' | sort)"

SANDBOX_COUNT=0
SANDBOX_SKIPPED=0
if [[ -n "${SANDBOX_CONTAINERS}" ]]; then
    while IFS= read -r cname; do
        [[ -n "${cname}" ]] || continue
        SAFE_NAME="${cname//[^A-Za-z0-9_.-]/_}"
        RAND_SB="$(openssl rand -hex 6)"
        SB_FINAL_OBJ="${REMOTE_WEEK_DEST}/sandbox-${SAFE_NAME}-${DATE_STAMP}.tar.gz.enc"
        SB_PARTIAL_OBJ="${SB_FINAL_OBJ}.partial-${RAND_SB}"
        SB_META="${RUN_TMP_DIR}/sb-${SAFE_NAME}.meta.json"
        CP_ERR_FILE="${RUN_TMP_DIR}/cp-${SAFE_NAME}.err"
        CURRENT_PARTIAL_OBJ="${SB_PARTIAL_OBJ}"

        suspend_err_trap
        set +e
        docker cp "${cname}:/home/workspace" - 2>"${CP_ERR_FILE}" \
            | "${PY_BIN}" "${BACKUP_LIB}" stream-cap-stdout --max-bytes "${MAX_SANDBOX_BYTES}" 2>"${RUN_TMP_DIR}/cap-${SAFE_NAME}.err" \
            | gzip -c 2>"${RUN_TMP_DIR}/gzip-${SAFE_NAME}.err" \
            | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}" 2>"${RUN_TMP_DIR}/enc-${SAFE_NAME}.err" \
            | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${SB_META}" 2>"${RUN_TMP_DIR}/hash-${SAFE_NAME}.err" \
            | rclone_noproxy rcat "${SB_PARTIAL_OBJ}" \
                --cache-dir "${RCLONE_CACHE_DIR}" \
                --temp-dir "${RUN_TMP_DIR}" \
                --log-file "${RCLONE_LOG_FILE}" \
                --log-level INFO 2>"${RUN_TMP_DIR}/rcat-${SAFE_NAME}.err"
        PIPE_STATUS=("${PIPESTATUS[@]}")
        set -e
        restore_err_trap

        CP_RC="${PIPE_STATUS[0]:-1}"
        CAP_RC="${PIPE_STATUS[1]:-1}"
        GZIP_RC="${PIPE_STATUS[2]:-1}"
        ENC_RC="${PIPE_STATUS[3]:-1}"
        HASH_RC="${PIPE_STATUS[4]:-1}"
        RCAT_RC="${PIPE_STATUS[5]:-1}"

        if [[ "${CAP_RC}" -eq 42 ]]; then
            delete_partial_remote "${SB_PARTIAL_OBJ}"
            CURRENT_PARTIAL_OBJ=""
            rm -f "${SB_META}"
            SANDBOX_SKIPPED=$((SANDBOX_SKIPPED + 1))
            log_msg "WARN" "SKIP_LARGE sandbox ${cname}: /home/workspace exceeded ${SANDBOX_MAX_GB} GB limit"
            continue
        fi

        if [[ "${CP_RC}" -ne 0 ]] || [[ "${CAP_RC}" -ne 0 ]] || [[ "${GZIP_RC}" -ne 0 ]] || [[ "${ENC_RC}" -ne 0 ]] || [[ "${HASH_RC}" -ne 0 ]] || [[ "${RCAT_RC}" -ne 0 ]]; then
            delete_partial_remote "${SB_PARTIAL_OBJ}"
            CURRENT_PARTIAL_OBJ=""
            rm -f "${SB_META}"
            CP_ERR_TEXT="$(cat "${CP_ERR_FILE}" 2>/dev/null || true)"
            if [[ "${CP_RC}" -ne 0 ]] && printf '%s' "${CP_ERR_TEXT}" | grep -qiE "No such container:path|Could not find the file"; then
                log_msg "INFO" "SKIP_NO_WORKSPACE sandbox ${cname}: /home/workspace absent"
                continue
            fi
            SB_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/rcat-${SAFE_NAME}.err" 2>/dev/null || true)"
            fail_with_alert "SANDBOX_BACKUP_FAILED" "Sandbox stream failed for ${cname} (pipe=${PIPE_STATUS[*]}): ${CP_ERR_TEXT} ${SB_ERR}"
        fi

        verify_and_promote_partial "${SB_PARTIAL_OBJ}" "${SB_FINAL_OBJ}" "${SB_META}" "sandbox-${SAFE_NAME}"
        SANDBOX_COUNT=$((SANDBOX_COUNT + 1))
    done <<EOF
${SANDBOX_CONTAINERS}
EOF
fi
log_msg "INFO" "Sandbox backups complete: uploaded=${SANDBOX_COUNT} skipped_large=${SANDBOX_SKIPPED}"

# 3. Redis snapshot via stdin password -> redis-cli --rdb - -> stream -> rclone rcat (.partial -> moveto)
if ! docker inspect "${REDIS_CONTAINER}" >/dev/null 2>&1; then
    if ! ALT_REDIS="$(docker ps --filter "name=redis" --format '{{.Names}}' 2>"${RUN_TMP_DIR}/redis-ps.err" | head -n 1)"; then
        fail_with_alert "REDIS_CONTAINER_MISSING" "Redis container lookup failed: ${REDIS_CONTAINER}"
    fi
    if [[ -n "${ALT_REDIS}" ]]; then
        REDIS_CONTAINER="${ALT_REDIS}"
    else
        fail_with_alert "REDIS_CONTAINER_MISSING" "Redis container not found: ${REDIS_CONTAINER}"
    fi
fi

REDIS_PASS="redis"
if [[ -f "${PROD_ENV_FILE}" ]] && [[ ! -L "${PROD_ENV_FILE}" ]]; then
    RAW_REDIS_PASS="$(grep -E '^[[:space:]]*(export[[:space:]]+)?REDIS_PASSWORD=' "${PROD_ENV_FILE}" | tail -n 1 | cut -d= -f2- || true)"
    RAW_REDIS_PASS="${RAW_REDIS_PASS#\"}"
    RAW_REDIS_PASS="${RAW_REDIS_PASS%\"}"
    RAW_REDIS_PASS="${RAW_REDIS_PASS#\'}"
    RAW_REDIS_PASS="${RAW_REDIS_PASS%\'}"
    if [[ -n "${RAW_REDIS_PASS}" ]]; then
        REDIS_PASS="${RAW_REDIS_PASS}"
    fi
fi

RAND_REDIS="$(openssl rand -hex 6)"
REDIS_FINAL_OBJ="${REMOTE_WEEK_DEST}/redis-${DATE_STAMP}.rdb.enc"
REDIS_PARTIAL_OBJ="${REDIS_FINAL_OBJ}.partial-${RAND_REDIS}"
REDIS_META="${RUN_TMP_DIR}/redis.meta.json"
CURRENT_PARTIAL_OBJ="${REDIS_PARTIAL_OBJ}"

log_msg "INFO" "Streaming Redis RDB snapshot from ${REDIS_CONTAINER} -> ${REDIS_PARTIAL_OBJ}"
suspend_err_trap
set +e
{
    printf '%s\n' "${REDIS_PASS}" | docker exec -i "${REDIS_CONTAINER}" sh -c 'IFS= read -r p && REDISCLI_AUTH="$p" exec redis-cli --rdb -'
} 2>"${RUN_TMP_DIR}/redis.err" \
    | "${PY_BIN}" "${BACKUP_LIB}" stream-cap-stdout --max-bytes 0 --require-prefix REDIS 2>"${RUN_TMP_DIR}/redis-hdr.err" \
    | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}" 2>"${RUN_TMP_DIR}/redis-enc.err" \
    | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${REDIS_META}" 2>"${RUN_TMP_DIR}/redis-hash.err" \
    | rclone_noproxy rcat "${REDIS_PARTIAL_OBJ}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" \
        --log-file "${RCLONE_LOG_FILE}" \
        --log-level INFO 2>"${RUN_TMP_DIR}/redis-rcat.err"
PIPE_STATUS=("${PIPESTATUS[@]}")
set -e
restore_err_trap

if [[ "${PIPE_STATUS[0]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[1]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[2]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[3]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[4]:-1}" -ne 0 ]]; then
    REDIS_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/redis.err" "${RUN_TMP_DIR}/redis-hdr.err" "${RUN_TMP_DIR}/redis-enc.err" "${RUN_TMP_DIR}/redis-rcat.err" 2>/dev/null || true)"
    delete_partial_remote "${REDIS_PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "REDIS_BACKUP_FAILED" "Redis stream failed on ${REDIS_CONTAINER} (pipe=${PIPE_STATUS[*]}): ${REDIS_ERR}"
fi
verify_and_promote_partial "${REDIS_PARTIAL_OBJ}" "${REDIS_FINAL_OBJ}" "${REDIS_META}" "redis"

# Prune Google Drive weekly/ keeping DRIVE_RETAIN_WEEKS (only YYYY-MM-DD directories)
REMOTE_WEEKLY_BASE="${RCLONE_REMOTE}:${RCLONE_DRIVE_DIR}/weekly"
REMOTE_DIRS="$(rclone_noproxy lsf --dirs-only "${REMOTE_WEEKLY_BASE}/" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" 2>/dev/null || true)"

if [[ -n "${REMOTE_DIRS}" ]]; then
    PRUNE_DRIVE_NAMES="$(printf '%s\n' "${REMOTE_DIRS}" | "${PY_BIN}" "${BACKUP_LIB}" select-prune-names --retain "${DRIVE_RETAIN_WEEKS}")"
    if [[ -n "${PRUNE_DRIVE_NAMES}" ]]; then
        while IFS= read -r dname; do
            [[ -n "${dname}" ]] || continue
            if rclone_noproxy purge "${REMOTE_WEEKLY_BASE}/${dname}" \
                --cache-dir "${RCLONE_CACHE_DIR}" \
                --temp-dir "${RUN_TMP_DIR}" \
                --log-file "${RCLONE_LOG_FILE}"; then
                log_msg "INFO" "PRUNE_DRIVE purged ${REMOTE_WEEKLY_BASE}/${dname}"
            else
                log_msg "WARN" "PRUNE_DRIVE failed to purge ${REMOTE_WEEKLY_BASE}/${dname}"
            fi
        done <<EOF
${PRUNE_DRIVE_NAMES}
EOF
    fi
fi

log_msg "INFO" "OK weekly streaming backup complete: drive=${REMOTE_WEEK_DEST}"
finish_backup
