#!/usr/bin/env bash
# Everflow daily offsite backup (§3.1 step 4):
# - Backup-set mode (when latest-verified-set.json exists): reads latest-verified-set.json,
#   builds minio-delta-<run_id>.tar.gz against weekly baseline via prod/offsite_sync.py,
#   encrypts & uploads .dump, asr-jobs archive, minio-manifest, and minio-delta as .staged/.partial,
#   verifies remote size & MD5, atomically moveto-promotes all artifacts, and publishes
#   db/<YYYY-MM-DD>/LATEST.json.
# - Legacy single-dump fallback (when latest-verified-set.json is absent): streams newest
#   dalton-pg-*.dump encrypted to db/<YYYY-MM-DD>/<dump>.enc with size + MD5 verification.
# Never falls back to internal /tmp; all rclone calls unset proxy env vars (B8b).

SCRIPT_NAME="dalton_db_offsite"
PROD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_DIR="$(cd "${PROD_DIR}/.." && pwd)"
OFFSITE_SYNC_PY="${PROD_DIR}/offsite_sync.py"
# shellcheck source=scripts/dalton/ops/_common.sh
source "${OPS_DIR}/_common.sh"

RUN_TMP_DIR=""
CURRENT_PARTIAL_OBJ=""
RCLONE_LOG_FILE=""
RCLONE_CACHE_DIR=""
EXT_TMP_ROOT=""
STAGED_REMOTES=()

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
    if [[ "${#STAGED_REMOTES[@]}" -gt 0 ]]; then
        local obj=""
        for obj in "${STAGED_REMOTES[@]}"; do
            delete_partial_remote "${obj}"
        done
        STAGED_REMOTES=()
    fi
    if [[ -n "${RUN_TMP_DIR:-}" ]] && [[ -d "${RUN_TMP_DIR}" ]]; then
        rm -rf "${RUN_TMP_DIR}" 2>/dev/null || true
    fi
}
install_failure_trap cleanup

load_offsite_backup_env

EXT_MOUNT_ROOT="${DALTON_EXTERNAL_MOUNT_ROOT:-/Volumes/EveSSD}"
BACKUP_ROOT="${DALTON_BACKUP_ROOT:-${EXT_MOUNT_ROOT}/Dalton/backups}"
PG_BACKUP_DIR="${DALTON_PG_BACKUP_DIR:-${BACKUP_ROOT}/pg}"
EXT_TMP_ROOT="${DALTON_EXTERNAL_TMP_ROOT:-${BACKUP_ROOT}/.tmp}"
SETS_DIR="${DALTON_BACKUP_SETS_DIR:-${BACKUP_ROOT}/sets}"
MIRROR_OBJECTS_DIR="${DALTON_MINIO_MIRROR_OBJECTS_DIR:-${BACKUP_ROOT}/minio-mirror/objects}"
LATEST_VERIFIED_SET="${DALTON_LATEST_VERIFIED_SET:-${SETS_DIR}/latest-verified-set.json}"

RCLONE_REMOTE="${DALTON_RCLONE_REMOTE:-gdrive}"
RCLONE_DRIVE_DIR="${DALTON_RCLONE_DRIVE_DIR:-DaltonBackup}"
DB_DRIVE_RETAIN="${DALTON_DB_RETAIN_COUNT:-${DALTON_DB_DRIVE_RETAIN:-7}}"
RCLONE_CACHE_DIR="${DALTON_RCLONE_CACHE_DIR:-${BACKUP_ROOT}/.rclone-cache}"
RCLONE_LOG_DIR="${DALTON_RCLONE_LOG_DIR:-${BACKUP_ROOT}/.rclone-logs}"

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
RUN_ID="$(date '+%Y%m%d-%H%M%S')-$(openssl rand -hex 3)"
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

if ! RCLONE_STATUS="$("${PY_BIN}" "${BACKUP_LIB}" check-rclone --remote "${RCLONE_REMOTE}" 2>&1)"; then
    fail_with_alert "RCLONE_NOT_CONFIGURED" "rclone remote unavailable for daily DB upload: ${RCLONE_STATUS}"
fi

ensure_backup_key || exit 1

RCLONE_LOG_FILE="${RCLONE_LOG_DIR}/rclone-db-${DATE_STAMP}.log"
REMOTE_DB_BASE="${RCLONE_REMOTE}:${RCLONE_DRIVE_DIR}/db"
REMOTE_DAY_DIR="${REMOTE_DB_BASE}/${DATE_STAMP}"

upload_and_verify_encrypted_file() {
    local src_file="$1"
    local remote_staged="$2"
    local label="$3"
    local meta_out="$4"

    local plain_size expected_enc staged_name
    plain_size="$(wc -c < "${src_file}" | tr -d ' ')"
    expected_enc="$("${PY_BIN}" "${BACKUP_LIB}" expected-enc-size --plain-size "${plain_size}")"
    staged_name="$(basename "${remote_staged}")"

    CURRENT_PARTIAL_OBJ="${remote_staged}"
    suspend_err_trap
    set +e
    openssl enc -aes-256-cbc -pbkdf2 -salt -in "${src_file}" -pass "file:${DALTON_BACKUP_KEY_FILE}" 2>"${RUN_TMP_DIR}/openssl-${label}.err" \
        | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${meta_out}" 2>"${RUN_TMP_DIR}/hash-${label}.err" \
        | rclone_noproxy rcat "${remote_staged}" \
            --size "${expected_enc}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" \
            --log-file "${RCLONE_LOG_FILE}" \
            --log-level INFO 2>"${RUN_TMP_DIR}/rcat-${label}.err"
    local pipe_status=("${PIPESTATUS[@]}")
    set -e
    restore_err_trap

    if [[ "${pipe_status[0]:-1}" -ne 0 ]] || [[ "${pipe_status[1]:-1}" -ne 0 ]] || [[ "${pipe_status[2]:-1}" -ne 0 ]]; then
        local pipe_err
        pipe_err="$(tr '\n' ' ' < "${RUN_TMP_DIR}/openssl-${label}.err" "${RUN_TMP_DIR}/hash-${label}.err" "${RUN_TMP_DIR}/rcat-${label}.err" 2>/dev/null || true)"
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "DB_UPLOAD_FAILED" "Streaming upload failed for ${label} (pipe=${pipe_status[*]}): ${pipe_err}"
    fi

    if [[ ! -s "${meta_out}" ]]; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "STREAM_META_MISSING" "Missing ciphertext stream metadata for ${remote_staged}"
    fi

    local lsjson_out=""
    if ! lsjson_out="$(rclone_noproxy lsjson "${remote_staged}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsjson-${label}.err")"; then
        local ls_err
        ls_err="$(tr '\n' ' ' < "${RUN_TMP_DIR}/lsjson-${label}.err" 2>/dev/null || true)"
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "DB_VERIFY_FAILED" "rclone lsjson failed for ${remote_staged}: ${ls_err}"
    fi

    if ! SIZE_MSG="$(printf '%s' "${lsjson_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-size --filename "${staged_name}" --expected-size "${expected_enc}" 2>&1)"; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "DB_SIZE_MISMATCH" "${SIZE_MSG}"
    fi

    if ! META_SIZE_MSG="$(printf '%s' "${lsjson_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-size --filename "${staged_name}" --meta-file "${meta_out}" 2>&1)"; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "DB_SIZE_MISMATCH" "${META_SIZE_MSG}"
    fi

    local hashsum_out=""
    if ! hashsum_out="$(rclone_noproxy hashsum md5 "${remote_staged}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/hashsum-${label}.err")"; then
        local hs_err
        hs_err="$(tr '\n' ' ' < "${RUN_TMP_DIR}/hashsum-${label}.err" 2>/dev/null || true)"
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "DB_HASHSUM_FAILED" "rclone hashsum md5 failed for ${remote_staged}: ${hs_err}"
    fi

    if ! HASH_MSG="$(printf '%s\n' "${hashsum_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-hash --filename "${staged_name}" --meta-file "${meta_out}" 2>&1)"; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "DB_HASH_MISMATCH" "${HASH_MSG}"
    fi

    CURRENT_PARTIAL_OBJ=""
}

prune_remote_daily_dirs() {
    local remote_dirs=""
    if ! remote_dirs="$(rclone_noproxy lsf --dirs-only "${REMOTE_DB_BASE}/" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsf-db-prune.err")"; then
        local lsf_err
        lsf_err="$(tr '\n' ' ' < "${RUN_TMP_DIR}/lsf-db-prune.err" 2>/dev/null || true)"
        log_msg "WARN" "PRUNE_DB_DRIVE aborted: failed listing ${REMOTE_DB_BASE}/ (${lsf_err})"
        return 0
    fi
    if [[ -n "${remote_dirs}" ]]; then
        local prune_names
        prune_names="$(printf '%s\n' "${remote_dirs}" | "${PY_BIN}" "${BACKUP_LIB}" select-prune-names --retain "${DB_DRIVE_RETAIN}")"
        if [[ -n "${prune_names}" ]]; then
            local dname=""
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
${prune_names}
EOF
        fi
    fi
}

# ---------------------------------------------------------------------------
# Mode A: Consistent backup-set mode (when latest-verified-set.json exists)
# ---------------------------------------------------------------------------
if [[ -f "${LATEST_VERIFIED_SET}" ]] && [[ "${DALTON_DB_OFFSITE_LEGACY_ONLY:-0}" != "1" ]]; then
    READ_SET_ARGS=(--set-file "${LATEST_VERIFIED_SET}" --expected-date "${DATE_STAMP}")
    if [[ "${DALTON_DB_OFFSITE_ALLOW_STALE:-0}" == "1" ]]; then
        READ_SET_ARGS+=(--allow-stale)
    fi
    SET_INFO="$("${PY_BIN}" "${OFFSITE_SYNC_PY}" read-verified-set \
        "${READ_SET_ARGS[@]}" 2>"${RUN_TMP_DIR}/set.err")" || {
        SET_ERR="$(cat "${RUN_TMP_DIR}/set.err" 2>/dev/null || true)"
        fail_with_alert "BACKUP_SET_INVALID" "Invalid or stale latest-verified-set.json (${LATEST_VERIFIED_SET}): ${SET_ERR}"
    }

    BACKUP_SET_ID="$(printf '%s\n' "${SET_INFO}" | sed -n '1p')"
    DUMP_PATH="$(printf '%s\n' "${SET_INFO}" | sed -n '2p')"
    MANIFEST_PATH="$(printf '%s\n' "${SET_INFO}" | sed -n '3p')"
    ASR_ARCHIVE_PATH="$(printf '%s\n' "${SET_INFO}" | sed -n '4p')"

    if [[ ! -s "${DUMP_PATH}" ]] || [[ ! -s "${MANIFEST_PATH}" ]]; then
        fail_with_alert "BACKUP_SET_MISSING_FILES" "Backup set ${BACKUP_SET_ID} is missing dump or manifest"
    fi

    DELTA_TAR="${RUN_TMP_DIR}/minio-delta-${RUN_ID}.tar.gz"
    DELTA_JSON="$("${PY_BIN}" "${OFFSITE_SYNC_PY}" build-delta \
        --daily-manifest "${MANIFEST_PATH}" \
        --sets-dir "${SETS_DIR}" \
        --mirror-objects-dir "${MIRROR_OBJECTS_DIR}" \
        --out-tar "${DELTA_TAR}" 2>"${RUN_TMP_DIR}/delta.err")" || {
        DELTA_ERR="$(tr '\n' ' ' < "${RUN_TMP_DIR}/delta.err" 2>/dev/null || true)"
        fail_with_alert "DELTA_BUILD_FAILED" "Failed building minio delta for set ${BACKUP_SET_ID}: ${DELTA_ERR}"
    }
    BASELINE_ID="$(printf '%s' "${DELTA_JSON}" | "${PY_BIN}" "${OFFSITE_SYNC_PY}" read-json-field --field baseline_id)"

    # Estimate total encrypted bytes for 2x Drive free-space pre-check
    TOTAL_ENC_BYTES=0
    for fpath in "${DUMP_PATH}" "${MANIFEST_PATH}" "${DELTA_TAR}" "${ASR_ARCHIVE_PATH}"; do
        [[ -n "${fpath}" ]] && [[ -f "${fpath}" ]] || continue
        psz="$(wc -c < "${fpath}" | tr -d ' ')"
        esz="$("${PY_BIN}" "${BACKUP_LIB}" expected-enc-size --plain-size "${psz}")"
        TOTAL_ENC_BYTES=$((TOTAL_ENC_BYTES + esz))
    done

    ABOUT_JSON="$(rclone_noproxy about "${RCLONE_REMOTE}:" --json \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>/dev/null || true)"
    if [[ -n "${ABOUT_JSON}" ]]; then
        if ! SPACE_MSG="$(printf '%s' "${ABOUT_JSON}" | "${PY_BIN}" "${BACKUP_LIB}" check-drive-space --file-size "${TOTAL_ENC_BYTES}" --multiplier 2 2>&1)"; then
            fail_with_alert "DRIVE_LOW_SPACE" "Skipped daily backup-set upload for ${BACKUP_SET_ID}: ${SPACE_MSG}"
        fi
        log_msg "INFO" "${SPACE_MSG}"
    fi

    RAND_TAG="$(openssl rand -hex 6)"
    DUMP_BASE="$(basename "${DUMP_PATH}")"
    MANIFEST_BASE="$(basename "${MANIFEST_PATH}")"
    DELTA_BASE="$(basename "${DELTA_TAR}")"

    # Every remote artifact carries unique RUN_ID so same-day reruns never overwrite old artifacts
    # referenced by a previously published LATEST.json.
    FINAL_DUMP_NAME="${DUMP_BASE}.${RUN_ID}.enc"
    FINAL_MANIFEST_NAME="${MANIFEST_BASE}.${RUN_ID}.enc"
    FINAL_DELTA_NAME="${DELTA_BASE}.enc"

    STAGED_DUMP="${REMOTE_DAY_DIR}/${FINAL_DUMP_NAME}.staged-${RAND_TAG}"
    FINAL_DUMP="${REMOTE_DAY_DIR}/${FINAL_DUMP_NAME}"
    STAGED_MANIFEST="${REMOTE_DAY_DIR}/${FINAL_MANIFEST_NAME}.staged-${RAND_TAG}"
    FINAL_MANIFEST="${REMOTE_DAY_DIR}/${FINAL_MANIFEST_NAME}"
    STAGED_DELTA="${REMOTE_DAY_DIR}/${FINAL_DELTA_NAME}.staged-${RAND_TAG}"
    FINAL_DELTA="${REMOTE_DAY_DIR}/${FINAL_DELTA_NAME}"

    upload_and_verify_encrypted_file "${DUMP_PATH}" "${STAGED_DUMP}" "dump" "${RUN_TMP_DIR}/dump.meta.json"
    STAGED_REMOTES+=("${STAGED_DUMP}")
    upload_and_verify_encrypted_file "${MANIFEST_PATH}" "${STAGED_MANIFEST}" "manifest" "${RUN_TMP_DIR}/manifest.meta.json"
    STAGED_REMOTES+=("${STAGED_MANIFEST}")
    upload_and_verify_encrypted_file "${DELTA_TAR}" "${STAGED_DELTA}" "delta" "${RUN_TMP_DIR}/delta.meta.json"
    STAGED_REMOTES+=("${STAGED_DELTA}")

    STAGED_ASR=""
    FINAL_ASR=""
    FINAL_ASR_NAME=""
    if [[ -n "${ASR_ARCHIVE_PATH}" ]] && [[ -f "${ASR_ARCHIVE_PATH}" ]]; then
        ASR_BASE="$(basename "${ASR_ARCHIVE_PATH}")"
        FINAL_ASR_NAME="${ASR_BASE}.${RUN_ID}.enc"
        STAGED_ASR="${REMOTE_DAY_DIR}/${FINAL_ASR_NAME}.staged-${RAND_TAG}"
        FINAL_ASR="${REMOTE_DAY_DIR}/${FINAL_ASR_NAME}"
        upload_and_verify_encrypted_file "${ASR_ARCHIVE_PATH}" "${STAGED_ASR}" "asr" "${RUN_TMP_DIR}/asr.meta.json"
        STAGED_REMOTES+=("${STAGED_ASR}")
    fi

    # Promote all verified staged objects to their immutable RUN_ID names before flipping LATEST.json
    promote_one() {
        local src="$1"
        local dst="$2"
        local label="$3"
        if ! rclone_noproxy moveto "${src}" "${dst}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" \
            --log-file "${RCLONE_LOG_FILE}" \
            --log-level INFO 2>"${RUN_TMP_DIR}/moveto-${label}.err"; then
            local mv_err
            mv_err="$(tr '\n' ' ' < "${RUN_TMP_DIR}/moveto-${label}.err" 2>/dev/null || true)"
            fail_with_alert "DB_MOVETO_FAILED" "rclone moveto failed (${src} -> ${dst}): ${mv_err}"
        fi
    }

    promote_one "${STAGED_DUMP}" "${FINAL_DUMP}" "dump"
    promote_one "${STAGED_MANIFEST}" "${FINAL_MANIFEST}" "manifest"
    promote_one "${STAGED_DELTA}" "${FINAL_DELTA}" "delta"
    if [[ -n "${STAGED_ASR}" ]]; then
        promote_one "${STAGED_ASR}" "${FINAL_ASR}" "asr"
    fi
    STAGED_REMOTES=()

    # Build and publish db/<YYYY-MM-DD>/LATEST.json ONLY after every immutable artifact is promoted
    ARTIFACTS_JSON="${RUN_TMP_DIR}/artifacts.json"
    "${PY_BIN}" "${OFFSITE_SYNC_PY}" write-artifacts-json \
        --dump-name "${FINAL_DUMP_NAME}" --dump-meta "${RUN_TMP_DIR}/dump.meta.json" \
        --manifest-name "${FINAL_MANIFEST_NAME}" --manifest-meta "${RUN_TMP_DIR}/manifest.meta.json" \
        --delta-name "${FINAL_DELTA_NAME}" --delta-meta "${RUN_TMP_DIR}/delta.meta.json" \
        --asr-name "${FINAL_ASR_NAME}" --asr-meta "${RUN_TMP_DIR}/asr.meta.json" \
        --out "${ARTIFACTS_JSON}"

    LATEST_LOCAL="${RUN_TMP_DIR}/LATEST.json"
    "${PY_BIN}" "${OFFSITE_SYNC_PY}" build-latest-json \
        --run-id "${RUN_ID}" \
        --date-stamp "${DATE_STAMP}" \
        --backup-set-id "${BACKUP_SET_ID}" \
        --baseline-id "${BASELINE_ID}" \
        --artifacts-json "${ARTIFACTS_JSON}" \
        --out "${LATEST_LOCAL}"

    STAGED_LATEST="${REMOTE_DAY_DIR}/LATEST.json.staged-${RAND_TAG}"
    FINAL_LATEST="${REMOTE_DAY_DIR}/LATEST.json"
    CURRENT_PARTIAL_OBJ="${STAGED_LATEST}"
    if ! rclone_noproxy rcat "${STAGED_LATEST}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" \
        --log-file "${RCLONE_LOG_FILE}" \
        --log-level INFO < "${LATEST_LOCAL}" 2>"${RUN_TMP_DIR}/rcat-latest.err"; then
        delete_partial_remote "${STAGED_LATEST}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "LATEST_UPLOAD_FAILED" "Failed uploading LATEST.json for set ${BACKUP_SET_ID}"
    fi
    promote_one "${STAGED_LATEST}" "${FINAL_LATEST}" "latest"
    CURRENT_PARTIAL_OBJ=""

    prune_remote_daily_dirs
    log_msg "INFO" "OK daily backup-set offsite upload complete: set=${BACKUP_SET_ID} run=${RUN_ID} baseline=${BASELINE_ID:-none} latest=${FINAL_LATEST}"
    finish_backup
fi

# ---------------------------------------------------------------------------
# Mode B: Legacy single-dump fallback (when latest-verified-set.json is absent)
# ---------------------------------------------------------------------------
if ! LATEST_DUMP="$("${PY_BIN}" "${BACKUP_LIB}" latest-pg-dump --pg-dir "${PG_BACKUP_DIR}" 2>"${RUN_TMP_DIR}/pg.err")"; then
    PG_ERR="$(cat "${RUN_TMP_DIR}/pg.err" 2>/dev/null || true)"
    fail_with_alert "PG_DUMP_MISSING" "No complete daily Postgres dump available in ${PG_BACKUP_DIR}: ${PG_ERR}"
fi

PLAIN_SIZE="$(wc -c < "${LATEST_DUMP}" | tr -d ' ')"
EXPECTED_ENC_SIZE="$("${PY_BIN}" "${BACKUP_LIB}" expected-enc-size --plain-size "${PLAIN_SIZE}")"

DUMP_BASE="$(basename "${LATEST_DUMP}")"
REMOTE_OBJ="${REMOTE_DAY_DIR}/${DUMP_BASE}.${RUN_ID}.enc"
RAND_DB="$(openssl rand -hex 6)"
PARTIAL_OBJ="${REMOTE_OBJ}.partial-${RAND_DB}"
HASH_META="${RUN_TMP_DIR}/db.meta.json"

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
upload_and_verify_encrypted_file "${LATEST_DUMP}" "${PARTIAL_OBJ}" "db" "${HASH_META}"

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

prune_remote_daily_dirs
log_msg "INFO" "OK daily DB offsite backup complete: ${REMOTE_OBJ} (${SIZE_MSG}; ${HASH_MSG})"
finish_backup
