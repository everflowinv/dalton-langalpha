#!/usr/bin/env bash
# Standalone Core zero-downtime backup utility.
#
# Backs up while the stack remains online (Compose-managed PostgreSQL only):
#   1. PostgreSQL database (upstream + Dalton migration chains live in the same DB)
#      using custom format (`pg_dump -Fc --no-owner --no-acl`) and verifies with
#      `pg_restore --list`.
#   2. MinIO objects across all buckets via chunked streaming tar.gz + incremental
#      per-object SHA-256 & ContentType manifest (fails if buckets cannot be listed).
#   3. Caddy certificate & PKI data (`/data` volume on `edge`).
#   4. `.env` secrets: encrypted as `env.enc` (AES-256-CBC + PBKDF2, parameters
#      recorded in `manifest.json`) ONLY when `--env-passphrase-file` (or
#      `DALTON_BACKUP_PASSPHRASE_FILE`) is supplied; plaintext `.env` is NEVER
#      placed in the backup directory.
#
# Publishes into a uniquely named timestamped directory under a lock, writes
# `manifest.json` + `SHA256SUMS`, and prunes older verified backups belonging to
# this instance according to `--retain`.
#
# Usage:
#   bash scripts/standalone/backup.sh
#   bash scripts/standalone/backup.sh --output-dir /var/backups/dalton --retain 14
#   bash scripts/standalone/backup.sh --env-passphrase-file /etc/dalton/backup.pass
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUTPUT_DIR=""
RETAIN=7
RETAIN_MAX=3650
ENV_PASSPHRASE_FILE="${DALTON_BACKUP_PASSPHRASE_FILE:-}"
COMPOSE_FILES_RAW=""
COMPOSE_FILES_EXPLICIT=0
VERIFY=1

ENV_CIPHER="aes-256-cbc"
ENV_KDF="pbkdf2"
ENV_KDF_DIGEST="sha256"
ENV_KDF_ITER=600000

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/backup.sh [OPTIONS]

Options:
  --work-dir <dir>            Deployment directory containing .env and compose files
                              (default: repository / release root).
  --output-dir <dir>          Parent directory for timestamped backup folders
                              (default: <work-dir>/backups).
  --retain <N>                Number of most recent backups to keep (default: 7;
                              0 = keep all; max: 3650).
  --env-passphrase-file <f>   Encrypt .env into env.enc (AES-256-CBC + PBKDF2) using the
                              passphrase stored in file <f> (or set DALTON_BACKUP_PASSPHRASE_FILE).
                              Without this option, .env is excluded from the backup directory.
  --compose-files <f1:f2>     Colon-separated Docker Compose files
                              (default: from .env COMPOSE_FILE / DALTON_TLS_MODE).
  --no-verify                 Skip post-backup verification checks (not recommended).
  -h, --help                  Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-dir)
      WORK_DIR="$2"
      shift 2
      ;;
    --work-dir=*)
      WORK_DIR="${1#--work-dir=}"
      shift
      ;;
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --output-dir=*)
      OUTPUT_DIR="${1#--output-dir=}"
      shift
      ;;
    --retain)
      RETAIN="$2"
      shift 2
      ;;
    --retain=*)
      RETAIN="${1#--retain=}"
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
      COMPOSE_FILES_EXPLICIT=1
      shift 2
      ;;
    --compose-files=*)
      COMPOSE_FILES_RAW="${1#--compose-files=}"
      COMPOSE_FILES_EXPLICIT=1
      shift
      ;;
    --no-verify)
      VERIFY=0
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

info() { printf "[backup] %s\n" "$1"; }
ok()   { printf "[backup] OK: %s\n" "$1"; }
warn() { printf "[backup] WARN: %s\n" "$1" >&2; }
err()  { printf "[backup] ERROR: %s\n" "$1" >&2; }

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

if ! [[ "$RETAIN" =~ ^[0-9]+$ ]]; then
  err "--retain must be a non-negative integer (got '${RETAIN}')."
  exit 2
fi
# Check decimal string bounds BEFORE Bash 64-bit arithmetic conversion to prevent overflow wrap.
RETAIN_STRIPPED="${RETAIN#"${RETAIN%%[!0]*}"}"
RETAIN_STRIPPED="${RETAIN_STRIPPED:-0}"
if (( ${#RETAIN_STRIPPED} > ${#RETAIN_MAX} )); then
  err "--retain exceeds maximum allowed value of ${RETAIN_MAX} (got '${RETAIN}')."
  exit 2
fi
RETAIN=$((10#$RETAIN_STRIPPED))
if (( RETAIN > RETAIN_MAX )); then
  err "--retain exceeds maximum allowed value of ${RETAIN_MAX} (got ${RETAIN})."
  exit 2
fi

WORK_DIR="$(cd "$WORK_DIR" && pwd)"
ENV_FILE="${WORK_DIR}/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  err "Missing ${ENV_FILE}. Run scripts/standalone/init.sh first or pass --work-dir."
  exit 1
fi

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

# External PostgreSQL guard: strictly read from .env (never shell environment).
DB_HOST="$(read_env_file_var "$ENV_FILE" "DB_HOST" "postgres")"
if [[ "$DB_HOST" != "postgres" ]]; then
  err "External PostgreSQL detected (DB_HOST='${DB_HOST}'; expected Compose-managed 'postgres')."
  err "backup.sh only backs up the Compose-managed PostgreSQL container. For external databases, run pg_dump directly against your database host."
  exit 1
fi

# Single object storage guard (v1): resolve effective chat_upload, ASR, and general
# storage using the application's actual config rules (including agent_config.yaml).
if ! python3 "${SCRIPT_DIR}/storage_config.py" check-env \
    --env-file "$ENV_FILE" \
    --work-dir "$WORK_DIR" \
    --caller backup; then
  exit 1
fi

DB_USER="$(read_env_file_var "$ENV_FILE" "DB_USER" "postgres")"
DB_NAME="$(read_env_file_var "$ENV_FILE" "DB_NAME" "postgres")"
PROJECT_NAME="$(read_env_file_var "$ENV_FILE" "COMPOSE_PROJECT_NAME" "")"
if [[ -z "$PROJECT_NAME" ]]; then
  PROJECT_NAME="$(sanitize_project_name "$(basename "$WORK_DIR")")"
fi
DOMAIN="$(read_env_file_var "$ENV_FILE" "DOMAIN" "localhost")"
TLS_MODE="$(read_env_file_var "$ENV_FILE" "DALTON_TLS_MODE" "loopback")"

if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="${WORK_DIR}/backups"
fi
if [[ ! -d "$OUTPUT_DIR" ]]; then
  mkdir -p "$OUTPUT_DIR"
  chmod 700 "$OUTPUT_DIR"
fi
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

if [[ -z "$COMPOSE_FILES_RAW" ]]; then
  env_compose_file="$(read_env_file_var "$ENV_FILE" "COMPOSE_FILE" "")"
  if [[ -n "$env_compose_file" ]]; then
    COMPOSE_FILES_RAW="$env_compose_file"
  elif [[ "$TLS_MODE" != "loopback" && "$DOMAIN" == *:* && -f "${WORK_DIR}/deploy/standalone/docker-compose.tls.ipv6.yml" ]]; then
    COMPOSE_FILES_RAW="${WORK_DIR}/docker-compose.yml:${WORK_DIR}/docker-compose.core.yml:${WORK_DIR}/deploy/standalone/docker-compose.tls.ipv6.yml"
  elif [[ "$TLS_MODE" != "loopback" && -f "${WORK_DIR}/deploy/standalone/docker-compose.tls.yml" ]]; then
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

# Unset .env keys, compose-interpolated keys, and COMPOSE_* variables so shell
# exports never shadow .env or shift project/DB selection.
ENV_UNSET_ARGS=()
while IFS= read -r env_key; do
  [[ -n "$env_key" ]] && ENV_UNSET_ARGS+=(-u "$env_key")
done < <(python3 - "$ENV_FILE" "${RESOLVED_COMPOSE_FILES[@]}" <<'PY'
import re, sys

env_path = sys.argv[1]
compose_files = sys.argv[2:]
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
with open(env_path, encoding="utf-8") as fh:
    for raw in fh:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = line_re.match(raw)
        if not m:
            continue
        key = m.group(1)
        if key != "PATH" and key not in seen:
            seen.add(key)
            print(key)
for cf in compose_files:
    with open(cf, encoding="utf-8") as fh:
        text = fh.read()
    for m in interp_re.finditer(text):
        key = m.group(1)
        if key != "PATH" and key not in seen:
            seen.add(key)
            print(key)
PY
)

compose_cmd() {
  env "${ENV_UNSET_ARGS[@]}" docker compose -p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" "$@"
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

if [[ -n "$ENV_PASSPHRASE_FILE" ]]; then
  if [[ ! -r "$ENV_PASSPHRASE_FILE" || ! -s "$ENV_PASSPHRASE_FILE" ]]; then
    err "Passphrase file is missing, unreadable, or empty: ${ENV_PASSPHRASE_FILE}"
    exit 1
  fi
  if ! command -v openssl >/dev/null 2>&1; then
    err "openssl is required to encrypt .env."
    exit 1
  fi
fi

TIMESTAMP="$(date -u +%Y%m%d-%H%M%SZ)"
RAND_SUFFIX="$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
BACKUP_NAME="dalton-backup-${TIMESTAMP}-${RAND_SUFFIX}"
FINAL_DIR="${OUTPUT_DIR}/${BACKUP_NAME}"
STAGE_DIR="$(mktemp -d "${OUTPUT_DIR}/.staging-${BACKUP_NAME}.XXXXXX")"
chmod 700 "$STAGE_DIR"

LOCK_DIR="${OUTPUT_DIR}/.dalton-backup.lock"
LOCK_HELD=0

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

acquire_publish_lock() {
  local waited=0
  local missing_pid_waited=0
  while ! mkdir "$LOCK_DIR" 2>/dev/null; do
    local pid_file="${LOCK_DIR}/pid"
    if [[ -f "$pid_file" ]]; then
      local lock_pid
      lock_pid="$(tr -d '[:space:]' < "$pid_file" 2>/dev/null || true)"
      if [[ -n "$lock_pid" && "$lock_pid" =~ ^[0-9]+$ ]]; then
        if ! pid_is_alive "$lock_pid"; then
          warn "Removing stale backup lock ${LOCK_DIR} left by dead PID ${lock_pid}."
          rm -f "$pid_file" 2>/dev/null || true
          rmdir "$LOCK_DIR" 2>/dev/null || true
          continue
        fi
        missing_pid_waited=0
      fi
    else
      missing_pid_waited=$(( missing_pid_waited + 1 ))
      if (( missing_pid_waited >= 5 )); then
        warn "Removing stale backup lock ${LOCK_DIR} (missing PID file)."
        rmdir "$LOCK_DIR" 2>/dev/null || true
        missing_pid_waited=0
        continue
      fi
    fi
    if (( waited >= 60 )); then
      err "Timed out waiting for backup publish lock: ${LOCK_DIR}"
      exit 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  printf '%s\n' "$$" > "${LOCK_DIR}/pid"
  LOCK_HELD=1
}

release_publish_lock() {
  if [[ "$LOCK_HELD" -eq 1 && -d "$LOCK_DIR" ]]; then
    local lock_pid=""
    if [[ -f "${LOCK_DIR}/pid" ]]; then
      lock_pid="$(tr -d '[:space:]' < "${LOCK_DIR}/pid" 2>/dev/null || true)"
    fi
    if [[ -z "$lock_pid" || "$lock_pid" == "$$" ]]; then
      rm -f "${LOCK_DIR}/pid" 2>/dev/null || true
      rmdir "$LOCK_DIR" 2>/dev/null || true
    fi
    LOCK_HELD=0
  fi
}

cleanup() {
  release_publish_lock
  if [[ -d "$STAGE_DIR" ]]; then
    rm -rf "$STAGE_DIR"
  fi
}
trap cleanup EXIT

info "Starting zero-downtime backup into ${FINAL_DIR}..."

# 1. PostgreSQL hot backup (custom format, single database holding both Alembic chains)
info "1/4 Backing up PostgreSQL database '${DB_NAME}'..."
compose_cmd exec -T postgres pg_dump \
  -U "$DB_USER" \
  -d "$DB_NAME" \
  -Fc \
  --no-owner \
  --no-acl > "${STAGE_DIR}/postgres.dump"

if [[ ! -s "${STAGE_DIR}/postgres.dump" ]]; then
  err "PostgreSQL dump is empty."
  exit 1
fi

PG_DUMP_BYTES="$(wc -c < "${STAGE_DIR}/postgres.dump" | tr -d ' ')"
ALEMBIC_UPSTREAM="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version;" 2>/dev/null | tr -d '\r\n' || true)"
ALEMBIC_DALTON="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version_dalton;" 2>/dev/null | tr -d '\r\n' || true)"

PG_TOC=""
PG_TOC_ENTRIES=0
if [[ "$VERIFY" -eq 1 ]]; then
  PG_TOC="$(compose_cmd exec -T postgres pg_restore --list < "${STAGE_DIR}/postgres.dump")"
  PG_TOC_ENTRIES="$(printf '%s\n' "$PG_TOC" | grep -vcE '^[[:space:]]*(;|$)' || true)"
  if (( PG_TOC_ENTRIES == 0 )); then
    err "pg_restore --list returned 0 TOC entries."
    exit 1
  fi
  if ! printf '%s\n' "$PG_TOC" | grep -q 'alembic_version_dalton'; then
    warn "TOC does not contain alembic_version_dalton (database may not be migrated yet)."
  fi
  ok "PostgreSQL dump verified (${PG_DUMP_BYTES} bytes, ${PG_TOC_ENTRIES} TOC entries; upstream=${ALEMBIC_UPSTREAM:-unknown}, dalton=${ALEMBIC_DALTON:-unknown})."
else
  ok "PostgreSQL dump written (${PG_DUMP_BYTES} bytes; verification skipped)."
fi

# 2. MinIO objects hot backup (chunked streaming tar.gz + incremental SHA-256; never reads whole objects into RAM)
info "2/4 Backing up MinIO objects (streaming)..."
compose_cmd exec -T backend uv run --no-sync python - > "${STAGE_DIR}/minio-objects.tar.gz" <<'PY'
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

CHUNK_BYTES = 1024 * 1024  # 1 MiB streaming chunks
RESTORE_MARKER_KEY = ".dalton-restore.json"
RESTORE_TAG_KEY = "dalton-restore"
BUCKET_ENV_VARS = (
    "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
    "DALTON_ASR_STORAGE_BUCKET_NAME",
    "STORAGE_BUCKET_NAME",
    "S3_BUCKET_NAME",
    "OSS_BUCKET_NAME",
    "R2_BUCKET_NAME",
)


def _env(primary: str, *fallbacks: str, default: str = "") -> str:
    for key in (primary, *fallbacks):
        val = (os.getenv(key) or "").strip()
        if val:
            return val
    return default


def is_restore_created_bucket(s3_client, bucket_name: str) -> bool:
    """Identify restore-created buckets via bucket tag or .dalton-restore.json marker (never name suffix)."""
    try:
        tag_resp = s3_client.get_bucket_tagging(Bucket=bucket_name)
        for tag in tag_resp.get("TagSet") or []:
            if tag.get("Key") == RESTORE_TAG_KEY and str(tag.get("Value", "")).lower() == "true":
                return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code not in (
            "NoSuchTagSet",
            "NoSuchTagSetError",
            "404",
            "NotFound",
            "NotImplemented",
            "MethodNotAllowed",
        ) and status not in (404, 405, 501):
            raise

    try:
        s3_client.head_object(Bucket=bucket_name, Key=RESTORE_MARKER_KEY)
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code not in ("404", "NoSuchKey", "NotFound") and status != 404:
            raise
    return False


class _HashingStream:
    """Chunked streaming reader that updates SHA-256 incrementally without buffering the whole object."""

    def __init__(self, body, expected_size: int) -> None:
        self._body = body
        self._expected_size = expected_size
        self.hasher = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        to_read = CHUNK_BYTES if (size is None or size < 0) else min(size, CHUNK_BYTES)
        chunk = self._body.read(to_read)
        if chunk:
            self.hasher.update(chunk)
            self.bytes_read += len(chunk)
            if self.bytes_read > self._expected_size:
                raise RuntimeError(
                    f"Object grew during backup stream ({self.bytes_read} > {self._expected_size} bytes)"
                )
        return chunk


def _norm_ep(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(raw)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            netloc = f"{host}:{parts.port}" if parts.port else host
            return urlunsplit((parts.scheme, netloc, parts.path, "", "")).rstrip("/").lower()
    except Exception:
        pass
    return raw.lower()


def _norm_prefix(raw: str) -> str:
    return (raw or "").strip().strip("/")


def _read_yaml_storage_provider() -> tuple[str | None, bool]:
    """Read `storage.provider` from agent_config.yaml (+ overlay), matching `src/utils/storage/__init__.py`."""
    from pathlib import Path

    try:
        import yaml  # type: ignore[import-not-found]
    except Exception:
        yaml = None

    def _from_file(path: Path) -> str | None:
        if not path.is_file():
            return None
        if yaml is not None:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict):
                st = data.get("storage")
                if isinstance(st, dict):
                    val = st.get("provider")
                    if val is not None and str(val).strip() != "":
                        return str(val).strip().lower()
        return None

    base = None
    ptc_cfg = (os.getenv("PTC_CONFIG_FILE") or "").strip()
    if ptc_cfg and Path(ptc_cfg).is_file():
        base = Path(ptc_cfg)
    else:
        for cand in (Path("/app/agent_config.yaml"), Path("agent_config.yaml")):
            if cand.is_file():
                base = cand
                break
    if base is None:
        return None, False

    provider = _from_file(base)
    app_env = (os.getenv("APP_ENV") or os.getenv("DALTON_PROFILE") or "core").strip()
    overlay = base.with_name(f"{base.stem}.{app_env}{base.suffix}")
    if overlay.is_file():
        ov_provider = _from_file(overlay)
        if ov_provider is not None:
            provider = ov_provider
    return provider, True


def resolve_runtime_storage_targets() -> dict[str, dict[str, str]]:
    """Resolve effective storage targets for chat_upload, asr, and general storage and verify single-service invariant."""
    targets: dict[str, dict[str, str]] = {}

    asr_ep = _norm_ep(os.getenv("DALTON_ASR_STORAGE_ENDPOINT_URL") or "")
    asr_bucket = (os.getenv("DALTON_ASR_STORAGE_BUCKET_NAME") or "").strip()
    asr_ak = (os.getenv("DALTON_ASR_STORAGE_ACCESS_KEY_ID") or "").strip()
    asr_sk = (os.getenv("DALTON_ASR_STORAGE_SECRET_ACCESS_KEY") or "").strip()
    asr_prefix = _norm_prefix(os.getenv("DALTON_ASR_STORAGE_KEY_PREFIX") or "")
    asr_target: dict[str, str] | None = None
    if asr_ep or (asr_bucket and asr_ak and asr_sk):
        asr_target = {
            "service_id": f"s3:{asr_ep}" if asr_ep else "s3:asr-unset-endpoint",
            "endpoint": asr_ep,
            "bucket": asr_bucket,
            "key_prefix": asr_prefix,
        }
        targets["asr"] = asr_target

    chat_ep = _norm_ep(os.getenv("DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL") or "")
    chat_bucket = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME") or "").strip()
    chat_ak = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID") or "").strip()
    chat_sk = (os.getenv("DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY") or "").strip()
    if chat_ep and chat_bucket and chat_ak and chat_sk:
        targets["chat_upload"] = {
            "service_id": f"s3:{chat_ep}",
            "endpoint": chat_ep,
            "bucket": chat_bucket,
            "key_prefix": _norm_prefix(os.getenv("DALTON_CHAT_UPLOAD_STORAGE_KEY_PREFIX") or ""),
        }
    elif asr_target is not None:
        targets["chat_upload"] = dict(asr_target)

    yaml_provider, yaml_found = _read_yaml_storage_provider()
    env_provider = (os.getenv("STORAGE_PROVIDER") or "").strip().lower()
    if yaml_found:
        raw_provider = yaml_provider if yaml_provider else (env_provider or "none")
    else:
        raw_provider = env_provider if env_provider else "s3"

    gen_prefix = _norm_prefix(os.getenv("STORAGE_KEY_PREFIX") or "")
    if raw_provider == "oss":
        oss_bucket = (os.getenv("OSS_BUCKET_NAME") or "").strip()
        if oss_bucket:
            oss_ep = _norm_ep(os.getenv("OSS_ENDPOINT") or "")
            oss_region = (os.getenv("OSS_REGION") or "").strip().lower()
            oss_id = oss_ep or (f"default-oss:{oss_region}" if oss_region else "default-oss")
            targets["general"] = {
                "service_id": f"oss:{oss_id}",
                "endpoint": oss_ep,
                "bucket": oss_bucket,
                "key_prefix": gen_prefix,
            }
    elif raw_provider != "none":
        s3_bucket = (os.getenv("STORAGE_BUCKET_NAME") or os.getenv("S3_BUCKET_NAME") or "").strip()
        if s3_bucket:
            s3_ep = _norm_ep(os.getenv("STORAGE_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or "")
            s3_region = (os.getenv("STORAGE_REGION") or os.getenv("S3_REGION") or "us-east-1").strip().lower()
            targets["general"] = {
                "service_id": f"s3:{s3_ep}" if s3_ep else f"s3:aws-s3-default:{s3_region}",
                "endpoint": s3_ep,
                "bucket": s3_bucket,
                "key_prefix": gen_prefix,
            }

    distinct_services = sorted({t["service_id"] for t in targets.values()})
    if len(distinct_services) > 1:
        details = ", ".join(
            f"{name}={t['service_id']} (bucket={t['bucket'] or '<unset>'})"
            for name, t in targets.items()
        )
        print(
            f"ERROR: Multiple object storage services detected: {details}",
            file=sys.stderr,
        )
        print(
            "ERROR: v1 only supports a single object storage service; configure all application buckets on the same storage service.",
            file=sys.stderr,
        )
        sys.exit(1)

    return targets


runtime_targets = resolve_runtime_storage_targets()
endpoint = _env(
    "DALTON_CHAT_UPLOAD_STORAGE_ENDPOINT_URL",
    "STORAGE_ENDPOINT_URL",
    default="http://minio:9000",
).rstrip("/")
default_bucket = (
    (runtime_targets.get("chat_upload") or {}).get("bucket")
    or _env(
        "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME",
        "STORAGE_BUCKET_NAME",
        default="chat-uploads",
    )
)
default_key_prefix = (runtime_targets.get("chat_upload") or {}).get("key_prefix", "")
access_key = _env(
    "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
    "MINIO_ROOT_USER",
    "STORAGE_ACCESS_KEY_ID",
)
secret_key = _env(
    "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
    "MINIO_ROOT_PASSWORD",
    "STORAGE_SECRET_ACCESS_KEY",
)
region = _env("DALTON_CHAT_UPLOAD_STORAGE_REGION", "STORAGE_REGION", default="us-east-1")
addressing_style = _env(
    "DALTON_CHAT_UPLOAD_STORAGE_ADDRESSING_STYLE",
    "STORAGE_ADDRESSING_STYLE",
    default="path",
).lower()

if not access_key or not secret_key:
    print("ERROR: MinIO credentials are missing in container environment.", file=sys.stderr)
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
        read_timeout=120,
        retries={"max_attempts": 5, "mode": "standard"},
    ),
)

# Fail loudly if buckets cannot be enumerated completely; never fall back silently.
try:
    resp = client.list_buckets()
except (ClientError, BotoCoreError) as exc:
    print(f"ERROR: Failed to list MinIO buckets: {exc}", file=sys.stderr)
    sys.exit(1)

all_buckets = sorted(b["Name"] for b in resp.get("Buckets", []) if b.get("Name"))

# Collect every bucket currently configured for the application so backups always
# include all active buckets — even if an active bucket was created by a prior restore.
configured_var_buckets: dict[str, str] = {}
for var_name in BUCKET_ENV_VARS:
    val = (os.getenv(var_name) or "").strip()
    if val:
        configured_var_buckets[var_name] = val
if "DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME" not in configured_var_buckets and default_bucket:
    configured_var_buckets["DALTON_CHAT_UPLOAD_STORAGE_BUCKET_NAME"] = default_bucket
active_buckets = set(configured_var_buckets.values())
for t in runtime_targets.values():
    if t.get("bucket"):
        active_buckets.add(t["bucket"])

buckets: list[str] = []
skipped_buckets: list[str] = []
for b in all_buckets:
    if b in active_buckets:
        buckets.append(b)
    elif is_restore_created_bucket(client, b):
        skipped_buckets.append(b)
    else:
        buckets.append(b)

if skipped_buckets:
    print(
        f"[backup] INFO: Skipping inactive restore-created bucket(s): {', '.join(skipped_buckets)}",
        file=sys.stderr,
    )
if default_bucket and default_bucket not in buckets:
    print(
        f"ERROR: Expected active bucket '{default_bucket}' not present in list_buckets() output ({buckets}).",
        file=sys.stderr,
    )
    sys.exit(1)

manifest_objects: list[dict[str, object]] = []
total_bytes = 0

with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz", format=tarfile.PAX_FORMAT) as tf:
    for bucket in buckets:
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for entry in page.get("Contents", []):
                key = entry["Key"]
                if key == RESTORE_MARKER_KEY:
                    continue
                obj = client.get_object(Bucket=bucket, Key=key)
                body = obj["Body"]
                try:
                    expected_size = int(obj.get("ContentLength", entry.get("Size", 0)))
                    content_type = obj.get("ContentType") or "application/octet-stream"
                    stream = _HashingStream(body, expected_size)

                    tar_name = f"objects/{bucket}/{key}"
                    tinfo = tarfile.TarInfo(name=tar_name)
                    tinfo.size = expected_size
                    tinfo.mode = 0o600
                    tinfo.pax_headers = {"DALTON.content_type": content_type}
                    tf.addfile(tinfo, stream)

                    # Verify EOF and exact byte count match ContentLength.
                    trailing = body.read(1)
                    if trailing:
                        raise RuntimeError(
                            f"Object '{bucket}/{key}' had extra bytes beyond ContentLength={expected_size}"
                        )
                    if stream.bytes_read != expected_size:
                        raise RuntimeError(
                            f"Object '{bucket}/{key}' truncated during backup "
                            f"({stream.bytes_read} != {expected_size} bytes)"
                        )
                finally:
                    body.close()

                digest = stream.hasher.hexdigest()
                total_bytes += stream.bytes_read
                manifest_objects.append(
                    {
                        "bucket": bucket,
                        "key": key,
                        "size": stream.bytes_read,
                        "sha256": digest,
                        "content_type": content_type,
                    }
                )

    manifest_raw = json.dumps(
        {
            "buckets": buckets,
            "primary_bucket": default_bucket,
            "primary_key_prefix": default_key_prefix,
            "targets": {
                name: {
                    "endpoint": t["endpoint"],
                    "bucket": t["bucket"],
                    "key_prefix": t["key_prefix"],
                }
                for name, t in runtime_targets.items()
            },
            "var_buckets": {k: v for k, v in configured_var_buckets.items() if v in buckets},
            "bucket_count": len(buckets),
            "object_count": len(manifest_objects),
            "total_bytes": total_bytes,
            "objects": manifest_objects,
        },
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    minfo = tarfile.TarInfo(name="_minio_manifest.json")
    minfo.size = len(manifest_raw)
    minfo.mode = 0o600
    tf.addfile(minfo, io.BytesIO(manifest_raw))
PY

MINIO_MANIFEST_FILE="${STAGE_DIR}/.minio-manifest.tmp.json"
tar -xOzf "${STAGE_DIR}/minio-objects.tar.gz" _minio_manifest.json > "$MINIO_MANIFEST_FILE"
MINIO_BUCKET_COUNT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["bucket_count"])' "$MINIO_MANIFEST_FILE")"
MINIO_OBJECT_COUNT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["object_count"])' "$MINIO_MANIFEST_FILE")"
MINIO_TOTAL_BYTES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["total_bytes"])' "$MINIO_MANIFEST_FILE")"

if [[ "$VERIFY" -eq 1 ]]; then
  ARCHIVED_OBJECT_COUNT="$(tar -tzf "${STAGE_DIR}/minio-objects.tar.gz" | awk '/^objects\// && !/\/$/ { c++ } END { print c+0 }')"
  if [[ "$ARCHIVED_OBJECT_COUNT" -ne "$MINIO_OBJECT_COUNT" ]]; then
    err "MinIO archive verification mismatch: archived=${ARCHIVED_OBJECT_COUNT} != manifest=${MINIO_OBJECT_COUNT}."
    exit 1
  fi
  ok "MinIO objects verified (${MINIO_BUCKET_COUNT} bucket(s), ${MINIO_OBJECT_COUNT} object(s), ${MINIO_TOTAL_BYTES} bytes)."
else
  ok "MinIO objects archived (${MINIO_BUCKET_COUNT} bucket(s), ${MINIO_OBJECT_COUNT} object(s); verification skipped)."
fi

# Immediately verify every completed (non-'uploading') chat upload in the PostgreSQL snapshot
# exists in the MinIO archive just enumerated; fail the backup if any object is missing.
if [[ -z "$PG_TOC" ]]; then
  PG_TOC="$(compose_cmd exec -T postgres pg_restore --list < "${STAGE_DIR}/postgres.dump")"
fi
if printf '%s\n' "$PG_TOC" | grep -Eq '^[[:space:]]*[0-9]+;[[:space:]]+[0-9]+[[:space:]]+[0-9]+[[:space:]]+TABLE DATA[[:space:]]+public[[:space:]]+dalton_chat_file_uploads([[:space:]]|$)'; then
  info "Verifying completed chat upload references in PostgreSQL snapshot against MinIO archive..."
  CHAT_UPLOADS_COPY_TMP="${STAGE_DIR}/.chat-uploads-copy.sql"
  if ! compose_cmd exec -T postgres pg_restore --data-only --schema=public --table=dalton_chat_file_uploads \
      < "${STAGE_DIR}/postgres.dump" > "$CHAT_UPLOADS_COPY_TMP"; then
    err "Failed to extract dalton_chat_file_uploads data from PostgreSQL snapshot for object verification."
    exit 1
  fi
  if ! python3 "${SCRIPT_DIR}/storage_config.py" verify-backup-refs \
      --copy-sql "$CHAT_UPLOADS_COPY_TMP" \
      --minio-manifest "$MINIO_MANIFEST_FILE" \
      --caller backup; then
    exit 1
  fi
  rm -f "$CHAT_UPLOADS_COPY_TMP"
fi

# 3. Caddy certificate & PKI data backup (/data on edge)
info "3/4 Backing up Caddy certificate & PKI data..."
compose_cmd exec -T edge tar -czf - -C /data . > "${STAGE_DIR}/caddy-data.tar.gz"

CADDY_FILE_COUNT=0
if [[ "$VERIFY" -eq 1 ]]; then
  CADDY_FILE_COUNT="$(tar -tzf "${STAGE_DIR}/caddy-data.tar.gz" | awk '!/\/$/ && $0 != "." && $0 != "./" { c++ } END { print c+0 }')"
  ok "Caddy data verified (${CADDY_FILE_COUNT} file(s))."
else
  ok "Caddy data archived (verification skipped)."
fi

# 4. .env handling (never copy plaintext .env into backup directory)
ENV_BACKUP_STATUS="excluded"
if [[ -n "$ENV_PASSPHRASE_FILE" ]]; then
  info "4/4 Encrypting .env into env.enc (${ENV_CIPHER} + ${ENV_KDF} ${ENV_KDF_ITER} iter)..."
  openssl enc "-${ENV_CIPHER}" -pbkdf2 -md "${ENV_KDF_DIGEST}" -iter "${ENV_KDF_ITER}" -salt \
    -in "$ENV_FILE" \
    -out "${STAGE_DIR}/env.enc" \
    -pass "file:${ENV_PASSPHRASE_FILE}"
  chmod 600 "${STAGE_DIR}/env.enc"
  if [[ "$VERIFY" -eq 1 ]]; then
    openssl enc -d "-${ENV_CIPHER}" -pbkdf2 -md "${ENV_KDF_DIGEST}" -iter "${ENV_KDF_ITER}" \
      -in "${STAGE_DIR}/env.enc" \
      -pass "file:${ENV_PASSPHRASE_FILE}" >/dev/null
  fi
  ENV_BACKUP_STATUS="encrypted"
  ok "Encrypted .env saved as env.enc (passphrase file is NOT stored in backup directory)."
else
  info "4/4 Skipping plaintext .env (secrets are never stored unencrypted in backup folders)."
  warn "Store ${ENV_FILE} separately in a password manager / secret vault, or pass --env-passphrase-file <file> next time to include an encrypted env.enc."
fi

# Write manifest.json (strictly metadata only; zero secrets)
RELEASE_SHA=""
if [[ -f "${WORK_DIR}/.release_git_sha" ]]; then
  RELEASE_SHA="$(tr -d '[:space:]' < "${WORK_DIR}/.release_git_sha")"
fi

python3 - "${STAGE_DIR}/manifest.json" \
  "$MINIO_MANIFEST_FILE" \
  "$TIMESTAMP" \
  "$RELEASE_SHA" \
  "$PROJECT_NAME" \
  "$DB_NAME" \
  "$ALEMBIC_UPSTREAM" \
  "$ALEMBIC_DALTON" \
  "$PG_DUMP_BYTES" \
  "$PG_TOC_ENTRIES" \
  "$MINIO_BUCKET_COUNT" \
  "$MINIO_OBJECT_COUNT" \
  "$MINIO_TOTAL_BYTES" \
  "$CADDY_FILE_COUNT" \
  "$ENV_BACKUP_STATUS" \
  "$ENV_CIPHER" \
  "$ENV_KDF" \
  "$ENV_KDF_DIGEST" \
  "$ENV_KDF_ITER" <<'PY'
import json, sys

(
    out_path,
    minio_manifest_path,
    ts,
    release_sha,
    compose_project,
    db_name,
    alembic_up,
    alembic_dalton,
    pg_bytes,
    pg_toc,
    minio_buckets,
    minio_objects,
    minio_bytes,
    caddy_files,
    env_status,
    env_cipher,
    env_kdf,
    env_digest,
    env_iter,
) = sys.argv[1:]

with open(minio_manifest_path, encoding="utf-8") as mfh:
    minio_meta = json.load(mfh)

manifest = {
    "schema_version": 2,
    "created_at_utc": ts,
    "release_git_sha": release_sha or None,
    "compose_project": compose_project or None,
    "database": {
        "name": db_name,
        "file": "postgres.dump",
        "format": "pg_dump_custom",
        "bytes": int(pg_bytes),
        "toc_entries": int(pg_toc),
        "alembic_upstream": alembic_up or None,
        "alembic_dalton": alembic_dalton or None,
    },
    "minio": {
        "file": "minio-objects.tar.gz",
        "primary_bucket": minio_meta.get("primary_bucket") or None,
        "primary_key_prefix": minio_meta.get("primary_key_prefix") or "",
        "targets": minio_meta.get("targets") or {},
        "bucket_count": int(minio_buckets),
        "object_count": int(minio_objects),
        "total_bytes": int(minio_bytes),
    },
    "caddy": {
        "file": "caddy-data.tar.gz",
        "file_count": int(caddy_files),
    },
    "env_backup": env_status,
    "env_encryption": (
        {
            "cipher": env_cipher,
            "kdf": env_kdf,
            "digest": env_digest,
            "iterations": int(env_iter),
            "integrity": "SHA256SUMS",
        }
        if env_status == "encrypted"
        else None
    ),
}

with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(manifest, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
rm -f "$MINIO_MANIFEST_FILE"
chmod 600 "${STAGE_DIR}/manifest.json"

# Write SHA256SUMS for every artifact in the backup directory
(
  cd "$STAGE_DIR"
  : > SHA256SUMS
  for f in postgres.dump minio-objects.tar.gz caddy-data.tar.gz manifest.json env.enc; do
    if [[ -f "$f" ]]; then
      printf '%s  %s\n' "$(sha256_file "$f")" "$f" >> SHA256SUMS
    fi
  done
  chmod 600 SHA256SUMS
)

# Publish atomically and prune under lock so concurrent backups never collide or
# delete unrelated directories.
acquire_publish_lock
while [[ -e "$FINAL_DIR" ]]; do
  RAND_SUFFIX="$(python3 -c 'import secrets; print(secrets.token_hex(3))')"
  BACKUP_NAME="dalton-backup-${TIMESTAMP}-${RAND_SUFFIX}"
  FINAL_DIR="${OUTPUT_DIR}/${BACKUP_NAME}"
done
mv "$STAGE_DIR" "$FINAL_DIR"
chmod 700 "$FINAL_DIR"

# Prune older backups belonging to this instance (must have a valid manifest.json)
PRUNED=0
if (( RETAIN > 0 )); then
  PRUNE_LIST="$(python3 - "$OUTPUT_DIR" "$PROJECT_NAME" "$RETAIN" <<'PY'
import json
import sys
from pathlib import Path

output_dir = Path(sys.argv[1])
project_name = sys.argv[2]
retain = int(sys.argv[3])

eligible: list[Path] = []
for entry in sorted(output_dir.iterdir(), key=lambda p: p.name):
    if entry.is_symlink() or not entry.is_dir():
        continue
    if not entry.name.startswith("dalton-backup-"):
        continue
    manifest_path = entry / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        continue
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if not isinstance(data, dict) or not data.get("schema_version"):
        continue
    m_proj = data.get("compose_project")
    if m_proj and project_name and m_proj != project_name:
        continue
    eligible.append(entry)

if len(eligible) > retain:
    for old_dir in eligible[: len(eligible) - retain]:
        print(str(old_dir))
PY
)"
  while IFS= read -r old_dir; do
    [[ -z "$old_dir" ]] && continue
    rm -rf "$old_dir"
    PRUNED=$(( PRUNED + 1 ))
  done <<< "$PRUNE_LIST"
fi
release_publish_lock

RESTORE_HINT_ARGS=(bash scripts/standalone/restore.sh --work-dir "$WORK_DIR" --backup-dir "$FINAL_DIR")
if [[ "$COMPOSE_FILES_EXPLICIT" -eq 1 ]]; then
  RESTORE_HINT_ARGS+=(--compose-files "$COMPOSE_FILES_RAW")
fi

printf "\n== Backup Complete ==\n"
printf "  Backup directory : %s\n" "$FINAL_DIR"
printf "  PostgreSQL dump  : %s bytes (%s TOC entries)\n" "$PG_DUMP_BYTES" "$PG_TOC_ENTRIES"
printf "  MinIO objects    : %s object(s) across %s bucket(s)\n" "$MINIO_OBJECT_COUNT" "$MINIO_BUCKET_COUNT"
printf "  Caddy data       : %s file(s)\n" "$CADDY_FILE_COUNT"
printf "  .env handling    : %s\n" "$ENV_BACKUP_STATUS"
if (( RETAIN > 0 )); then
  printf "  Retention policy : keep %s (pruned %s older backup(s))\n" "$RETAIN" "$PRUNED"
fi
printf "\n  Next steps:\n"
printf "  - Copy %s to encrypted off-host storage (caddy-data.tar.gz contains TLS private keys).\n" "$FINAL_DIR"
if [[ "$ENV_BACKUP_STATUS" == "excluded" ]]; then
  printf "  - Keep %s backed up separately in your secret vault.\n" "$ENV_FILE"
else
  printf "  - Keep your decryption passphrase stored separately from %s.\n" "$FINAL_DIR"
fi
printf "  - Restore on a clean host with:\n"
printf "      %s\n" "$(fmt_cmd "${RESTORE_HINT_ARGS[@]}")"
