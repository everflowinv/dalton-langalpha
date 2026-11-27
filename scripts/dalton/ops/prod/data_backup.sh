#!/usr/bin/env bash
# B3 / §3.1 step 4: Weekly Dalton data & MinIO baseline backup -> streaming pipeline
# (<producer> | openssl enc | rclone rcat .partial -> verify size & MD5 -> moveto)
# to Google Drive (<remote>:DaltonBackup/weekly/<YYYY-MM-DD>/, default 4w retention;
# pruning exempts any weekly baseline_id still referenced by retained daily db/*/LATEST.json).
# Postgres is backed up daily by prod/db_offsite.sh and intentionally omitted here.

SCRIPT_NAME="dalton_data_backup"
PROD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_DIR="$(cd "${PROD_DIR}/.." && pwd)"
OFFSITE_SYNC_PY="${PROD_DIR}/offsite_sync.py"
# shellcheck source=scripts/dalton/ops/_common.sh
source "${OPS_DIR}/_common.sh"

RUN_TMP_DIR=""
VAR_SQLITE_STAGING=""
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
    if [[ -n "${VAR_SQLITE_STAGING:-}" ]] && [[ -d "${VAR_SQLITE_STAGING}" ]]; then
        rm -rf "${VAR_SQLITE_STAGING}" 2>/dev/null || true
    fi
    if [[ -n "${RUN_TMP_DIR:-}" ]] && [[ -d "${RUN_TMP_DIR}" ]]; then
        rm -rf "${RUN_TMP_DIR}" 2>/dev/null || true
    fi
}
install_failure_trap cleanup

load_offsite_backup_env

EXT_MOUNT_ROOT="${DALTON_EXTERNAL_MOUNT_ROOT:-/Volumes/EveSSD}"
BACKUP_ROOT="${DALTON_BACKUP_ROOT:-${EXT_MOUNT_ROOT}/Dalton/backups}"
WEEKLY_ROOT="${DALTON_WEEKLY_BACKUP_ROOT:-${BACKUP_ROOT}/weekly}"
SETS_DIR="${DALTON_BACKUP_SETS_DIR:-${BACKUP_ROOT}/sets}"
EXT_TMP_ROOT="${DALTON_EXTERNAL_TMP_ROOT:-${BACKUP_ROOT}/.tmp}"

MINIO_VOLUME="${DALTON_MINIO_VOLUME:-dalton-langalpha_dalton-minio-data}"
DOCKER_HELPER_IMAGE="${DALTON_DOCKER_HELPER_IMAGE:-alpine:3.20}"
SANDBOX_PREFIX="${DALTON_SANDBOX_CONTAINER_PREFIX:-langalpha-sandbox-}"
SANDBOX_MAX_GB="${DALTON_SANDBOX_MAX_SIZE_GB:-5}"
REDIS_CONTAINER="${DALTON_REDIS_CONTAINER:-dalton-langalpha-redis-1}"
PROD_ENV_FILE="${DALTON_PROD_ENV_FILE:-${HOME}/Projects/dalton-langalpha/.env}"
PROD_CHECKOUT_DIR="${DALTON_PROD_CHECKOUT_DIR:-${HOME}/Projects/dalton-langalpha}"
PROD_VAR_DIR="${DALTON_PROD_VAR_DIR:-${PROD_CHECKOUT_DIR}/var/dalton}"
DATA_VAR_EXCLUDES="${DALTON_DATA_VAR_EXCLUDES:-venv,.venv,__pycache__,*.pyc,node_modules,.DS_Store,pr_baseline,tmp,openclaw-data/temp,openclaw-data/skills/*/temp,run-secrets,.dalton-outbox-*}"
WEEKLY_MINIO_MODE="${DALTON_WEEKLY_MINIO_MODE:-versioned}"

# Load production .env storage variables (matching prod/backup.sh) while preserving
# offsite-backup.env overrides (re-loaded right after).
if [[ "${DALTON_DATA_BACKUP_LOAD_PROD_ENV:-1}" == "1" ]] && [[ -f "${PROD_ENV_FILE}" ]] && [[ ! -L "${PROD_ENV_FILE}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${PROD_ENV_FILE}" 2>/dev/null || true
    set +a
    load_offsite_backup_env
    PROD_ENV_FILE="${DALTON_PROD_ENV_FILE:-${HOME}/Projects/dalton-langalpha/.env}"
    PROD_CHECKOUT_DIR="${DALTON_PROD_CHECKOUT_DIR:-${HOME}/Projects/dalton-langalpha}"
    PROD_VAR_DIR="${DALTON_PROD_VAR_DIR:-${PROD_CHECKOUT_DIR}/var/dalton}"
    WEEKLY_MINIO_MODE="${DALTON_WEEKLY_MINIO_MODE:-versioned}"
fi

RCLONE_REMOTE="${DALTON_RCLONE_REMOTE:-gdrive}"
RCLONE_DRIVE_DIR="${DALTON_RCLONE_DRIVE_DIR:-DaltonBackup}"
DRIVE_RETAIN_WEEKS="${DALTON_DRIVE_RETAIN_WEEKS:-4}"
RCLONE_CACHE_DIR="${DALTON_RCLONE_CACHE_DIR:-${BACKUP_ROOT}/.rclone-cache}"
RCLONE_LOG_DIR="${DALTON_RCLONE_LOG_DIR:-${BACKUP_ROOT}/.rclone-logs}"

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
RUN_ID="$(date '+%Y%m%d-%H%M%S')-$(openssl rand -hex 3)"
BASELINE_ID="${DATE_STAMP}-${RUN_ID}"
RUN_TMP_DIR="$(mktemp -d "${EXT_TMP_ROOT}/run-${DATE_STAMP}.XXXXXX")"

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

if [[ "${WEEKLY_MINIO_MODE}" != "versioned" ]]; then
    if ! docker volume inspect "${MINIO_VOLUME}" >/dev/null 2>&1; then
        fail_with_alert "MINIO_VOLUME_MISSING" "Docker volume not found: ${MINIO_VOLUME}"
    fi
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
    log_msg "INFO" "Verified & promoted ${final_obj} (${size_msg}; ${hash_msg})"
}

# 1. MinIO weekly baseline (versioned S3 baseline by default, with immutable RUN_ID filename;
#    staged baseline manifest is NOT published into SETS_DIR until remote verification + LATEST.json succeed)
RAND_MINIO="$(openssl rand -hex 6)"
MINIO_FINAL_NAME="minio-${DATE_STAMP}-${RUN_ID}.tar.gz.enc"
MINIO_FINAL_OBJ="${REMOTE_WEEK_DEST}/${MINIO_FINAL_NAME}"
MINIO_PARTIAL_OBJ="${MINIO_FINAL_OBJ}.partial-${RAND_MINIO}"
MINIO_META="${RUN_TMP_DIR}/minio.meta.json"
STAGED_BASELINE_MANIFEST=""
CURRENT_PARTIAL_OBJ="${MINIO_PARTIAL_OBJ}"

if [[ "${WEEKLY_MINIO_MODE}" == "versioned" ]]; then
    BASELINE_TAR="${RUN_TMP_DIR}/minio-baseline-${BASELINE_ID}.tar.gz"
    STAGED_BASELINE_MANIFEST="${RUN_TMP_DIR}/weekly-baseline-${BASELINE_ID}.json"
    log_msg "INFO" "Building versioned MinIO weekly baseline ${BASELINE_ID} -> ${MINIO_PARTIAL_OBJ}"
    if ! "${PY_BIN}" "${OFFSITE_SYNC_PY}" weekly-minio-baseline \
        --baseline-id "${BASELINE_ID}" \
        --out-tar "${BASELINE_TAR}" \
        --manifest-out "${STAGED_BASELINE_MANIFEST}" \
        --tmp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/minio-baseline.err"; then
        BL_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/minio-baseline.err" 2>/dev/null || true)"
        fail_with_alert "MINIO_BACKUP_FAILED" "Versioned MinIO baseline build failed: ${BL_ERR}"
    fi
    suspend_err_trap
    set +e
    openssl enc -aes-256-cbc -pbkdf2 -salt -in "${BASELINE_TAR}" -pass "file:${DALTON_BACKUP_KEY_FILE}" 2>"${RUN_TMP_DIR}/minio-enc.err" \
        | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${MINIO_META}" 2>"${RUN_TMP_DIR}/minio-hash.err" \
        | rclone_noproxy rcat "${MINIO_PARTIAL_OBJ}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" \
            --log-file "${RCLONE_LOG_FILE}" \
            --log-level INFO 2>"${RUN_TMP_DIR}/minio-rcat.err"
    PIPE_STATUS=("${PIPESTATUS[@]}")
    set -e
    restore_err_trap
    rm -f "${BASELINE_TAR}"
    if [[ "${PIPE_STATUS[0]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[1]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[2]:-1}" -ne 0 ]]; then
        MINIO_ERR="$(cat "${RUN_TMP_DIR}/minio-enc.err" "${RUN_TMP_DIR}/minio-hash.err" "${RUN_TMP_DIR}/minio-rcat.err" 2>/dev/null | tr '\n' ' ' || true)"
        delete_partial_remote "${MINIO_PARTIAL_OBJ}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "MINIO_BACKUP_FAILED" "MinIO baseline upload failed (pipe=${PIPE_STATUS[*]}): ${MINIO_ERR}"
    fi
else
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
        MINIO_ERR="$(cat "${RUN_TMP_DIR}/minio.err" "${RUN_TMP_DIR}/minio-enc.err" "${RUN_TMP_DIR}/minio-hash.err" "${RUN_TMP_DIR}/minio-rcat.err" 2>/dev/null | tr '\n' ' ' || true)"
        delete_partial_remote "${MINIO_PARTIAL_OBJ}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "MINIO_BACKUP_FAILED" "MinIO stream failed (pipe=${PIPE_STATUS[*]}): ${MINIO_ERR}"
    fi
fi
verify_and_promote_partial "${MINIO_PARTIAL_OBJ}" "${MINIO_FINAL_OBJ}" "${MINIO_META}" "minio"

# 2. User sandbox containers (/home/workspace), including stopped containers
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
        SB_FINAL_OBJ="${REMOTE_WEEK_DEST}/sandbox-${SAFE_NAME}-${DATE_STAMP}-${RUN_ID}.tar.gz.enc"
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

# 3. Redis snapshot via stdin password -> redis-cli --rdb -
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
REDIS_FINAL_NAME="redis-${DATE_STAMP}-${RUN_ID}.rdb.enc"
REDIS_FINAL_OBJ="${REMOTE_WEEK_DEST}/${REDIS_FINAL_NAME}"
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
    REDIS_ERR="$(cat "${RUN_TMP_DIR}/redis.err" "${RUN_TMP_DIR}/redis-hdr.err" "${RUN_TMP_DIR}/redis-enc.err" "${RUN_TMP_DIR}/redis-hash.err" "${RUN_TMP_DIR}/redis-rcat.err" 2>/dev/null | tr '\n' ' ' || true)"
    delete_partial_remote "${REDIS_PARTIAL_OBJ}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "REDIS_BACKUP_FAILED" "Redis stream failed on ${REDIS_CONTAINER} (pipe=${PIPE_STATUS[*]}): ${REDIS_ERR}"
fi
verify_and_promote_partial "${REDIS_PARTIAL_OBJ}" "${REDIS_FINAL_OBJ}" "${REDIS_META}" "redis"

# 4. Host var/dalton directory streaming backup -> snapshot SQLite -> openssl enc -> rclone rcat
if [[ ! -d "${PROD_VAR_DIR}" ]]; then
    fail_with_alert "VAR_DALTON_BACKUP_FAILED" "var/dalton directory not found: ${PROD_VAR_DIR}"
fi

VAR_FINAL_NAME="var-dalton-${DATE_STAMP}-${RUN_ID}.tar.gz.enc"
VAR_FINAL_OBJ="${REMOTE_WEEK_DEST}/${VAR_FINAL_NAME}"
VAR_SQLITE_STAGING="${RUN_TMP_DIR}/var-dalton-sqlite-staging"
VAR_SQLITE_LIST="${RUN_TMP_DIR}/var-dalton-sqlite.nul"
VAR_SQLITE_ERR="${RUN_TMP_DIR}/var-dalton-sqlite.err"
VAR_META="${RUN_TMP_DIR}/var-dalton.meta.json"
VAR_MAX_ATTEMPTS=2
VAR_ATTEMPT=1

while true; do
    rm -rf "${VAR_SQLITE_STAGING}" "${VAR_SQLITE_LIST}" "${VAR_SQLITE_ERR}" "${VAR_META}" \
        "${RUN_TMP_DIR}/var-dalton.err" "${RUN_TMP_DIR}/var-dalton-enc.err" \
        "${RUN_TMP_DIR}/var-dalton-hash.err" "${RUN_TMP_DIR}/var-dalton-rcat.err"
    mkdir -p "${VAR_SQLITE_STAGING}"

    if ! "${PY_BIN}" "${BACKUP_LIB}" snapshot-sqlite \
        --src-dir "${PROD_VAR_DIR}" \
        --dest-dir "${VAR_SQLITE_STAGING}" \
        --excludes "${DATA_VAR_EXCLUDES}" \
        >"${VAR_SQLITE_LIST}" 2>"${VAR_SQLITE_ERR}"; then
        SNAP_ERR="$(tr '\n' ' ' < "${VAR_SQLITE_ERR}" 2>/dev/null || true)"
        rm -rf "${VAR_SQLITE_STAGING}" "${VAR_SQLITE_LIST}" "${VAR_SQLITE_ERR}"
        fail_with_alert "VAR_DALTON_BACKUP_FAILED" "SQLite snapshot failed for ${PROD_VAR_DIR}: ${SNAP_ERR}"
    fi

    VAR_TAR_CMD=()
    while IFS= read -r -d '' tok; do
        VAR_TAR_CMD+=("${tok}")
    done < <("${PY_BIN}" "${BACKUP_LIB}" var-dalton-tar-cmd \
        --src-dir "${PROD_VAR_DIR}" \
        --excludes "${DATA_VAR_EXCLUDES}" \
        --sqlite-manifest "${VAR_SQLITE_LIST}" \
        --staging-dir "${VAR_SQLITE_STAGING}")
    if [[ "${#VAR_TAR_CMD[@]}" -lt 4 ]] || [[ "${VAR_TAR_CMD[0]}" != "tar" ]]; then
        rm -rf "${VAR_SQLITE_STAGING}" "${VAR_SQLITE_LIST}" "${VAR_SQLITE_ERR}"
        fail_with_alert "VAR_DALTON_BACKUP_FAILED" "Failed to construct var/dalton tar command for ${PROD_VAR_DIR}"
    fi

    RAND_VAR="$(openssl rand -hex 6)"
    VAR_PARTIAL_OBJ="${VAR_FINAL_OBJ}.partial-${RAND_VAR}"
    CURRENT_PARTIAL_OBJ="${VAR_PARTIAL_OBJ}"

    log_msg "INFO" "Streaming var/dalton ${PROD_VAR_DIR} -> ${VAR_PARTIAL_OBJ} (attempt ${VAR_ATTEMPT}/${VAR_MAX_ATTEMPTS})"
    suspend_err_trap
    set +e
    COPYFILE_DISABLE=1 "${VAR_TAR_CMD[@]}" 2>"${RUN_TMP_DIR}/var-dalton.err" \
        | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${DALTON_BACKUP_KEY_FILE}" 2>"${RUN_TMP_DIR}/var-dalton-enc.err" \
        | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${VAR_META}" 2>"${RUN_TMP_DIR}/var-dalton-hash.err" \
        | rclone_noproxy rcat "${VAR_PARTIAL_OBJ}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" \
            --log-file "${RCLONE_LOG_FILE}" \
            --log-level INFO 2>"${RUN_TMP_DIR}/var-dalton-rcat.err"
    PIPE_STATUS=("${PIPESTATUS[@]}")
    set -e
    restore_err_trap

    TAR_RC="${PIPE_STATUS[0]:-1}"
    VAR_PIPE_STR="${TAR_RC} ${PIPE_STATUS[1]:-1} ${PIPE_STATUS[2]:-1} ${PIPE_STATUS[3]:-1}"
    if [[ "${TAR_RC}" -ne 0 ]] || [[ "${PIPE_STATUS[1]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[2]:-1}" -ne 0 ]] || [[ "${PIPE_STATUS[3]:-1}" -ne 0 ]]; then
        delete_partial_remote "${VAR_PARTIAL_OBJ}"
        CURRENT_PARTIAL_OBJ=""
        if "${PY_BIN}" "${BACKUP_LIB}" should-retry-tar --attempt "${VAR_ATTEMPT}" --pipe-status "${VAR_PIPE_STR}" --max-retries 1; then
            TAR_REASON="$("${PY_BIN}" "${BACKUP_LIB}" summarize-tar-stderr --err-file "${RUN_TMP_DIR}/var-dalton.err" 2>/dev/null || echo "tar exited ${TAR_RC}")"
            log_msg "WARN" "var/dalton tar failed on attempt ${VAR_ATTEMPT}, retrying once: ${TAR_REASON}"
            rm -rf "${VAR_SQLITE_STAGING}" "${VAR_SQLITE_LIST}" "${VAR_SQLITE_ERR}" "${VAR_META}"
            VAR_ATTEMPT=$((VAR_ATTEMPT + 1))
            continue
        fi
        VAR_ERR="$(cat "${RUN_TMP_DIR}/var-dalton.err" "${RUN_TMP_DIR}/var-dalton-enc.err" "${RUN_TMP_DIR}/var-dalton-hash.err" "${RUN_TMP_DIR}/var-dalton-rcat.err" 2>/dev/null | tr '\n' ' ' || true)"
        rm -rf "${VAR_SQLITE_STAGING}" "${VAR_SQLITE_LIST}" "${VAR_SQLITE_ERR}" "${VAR_META}"
        fail_with_alert "VAR_DALTON_BACKUP_FAILED" "var/dalton stream failed (pipe=${PIPE_STATUS[*]}): ${VAR_ERR}"
    fi

    rm -rf "${VAR_SQLITE_STAGING}" "${VAR_SQLITE_LIST}" "${VAR_SQLITE_ERR}"
    verify_and_promote_partial "${VAR_PARTIAL_OBJ}" "${VAR_FINAL_OBJ}" "${VAR_META}" "var-dalton"
    break
done

# 5. Publish an IMMUTABLE per-baseline checksum manifest (`baseline-<baseline_id>.json`)
#    so same-day weekly reruns NEVER clobber the verification manifest referenced by earlier
#    daily incrementals, THEN flip `weekly/<YYYY-MM-DD>/LATEST.json` and the local pointer.
WEEKLY_LATEST_LOCAL="${RUN_TMP_DIR}/LATEST.json"
"${PY_BIN}" "${OFFSITE_SYNC_PY}" build-weekly-latest-json \
    --run-id "${RUN_ID}" \
    --date-stamp "${DATE_STAMP}" \
    --baseline-id "${BASELINE_ID}" \
    --minio-mode "${WEEKLY_MINIO_MODE}" \
    --minio-name "${MINIO_FINAL_NAME}" --minio-meta "${MINIO_META}" \
    --redis-name "${REDIS_FINAL_NAME}" --redis-meta "${REDIS_META}" \
    --var-name "${VAR_FINAL_NAME}" --var-meta "${VAR_META}" \
    --out "${WEEKLY_LATEST_LOCAL}"

BASELINE_MANIFEST_REMOTE_NAME="baseline-${BASELINE_ID}.json"
RAND_BL_MANIFEST="$(openssl rand -hex 6)"
STAGED_BASELINE_MANIFEST_REMOTE="${REMOTE_WEEK_DEST}/${BASELINE_MANIFEST_REMOTE_NAME}.partial-${RAND_BL_MANIFEST}"
FINAL_BASELINE_MANIFEST_REMOTE="${REMOTE_WEEK_DEST}/${BASELINE_MANIFEST_REMOTE_NAME}"
CURRENT_PARTIAL_OBJ="${STAGED_BASELINE_MANIFEST_REMOTE}"
if ! rclone_noproxy rcat "${STAGED_BASELINE_MANIFEST_REMOTE}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" \
    --log-file "${RCLONE_LOG_FILE}" \
    --log-level INFO < "${WEEKLY_LATEST_LOCAL}" 2>"${RUN_TMP_DIR}/rcat-weekly-baseline-manifest.err"; then
    delete_partial_remote "${STAGED_BASELINE_MANIFEST_REMOTE}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "WEEKLY_MANIFEST_UPLOAD_FAILED" "Failed uploading immutable weekly manifest ${BASELINE_MANIFEST_REMOTE_NAME}"
fi
if ! rclone_noproxy moveto "${STAGED_BASELINE_MANIFEST_REMOTE}" "${FINAL_BASELINE_MANIFEST_REMOTE}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" \
    --log-file "${RCLONE_LOG_FILE}" \
    --log-level INFO 2>"${RUN_TMP_DIR}/moveto-weekly-baseline-manifest.err"; then
    delete_partial_remote "${STAGED_BASELINE_MANIFEST_REMOTE}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "WEEKLY_MANIFEST_MOVETO_FAILED" "Failed promoting immutable weekly manifest ${BASELINE_MANIFEST_REMOTE_NAME}"
fi
CURRENT_PARTIAL_OBJ=""

RAND_LATEST="$(openssl rand -hex 6)"
STAGED_WEEKLY_LATEST="${REMOTE_WEEK_DEST}/LATEST.json.partial-${RAND_LATEST}"
FINAL_WEEKLY_LATEST="${REMOTE_WEEK_DEST}/LATEST.json"
CURRENT_PARTIAL_OBJ="${STAGED_WEEKLY_LATEST}"
if ! rclone_noproxy rcat "${STAGED_WEEKLY_LATEST}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" \
    --log-file "${RCLONE_LOG_FILE}" \
    --log-level INFO < "${WEEKLY_LATEST_LOCAL}" 2>"${RUN_TMP_DIR}/rcat-weekly-latest.err"; then
    delete_partial_remote "${STAGED_WEEKLY_LATEST}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "WEEKLY_LATEST_UPLOAD_FAILED" "Failed uploading weekly LATEST.json for ${BASELINE_ID}"
fi
if ! rclone_noproxy moveto "${STAGED_WEEKLY_LATEST}" "${FINAL_WEEKLY_LATEST}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" \
    --log-file "${RCLONE_LOG_FILE}" \
    --log-level INFO 2>"${RUN_TMP_DIR}/moveto-weekly-latest.err"; then
    delete_partial_remote "${STAGED_WEEKLY_LATEST}"
    CURRENT_PARTIAL_OBJ=""
    fail_with_alert "WEEKLY_LATEST_MOVETO_FAILED" "Failed promoting weekly LATEST.json for ${BASELINE_ID}"
fi
CURRENT_PARTIAL_OBJ=""

if [[ "${WEEKLY_MINIO_MODE}" == "versioned" ]] && [[ -n "${STAGED_BASELINE_MANIFEST}" ]] && [[ -f "${STAGED_BASELINE_MANIFEST}" ]]; then
    mkdir -p "${SETS_DIR}"
    if ! PUBLISHED_BL="$("${PY_BIN}" "${OFFSITE_SYNC_PY}" publish-weekly-baseline-pointer \
        --staged-manifest "${STAGED_BASELINE_MANIFEST}" \
        --sets-dir "${SETS_DIR}" \
        --baseline-id "${BASELINE_ID}" 2>"${RUN_TMP_DIR}/pub-bl.err")"; then
        PUB_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/pub-bl.err" 2>/dev/null || true)"
        fail_with_alert "BASELINE_POINTER_FAILED" "Failed publishing local weekly baseline pointer: ${PUB_ERR}"
    fi
    log_msg "INFO" "Published verified weekly baseline pointer: ${BASELINE_ID} (${PUBLISHED_BL})"
fi

# Prune Google Drive weekly/ keeping DRIVE_RETAIN_WEEKS while exempting any baseline_id
# referenced by retained daily db/*/LATEST.json files (§3.1 step 4).
# Fail closed: if listing daily dirs or reading ANY daily LATEST.json fails, abort pruning completely.
REFERENCED_BASELINES_FILE="${RUN_TMP_DIR}/referenced-baselines.txt"
: > "${REFERENCED_BASELINES_FILE}"
REMOTE_DRIVE_BASE="${RCLONE_REMOTE}:${RCLONE_DRIVE_DIR}"
REMOTE_DB_BASE="${REMOTE_DRIVE_BASE}/db"
DEP_SCAN_OK=1

TOP_DIRS=""
if ! TOP_DIRS="$(rclone_noproxy lsf --dirs-only "${REMOTE_DRIVE_BASE}/" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsf-top.err")"; then
    DEP_SCAN_OK=0
    log_msg "WARN" "PRUNE_DRIVE aborted: failed listing ${REMOTE_DRIVE_BASE}/"
fi

if [[ "${DEP_SCAN_OK}" -eq 1 ]] && printf '%s\n' "${TOP_DIRS}" | grep -xE 'db/?' >/dev/null; then
    DAILY_DIRS=""
    if ! DAILY_DIRS="$(rclone_noproxy lsf --dirs-only "${REMOTE_DB_BASE}/" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsf-db.err")"; then
        DEP_SCAN_OK=0
        log_msg "WARN" "PRUNE_DRIVE aborted: failed listing ${REMOTE_DB_BASE}/"
    fi
    if [[ "${DEP_SCAN_OK}" -eq 1 ]] && [[ -n "${DAILY_DIRS}" ]]; then
        while IFS= read -r ddir; do
            ddir="${ddir%/}"
            [[ -n "${ddir}" ]] || continue
            DAY_FILES=""
            if ! DAY_FILES="$(rclone_noproxy lsf --files-only "${REMOTE_DB_BASE}/${ddir}/" \
                --cache-dir "${RCLONE_CACHE_DIR}" \
                --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsf-day.err")"; then
                DEP_SCAN_OK=0
                log_msg "WARN" "PRUNE_DRIVE aborted: failed listing ${REMOTE_DB_BASE}/${ddir}/"
                break
            fi
            if printf '%s\n' "${DAY_FILES}" | grep -x 'LATEST.json' >/dev/null; then
                LATEST_RAW=""
                if ! LATEST_RAW="$(rclone_noproxy cat "${REMOTE_DB_BASE}/${ddir}/LATEST.json" \
                    --cache-dir "${RCLONE_CACHE_DIR}" \
                    --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/cat-latest.err")"; then
                    DEP_SCAN_OK=0
                    log_msg "WARN" "PRUNE_DRIVE aborted: failed reading ${REMOTE_DB_BASE}/${ddir}/LATEST.json"
                    break
                fi
                BFIELDS=""
                if ! BFIELDS="$(printf '%s' "${LATEST_RAW}" | "${PY_BIN}" "${OFFSITE_SYNC_PY}" read-json-field --field baseline_id --field baseline_dir 2>"${RUN_TMP_DIR}/parse-latest.err")"; then
                    DEP_SCAN_OK=0
                    log_msg "WARN" "PRUNE_DRIVE aborted: failed parsing ${REMOTE_DB_BASE}/${ddir}/LATEST.json"
                    break
                fi
                while IFS= read -r bref; do
                    [[ -n "${bref}" ]] && printf '%s\n' "${bref}" >> "${REFERENCED_BASELINES_FILE}"
                done <<EOF
${BFIELDS}
EOF
            fi
        done <<EOF
${DAILY_DIRS}
EOF
    fi
fi

REMOTE_WEEKLY_BASE="${REMOTE_DRIVE_BASE}/weekly"
if [[ "${DEP_SCAN_OK}" -eq 1 ]]; then
    REMOTE_DIRS=""
    if ! REMOTE_DIRS="$(rclone_noproxy lsf --dirs-only "${REMOTE_WEEKLY_BASE}/" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsf-weekly.err")"; then
        log_msg "WARN" "PRUNE_DRIVE aborted: failed listing ${REMOTE_WEEKLY_BASE}/"
        REMOTE_DIRS=""
    fi
    if [[ -n "${REMOTE_DIRS}" ]]; then
        PRUNE_DRIVE_NAMES="$(printf '%s\n' "${REMOTE_DIRS}" | "${PY_BIN}" "${OFFSITE_SYNC_PY}" select-weekly-prune \
            --retain "${DRIVE_RETAIN_WEEKS}" \
            --referenced-file "${REFERENCED_BASELINES_FILE}")"
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
else
    log_msg "WARN" "PRUNE_DRIVE skipped due to incomplete daily baseline dependency scan; zero remote dirs deleted"
fi

log_msg "INFO" "OK weekly streaming backup complete: drive=${REMOTE_WEEK_DEST} baseline=${BASELINE_ID}"
finish_backup
