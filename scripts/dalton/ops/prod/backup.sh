#!/usr/bin/env bash
# Daily / tagged Everflow Dalton consistent backup set (§3.1):
#   verify-mount -> GC advisory locks -> REPEATABLE READ pg_export_snapshot ->
#   pg_dump -Fc --snapshot -> list_object_versions + mirror pool -> terminal asr_jobs ->
#   storage_refs full reconciliation -> atomic same-disk promote -> tiered retention & pool prune.
#
# Always emits a single terminal summary line compatible with deploy_main.sh:
#   "YYYY-mm-dd HH:MM:SS ok <size> <dump_path> set=<backup_set_id>"
set -euo pipefail
umask 077

export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"
BACKUP_LIB="${OPS_DIR}/backup_lib.py"
LOCAL_BACKUP_SET_PY="${SCRIPT_DIR}/local_backup_set.py"

TAG="daily"
SKIP_MOUNT_CHECK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag)
            TAG="$2"
            shift 2
            ;;
        --tag=*)
            TAG="${1#--tag=}"
            shift
            ;;
        --skip-mount-check)
            SKIP_MOUNT_CHECK=1
            shift
            ;;
        -h|--help)
            cat <<'EOF'
Usage: backup.sh [--tag daily|weekly|deploy|manual-<label>] [--skip-mount-check]
EOF
            exit 0
            ;;
        *)
            echo "$(date '+%F %T') fail unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

resolve_py() {
    if [[ -n "${DALTON_REPO_PYTHON:-}" ]] && [[ -x "${DALTON_REPO_PYTHON}" ]]; then
        printf '%s\n' "${DALTON_REPO_PYTHON}"
    elif [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        printf '%s\n' "${REPO_ROOT}/.venv/bin/python"
    elif [[ -x "/opt/homebrew/bin/python3" ]]; then
        printf '%s\n' "/opt/homebrew/bin/python3"
    else
        command -v python3 || echo "/usr/bin/python3"
    fi
}

PY_BIN="$(resolve_py)"
EXT_MOUNT_ROOT="${DALTON_EXTERNAL_MOUNT_ROOT:-/Volumes/EveSSD}"
BACKUP_ROOT="${DALTON_BACKUP_ROOT:-${EXT_MOUNT_ROOT}/Dalton/backups}"
BACKUP_DIR="${DALTON_BACKUP_DIR:-${BACKUP_ROOT}/pg}"
ASR_JOBS_DIR="${DALTON_ASR_JOBS_DIR:-${REPO_ROOT}/var/dalton/asr_jobs}"
COMPOSE_FILES="${DALTON_BACKUP_COMPOSE_FILES:-docker-compose.yml:docker-compose.dalton.yml}"
DETAIL_LOG="${DALTON_BACKUP_DETAIL_LOG:-${HOME}/Library/Logs/dalton-pg-backup-detail.log}"

cd "${REPO_ROOT}"

# Load repo .env into subshell if present (for storage/postgres variables), without echoing secrets.
if [[ "${DALTON_BACKUP_LOAD_ENV:-1}" == "1" ]] && [[ -f "${REPO_ROOT}/.env" ]] && [[ ! -L "${REPO_ROOT}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_ROOT}/.env" 2>/dev/null || true
    set +a
fi

if [[ "${DALTON_BACKUP_SET_MODE:-consistent}" == "legacy" ]]; then
    mkdir -p "${BACKUP_DIR}"
    OUT="${BACKUP_DIR}/dalton-pg-$(date +%Y%m%d-%H%M%S).dump"
    docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "${OUT}.tmp"
    [ -s "${OUT}.tmp" ] && mv "${OUT}.tmp" "${OUT}"
    PRUNE_SCRIPT="${DALTON_PRUNE_SCRIPT:-${BACKUP_DIR}/prune_backups.py}"
    if [[ -f "${PRUNE_SCRIPT}" ]]; then
        /usr/bin/python3 "${PRUNE_SCRIPT}" --apply >/dev/null
    fi
    echo "$(date '+%F %T') ok $(du -h "${OUT}" | cut -f1) ${OUT}"
    exit 0
fi

# Verify external drive mount & standard layout before writing anything.
if [[ "${SKIP_MOUNT_CHECK}" != "1" ]]; then
    if ! MOUNT_ERR="$("${PY_BIN}" "${BACKUP_LIB}" verify-mount \
        --mount-root "${EXT_MOUNT_ROOT}" \
        --backups-root "${BACKUP_ROOT}" 2>&1)"; then
        echo "$(date '+%F %T') fail mount_check: ${MOUNT_ERR}"
        exit 1
    fi
fi

mkdir -p "$(dirname "${DETAIL_LOG}")" 2>/dev/null || true
RUN_ARGS=(
    run
    --mount-root "${EXT_MOUNT_ROOT}"
    --backups-root "${BACKUP_ROOT}"
    --tag "${TAG}"
    --compose-files "${COMPOSE_FILES}"
)
if [[ -d "${ASR_JOBS_DIR}" ]]; then
    RUN_ARGS+=(--asr-jobs-dir "${ASR_JOBS_DIR}")
fi
if [[ "${SKIP_MOUNT_CHECK}" == "1" ]]; then
    RUN_ARGS+=(--skip-mount-check)
fi

ERR_TMP="$(mktemp "${BACKUP_ROOT}/.tmp/backup-sh-err.XXXXXX" 2>/dev/null || mktemp "/tmp/backup-sh-err.XXXXXX")"
trap 'rm -f "${ERR_TMP}" 2>/dev/null || true' EXIT

set +e
JSON_OUT="$("${PY_BIN}" "${LOCAL_BACKUP_SET_PY}" "${RUN_ARGS[@]}" 2>"${ERR_TMP}")"
RC=$?
set -e

if [[ -s "${ERR_TMP}" ]]; then
    cat "${ERR_TMP}" >> "${DETAIL_LOG}" 2>/dev/null || true
fi

if [[ "${RC}" -ne 0 ]] || [[ -z "${JSON_OUT}" ]]; then
    ERR_LINE="$(tail -n 1 "${ERR_TMP}" 2>/dev/null | tr '\n' ' ' || true)"
    echo "$(date '+%F %T') fail rc=${RC} ${ERR_LINE}"
    exit "${RC:-1}"
fi

PARSED="$(printf '%s' "${JSON_OUT}" | "${PY_BIN}" "${SCRIPT_DIR}/offsite_sync.py" read-json-field --field dump_path --field backup_set_id)"
DUMP_PATH="$(printf '%s\n' "${PARSED}" | sed -n '1p')"
SET_ID="$(printf '%s\n' "${PARSED}" | sed -n '2p')"
HUMAN_SIZE="$(du -h "${DUMP_PATH}" | awk '{print $1}')"

echo "$(date '+%F %T') ok  ${HUMAN_SIZE} ${DUMP_PATH} set=${SET_ID}"
