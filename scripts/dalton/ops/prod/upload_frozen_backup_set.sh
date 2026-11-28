#!/usr/bin/env bash
# Encrypt and upload a verified frozen backup set to Google Drive under
# <remote>:<drive_dir>/frozen/<tag>-<set_id>/ (e.g. manual-pre-blob-inline-<set>/).
# Default mode is dry-run (prints files and target paths only); pass --apply to upload.
# Reuses ~/.config/dalton/offsite-backup.env, _common.sh, backup_lib.py, and offsite_sync.py.
# Compatible with macOS Bash 3.2 under `set -euo pipefail`.

# Disable shell command tracing before loading env/tokens so secrets never leak to stderr.
{ set +x; set +v; } 2>/dev/null

SCRIPT_NAME="upload_frozen_backup_set"
PROD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_DIR="$(cd "${PROD_DIR}/.." && pwd)"
OFFSITE_SYNC_PY="${PROD_DIR}/offsite_sync.py"
# shellcheck source=scripts/dalton/ops/_common.sh
source "${OPS_DIR}/_common.sh"

usage() {
    cat <<'EOF'
Usage: upload_frozen_backup_set.sh [--dry-run | --apply] [--set-id <backup_set_id>] [<backup_set_id>]

Encrypts and uploads a verified frozen backup set (dump, asr-jobs archive,
backup-set/minio-manifest JSONs, and object archive if present) to
<DALTON_RCLONE_REMOTE>:<DALTON_RCLONE_DRIVE_DIR>/frozen/<tag>-<set_id>/.

Default mode is dry-run; pass --apply to encrypt, upload, and verify.
Re-running with --apply skips files already verified under the current key.
EOF
}

APPLY=0
SET_ID=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --apply)
            APPLY=1
            shift
            ;;
        --dry-run)
            APPLY=0
            shift
            ;;
        --set-id)
            if [[ $# -lt 2 ]] || [[ -z "${2:-}" ]]; then
                echo "Error: --set-id requires a value" >&2
                usage >&2
                exit 2
            fi
            if [[ -n "${SET_ID}" ]]; then
                echo "Error: backup set ID specified more than once" >&2
                exit 2
            fi
            SET_ID="$2"
            shift 2
            ;;
        --set-id=*)
            if [[ -n "${SET_ID}" ]]; then
                echo "Error: backup set ID specified more than once" >&2
                exit 2
            fi
            SET_ID="${1#--set-id=}"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "Error: unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            if [[ -n "${SET_ID}" ]]; then
                echo "Error: unexpected extra argument: $1" >&2
                usage >&2
                exit 2
            fi
            SET_ID="$1"
            shift
            ;;
    esac
done

if [[ -z "${SET_ID}" ]]; then
    echo "Error: backup set ID is required" >&2
    usage >&2
    exit 2
fi

if [[ ! "${SET_ID}" =~ ^[A-Za-z0-9._-]{1,128}$ ]] || [[ "${SET_ID}" == "." ]] || [[ "${SET_ID}" == ".." ]] || [[ "${SET_ID}" == *".."* ]]; then
    echo "Error: invalid backup set ID: ${SET_ID}" >&2
    exit 2
fi

load_offsite_backup_env
# Suppress rclone HTTP/auth dump and debug logging inherited from shell or env file.
unset RCLONE_DUMP RCLONE_DUMP_HEADERS RCLONE_DUMP_BODIES RCLONE_LOG_LEVEL RCLONE_VERBOSE

EXT_MOUNT_ROOT="${DALTON_EXTERNAL_MOUNT_ROOT:-/Volumes/EveSSD}"
BACKUP_ROOT="${DALTON_BACKUP_ROOT:-${EXT_MOUNT_ROOT}/Dalton/backups}"
EXT_TMP_ROOT="${DALTON_EXTERNAL_TMP_ROOT:-${BACKUP_ROOT}/.tmp}"
SETS_DIR="${DALTON_BACKUP_SETS_DIR:-${BACKUP_ROOT}/sets}"
MIRROR_OBJECTS_DIR="${DALTON_MINIO_MIRROR_OBJECTS_DIR:-${BACKUP_ROOT}/minio-mirror/objects}"

RCLONE_REMOTE="${DALTON_RCLONE_REMOTE:-gdrive}"
RCLONE_DRIVE_DIR="${DALTON_RCLONE_DRIVE_DIR:-DaltonBackup}"
RCLONE_CACHE_DIR="${DALTON_RCLONE_CACHE_DIR:-${BACKUP_ROOT}/.rclone-cache}"
RCLONE_LOG_DIR="${DALTON_RCLONE_LOG_DIR:-${BACKUP_ROOT}/.rclone-logs}"
PACK_SPACE_MULTIPLIER="${DALTON_FROZEN_PACK_SPACE_MULTIPLIER:-1.1}"

PY_BIN="$(resolve_python_bin)"

# ---------------------------------------------------------------------------
# Default mode: read-only dry-run (no encryption, no rclone calls, no writes)
# ---------------------------------------------------------------------------
if [[ "${APPLY}" -ne 1 ]]; then
    "${PY_BIN}" "${OFFSITE_SYNC_PY}" plan-frozen-set \
        --set-id "${SET_ID}" \
        --backups-root "${BACKUP_ROOT}" \
        --sets-dir "${SETS_DIR}" \
        --mirror-objects-dir "${MIRROR_OBJECTS_DIR}" \
        --remote "${RCLONE_REMOTE}" \
        --drive-dir "${RCLONE_DRIVE_DIR}" \
        --dry-run
    exit $?
fi

# ---------------------------------------------------------------------------
# Apply mode: mount check, encrypt, upload, verify, promote, record manifest
# ---------------------------------------------------------------------------
RUN_TMP_DIR=""
RUN_KEY_FILE=""
CURRENT_PARTIAL_OBJ=""
RCLONE_LOG_FILE=""
TARGET_LOCK_DIR=""
TARGET_LOCK_HELD=0

pid_is_running() {
    local pid="$1"
    [[ "${pid}" =~ ^[0-9]+$ ]] && [[ "${pid}" -gt 0 ]] || return 1
    if kill -0 "${pid}" 2>/dev/null; then
        return 0
    fi
    local err
    err="$(kill -0 "${pid}" 2>&1 || true)"
    case "${err}" in
        *"Operation not permitted"*|*"Not owner"*) return 0 ;;
        *) return 1 ;;
    esac
}

release_target_lock() {
    if [[ "${TARGET_LOCK_HELD:-0}" == "1" ]] && [[ -n "${TARGET_LOCK_DIR:-}" ]]; then
        rm -rf "${TARGET_LOCK_DIR}" 2>/dev/null || true
        TARGET_LOCK_HELD=0
    fi
}

acquire_target_lock() {
    local remote_target="$1"
    local lock_key="$2"
    TARGET_LOCK_DIR="${EXT_TMP_ROOT}/frozen-target-${lock_key}.lock"

    if mkdir "${TARGET_LOCK_DIR}" 2>/dev/null; then
        TARGET_LOCK_HELD=1
        printf '%s\n' "$$" > "${TARGET_LOCK_DIR}/pid"
        printf '%s\n' "${remote_target}" > "${TARGET_LOCK_DIR}/target"
        return 0
    fi

    # Lock directory exists: read holder PID (with brief grace wait in case holder just created dir).
    local holder_pid=""
    if [[ -f "${TARGET_LOCK_DIR}/pid" ]]; then
        holder_pid="$(tr -dc '0-9' < "${TARGET_LOCK_DIR}/pid" 2>/dev/null || true)"
    fi
    if [[ -z "${holder_pid}" ]]; then
        sleep 0.2 2>/dev/null || true
        if [[ -f "${TARGET_LOCK_DIR}/pid" ]]; then
            holder_pid="$(tr -dc '0-9' < "${TARGET_LOCK_DIR}/pid" 2>/dev/null || true)"
        fi
    fi

    if [[ -n "${holder_pid}" ]] && pid_is_running "${holder_pid}"; then
        fail_with_alert "FROZEN_TARGET_LOCKED" "set=${RESOLVED_SET_ID:-${SET_ID}} target=${remote_target} holder_pid=${holder_pid} result=target_locked"
    fi

    # Stale lock (holder PID is no longer running): clean up and retry once.
    rm -rf "${TARGET_LOCK_DIR}" 2>/dev/null || true
    if mkdir "${TARGET_LOCK_DIR}" 2>/dev/null; then
        TARGET_LOCK_HELD=1
        printf '%s\n' "$$" > "${TARGET_LOCK_DIR}/pid"
        printf '%s\n' "${remote_target}" > "${TARGET_LOCK_DIR}/target"
        return 0
    fi

    fail_with_alert "FROZEN_TARGET_LOCKED" "set=${RESOLVED_SET_ID:-${SET_ID}} target=${remote_target} result=target_locked"
}

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
    if ! rclone_noproxy_nodump deletefile "${partial_obj}" \
        ${extra_args[@]+"${extra_args[@]}"} >/dev/null 2>&1; then
        log_msg "WARN" "file=$(basename "${partial_obj}") result=partial_cleanup_failed" || true
    fi
}

cleanup() {
    wait 2>/dev/null || true
    if [[ -n "${CURRENT_PARTIAL_OBJ:-}" ]]; then
        delete_partial_remote "${CURRENT_PARTIAL_OBJ}"
        CURRENT_PARTIAL_OBJ=""
    fi
    if [[ -n "${RUN_TMP_DIR:-}" ]] && [[ -e "${RUN_TMP_DIR}" ]]; then
        if ! rm -rf "${RUN_TMP_DIR}" 2>/dev/null || [[ -e "${RUN_TMP_DIR}" ]]; then
            log_msg "ERROR" "set=${SET_ID} result=tmp_cleanup_failed" || true
        fi
    fi
    release_target_lock
}
install_failure_trap cleanup

# Hard check: external drive must be mounted and all runtime/cache/log dirs must stay on it.
if ! MOUNT_ERR="$("${PY_BIN}" "${BACKUP_LIB}" verify-mount \
    --mount-root "${EXT_MOUNT_ROOT}" \
    --target-path "${EXT_TMP_ROOT}" "${RCLONE_CACHE_DIR}" "${RCLONE_LOG_DIR}" 2>&1)"; then
    fail_with_alert "EXTERNAL_DRIVE_UNMOUNTED" "set=${SET_ID} result=external_drive_unmounted"
fi

mkdir -p "${EXT_TMP_ROOT}" "${RCLONE_CACHE_DIR}" "${RCLONE_LOG_DIR}" \
    || fail_with_alert "EXTERNAL_DRIVE_NOT_WRITABLE" "set=${SET_ID} result=external_drive_not_writable"

DATE_STAMP="$(date '+%Y-%m-%d')"
RUN_TMP_DIR="$(mktemp -d "${EXT_TMP_ROOT}/frozen-run-${DATE_STAMP}.XXXXXX")"
chmod 700 "${RUN_TMP_DIR}"

if ! MOUNT_ERR="$("${PY_BIN}" "${BACKUP_LIB}" verify-mount \
    --mount-root "${EXT_MOUNT_ROOT}" \
    --target-path "${RUN_TMP_DIR}" 2>&1)"; then
    fail_with_alert "EXTERNAL_DRIVE_UNMOUNTED" "set=${SET_ID} result=external_drive_unmounted"
fi

export TMPDIR="${RUN_TMP_DIR}"
export TEMP="${RUN_TMP_DIR}"
export TMP="${RUN_TMP_DIR}"
export RCLONE_TEMP_DIR="${RUN_TMP_DIR}"
export RCLONE_CACHE_DIR="${RCLONE_CACHE_DIR}"

if ! RCLONE_STATUS="$("${PY_BIN}" "${BACKUP_LIB}" check-rclone --remote "${RCLONE_REMOTE}" 2>&1)"; then
    fail_with_alert "RCLONE_NOT_CONFIGURED" "set=${SET_ID} result=rclone_not_configured"
fi

# Frozen backup uploads must use an existing backup key, read once per run into a 0600 snapshot.
require_existing_backup_key || exit 1
RUN_KEY_FILE="${RUN_TMP_DIR}/run-backup.key"
if ! (umask 077 && cat < "${DALTON_BACKUP_KEY_FILE}" > "${RUN_KEY_FILE}" && chmod 600 "${RUN_KEY_FILE}"); then
    fail_with_alert "KEY_SNAPSHOT_FAILED" "set=${SET_ID} result=key_snapshot_failed"
fi
DALTON_BACKUP_KEY_FILE="${RUN_KEY_FILE}" require_existing_backup_key || exit 1

RCLONE_LOG_FILE="${RCLONE_LOG_DIR}/rclone-frozen-${DATE_STAMP}.log"
PLAN_JSON="${RUN_TMP_DIR}/plan.json"
MANIFEST_SNAPSHOT_JSON="${RUN_TMP_DIR}/minio-manifest-snapshot.json"
PLAN_ROWS="${RUN_TMP_DIR}/plan.rows"
PENDING_ROWS="${RUN_TMP_DIR}/pending.rows"
: > "${PENDING_ROWS}"

if ! "${PY_BIN}" "${OFFSITE_SYNC_PY}" plan-frozen-set \
    --set-id "${SET_ID}" \
    --backups-root "${BACKUP_ROOT}" \
    --sets-dir "${SETS_DIR}" \
    --mirror-objects-dir "${MIRROR_OBJECTS_DIR}" \
    --remote "${RCLONE_REMOTE}" \
    --drive-dir "${RCLONE_DRIVE_DIR}" \
    --manifest-snapshot-out "${MANIFEST_SNAPSHOT_JSON}" \
    --out "${PLAN_JSON}" 2>"${RUN_TMP_DIR}/plan.err"; then
    PLAN_ERR_REASON="$(tail -n 1 "${RUN_TMP_DIR}/plan.err" 2>/dev/null || true)"
    if [[ -n "${PLAN_ERR_REASON}" ]]; then
        printf 'Error: %s\n' "${PLAN_ERR_REASON}" >&2
    fi
    fail_with_alert "FROZEN_SET_INVALID" "set=${SET_ID} result=invalid_backup_set${PLAN_ERR_REASON:+ reason=${PLAN_ERR_REASON}}"
fi

PLAN_HEADER="$("${PY_BIN}" "${OFFSITE_SYNC_PY}" read-json-field \
    --file "${PLAN_JSON}" \
    --field backup_set_id \
    --field remote_dir \
    --field manifest_file \
    --field target_lock_key)"
RESOLVED_SET_ID="$(printf '%s\n' "${PLAN_HEADER}" | sed -n '1p')"
REMOTE_DIR="$(printf '%s\n' "${PLAN_HEADER}" | sed -n '2p')"
MANIFEST_LOCAL_PATH="$(printf '%s\n' "${PLAN_HEADER}" | sed -n '3p')"
TARGET_LOCK_KEY="$(printf '%s\n' "${PLAN_HEADER}" | sed -n '4p')"

"${PY_BIN}" "${OFFSITE_SYNC_PY}" frozen-plan-tsv --plan-json "${PLAN_JSON}" > "${PLAN_ROWS}"

# Serialize concurrent --apply runs against the same resolved remote target.
acquire_target_lock "${REMOTE_DIR}" "${TARGET_LOCK_KEY}"

# Load and decrypt existing remote MANIFEST.json.enc if present; fail hard on key mismatch or corruption.
REMOTE_MANIFEST_NAME="MANIFEST.json.enc"
REMOTE_MANIFEST_PATH="${REMOTE_DIR}/${REMOTE_MANIFEST_NAME}"
REMOTE_MANIFEST_ENC_LOCAL="${RUN_TMP_DIR}/remote-manifest.json.enc"
REMOTE_MANIFEST_LOCAL="${RUN_TMP_DIR}/remote-manifest.json"
: > "${REMOTE_MANIFEST_LOCAL}"

MANIFEST_LSJSON_FILE="${RUN_TMP_DIR}/remote-manifest-lsjson.json"
suspend_err_trap
set +e
rclone_noproxy_nodump lsjson "${REMOTE_MANIFEST_PATH}" \
    --cache-dir "${RCLONE_CACHE_DIR}" \
    --temp-dir "${RUN_TMP_DIR}" > "${MANIFEST_LSJSON_FILE}" 2>"${RUN_TMP_DIR}/remote-manifest-lsjson.err"
MANIFEST_LSJSON_RC=$?
set -e
restore_err_trap

if [[ "${MANIFEST_LSJSON_RC}" -eq 0 ]]; then
    suspend_err_trap
    set +e
    REMOTE_MANIFEST_SIZE="$("${PY_BIN}" "${OFFSITE_SYNC_PY}" probe-lsjson-file \
        --filename "${REMOTE_MANIFEST_NAME}" < "${MANIFEST_LSJSON_FILE}" 2>"${RUN_TMP_DIR}/manifest-probe.err")"
    PROBE_RC=$?
    set -e
    restore_err_trap

    if [[ "${PROBE_RC}" -eq 0 ]]; then
        if ! rclone_noproxy_nodump cat "${REMOTE_MANIFEST_PATH}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" > "${REMOTE_MANIFEST_ENC_LOCAL}" 2>"${RUN_TMP_DIR}/manifest-cat.err"; then
            fail_with_alert "FROZEN_MANIFEST_READ_FAILED" "file=${REMOTE_MANIFEST_NAME} result=cat_failed"
        fi
        DOWNLOADED_MANIFEST_SIZE="$(wc -c < "${REMOTE_MANIFEST_ENC_LOCAL}" | tr -d ' ')"
        if [[ "${DOWNLOADED_MANIFEST_SIZE}" != "${REMOTE_MANIFEST_SIZE}" ]]; then
            fail_with_alert "FROZEN_MANIFEST_READ_FAILED" "file=${REMOTE_MANIFEST_NAME} size=${DOWNLOADED_MANIFEST_SIZE} result=size_mismatch"
        fi
        if ! openssl enc -d -aes-256-cbc -pbkdf2 \
            -in "${REMOTE_MANIFEST_ENC_LOCAL}" \
            -out "${REMOTE_MANIFEST_LOCAL}" \
            -pass "file:${RUN_KEY_FILE}" 2>"${RUN_TMP_DIR}/manifest-dec.err"; then
            fail_with_alert "FROZEN_MANIFEST_KEY_MISMATCH" "file=${REMOTE_MANIFEST_NAME} result=decrypt_failed"
        fi
        if ! "${PY_BIN}" "${OFFSITE_SYNC_PY}" validate-frozen-manifest \
            --plan-json "${PLAN_JSON}" \
            --manifest-file "${REMOTE_MANIFEST_LOCAL}" \
            --key-file "${RUN_KEY_FILE}" >/dev/null 2>"${RUN_TMP_DIR}/manifest-val.err"; then
            fail_with_alert "FROZEN_MANIFEST_KEY_MISMATCH" "file=${REMOTE_MANIFEST_NAME} result=key_or_manifest_mismatch"
        fi
    elif [[ "${PROBE_RC}" -ne 1 ]]; then
        fail_with_alert "FROZEN_MANIFEST_CHECK_FAILED" "file=${REMOTE_MANIFEST_NAME} result=lsjson_parse_failed"
    fi
elif [[ "${MANIFEST_LSJSON_RC}" -ne 3 ]]; then
    fail_with_alert "FROZEN_MANIFEST_CHECK_FAILED" "file=${REMOTE_MANIFEST_NAME} result=lsjson_failed"
fi

# ---------------------------------------------------------------------------
# Pass 1: Skip already-verified remote files; queue pending files
# ---------------------------------------------------------------------------
SKIPPED_COUNT=0
UPLOADED_COUNT=0
NEEDED_ENC_BYTES=0

while IFS='|' read -r key kind filename remote_filename remote_path plain_size expected_enc_size local_path source_sha256 pack_fingerprint; do
    [[ -n "${key}" ]] || continue

    if [[ -s "${REMOTE_MANIFEST_LOCAL}" ]]; then
        chk_lsjson="${RUN_TMP_DIR}/chk-lsjson-${key}.json"
        chk_hash="${RUN_TMP_DIR}/chk-hash-${key}.txt"
        if rclone_noproxy_nodump lsjson "${remote_path}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" > "${chk_lsjson}" 2>/dev/null \
           && rclone_noproxy_nodump hashsum md5 "${remote_path}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" > "${chk_hash}" 2>/dev/null; then
            if CHK_MSG="$("${PY_BIN}" "${OFFSITE_SYNC_PY}" check-frozen-remote \
                --plan-json "${PLAN_JSON}" \
                --key "${key}" \
                --key-file "${RUN_KEY_FILE}" \
                --remote-manifest-file "${REMOTE_MANIFEST_LOCAL}" \
                --lsjson-file "${chk_lsjson}" \
                --hashsum-file "${chk_hash}" 2>/dev/null)"; then
                log_msg "INFO" "file=${remote_filename} size=${plain_size} result=skipped"
                SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
                continue
            fi
        fi
    fi

    est_enc_size="${expected_enc_size}"
    if [[ "${kind}" == "mirror_pack" ]]; then
        est_enc_size="$("${PY_BIN}" "${BACKUP_LIB}" expected-enc-size --plain-size "${plain_size}")"
    fi

    printf '%s|%s|%s|%s|%s|%s|%s|%s|%s|%s\n' \
        "${key}" "${kind}" "${filename}" "${remote_filename}" "${remote_path}" \
        "${plain_size}" "${est_enc_size}" "${local_path}" "${source_sha256}" "${pack_fingerprint}" \
        >> "${PENDING_ROWS}"
    NEEDED_ENC_BYTES=$((NEEDED_ENC_BYTES + est_enc_size))
done < "${PLAN_ROWS}"

# Pre-check Drive free space (2x remaining encrypted bytes) when anything remains to upload.
if [[ "${NEEDED_ENC_BYTES}" -gt 0 ]]; then
    ABOUT_JSON="$(rclone_noproxy_nodump about "${RCLONE_REMOTE}:" --json \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>/dev/null || true)"
    if [[ -n "${ABOUT_JSON}" ]]; then
        if ! SPACE_MSG="$(printf '%s' "${ABOUT_JSON}" | "${PY_BIN}" "${BACKUP_LIB}" check-drive-space --file-size "${NEEDED_ENC_BYTES}" --multiplier 2 2>&1)"; then
            fail_with_alert "DRIVE_LOW_SPACE" "set=${RESOLVED_SET_ID} size=${NEEDED_ENC_BYTES} result=drive_low_space"
        fi
    fi
fi

rclone_moveto_overwrite() {
    local src_obj="$1"
    local dst_obj="$2"
    local err_file="$3"
    (
        unset RCLONE_SIZE_ONLY
        rclone_noproxy_nodump moveto "${src_obj}" "${dst_obj}" \
            --ignore-times \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" \
            --log-file "${RCLONE_LOG_FILE}"
    ) 2>"${err_file}"
}

verify_remote_path_size_and_md5() {
    local remote_obj="$1"
    local expected_name="$2"
    local expected_enc="$3"
    local meta_out="$4"
    local label="$5"
    local report_name="$6"
    local cleanup_staged="$7"
    local size_alert_code="$8"
    local hash_alert_code="$9"

    local lsjson_out=""
    if ! lsjson_out="$(rclone_noproxy_nodump lsjson "${remote_obj}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/lsjson-${label}.err")"; then
        delete_partial_remote "${cleanup_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "${size_alert_code}" "file=${report_name} size=${expected_enc} result=lsjson_failed"
    fi

    if ! printf '%s' "${lsjson_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-size --filename "${expected_name}" --expected-size "${expected_enc}" >/dev/null 2>&1; then
        delete_partial_remote "${cleanup_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "${size_alert_code}" "file=${report_name} size=${expected_enc} result=size_mismatch"
    fi

    if ! printf '%s' "${lsjson_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-size --filename "${expected_name}" --meta-file "${meta_out}" >/dev/null 2>&1; then
        delete_partial_remote "${cleanup_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "${size_alert_code}" "file=${report_name} size=${expected_enc} result=meta_size_mismatch"
    fi

    local hashsum_out=""
    if ! hashsum_out="$(rclone_noproxy_nodump hashsum md5 "${remote_obj}" \
        --cache-dir "${RCLONE_CACHE_DIR}" \
        --temp-dir "${RUN_TMP_DIR}" 2>"${RUN_TMP_DIR}/hashsum-${label}.err")"; then
        delete_partial_remote "${cleanup_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "${hash_alert_code}" "file=${report_name} size=${expected_enc} result=hashsum_failed"
    fi

    if ! printf '%s\n' "${hashsum_out}" | "${PY_BIN}" "${BACKUP_LIB}" verify-remote-hash --filename "${expected_name}" --meta-file "${meta_out}" >/dev/null 2>&1; then
        delete_partial_remote "${cleanup_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "${hash_alert_code}" "file=${report_name} size=${expected_enc} result=hash_mismatch"
    fi
}

upload_and_verify_encrypted_file() {
    local src_file="$1"
    local remote_staged="$2"
    local remote_name="$3"
    local plain_size="$4"
    local expected_enc="$5"
    local expected_plain_sha="$6"
    local label="$7"
    local plain_meta_out="$8"
    local meta_out="$9"

    local staged_name
    staged_name="$(basename "${remote_staged}")"

    CURRENT_PARTIAL_OBJ="${remote_staged}"
    suspend_err_trap
    set +e
    "${PY_BIN}" "${OFFSITE_SYNC_PY}" stream-plain-meta \
        --meta-out "${plain_meta_out}" \
        --expected-size "${plain_size}" \
        --expected-sha256 "${expected_plain_sha}" \
        < "${src_file}" 2>"${RUN_TMP_DIR}/plain-${label}.err" \
        | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${RUN_KEY_FILE}" 2>"${RUN_TMP_DIR}/openssl-${label}.err" \
        | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${meta_out}" 2>"${RUN_TMP_DIR}/hash-${label}.err" \
        | rclone_noproxy_nodump rcat "${remote_staged}" \
            --size "${expected_enc}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" \
            --log-file "${RCLONE_LOG_FILE}" 2>"${RUN_TMP_DIR}/rcat-${label}.err"
    local pipe_status=("${PIPESTATUS[@]}")
    set -e
    restore_err_trap

    if [[ "${pipe_status[0]:-1}" -eq 3 ]]; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "FROZEN_SOURCE_SHA_MISMATCH" "file=${remote_name} size=${plain_size} result=source_sha_mismatch"
    fi

    if [[ "${pipe_status[0]:-1}" -ne 0 ]] || [[ "${pipe_status[1]:-1}" -ne 0 ]] || [[ "${pipe_status[2]:-1}" -ne 0 ]] || [[ "${pipe_status[3]:-1}" -ne 0 ]]; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "FROZEN_UPLOAD_FAILED" "file=${remote_name} size=${expected_enc} result=upload_failed"
    fi

    if [[ ! -s "${plain_meta_out}" ]] || [[ ! -s "${meta_out}" ]]; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "STREAM_META_MISSING" "file=${remote_name} size=${expected_enc} result=stream_meta_missing"
    fi

    verify_remote_path_size_and_md5 \
        "${remote_staged}" \
        "${staged_name}" \
        "${expected_enc}" \
        "${meta_out}" \
        "staged-${label}" \
        "${remote_name}" \
        "${remote_staged}" \
        "FROZEN_SIZE_MISMATCH" \
        "FROZEN_HASH_MISMATCH"

    CURRENT_PARTIAL_OBJ=""
}

publish_encrypted_manifest() {
    local local_manifest="$1"
    local rand_tag staged_manifest staged_name manifest_plain_size manifest_enc_size plain_meta_out meta_out
    rand_tag="$(openssl rand -hex 6)"
    staged_manifest="${REMOTE_MANIFEST_PATH}.staged-${rand_tag}"
    staged_name="$(basename "${staged_manifest}")"
    manifest_plain_size="$(wc -c < "${local_manifest}" | tr -d ' ')"
    manifest_enc_size="$("${PY_BIN}" "${BACKUP_LIB}" expected-enc-size --plain-size "${manifest_plain_size}")"
    plain_meta_out="${RUN_TMP_DIR}/manifest-plain.meta.json"
    meta_out="${RUN_TMP_DIR}/manifest-upload.meta.json"

    CURRENT_PARTIAL_OBJ="${staged_manifest}"
    suspend_err_trap
    set +e
    "${PY_BIN}" "${OFFSITE_SYNC_PY}" stream-plain-meta \
        --meta-out "${plain_meta_out}" \
        --expected-size "${manifest_plain_size}" \
        < "${local_manifest}" 2>"${RUN_TMP_DIR}/plain-manifest.err" \
        | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:${RUN_KEY_FILE}" 2>"${RUN_TMP_DIR}/openssl-manifest.err" \
        | "${PY_BIN}" "${BACKUP_LIB}" stream-hash-meta --meta-out "${meta_out}" 2>"${RUN_TMP_DIR}/hash-manifest.err" \
        | rclone_noproxy_nodump rcat "${staged_manifest}" \
            --size "${manifest_enc_size}" \
            --cache-dir "${RCLONE_CACHE_DIR}" \
            --temp-dir "${RUN_TMP_DIR}" \
            --log-file "${RCLONE_LOG_FILE}" 2>"${RUN_TMP_DIR}/rcat-manifest.err"
    local pipe_status=("${PIPESTATUS[@]}")
    set -e
    restore_err_trap

    if [[ "${pipe_status[0]:-1}" -ne 0 ]] || [[ "${pipe_status[1]:-1}" -ne 0 ]] || [[ "${pipe_status[2]:-1}" -ne 0 ]] || [[ "${pipe_status[3]:-1}" -ne 0 ]]; then
        delete_partial_remote "${staged_manifest}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "FROZEN_MANIFEST_UPLOAD_FAILED" "file=${REMOTE_MANIFEST_NAME} size=${manifest_enc_size} result=upload_failed"
    fi

    if [[ ! -s "${meta_out}" ]]; then
        delete_partial_remote "${staged_manifest}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "FROZEN_MANIFEST_VERIFY_FAILED" "file=${REMOTE_MANIFEST_NAME} size=${manifest_enc_size} result=stream_meta_missing"
    fi

    verify_remote_path_size_and_md5 \
        "${staged_manifest}" \
        "${staged_name}" \
        "${manifest_enc_size}" \
        "${meta_out}" \
        "staged-manifest" \
        "${REMOTE_MANIFEST_NAME}" \
        "${staged_manifest}" \
        "FROZEN_MANIFEST_VERIFY_FAILED" \
        "FROZEN_MANIFEST_VERIFY_FAILED"

    if ! rclone_moveto_overwrite "${staged_manifest}" "${REMOTE_MANIFEST_PATH}" "${RUN_TMP_DIR}/moveto-manifest.err"; then
        delete_partial_remote "${staged_manifest}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "FROZEN_MANIFEST_MOVETO_FAILED" "file=${REMOTE_MANIFEST_NAME} size=${manifest_enc_size} result=moveto_failed"
    fi

    verify_remote_path_size_and_md5 \
        "${REMOTE_MANIFEST_PATH}" \
        "${REMOTE_MANIFEST_NAME}" \
        "${manifest_enc_size}" \
        "${meta_out}" \
        "final-manifest" \
        "${REMOTE_MANIFEST_NAME}" \
        "${staged_manifest}" \
        "FROZEN_MANIFEST_FINAL_VERIFY_FAILED" \
        "FROZEN_MANIFEST_FINAL_VERIFY_FAILED"

    CURRENT_PARTIAL_OBJ=""
}

# ---------------------------------------------------------------------------
# Pass 2: Upload, verify staged & final path, and record each pending file
# ---------------------------------------------------------------------------
while IFS='|' read -r key kind filename remote_filename remote_path eff_plain_size eff_enc_size src_path expected_plain_sha plan_pack_fp; do
    [[ -n "${key}" ]] || continue

    actual_pack_fp=""
    if [[ "${kind}" == "mirror_pack" ]]; then
        if ! "${PY_BIN}" "${OFFSITE_SYNC_PY}" check-local-pack-space \
            --dir "${RUN_TMP_DIR}" \
            --raw-bytes "${eff_plain_size}" \
            --multiplier "${PACK_SPACE_MULTIPLIER}" >/dev/null 2>"${RUN_TMP_DIR}/space-${key}.err"; then
            fail_with_alert "EXTERNAL_DRIVE_LOW_SPACE" "file=${filename} size=${eff_plain_size} result=external_drive_low_space"
        fi
        src_path="${RUN_TMP_DIR}/${filename}"
        pack_json="${RUN_TMP_DIR}/pack-${key}.json"
        if ! "${PY_BIN}" "${OFFSITE_SYNC_PY}" pack-frozen-objects \
            --manifest "${MANIFEST_LOCAL_PATH}" \
            --expected-set-id "${RESOLVED_SET_ID}" \
            --mirror-objects-dir "${MIRROR_OBJECTS_DIR}" \
            --out-tar "${src_path}" \
            --expected-pack-fingerprint "${plan_pack_fp}" \
            --min-free-multiplier "${PACK_SPACE_MULTIPLIER}" \
            > "${pack_json}" 2>"${RUN_TMP_DIR}/pack-${key}.err"; then
            fail_with_alert "FROZEN_PACK_FAILED" "file=${filename} size=${eff_plain_size} result=pack_failed"
        fi
        pack_fields="$("${PY_BIN}" "${OFFSITE_SYNC_PY}" read-json-field \
            --file "${pack_json}" \
            --field tar_size \
            --field tar_sha256 \
            --field pack_fingerprint)"
        eff_plain_size="$(printf '%s\n' "${pack_fields}" | sed -n '1p')"
        expected_plain_sha="$(printf '%s\n' "${pack_fields}" | sed -n '2p')"
        actual_pack_fp="$(printf '%s\n' "${pack_fields}" | sed -n '3p')"
        eff_enc_size="$("${PY_BIN}" "${BACKUP_LIB}" expected-enc-size --plain-size "${eff_plain_size}")"
    fi

    rand_tag="$(openssl rand -hex 6)"
    remote_staged="${remote_path}.staged-${rand_tag}"
    plain_meta_out="${RUN_TMP_DIR}/plain-meta-${key}.json"
    meta_out="${RUN_TMP_DIR}/meta-${key}.json"

    upload_and_verify_encrypted_file \
        "${src_path}" \
        "${remote_staged}" \
        "${remote_filename}" \
        "${eff_plain_size}" \
        "${eff_enc_size}" \
        "${expected_plain_sha}" \
        "${key}" \
        "${plain_meta_out}" \
        "${meta_out}"

    CURRENT_PARTIAL_OBJ="${remote_staged}"
    if ! rclone_moveto_overwrite "${remote_staged}" "${remote_path}" "${RUN_TMP_DIR}/moveto-${key}.err"; then
        delete_partial_remote "${remote_staged}"
        CURRENT_PARTIAL_OBJ=""
        fail_with_alert "FROZEN_MOVETO_FAILED" "file=${remote_filename} size=${eff_enc_size} result=moveto_failed"
    fi

    verify_remote_path_size_and_md5 \
        "${remote_path}" \
        "${remote_filename}" \
        "${eff_enc_size}" \
        "${meta_out}" \
        "final-${key}" \
        "${remote_filename}" \
        "${remote_staged}" \
        "FROZEN_FINAL_VERIFY_FAILED" \
        "FROZEN_FINAL_VERIFY_FAILED"
    CURRENT_PARTIAL_OBJ=""

    if ! "${PY_BIN}" "${OFFSITE_SYNC_PY}" merge-frozen-manifest \
        --plan-json "${PLAN_JSON}" \
        --key "${key}" \
        --key-file "${RUN_KEY_FILE}" \
        --plain-size "${eff_plain_size}" \
        --meta-file "${meta_out}" \
        --plain-meta-file "${plain_meta_out}" \
        --actual-pack-fingerprint "${actual_pack_fp}" \
        --existing-manifest-file "${REMOTE_MANIFEST_LOCAL}" \
        --out "${REMOTE_MANIFEST_LOCAL}" 2>"${RUN_TMP_DIR}/merge-${key}.err"; then
        fail_with_alert "FROZEN_MANIFEST_MERGE_FAILED" "file=${remote_filename} size=${eff_enc_size} result=manifest_merge_failed"
    fi

    publish_encrypted_manifest "${REMOTE_MANIFEST_LOCAL}"

    if [[ "${kind}" == "mirror_pack" ]] && [[ -e "${src_path}" ]]; then
        if ! rm -f "${src_path}" 2>/dev/null || [[ -e "${src_path}" ]]; then
            fail_with_alert "FROZEN_TMP_CLEANUP_FAILED" "file=${filename} result=plaintext_cleanup_failed"
        fi
    fi

    log_msg "INFO" "file=${remote_filename} plain_size=${eff_plain_size} enc_size=${eff_enc_size} result=uploaded"
    UPLOADED_COUNT=$((UPLOADED_COUNT + 1))
done < "${PENDING_ROWS}"

# Confirm the temporary directory (and any plaintext artifacts) is fully removed before reporting ok.
if [[ -n "${RUN_TMP_DIR:-}" ]] && [[ -e "${RUN_TMP_DIR}" ]]; then
    if ! rm -rf "${RUN_TMP_DIR}" 2>/dev/null || [[ -e "${RUN_TMP_DIR}" ]]; then
        fail_with_alert "FROZEN_TMP_CLEANUP_FAILED" "set=${RESOLVED_SET_ID} result=tmp_cleanup_failed"
    fi
    RUN_TMP_DIR=""
fi
release_target_lock

log_msg "INFO" "set=${RESOLVED_SET_ID} uploaded=${UPLOADED_COUNT} skipped=${SKIPPED_COUNT} result=ok"
printf 'ok\n'
finish_backup
