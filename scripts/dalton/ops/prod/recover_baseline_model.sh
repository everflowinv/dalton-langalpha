#!/usr/bin/env bash
# Everflow production read-only baseline model recovery script (modeling v2 §4.1 & §8.5).
#
# Strictly read-only: locates the pre-corruption China Ecommerce baseline workbook via
#   1) Large-file upload channel (`dalton_chat_file_uploads.actual_sha256` + MinIO / work/uploads)
#   2) Small-attachment & turn-history channel (`conversation_queries` + `chat_attachments`,
#      including inline attachments without `url`/`attachment_id` matched by sender, filename,
#      size, time window, and verified against `records[0].input_sha256`)
#   3) Encrypted Google Drive `rclone` backup fallback (AES-256-CBC PBKDF2 streaming decrypt)
#
# Usage:
#   scripts/dalton/ops/prod/recover_baseline_model.sh \
#     --workspace-id <UUID> \
#     --output <path/to/recovered.xlsx> \
#     [--workspace-dir <dir>] [--lineage-file <model_lineage.json>] \
#     [--before-ts <ISO8601>] [--expected-sha256 <64-hex>] [--expected-filename <name>] \
#     [--db-url <postgres://...>] [--sqlite-db <test.db>] \
#     [--object-store-dir <dir>] [--minio-mc-alias <alias>] [--minio-bucket <bucket>] \
#     [--rclone-remote <remote>] [--rclone-drive-dir <dir>] [--backup-key-file <key>] \
#     [--manifest-out <manifest.json>]

set -euo pipefail
umask 077

PROD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${PROD_DIR}/../../../.." && pwd)"
HELPER_PY="${PROD_DIR}/recover_baseline_model_lib.py"

if [[ -n "${DALTON_REPO_PYTHON:-}" ]] && [[ -x "${DALTON_REPO_PYTHON}" ]]; then
    PY_BIN="${DALTON_REPO_PYTHON}"
elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PY_BIN="${REPO_ROOT}/.venv/bin/python"
else
    PY_BIN="$(command -v python3 || echo python3)"
fi

# Everflow production defaults (isolated inside scripts/dalton/ops/prod/ per standalone-core rules).
RCLONE_REMOTE="${DALTON_RCLONE_REMOTE:-gdrive}"
RCLONE_DRIVE_DIR="${DALTON_RCLONE_DRIVE_DIR:-DaltonBackup}"
RCLONE_BIN="${DALTON_RCLONE_BIN:-rclone}"
BACKUP_KEY_FILE="${DALTON_BACKUP_KEY_FILE:-${HOME}/.config/dalton/backup.key}"
DB_URL="${DATABASE_URL:-}"
MINIO_MC_ALIAS="${DALTON_MINIO_MC_ALIAS:-}"
MINIO_BUCKET="${DALTON_CHAT_UPLOAD_STORAGE_BUCKET:-${DALTON_MINIO_BUCKET:-}}"

HAS_DB_FLAG=0
HAS_RCLONE_REMOTE_FLAG=0
HAS_RCLONE_DIR_FLAG=0
HAS_KEY_FLAG=0
HAS_MC_ALIAS_FLAG=0
HAS_BUCKET_FLAG=0

for arg in "$@"; do
    case "$arg" in
        --db-url|--db-url=*|--sqlite-db|--sqlite-db=*) HAS_DB_FLAG=1 ;;
        --rclone-remote|--rclone-remote=*) HAS_RCLONE_REMOTE_FLAG=1 ;;
        --rclone-drive-dir|--rclone-drive-dir=*) HAS_RCLONE_DIR_FLAG=1 ;;
        --backup-key-file|--backup-key-file=*) HAS_KEY_FLAG=1 ;;
        --minio-mc-alias|--minio-mc-alias=*) HAS_MC_ALIAS_FLAG=1 ;;
        --minio-bucket|--minio-bucket=*) HAS_BUCKET_FLAG=1 ;;
    esac
done

EXTRA_ARGS=(--rclone-bin "${RCLONE_BIN}")
if [[ "${HAS_DB_FLAG}" -eq 0 ]] && [[ -n "${DB_URL}" ]]; then
    EXTRA_ARGS+=(--db-url "${DB_URL}")
fi
if [[ "${HAS_RCLONE_REMOTE_FLAG}" -eq 0 ]] && [[ -n "${RCLONE_REMOTE}" ]]; then
    EXTRA_ARGS+=(--rclone-remote "${RCLONE_REMOTE}")
fi
if [[ "${HAS_RCLONE_DIR_FLAG}" -eq 0 ]] && [[ -n "${RCLONE_DRIVE_DIR}" ]]; then
    EXTRA_ARGS+=(--rclone-drive-dir "${RCLONE_DRIVE_DIR}")
fi
if [[ "${HAS_KEY_FLAG}" -eq 0 ]] && [[ -f "${BACKUP_KEY_FILE}" ]]; then
    EXTRA_ARGS+=(--backup-key-file "${BACKUP_KEY_FILE}")
fi
if [[ "${HAS_MC_ALIAS_FLAG}" -eq 0 ]] && [[ -n "${MINIO_MC_ALIAS}" ]]; then
    EXTRA_ARGS+=(--minio-mc-alias "${MINIO_MC_ALIAS}")
fi
if [[ "${HAS_BUCKET_FLAG}" -eq 0 ]] && [[ -n "${MINIO_BUCKET}" ]]; then
    EXTRA_ARGS+=(--minio-bucket "${MINIO_BUCKET}")
fi

# Strip proxy env vars when invoking rclone (consistent with dalton_data_backup.sh / _common.sh).
exec env \
    -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    -u all_proxy -u ALL_PROXY -u no_proxy -u NO_PROXY \
    "${PY_BIN}" "${HELPER_PY}" "${EXTRA_ARGS[@]}" "$@"
