#!/usr/bin/env bash
# Standalone Core formal restore utility.
#
# Restores a timestamped backup directory created by `scripts/standalone/backup.sh`
# using a non-destructive "switch names" design with a single commit point:
#   1. Verifies backup integrity (`SHA256SUMS` + `manifest.json`).
#   2. Stages candidate `.env` into a temporary file (`--replace-env` decrypts
#      `env.enc`; otherwise copies existing `.env`). All preflight checks
#      (project name, external DB, Compose files, non-empty targets) run against
#      the NEW configuration, under a per-Compose-project lock at
#      `${XDG_RUNTIME_DIR:-/tmp}/dalton-<project>.restore.lock`.
#   3. Starts infrastructure (`postgres`, `redis`, `minio`), validates
#      `pg_restore --list`, and checks target DB (all non-system schemas) and
#      target MinIO buckets before mutating anything; refuses non-empty targets
#      unless `--replace-data` is passed.
#   4. Restores MinIO objects into brand-new bucket(s) `<bucket>-r<YYYYmmddHHMMSS>`
#      (or `<prefix>-<short_hash>-r<ts>` when >63 chars; verifies non-existence
#      first, tags provenance `dalton-restore=true` / `.dalton-restore.json`),
#      read-back verifies size, SHA-256, and `ContentType`, and updates all
#      matching bucket env vars in the staged `.env`. Original buckets are NEVER
#      copied, overwritten, or deleted.
#   5. Restores PostgreSQL dump into staging database `<db>_restore_<ts>` and
#      runs migrations against it (and rebuilds `edge` if `.env` came from backup).
#   6. SINGLE COMMIT POINT: stops app containers (`backend`, `crawler`, `edge`),
#      preserves existing `.env` as `.env.pre-restore-<ts>`, and commits both the
#      single-transaction database rename (`<db>` -> `<db>_pre_restore_<ts>`,
#      `<db>_restore_<ts>` -> `<db>`) and the atomic `.env` replacement together.
#      Any failure before this step leaves `.env`, database name, and original
#      buckets untouched and cleans up the staging DB and newly created bucket(s).
#   7. Restores Caddy `/data` (preserving a verified pre-restore archive) and
#      checks DB-referenced objects against the new active MinIO bucket.
#   8. Starts the full stack and runs unified health & schema checks.
#
# Usage:
#   bash scripts/standalone/restore.sh --backup-dir /var/backups/dalton/dalton-backup-20260101-020000Z-a1b2c3
#   bash scripts/standalone/restore.sh --backup-dir /path/to/backup --env-passphrase-file /etc/dalton/backup.pass
#   bash scripts/standalone/restore.sh --backup-dir /path/to/backup --replace-data
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BACKUP_DIR=""
ENV_PASSPHRASE_FILE="${DALTON_BACKUP_PASSPHRASE_FILE:-}"
COMPOSE_FILES_RAW=""
CA_FILE=""
REPLACE_DATA=0
REPLACE_ENV=0
SKIP_EDGE_BUILD=0
SKIP_CADDY=0
SKIP_MINIO=0

# Capture caller-supplied COMPOSE_PROJECT_NAME (Compose gives process env precedence over .env)
ENV_SHELL_PROJECT="${COMPOSE_PROJECT_NAME:-}"

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/restore.sh --backup-dir <dir> [OPTIONS]

Options:
  --backup-dir <dir>          Backup directory created by backup.sh (required).
  --work-dir <dir>            Deployment directory containing compose files (default: repo root).
  --env-passphrase-file <f>   Decrypt env.enc from backup into staged .env
                              (or set DALTON_BACKUP_PASSPHRASE_FILE).
  --compose-files <f1:f2>     Colon-separated Docker Compose files
                              (default: from target .env COMPOSE_FILE / DALTON_TLS_MODE).
  --ca-file <path>            CA certificate PEM file used to verify HTTPS in custom/auto mode.
  --replace-data              Allow replacing an existing non-empty database (via staging DB +
                              single-transaction rename keeping <db>_pre_restore_<ts>) and
                              switching object storage to new <bucket>-r<ts> bucket(s) while
                              leaving original bucket(s) untouched.
  --replace-env               Allow replacing an existing <work-dir>/.env from env.enc
                              (previous .env preserved as .env.pre-restore-<ts>).
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
    --ca-file)
      CA_FILE="$2"
      shift 2
      ;;
    --ca-file=*)
      CA_FILE="${1#--ca-file=}"
      shift
      ;;
    --replace-data)
      REPLACE_DATA=1
      shift
      ;;
    --replace-env)
      REPLACE_ENV=1
      shift
      ;;
    --force)
      echo "ERROR: --force has been split into --replace-data (replace DB & switch MinIO bucket) and --replace-env (replace .env from env.enc)." >&2
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

sanitize_project_name() {
  local raw="$1"
  local cleaned
  cleaned="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^[^a-z0-9]+//; s/[-_]+$//')"
  printf '%s' "${cleaned:-dalton-core}"
}

pid_is_alive() {
  local pid="$1"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 1
  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  if ps -p "$pid" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

RESTORE_TS="$(date -u +%Y%m%d%H%M%S)"
RESTORE_LOCK_DIR=""
RESTORE_LOCK_HELD=0
ENV_FILE="${WORK_DIR}/.env"
ENV_EXISTED_BEFORE=0
ORIG_ENV_MODE="600"
if [[ -f "$ENV_FILE" ]]; then
  ENV_EXISTED_BEFORE=1
  ORIG_ENV_MODE="$(get_file_mode "$ENV_FILE")"
fi
STAGED_ENV_TMP=""
ENV_CREATED_FRESH=0
ENV_FROM_BACKUP=0
ENV_PRE_BACKUP=""
ACTIVE_COMPOSE_ENV=""
COMPOSE_READY=0
RESTORE_COMMITTED=0
MINIO_MANIFEST_TMP=""
CREATED_BUCKETS_LOG=""
MINIO_NEW_BUCKETS_PENDING=0
STAGE_DB=""
STAGE_DB_CREATED=0
PRE_RESTORE_DB=""
DB_SWAPPED=0
CADDY_PRE_BACKUP=""
CFG_DB_USER="postgres"
CFG_DB_NAME="postgres"
ORIGINAL_BUCKETS=()
RESTORED_BUCKETS=()
UPDATED_ENV_BUCKET_PAIRS=()

acquire_restore_lock() {
  local lock_parent
  lock_parent="$(dirname "$RESTORE_LOCK_DIR")"
  if [[ ! -d "$lock_parent" ]]; then
    mkdir -p "$lock_parent"
  fi
  local waited=0
  local missing_pid_waited=0
  while ! mkdir "$RESTORE_LOCK_DIR" 2>/dev/null; do
    local pid_file="${RESTORE_LOCK_DIR}/pid"
    if [[ -f "$pid_file" ]]; then
      local lock_pid
      lock_pid="$(tr -d '[:space:]' < "$pid_file" 2>/dev/null || true)"
      if [[ -n "$lock_pid" && "$lock_pid" =~ ^[0-9]+$ ]]; then
        if ! pid_is_alive "$lock_pid"; then
          warn "Removing stale restore lock ${RESTORE_LOCK_DIR} left by dead PID ${lock_pid}."
          rm -f "$pid_file" 2>/dev/null || true
          rmdir "$RESTORE_LOCK_DIR" 2>/dev/null || true
          continue
        fi
        missing_pid_waited=0
      fi
    else
      missing_pid_waited=$(( missing_pid_waited + 1 ))
      if (( missing_pid_waited >= 5 )); then
        warn "Removing stale restore lock ${RESTORE_LOCK_DIR} (missing PID file)."
        rmdir "$RESTORE_LOCK_DIR" 2>/dev/null || true
        missing_pid_waited=0
        continue
      fi
    fi
    if (( waited >= 60 )); then
      err "Another restore is already running for project '${LOCK_PROJECT}' (timed out waiting for ${RESTORE_LOCK_DIR})."
      exit 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  printf '%s\n' "$$" > "${RESTORE_LOCK_DIR}/pid"
  RESTORE_LOCK_HELD=1
}

release_restore_lock() {
  if [[ "$RESTORE_LOCK_HELD" -eq 1 && -n "$RESTORE_LOCK_DIR" && -d "$RESTORE_LOCK_DIR" ]]; then
    local lock_pid=""
    if [[ -f "${RESTORE_LOCK_DIR}/pid" ]]; then
      lock_pid="$(tr -d '[:space:]' < "${RESTORE_LOCK_DIR}/pid" 2>/dev/null || true)"
    fi
    if [[ -z "$lock_pid" || "$lock_pid" == "$$" ]]; then
      rm -f "${RESTORE_LOCK_DIR}/pid" 2>/dev/null || true
      rmdir "$RESTORE_LOCK_DIR" 2>/dev/null || true
    fi
    RESTORE_LOCK_HELD=0
  fi
}

cleanup_minio_new_buckets() {
  [[ "$MINIO_NEW_BUCKETS_PENDING" -eq 1 && "$RESTORE_COMMITTED" -eq 0 && "$COMPOSE_READY" -eq 1 ]] || return 0
  [[ -n "$CREATED_BUCKETS_LOG" && -f "$CREATED_BUCKETS_LOG" ]] || return 0
  local created_list
  created_list="$(awk -F'^CREATED_BUCKET:' '/^CREATED_BUCKET:/ {print $2}' "$CREATED_BUCKETS_LOG" | awk 'NF && !seen[$0]++')"
  [[ -n "$created_list" ]] || return 0

  warn "Cleaning up uncommitted MinIO restore bucket(s) created during this failed run..."
  local cleanup_py='
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

def delete_keys_checked(client, bucket: str, keys: list[str]) -> int:
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        resp = client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": False},
        )
        errors = resp.get("Errors") or []
        if errors:
            parts = []
            for e in errors[:10]:
                ek = e.get("Key", "?")
                ec = e.get("Code", "Error")
                em = e.get("Message", "")
                parts.append(f"{ek}: {ec} ({em})")
            details = "; ".join(parts)
            raise RuntimeError(
                f"DeleteObjects failed for {len(errors)} object(s) in bucket {bucket!r}: {details}"
            )
        deleted += len(batch)
    return deleted

buckets = [line.strip() for line in sys.stdin if line.strip()]
if not buckets:
    sys.exit(0)

endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID")
secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY")
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "STORAGE_REGION", default="us-east-1")
addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()
if not access_key or not secret_key:
    sys.exit(0)

client = boto3.client(
    "s3",
    endpoint_url=endpoint,
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name=region,
    config=Config(signature_version="s3v4", s3={"addressing_style": addressing_style}, connect_timeout=5, read_timeout=30),
)

failed: list[str] = []
for b in buckets:
    try:
        client.head_bucket(Bucket=b)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in ("404", "NoSuchBucket", "NotFound") or status == 404:
            continue
        failed.append(f"{b}: {exc}")
        continue
    try:
        keys: list[str] = []
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=b):
            keys.extend(e["Key"] for e in page.get("Contents", []))
        if keys:
            delete_keys_checked(client, b, keys)
        client.delete_bucket(Bucket=b)
    except Exception as exc:
        failed.append(f"{b}: {exc}")

if failed:
    for item in failed:
        print(f"[restore] WARN: Failed to clean up temporary bucket {item}", file=sys.stderr)
    sys.exit(1)
'
  if ! printf '%s\n' "$created_list" | compose_cmd run --rm -T --no-deps minio-init \
      uv run --no-sync python -c "$cleanup_py" >&2; then
    warn "One or more uncommitted MinIO restore buckets could not be removed automatically."
  fi
  MINIO_NEW_BUCKETS_PENDING=0
}

cleanup() {
  # Run container-dependent cleanups FIRST while STAGED_ENV_TMP / ACTIVE_COMPOSE_ENV still exist.
  cleanup_minio_new_buckets
  if [[ "$STAGE_DB_CREATED" -eq 1 && "$DB_SWAPPED" -eq 0 && "$COMPOSE_READY" -eq 1 && -n "$STAGE_DB" ]]; then
    warn "Cleaning up unfinished staging database '${STAGE_DB}'..."
    compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${STAGE_DB}' AND pid <> pg_backend_pid();" \
      -c "DROP DATABASE IF EXISTS \"${STAGE_DB}\";" >/dev/null 2>&1 || true
  fi
  if [[ -n "$STAGED_ENV_TMP" && -f "$STAGED_ENV_TMP" ]]; then
    rm -f "$STAGED_ENV_TMP"
  fi
  if [[ "$ENV_CREATED_FRESH" -eq 1 && "$RESTORE_COMMITTED" -eq 0 && -f "$ENV_FILE" ]]; then
    rm -f "$ENV_FILE"
  fi
  if [[ -n "$CREATED_BUCKETS_LOG" && -f "$CREATED_BUCKETS_LOG" ]]; then
    rm -f "$CREATED_BUCKETS_LOG"
  fi
  if [[ -n "$MINIO_MANIFEST_TMP" && -f "$MINIO_MANIFEST_TMP" ]]; then
    rm -f "$MINIO_MANIFEST_TMP"
  fi
  release_restore_lock
}
trap cleanup EXIT

# Step 1: Verify checksums in backup directory
info "1/8 Verifying backup archive integrity..."
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

# Read PBKDF2 encryption parameters and compose_project from manifest.json (safe defaults for v1 backups)
ENV_CIPHER="aes-256-cbc"
ENV_KDF="pbkdf2"
ENV_KDF_DIGEST="sha256"
ENV_KDF_ITER="600000"
MANIFEST_COMPOSE_PROJECT=""
if [[ -f "${BACKUP_DIR}/manifest.json" ]]; then
  IFS=$'\t' read -r ENV_CIPHER ENV_KDF ENV_KDF_DIGEST ENV_KDF_ITER MANIFEST_COMPOSE_PROJECT < <(python3 - "${BACKUP_DIR}/manifest.json" <<'PY'
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
proj = str(data.get("compose_project") or "")
print(f"{cipher}\t{kdf}\t{digest}\t{iters}\t{proj}")
PY
  )
fi

# Step 2: Prepare candidate new .env in STAGED_ENV_TMP (NEVER modify ${WORK_DIR}/.env until Step 6)
STAGED_ENV_TMP="$(mktemp "${WORK_DIR}/.env.restore.XXXXXX")"
chmod 600 "$STAGED_ENV_TMP"

if [[ -f "${BACKUP_DIR}/env.enc" && -n "$ENV_PASSPHRASE_FILE" ]]; then
  if [[ ! -r "$ENV_PASSPHRASE_FILE" || ! -s "$ENV_PASSPHRASE_FILE" ]]; then
    err "Passphrase file is missing, unreadable, or empty: ${ENV_PASSPHRASE_FILE}"
    exit 1
  fi
  if [[ "$ENV_EXISTED_BEFORE" -eq 0 || "$REPLACE_ENV" -eq 1 ]]; then
    info "2/8 Decrypting env.enc into staging .env (${ENV_CIPHER}/${ENV_KDF}/${ENV_KDF_DIGEST}/${ENV_KDF_ITER})..."
    if ! openssl enc -d "-${ENV_CIPHER}" -pbkdf2 -md "${ENV_KDF_DIGEST}" -iter "${ENV_KDF_ITER}" \
        -in "${BACKUP_DIR}/env.enc" \
        -out "$STAGED_ENV_TMP" \
        -pass "file:${ENV_PASSPHRASE_FILE}"; then
      err "Failed to decrypt ${BACKUP_DIR}/env.enc (check passphrase file)."
      exit 1
    fi
    chmod 600 "$STAGED_ENV_TMP"
    ENV_FROM_BACKUP=1
    ok "Decrypted env.enc into temporary file (will commit to ${ENV_FILE} only after all data & migrations succeed)."
  else
    info "2/8 Staging copy of existing ${ENV_FILE} (pass --replace-env to replace from env.enc)."
    cp "$ENV_FILE" "$STAGED_ENV_TMP"
    chmod 600 "$STAGED_ENV_TMP"
  fi
elif [[ "$ENV_EXISTED_BEFORE" -eq 1 ]]; then
  info "2/8 Staging copy of existing ${ENV_FILE}."
  cp "$ENV_FILE" "$STAGED_ENV_TMP"
  chmod 600 "$STAGED_ENV_TMP"
else
  err "Missing ${ENV_FILE}. Copy your saved .env into ${WORK_DIR}, pass --env-passphrase-file (if env.enc is in the backup), or run scripts/standalone/init.sh first."
  exit 1
fi

# All --replace-env preflight checks use the NEW configuration in STAGED_ENV_TMP.
# Project-name consistency guard when replacing an existing instance's .env:
if [[ "$ENV_EXISTED_BEFORE" -eq 1 && "$ENV_FROM_BACKUP" -eq 1 ]]; then
  CUR_PROJECT_RAW="${ENV_SHELL_PROJECT:-$(read_env_file_var "$ENV_FILE" "COMPOSE_PROJECT_NAME" "")}"
  CUR_PROJECT="$(sanitize_project_name "${CUR_PROJECT_RAW:-$(basename "$WORK_DIR")}")"
  STAGED_PROJECT_RAW="$(read_env_file_var "$STAGED_ENV_TMP" "COMPOSE_PROJECT_NAME" "")"
  BACKUP_PROJECT_RAW="${STAGED_PROJECT_RAW:-$MANIFEST_COMPOSE_PROJECT}"
  if [[ -n "$BACKUP_PROJECT_RAW" ]]; then
    BACKUP_PROJECT="$(sanitize_project_name "$BACKUP_PROJECT_RAW")"
    if [[ "$BACKUP_PROJECT" != "$CUR_PROJECT" ]]; then
      err "COMPOSE_PROJECT_NAME mismatch: new backup config specifies '${BACKUP_PROJECT}', but current instance is '${CUR_PROJECT}'."
      err "Refusing --replace-env because changing COMPOSE_PROJECT_NAME would switch Docker Compose to a different project's containers and volumes (leaving existing ${ENV_FILE} untouched)."
      exit 1
    fi
  fi
fi

# Acquire restore lock keyed by Compose project name (not deployment directory)
LOCK_PROJECT_RAW="${ENV_SHELL_PROJECT:-$(read_env_file_var "$STAGED_ENV_TMP" "COMPOSE_PROJECT_NAME" "")}"
LOCK_PROJECT="$(sanitize_project_name "${LOCK_PROJECT_RAW:-$(basename "$WORK_DIR")}")"
RESTORE_LOCK_DIR="${XDG_RUNTIME_DIR:-/tmp}/dalton-${LOCK_PROJECT}.restore.lock"
acquire_restore_lock

# External PostgreSQL guard using the NEW configuration in STAGED_ENV_TMP
CFG_DB_HOST="$(read_env_file_var "$STAGED_ENV_TMP" "DB_HOST" "postgres")"
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

# Read all target configuration strictly from STAGED_ENV_TMP (new config)
CFG_DB_USER="$(read_env_file_var "$STAGED_ENV_TMP" "DB_USER" "postgres")"
CFG_DB_NAME="$(read_env_file_var "$STAGED_ENV_TMP" "DB_NAME" "postgres")"
CFG_DOMAIN="$(read_env_file_var "$STAGED_ENV_TMP" "DOMAIN" "localhost")"
CFG_TLS_MODE="$(read_env_file_var "$STAGED_ENV_TMP" "DALTON_TLS_MODE" "loopback")"
CFG_EDGE_PORT="$(read_env_file_var "$STAGED_ENV_TMP" "DALTON_EDGE_PORT" "8088")"
CFG_EDGE_HTTPS_PORT="$(read_env_file_var "$STAGED_ENV_TMP" "DALTON_EDGE_HTTPS_PORT" "18443")"
validate_db_identifiers "$CFG_DB_USER" "$CFG_DB_NAME"

# Build Compose file list from NEW configuration in STAGED_ENV_TMP
if [[ -z "$COMPOSE_FILES_RAW" ]]; then
  env_compose_file="$(read_env_file_var "$STAGED_ENV_TMP" "COMPOSE_FILE" "")"
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
COMPOSE_ARGS=(--project-directory "$WORK_DIR")
for cf in "${COMPOSE_FILES_ARR[@]}"; do
  [[ -z "$cf" ]] && continue
  if [[ "$cf" != /* ]]; then
    cf="${WORK_DIR}/${cf}"
  fi
  if [[ ! -f "$cf" ]]; then
    err "Compose file not found: ${cf}"
    exit 1
  fi
  COMPOSE_ARGS+=(-f "$cf")
done

ACTIVE_COMPOSE_ENV="$STAGED_ENV_TMP"
compose_cmd() {
  docker compose --env-file "$ACTIVE_COMPOSE_ENV" "${COMPOSE_ARGS[@]}" "$@"
}
COMPOSE_READY=1

# On a clean host with no existing .env, Docker Compose requires ${WORK_DIR}/.env to
# exist before parsing `env_file: .env`. Place a temporary copy ONLY after static checks
# pass; cleanup() deletes it if anything fails before the single commit point in Step 6.
if [[ "$ENV_EXISTED_BEFORE" -eq 0 ]]; then
  cp "$STAGED_ENV_TMP" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ENV_CREATED_FRESH=1
fi

# Step 3: Start infrastructure & inspect target DB (all non-system schemas) and MinIO buckets
# using the NEW configuration values BEFORE mutating anything.
info "3/8 Starting infrastructure services (postgres, redis, minio) and inspecting target state..."
compose_cmd up -d postgres redis minio

info "Waiting for PostgreSQL to become ready..."
pg_ready=0
for _ in $(seq 1 60); do
  if compose_cmd exec -T postgres pg_isready -U "$CFG_DB_USER" -d template1 -q >/dev/null 2>&1; then
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

DB_EXISTS="$(compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -Atqc \
  "SELECT 1 FROM pg_database WHERE datname = '${CFG_DB_NAME}';" | tr -d '[:space:]')"
if [[ "$DB_EXISTS" == "1" ]]; then
  DB_EXISTS=1
  DB_OBJECT_COUNT="$(compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -Atqc \
    "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f');" \
    | tr -d '[:space:]')"
  if [[ -z "$DB_OBJECT_COUNT" || ! "$DB_OBJECT_COUNT" =~ ^[0-9]+$ ]]; then
    err "Could not inspect target database '${CFG_DB_NAME}'."
    exit 1
  fi
else
  DB_EXISTS=0
  DB_OBJECT_COUNT=0
fi

if (( DB_OBJECT_COUNT > 0 )) && [[ "$REPLACE_DATA" -ne 1 ]]; then
  err "Target database '${CFG_DB_NAME}' is NOT empty (${DB_OBJECT_COUNT} object(s) across non-system schemas)."
  err "Refusing to replace existing data. Pass --replace-data to stage, migrate, and atomically switch '${CFG_DB_NAME}'."
  exit 1
fi

# Extract all configured bucket names from the NEW configuration (STAGED_ENV_TMP) for preflight
STAGED_CONFIG_BUCKETS_JSON="$(python3 - "$STAGED_ENV_TMP" <<'PY'
import json, re, sys

BUCKET_VARS = (
    "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
    "DALTON_ASR_STORAGE_BUCKET_NAME",
    "STORAGE_BUCKET_NAME",
    "S3_BUCKET_NAME",
    "OSS_BUCKET_NAME",
    "R2_BUCKET_NAME",
)
line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([=:])(.*)$")
vals: dict[str, str] = {}
with open(sys.argv[1], encoding="utf-8") as fh:
    for raw in fh:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = line_re.match(raw.rstrip("\r\n"))
        if not m or m.group(1) not in BUCKET_VARS:
            continue
        val = m.group(3).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        elif val and val[0] not in ("'", '"'):
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        if val:
            vals[m.group(1)] = val
if "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" not in vals:
    vals["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] = vals.get("STORAGE_BUCKET_NAME") or "chat-uploads"
print(json.dumps(sorted(set(vals.values()))))
PY
)"

# Pre-extract MinIO manifest and inspect target MinIO buckets before mutating anything
if [[ "$SKIP_MINIO" -eq 0 && -f "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
  MINIO_MANIFEST_TMP="$(mktemp)"
  chmod 600 "$MINIO_MANIFEST_TMP"
  if ! tar -xOzf "${BACKUP_DIR}/minio-objects.tar.gz" _minio_manifest.json > "$MINIO_MANIFEST_TMP"; then
    err "Failed to extract _minio_manifest.json from ${BACKUP_DIR}/minio-objects.tar.gz."
    exit 1
  fi

  MINIO_PREFLIGHT_PY='
from __future__ import annotations
import json, os, sys, time
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default

manifest = json.loads(sys.stdin.read() or "{}")
config_buckets = set(json.loads(os.environ.get("DALTON_TARGET_CONFIG_BUCKETS", "[]")))
endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID")
secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY")
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "STORAGE_REGION", default="us-east-1")
addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()

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

target_buckets = set(manifest.get("buckets") or []) | config_buckets
existing_buckets = {b["Name"] for b in resp.get("Buckets", []) if b.get("Name")}
non_empty: list[tuple[str, int]] = []
for bucket in sorted(target_buckets & existing_buckets):
    count = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for entry in page.get("Contents", []):
            if entry.get("Key") != ".dalton-restore.json":
                count += 1
    if count > 0:
        non_empty.append((bucket, count))

if non_empty:
    summary = ", ".join(f"{b}({c})" for b, c in non_empty)
    print(summary)
    sys.exit(10)
print("EMPTY")
'
  set +e
  MINIO_PREFLIGHT_OUT="$(compose_cmd run --rm -T --no-deps \
    -e "DALTON_TARGET_CONFIG_BUCKETS=${STAGED_CONFIG_BUCKETS_JSON}" \
    minio-init uv run --no-sync python -c "$MINIO_PREFLIGHT_PY" < "$MINIO_MANIFEST_TMP" 2>&1)"
  MINIO_PREFLIGHT_RC=$?
  set -e
  if [[ "$MINIO_PREFLIGHT_RC" -eq 10 ]]; then
    if [[ "$REPLACE_DATA" -ne 1 ]]; then
      err "Target MinIO storage is NOT empty (${MINIO_PREFLIGHT_OUT})."
      err "Refusing to switch away from existing objects. Pass --replace-data to restore into new <bucket>-r<ts> bucket(s) while keeping existing bucket(s) untouched."
      exit 1
    fi
    info "Target MinIO bucket(s) contain existing objects (${MINIO_PREFLIGHT_OUT}); original bucket(s) will stay untouched and new <bucket>-r${RESTORE_TS} bucket(s) will be created."
  elif [[ "$MINIO_PREFLIGHT_RC" -ne 0 ]]; then
    err "Failed to inspect target MinIO storage: ${MINIO_PREFLIGHT_OUT}"
    exit 1
  else
    ok "Confirmed target MinIO buckets are empty."
  fi
fi

# Step 4: Restore MinIO objects into brand-new bucket(s) <bucket>-r<YYYYmmddHHMMSS>
# (or <prefix>-<short_hash>-r<ts> when >63 chars), verify read-back (size, SHA-256, ContentType),
# and update all bucket env vars inside STAGED_ENV_TMP (NEVER touching original buckets or live .env).
if [[ "$SKIP_MINIO" -eq 0 && -f "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
  info "4/8 Restoring MinIO objects into new <bucket>-r${RESTORE_TS} bucket(s) with read-back verification..."
  CREATED_BUCKETS_LOG="$(mktemp)"
  chmod 600 "$CREATED_BUCKETS_LOG"

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

def derive_restore_bucket_name(bucket: str, ts: str) -> str:
    """Build <bucket>-r<YYYYmmddHHMMSS>, or <prefix>-<short_hash>-r<ts> when >63 chars."""
    suffix = f"-r{ts}"
    if len(bucket) + len(suffix) <= 63:
        return f"{bucket}{suffix}"
    short_hash = hashlib.sha256(bucket.encode("utf-8")).hexdigest()[:8]
    tail = f"-{short_hash}{suffix}"
    max_prefix = 63 - len(tail)
    prefix = bucket[:max_prefix].rstrip("-.") or "b"
    return f"{prefix}{tail}"

restore_ts = os.environ["DALTON_RESTORE_TS"]
endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID")
secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY")
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "STORAGE_REGION", default="us-east-1")
addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()

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

# Derive unique target bucket names for all source buckets BEFORE creating anything.
new_for_bucket: dict[str, str] = {}
used_new_names: set[str] = set()
for src_b in sorted(source_buckets):
    new_b = derive_restore_bucket_name(src_b, restore_ts)
    if new_b in used_new_names:
        print(
            f"ERROR: Derived restore bucket name collision for {src_b!r} -> {new_b!r}",
            file=sys.stderr,
        )
        sys.exit(1)
    used_new_names.add(new_b)
    new_for_bucket[src_b] = new_b

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

# Verify NONE of the derived new bucket names already exist before creating any bucket.
for src_b, new_b in sorted(new_for_bucket.items()):
    try:
        client.head_bucket(Bucket=new_b)
        print(
            f"ERROR: Target restore bucket {new_b!r} (for source {src_b!r}) already exists; refusing to reuse or overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code not in ("404", "NoSuchBucket", "NotFound") and status != 404:
            print(
                f"ERROR: Cannot confirm target restore bucket {new_b!r} does not exist: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)

def create_fresh_bucket(src_b: str, new_b: str) -> None:
    kwargs: dict[str, object] = {"Bucket": new_b}
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    client.create_bucket(**kwargs)
    # Emit immediately so host cleanup knows ONLY buckets created by this run.
    print(f"CREATED_BUCKET:{new_b}", flush=True)
    try:
        client.delete_bucket_policy(Bucket=new_b)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code != "NoSuchBucketPolicy":
            raise
    # Record provenance via S3 bucket tags; fall back to .dalton-restore.json marker if tagging is unsupported.
    try:
        client.put_bucket_tagging(
            Bucket=new_b,
            Tagging={
                "TagSet": [
                    {"Key": "dalton-restore", "Value": "true"},
                    {"Key": "dalton-restore-source", "Value": src_b},
                    {"Key": "dalton-restore-ts", "Value": restore_ts},
                ]
            },
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in ("NotImplemented", "MethodNotAllowed") or status in (405, 501):
            marker_body = json.dumps(
                {"restore": True, "source_bucket": src_b, "restore_ts": restore_ts},
                ensure_ascii=False,
            ).encode("utf-8")
            client.put_object(
                Bucket=new_b,
                Key=RESTORE_MARKER_KEY,
                Body=marker_body,
                ContentType="application/json",
            )
        else:
            raise

for src_b, new_b in sorted(new_for_bucket.items()):
    create_fresh_bucket(src_b, new_b)

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
        src_b, sep, key = rel.partition("/")
        if not sep or not src_b or not key or key == RESTORE_MARKER_KEY:
            fp = tf.extractfile(member)
            if fp is not None:
                while fp.read(CHUNK_BYTES):
                    pass
            continue
        fp = tf.extractfile(member)
        if fp is None:
            continue
        if src_b not in new_for_bucket:
            print(f"ERROR: Archive contains object for undeclared bucket {src_b!r}", file=sys.stderr)
            sys.exit(1)
        new_b = new_for_bucket[src_b]
        meta = manifest_by_pair.get((src_b, key)) or {}
        content_type = (
            str(meta.get("content_type") or "").strip()
            or str((member.pax_headers or {}).get("DALTON.content_type") or "").strip()
            or "application/octet-stream"
        )
        upload_streaming(new_b, key, fp, member.size, content_type)
        uploaded_pairs.add((src_b, key))

if len(uploaded_pairs) != len(expected_objects):
    print(f"ERROR: Restored {len(uploaded_pairs)} objects, expected {len(expected_objects)}", file=sys.stderr)
    sys.exit(1)

# Read-back verification from new bucket(s): size, SHA-256, and ContentType
for item in expected_objects:
    src_b = str(item["bucket"])
    new_b = new_for_bucket[src_b]
    key = str(item["key"])
    exp_size = int(item["size"])
    exp_sha = str(item["sha256"])
    exp_ct = str(item.get("content_type") or "application/octet-stream")
    resp = client.get_object(Bucket=new_b, Key=key)
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
            f"ERROR: Read-back mismatch for {(new_b, key)}: "
            f"got=({got_size}, {got_sha}), expected=({exp_size}, {exp_sha})",
            file=sys.stderr,
        )
        sys.exit(1)
    if got_ct != exp_ct:
        print(
            f"ERROR: Read-back ContentType mismatch for {(new_b, key)}: "
            f"got={got_ct!r}, expected={exp_ct!r}",
            file=sys.stderr,
        )
        sys.exit(1)

for src_b, new_b in sorted(new_for_bucket.items()):
    print(f"BUCKET_MAP:{src_b}\t{new_b}", flush=True)
print(
    f"[restore] Restored and read-back verified {len(uploaded_pairs)} object(s) into "
    f"{len(new_for_bucket)} new bucket(s); original bucket(s) untouched.",
    flush=True,
)
'
  MINIO_NEW_BUCKETS_PENDING=1
  manifest_size="$(wc -c < "$MINIO_MANIFEST_TMP" | tr -d '[:space:]')"
  {
    printf '%s\n' "$manifest_size"
    cat "$MINIO_MANIFEST_TMP"
    cat "${BACKUP_DIR}/minio-objects.tar.gz"
  } | compose_cmd run --rm -T --no-deps -e "DALTON_RESTORE_TS=${RESTORE_TS}" \
        minio-init uv run --no-sync python -c "$MINIO_RESTORE_PY" > "$CREATED_BUCKETS_LOG"

  while IFS= read -r mline; do
    case "$mline" in
      CREATED_BUCKET:*|BUCKET_MAP:*) ;;
      "") ;;
      *) printf '%s\n' "$mline" ;;
    esac
  done < "$CREATED_BUCKETS_LOG"

  # Update ALL bucket-name env vars inside STAGED_ENV_TMP (still NOT touching live .env!)
  ENV_BUCKET_UPDATE_OUT="$(python3 - "$STAGED_ENV_TMP" "$MINIO_MANIFEST_TMP" "$CREATED_BUCKETS_LOG" <<'PY'
from __future__ import annotations
import json, os, re, sys

BUCKET_ENV_VARS = (
    "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
    "DALTON_ASR_STORAGE_BUCKET_NAME",
    "STORAGE_BUCKET_NAME",
    "S3_BUCKET_NAME",
    "OSS_BUCKET_NAME",
    "R2_BUCKET_NAME",
)

staged_env_path, manifest_path, log_path = sys.argv[1:4]
manifest = json.loads(open(manifest_path, encoding="utf-8").read() or "{}")

bucket_map: dict[str, str] = {}
with open(log_path, encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("BUCKET_MAP:"):
            src_b, _, new_b = line[len("BUCKET_MAP:"):].rstrip("\r\n").partition("\t")
            if src_b and new_b:
                bucket_map[src_b] = new_b

if not bucket_map:
    sys.exit(0)

line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([=:])(.*)$")
raw_lines = open(staged_env_path, encoding="utf-8").read().splitlines(keepends=True)
cur_vals: dict[str, str] = {}
for raw in raw_lines:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#"):
        continue
    m = line_re.match(raw.rstrip("\r\n"))
    if not m or m.group(1) not in BUCKET_ENV_VARS:
        continue
    val = m.group(3).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    elif val and val[0] not in ("'", '"'):
        val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
    cur_vals[m.group(1)] = val

updates: dict[str, str] = {}
var_buckets = manifest.get("var_buckets") or {}
if isinstance(var_buckets, dict):
    for var_name, src_b in var_buckets.items():
        if var_name not in BUCKET_ENV_VARS or src_b not in bucket_map:
            continue
        if var_name == "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" or cur_vals.get(var_name):
            updates[var_name] = bucket_map[src_b]

for var_name in BUCKET_ENV_VARS:
    if var_name in updates:
        continue
    cur_v = cur_vals.get(var_name, "")
    if cur_v and cur_v in bucket_map:
        updates[var_name] = bucket_map[cur_v]

if "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" not in updates:
    primary = str(manifest.get("primary_bucket") or "").strip()
    if primary and primary in bucket_map:
        updates["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] = bucket_map[primary]
    elif "chat-uploads" in bucket_map:
        updates["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] = bucket_map["chat-uploads"]
    elif len(bucket_map) == 1:
        updates["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] = next(iter(bucket_map.values()))

if updates:
    replaced: set[str] = set()
    out_lines: list[str] = []
    for raw in raw_lines:
        m = line_re.match(raw.rstrip("\r\n"))
        if m and m.group(1) in updates:
            k = m.group(1)
            if k not in replaced:
                out_lines.append(f"{k}={updates[k]}\n")
                replaced.add(k)
            continue
        out_lines.append(raw)
    if out_lines and not out_lines[-1].endswith("\n"):
        out_lines[-1] = out_lines[-1] + "\n"
    for k in BUCKET_ENV_VARS:
        if k in updates and k not in replaced:
            out_lines.append(f"{k}={updates[k]}\n")
    tmp_out = f"{staged_env_path}.rewrite"
    with open(tmp_out, "w", encoding="utf-8") as fh:
        fh.writelines(out_lines)
    os.chmod(tmp_out, 0o600)
    os.replace(tmp_out, staged_env_path)

for k in BUCKET_ENV_VARS:
    if k in updates:
        old_v = cur_vals.get(k) or ("chat-uploads" if k == "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" else "")
        print(f"ENV_BUCKET_UPDATE:{k}\t{old_v}\t{updates[k]}")
PY
)"
  while IFS= read -r uline; do
    if [[ "$uline" == ENV_BUCKET_UPDATE:* ]]; then
      IFS=$'\t' read -r var_name old_b new_b <<< "${uline#ENV_BUCKET_UPDATE:}"
      UPDATED_ENV_BUCKET_PAIRS+=("${var_name}: ${old_b:-<unset>} -> ${new_b}")
      if [[ "$ENV_EXISTED_BEFORE" -eq 1 ]]; then
        prev_live_b="$(read_env_file_var "$ENV_FILE" "$var_name" "")"
        if [[ -z "$prev_live_b" && "$var_name" == "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" ]]; then
          prev_live_b="chat-uploads"
        fi
        if [[ -n "$prev_live_b" && "$prev_live_b" != "$new_b" ]]; then
          ORIGINAL_BUCKETS+=("$prev_live_b")
        fi
      fi
      RESTORED_BUCKETS+=("$new_b")
      info "Staged .env update: ${var_name}=${new_b} (previous: ${old_b:-<default>})"
    fi
  done <<< "$ENV_BUCKET_UPDATE_OUT"
  ok "MinIO new bucket(s) verified and staged in temporary .env (original buckets untouched)."
else
  info "4/8 Skipping MinIO restore."
fi

# Step 5: Restore PostgreSQL dump into staging database <db>_restore_<ts> and run migrations
# BEFORE stopping app containers or committing .env / database rename.
db_prefix="${CFG_DB_NAME:0:32}"
STAGE_DB="${db_prefix}_restore_${RESTORE_TS}"
PRE_RESTORE_DB="${db_prefix}_pre_restore_${RESTORE_TS}"

info "5/8 Restoring into staging database '${STAGE_DB}' and running migrations before commit..."
stage_exists="$(compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -Atqc \
  "SELECT 1 FROM pg_database WHERE datname = '${STAGE_DB}';" | tr -d '[:space:]')"
if [[ "$stage_exists" == "1" ]]; then
  err "Staging database '${STAGE_DB}' already exists; refusing to overwrite."
  exit 1
fi

compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"${STAGE_DB}\" OWNER \"${CFG_DB_USER}\";"
STAGE_DB_CREATED=1

compose_cmd exec -T postgres pg_restore \
  -U "$CFG_DB_USER" \
  -d "$STAGE_DB" \
  --no-owner \
  --no-acl \
  --exit-on-error < "${BACKUP_DIR}/postgres.dump"
ok "Staging database '${STAGE_DB}' restored."

info "Running migrations against staging database '${STAGE_DB}'..."
compose_cmd run --rm -e "DB_NAME=${STAGE_DB}" migrate
ok "Staging database '${STAGE_DB}' migrated to current heads."

# Rebuild edge before commit when .env came from backup so a build failure never mutates live DB or .env.
if [[ "$ENV_FROM_BACKUP" -eq 1 && "$SKIP_EDGE_BUILD" -eq 0 ]]; then
  info "Rebuilding edge image to match staged .env before commit..."
  compose_cmd build edge
  ok "Rebuilt edge image."
fi

# Step 6: SINGLE COMMIT POINT — stop app containers, preserve existing .env as .env.pre-restore-<ts>,
# then commit BOTH the single-transaction database rename AND the new .env together.
stop_app_containers() {
  local running_apps
  running_apps="$(compose_cmd ps --status running --services 2>/dev/null | grep -E '^(backend|crawler|edge)$' || true)"
  if [[ -n "$running_apps" ]]; then
    info "Stopping application containers (backend, crawler, edge)..."
    compose_cmd stop backend crawler edge
  fi
}

info "6/8 Committing database rename and staged .env together (single commit point)..."
stop_app_containers

if [[ "$ENV_EXISTED_BEFORE" -eq 1 ]]; then
  ENV_PRE_BACKUP="${WORK_DIR}/.env.pre-restore-${RESTORE_TS}"
  cp -p "$ENV_FILE" "$ENV_PRE_BACKUP"
  chmod 600 "$ENV_PRE_BACKUP"
  if ! cmp -s "$ENV_FILE" "$ENV_PRE_BACKUP"; then
    err "Failed to verify pre-restore .env copy at ${ENV_PRE_BACKUP}; aborting before commit."
    exit 1
  fi
  ok "Preserved existing .env as ${ENV_PRE_BACKUP}."
fi

if [[ "$DB_EXISTS" -eq 1 ]]; then
  info "Atomically swapping '${CFG_DB_NAME}' -> '${PRE_RESTORE_DB}' and '${STAGE_DB}' -> '${CFG_DB_NAME}' in a single transaction..."
  compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname IN ('${CFG_DB_NAME}', '${STAGE_DB}')
   AND pid <> pg_backend_pid();
DO \$\$
BEGIN
  FOR i IN 1..30 LOOP
    EXIT WHEN NOT EXISTS (
      SELECT 1 FROM pg_stat_activity
       WHERE datname IN ('${CFG_DB_NAME}', '${STAGE_DB}')
         AND pid <> pg_backend_pid()
    );
    PERFORM pg_sleep(0.1);
  END LOOP;
END \$\$;
BEGIN;
ALTER DATABASE "${CFG_DB_NAME}" RENAME TO "${PRE_RESTORE_DB}";
ALTER DATABASE "${STAGE_DB}" RENAME TO "${CFG_DB_NAME}";
COMMIT;
SQL
else
  info "Atomically renaming staging database '${STAGE_DB}' -> '${CFG_DB_NAME}'..."
  compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 <<SQL
SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
 WHERE datname = '${STAGE_DB}'
   AND pid <> pg_backend_pid();
DO \$\$
BEGIN
  FOR i IN 1..30 LOOP
    EXIT WHEN NOT EXISTS (
      SELECT 1 FROM pg_stat_activity
       WHERE datname = '${STAGE_DB}'
         AND pid <> pg_backend_pid()
    );
    PERFORM pg_sleep(0.1);
  END LOOP;
END \$\$;
ALTER DATABASE "${STAGE_DB}" RENAME TO "${CFG_DB_NAME}";
SQL
fi

chmod "${ORIG_ENV_MODE}" "$STAGED_ENV_TMP"
if ! mv "$STAGED_ENV_TMP" "$ENV_FILE"; then
  err "Failed to atomically move staged .env to ${ENV_FILE}; reverting database rename..."
  if [[ "$DB_EXISTS" -eq 1 ]]; then
    compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 <<SQL || true
BEGIN;
ALTER DATABASE "${CFG_DB_NAME}" RENAME TO "${STAGE_DB}";
ALTER DATABASE "${PRE_RESTORE_DB}" RENAME TO "${CFG_DB_NAME}";
COMMIT;
SQL
  else
    compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 \
      -c "ALTER DATABASE \"${CFG_DB_NAME}\" RENAME TO \"${STAGE_DB}\";" || true
  fi
  exit 1
fi

STAGED_ENV_TMP=""
ACTIVE_COMPOSE_ENV="$ENV_FILE"
DB_SWAPPED=1
STAGE_DB_CREATED=0
MINIO_NEW_BUCKETS_PENDING=0
RESTORE_COMMITTED=1

# If the previous DB was empty (0 objects), drop the empty pre-restore copy.
if [[ "$DB_EXISTS" -eq 1 && "$DB_OBJECT_COUNT" -eq 0 ]]; then
  compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"${PRE_RESTORE_DB}\";" >/dev/null 2>&1 || true
  PRE_RESTORE_DB=""
elif [[ "$DB_EXISTS" -eq 0 ]]; then
  PRE_RESTORE_DB=""
fi

ok "Committed database rename and new ${ENV_FILE} together."

# Re-read configuration STRICTLY from the committed .env file (never from shell variables).
CFG_DB_USER="$(read_env_file_var "$ENV_FILE" "DB_USER" "postgres")"
CFG_DB_NAME="$(read_env_file_var "$ENV_FILE" "DB_NAME" "postgres")"
CFG_DOMAIN="$(read_env_file_var "$ENV_FILE" "DOMAIN" "localhost")"
CFG_TLS_MODE="$(read_env_file_var "$ENV_FILE" "DALTON_TLS_MODE" "loopback")"
CFG_EDGE_PORT="$(read_env_file_var "$ENV_FILE" "DALTON_EDGE_PORT" "8088")"
CFG_EDGE_HTTPS_PORT="$(read_env_file_var "$ENV_FILE" "DALTON_EDGE_HTTPS_PORT" "18443")"
CFG_CHAT_BUCKET="$(read_env_file_var "$ENV_FILE" "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" "chat-uploads")"

if [[ "$ENV_FROM_BACKUP" -eq 0 ]]; then
  warn "Existing ${ENV_FILE} secrets were preserved (not overwritten from env.enc; bucket names updated)."
  warn "If BYOK_ENCRYPTION_KEY in ${ENV_FILE} differs from the backup's original .env, saved LLM API keys will fail to decrypt."
fi

# Step 7: Restore Caddy /data (with verified pre-restore archive) and check DB-referenced objects
if [[ "$SKIP_CADDY" -eq 0 && -f "${BACKUP_DIR}/caddy-data.tar.gz" ]]; then
  info "7/8 Restoring Caddy certificate & PKI data and checking DB object references..."
  existing_caddy_entry="$(compose_cmd run --rm -T --no-deps --entrypoint /bin/sh edge \
    -c 'ls -A /data 2>/dev/null | head -n 1' || true)"
  if [[ -n "$existing_caddy_entry" ]]; then
    mkdir -p "${WORK_DIR}/.restore-backup"
    chmod 700 "${WORK_DIR}/.restore-backup"
    CADDY_PRE_BACKUP="${WORK_DIR}/.restore-backup/caddy-pre-restore-${RESTORE_TS}.tar.gz"
    info "Backing up existing Caddy /data to ${CADDY_PRE_BACKUP} before clearing..."
    compose_cmd run --rm -T --no-deps --entrypoint /bin/sh edge \
      -c 'tar -czf - -C /data .' > "$CADDY_PRE_BACKUP"
    chmod 600 "$CADDY_PRE_BACKUP"
    if ! tar -tzf "$CADDY_PRE_BACKUP" >/dev/null 2>&1; then
      err "Pre-restore Caddy backup archive ${CADDY_PRE_BACKUP} failed integrity check; leaving Caddy /data untouched."
      exit 1
    fi
    ok "Backed up existing Caddy /data to ${CADDY_PRE_BACKUP}."
  fi
  if ! compose_cmd run --rm -T --no-deps --entrypoint /bin/sh edge \
      -c 'rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null || true; tar -xzf - -C /data' \
      < "${BACKUP_DIR}/caddy-data.tar.gz"; then
    err "Failed to extract Caddy certificate data from ${BACKUP_DIR}/caddy-data.tar.gz."
    if [[ -n "$CADDY_PRE_BACKUP" && -f "$CADDY_PRE_BACKUP" ]]; then
      warn "Restoring previous Caddy /data from ${CADDY_PRE_BACKUP}..."
      compose_cmd run --rm -T --no-deps --entrypoint /bin/sh edge \
        -c 'rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null || true; tar -xzf - -C /data' \
        < "$CADDY_PRE_BACKUP" >/dev/null 2>&1 || true
    fi
    exit 1
  fi
  ok "Caddy certificate data restored."
else
  info "7/8 Skipping Caddy certificate restore; checking DB object references..."
fi

# Report any DB-referenced objects missing in the new active MinIO bucket (warning only)
if [[ "$SKIP_MINIO" -eq 0 ]]; then
  DB_OBJ_REFS=""
  has_chat_uploads="$(compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -Atqc \
    "SELECT to_regclass('public.dalton_chat_file_uploads');" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ -n "$has_chat_uploads" ]]; then
    DB_OBJ_REFS="$(compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -Atqc "
      SELECT 'chat_upload' || E'\t' || upload_id || E'\t' || object_key
      FROM dalton_chat_file_uploads
      WHERE cleaned_at IS NULL
        AND state IN ('ready', 'completing', 'uploading')
        AND object_key IS NOT NULL AND object_key <> '';
    " 2>/dev/null || true)"
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

endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
bucket = _env("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "STORAGE_BUCKET_NAME", default="chat-uploads")
prefix = _env("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX", default="").strip("/")
access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID")
secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY")
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "STORAGE_REGION", default="us-east-1")
addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()

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
    print(f"[restore] WARN: {len(missing)}/{checked} DB-referenced object(s) missing from MinIO:", file=sys.stderr)
    for item in missing:
        print(f"[restore] WARN:   missing: {item}", file=sys.stderr)
else:
    print(f"[restore] OK: All {checked} DB-referenced object(s) present in MinIO bucket {bucket!r}.")
'
    printf '%s\n' "$DB_OBJ_REFS" | compose_cmd run --rm -T --no-deps minio-init \
      uv run --no-sync python -c "$MISSING_CHECK_PY" || warn "Could not complete DB-to-MinIO reference check."
  else
    ok "No active DB object references to verify."
  fi
fi

# Step 8: Start full stack and run unified health checks
info "8/8 Starting services and running health checks..."
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
  err "Inspect logs with: $(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" logs --tail=100 backend)"
  exit 1
fi
ok "Backend /health passed."

if ! compose_cmd ps --status running --services | grep -qx "edge"; then
  err "Edge container is not running after restore."
  exit 1
fi

# Determine actual published host port from the running edge container, falling back to committed .env.
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
      if curl -sf "${probe_curl_extra[@]}" --cacert "$tmp_ca" --resolve "${probe_url_host}:${PROBE_HTTPS_PORT}:${probe_connect_ip}" \
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
    if curl -sf "${probe_curl_extra[@]}" "${curl_ca_args[@]}" --resolve "${probe_url_host}:${PROBE_HTTPS_PORT}:${probe_connect_ip}" \
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

RESTORED_UP="$(compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -Atqc "SELECT version_num FROM alembic_version;" | tr -d '\r\n')"
RESTORED_DA="$(compose_cmd exec -T postgres psql -U "$CFG_DB_USER" -d "$CFG_DB_NAME" -Atqc "SELECT version_num FROM alembic_version_dalton;" | tr -d '\r\n')"
ok "Migrations verified at head (upstream=${RESTORED_UP}, dalton=${RESTORED_DA})."

# Deduplicate ORIGINAL_BUCKETS for display
DEDUP_ORIG_BUCKETS=()
if (( ${#ORIGINAL_BUCKETS[@]} > 0 )); then
  while IFS= read -r ob; do
    [[ -n "$ob" ]] && DEDUP_ORIG_BUCKETS+=("$ob")
  done < <(printf '%s\n' "${ORIGINAL_BUCKETS[@]}" | awk 'NF && !seen[$0]++')
fi

printf "\n== Restore Complete ==\n"
printf "  Restored from    : %s\n" "$BACKUP_DIR"
printf "  Database         : %s (upstream=%s, dalton=%s)\n" "$CFG_DB_NAME" "$RESTORED_UP" "$RESTORED_DA"
printf "  Active bucket    : %s\n" "$CFG_CHAT_BUCKET"
printf "  Edge TLS mode    : %s\n" "$CFG_TLS_MODE"
if (( ${#UPDATED_ENV_BUCKET_PAIRS[@]} > 0 )); then
  printf "  Bucket switches  :\n"
  for pair in "${UPDATED_ENV_BUCKET_PAIRS[@]}"; do
    printf "    - %s\n" "$pair"
  done
fi
if [[ -n "$ENV_PRE_BACKUP" && -f "$ENV_PRE_BACKUP" ]]; then
  printf "  Previous .env    : %s\n" "$ENV_PRE_BACKUP"
fi
if [[ -n "$PRE_RESTORE_DB" ]]; then
  printf "  Previous DB kept : %s\n" "$PRE_RESTORE_DB"
fi
if (( ${#DEDUP_ORIG_BUCKETS[@]} > 0 )); then
  printf "  Previous buckets : %s (untouched)\n" "${DEDUP_ORIG_BUCKETS[*]}"
fi
if [[ -n "$CADDY_PRE_BACKUP" && -f "$CADDY_PRE_BACKUP" ]]; then
  printf "  Old Caddy backup : %s\n" "$CADDY_PRE_BACKUP"
fi

if [[ -n "$PRE_RESTORE_DB" || ( -n "$ENV_PRE_BACKUP" && -f "$ENV_PRE_BACKUP" ) ]]; then
  printf "\n  To roll back to the pre-restore state (see STANDALONE-DEPLOY.md §6.2):\n"
  printf "    1. %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" stop backend crawler edge)"
  if [[ -n "$ENV_PRE_BACKUP" && -f "$ENV_PRE_BACKUP" ]]; then
    printf "    2. %s\n" "$(fmt_cmd cp -p "$ENV_PRE_BACKUP" "$ENV_FILE")"
  fi
  if [[ -n "$PRE_RESTORE_DB" ]]; then
    printf "    3. Swap '%s' back to '%s' in a single transaction, then restart:\n" "$PRE_RESTORE_DB" "$CFG_DB_NAME"
    printf "       %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" exec -T postgres psql -U "$CFG_DB_USER" -d template1 -v ON_ERROR_STOP=1 -c "BEGIN; ALTER DATABASE \"${CFG_DB_NAME}\" RENAME TO \"${db_prefix}_abandoned_${RESTORE_TS}\"; ALTER DATABASE \"${PRE_RESTORE_DB}\" RENAME TO \"${CFG_DB_NAME}\"; COMMIT;")"
  fi
  printf "    4. %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" up -d)"
fi

if [[ -n "$PRE_RESTORE_DB" || ${#DEDUP_ORIG_BUCKETS[@]} -gt 0 || ( -n "$ENV_PRE_BACKUP" && -f "$ENV_PRE_BACKUP" ) || ( -n "$CADDY_PRE_BACKUP" && -f "$CADDY_PRE_BACKUP" ) ]]; then
  printf "\n  After verifying data and uploads look right, clean up old artifacts:\n"
  if [[ -n "$PRE_RESTORE_DB" ]]; then
    printf "    %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" exec -T postgres psql -U "$CFG_DB_USER" -d template1 -c "DROP DATABASE \"${PRE_RESTORE_DB}\";")"
  fi
  for old_b in "${DEDUP_ORIG_BUCKETS[@]}"; do
    printf "    %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" exec -T backend uv run --no-sync python -c "import os,boto3; from botocore.config import Config; b='${old_b}'; c=boto3.client('s3', endpoint_url=os.getenv('DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL','http://minio:9000'), aws_access_key_id=os.getenv('DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID') or os.getenv('MINIO_ROOT_USER'), aws_secret_access_key=os.getenv('DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY') or os.getenv('MINIO_ROOT_PASSWORD'), region_name=os.getenv('DALTON_CHAT_UPLOAD_STORAGE_REGION','us-east-1'), config=Config(signature_version='s3v4', s3={'addressing_style':'path'})); [(_r := c.delete_objects(Bucket=b, Delete={'Objects':[{'Key':o['Key']} for o in p.get('Contents',[])], 'Quiet':False})) and (_r.get('Errors') and (_ for _ in ()).throw(RuntimeError(f'DeleteObjects failed: {_r[\"Errors\"]}'))) for p in c.get_paginator('list_objects_v2').paginate(Bucket=b) if p.get('Contents')]; c.delete_bucket(Bucket=b); print('Removed', b)")"
  done
  if [[ -n "$ENV_PRE_BACKUP" && -f "$ENV_PRE_BACKUP" ]]; then
    printf "    %s\n" "$(fmt_cmd rm -f "$ENV_PRE_BACKUP")"
  fi
  if [[ -n "$CADDY_PRE_BACKUP" && -f "$CADDY_PRE_BACKUP" ]]; then
    printf "    %s\n" "$(fmt_cmd rm -f "$CADDY_PRE_BACKUP")"
  fi
fi

printf "\n  Next steps:\n"
printf "  - Verify service status: %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" ps)"
printf "  - Open the web UI and confirm your workspaces, threads, and uploaded files are visible.\n"
