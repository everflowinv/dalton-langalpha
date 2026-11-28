#!/usr/bin/env bash
# Standalone Core formal restore utility (v1: empty target instance only).
#
# Restores a timestamped backup directory created by `scripts/standalone/backup.sh`
# into an EMPTY target instance:
#   1. Verifies backup integrity (`SHA256SUMS`, `manifest.json`, and MinIO archive manifest).
#   2. Acquires deployment-directory lock `/tmp/dalton-restore-dir-<sha256[:16]>.lock`
#      FIRST (before inspecting or reading `.env`), prepares the candidate `.env`
#      (strictly file-only; never reads shell env; only decrypts `env.enc` when
#      creating or `--replace-env`-ing `.env`), enforces exact verbatim
#      `COMPOSE_PROJECT_NAME` match for `--replace-env`, acquires project lock
#      `/tmp/dalton-<project>.restore.lock` SECOND (`flock`, or `mkdir` fallback
#      without PID-based auto-cleanup), cleans leftover `.env.tmp-*` files, and
#      runs static preflight checks (external DB guard, identifiers, Compose files).
#      When restoring from `env.enc`, backs up any existing `.env` (using `cp -P -p`
#      + mode 600 for regular files, or moving a symlink itself without touching its
#      target) to `${ENV_FILE}.pre-restore-<timestamp>` and atomically moves the
#      decrypted `.env` into `${ENV_FILE}` (mode 600) BEFORE any compose-dependent
#      preflight so all preflight, comparison, write, and startup steps use the
#      exact same regular `docker compose` arguments (no temporary compose override).
#   3. Verifies effective `backend` storage config (`docker compose config --format json`),
#      verifies the entire PostgreSQL instance is empty (all non-template DBs
#      in `pg_database` including `postgres` checked over TCP with superuser
#      credentials for non-system `pg_class`, `pg_proc`, and `pg_type` objects;
#      object storage has 0 buckets or only the empty `minio_init` bucket),
#      verifies backup bucket/key-prefix parity against effective backend config,
#      and validates `pg_restore --list`. Any failure before data writes start
#      automatically rolls `.env` back via `trap`. Refuses non-empty targets and
#      points to the manual reset-and-restore procedure in
#      `docs/dalton/dev-guide/STANDALONE-DEPLOY.md` (§6.2).
#   4. Restores MinIO objects directly into their original bucket name(s) with
#      read-back verification of size, SHA-256, and `ContentType`.
#   5. Restores PostgreSQL directly into the official database, runs migrations,
#      and verifies DB-referenced objects against MinIO.
#   6. Rebuilds `edge` if `.env` came from backup (unless `--skip-edge-build`)
#      and restores Caddy `/data`.
#   7. Starts the full stack and runs unified backend/edge health & schema checks.
#
# Every `docker compose` call unsets all keys present in `.env` (plus
# `COMPOSE_PROJECT_NAME` and `COMPOSE_FILE`) via `env -u` so exported shell
# variables never shadow the target `.env` file.
#
# Usage:
#   bash scripts/standalone/restore.sh --backup-dir /var/backups/dalton/dalton-backup-20260101-020000Z-a1b2c3
#   bash scripts/standalone/restore.sh --backup-dir /path/to/backup --env-passphrase-file /etc/dalton/backup.pass
#   bash scripts/standalone/restore.sh --backup-dir /path/to/backup --env-passphrase-file /etc/dalton/backup.pass --replace-env
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/standalone/env_tokens.sh
source "${SCRIPT_DIR}/env_tokens.sh"
WORK_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BACKUP_DIR=""
ENV_PASSPHRASE_FILE="${DALTON_BACKUP_PASSPHRASE_FILE:-}"
COMPOSE_FILES_RAW=""
PROJECT_NAME_ARG=""
PROJECT_NAME_EXPLICIT=0
CA_FILE=""
REPLACE_ENV=0
SKIP_EDGE_BUILD=0
SKIP_CADDY=0
SKIP_MINIO=0
SKIP_WIKI=0

MANUAL_OVERWRITE_REF="docs/dalton/dev-guide/STANDALONE-DEPLOY.md (§6.2 在已有实例上覆盖恢复)"

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/restore.sh --backup-dir <dir> [OPTIONS]

Restores a backup into an EMPTY target instance (database must have no objects
in non-system schemas, and object storage must have no buckets or only the empty
minio-init bucket). To overwrite an existing instance, follow the manual
`docker compose down -v` procedure in docs/dalton/dev-guide/STANDALONE-DEPLOY.md (§6.2).

Options:
  --backup-dir <dir>          Backup directory created by backup.sh (required).
  --work-dir <dir>            Deployment directory containing compose files (default: repo root).
  --env-passphrase-file <f>   Decrypt env.enc from backup into .env on an empty target
                              (or set DALTON_BACKUP_PASSPHRASE_FILE).
  --compose-files <f1:f2>     Colon-separated Docker Compose files
                              (default: from target .env COMPOSE_FILE / DALTON_TLS_MODE).
  --project-name <name>       Explicit Docker Compose project name (overrides .env
                              COMPOSE_PROJECT_NAME and work-dir basename).
  --ca-file <path>            CA certificate PEM file used to verify HTTPS in custom/auto mode.
  --replace-env               Allow replacing an existing <work-dir>/.env from env.enc on an
                              empty target instance (backs up existing .env to
                              .env.pre-restore-<ts> and rolls back on pre-write failure).
  --skip-edge-build           Skip rebuilding the edge image after restoring .env.
  --skip-minio                Skip restoring MinIO objects.
  --skip-caddy                Skip restoring Caddy certificate data.
  -h, --help                  Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-dir)
      BACKUP_DIR="$2"
      shift 2
      ;;
    --backup-dir=*)
      BACKUP_DIR="${1#--backup-dir=}"
      shift
      ;;
    --work-dir)
      WORK_DIR="$2"
      shift 2
      ;;
    --work-dir=*)
      WORK_DIR="${1#--work-dir=}"
      shift
      ;;
    --env-passphrase-file)
      ENV_PASSPHRASE_FILE="$2"
      shift 2
      ;;
    --env-passphrase-file=*)
      ENV_PASSPHRASE_FILE="${1#--env-passphrase-file=}"
      shift
      ;;
    --compose-files)
      COMPOSE_FILES_RAW="$2"
      shift 2
      ;;
    --compose-files=*)
      COMPOSE_FILES_RAW="${1#--compose-files=}"
      shift
      ;;
    --project-name)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --project-name requires a non-empty value." >&2
        exit 2
      fi
      PROJECT_NAME_ARG="$2"
      PROJECT_NAME_EXPLICIT=1
      shift 2
      ;;
    --project-name=*)
      PROJECT_NAME_ARG="${1#--project-name=}"
      if [[ -z "$PROJECT_NAME_ARG" ]]; then
        echo "ERROR: --project-name requires a non-empty value." >&2
        exit 2
      fi
      PROJECT_NAME_EXPLICIT=1
      shift
      ;;
    --ca-file)
      CA_FILE="$2"
      shift 2
      ;;
    --ca-file=*)
      CA_FILE="${1#--ca-file=}"
      shift
      ;;
    --replace-env)
      REPLACE_ENV=1
      shift
      ;;
    --force)
      echo "ERROR: --force is no longer supported. restore.sh only restores into an empty target instance (see ${MANUAL_OVERWRITE_REF}); pass --replace-env to replace an existing .env from env.enc on an empty target." >&2
      exit 2
      ;;
    --skip-edge-build)
      SKIP_EDGE_BUILD=1
      shift
      ;;
    --skip-minio)
      SKIP_MINIO=1
      shift
      ;;
    --skip-caddy)
      SKIP_CADDY=1
      shift
      ;;
    --skip-wiki)
      SKIP_WIKI=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

info() { printf "[restore] %s\n" "$1"; }
ok()   { printf "[restore] OK: %s\n" "$1"; }
warn() { printf "[restore] WARN: %s\n" "$1" >&2; }
err()  { printf "[restore] ERROR: %s\n" "$1" >&2; }

fmt_cmd() {
  local out="" arg
  for arg in "$@"; do
    if [[ -n "$out" ]]; then
      out+=" "
    fi
    out+="$(printf '%q' "$arg")"
  done
  printf '%s' "$out"
}

print_non_empty_manual_hint() {
  local proj="${COMPOSE_PROJECT:-<project>}"
  local down_cmd="docker compose -p ${proj} -f docker-compose.yml -f docker-compose.core.yml --env-file ${ENV_FILE} down -v"
  if [[ -n "$COMPOSE_PROJECT" && "${#COMPOSE_FILE_ARGS[@]}" -gt 0 ]]; then
    down_cmd="$(fmt_cmd docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" down -v)"
  fi
  err "restore.sh only restores into an empty target instance."
  err "To overwrite an existing instance (or roll back after a database migration), follow ${MANUAL_OVERWRITE_REF}:"
  err "  1. Confirm you have a verified backup (check backup.sh verification output and SHA256SUMS)."
  err "  2. List and confirm ALL volumes belonging to project '${proj}':"
  err "       $(fmt_cmd docker volume ls --filter "label=com.docker.compose.project=${proj}")"
  err "     Then remove this project's containers and volumes (deletes current data):"
  err "       ${down_cmd}"
  err "  3. Re-run restore.sh against the now-empty instance."
}

validate_project_name() {
  local name="$1"
  local source_label="${2:---project-name}"
  local LC_ALL=C
  if [[ -z "$name" || "$name" == *$'\n'* || ! "$name" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    err "Invalid ${source_label} '${name}': must match ^[a-z0-9][a-z0-9_-]*$ (lowercase ASCII letters, digits, hyphens, underscores; must start with a letter or digit)."
    exit 2
  fi
}

normalize_env_project_name() {
  local raw="$1"
  printf '%s' "$raw" | LC_ALL=C tr '[:upper:]' '[:lower:]'
}

sanitize_project_name() {
  local raw="$1"
  local cleaned
  cleaned="$(printf '%s' "$raw" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C sed -E 's/[^a-z0-9_-]+/-/g; s/^[^a-z0-9]+//; s/[-_]+$//')"
  printf '%s' "${cleaned:-dalton-core}"
}

if [[ "$PROJECT_NAME_EXPLICIT" -eq 1 ]]; then
  validate_project_name "$PROJECT_NAME_ARG" "--project-name"
fi

if [[ -z "$BACKUP_DIR" ]]; then
  err "--backup-dir is required."
  usage >&2
  exit 2
fi
if [[ ! -d "$BACKUP_DIR" ]]; then
  err "Backup directory does not exist: ${BACKUP_DIR}"
  exit 1
fi
if [[ ! -d "$WORK_DIR" ]]; then
  err "Work directory does not exist: ${WORK_DIR}"
  exit 1
fi
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"
WORK_DIR_REAL="$(cd "$WORK_DIR" && pwd -P)"

if [[ ! -f "${BACKUP_DIR}/postgres.dump" ]]; then
  err "Missing ${BACKUP_DIR}/postgres.dump."
  exit 1
fi
if [[ -n "$CA_FILE" && ( ! -r "$CA_FILE" || ! -s "$CA_FILE" ) ]]; then
  err "CA file is missing, unreadable, or empty: ${CA_FILE}"
  exit 1
fi

get_file_mode() {
  local target="$1"
  if [[ -L "$target" || ! -f "$target" ]]; then
    printf '600'
    return 0
  fi
  if stat -c '%a' "$target" >/dev/null 2>&1; then
    stat -c '%a' "$target"
  elif stat -f '%Lp' "$target" >/dev/null 2>&1; then
    stat -f '%Lp' "$target"
  else
    printf '600'
  fi
}

# Strict file-only .env reader (never consults shell environment variables).
read_env_file_var() {
  local env_path="$1" key="$2" default_val="${3:-}"
  if [[ ! -f "$env_path" ]]; then
    printf '%s' "$default_val"
    return 0
  fi
  local val
  val="$(python3 - "$env_path" "$key" <<'PY'
import re, sys

env_path, target_key = sys.argv[1], sys.argv[2]
line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([=:])(.*)$")
found = None
with open(env_path, encoding="utf-8") as fh:
    for raw in fh:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = line_re.match(raw.rstrip("\r\n"))
        if not m or m.group(1) != target_key:
            continue
        val = m.group(3).strip()
        if val and val[0] in ("'", '"'):
            # Quoted value, possibly followed by an inline comment: keep what is
            # inside the quotes, as Compose does.
            end = val.find(val[0], 1)
            val = val[1:end] if end > 0 else val[1:]
        elif val:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        found = val
if found is not None:
    sys.stdout.write(found)
PY
)"
  if [[ -n "$val" ]]; then
    printf '%s' "$val"
  else
    printf '%s' "$default_val"
  fi
}

DIR_LOCK_HASH=""
DIR_LOCK_PATH=""
DIR_LOCK_MODE=""
DIR_LOCK_HELD=0
RESTORE_LOCK_PATH=""
RESTORE_LOCK_MODE=""
RESTORE_LOCK_HELD=0
COMPOSE_PROJECT=""
COMPOSE_FILE_ARGS=()
STORAGE_ANY_CONFIGURED=1
MUTATION_STARTED=0
RESTORE_SUCCEEDED=0
ENV_FILE="${WORK_DIR}/.env"
ENV_EXISTED_BEFORE=0
ENV_WAS_SYMLINK=0
ORIG_ENV_MODE="600"
STAGED_ENV_TMP=""
ENV_FROM_BACKUP=0
ENV_BACKUP_PATH=""
CANDIDATE_ENV=""
MINIO_MANIFEST_TMP=""
# Set at preflight when the backup carries an object archive; the write step trusts this,
# not a second look at the directory, so an archive that vanishes later stops the restore.
MINIO_ARCHIVE_SEEN=0
# A side copy of .env still being written; cleanup removes it (it holds credentials).
ENV_BACKUP_PARTIAL=""
COMPOSE_CFG_TMP=""

compute_dir_lock_hash() {
  local real_dir="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "$real_dir" | sha256sum | cut -c1-16
  elif command -v shasum >/dev/null 2>&1; then
    printf '%s' "$real_dir" | shasum -a 256 | cut -c1-16
  else
    python3 -c 'import hashlib, sys; sys.stdout.write(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:16])' "$real_dir"
  fi
}

acquire_dir_lock() {
  DIR_LOCK_HASH="$(compute_dir_lock_hash "$WORK_DIR_REAL")"
  DIR_LOCK_PATH="/tmp/dalton-restore-dir-${DIR_LOCK_HASH}.lock"
  if command -v flock >/dev/null 2>&1; then
    if [[ -d "$DIR_LOCK_PATH" ]]; then
      err "这个目录里已有另一个恢复在进行（目录锁路径已存在且为目录：${DIR_LOCK_PATH}）。"
      err "确认当前没有 restore.sh 在目录 '${WORK_DIR_REAL}' 运行后，请手动删除："
      err "  rmdir $(printf '%q' "$DIR_LOCK_PATH")"
      exit 1
    fi
    local lock_open_ok=1
    exec 8>&2
    exec 2>/dev/null
    if ! exec 7>"$DIR_LOCK_PATH"; then
      lock_open_ok=0
    fi
    exec 2>&8
    exec 8>&-
    if [[ "$lock_open_ok" -ne 1 ]]; then
      err "无法打开部署目录恢复锁文件：${DIR_LOCK_PATH}"
      err "请检查权限并确认没有其他 restore.sh 正在目录 '${WORK_DIR_REAL}' 运行。"
      exit 1
    fi
    if ! flock -n 7; then
      exec 7>&-
      err "这个目录里已有另一个恢复在进行（目录锁已被占用：${DIR_LOCK_PATH}）。"
      err "如果确认当前没有其他 restore.sh 正在运行，请手动清理残留锁文件："
      err "  rm -f $(printf '%q' "$DIR_LOCK_PATH")"
      exit 1
    fi
    DIR_LOCK_MODE="flock"
    DIR_LOCK_HELD=1
  else
    if ! mkdir "$DIR_LOCK_PATH" 2>/dev/null; then
      err "这个目录里已有另一个恢复在进行（目录锁已存在：${DIR_LOCK_PATH}）。"
      err "确认当前没有 restore.sh 在目录 '${WORK_DIR_REAL}' 运行后，请手动删除残留锁："
      err "  rmdir $(printf '%q' "$DIR_LOCK_PATH")"
      exit 1
    fi
    DIR_LOCK_MODE="mkdir"
    DIR_LOCK_HELD=1
  fi
}

release_dir_lock() {
  if [[ "$DIR_LOCK_HELD" -ne 1 ]]; then
    return 0
  fi
  if [[ "$DIR_LOCK_MODE" == "flock" ]]; then
    flock -u 7 2>/dev/null || true
    exec 7>&-
  elif [[ "$DIR_LOCK_MODE" == "mkdir" && -n "$DIR_LOCK_PATH" && -d "$DIR_LOCK_PATH" ]]; then
    rmdir "$DIR_LOCK_PATH" 2>/dev/null || true
  fi
  DIR_LOCK_HELD=0
}

acquire_restore_lock() {
  if command -v flock >/dev/null 2>&1; then
    if [[ -d "$RESTORE_LOCK_PATH" ]]; then
      err "Restore lock path exists as a directory: ${RESTORE_LOCK_PATH}"
      err "Confirm no restore.sh process is running for project '${LOCK_PROJECT}', then remove it manually:"
      err "  rmdir $(printf '%q' "$RESTORE_LOCK_PATH")"
      exit 1
    fi
    # Redirect stderr ONLY while opening fd 9; restore fd 2 immediately so script
    # error output is never swallowed.
    local lock_open_ok=1
    exec 8>&2
    exec 2>/dev/null
    if ! exec 9>"$RESTORE_LOCK_PATH"; then
      lock_open_ok=0
    fi
    exec 2>&8
    exec 8>&-
    if [[ "$lock_open_ok" -ne 1 ]]; then
      err "Cannot open restore lock file: ${RESTORE_LOCK_PATH}"
      err "Confirm no restore.sh process is running for project '${LOCK_PROJECT}' and check permissions, then remove it manually if stale."
      exit 1
    fi
    if ! flock -n 9; then
      exec 9>&-
      err "Another restore is already running for project '${LOCK_PROJECT}' (lock held: ${RESTORE_LOCK_PATH})."
      err "If you believe this lock is stale, confirm no restore.sh process is running and remove it manually:"
      err "  rm -f $(printf '%q' "$RESTORE_LOCK_PATH")"
      exit 1
    fi
    RESTORE_LOCK_MODE="flock"
    RESTORE_LOCK_HELD=1
  else
    if ! mkdir "$RESTORE_LOCK_PATH" 2>/dev/null; then
      err "Restore lock already exists for project '${LOCK_PROJECT}': ${RESTORE_LOCK_PATH}"
      err "Another restore may be running, or an interrupted run left this lock behind."
      err "Confirm no restore.sh process is running for project '${LOCK_PROJECT}', then remove the lock manually:"
      err "  rmdir $(printf '%q' "$RESTORE_LOCK_PATH")"
      exit 1
    fi
    RESTORE_LOCK_MODE="mkdir"
    RESTORE_LOCK_HELD=1
  fi
}

release_restore_lock() {
  if [[ "$RESTORE_LOCK_HELD" -ne 1 ]]; then
    return 0
  fi
  if [[ "$RESTORE_LOCK_MODE" == "flock" ]]; then
    flock -u 9 2>/dev/null || true
    exec 9>&-
  elif [[ "$RESTORE_LOCK_MODE" == "mkdir" && -n "$RESTORE_LOCK_PATH" && -d "$RESTORE_LOCK_PATH" ]]; then
    rmdir "$RESTORE_LOCK_PATH" 2>/dev/null || true
  fi
  RESTORE_LOCK_HELD=0
}

cleanup() {
  local rc=$?
  if [[ -n "$ENV_BACKUP_PARTIAL" ]]; then
    rm -f "$ENV_BACKUP_PARTIAL"
  fi
  if [[ -n "$STAGED_ENV_TMP" && -f "$STAGED_ENV_TMP" ]]; then
    rm -f "$STAGED_ENV_TMP"
  fi
  if [[ -n "$MINIO_MANIFEST_TMP" && -f "$MINIO_MANIFEST_TMP" ]]; then
    rm -f "$MINIO_MANIFEST_TMP"
  fi
  if [[ -n "$COMPOSE_CFG_TMP" ]]; then
    rm -f "$COMPOSE_CFG_TMP" "${COMPOSE_CFG_TMP}.err"
  fi
  if [[ "$rc" -ne 0 && "$RESTORE_SUCCEEDED" -ne 1 && "$MUTATION_STARTED" -eq 0 ]]; then
    if [[ -n "$ENV_BACKUP_PATH" && ( -f "$ENV_BACKUP_PATH" || -L "$ENV_BACKUP_PATH" ) ]]; then
      if [[ -e "$ENV_FILE" || -L "$ENV_FILE" ]]; then
        rm -f "$ENV_FILE"
      fi
      if mv "$ENV_BACKUP_PATH" "$ENV_FILE"; then
        if [[ "$ENV_WAS_SYMLINK" -eq 0 && ! -L "$ENV_FILE" ]]; then
          chmod "$ORIG_ENV_MODE" "$ENV_FILE" 2>/dev/null || true
        fi
        ENV_BACKUP_PATH=""
        warn "已把 .env 恢复为恢复前的内容（${ENV_FILE}）。"
      else
        err "Failed to restore original ${ENV_FILE} from ${ENV_BACKUP_PATH}; restore it manually."
      fi
    elif [[ "$ENV_EXISTED_BEFORE" -eq 0 && "$ENV_FROM_BACKUP" -eq 1 && ( -f "$ENV_FILE" || -L "$ENV_FILE" ) ]]; then
      rm -f "$ENV_FILE"
      warn "已把 .env 恢复为恢复前的内容（已删除本次新写入的 ${ENV_FILE}）。"
    fi
  fi
  release_restore_lock
  release_dir_lock
  if [[ "$rc" -ne 0 && "$RESTORE_SUCCEEDED" -ne 1 && "$MUTATION_STARTED" -eq 1 ]]; then
    local proj="${COMPOSE_PROJECT:-<project>}"
    local down_cmd="docker compose -p ${proj} -f docker-compose.yml -f docker-compose.core.yml --env-file ${ENV_FILE} down -v"
    if [[ -n "$COMPOSE_PROJECT" && "${#COMPOSE_FILE_ARGS[@]}" -gt 0 ]]; then
      down_cmd="$(fmt_cmd docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" down -v)"
    fi
    err "Restore failed after writing data to target database or object storage; the instance is now in a partial/half-restored state (半成品)."
    if [[ -n "$ENV_BACKUP_PATH" && ( -f "$ENV_BACKUP_PATH" || -L "$ENV_BACKUP_PATH" ) ]]; then
      err "未自动换回 ${ENV_FILE}（写入开始后换回会导致配置与已写入数据不一致）；原 .env 备份路径：${ENV_BACKUP_PATH}，请按手册处理。"
    elif [[ "$ENV_FROM_BACKUP" -eq 1 && "$ENV_EXISTED_BEFORE" -eq 0 && ( -f "$ENV_FILE" || -L "$ENV_FILE" ) ]]; then
      err "未自动删除已写入的 ${ENV_FILE}（写入开始后删除会导致配置与已写入数据不一致），请按手册处理。"
    fi
    err "Your backup directory (${BACKUP_DIR}) was NOT modified."
    err "Follow ${MANUAL_OVERWRITE_REF} to confirm volumes and reset before re-running restore.sh:"
    err "  $(fmt_cmd docker volume ls --filter "label=com.docker.compose.project=${proj}")"
    err "  ${down_cmd}"
  fi
}

on_signal() {
  local sig_rc="$1"
  trap - INT TERM
  exit "$sig_rc"
}
trap cleanup EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

# Step 1: Verify checksums, manifest.json, and MinIO archive readability
info "1/7 Verifying backup archive integrity..."
if [[ -f "${BACKUP_DIR}/SHA256SUMS" ]]; then
  (
    cd "$BACKUP_DIR"
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum -c SHA256SUMS >/dev/null
    else
      shasum -a 256 -c SHA256SUMS >/dev/null
    fi
  )
  ok "SHA256SUMS verification passed."
else
  warn "No SHA256SUMS file found in ${BACKUP_DIR}; proceeding with structural checks."
fi

ENV_CIPHER="aes-256-cbc"
ENV_KDF="pbkdf2"
ENV_KDF_DIGEST="sha256"
ENV_KDF_ITER="600000"
if [[ -f "${BACKUP_DIR}/manifest.json" ]]; then
  IFS=$'\t' read -r ENV_CIPHER ENV_KDF ENV_KDF_DIGEST ENV_KDF_ITER < <(python3 - "${BACKUP_DIR}/manifest.json" <<'PY'
import json, sys

cipher, kdf, digest, iters = "aes-256-cbc", "pbkdf2", "sha256", 600000
with open(sys.argv[1], encoding="utf-8") as fh:
    data = json.load(fh)
enc = data.get("env_encryption")
if isinstance(enc, dict):
    cipher = str(enc.get("cipher") or cipher)
    kdf = str(enc.get("kdf") or kdf)
    digest = str(enc.get("digest") or digest)
    iters = int(enc.get("iterations") or iters)
if cipher not in ("aes-256-cbc",):
    print(f"ERROR: Unsupported env_encryption cipher: {cipher}", file=sys.stderr)
    sys.exit(1)
if kdf != "pbkdf2":
    print(f"ERROR: Unsupported env_encryption kdf: {kdf}", file=sys.stderr)
    sys.exit(1)
if digest not in ("sha256", "sha512"):
    print(f"ERROR: Unsupported env_encryption digest: {digest}", file=sys.stderr)
    sys.exit(1)
if iters < 10000 or iters > 10000000:
    print(f"ERROR: Unsupported env_encryption iterations: {iters}", file=sys.stderr)
    sys.exit(1)
print(f"{cipher}\t{kdf}\t{digest}\t{iters}")
PY
  )
fi

if [[ -f "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
  MINIO_ARCHIVE_SEEN=1
  MINIO_MANIFEST_TMP="$(mktemp)"
  chmod 600 "$MINIO_MANIFEST_TMP"
  if ! tar -xOzf "${BACKUP_DIR}/minio-objects.tar.gz" _minio_manifest.json > "$MINIO_MANIFEST_TMP"; then
    err "Failed to extract _minio_manifest.json from ${BACKUP_DIR}/minio-objects.tar.gz."
    exit 1
  fi
fi

# Step 2: Acquire deployment-directory lock FIRST (before any check or read of `.env`),
# then inspect `.env` existence/symlink/mode under the directory lock (decrypting
# `env.enc` ONLY when `.env` is absent or `--replace-env` is explicitly requested),
# then acquire the project lock SECOND, clean leftover `.env.tmp-*`, and run static
# preflight checks.
resolve_project_name_from_env() {
  local env_path="$1"
  local raw_proj proj
  raw_proj="$(read_env_file_var "$env_path" "COMPOSE_PROJECT_NAME" "")"
  if [[ -n "$raw_proj" ]]; then
    proj="$(normalize_env_project_name "$raw_proj")"
    validate_project_name "$proj" "COMPOSE_PROJECT_NAME"
  else
    proj="$(sanitize_project_name "$(basename "$WORK_DIR")")"
    validate_project_name "$proj" "COMPOSE_PROJECT_NAME"
  fi
  printf '%s' "$proj"
}

decrypt_backup_env() {
  local dest="$1"
  if [[ ! -r "$ENV_PASSPHRASE_FILE" || ! -s "$ENV_PASSPHRASE_FILE" ]]; then
    err "Passphrase file is missing, unreadable, or empty: ${ENV_PASSPHRASE_FILE}"
    exit 1
  fi
  chmod 600 "$dest"
  if ! openssl enc -d "-${ENV_CIPHER}" -pbkdf2 -md "${ENV_KDF_DIGEST}" -iter "${ENV_KDF_ITER}" \
      -in "${BACKUP_DIR}/env.enc" \
      -out "$dest" \
      -pass "file:${ENV_PASSPHRASE_FILE}"; then
    err "Failed to decrypt ${BACKUP_DIR}/env.enc (check passphrase file)."
    exit 1
  fi
  chmod 600 "$dest"
}

prepare_env_under_dir_lock() {
  # Inspect .env state strictly AFTER acquiring the deployment-directory lock.
  ENV_EXISTED_BEFORE=0
  ENV_WAS_SYMLINK=0
  ORIG_ENV_MODE="600"
  ENV_FROM_BACKUP=0
  CANDIDATE_ENV=""

  if [[ -L "$ENV_FILE" ]]; then
    ENV_EXISTED_BEFORE=1
    ENV_WAS_SYMLINK=1
  elif [[ -f "$ENV_FILE" ]]; then
    ENV_EXISTED_BEFORE=1
    ENV_WAS_SYMLINK=0
    ORIG_ENV_MODE="$(get_file_mode "$ENV_FILE")"
  fi

  # Keeping existing .env: never read ENV_PASSPHRASE_FILE and never decrypt env.enc.
  if [[ "$ENV_EXISTED_BEFORE" -eq 1 && "$REPLACE_ENV" -eq 0 ]]; then
    if [[ "$ENV_WAS_SYMLINK" -eq 1 && ! -f "$ENV_FILE" ]]; then
      err "${ENV_FILE} is a broken symlink (target does not exist or is not a regular file)."
      exit 1
    fi
    if [[ -f "${BACKUP_DIR}/env.enc" ]]; then
      info "2/7 Using existing ${ENV_FILE} (pass --replace-env to replace it from env.enc on an empty target)."
    else
      info "2/7 Using existing ${ENV_FILE}."
    fi
    CANDIDATE_ENV="$ENV_FILE"
    return 0
  fi

  # Replacing or creating .env from backup's env.enc.
  if [[ -f "${BACKUP_DIR}/env.enc" && -n "$ENV_PASSPHRASE_FILE" ]]; then
    info "2/7 Decrypting env.enc for preflight validation (${ENV_CIPHER}/${ENV_KDF}/${ENV_KDF_DIGEST}/${ENV_KDF_ITER})..."
    STAGED_ENV_TMP="$(mktemp "${WORK_DIR}/.env.tmp-decrypt-XXXXXX")"
    chmod 600 "$STAGED_ENV_TMP"
    decrypt_backup_env "$STAGED_ENV_TMP"
    ok "Decrypted env.enc into temporary staging file."
    ENV_FROM_BACKUP=1
    CANDIDATE_ENV="$STAGED_ENV_TMP"
  elif [[ "$ENV_EXISTED_BEFORE" -eq 1 ]]; then
    err "--replace-env requires ${BACKUP_DIR}/env.enc and --env-passphrase-file (or DALTON_BACKUP_PASSPHRASE_FILE)."
    exit 1
  else
    err "Missing ${ENV_FILE}. Copy your saved .env into ${WORK_DIR}, pass --env-passphrase-file (if env.enc is in the backup), or run scripts/standalone/init.sh first."
    exit 1
  fi

  # Exact verbatim COMPOSE_PROJECT_NAME check when replacing an existing .env (no normalization).
  if [[ "$ENV_EXISTED_BEFORE" -eq 1 && "$ENV_FROM_BACKUP" -eq 1 ]]; then
    CUR_PROJECT="$(read_env_file_var "$ENV_FILE" "COMPOSE_PROJECT_NAME" "")"
    NEW_PROJECT="$(read_env_file_var "$CANDIDATE_ENV" "COMPOSE_PROJECT_NAME" "")"
    if [[ "$NEW_PROJECT" != "$CUR_PROJECT" ]]; then
      err "COMPOSE_PROJECT_NAME mismatch: backup .env specifies '${NEW_PROJECT:-<unset>}', but current ${ENV_FILE} specifies '${CUR_PROJECT:-<unset>}'."
      err "Refusing --replace-env (exact verbatim match required; existing ${ENV_FILE} left untouched)."
      exit 1
    fi
  fi
}

# Fixed lock acquisition order:
#   1) deployment-directory lock (/tmp/dalton-restore-dir-<sha256[:16]>.lock)
#   2) project lock (/tmp/dalton-<project>.restore.lock)
acquire_dir_lock

if [[ "$PROJECT_NAME_EXPLICIT" -eq 1 ]]; then
  LOCK_PROJECT="$PROJECT_NAME_ARG"
  COMPOSE_PROJECT="$LOCK_PROJECT"
  RESTORE_LOCK_PATH="/tmp/dalton-${LOCK_PROJECT}.restore.lock"
  acquire_restore_lock
  prepare_env_under_dir_lock
else
  prepare_env_under_dir_lock
  LOCK_PROJECT="$(resolve_project_name_from_env "$CANDIDATE_ENV")"
  COMPOSE_PROJECT="$LOCK_PROJECT"
  RESTORE_LOCK_PATH="/tmp/dalton-${LOCK_PROJECT}.restore.lock"
  acquire_restore_lock
fi

# Clean up any leftover .env.tmp-* from a prior kill -9 (safe under project lock;
# never remove the current run's own decrypted STAGED_ENV_TMP).
STALE_ENV_TMPS=()
shopt -s nullglob
for tmpf in "${WORK_DIR}"/.env.tmp-*; do
  if [[ -n "$STAGED_ENV_TMP" && "$tmpf" == "$STAGED_ENV_TMP" ]]; then
    continue
  fi
  STALE_ENV_TMPS+=("$tmpf")
done
shopt -u nullglob
if (( ${#STALE_ENV_TMPS[@]} > 0 )); then
  warn "Removing leftover temporary .env file(s) from a previously interrupted restore: ${STALE_ENV_TMPS[*]}"
  rm -f "${STALE_ENV_TMPS[@]}"
fi

# Unified PostgreSQL configuration guard using strictly CANDIDATE_ENV via stdlib resolver
# (same DB_* > DATABASE_URL rule and conflict check as backend and backup).
if ! RESTORE_PG_CFG_JSON="$(python3 "${SCRIPT_DIR}/storage_config.py" resolve-pg-config \
    --env-file "$CANDIDATE_ENV" \
    --default-host postgres \
    --check-conflict \
    --caller restore)"; then
  exit 1
fi
RESTORE_PG_LINES="$(printf '%s' "$RESTORE_PG_CFG_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("host", "user", "password", "dbname"):
    print(d.get(k, ""))
')"
CFG_DB_HOST="$(printf '%s\n' "$RESTORE_PG_LINES" | sed -n '1p')"
if [[ "$CFG_DB_HOST" != "postgres" ]]; then
  err "External PostgreSQL detected (DB_HOST='${CFG_DB_HOST}'; expected Compose-managed 'postgres')."
  err "restore.sh only manages the Compose-managed PostgreSQL container. For external databases, restore directly with pg_restore."
  exit 1
fi

validate_db_identifiers() {
  local u="$1" n="$2"
  if ! [[ "$u" =~ ^[a-zA-Z0-9_]+$ ]] || ! [[ "$n" =~ ^[a-zA-Z0-9_]+$ ]]; then
    err "DB_USER and DB_NAME must contain only ASCII letters, digits, and underscores."
    exit 1
  fi
  if [[ "$n" == "template0" || "$n" == "template1" ]]; then
    err "Refusing to restore into PostgreSQL template database '${n}'."
    exit 1
  fi
}

# Read all target configuration strictly from CANDIDATE_ENV (never shell environment)
CFG_DB_USER="$(printf '%s\n' "$RESTORE_PG_LINES" | sed -n '2p')"
CFG_DB_PASSWORD="$(printf '%s\n' "$RESTORE_PG_LINES" | sed -n '3p')"
CFG_DB_NAME="$(printf '%s\n' "$RESTORE_PG_LINES" | sed -n '4p')"
CFG_DB_USER="${CFG_DB_USER:-postgres}"
CFG_DB_NAME="${CFG_DB_NAME:-postgres}"
CFG_DOMAIN="$(read_env_file_var "$CANDIDATE_ENV" "DOMAIN" "localhost")"
CFG_TLS_MODE="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_TLS_MODE" "loopback")"
CFG_EDGE_PORT="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_EDGE_PORT" "8088")"
CFG_EDGE_HTTPS_PORT="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_EDGE_HTTPS_PORT" "18443")"
CFG_EDGE_TLS_BIND="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_EDGE_TLS_BIND" "")"
CFG_CHAT_BUCKET="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" "chat-uploads")"
validate_db_identifiers "$CFG_DB_USER" "$CFG_DB_NAME"

# Build Compose file list from CANDIDATE_ENV
if [[ -z "$COMPOSE_FILES_RAW" ]]; then
  env_compose_file="$(read_env_file_var "$CANDIDATE_ENV" "COMPOSE_FILE" "")"
  if [[ -n "$env_compose_file" ]]; then
    COMPOSE_FILES_RAW="$env_compose_file"
  elif [[ "$CFG_TLS_MODE" != "loopback" && "$CFG_DOMAIN" == *:* && -f "${WORK_DIR}/deploy/standalone/docker-compose.tls.ipv6.yml" ]]; then
    COMPOSE_FILES_RAW="${WORK_DIR}/docker-compose.yml:${WORK_DIR}/docker-compose.core.yml:${WORK_DIR}/deploy/standalone/docker-compose.tls.ipv6.yml"
  elif [[ "$CFG_TLS_MODE" != "loopback" && -f "${WORK_DIR}/deploy/standalone/docker-compose.tls.yml" ]]; then
    COMPOSE_FILES_RAW="${WORK_DIR}/docker-compose.yml:${WORK_DIR}/docker-compose.core.yml:${WORK_DIR}/deploy/standalone/docker-compose.tls.yml"
  else
    COMPOSE_FILES_RAW="${WORK_DIR}/docker-compose.yml:${WORK_DIR}/docker-compose.core.yml"
  fi
fi

IFS=':' read -r -a COMPOSE_FILES_ARR <<< "$COMPOSE_FILES_RAW"
COMPOSE_FILE_ARGS=()
RESOLVED_COMPOSE_FILES=()
for cf in "${COMPOSE_FILES_ARR[@]}"; do
  [[ -z "$cf" ]] && continue
  if [[ "$cf" != /* ]]; then
    cf="${WORK_DIR}/${cf}"
  fi
  if [[ ! -f "$cf" ]]; then
    err "Compose file not found: ${cf}"
    exit 1
  fi
  COMPOSE_FILE_ARGS+=(-f "$cf")
  RESOLVED_COMPOSE_FILES+=("$cf")
done

# Collect every variable key appearing in CANDIDATE_ENV, existing .env, and
# compose file interpolations (plus COMPOSE_PROJECT_NAME, COMPOSE_FILE,
# COMPOSE_PROFILES) so every `docker compose` call unsets them via `env -u`.
ENV_UNSET_ARGS=()
while IFS= read -r env_key; do
  [[ -n "$env_key" ]] && ENV_UNSET_ARGS+=(-u "$env_key")
done < <(python3 - "$CANDIDATE_ENV" "$ENV_FILE" "${RESOLVED_COMPOSE_FILES[@]}" <<'PY'
import os, re, sys

cand_env, orig_env = sys.argv[1], sys.argv[2]
compose_files = sys.argv[3:]
line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[=:]")
interp_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
seen: set[str] = set()
for key in (
    "COMPOSE_PROJECT_NAME",
    "COMPOSE_FILE",
    "COMPOSE_PROFILES",
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "DATABASE_URL",
    "DALTON_RESEARCH_HOST_TOKEN",
    "DALTON_RESEARCH_ADMIN_TOKEN",
):
    seen.add(key)
    print(key)
for env_path in (cand_env, orig_env):
    if not env_path or not os.path.isfile(env_path):
        continue
    with open(env_path, encoding="utf-8") as fh:
        for raw in fh:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = line_re.match(raw)
            if not m:
                continue
            key = m.group(1)
            if key == "PATH":
                continue
            if key not in seen:
                seen.add(key)
                print(key)
for cf in compose_files:
    with open(cf, encoding="utf-8") as fh:
        text = fh.read()
    for m in interp_re.finditer(text):
        key = m.group(1)
        if key == "PATH":
            continue
        if key not in seen:
            seen.add(key)
            print(key)
PY
)

# When restoring from env.enc, swap the decrypted file into place at $ENV_FILE
# BEFORE any compose-dependent preflight so preflight, storage config comparison,
# writes, and startup all use the exact same regular `docker compose` arguments
# (no temporary compose override file). If any step fails before data writes
# begin, cleanup() restores the original .env (or deletes the newly placed one).
if [[ "$ENV_FROM_BACKUP" -eq 1 ]]; then
  if [[ "$ENV_EXISTED_BEFORE" -eq 1 ]]; then
    env_bak_ts="$(date -u +%Y%m%d-%H%M%SZ)"
    env_bak_candidate="${ENV_FILE}.pre-restore-${env_bak_ts}"
    if [[ -e "$env_bak_candidate" || -L "$env_bak_candidate" ]]; then
      env_bak_seq=1
      while [[ -e "${env_bak_candidate}-${env_bak_seq}" || -L "${env_bak_candidate}-${env_bak_seq}" ]]; do
        env_bak_seq=$((env_bak_seq + 1))
      done
      env_bak_candidate="${env_bak_candidate}-${env_bak_seq}"
    fi
    if [[ "$ENV_WAS_SYMLINK" -eq 1 ]]; then
      # A rename is atomic: either the link moved and the backup exists, or nothing
      # changed and cleanup finds no backup to put back.
      ENV_BACKUP_PATH="$env_bak_candidate"
      mv "$ENV_FILE" "$ENV_BACKUP_PATH"
      info "Backed up existing symlink ${ENV_FILE} to ${ENV_BACKUP_PATH} (symlink preserved; target untouched)."
    else
      # Copy to a side name and compare before naming it the backup, so a copy cut
      # short (a full disk) is never what cleanup puts back over the intact .env.
      env_bak_tmp="${env_bak_candidate}.partial"
      ENV_BACKUP_PARTIAL="$env_bak_tmp"
      if ! { cp -P -p "$ENV_FILE" "$env_bak_tmp" && chmod 600 "$env_bak_tmp" && cmp -s "$ENV_FILE" "$env_bak_tmp"; }; then
        rm -f "$env_bak_tmp"
        err "Could not back up ${ENV_FILE} (disk full?). Nothing was changed."
        exit 1
      fi
      mv "$env_bak_tmp" "$env_bak_candidate"
      ENV_BACKUP_PARTIAL=""
      ENV_BACKUP_PATH="$env_bak_candidate"
      info "Backed up existing ${ENV_FILE} to ${ENV_BACKUP_PATH} (mode 600)."
    fi
  fi
  chmod 600 "$STAGED_ENV_TMP"
  mv "$STAGED_ENV_TMP" "$ENV_FILE"
  STAGED_ENV_TMP=""
  CANDIDATE_ENV="$ENV_FILE"
  ok "Atomically placed decrypted backup .env at ${ENV_FILE} (mode 600)."
fi

compose_cmd() {
  env "${ENV_UNSET_ARGS[@]}" docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" "$@"
}

service_is_running() {
  local svc="$1"
  compose_cmd ps --status running --services 2>/dev/null | grep -qx "$svc"
}

# Before starting containers or writing .env/data, render the effective Compose
# configuration (`docker compose -p ... -f ... --env-file ... config --format json`)
# and validate the `backend` service's actual effective storage environment
# (chat_upload, ASR, general: bucket, endpoint, key prefix) against the backup
# manifest. `.env` is kept only as an auxiliary reference; `compose config` is
# authoritative.
COMPOSE_CFG_TMP="$(mktemp)"
chmod 600 "$COMPOSE_CFG_TMP"
set +e
compose_cmd config --format json > "$COMPOSE_CFG_TMP" 2>"${COMPOSE_CFG_TMP}.err"
COMPOSE_CFG_RC=$?
set -e
if [[ "$COMPOSE_CFG_RC" -ne 0 ]]; then
  err "Failed to render effective Docker Compose configuration via 'docker compose config --format json':"
  err "  $(cat "${COMPOSE_CFG_TMP}.err")"
  exit 1
fi

# Re-resolve effective backend PostgreSQL config after Compose interpolation so backup,
# backend, and restore agree on one exact DB target and reject any DB_* vs DATABASE_URL conflict.
if ! RESTORE_COMPOSE_PG_JSON="$(python3 "${SCRIPT_DIR}/storage_config.py" resolve-pg-config \
    --env-file "$ENV_FILE" \
    --compose-config-json "$COMPOSE_CFG_TMP" \
    --service backend \
    --default-host postgres \
    --check-conflict \
    --caller restore)"; then
  exit 1
fi
RESTORE_COMPOSE_PG_LINES="$(printf '%s' "$RESTORE_COMPOSE_PG_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("host", "user", "password", "dbname"):
    print(d.get(k, ""))
')"
CFG_DB_HOST="$(printf '%s\n' "$RESTORE_COMPOSE_PG_LINES" | sed -n '1p')"
if [[ "$CFG_DB_HOST" != "postgres" && "$CFG_DB_HOST" != "127.0.0.1" && "$CFG_DB_HOST" != "localhost" ]]; then
  err "External PostgreSQL detected in effective backend Compose config (host='${CFG_DB_HOST}'; expected Compose-managed 'postgres')."
  exit 1
fi
CFG_DB_USER="$(printf '%s\n' "$RESTORE_COMPOSE_PG_LINES" | sed -n '2p')"
CFG_DB_PASSWORD="$(printf '%s\n' "$RESTORE_COMPOSE_PG_LINES" | sed -n '3p')"
CFG_DB_NAME="$(printf '%s\n' "$RESTORE_COMPOSE_PG_LINES" | sed -n '4p')"
CFG_DB_USER="${CFG_DB_USER:-postgres}"
CFG_DB_NAME="${CFG_DB_NAME:-postgres}"
validate_db_identifiers "$CFG_DB_USER" "$CFG_DB_NAME"

STORAGE_CHECK_ARGS=(
  python3 "${SCRIPT_DIR}/storage_config.py" check-env
  --compose-config-json "$COMPOSE_CFG_TMP"
  --service backend
  --env-file "$ENV_FILE"
  --work-dir "$WORK_DIR"
  --caller restore
  --json
)
if [[ "$SKIP_MINIO" -eq 1 ]]; then
  STORAGE_CHECK_ARGS+=(--require-none-for-skip-minio)
fi
if ! STORAGE_CFG_JSON="$("${STORAGE_CHECK_ARGS[@]}")"; then
  exit 1
fi
IFS=$'\t' read -r STORAGE_ANY_CONFIGURED RESOLVED_PRIMARY_BUCKET < <(
  printf '%s' "$STORAGE_CFG_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print(("1" if d.get("any_configured") else "0") + "\t" + (d.get("primary_bucket") or ""))
'
)
if [[ -n "$RESOLVED_PRIMARY_BUCKET" ]]; then
  CFG_CHAT_BUCKET="$RESOLVED_PRIMARY_BUCKET"
fi

if [[ "$SKIP_MINIO" -eq 0 && ( -n "$MINIO_MANIFEST_TMP" || -f "${BACKUP_DIR}/manifest.json" ) ]]; then
  CMP_ARGS=(
    python3 "${SCRIPT_DIR}/storage_config.py" compare-backup
    --compose-config-json "$COMPOSE_CFG_TMP"
    --service backend
    --env-file "$ENV_FILE"
    --work-dir "$WORK_DIR"
    --caller restore
  )
  if [[ -n "$MINIO_MANIFEST_TMP" && -f "$MINIO_MANIFEST_TMP" ]]; then
    CMP_ARGS+=(--minio-manifest "$MINIO_MANIFEST_TMP")
  fi
  if [[ -f "${BACKUP_DIR}/manifest.json" ]]; then
    CMP_ARGS+=(--backup-manifest "${BACKUP_DIR}/manifest.json")
  fi
  if ! "${CMP_ARGS[@]}"; then
    exit 1
  fi
fi

# Step 3: Verify entire target PostgreSQL instance and object storage are empty
# and validate postgres.dump before writing any data.
info "3/7 Verifying target instance is empty and validating postgres.dump..."
PERSONAL_INTEGRATIONS_RESET_SQL="${WORK_DIR}/scripts/dalton/sql/personal_integrations_restore_reset.sql"
if [[ ! -f "$PERSONAL_INTEGRATIONS_RESET_SQL" || ! -r "$PERSONAL_INTEGRATIONS_RESET_SQL" || ! -s "$PERSONAL_INTEGRATIONS_RESET_SQL" ]]; then
  err "Required personal-integration restore reset SQL is missing, unreadable, or empty: ${PERSONAL_INTEGRATIONS_RESET_SQL}"
  exit 1
fi
if ! service_is_running postgres; then
  compose_cmd up -d postgres
fi

info "Waiting for PostgreSQL to become ready..."
pg_ready=0
for _ in $(seq 1 60); do
  if compose_cmd exec -T postgres pg_isready -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d template1 -q >/dev/null 2>&1; then
    pg_ready=1
    break
  fi
  sleep 2
done
if [[ "$pg_ready" -ne 1 ]]; then
  err "PostgreSQL did not become ready within 120s."
  exit 1
fi

# Validate the dump with pg_restore --list before mutating anything
if ! compose_cmd exec -T postgres pg_restore --list < "${BACKUP_DIR}/postgres.dump" >/dev/null; then
  err "Backup file ${BACKUP_DIR}/postgres.dump failed pg_restore --list validation."
  exit 1
fi

# Whole-instance PostgreSQL empty check using the candidate/backup superuser
# credentials over TCP (rejects credential mismatches and non-superusers).
set +e
PG_SUPER_CHECK="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
  psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 -Atqc \
  "SELECT current_setting('is_superuser');" 2>&1)"
PG_SUPER_RC=$?
set -e
if [[ "$PG_SUPER_RC" -ne 0 ]]; then
  err "Failed to connect to target PostgreSQL instance as '${CFG_DB_USER}' using the superuser credentials from the target/backup .env:"
  err "  ${PG_SUPER_CHECK}"
  err "If this PostgreSQL volume was initialized with different credentials, follow ${MANUAL_OVERWRITE_REF} to reset it first."
  exit 1
fi
PG_SUPER_VAL="$(printf '%s' "$PG_SUPER_CHECK" | tr -d '[:space:]')"
if [[ "$PG_SUPER_VAL" != "on" ]]; then
  err "PostgreSQL user '${CFG_DB_USER}' is not a superuser (is_superuser='${PG_SUPER_VAL}'). Superuser credentials are required to inspect every database in the PostgreSQL instance."
  exit 1
fi

set +e
PG_ALL_DBS_RAW="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
  psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 -Atqc \
  "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('template0', 'template1') ORDER BY datname;" 2>&1)"
PG_ALL_DBS_RC=$?
set -e
if [[ "$PG_ALL_DBS_RC" -ne 0 ]]; then
  err "Failed to enumerate non-template databases from pg_database: ${PG_ALL_DBS_RAW}"
  exit 1
fi

DB_EXISTS=0
CHECKED_DBS=()
NON_EMPTY_APP_DESC=""
NON_EMPTY_OTHER_DBS=()
NON_EMPTY_OTHER_DESCS=()
PG_EMPTY_SQL="SELECT \
  (SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace \
   WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') \
     AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'), \
  (SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace \
   WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') \
     AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'), \
  (SELECT COUNT(*) FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace \
   WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') \
     AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%');"

while IFS= read -r db_name; do
  db_name="$(printf '%s' "$db_name" | tr -d '\r')"
  [[ -z "$db_name" ]] && continue
  CHECKED_DBS+=("$db_name")
  if [[ "$db_name" == "$CFG_DB_NAME" ]]; then
    DB_EXISTS=1
  fi
  set +e
  db_counts_raw="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
    psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d "$db_name" -v ON_ERROR_STOP=1 -F $'\t' -Atqc \
    "$PG_EMPTY_SQL" 2>&1)"
  db_counts_rc=$?
  set -e
  if [[ "$db_counts_rc" -ne 0 ]]; then
    err "Failed to connect to or inspect PostgreSQL database '${db_name}': ${db_counts_raw}"
    exit 1
  fi
  db_counts_line="$(printf '%s' "$db_counts_raw" | tr -d '\r' | tail -n 1)"
  IFS=$'\t' read -r rel_cnt proc_cnt type_cnt <<< "$db_counts_line"
  if [[ ! "$rel_cnt" =~ ^[0-9]+$ || ! "$proc_cnt" =~ ^[0-9]+$ || ! "$type_cnt" =~ ^[0-9]+$ ]]; then
    err "Could not parse user-object counts for PostgreSQL database '${db_name}' (got '${db_counts_line}')."
    exit 1
  fi
  total_cnt=$(( rel_cnt + proc_cnt + type_cnt ))
  if (( total_cnt > 0 )); then
    desc="Target PostgreSQL database '${db_name}' is NOT empty (${total_cnt} user object(s): relations=${rel_cnt}, functions=${proc_cnt}, types=${type_cnt})."
    if [[ "$db_name" == "$CFG_DB_NAME" ]]; then
      NON_EMPTY_APP_DESC="$desc"
    else
      NON_EMPTY_OTHER_DBS+=("$db_name")
      NON_EMPTY_OTHER_DESCS+=("$desc")
    fi
  fi
done <<< "$PG_ALL_DBS_RAW"

if (( ${#NON_EMPTY_OTHER_DBS[@]} > 0 )); then
  for desc in "${NON_EMPTY_OTHER_DESCS[@]}"; do
    err "$desc"
  done
  if [[ -n "$NON_EMPTY_APP_DESC" ]]; then
    err "$NON_EMPTY_APP_DESC"
  fi
  OTHER_DB_LIST="$(printf '%s, ' "${NON_EMPTY_OTHER_DBS[@]}")"
  OTHER_DB_LIST="${OTHER_DB_LIST%, }"
  err "这个 PostgreSQL 实例里还有其他数据库（${OTHER_DB_LIST}），backup.sh 只备份 ${CFG_DB_NAME}。请先自行备份这些库，确认不再需要后再按手册清空实例（参见 ${MANUAL_OVERWRITE_REF}）。"
  exit 1
fi

if [[ -n "$NON_EMPTY_APP_DESC" ]]; then
  err "$NON_EMPTY_APP_DESC"
  print_non_empty_manual_hint
  exit 1
fi

if (( ${#CHECKED_DBS[@]} == 0 )); then
  err "pg_database returned zero non-template databases; refusing restore."
  exit 1
fi
ok "Confirmed entire PostgreSQL instance is empty across ${#CHECKED_DBS[@]} non-template database(s): ${CHECKED_DBS[*]}."

if [[ "$SKIP_MINIO" -eq 0 && "$STORAGE_ANY_CONFIGURED" -eq 1 ]]; then
  if [[ ! -f "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
    err "Application has object storage configured, but backup is missing ${BACKUP_DIR}/minio-objects.tar.gz."
    exit 1
  fi
  if ! service_is_running minio; then
    compose_cmd up -d minio
  fi

  MINIO_PREFLIGHT_PY='
from __future__ import annotations
import os, sys, time
from pathlib import Path
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path("/app")))
from scripts.standalone.storage_config import configured_minio_init_buckets

def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default

endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "DALTON_ASR_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", "S3_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
allowed_init_buckets = set(configured_minio_init_buckets(os.environ))
access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "DALTON_ASR_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID", "S3_ACCESS_KEY_ID")
secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY")
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "DALTON_ASR_STORAGE_REGION", "STORAGE_REGION", "S3_REGION", default="us-east-1")
addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "DALTON_ASR_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()

if not access_key or not secret_key:
    print("ERROR: MinIO credentials missing in container environment.", file=sys.stderr)
    sys.exit(1)

client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name=region,
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": addressing_style},
        connect_timeout=5,
        read_timeout=30,
        retries={"max_attempts": 5, "mode": "standard"},
    ),
)

resp = None
last_err = None
for _ in range(15):
    try:
        resp = client.list_buckets()
        break
    except (ClientError, BotoCoreError) as exc:
        last_err = exc
        time.sleep(2)
if resp is None:
    print(f"ERROR: MinIO not reachable for preflight: {last_err}", file=sys.stderr)
    sys.exit(1)

existing_buckets = sorted(b["Name"] for b in resp.get("Buckets", []) if b.get("Name"))
unexpected_buckets = [b for b in existing_buckets if b not in allowed_init_buckets]
non_empty: list[tuple[str, int]] = []
for bucket in existing_buckets:
    count = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        count += len(page.get("Contents") or [])
    if count > 0:
        non_empty.append((bucket, count))

if unexpected_buckets or non_empty:
    parts: list[str] = []
    if unexpected_buckets:
        parts.append("unexpected bucket(s): " + ", ".join(unexpected_buckets))
    if non_empty:
        parts.append("non-empty bucket(s): " + ", ".join(f"{b}({c})" for b, c in non_empty))
    print("; ".join(parts))
    sys.exit(10)

print("EMPTY")
'
  set +e
  MINIO_PREFLIGHT_OUT="$(compose_cmd run --rm -T --no-deps \
    backend uv run --no-sync python -c "$MINIO_PREFLIGHT_PY" 2>&1)"
  MINIO_PREFLIGHT_RC=$?
  set -e
  if [[ "$MINIO_PREFLIGHT_RC" -eq 10 ]]; then
    err "Target object storage is NOT empty (${MINIO_PREFLIGHT_OUT})."
    print_non_empty_manual_hint
    exit 1
  elif [[ "$MINIO_PREFLIGHT_RC" -ne 0 ]]; then
    err "Failed to inspect target object storage: ${MINIO_PREFLIGHT_OUT}"
    exit 1
  fi
  ok "Confirmed target object storage is empty (no objects; only empty minio-init bucket allowed)."
fi

if [[ "$ENV_FROM_BACKUP" -eq 0 ]]; then
  warn "Using existing ${ENV_FILE} (not overwritten from env.enc)."
  warn "If BYOK_ENCRYPTION_KEY in ${ENV_FILE} differs from the backup's original .env, saved LLM API keys will fail to decrypt."
fi

# Ensure infrastructure services are up before starting data writes
compose_cmd up -d postgres redis minio

# Step 4: Restore MinIO objects directly into original bucket name(s) with read-back verification
if [[ "$MINIO_ARCHIVE_SEEN" -eq 0 && -e "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
  err "${BACKUP_DIR}/minio-objects.tar.gz appeared after the preflight checks; the backup directory is still changing. Wait until it is complete and run restore again."
  exit 1
fi
if [[ "$SKIP_MINIO" -eq 0 && "$STORAGE_ANY_CONFIGURED" -eq 1 && "$MINIO_ARCHIVE_SEEN" -eq 1 ]]; then
  info "4/7 Restoring MinIO objects into original bucket(s) with read-back verification..."

  MINIO_RESTORE_PY='
from __future__ import annotations
import hashlib, json, os, sys, tarfile
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CHUNK_BYTES = 1024 * 1024        # 1 MiB read/verify chunks
PART_BYTES = 8 * 1024 * 1024     # 8 MiB multipart upload parts (>= S3 5 MiB min)
RESTORE_MARKER_KEY = ".dalton-restore.json"

def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default

endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "DALTON_ASR_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", "S3_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "DALTON_ASR_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID", "S3_ACCESS_KEY_ID")
secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY", "S3_SECRET_ACCESS_KEY")
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "DALTON_ASR_STORAGE_REGION", "STORAGE_REGION", "S3_REGION", default="us-east-1")
addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "DALTON_ASR_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()

if not access_key or not secret_key:
    print("ERROR: MinIO credentials missing in container environment.", file=sys.stderr)
    sys.exit(1)

meta_len_line = sys.stdin.buffer.readline()
if not meta_len_line:
    print("ERROR: Missing manifest header on restore stream.", file=sys.stderr)
    sys.exit(1)
meta_len = int(meta_len_line.decode("ascii").strip())
manifest_raw = sys.stdin.buffer.read(meta_len)
if len(manifest_raw) != meta_len:
    print("ERROR: Truncated manifest header on restore stream.", file=sys.stderr)
    sys.exit(1)
manifest = json.loads(manifest_raw.decode("utf-8"))

expected_objects = [
    item for item in (manifest.get("objects") or [])
    if item.get("key") != RESTORE_MARKER_KEY
]
manifest_by_pair: dict[tuple[str, str], dict[str, object]] = {}
for item in expected_objects:
    pair = (str(item["bucket"]), str(item["key"]).lstrip("/"))
    if pair in manifest_by_pair:
        print(f"ERROR: Manifest contains duplicate object {pair!r}", file=sys.stderr)
        sys.exit(1)
    manifest_by_pair[pair] = item

source_buckets: set[str] = set(manifest.get("buckets") or [])
for item in expected_objects:
    source_buckets.add(str(item["bucket"]))
if not source_buckets:
    primary = str(manifest.get("primary_bucket") or "").strip() or "chat-uploads"
    source_buckets.add(primary)

client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name=region,
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": addressing_style},
        connect_timeout=5,
        read_timeout=120,
        retries={"max_attempts": 5, "mode": "standard"},
    ),
)

def ensure_private_bucket(bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in ("404", "NoSuchBucket", "NotFound") or status == 404:
            kwargs: dict[str, object] = {"Bucket": bucket}
            if region and region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            client.create_bucket(**kwargs)
        else:
            raise
    try:
        client.delete_bucket_policy(Bucket=bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code != "NoSuchBucketPolicy":
            raise

for b in sorted(source_buckets):
    ensure_private_bucket(b)

def _read_exact(fp, max_bytes: int) -> bytes:
    parts: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        chunk = fp.read(min(remaining, CHUNK_BYTES))
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    return b"".join(parts)

def upload_streaming(bucket: str, key: str, fp, total_size: int, content_type: str) -> tuple[int, str]:
    hasher = hashlib.sha256()
    written = 0
    if total_size <= PART_BYTES:
        buf = _read_exact(fp, total_size)
        hasher.update(buf)
        written = len(buf)
        client.put_object(Bucket=bucket, Key=key, Body=buf, ContentType=content_type)
        return written, hasher.hexdigest()

    mpu = client.create_multipart_upload(Bucket=bucket, Key=key, ContentType=content_type)
    upload_id = mpu["UploadId"]
    etags: list[dict[str, object]] = []
    part_num = 1
    try:
        while True:
            part_bytes = _read_exact(fp, PART_BYTES)
            if not part_bytes:
                break
            hasher.update(part_bytes)
            written += len(part_bytes)
            res = client.upload_part(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=part_num,
                Body=part_bytes,
            )
            etags.append({"ETag": res["ETag"], "PartNumber": part_num})
            part_num += 1
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": etags},
        )
    except Exception:
        try:
            client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except Exception:
            pass
        raise
    return written, hasher.hexdigest()

uploaded_pairs: set[tuple[str, str]] = set()
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as tf:
    for member in tf:
        if not member.isfile():
            continue
        if member.name == "_minio_manifest.json":
            fp = tf.extractfile(member)
            if fp is not None:
                while fp.read(CHUNK_BYTES):
                    pass
            continue
        if not member.name.startswith("objects/"):
            continue
        rel = member.name[len("objects/"):]
        bucket, sep, key = rel.partition("/")
        if not sep or not bucket or not key or key == RESTORE_MARKER_KEY:
            fp = tf.extractfile(member)
            if fp is not None:
                while fp.read(CHUNK_BYTES):
                    pass
            continue
        fp = tf.extractfile(member)
        if fp is None:
            continue
        if bucket not in source_buckets:
            print(f"ERROR: Archive contains object for undeclared bucket {bucket!r}", file=sys.stderr)
            sys.exit(1)
        if (bucket, key) not in manifest_by_pair:
            print(f"ERROR: Archive contains undeclared object {(bucket, key)!r} not present in manifest", file=sys.stderr)
            sys.exit(1)
        if (bucket, key) in uploaded_pairs:
            print(f"ERROR: Archive contains duplicate object {(bucket, key)!r}", file=sys.stderr)
            sys.exit(1)
        meta = manifest_by_pair[(bucket, key)]
        content_type = (
            str(meta.get("content_type") or "").strip()
            or str((member.pax_headers or {}).get("DALTON.content_type") or "").strip()
            or "application/octet-stream"
        )
        upload_streaming(bucket, key, fp, member.size, content_type)
        uploaded_pairs.add((bucket, key))

expected_pairs = set(manifest_by_pair.keys())
if uploaded_pairs != expected_pairs:
    missing_up = sorted(expected_pairs - uploaded_pairs)[:10]
    extra_up = sorted(uploaded_pairs - expected_pairs)[:10]
    print(
        f"ERROR: Restored key set mismatch: uploaded={len(uploaded_pairs)}, expected={len(expected_pairs)} "
        f"(missing_sample={missing_up}, extra_sample={extra_up})",
        file=sys.stderr,
    )
    sys.exit(1)

# Post-import bucket listing check: actual key set across all restored buckets must match manifest exactly.
actual_pairs: set[tuple[str, str]] = set()
for b in sorted(source_buckets):
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=b):
        for entry in page.get("Contents") or []:
            k = str(entry.get("Key") or "")
            if k and k != RESTORE_MARKER_KEY:
                actual_pairs.add((b, k))

if actual_pairs != expected_pairs:
    missing_live = sorted(expected_pairs - actual_pairs)[:10]
    extra_live = sorted(actual_pairs - expected_pairs)[:10]
    print(
        f"ERROR: Post-import object key set mismatch against manifest: "
        f"actual={len(actual_pairs)}, expected={len(expected_pairs)} "
        f"(missing_sample={missing_live}, extra_sample={extra_live})",
        file=sys.stderr,
    )
    sys.exit(1)

# Read-back verification: size, SHA-256, and ContentType
for item in expected_objects:
    bucket = str(item["bucket"])
    key = str(item["key"])
    exp_size = int(item["size"])
    exp_sha = str(item["sha256"])
    exp_ct = str(item.get("content_type") or "application/octet-stream")
    resp = client.get_object(Bucket=bucket, Key=key)
    body = resp["Body"]
    hasher = hashlib.sha256()
    got_size = 0
    try:
        for chunk in body.iter_chunks(chunk_size=CHUNK_BYTES):
            if chunk:
                hasher.update(chunk)
                got_size += len(chunk)
    finally:
        body.close()
    got_sha = hasher.hexdigest()
    got_ct = resp.get("ContentType") or "application/octet-stream"
    if got_size != exp_size or got_sha != exp_sha:
        print(
            f"ERROR: Read-back mismatch for {(bucket, key)}: "
            f"got=({got_size}, {got_sha}), expected=({exp_size}, {exp_sha})",
            file=sys.stderr,
        )
        sys.exit(1)
    if got_ct != exp_ct:
        print(
            f"ERROR: Read-back ContentType mismatch for {(bucket, key)}: "
            f"got={got_ct!r}, expected={exp_ct!r}",
            file=sys.stderr,
        )
        sys.exit(1)

print(
    f"[restore] Restored and read-back verified {len(uploaded_pairs)} object(s) into "
    f"{len(source_buckets)} bucket(s): {sorted(source_buckets)}.",
    flush=True,
)
'
  if [[ -z "$MINIO_MANIFEST_TMP" || ! -f "$MINIO_MANIFEST_TMP" || ! -r "$MINIO_MANIFEST_TMP" ]]; then
    err "MinIO manifest temporary file is missing or not readable: ${MINIO_MANIFEST_TMP:-<unset>}"
    exit 1
  fi
  manifest_size="$(wc -c < "$MINIO_MANIFEST_TMP" | tr -d '[:space:]')"
  if [[ ! "$manifest_size" =~ ^[0-9]+$ ]]; then
    err "Could not determine MinIO manifest size from ${MINIO_MANIFEST_TMP}."
    exit 1
  fi
  if [[ ! -f "${BACKUP_DIR}/minio-objects.tar.gz" || ! -r "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
    err "MinIO archive is missing or not readable: ${BACKUP_DIR}/minio-objects.tar.gz"
    exit 1
  fi
  if [[ ! -f "${BACKUP_DIR}/postgres.dump" || ! -r "${BACKUP_DIR}/postgres.dump" ]]; then
    err "PostgreSQL dump is missing or not readable: ${BACKUP_DIR}/postgres.dump"
    exit 1
  fi
  # Mark mutation boundary immediately before the first actual write to object storage.
  MUTATION_STARTED=1
  {
    printf '%s\n' "$manifest_size"
    cat "$MINIO_MANIFEST_TMP"
    cat "${BACKUP_DIR}/minio-objects.tar.gz"
  } | compose_cmd run --rm -T --no-deps backend uv run --no-sync python -c "$MINIO_RESTORE_PY"
  ok "MinIO objects restored into original bucket(s) and verified."
else
  info "4/7 Skipping MinIO restore."
fi

# Step 5: Restore PostgreSQL directly into official database, run migrations, and verify DB object references
info "5/7 Restoring PostgreSQL directly into '${CFG_DB_NAME}' and running migrations..."
if [[ ! -f "${BACKUP_DIR}/postgres.dump" || ! -r "${BACKUP_DIR}/postgres.dump" ]]; then
  err "PostgreSQL dump is missing or not readable: ${BACKUP_DIR}/postgres.dump"
  exit 1
fi
# Mark mutation boundary immediately before the first actual write to PostgreSQL
# (in case Step 4 MinIO restore was skipped).
MUTATION_STARTED=1
if [[ "$DB_EXISTS" -eq 0 ]]; then
  compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
    psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE \"${CFG_DB_NAME}\" OWNER \"${CFG_DB_USER}\";"
fi

compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres pg_restore \
  -h 127.0.0.1 -p 5432 \
  -U "$CFG_DB_USER" \
  -d "$CFG_DB_NAME" \
  --no-owner \
  --no-acl \
  --exit-on-error < "${BACKUP_DIR}/postgres.dump"
ok "Database '${CFG_DB_NAME}' restored."

info "Running migrations against '${CFG_DB_NAME}'..."
compose_cmd run --rm migrate
ok "Database '${CFG_DB_NAME}' migrated to current heads."

# Clear & restore terminal `asr-jobs` volume BEFORE post-restore convergence and BEFORE
# starting backend or transcription services, so pre-restore directories can never be
# auto-resumed by `recover_incomplete_jobs`.
RESTORE_HAS_TRANSCRIPTION=0
RESTORE_ASR_VOLUME="${COMPOSE_PROJECT}_dalton-asr-jobs"
if compose_cmd --profile transcription config --services 2>/dev/null | grep -qx "transcription"; then
  RESTORE_HAS_TRANSCRIPTION=1
  RESTORE_ASR_CFG_JSON="$(compose_cmd --profile transcription config --format json 2>/dev/null || true)"
  if [[ -n "$RESTORE_ASR_CFG_JSON" ]]; then
    RESOLVED_RESTORE_ASR_VOL="$(printf '%s' "$RESTORE_ASR_CFG_JSON" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
vol = (cfg.get("volumes") or {}).get("dalton-asr-jobs") or {}
print(str(vol.get("name") or ""))
' 2>/dev/null || true)"
    if [[ -n "$RESOLVED_RESTORE_ASR_VOL" ]]; then
      RESTORE_ASR_VOLUME="$RESOLVED_RESTORE_ASR_VOL"
    fi
  fi
  if [[ -f "${BACKUP_DIR}/asr-jobs.tar.gz" ]]; then
    info "Restoring terminal ASR jobs archive into /var/lib/dalton/asr_jobs..."
    if ! compose_cmd --profile transcription run --rm -T --no-deps --entrypoint /bin/sh transcription \
        -c 'set -e; mkdir -p /var/lib/dalton/asr_jobs; find /var/lib/dalton/asr_jobs -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; if [ -n "$(ls -A /var/lib/dalton/asr_jobs)" ]; then echo "ERROR: /var/lib/dalton/asr_jobs is not empty after clearing" >&2; exit 1; fi; tar -xzf - -C /var/lib/dalton/asr_jobs' \
        < "${BACKUP_DIR}/asr-jobs.tar.gz"; then
      err "Failed to clear and extract terminal ASR jobs from ${BACKUP_DIR}/asr-jobs.tar.gz; refusing to start services."
      exit 1
    fi
    ok "Terminal ASR jobs archive restored into /var/lib/dalton/asr_jobs."
  else
    info "Clearing /var/lib/dalton/asr_jobs volume (no asr-jobs.tar.gz in backup)..."
    if ! compose_cmd --profile transcription run --rm -T --no-deps --entrypoint /bin/sh transcription \
        -c 'set -e; mkdir -p /var/lib/dalton/asr_jobs; find /var/lib/dalton/asr_jobs -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; if [ -n "$(ls -A /var/lib/dalton/asr_jobs)" ]; then echo "ERROR: /var/lib/dalton/asr_jobs is not empty after clearing" >&2; exit 1; fi'; then
      err "Failed to clear /var/lib/dalton/asr_jobs volume before service start; refusing to start services."
      exit 1
    fi
    ok "Cleared /var/lib/dalton/asr_jobs volume."
  fi
elif [[ -f "${BACKUP_DIR}/asr-jobs.tar.gz" ]]; then
  err "Backup includes asr-jobs.tar.gz, but transcription service is not defined in Compose; refusing to start services."
  exit 1
fi

# Converge restored inflight uploads and non-terminal transcription jobs BEFORE verification
# and BEFORE starting services (`compose up -d`). Failure aborts restore immediately.
info "Running post-restore inflight state convergence (reconcile-restored-inflight-states)..."
RECONCILE_RUN_ARGS=(run --rm -T --no-deps)
RECONCILE_CLI_ARGS=(reconcile-restored-inflight-states)
if [[ "$RESTORE_HAS_TRANSCRIPTION" -eq 1 ]]; then
  RECONCILE_RUN_ARGS+=(-v "${RESTORE_ASR_VOLUME}:/var/lib/dalton/asr_jobs")
  RECONCILE_CLI_ARGS+=(--asr-jobs-dir /var/lib/dalton/asr_jobs)
fi
if [[ "$SKIP_MINIO" -eq 1 || "$STORAGE_ANY_CONFIGURED" -eq 0 ]]; then
  RECONCILE_CLI_ARGS+=(--skip-object-check)
fi
if ! compose_cmd "${RECONCILE_RUN_ARGS[@]}" backend \
    uv run --no-sync python scripts/standalone/consistent_backup.py "${RECONCILE_CLI_ARGS[@]}"; then
  err "Post-restore inflight state convergence failed; refusing to run compose up -d."
  exit 1
fi
ok "Post-restore inflight states converged."

info "Clearing restored personal-integration tokens and transient OAuth state..."
compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
  psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -v ON_ERROR_STOP=1 \
  < "$PERSONAL_INTEGRATIONS_RESET_SQL"
ok "Restored personal-integration tokens cleared (existing connections marked needs_reauth)."

# Verify every completed DB-referenced object exists in object storage; skip in-progress
# 'uploading' records (incomplete at backup time) and report how many were skipped.
if [[ "$SKIP_MINIO" -eq 0 && "$STORAGE_ANY_CONFIGURED" -eq 1 ]]; then
  DB_OBJ_REFS=""
  SKIPPED_UPLOADING_COUNT=0
  has_chat_uploads="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
    psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -v ON_ERROR_STOP=1 -Atqc \
    "SELECT to_regclass('public.dalton_chat_file_uploads');" | tr -d '[:space:]')"
  if [[ -n "$has_chat_uploads" ]]; then
    SKIPPED_UPLOADING_COUNT="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
      psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -v ON_ERROR_STOP=1 -Atqc "
      SELECT COUNT(*)
      FROM dalton_chat_file_uploads
      WHERE cleaned_at IS NULL
        AND state = 'uploading';
    " | tr -d '[:space:]')"
    if ! [[ "$SKIPPED_UPLOADING_COUNT" =~ ^[0-9]+$ ]]; then
      err "Could not parse skipped 'uploading' record count (got '${SKIPPED_UPLOADING_COUNT}')."
      exit 1
    fi
    if (( SKIPPED_UPLOADING_COUNT > 0 )); then
      info "Skipping ${SKIPPED_UPLOADING_COUNT} in-progress 'uploading' chat upload record(s) during object reference verification (incomplete at backup time)."
    fi
    DB_OBJ_REFS="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres \
      psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -v ON_ERROR_STOP=1 -Atqc "
      SELECT 'chat_upload' || E'\t' || upload_id || E'\t' || object_key
      FROM dalton_chat_file_uploads
      WHERE cleaned_at IS NULL
        AND state IN ('ready', 'completing')
        AND object_key IS NOT NULL AND object_key <> '';
    ")"
  fi
  if [[ -n "$DB_OBJ_REFS" ]]; then
    MISSING_CHECK_PY='
from __future__ import annotations
import os, sys
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default

skipped_uploading = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 0

dedicated_ok = all(
    (os.getenv(k) or "").strip()
    for k in (
        "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
        "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
    )
)
if dedicated_ok:
    endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL").rstrip("/")
    bucket = _env("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME")
    prefix = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX") or "").strip().strip("/")
    access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID")
    secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY")
    region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "DALTON_ASR_STORAGE_REGION", default="us-east-1")
    addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "DALTON_ASR_STORAGE_ADDRESSING_STYLE", default="path").lower()
else:
    endpoint = _env("DALTON_ASR_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
    bucket = _env("DALTON_ASR_STORAGE_BUCKET_NAME", "STORAGE_BUCKET_NAME", default="chat-uploads")
    prefix = (os.getenv("DALTON_ASR_STORAGE_KEY_PREFIX") or "").strip().strip("/")
    access_key = _env("DALTON_ASR_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID")
    secret_key = _env("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY")
    region = _env("DALTON_ASR_STORAGE_REGION", "STORAGE_REGION", default="us-east-1")
    addressing_style = _env("DALTON_ASR_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()

client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name=region,
    config=Config(
        signature_version="s3v4",
        s3={"addressing_style": addressing_style},
        connect_timeout=5,
        read_timeout=15,
    ),
)

missing: list[str] = []
checked = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    parts = line.split("\t", 2)
    if len(parts) != 3:
        continue
    kind, ref_id, obj_key = parts
    full_key = obj_key.lstrip("/")
    if prefix and not full_key.startswith(f"{prefix}/"):
        full_key = f"{prefix}/{full_key}"
    checked += 1
    try:
        client.head_object(Bucket=bucket, Key=full_key)
    except ClientError:
        missing.append(f"{kind}:{ref_id} -> {bucket}/{full_key}")

if missing:
    print(f"[restore] ERROR: {len(missing)}/{checked} DB-referenced object(s) missing from object storage:", file=sys.stderr)
    for item in missing:
        print(f"[restore] ERROR:   missing: {item}", file=sys.stderr)
    print(
        "[restore] ERROR: Note on online backup consistency window: backup.sh snapshots PostgreSQL first and streams object storage immediately afterward; objects deleted or still in flight during that online backup window (or an inconsistent bucket/key prefix) can leave DB references missing from object storage.",
        file=sys.stderr,
    )
    sys.exit(1)
else:
    print(
        f"[restore] OK: All {checked} completed DB-referenced object(s) present in MinIO bucket {bucket!r} "
        f"(skipped {skipped_uploading} in-progress \x27uploading\x27 record(s))."
    )
'
    printf '%s\n' "$DB_OBJ_REFS" | compose_cmd run --rm -T --no-deps backend \
      uv run --no-sync python -c "$MISSING_CHECK_PY" "$SKIPPED_UPLOADING_COUNT"
  else
    ok "No active completed DB object references to verify (skipped ${SKIPPED_UPLOADING_COUNT} in-progress 'uploading' record(s))."
  fi
fi

# Step 6: Rebuild edge if .env came from backup, and restore Caddy /data
if [[ "$ENV_FROM_BACKUP" -eq 1 && "$SKIP_EDGE_BUILD" -eq 0 ]]; then
  info "6/7 Rebuilding edge image to match restored .env..."
  compose_cmd build edge
  ok "Rebuilt edge image."
fi

if [[ "$SKIP_CADDY" -eq 0 && -f "${BACKUP_DIR}/caddy-data.tar.gz" ]]; then
  info "6/7 Restoring Caddy certificate & PKI data..."
  if ! compose_cmd run --rm -T --no-deps --entrypoint /bin/sh edge \
      -c 'rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null || true; tar -xzf - -C /data' \
      < "${BACKUP_DIR}/caddy-data.tar.gz"; then
    err "Failed to extract Caddy certificate data from ${BACKUP_DIR}/caddy-data.tar.gz."
    exit 1
  fi
  ok "Caddy certificate data restored."
else
  info "6/7 Skipping Caddy certificate restore."
fi

if [[ "$SKIP_WIKI" -eq 0 && -f "${BACKUP_DIR}/wiki-data.tar.gz" ]]; then
  if compose_cmd config --services 2>/dev/null | grep -qx "research-host"; then
    info "6/7 Restoring Wiki data volume (/var/lib/dalton/wiki)..."
    if ! compose_cmd run --rm -T --no-deps --entrypoint /bin/sh research-host \
        -c 'mkdir -p /var/lib/dalton/wiki && rm -rf /var/lib/dalton/wiki/* /var/lib/dalton/wiki/.[!.]* /var/lib/dalton/wiki/..?* 2>/dev/null || true; tar -xzf - -C /var/lib/dalton/wiki' \
        < "${BACKUP_DIR}/wiki-data.tar.gz"; then
      err "Failed to extract Wiki data volume from ${BACKUP_DIR}/wiki-data.tar.gz."
      exit 1
    fi
    WIKI_VERIFY_PY='
import json, os, sqlite3, sys
from pathlib import Path

root = Path(os.getenv("DALTON_WIKI_DATA_DIR") or "/var/lib/dalton/wiki")
db = root / "wiki" / "vectors.db"
audit_path = root / "state" / "wiki_ingest_audit.jsonl"
img_db = root / "state" / "research-images" / "index.sqlite"

errors: list[str] = []
doc_ids: set[int] = set()
doc_filepaths: set[str] = set()
has_rcpts = None
receipt_rows: list[tuple] = []

if db.is_file():
    conn = sqlite3.connect(str(db))
    try:
        res = conn.execute("PRAGMA integrity_check").fetchone()
        if not res or str(res[0]).lower() != "ok":
            errors.append(f"Wiki vectors.db integrity_check failed: {res}")
        else:
            docs = conn.execute("SELECT id, filepath FROM documents").fetchall()
            for doc_num, fp in docs:
                doc_ids.add(int(doc_num))
                doc_filepaths.add(str(fp))
                if not (root / str(fp)).is_file():
                    errors.append(f"documents row id={doc_num} references missing markdown file: {fp}")
            has_rcpts = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type=\"table\" AND name=\"ingest_receipts\""
            ).fetchone()
            if has_rcpts:
                receipt_rows = conn.execute(
                    "SELECT request_id, doc_id, filepath, sha256, receipt_json FROM ingest_receipts"
                ).fetchall()
    finally:
        conn.close()

# Parse audit JSONL entries (if present)
audit_entries: list[dict] = []
successful_audits: list[dict] = []
if audit_path.is_file():
    for lineno, raw in enumerate(audit_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception as exc:
            errors.append(f"Malformed JSON in wiki_ingest_audit.jsonl line {lineno}: {exc}")
            continue
        if not isinstance(obj, dict):
            continue
        audit_entries.append(obj)
        if obj.get("status") in ("ingested", "done"):
            successful_audits.append(obj)

# Require vectors.db whenever successful audit records or Wiki Markdown files exist
wiki_md_files = (
    [p for p in (root / "wiki").rglob("*.md") if p.is_file()]
    if (root / "wiki").is_dir()
    else []
)
if (successful_audits or wiki_md_files) and not db.is_file():
    errors.append(
        f"Wiki index wiki/vectors.db is missing despite {len(successful_audits)} successful audit record(s) "
        f"and {len(wiki_md_files)} markdown file(s)."
    )

if any(e.get("image_id") for e in successful_audits) and not img_db.is_file():
    errors.append(
        "Audit records reference image_id, but state/research-images/index.sqlite is missing."
    )

# Verify image index SQLite, asset files, and refs -> assets & documents consistency
img_ref_ids: set[str] = set()
if img_db.is_file():
    conn = sqlite3.connect(str(img_db))
    try:
        res = conn.execute("PRAGMA integrity_check").fetchone()
        if not res or str(res[0]).lower() != "ok":
            errors.append(f"Wiki image index integrity_check failed: {res}")
        else:
            assets = conn.execute("SELECT sha, path FROM assets").fetchall()
            asset_shas: set[str] = set()
            for sha, rel_path in assets:
                asset_shas.add(str(sha))
                if not rel_path or not (root / str(rel_path)).is_file():
                    errors.append(f"Image asset sha={sha} missing file on disk: {rel_path}")
            refs = conn.execute("SELECT id, asset_sha, source, doc_key FROM refs").fetchall()
            for ref_id, asset_sha, source, doc_key in refs:
                img_ref_ids.add(f"image:{ref_id}")
                if str(asset_sha) not in asset_shas:
                    errors.append(f"Image ref id={ref_id} points to missing asset_sha={asset_sha}")
                if str(source) == "company-wiki" and str(doc_key).startswith("wiki:") and db.is_file():
                    try:
                        ref_doc_num = int(str(doc_key).split(":", 1)[1])
                    except ValueError:
                        ref_doc_num = -1
                    if ref_doc_num not in doc_ids:
                        errors.append(f"Image ref id={ref_id} references missing wiki document {doc_key}")
    finally:
        conn.close()

# Cross-check DB ingest_receipts <-> documents <-> originals <-> image index <-> audit JSONL (per-record!)
receipt_keys: list[tuple[str, str, str]] = []
for req_id, doc_id_str, fp, sha256, rcpt_json in receipt_rows:
    req_str = str(req_id or "")
    doc_str = str(doc_id_str or "")
    fp_str = str(fp or "")
    sha_str = str(sha256 or "")
    receipt_keys.append((req_str, doc_str, sha_str))

    if fp_str not in doc_filepaths or not (root / fp_str).is_file():
        errors.append(f"ingest_receipts request_id={req_str} references missing document filepath={fp_str}")
    if doc_str.startswith("wiki:"):
        try:
            dnum = int(doc_str.split(":", 1)[1])
        except ValueError:
            dnum = -1
        if dnum not in doc_ids:
            errors.append(f"ingest_receipts request_id={req_str} references missing doc_id={doc_str}")
    rcpt = {}
    if rcpt_json:
        try:
            rcpt = json.loads(rcpt_json)
        except Exception as exc:
            errors.append(f"ingest_receipts request_id={req_str} has corrupt receipt_json: {exc}")
    orig_fp = rcpt.get("original_filepath")
    if orig_fp and not (root / str(orig_fp)).is_file():
        errors.append(f"ingest_receipts request_id={req_str} references missing original file: {orig_fp}")
    img_id = rcpt.get("image_id")
    if img_id and str(img_id) not in img_ref_ids:
        errors.append(f"ingest_receipts request_id={req_str} references missing image index entry: {img_id}")

    # Every DB receipt must match a SINGLE audit record on doc_id, sha256, and (when explicit) request_id
    explicit_req = req_str if (req_str and not req_str.startswith("sha:")) else None
    matched_audit = False
    for entry in successful_audits:
        if str(entry.get("doc_id") or "") != doc_str:
            continue
        if str(entry.get("sha256") or "") != sha_str:
            continue
        if explicit_req is not None and str(entry.get("request_id") or "") != explicit_req:
            continue
        e_fp = entry.get("filepath")
        if e_fp and str(e_fp) != fp_str:
            continue
        matched_audit = True
        break
    if not matched_audit:
        errors.append(
            f"ingest_receipts mismatch: request_id={req_str}, doc_id={doc_str}, sha256={sha_str[:12]}... "
            f"has no matching entry (same request_id, doc_id, and sha256) in state/wiki_ingest_audit.jsonl"
        )

# Cross-check successful audit JSONL entries -> documents, originals, image index, and ingest_receipts
if db.is_file():
    if successful_audits and not has_rcpts:
        errors.append(
            f"state/wiki_ingest_audit.jsonl has {len(successful_audits)} successful record(s), "
            f"but ingest_receipts table is missing in wiki/vectors.db"
        )
    elif successful_audits and not receipt_keys:
        errors.append(
            f"state/wiki_ingest_audit.jsonl has {len(successful_audits)} successful record(s), "
            f"but ingest_receipts table is empty in wiki/vectors.db"
        )
    for entry in successful_audits:
        e_req = str(entry.get("request_id") or "")
        e_doc = str(entry.get("doc_id") or "")
        e_sha = str(entry.get("sha256") or "")
        e_fp = entry.get("filepath")
        if e_fp and (str(e_fp) not in doc_filepaths or not (root / str(e_fp)).is_file()):
            errors.append(f"wiki_ingest_audit.jsonl entry (doc_id={e_doc}) references missing document filepath={e_fp}")
        if e_doc.startswith("wiki:"):
            try:
                e_dnum = int(e_doc.split(":", 1)[1])
            except ValueError:
                e_dnum = -1
            if e_dnum not in doc_ids:
                errors.append(f"wiki_ingest_audit.jsonl entry references missing doc_id={e_doc}")
        e_orig = entry.get("original_filepath")
        if e_orig and not (root / str(e_orig)).is_file():
            errors.append(f"wiki_ingest_audit.jsonl entry (doc_id={e_doc}) references missing original file={e_orig}")
        e_img = entry.get("image_id")
        if e_img and str(e_img) not in img_ref_ids:
            errors.append(f"wiki_ingest_audit.jsonl entry (doc_id={e_doc}) references missing image_id={e_img}")
        if has_rcpts and (e_doc or e_sha or e_req):
            matched_rcpt = False
            for r_req, r_doc, r_sha in receipt_keys:
                if e_doc and r_doc != e_doc:
                    continue
                if e_sha and r_sha != e_sha:
                    continue
                if e_req and r_req != e_req:
                    continue
                matched_rcpt = True
                break
            if not matched_rcpt:
                req_label = e_req if e_req else "(none)"
                errors.append(
                    f"wiki_ingest_audit.jsonl mismatch: request_id={req_label}, doc_id={e_doc}, "
                    f"sha256={e_sha[:12]}... has no matching row in ingest_receipts"
                )

if errors:
    for msg in errors[:10]:
        print(f"ERROR: {msg}", file=sys.stderr)
    if len(errors) > 10:
        print(f"ERROR: ... and {len(errors) - 10} more Wiki consistency error(s).", file=sys.stderr)
    sys.exit(1)
'
    if ! compose_cmd run --rm -T --no-deps --entrypoint /bin/sh research-host \
        -c 'uv run --no-sync python -c "$1"' _ "$WIKI_VERIFY_PY"; then
      err "Restored Wiki data volume failed post-restore integrity verification."
      exit 1
    fi
    ok "Wiki data volume restored and verified."
  else
    warn "Backup includes wiki-data.tar.gz, but research-host service is not defined in Compose; skipping Wiki restore."
  fi
else
  info "6/7 Skipping Wiki data volume restore."
fi

# Backfill missing research-host tokens in restored/existing .env before starting the stack
# (pre-Batch-3a backups lack DALTON_RESEARCH_HOST_TOKEN / DALTON_RESEARCH_ADMIN_TOKEN).
if compose_cmd config --services 2>/dev/null | grep -qx "research-host"; then
  restored_rh_tokens="$(ensure_research_host_tokens "$ENV_FILE")"
  if [[ -n "$restored_rh_tokens" ]]; then
    ok "Generated missing research-host token(s) in ${ENV_FILE}: ${restored_rh_tokens} (existing values preserved)."
  fi
fi

# Step 7: Start full stack and run unified health & schema checks
info "7/7 Starting services and running health checks..."
compose_cmd up -d

info "Waiting for backend health check..."
backend_healthy=0
for _ in $(seq 1 60); do
  if compose_cmd exec -T backend curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    backend_healthy=1
    break
  fi
  sleep 2
done
if [[ "$backend_healthy" -ne 1 ]]; then
  err "Backend did not pass /health within 120s."
  err "Inspect logs with: $(fmt_cmd docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" logs --tail=100 backend)"
  exit 1
fi
ok "Backend /health passed."

if ! compose_cmd ps --status running --services | grep -qx "edge"; then
  err "Edge container is not running after restore."
  exit 1
fi

# Determine actual published host port from the running edge container, falling back to .env.
resolve_edge_port() {
  local container_port="$1" fallback_port="$2"
  local mapping host_port
  mapping="$(compose_cmd port edge "$container_port" 2>/dev/null | head -n 1 | tr -d '\r\n' || true)"
  host_port="${mapping##*:}"
  if [[ "$host_port" =~ ^[0-9]+$ ]]; then
    printf '%s' "$host_port"
  else
    printf '%s' "$fallback_port"
  fi
}

PROBE_EDGE_PORT="$(resolve_edge_port 8088 "$CFG_EDGE_PORT")"
PROBE_HTTPS_PORT="$(resolve_edge_port 443 "$CFG_EDGE_HTTPS_PORT")"

resolve_probe_bind_addr() {
  local raw="$1"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  raw="${raw%"${raw##*[![:space:]]}"}"
  local inner="${raw#[}"
  inner="${inner%]}"
  if [[ -z "$inner" || "$inner" == "0.0.0.0" ]]; then
    printf '127.0.0.1'
  elif [[ "$inner" == "::" ]]; then
    printf '[::1]'
  elif [[ "$inner" == *:* ]]; then
    printf '[%s]' "$inner"
  else
    printf '%s' "$inner"
  fi
}

probe_domain="${CFG_DOMAIN#[}"
probe_domain="${probe_domain%]}"
probe_url_host="$probe_domain"
probe_curl_extra=()
if [[ "$probe_domain" == *:* ]]; then
  probe_url_host="[${probe_domain}]"
  probe_curl_extra=(-g)
else
  probe_resolve_addr="$(resolve_probe_bind_addr "$CFG_EDGE_TLS_BIND")"
  probe_curl_extra=(--resolve "${probe_url_host}:${PROBE_HTTPS_PORT}:${probe_resolve_addr}")
fi

edge_ok=0
if [[ "$CFG_TLS_MODE" == "loopback" ]]; then
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PROBE_EDGE_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
      edge_ok=1
      break
    fi
    sleep 2
  done
elif [[ "$CFG_TLS_MODE" == "internal" ]]; then
  tmp_ca="$(mktemp)"
  for _ in $(seq 1 30); do
    if compose_cmd exec -T edge cat /data/caddy/pki/authorities/local/root.crt > "$tmp_ca" 2>/dev/null && [[ -s "$tmp_ca" ]]; then
      if curl -sf "${probe_curl_extra[@]}" --cacert "$tmp_ca" \
          "https://${probe_url_host}:${PROBE_HTTPS_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
        edge_ok=1
        break
      fi
    fi
    sleep 2
  done
  rm -f "$tmp_ca"
else
  curl_ca_args=()
  if [[ -n "$CA_FILE" ]]; then
    curl_ca_args=(--cacert "$CA_FILE")
  fi
  for _ in $(seq 1 30); do
    if curl -sf "${probe_curl_extra[@]}" ${curl_ca_args[@]+"${curl_ca_args[@]}"} \
        "https://${probe_url_host}:${PROBE_HTTPS_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
      edge_ok=1
      break
    fi
    sleep 2
  done
fi

if [[ "$edge_ok" -ne 1 ]]; then
  err "Edge gateway failed post-restore health probe (mode=${CFG_TLS_MODE})."
  if [[ "$CFG_TLS_MODE" == "custom" && -z "$CA_FILE" ]]; then
    err "For certificates signed by an enterprise/private CA, pass --ca-file <ca.pem>."
  elif [[ "$CFG_TLS_MODE" == "auto" ]]; then
    err "For public ACME mode, verify DNS resolution and inbound TCP ports 80/443."
  fi
  exit 1
fi
ok "Edge gateway probe passed (mode=${CFG_TLS_MODE})."

RESTORED_UP="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -Atqc "SELECT version_num FROM alembic_version ORDER BY version_num;" | tr -d '\r\n')"
RESTORED_DA="$(compose_cmd exec -T -e PGPASSWORD="$CFG_DB_PASSWORD" postgres psql -h 127.0.0.1 -p 5432 -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -Atqc "SELECT version_num FROM alembic_version_dalton ORDER BY version_num;" | tr -d '\r\n')"
ok "Migrations verified at head (upstream=${RESTORED_UP}, dalton=${RESTORED_DA})."

RESTORE_SUCCEEDED=1

printf "\n== Restore Complete ==\n"
printf "  Restored from    : %s\n" "$BACKUP_DIR"
printf "  Database         : %s (upstream=%s, dalton=%s)\n" "$CFG_DB_NAME" "$RESTORED_UP" "$RESTORED_DA"
printf "  Active bucket    : %s\n" "$CFG_CHAT_BUCKET"
printf "  Edge TLS mode    : %s\n" "$CFG_TLS_MODE"
if [[ -n "$ENV_BACKUP_PATH" && ( -f "$ENV_BACKUP_PATH" || -L "$ENV_BACKUP_PATH" ) ]]; then
  printf "  Previous .env    : %s\n" "$ENV_BACKUP_PATH"
fi
printf "\n  Next steps:\n"
printf "  - Verify service status: %s\n" "$(fmt_cmd docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" ps)"
printf "  - Open the web UI and confirm your workspaces, threads, and uploaded files are visible.\n"
if [[ -n "$ENV_BACKUP_PATH" && ( -f "$ENV_BACKUP_PATH" || -L "$ENV_BACKUP_PATH" ) ]]; then
  if [[ "$ENV_WAS_SYMLINK" -eq 1 ]]; then
    printf "  - 原 .env 符号链接已备份保留在 %s（原目标文件未修改），确认恢复无误后请自行删除。\n" "$ENV_BACKUP_PATH"
  else
    printf "  - 原 .env 已备份保留在 %s（权限 600），确认恢复无误后请自行删除。\n" "$ENV_BACKUP_PATH"
  fi
fi
