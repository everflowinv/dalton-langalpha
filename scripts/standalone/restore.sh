#!/usr/bin/env bash
# Standalone Core formal restore utility (v1: empty target instance only).
#
# Restores a timestamped backup directory created by `scripts/standalone/backup.sh`
# into an EMPTY target instance:
#   1. Verifies backup integrity (`SHA256SUMS`, `manifest.json`, and MinIO archive manifest).
#   2. Prepares the candidate `.env` (strictly file-only; never reads shell env),
#      enforces exact verbatim `COMPOSE_PROJECT_NAME` match for `--replace-env`,
#      acquires `/tmp/dalton-<project>.restore.lock` (`flock`, or `mkdir` fallback
#      without PID-based auto-cleanup), cleans leftover `.env.tmp-*` files, and
#      runs static preflight checks (external DB guard, identifiers, Compose files).
#   3. Verifies the entire PostgreSQL instance is empty (all non-template DBs
#      in `pg_database` including `postgres` checked over TCP with superuser
#      credentials for non-system `pg_class`, `pg_proc`, and `pg_type` objects;
#      object storage has 0 buckets or only the empty `minio_init` bucket),
#      verifies backup bucket/key-prefix parity against `.env`, and validates
#      `pg_restore --list`. Refuses non-empty targets and points to the manual
#      reset-and-restore procedure in `docs/dalton/dev-guide/STANDALONE-DEPLOY.md` (§6.2).
#      Only after validation succeeds, atomically writes `.env` via `.env.tmp-*` + `mv`.
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
                              empty target instance (validated before atomic rename).
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
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
WORK_DIR="$(cd "$WORK_DIR" && pwd)"

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
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif val and val[0] not in ("'", '"'):
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
ORIG_ENV_MODE="600"
if [[ -f "$ENV_FILE" ]]; then
  ENV_EXISTED_BEFORE=1
  ORIG_ENV_MODE="$(get_file_mode "$ENV_FILE")"
fi
STAGED_ENV_TMP=""
ENV_WRITE_TMP=""
ENV_FROM_BACKUP=0
CANDIDATE_ENV=""
ACTIVE_COMPOSE_ENV=""
RESTORE_COMPOSE_OVERRIDE=""
MINIO_MANIFEST_TMP=""
COMPOSE_CFG_TMP=""

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
  if [[ -n "$ENV_WRITE_TMP" && -f "$ENV_WRITE_TMP" ]]; then
    rm -f "$ENV_WRITE_TMP"
  fi
  if [[ -n "$STAGED_ENV_TMP" && -f "$STAGED_ENV_TMP" ]]; then
    rm -f "$STAGED_ENV_TMP"
  fi
  if [[ -n "$RESTORE_COMPOSE_OVERRIDE" && -f "$RESTORE_COMPOSE_OVERRIDE" ]]; then
    rm -f "$RESTORE_COMPOSE_OVERRIDE"
  fi
  if [[ -n "$MINIO_MANIFEST_TMP" && -f "$MINIO_MANIFEST_TMP" ]]; then
    rm -f "$MINIO_MANIFEST_TMP"
  fi
  if [[ -n "$COMPOSE_CFG_TMP" ]]; then
    rm -f "$COMPOSE_CFG_TMP" "${COMPOSE_CFG_TMP}.err"
  fi
  release_restore_lock
  if [[ "$rc" -ne 0 && "$RESTORE_SUCCEEDED" -ne 1 && "$MUTATION_STARTED" -eq 1 ]]; then
    local proj="${COMPOSE_PROJECT:-<project>}"
    local down_cmd="docker compose -p ${proj} -f docker-compose.yml -f docker-compose.core.yml --env-file ${ENV_FILE} down -v"
    if [[ -n "$COMPOSE_PROJECT" && "${#COMPOSE_FILE_ARGS[@]}" -gt 0 ]]; then
      down_cmd="$(fmt_cmd docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" down -v)"
    fi
    err "Restore failed after writing .env or modifying target state; the instance is now in a partial/half-restored state (半成品)."
    err "Your backup directory (${BACKUP_DIR}) was NOT modified."
    err "Follow ${MANUAL_OVERWRITE_REF} to confirm volumes and reset before re-running restore.sh:"
    err "  $(fmt_cmd docker volume ls --filter "label=com.docker.compose.project=${proj}")"
    err "  ${down_cmd}"
  fi
}
trap cleanup EXIT

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
  MINIO_MANIFEST_TMP="$(mktemp)"
  chmod 600 "$MINIO_MANIFEST_TMP"
  if ! tar -xOzf "${BACKUP_DIR}/minio-objects.tar.gz" _minio_manifest.json > "$MINIO_MANIFEST_TMP"; then
    err "Failed to extract _minio_manifest.json from ${BACKUP_DIR}/minio-objects.tar.gz."
    exit 1
  fi
fi

# Step 2: Prepare candidate .env (file-only), check project name verbatim, acquire lock,
# clean leftover .env.tmp-*, and run static preflight checks.
if [[ -f "${BACKUP_DIR}/env.enc" && -n "$ENV_PASSPHRASE_FILE" ]]; then
  if [[ ! -r "$ENV_PASSPHRASE_FILE" || ! -s "$ENV_PASSPHRASE_FILE" ]]; then
    err "Passphrase file is missing, unreadable, or empty: ${ENV_PASSPHRASE_FILE}"
    exit 1
  fi
  if [[ "$ENV_EXISTED_BEFORE" -eq 0 || "$REPLACE_ENV" -eq 1 ]]; then
    info "2/7 Decrypting env.enc for preflight validation (${ENV_CIPHER}/${ENV_KDF}/${ENV_KDF_DIGEST}/${ENV_KDF_ITER})..."
    STAGED_ENV_TMP="$(mktemp "${WORK_DIR}/.env.tmp-decrypt-XXXXXX")"
    chmod 600 "$STAGED_ENV_TMP"
    if ! openssl enc -d "-${ENV_CIPHER}" -pbkdf2 -md "${ENV_KDF_DIGEST}" -iter "${ENV_KDF_ITER}" \
        -in "${BACKUP_DIR}/env.enc" \
        -out "$STAGED_ENV_TMP" \
        -pass "file:${ENV_PASSPHRASE_FILE}"; then
      err "Failed to decrypt ${BACKUP_DIR}/env.enc (check passphrase file)."
      exit 1
    fi
    chmod 600 "$STAGED_ENV_TMP"
    ENV_FROM_BACKUP=1
    CANDIDATE_ENV="$STAGED_ENV_TMP"
    ok "Decrypted env.enc into temporary file (will write ${ENV_FILE} atomically only after all preflight checks pass)."
  else
    info "2/7 Using existing ${ENV_FILE} (pass --replace-env to replace it from env.enc on an empty target)."
    CANDIDATE_ENV="$ENV_FILE"
  fi
elif [[ "$ENV_EXISTED_BEFORE" -eq 1 ]]; then
  if [[ "$REPLACE_ENV" -eq 1 ]]; then
    err "--replace-env requires ${BACKUP_DIR}/env.enc and --env-passphrase-file (or DALTON_BACKUP_PASSPHRASE_FILE)."
    exit 1
  fi
  info "2/7 Using existing ${ENV_FILE}."
  CANDIDATE_ENV="$ENV_FILE"
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

# Acquire fixed-path project restore lock at /tmp/dalton-<project>.restore.lock
if [[ "$PROJECT_NAME_EXPLICIT" -eq 1 ]]; then
  LOCK_PROJECT="$PROJECT_NAME_ARG"
else
  RAW_ENV_PROJECT="$(read_env_file_var "$CANDIDATE_ENV" "COMPOSE_PROJECT_NAME" "")"
  if [[ -n "$RAW_ENV_PROJECT" ]]; then
    LOCK_PROJECT="$(normalize_env_project_name "$RAW_ENV_PROJECT")"
    validate_project_name "$LOCK_PROJECT" "COMPOSE_PROJECT_NAME"
  else
    LOCK_PROJECT="$(sanitize_project_name "$(basename "$WORK_DIR")")"
    validate_project_name "$LOCK_PROJECT" "COMPOSE_PROJECT_NAME"
  fi
fi
COMPOSE_PROJECT="$LOCK_PROJECT"
RESTORE_LOCK_PATH="/tmp/dalton-${LOCK_PROJECT}.restore.lock"
acquire_restore_lock

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

# External PostgreSQL guard using strictly the candidate .env file
CFG_DB_HOST="$(read_env_file_var "$CANDIDATE_ENV" "DB_HOST" "postgres")"
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
CFG_DB_USER="$(read_env_file_var "$CANDIDATE_ENV" "DB_USER" "postgres")"
CFG_DB_PASSWORD="$(read_env_file_var "$CANDIDATE_ENV" "DB_PASSWORD" "postgres")"
CFG_DB_NAME="$(read_env_file_var "$CANDIDATE_ENV" "DB_NAME" "postgres")"
CFG_DOMAIN="$(read_env_file_var "$CANDIDATE_ENV" "DOMAIN" "localhost")"
CFG_TLS_MODE="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_TLS_MODE" "loopback")"
CFG_EDGE_PORT="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_EDGE_PORT" "8088")"
CFG_EDGE_HTTPS_PORT="$(read_env_file_var "$CANDIDATE_ENV" "DALTON_EDGE_HTTPS_PORT" "18443")"
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

# Generate an env_file !override file that replaces ONLY entries pointing to
# `${WORK_DIR}/.env` with the target env path, while preserving every other
# `env_file` entry (e.g. `storage.env` from an additional compose file) in its
# exact original order and form. During preflight the `.env` entry points to
# $CANDIDATE_ENV so ${WORK_DIR}/.env stays untouched until all checks pass; after
# writing ${ENV_FILE} atomically, the `.env` entry is updated in-place to
# ${ENV_FILE}. Consequently preflight, formal writes, startup, and subsequent
# regular `docker compose up` runs after cleanup removes the override file all
# see the exact same effective `env_file` list.
write_compose_env_override() {
  local target_env_path="$1"
  local out_path="$2"
  python3 - "$target_env_path" "$WORK_DIR" "${RESOLVED_COMPOSE_FILES[@]}" > "$out_path" <<'PY'
import json, os, re, sys

env_path = sys.argv[1]
work_dir = sys.argv[2]
compose_files = sys.argv[3:]

work_norm = os.path.normpath(os.path.abspath(work_dir))
dotenv_norm = os.path.normpath(os.path.join(work_norm, ".env"))
dotenv_real = os.path.realpath(dotenv_norm)


def is_dotenv_path(raw_path: str) -> bool:
    p = raw_path.strip()
    if not p or "$" in p:
        return False
    if os.path.isabs(p):
        cand_norm = os.path.normpath(p)
        return cand_norm == dotenv_norm or os.path.realpath(cand_norm) == dotenv_real
    cand_norm = os.path.normpath(os.path.join(work_norm, p))
    return cand_norm == dotenv_norm or os.path.realpath(cand_norm) == dotenv_real


def strip_comment(text: str) -> str:
    in_quote: str | None = None
    escaped = False
    for idx, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if in_quote == '"' and ch == "\\":
            escaped = True
            continue
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
        elif ch == "#" and (idx == 0 or text[idx - 1].isspace()):
            return text[:idx].rstrip()
    return text.rstrip()


def parse_scalar(text: str):
    val = strip_comment(text).strip()
    if not val:
        return ""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        if val[0] == '"':
            try:
                return json.loads(val)
            except Exception:
                return val[1:-1]
        return val[1:-1].replace("''", "'")
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    return val


def split_flow_items(inner: str) -> list[str]:
    items: list[str] = []
    buf: list[str] = []
    in_quote: str | None = None
    depth = 0
    escaped = False
    for ch in inner:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_quote == '"' and ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if in_quote is not None:
            buf.append(ch)
            if ch == in_quote:
                in_quote = None
            continue
        if ch in ("'", '"'):
            in_quote = ch
            buf.append(ch)
        elif ch in ("{", "["):
            depth += 1
            buf.append(ch)
        elif ch in ("}", "]"):
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            part = "".join(buf).strip()
            if part:
                items.append(part)
            buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        items.append(tail)
    return items


def parse_flow_mapping(text: str) -> dict:
    inner = text.strip()
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1]
    out: dict = {}
    for pair in split_flow_items(inner):
        if ":" not in pair:
            continue
        k, v = pair.split(":", 1)
        out[str(parse_scalar(k))] = parse_scalar(v)
    return out


def parse_env_file_fallback(cf: str) -> dict[str, tuple[str | None, list]]:
    result: dict[str, tuple[str | None, list]] = {}
    in_services = False
    svc_indent: int | None = None
    prop_indent: int | None = None
    cur_svc: str | None = None
    in_env_file = False
    ef_indent = 0
    ef_tag: str | None = None
    ef_entries: list = []
    cur_dict: dict | None = None
    cur_item_indent: int | None = None

    def flush_env_file() -> None:
        nonlocal in_env_file, ef_tag, ef_entries, cur_dict, cur_item_indent
        if in_env_file and cur_svc is not None:
            result[cur_svc] = (ef_tag, ef_entries)
        in_env_file = False
        ef_tag = None
        ef_entries = []
        cur_dict = None
        cur_item_indent = None

    with open(cf, encoding="utf-8") as fh:
        for raw in fh:
            cleaned = strip_comment(raw.rstrip("\r\n"))
            stripped = cleaned.strip()
            if not stripped:
                continue
            indent = len(cleaned) - len(cleaned.lstrip(" "))
            if indent == 0:
                flush_env_file()
                in_services = stripped == "services:" or stripped.startswith("services:")
                svc_indent = None
                prop_indent = None
                cur_svc = None
                continue
            if not in_services:
                continue
            if svc_indent is None or indent <= svc_indent:
                flush_env_file()
                if stripped.endswith(":"):
                    svc_indent = indent
                    prop_indent = None
                    cur_svc = str(parse_scalar(stripped[:-1]))
                else:
                    cur_svc = None
                continue
            if cur_svc is None:
                continue
            if in_env_file and (
                indent < ef_indent
                or (
                    indent == ef_indent
                    and not (stripped.startswith("-") and (len(stripped) == 1 or stripped[1].isspace()))
                )
            ):
                flush_env_file()
            if not in_env_file:
                if prop_indent is None:
                    prop_indent = indent
                if indent != prop_indent:
                    continue
                if stripped.startswith("env_file:"):
                    rest = stripped[len("env_file:"):].strip()
                    tag: str | None = None
                    for candidate_tag in ("!override", "!reset"):
                        if rest == candidate_tag or rest.startswith(candidate_tag + " "):
                            tag = candidate_tag
                            rest = rest[len(candidate_tag):].strip()
                            break
                    if not rest:
                        in_env_file = True
                        ef_indent = indent
                        ef_tag = tag
                        ef_entries = []
                        cur_dict = None
                        cur_item_indent = None
                    elif rest.startswith("[") and rest.endswith("]"):
                        entries = []
                        for tok in split_flow_items(rest[1:-1]):
                            if tok.startswith("{") and tok.endswith("}"):
                                entries.append(parse_flow_mapping(tok))
                            else:
                                entries.append(str(parse_scalar(tok)))
                        result[cur_svc] = (tag, entries)
                    elif rest.startswith("{") and rest.endswith("}"):
                        result[cur_svc] = (tag, [parse_flow_mapping(rest)])
                    else:
                        val = parse_scalar(rest)
                        result[cur_svc] = (tag, [] if val in (None, "") else [str(val)])
                continue

            if stripped.startswith("-") and (len(stripped) == 1 or stripped[1].isspace()):
                item_text = stripped[1:].strip()
                cur_dict = None
                cur_item_indent = indent
                if not item_text:
                    cur_dict = {}
                    ef_entries.append(cur_dict)
                elif item_text.startswith("{") and item_text.endswith("}"):
                    ef_entries.append(parse_flow_mapping(item_text))
                elif re.match(r"^[A-Za-z_][A-Za-z0-9_-]*\s*:", item_text):
                    k, v = item_text.split(":", 1)
                    cur_dict = {k.strip(): parse_scalar(v)}
                    ef_entries.append(cur_dict)
                else:
                    ef_entries.append(str(parse_scalar(item_text)))
            elif cur_dict is not None and (cur_item_indent is None or indent > cur_item_indent):
                if ":" in stripped:
                    k, v = stripped.split(":", 1)
                    cur_dict[k.strip()] = parse_scalar(v)

    flush_env_file()
    return result


def parse_env_file_yaml(cf: str) -> dict[str, tuple[str | None, list]] | None:
    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        return None

    class _Tagged:
        def __init__(self, tag: str, value):
            self.tag = tag
            self.value = value

    class _Loader(yaml.SafeLoader):
        pass

    def _ctor(loader, node):
        if isinstance(node, yaml.ScalarNode):
            val = loader.construct_scalar(node)
        elif isinstance(node, yaml.SequenceNode):
            val = loader.construct_sequence(node, deep=True)
        elif isinstance(node, yaml.MappingNode):
            val = loader.construct_mapping(node, deep=True)
        else:
            val = None
        return _Tagged(node.tag, val)

    def _multi_ctor(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        return None

    _Loader.add_constructor("!override", _ctor)
    _Loader.add_constructor("!reset", _ctor)
    _Loader.add_multi_constructor("!", _multi_ctor)

    try:
        with open(cf, encoding="utf-8") as fh:
            data = yaml.load(fh, Loader=_Loader)
    except Exception:
        return None
    if not isinstance(data, dict):
        return {}
    services = data.get("services")
    if not isinstance(services, dict):
        return {}
    out: dict[str, tuple[str | None, list]] = {}
    for svc_name, svc_cfg in services.items():
        if not isinstance(svc_cfg, dict) or "env_file" not in svc_cfg:
            continue
        raw_ef = svc_cfg["env_file"]
        tag = None
        val = raw_ef
        if isinstance(raw_ef, _Tagged):
            tag = raw_ef.tag
            val = raw_ef.value
        if val is None:
            entries = []
        elif isinstance(val, str):
            entries = [val]
        elif isinstance(val, dict):
            entries = [dict(val)]
        elif isinstance(val, list):
            entries = [
                dict(x) if isinstance(x, dict) else str(x)
                for x in val
                if isinstance(x, (str, dict))
            ]
        else:
            entries = []
        out[str(svc_name)] = (tag, entries)
    return out


merged: dict[str, list] = {}
for cf in compose_files:
    parsed = parse_env_file_yaml(cf)
    if parsed is None:
        parsed = parse_env_file_fallback(cf)
    for svc_name, (tag, entries) in parsed.items():
        if tag == "!reset":
            merged[svc_name] = []
        elif tag == "!override":
            merged[svc_name] = list(entries)
        else:
            merged.setdefault(svc_name, []).extend(entries)

escaped_target = env_path.replace("$", "$$")
rewritten_svcs: dict[str, list] = {}
for svc, entries in merged.items():
    replaced_any = False
    new_entries: list = []
    for entry in entries:
        if isinstance(entry, str):
            if is_dotenv_path(entry):
                new_entries.append(escaped_target)
                replaced_any = True
            else:
                new_entries.append(entry)
        elif isinstance(entry, dict):
            item = dict(entry)
            p = item.get("path")
            if isinstance(p, str) and is_dotenv_path(p):
                item["path"] = escaped_target
                replaced_any = True
            new_entries.append(item)
    if replaced_any:
        rewritten_svcs[svc] = new_entries

if rewritten_svcs:
    print("services:")
    for svc in sorted(rewritten_svcs):
        print(f"  {svc}:")
        print("    env_file: !override")
        for item in rewritten_svcs[svc]:
            if isinstance(item, str):
                print(f"      - {json.dumps(item)}")
            elif isinstance(item, dict) and item:
                first = True
                for k, v in item.items():
                    prefix = "      - " if first else "        "
                    first = False
                    print(f"{prefix}{k}: {json.dumps(v)}")
PY
}

if [[ "$ENV_FROM_BACKUP" -eq 1 ]]; then
  RESTORE_COMPOSE_OVERRIDE="$(mktemp "${TMPDIR:-/tmp}/dalton-restore-override.XXXXXX")"
  chmod 600 "$RESTORE_COMPOSE_OVERRIDE"
  write_compose_env_override "$CANDIDATE_ENV" "$RESTORE_COMPOSE_OVERRIDE"
  if [[ ! -s "$RESTORE_COMPOSE_OVERRIDE" ]]; then
    rm -f "$RESTORE_COMPOSE_OVERRIDE"
    RESTORE_COMPOSE_OVERRIDE=""
  fi
fi

ACTIVE_COMPOSE_ENV="$CANDIDATE_ENV"
compose_cmd() {
  if [[ -n "$RESTORE_COMPOSE_OVERRIDE" && -f "$RESTORE_COMPOSE_OVERRIDE" ]]; then
    env "${ENV_UNSET_ARGS[@]}" docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" -f "$RESTORE_COMPOSE_OVERRIDE" --env-file "$ACTIVE_COMPOSE_ENV" --project-directory "$WORK_DIR" "$@"
  else
    env "${ENV_UNSET_ARGS[@]}" docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ACTIVE_COMPOSE_ENV" --project-directory "$WORK_DIR" "$@"
  fi
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

STORAGE_CHECK_ARGS=(
  python3 "${SCRIPT_DIR}/storage_config.py" check-env
  --compose-config-json "$COMPOSE_CFG_TMP"
  --service backend
  --env-file "$CANDIDATE_ENV"
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
    --env-file "$CANDIDATE_ENV"
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

# Step 3: Verify entire target PostgreSQL instance and object storage are empty,
# validate postgres.dump, then atomically write .env.
info "3/7 Verifying target instance is empty and validating postgres.dump..."
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
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default

endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "DALTON_ASR_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", "S3_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
minio_init_bucket = _env("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "DALTON_ASR_STORAGE_BUCKET_NAME", "STORAGE_BUCKET_NAME", "S3_BUCKET_NAME", default="chat-uploads")
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
unexpected_buckets = [b for b in existing_buckets if b != minio_init_bucket]
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

# Preflight passed: mark mutation boundary before atomically writing .env or data.
MUTATION_STARTED=1
if [[ "$ENV_FROM_BACKUP" -eq 1 ]]; then
  ENV_WRITE_TMP="$(mktemp "${WORK_DIR}/.env.tmp-XXXXXX")"
  cp "$CANDIDATE_ENV" "$ENV_WRITE_TMP"
  chmod "$ORIG_ENV_MODE" "$ENV_WRITE_TMP"
  mv "$ENV_WRITE_TMP" "$ENV_FILE"
  ENV_WRITE_TMP=""
  ACTIVE_COMPOSE_ENV="$ENV_FILE"
  # Retarget the `.env` entry inside the override file in-place to the now-written
  # ${ENV_FILE} (still preserving every other `env_file` entry in its exact original
  # order) so $STAGED_ENV_TMP can be deleted immediately while preflight, formal
  # writes, startup, and subsequent regular deployments all share the same env_file list.
  if [[ -n "$RESTORE_COMPOSE_OVERRIDE" && -f "$RESTORE_COMPOSE_OVERRIDE" ]]; then
    write_compose_env_override "$ENV_FILE" "$RESTORE_COMPOSE_OVERRIDE"
    chmod 600 "$RESTORE_COMPOSE_OVERRIDE"
  fi
  if [[ -n "$STAGED_ENV_TMP" && -f "$STAGED_ENV_TMP" ]]; then
    rm -f "$STAGED_ENV_TMP"
    STAGED_ENV_TMP=""
  fi
  ok "Atomically wrote ${ENV_FILE} from backup env.enc."
else
  warn "Using existing ${ENV_FILE} (not overwritten from env.enc)."
  warn "If BYOK_ENCRYPTION_KEY in ${ENV_FILE} differs from the backup's original .env, saved LLM API keys will fail to decrypt."
fi

# Ensure infrastructure services match the final .env
compose_cmd up -d postgres redis minio

# Step 4: Restore MinIO objects directly into original bucket name(s) with read-back verification
if [[ "$SKIP_MINIO" -eq 0 && "$STORAGE_ANY_CONFIGURED" -eq 1 && -f "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
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
manifest_by_pair: dict[tuple[str, str], dict[str, object]] = {
    (str(item["bucket"]), str(item["key"])): item for item in expected_objects
}

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
        meta = manifest_by_pair.get((bucket, key)) or {}
        content_type = (
            str(meta.get("content_type") or "").strip()
            or str((member.pax_headers or {}).get("DALTON.content_type") or "").strip()
            or "application/octet-stream"
        )
        upload_streaming(bucket, key, fp, member.size, content_type)
        uploaded_pairs.add((bucket, key))

if len(uploaded_pairs) != len(expected_objects):
    print(f"ERROR: Restored {len(uploaded_pairs)} objects, expected {len(expected_objects)}", file=sys.stderr)
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
  manifest_size="$(wc -c < "$MINIO_MANIFEST_TMP" | tr -d '[:space:]')"
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

probe_domain="${CFG_DOMAIN#[}"
probe_domain="${probe_domain%]}"
probe_url_host="$probe_domain"
probe_connect_ip="127.0.0.1"
probe_curl_extra=()
if [[ "$probe_domain" == *:* ]]; then
  probe_url_host="[${probe_domain}]"
  probe_connect_ip="[${probe_domain}]"
  probe_curl_extra=(-g)
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
      if curl -sf ${probe_curl_extra[@]+"${probe_curl_extra[@]}"} --cacert "$tmp_ca" --resolve "${probe_url_host}:${PROBE_HTTPS_PORT}:${probe_connect_ip}" \
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
    if curl -sf ${probe_curl_extra[@]+"${probe_curl_extra[@]}"} ${curl_ca_args[@]+"${curl_ca_args[@]}"} --resolve "${probe_url_host}:${PROBE_HTTPS_PORT}:${probe_connect_ip}" \
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
printf "\n  Next steps:\n"
printf "  - Verify service status: %s\n" "$(fmt_cmd docker compose -p "$COMPOSE_PROJECT" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" ps)"
printf "  - Open the web UI and confirm your workspaces, threads, and uploaded files are visible.\n"
