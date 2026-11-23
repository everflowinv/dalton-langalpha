#!/usr/bin/env bash
# Standalone Core formal restore utility.
#
# Restores a timestamped backup directory created by `scripts/standalone/backup.sh`
# onto a clean host (or replaces an existing database/MinIO dataset when
# `--replace-data` is passed; `--replace-env` authorizes overwriting `.env`):
#   1. Verifies backup integrity (`SHA256SUMS` + `pg_restore --list`).
#   2. Decrypts `env.enc` into `.env` (mode 0600, using KDF params from
#      `manifest.json`) when `--env-passphrase-file` is provided and `.env` is
#      absent (or `--replace-env` is set). Rebuilds `edge` when `.env` is restored.
#   3. Refuses external PostgreSQL (`DB_HOST != postgres`).
#   4. Inspects all non-system DB schemas and target MinIO buckets before mutating
#      anything; refuses non-empty targets unless `--replace-data` is given.
#   5. When replacing an existing database, restores and migrates into a staging
#      database `<db>_restore_<ts>` first, then stops app containers and swaps via
#      `ALTER DATABASE ... RENAME`, keeping `<db>_pre_restore_<ts>` intact.
#   6. Restores MinIO objects via chunked streaming upload with `ContentType`,
#      read-back SHA-256 & ContentType verification, and extra-object pruning when
#      `--replace-data` is set.
#   7. Restores Caddy certificate data (`/data` volume on `edge`), reports any
#      DB-referenced objects missing from MinIO (warning only), starts the full
#      stack, and runs end-to-end health checks (supporting `--ca-file`).
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

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/restore.sh --backup-dir <dir> [OPTIONS]

Options:
  --backup-dir <dir>          Backup directory created by backup.sh (required).
  --work-dir <dir>            Deployment directory containing compose files (default: repo root).
  --env-passphrase-file <f>   Decrypt env.enc from backup into <work-dir>/.env
                              (or set DALTON_BACKUP_PASSPHRASE_FILE).
  --compose-files <f1:f2>     Colon-separated Docker Compose files
                              (default: from .env COMPOSE_FILE / DALTON_TLS_MODE).
  --ca-file <path>            CA certificate PEM file used to verify HTTPS in custom/auto mode.
  --replace-data              Allow replacing an existing non-empty database (via staging DB +
                              atomic rename keeping <db>_pre_restore_<ts>) and reconciling
                              MinIO objects (deletes objects not in the backup manifest).
  --replace-env               Allow overwriting an existing <work-dir>/.env from env.enc.
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
      echo "ERROR: --force has been split into --replace-data (replace DB & MinIO data) and --replace-env (overwrite .env from env.enc)." >&2
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

read_env_var() {
  local key="$1" default_val="${2:-}"
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  local line val
  line="$(grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
  val="${line#*=}"
  if [[ "$val" == \"*\" && "$val" == *\" ]]; then
    val="${val#\"}"
    val="${val%\"}"
  elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
    val="${val#\'}"
    val="${val%\'}"
  fi
  if [[ -n "$val" ]]; then
    printf '%s' "$val"
  else
    printf '%s' "$default_val"
  fi
}

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

# Read PBKDF2 encryption parameters from manifest.json (with safe defaults for v1 backups)
ENV_CIPHER="aes-256-cbc"
ENV_KDF="pbkdf2"
ENV_KDF_DIGEST="sha256"
ENV_KDF_ITER="600000"
if [[ -f "${BACKUP_DIR}/manifest.json" ]]; then
  read -r ENV_CIPHER ENV_KDF ENV_KDF_DIGEST ENV_KDF_ITER < <(python3 - "${BACKUP_DIR}/manifest.json" <<'PY'
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
print(cipher, kdf, digest, iters)
PY
  )
fi

# Step 2: Restore or verify .env
ENV_FILE="${WORK_DIR}/.env"
ENV_REPLACED_FROM_BACKUP=0
if [[ -f "${BACKUP_DIR}/env.enc" && -n "$ENV_PASSPHRASE_FILE" ]]; then
  if [[ ! -r "$ENV_PASSPHRASE_FILE" || ! -s "$ENV_PASSPHRASE_FILE" ]]; then
    err "Passphrase file is missing, unreadable, or empty: ${ENV_PASSPHRASE_FILE}"
    exit 1
  fi
  if [[ ! -f "$ENV_FILE" || "$REPLACE_ENV" -eq 1 ]]; then
    info "2/8 Decrypting env.enc into ${ENV_FILE} (mode 0600; ${ENV_CIPHER}/${ENV_KDF}/${ENV_KDF_DIGEST}/${ENV_KDF_ITER})..."
    tmp_env="$(mktemp "${WORK_DIR}/.env.restore.XXXXXX")"
    chmod 600 "$tmp_env"
    if ! openssl enc -d "-${ENV_CIPHER}" -pbkdf2 -md "${ENV_KDF_DIGEST}" -iter "${ENV_KDF_ITER}" \
        -in "${BACKUP_DIR}/env.enc" \
        -out "$tmp_env" \
        -pass "file:${ENV_PASSPHRASE_FILE}"; then
      rm -f "$tmp_env"
      err "Failed to decrypt ${BACKUP_DIR}/env.enc (check passphrase file)."
      exit 1
    fi
    chmod 600 "$tmp_env"
    mv "$tmp_env" "$ENV_FILE"
    ENV_REPLACED_FROM_BACKUP=1
    ok "Decrypted .env into ${ENV_FILE}."
  else
    info "2/8 Keeping existing ${ENV_FILE} (pass --replace-env to overwrite from env.enc)."
  fi
elif [[ -f "$ENV_FILE" ]]; then
  chmod 600 "$ENV_FILE"
  info "2/8 Using existing ${ENV_FILE}."
else
  err "Missing ${ENV_FILE}. Copy your saved .env into ${WORK_DIR}, pass --env-passphrase-file (if env.enc is in the backup), or run scripts/standalone/init.sh first."
  exit 1
fi

if [[ "$ENV_REPLACED_FROM_BACKUP" -eq 0 ]]; then
  warn "Existing ${ENV_FILE} was NOT overwritten from this backup's env.enc."
  warn "If BYOK_ENCRYPTION_KEY in ${ENV_FILE} differs from the backup's original .env, saved LLM API keys will fail to decrypt."
fi

# External PostgreSQL guard: refuse non-Compose databases before touching containers.
DB_HOST="$(read_env_var "DB_HOST" "postgres")"
if [[ "$DB_HOST" != "postgres" ]]; then
  err "External PostgreSQL detected (DB_HOST='${DB_HOST}'; expected Compose-managed 'postgres')."
  err "restore.sh only manages the Compose-managed PostgreSQL container. For external databases, restore directly with pg_restore."
  exit 1
fi

DB_USER="$(read_env_var "DB_USER" "postgres")"
DB_NAME="$(read_env_var "DB_NAME" "postgres")"
DOMAIN="$(read_env_var "DOMAIN" "localhost")"
TLS_MODE="$(read_env_var "DALTON_TLS_MODE" "loopback")"
EDGE_PORT="$(read_env_var "DALTON_EDGE_PORT" "8088")"
EDGE_HTTPS_PORT="$(read_env_var "DALTON_EDGE_HTTPS_PORT" "18443")"

if ! [[ "$DB_USER" =~ ^[a-zA-Z0-9_]+$ ]] || ! [[ "$DB_NAME" =~ ^[a-zA-Z0-9_]+$ ]]; then
  err "DB_USER and DB_NAME must contain only ASCII letters, digits, and underscores."
  exit 1
fi
if [[ "$DB_NAME" == "template0" || "$DB_NAME" == "template1" ]]; then
  err "Refusing to restore into PostgreSQL template database '${DB_NAME}'."
  exit 1
fi

if [[ -z "$COMPOSE_FILES_RAW" ]]; then
  env_compose_file="$(read_env_var "COMPOSE_FILE" "")"
  if [[ -n "$env_compose_file" ]]; then
    COMPOSE_FILES_RAW="$env_compose_file"
  elif [[ "$TLS_MODE" != "loopback" && -f "${WORK_DIR}/deploy/standalone/docker-compose.tls.yml" ]]; then
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

compose_cmd() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

# Rebuild edge after restoring .env so build-time VITE_* branding/auth args match.
if [[ "$ENV_REPLACED_FROM_BACKUP" -eq 1 && "$SKIP_EDGE_BUILD" -eq 0 ]]; then
  info "Rebuilding edge image to match restored .env..."
  compose_cmd build edge
  ok "Rebuilt edge image."
fi

MINIO_MANIFEST_TMP=""
STAGE_DB=""
STAGE_DB_CREATED=0
PRE_RESTORE_DB=""
DB_SWAPPED=0

cleanup() {
  if [[ -n "$MINIO_MANIFEST_TMP" && -f "$MINIO_MANIFEST_TMP" ]]; then
    rm -f "$MINIO_MANIFEST_TMP"
  fi
  if [[ "$STAGE_DB_CREATED" -eq 1 && "$DB_SWAPPED" -eq 0 && -n "$STAGE_DB" ]]; then
    warn "Cleaning up unfinished staging database '${STAGE_DB}'..."
    compose_cmd exec -T postgres psql -U "$DB_USER" -d template1 -v ON_ERROR_STOP=1 \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${STAGE_DB}' AND pid <> pg_backend_pid();" \
      -c "DROP DATABASE IF EXISTS \"${STAGE_DB}\";" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# Step 3: Start infrastructure & inspect DB (all non-system schemas) and MinIO BEFORE mutating anything
info "3/8 Starting infrastructure services (postgres, redis, minio) and inspecting target state..."
compose_cmd up -d postgres redis minio

info "Waiting for PostgreSQL to become ready..."
pg_ready=0
for _ in $(seq 1 60); do
  if compose_cmd exec -T postgres pg_isready -U "$DB_USER" -d "$DB_NAME" -q >/dev/null 2>&1; then
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

# Check all non-system schemas for existing relations (tables, views, sequences, etc.)
DB_OBJECT_COUNT="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc \
  "SELECT COUNT(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f');" \
  | tr -d '[:space:]')"

if [[ -z "$DB_OBJECT_COUNT" || ! "$DB_OBJECT_COUNT" =~ ^[0-9]+$ ]]; then
  err "Could not inspect target database '${DB_NAME}'."
  exit 1
fi

if (( DB_OBJECT_COUNT > 0 )) && [[ "$REPLACE_DATA" -ne 1 ]]; then
  err "Target database '${DB_NAME}' is NOT empty (${DB_OBJECT_COUNT} object(s) across non-system schemas)."
  err "Refusing to overwrite existing data. Pass --replace-data to stage, migrate, and swap '${DB_NAME}' safely."
  exit 1
fi

# Pre-extract MinIO manifest and inspect target MinIO buckets before mutating DB or stopping app
if [[ "$SKIP_MINIO" -eq 0 && -f "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
  MINIO_MANIFEST_TMP="$(mktemp)"
  chmod 600 "$MINIO_MANIFEST_TMP"
  if ! tar -xOzf "${BACKUP_DIR}/minio-objects.tar.gz" _minio_manifest.json > "$MINIO_MANIFEST_TMP"; then
    err "Failed to extract _minio_manifest.json from ${BACKUP_DIR}/minio-objects.tar.gz."
    exit 1
  fi

  MINIO_PREFLIGHT_PY='
from __future__ import annotations
import json, os, sys
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default

manifest = json.loads(sys.stdin.read() or "{}")
endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
default_bucket = _env("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "STORAGE_BUCKET_NAME", default="chat-uploads")
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

target_buckets = set(manifest.get("buckets") or [])
if default_bucket:
    target_buckets.add(default_bucket)

existing_buckets = {b["Name"] for b in client.list_buckets().get("Buckets", []) if b.get("Name")}
non_empty: list[tuple[str, int]] = []
for bucket in sorted(target_buckets & existing_buckets):
    count = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        count += len(page.get("Contents", []))
    if count > 0:
        non_empty.append((bucket, count))

if non_empty:
    summary = ", ".join(f"{b}({c})" for b, c in non_empty)
    print(summary)
    sys.exit(10)
print("EMPTY")
'
  set +e
  MINIO_PREFLIGHT_OUT="$(compose_cmd run --rm -T --no-deps minio-init uv run --no-sync python -c "$MINIO_PREFLIGHT_PY" < "$MINIO_MANIFEST_TMP" 2>&1)"
  MINIO_PREFLIGHT_RC=$?
  set -e
  if [[ "$MINIO_PREFLIGHT_RC" -eq 10 ]]; then
    if [[ "$REPLACE_DATA" -ne 1 ]]; then
      err "Target MinIO storage is NOT empty (${MINIO_PREFLIGHT_OUT})."
      err "Refusing to overwrite existing objects. Pass --replace-data to replace database and reconcile MinIO objects."
      exit 1
    fi
    info "Target MinIO buckets contain existing objects (${MINIO_PREFLIGHT_OUT}); will reconcile because --replace-data was specified."
  elif [[ "$MINIO_PREFLIGHT_RC" -ne 0 ]]; then
    err "Failed to inspect target MinIO storage: ${MINIO_PREFLIGHT_OUT}"
    exit 1
  else
    ok "Confirmed target MinIO buckets are empty."
  fi
fi

# Step 4: Restore PostgreSQL database (safe staging DB + migrate + swap when replacing data)
stop_app_containers() {
  local running_apps
  running_apps="$(compose_cmd ps --status running --services 2>/dev/null | grep -E '^(backend|crawler|edge)$' || true)"
  if [[ -n "$running_apps" ]]; then
    info "Stopping application containers (backend, crawler, edge)..."
    compose_cmd stop backend crawler edge
  fi
}

if (( DB_OBJECT_COUNT > 0 )); then
  RESTORE_TS="$(date -u +%Y%m%d%H%M%S)"
  # Keep PostgreSQL identifier length <= 63 characters
  db_prefix="${DB_NAME:0:32}"
  STAGE_DB="${db_prefix}_restore_${RESTORE_TS}"
  PRE_RESTORE_DB="${db_prefix}_pre_restore_${RESTORE_TS}"

  info "4/8 Restoring into staging database '${STAGE_DB}' and running migrations before switching..."
  compose_cmd exec -T postgres psql -U "$DB_USER" -d template1 -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS \"${STAGE_DB}\";" \
    -c "CREATE DATABASE \"${STAGE_DB}\" OWNER \"${DB_USER}\";"
  STAGE_DB_CREATED=1

  compose_cmd exec -T postgres pg_restore \
    -U "$DB_USER" \
    -d "$STAGE_DB" \
    --no-owner \
    --no-acl \
    --exit-on-error < "${BACKUP_DIR}/postgres.dump"
  ok "Staging database '${STAGE_DB}' restored."

  info "Running migrations against staging database '${STAGE_DB}'..."
  compose_cmd run --rm -e "DB_NAME=${STAGE_DB}" migrate
  ok "Staging database '${STAGE_DB}' migrated to current heads."

  # Only stop live app containers and swap databases AFTER restore & migrations succeeded.
  stop_app_containers
  info "Swapping '${DB_NAME}' -> '${PRE_RESTORE_DB}' and '${STAGE_DB}' -> '${DB_NAME}'..."
  compose_cmd exec -T postgres psql -U "$DB_USER" -d template1 -v ON_ERROR_STOP=1 \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('${DB_NAME}', '${STAGE_DB}') AND pid <> pg_backend_pid();" \
    -c "ALTER DATABASE \"${DB_NAME}\" RENAME TO \"${PRE_RESTORE_DB}\";" \
    -c "ALTER DATABASE \"${STAGE_DB}\" RENAME TO \"${DB_NAME}\";"
  DB_SWAPPED=1
  STAGE_DB_CREATED=0
  ok "Database switched to restored data (previous database preserved as '${PRE_RESTORE_DB}')."
else
  stop_app_containers
  info "4/8 Target database '${DB_NAME}' is empty; restoring PostgreSQL database..."
  compose_cmd exec -T postgres pg_restore \
    -U "$DB_USER" \
    -d "$DB_NAME" \
    --no-owner \
    --no-acl \
    --exit-on-error < "${BACKUP_DIR}/postgres.dump"
  ok "PostgreSQL database restored."
  info "Running migrations on '${DB_NAME}'..."
  compose_cmd run --rm migrate
  ok "Migrations succeeded."
fi

# Step 5: Restore MinIO objects (streaming chunked upload + ContentType + read-back SHA-256 verify + reconciliation)
if [[ "$SKIP_MINIO" -eq 0 && -f "${BACKUP_DIR}/minio-objects.tar.gz" ]]; then
  info "5/8 Restoring MinIO buckets and objects (streaming with read-back verification)..."
  compose_cmd run --rm minio-init
  MINIO_RESTORE_PY='
from __future__ import annotations
import hashlib, json, os, sys, tarfile
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CHUNK_BYTES = 1024 * 1024        # 1 MiB read/verify chunks
PART_BYTES = 8 * 1024 * 1024     # 8 MiB multipart upload parts (>= S3 5 MiB min)

def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default

replace_data = os.getenv("DALTON_RESTORE_REPLACE_DATA", "0") == "1"
endpoint = _env("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL", "STORAGE_ENDPOINT_URL", default="http://minio:9000").rstrip("/")
default_bucket = _env("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME", "STORAGE_BUCKET_NAME", default="chat-uploads")
access_key = _env("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID", "MINIO_ROOT_USER", "STORAGE_ACCESS_KEY_ID")
secret_key = _env("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY", "MINIO_ROOT_PASSWORD", "STORAGE_SECRET_ACCESS_KEY")
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "STORAGE_REGION", default="us-east-1")
addressing_style = _env("DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE", "STORAGE_ADDRESSING_STYLE", default="path").lower()

if not access_key or not secret_key:
    print("ERROR: MinIO credentials missing in container environment.", file=sys.stderr)
    sys.exit(1)

# Read length-prefixed pre-extracted manifest before the gzipped tar stream
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

manifest_by_pair: dict[tuple[str, str], dict[str, object]] = {}
for item in manifest.get("objects", []):
    manifest_by_pair[(item["bucket"], item["key"])] = item

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

ensured_buckets: set[str] = set()
def ensure_bucket(bucket: str) -> None:
    if bucket in ensured_buckets:
        return
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in ("404", "NoSuchBucket", "NotFound") or status == 404:
            kwargs = {"Bucket": bucket}
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
    ensured_buckets.add(bucket)

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

for bucket in manifest.get("buckets", []):
    ensure_bucket(bucket)
if default_bucket:
    ensure_bucket(default_bucket)

uploaded_pairs: set[tuple[str, str]] = set()

with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as tf:
    for member in tf:
        if not member.isfile():
            continue
        if member.name == "_minio_manifest.json":
            # Drain member stream without buffering
            fp = tf.extractfile(member)
            if fp is not None:
                while fp.read(CHUNK_BYTES):
                    pass
            continue
        if not member.name.startswith("objects/"):
            continue
        rel = member.name[len("objects/"):]
        bucket, sep, key = rel.partition("/")
        if not sep or not bucket or not key:
            continue
        fp = tf.extractfile(member)
        if fp is None:
            continue
        ensure_bucket(bucket)
        meta = manifest_by_pair.get((bucket, key)) or {}
        content_type = (
            str(meta.get("content_type") or "").strip()
            or str((member.pax_headers or {}).get("DALTON.content_type") or "").strip()
            or "application/octet-stream"
        )
        upload_streaming(bucket, key, fp, member.size, content_type)
        uploaded_pairs.add((bucket, key))

expected_objects = manifest.get("objects", [])
if len(uploaded_pairs) != len(expected_objects):
    print(f"ERROR: Uploaded {len(uploaded_pairs)} objects, expected {len(expected_objects)}", file=sys.stderr)
    sys.exit(1)

# Read-back verification from target MinIO (streaming SHA-256 + size + ContentType)
for item in expected_objects:
    bucket = item["bucket"]
    key = item["key"]
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

# Reconcile extra objects when --replace-data is enabled
pruned = 0
if replace_data:
    expected_pairs = set(manifest_by_pair.keys())
    for bucket in sorted(ensured_buckets):
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket):
            to_delete = [
                {"Key": entry["Key"]}
                for entry in page.get("Contents", [])
                if (bucket, entry["Key"]) not in expected_pairs
            ]
            if to_delete:
                client.delete_objects(Bucket=bucket, Delete={"Objects": to_delete})
                pruned += len(to_delete)

print(
    f"[restore] MinIO restored and read-back verified {len(uploaded_pairs)} object(s) "
    f"across {len(ensured_buckets)} bucket(s) (reconciled/deleted {pruned} extra object(s))."
)
'
  manifest_size="$(wc -c < "$MINIO_MANIFEST_TMP" | tr -d '[:space:]')"
  {
    printf '%s\n' "$manifest_size"
    cat "$MINIO_MANIFEST_TMP"
    cat "${BACKUP_DIR}/minio-objects.tar.gz"
  } | compose_cmd run --rm -T --no-deps -e "DALTON_RESTORE_REPLACE_DATA=${REPLACE_DATA}" \
        minio-init uv run --no-sync python -c "$MINIO_RESTORE_PY"
  ok "MinIO objects restored, ContentType preserved, and read-back SHA-256 verified."
else
  info "5/8 Skipping MinIO restore."
fi

# Step 6: Restore Caddy certificate data
if [[ "$SKIP_CADDY" -eq 0 && -f "${BACKUP_DIR}/caddy-data.tar.gz" ]]; then
  info "6/8 Restoring Caddy certificate & PKI data..."
  compose_cmd run --rm -T --no-deps --entrypoint /bin/sh edge \
    -c 'rm -rf /data/* /data/.[!.]* /data/..?* 2>/dev/null || true; tar -xzf - -C /data' \
    < "${BACKUP_DIR}/caddy-data.tar.gz"
  ok "Caddy certificate data restored."
else
  info "6/8 Skipping Caddy certificate restore."
fi

# Step 7: Report any DB-referenced objects missing in MinIO (warning only; never fails restore)
if [[ "$SKIP_MINIO" -eq 0 ]]; then
  info "7/8 Checking DB-referenced objects against MinIO..."
  DB_OBJ_REFS=""
  has_chat_uploads="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc \
    "SELECT to_regclass('public.dalton_chat_file_uploads');" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ -n "$has_chat_uploads" ]]; then
    DB_OBJ_REFS="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "
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
    print(f"[restore] OK: All {checked} DB-referenced object(s) present in MinIO.")
'
    printf '%s\n' "$DB_OBJ_REFS" | compose_cmd run --rm -T --no-deps minio-init \
      uv run --no-sync python -c "$MISSING_CHECK_PY" || warn "Could not complete DB-to-MinIO reference check."
  else
    ok "No active DB object references to verify."
  fi
else
  info "7/8 Skipping DB-to-MinIO reference check (--skip-minio)."
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

# Unified edge gateway liveness probe (same success criteria as upgrade.sh)
edge_ok=0
if [[ "$TLS_MODE" == "loopback" ]]; then
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${EDGE_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
      edge_ok=1
      break
    fi
    sleep 2
  done
elif [[ "$TLS_MODE" == "internal" ]]; then
  tmp_ca="$(mktemp)"
  for _ in $(seq 1 30); do
    if compose_cmd exec -T edge cat /data/caddy/pki/authorities/local/root.crt > "$tmp_ca" 2>/dev/null && [[ -s "$tmp_ca" ]]; then
      if curl -sf --cacert "$tmp_ca" --resolve "${DOMAIN}:${EDGE_HTTPS_PORT}:127.0.0.1" \
          "https://${DOMAIN}:${EDGE_HTTPS_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
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
    if curl -sf "${curl_ca_args[@]}" --resolve "${DOMAIN}:${EDGE_HTTPS_PORT}:127.0.0.1" \
        "https://${DOMAIN}:${EDGE_HTTPS_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
      edge_ok=1
      break
    fi
    sleep 2
  done
fi

if [[ "$edge_ok" -ne 1 ]]; then
  err "Edge gateway failed post-restore health probe (mode=${TLS_MODE})."
  if [[ "$TLS_MODE" == "custom" && -z "$CA_FILE" ]]; then
    err "For certificates signed by an enterprise/private CA, pass --ca-file <ca.pem>."
  elif [[ "$TLS_MODE" == "auto" ]]; then
    err "For public ACME mode, verify DNS resolution and inbound TCP ports 80/443."
  fi
  exit 1
fi
ok "Edge gateway probe passed (mode=${TLS_MODE})."

RESTORED_UP="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version;" | tr -d '\r\n')"
RESTORED_DA="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version_dalton;" | tr -d '\r\n')"
ok "Migrations verified at head (upstream=${RESTORED_UP}, dalton=${RESTORED_DA})."

printf "\n== Restore Complete ==\n"
printf "  Restored from    : %s\n" "$BACKUP_DIR"
printf "  Database         : %s (upstream=%s, dalton=%s)\n" "$DB_NAME" "$RESTORED_UP" "$RESTORED_DA"
printf "  Edge TLS mode    : %s\n" "$TLS_MODE"
if [[ -n "$PRE_RESTORE_DB" ]]; then
  printf "  Previous DB kept : %s\n" "$PRE_RESTORE_DB"
  printf "\n  After verifying data looks right, remove the preserved pre-restore database with:\n"
  printf "    %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" exec -T postgres psql -U "$DB_USER" -d template1 -c "DROP DATABASE \"${PRE_RESTORE_DB}\";")"
fi
printf "\n  Next steps:\n"
printf "  - Verify service status: %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" ps)"
printf "  - Open the web UI and confirm your workspaces and threads are visible.\n"
