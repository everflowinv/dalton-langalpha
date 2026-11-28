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
PROJECT_NAME_ARG=""
PROJECT_NAME_EXPLICIT=0
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
  --project-name <name>       Explicit Docker Compose project name (overrides .env
                              COMPOSE_PROJECT_NAME and work-dir basename).
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

# External PostgreSQL & conflict guard: strictly read from .env via stdlib resolver (never shell env).
if ! EARLY_PG_CFG_JSON="$(python3 "${SCRIPT_DIR}/storage_config.py" resolve-pg-config \
    --env-file "$ENV_FILE" \
    --default-host postgres \
    --check-conflict \
    --caller backup)"; then
  exit 1
fi
EARLY_PG_LINES="$(printf '%s' "$EARLY_PG_CFG_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("host", "user", "dbname"):
    print(d.get(k, ""))
')"
DB_HOST="$(printf '%s\n' "$EARLY_PG_LINES" | sed -n '1p')"
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

DB_USER="$(printf '%s\n' "$EARLY_PG_LINES" | sed -n '2p')"
DB_NAME="$(printf '%s\n' "$EARLY_PG_LINES" | sed -n '3p')"
DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-postgres}"
if [[ "$PROJECT_NAME_EXPLICIT" -eq 1 ]]; then
  PROJECT_NAME="$PROJECT_NAME_ARG"
else
  RAW_ENV_PROJECT="$(read_env_file_var "$ENV_FILE" "COMPOSE_PROJECT_NAME" "")"
  if [[ -n "$RAW_ENV_PROJECT" ]]; then
    PROJECT_NAME="$(normalize_env_project_name "$RAW_ENV_PROJECT")"
    validate_project_name "$PROJECT_NAME" "COMPOSE_PROJECT_NAME"
  else
    PROJECT_NAME="$(sanitize_project_name "$(basename "$WORK_DIR")")"
    validate_project_name "$PROJECT_NAME" "COMPOSE_PROJECT_NAME"
  fi
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
    "DATABASE_URL",
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
PUBLISH_LOCK_FILE="${DALTON_PUBLISH_LOCK_FILE:-${OUTPUT_DIR}/.dalton-backup.lock.flock}"
LOCK_HELD=0
PUBLISH_LOCK_HELD=0
PUBLISH_LOCK_FD_OPEN=0
PUBLISH_LOCK_HELPER_PID=""
PUBLISH_LOCK_TMP_DIR=""
WIKI_INSTANCE_HASH="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "${WORK_DIR}|${PROJECT_NAME}")"
WIKI_LOCK_FILE="${DALTON_WIKI_BACKUP_LOCK_FILE:-/tmp/dalton-wiki-backup-${PROJECT_NAME}-${WIKI_INSTANCE_HASH}.flock}"
WIKI_LOCK_HELD=0
WIKI_LOCK_FD_OPEN=0
WIKI_LOCK_HELPER_PID=""
WIKI_LOCK_CTL_FIFO=""
WIKI_LOCK_STATUS_FILE=""
WIKI_LOCK_ERR_FILE=""
WIKI_WRITES_PAUSED=0
WIKI_PAUSE_PENDING=0
WIKI_PAUSE_CMD_PID=""
WIKI_PAUSE_LEASE_TMP=""
WIKI_PAUSE_PORT="8951"
WIKI_PAUSE_ADMIN_TOKEN=""
WIKI_PAUSE_OWNER="${BACKUP_NAME}-pid$$"
WIKI_PAUSE_LEASE_ID=""
WIKI_PAUSE_TTL_S="${DALTON_WIKI_PAUSE_TTL_S:-300}"
if [[ ! "$WIKI_PAUSE_TTL_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_PAUSE_TTL_S="300"
fi
WIKI_PAUSE_HEARTBEAT_S="${DALTON_WIKI_PAUSE_HEARTBEAT_S:-}"
if [[ -z "$WIKI_PAUSE_HEARTBEAT_S" || ! "$WIKI_PAUSE_HEARTBEAT_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_PAUSE_HEARTBEAT_S="$(python3 -c 'import sys; ttl=float(sys.argv[1]); print(f"{max(0.05, min(60.0, ttl / 3.0)):.3f}")' "$WIKI_PAUSE_TTL_S")"
fi
WIKI_PAUSE_HEARTBEAT_TIMEOUT_S="${DALTON_WIKI_PAUSE_HEARTBEAT_TIMEOUT_S:-}"
if [[ -z "$WIKI_PAUSE_HEARTBEAT_TIMEOUT_S" || ! "$WIKI_PAUSE_HEARTBEAT_TIMEOUT_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_PAUSE_HEARTBEAT_TIMEOUT_S="$(python3 -c 'import sys; ttl=float(sys.argv[1]); print(f"{max(1.0, min(30.0, ttl / 2.0)):.3f}")' "$WIKI_PAUSE_TTL_S")"
fi
WIKI_EXEC_TIMEOUT_S="${DALTON_WIKI_EXEC_TIMEOUT_S:-$WIKI_PAUSE_HEARTBEAT_TIMEOUT_S}"
if [[ -z "$WIKI_EXEC_TIMEOUT_S" || ! "$WIKI_EXEC_TIMEOUT_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_EXEC_TIMEOUT_S="$WIKI_PAUSE_HEARTBEAT_TIMEOUT_S"
fi
WIKI_RESUME_TIMEOUT_S="${DALTON_WIKI_RESUME_TIMEOUT_S:-$WIKI_EXEC_TIMEOUT_S}"
if [[ -z "$WIKI_RESUME_TIMEOUT_S" || ! "$WIKI_RESUME_TIMEOUT_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_RESUME_TIMEOUT_S="$WIKI_EXEC_TIMEOUT_S"
fi
WIKI_DRAIN_POLL_S="${DALTON_WIKI_DRAIN_POLL_S:-1}"
if [[ ! "$WIKI_DRAIN_POLL_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_DRAIN_POLL_S="1"
fi
WIKI_DRAIN_MAX_POLLS="${DALTON_WIKI_DRAIN_MAX_POLLS:-30}"
if [[ ! "$WIKI_DRAIN_MAX_POLLS" =~ ^[1-9][0-9]*$ ]]; then
  WIKI_DRAIN_MAX_POLLS="30"
fi
WIKI_DRAIN_TIMEOUT_S="${DALTON_WIKI_DRAIN_TIMEOUT_S:-35}"
if [[ -z "$WIKI_DRAIN_TIMEOUT_S" || ! "$WIKI_DRAIN_TIMEOUT_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_DRAIN_TIMEOUT_S="35"
fi
WIKI_PAUSE_RECOVERY_TIMEOUT_S="${DALTON_WIKI_PAUSE_RECOVERY_TIMEOUT_S:-}"
if [[ -z "$WIKI_PAUSE_RECOVERY_TIMEOUT_S" || ! "$WIKI_PAUSE_RECOVERY_TIMEOUT_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_PAUSE_RECOVERY_TIMEOUT_S="$(python3 -c 'import sys; t=float(sys.argv[1]); print(f"{min(15.0, max(2.0, t * 3.0)):.3f}")' "$WIKI_EXEC_TIMEOUT_S")"
fi
WIKI_PAUSE_RECOVERY_POLL_S="${DALTON_WIKI_PAUSE_RECOVERY_POLL_S:-0.25}"
if [[ -z "$WIKI_PAUSE_RECOVERY_POLL_S" || ! "$WIKI_PAUSE_RECOVERY_POLL_S" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  WIKI_PAUSE_RECOVERY_POLL_S="0.25"
fi
WIKI_HEARTBEAT_PID=""
WIKI_HEARTBEAT_FD_OPEN=0
WIKI_HEARTBEAT_CTL_FIFO=""
WIKI_HEARTBEAT_FAILED_FILE=""

# Run a command in its own process group with a single wall-clock deadline that
# covers BOTH the process group AND reading stdout to EOF (so a descendant still
# holding stdout after the group leader exits cannot stall command substitution).
# On timeout or signal, closes our stdout pipe read end, sends SIGTERM to the
# entire process group, waits a short grace period, then unconditionally sends
# SIGKILL to the entire process group and reaps the direct child.
# Exits with child exit code, or 124 on timeout.
run_with_timeout_pg() {
  local timeout_s="$1"
  shift
  local py_src=""
  IFS= read -r -d '' py_src <<'PY' || true
import errno, os, select, signal, subprocess, sys, time

timeout_s = max(0.05, float(sys.argv[1]))
cmd = sys.argv[2:]
if not cmd:
    sys.exit(2)

active_proc = None

def pgrp_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def terminate_pg(proc) -> None:
    if proc is None:
        return
    pgid = proc.pid
    if proc.stdout is not None:
        try:
            proc.stdout.close()
        except OSError:
            pass
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    grace_deadline = time.monotonic() + 0.4
    while time.monotonic() < grace_deadline:
        child_done = proc.poll() is not None
        if child_done and not pgrp_alive(pgid):
            break
        time.sleep(0.02)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass
    kill_deadline = time.monotonic() + 0.5
    while time.monotonic() < kill_deadline:
        if not pgrp_alive(pgid):
            break
        time.sleep(0.02)

def on_sig(signum, _frame) -> None:
    terminate_pg(active_proc)
    sys.exit(128 + signum)

signal.signal(signal.SIGTERM, on_sig)
signal.signal(signal.SIGINT, on_sig)

active_proc = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    start_new_session=True,
)
pgid = active_proc.pid
assert active_proc.stdout is not None
stdout_fd = active_proc.stdout.fileno()
os.set_blocking(stdout_fd, False)

deadline = time.monotonic() + timeout_s
chunks: list[bytes] = []
stdout_eof = False

while True:
    rem = deadline - time.monotonic()
    if rem <= 0:
        terminate_pg(active_proc)
        sys.exit(124)

    if not stdout_eof:
        try:
            r, _, _ = select.select([stdout_fd], [], [], min(0.02, rem))
        except InterruptedError:
            continue
        if r:
            while True:
                if time.monotonic() >= deadline:
                    terminate_pg(active_proc)
                    sys.exit(124)
                try:
                    data = os.read(stdout_fd, 65536)
                except BlockingIOError:
                    break
                except OSError as exc:
                    if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                        break
                    stdout_eof = True
                    break
                if not data:
                    stdout_eof = True
                    break
                chunks.append(data)
                if time.monotonic() >= deadline:
                    terminate_pg(active_proc)
                    sys.exit(124)
    else:
        if active_proc.poll() is not None and not pgrp_alive(pgid):
            break
        time.sleep(min(0.02, rem))

    if stdout_eof and active_proc.poll() is not None and not pgrp_alive(pgid):
        break

try:
    active_proc.stdout.close()
except OSError:
    pass
rc = active_proc.returncode if active_proc.returncode is not None else 1
if chunks:
    sys.stdout.buffer.write(b"".join(chunks))
    sys.stdout.buffer.flush()
sys.exit(rc)
PY
  if [[ "${RUN_WITH_TIMEOUT_PG_EXEC:-0}" -eq 1 ]]; then
    exec python3 -c "$py_src" "$timeout_s" "$@"
  fi
  python3 -c "$py_src" "$timeout_s" "$@"
}

_kill_lock_helper_and_reap() {
  local pid="$1"
  [[ -n "$pid" ]] || return 0
  kill -TERM -- "-${pid}" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  local waited_ms=0
  while (( waited_ms < 400 )); do
    if ! kill -0 "$pid" 2>/dev/null && ! kill -0 -- "-${pid}" 2>/dev/null; then
      break
    fi
    sleep 0.02
    waited_ms=$(( waited_ms + 20 ))
  done
  kill -KILL -- "-${pid}" 2>/dev/null || true
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  local reap_ms=0
  while (( reap_ms < 500 )); do
    if ! kill -0 "$pid" 2>/dev/null && ! kill -0 -- "-${pid}" 2>/dev/null; then
      break
    fi
    sleep 0.02
    reap_ms=$(( reap_ms + 20 ))
  done
}

# Acquire an atomic OS-level exclusive advisory lock (fcntl.flock) per deployment instance.
# Kernel automatically releases the lock on process exit/crash, eliminating PID-file cleanup races.
acquire_wiki_backup_lock() {
  local max_wait="${DALTON_WIKI_BACKUP_LOCK_WAIT_S:-60}"
  if [[ ! "$max_wait" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    max_wait=60
  fi
  local handshake_timeout="${DALTON_WIKI_BACKUP_LOCK_HANDSHAKE_TIMEOUT_S:-}"
  if [[ -z "$handshake_timeout" || ! "$handshake_timeout" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    handshake_timeout="$(exec 8>&- 9>&-; python3 -c 'import sys; print(f"{float(sys.argv[1]) + 5.0:.3f}")' "$max_wait")"
  fi
  WIKI_LOCK_CTL_FIFO="${STAGE_DIR}/.wiki-lock-ctl.fifo"
  WIKI_LOCK_STATUS_FILE="${STAGE_DIR}/.wiki-lock-status"
  WIKI_LOCK_ERR_FILE="${STAGE_DIR}/.wiki-lock-err"
  rm -f "$WIKI_LOCK_CTL_FIFO" "$WIKI_LOCK_STATUS_FILE" "$WIKI_LOCK_ERR_FILE"
  mkfifo "$WIKI_LOCK_CTL_FIFO"
  local wiki_lock_py
  wiki_lock_py='
import fcntl, os, sys, time

try:
    os.setsid()
except OSError:
    pass

lock_path, max_wait_s, status_file = sys.argv[1], float(sys.argv[2]), sys.argv[3]

def write_status(msg: str) -> None:
    tmp = f"{status_file}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as sf:
        sf.write(msg + "\n")
        sf.flush()
    os.replace(tmp, status_file)

fd = -1
try:
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.0, max_wait_s)
    acquired = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    write_status("ACQUIRED" if acquired else "TIMEOUT")
except Exception as exc:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    write_status(f"ERROR:{type(exc).__name__}: {exc}")
    sys.exit(2)

if not acquired:
    os.close(fd)
    sys.exit(1)

try:
    for line in sys.stdin:
        if line.strip() == "RELEASE":
            break
finally:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)
'
  # Open control FIFO read/write in parent so neither parent nor helper can hang on FIFO open.
  # Launch Python lock helper directly (no wrapper subshell) so $! IS the actual lock helper PID.
  exec 8<>"$WIKI_LOCK_CTL_FIFO"
  WIKI_LOCK_FD_OPEN=1
  python3 -c "$wiki_lock_py" "$WIKI_LOCK_FILE" "$max_wait" "$WIKI_LOCK_STATUS_FILE" < "$WIKI_LOCK_CTL_FIFO" >/dev/null 2>"$WIKI_LOCK_ERR_FILE" 8>&- 9>&- &
  WIKI_LOCK_HELPER_PID=$!

  local lock_status=""
  lock_status="$(exec 8>&- 9>&-; python3 - "$WIKI_LOCK_HELPER_PID" "$WIKI_LOCK_STATUS_FILE" "$WIKI_LOCK_ERR_FILE" "$handshake_timeout" <<'PY'
import os, subprocess, sys, time

helper_pid = int(sys.argv[1])
status_file = sys.argv[2]
err_file = sys.argv[3]
timeout_s = max(0.1, float(sys.argv[4]))
deadline = time.monotonic() + timeout_s

def read_first_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().strip().splitlines()
            return lines[0] if lines else ""
    except OSError:
        return ""

def helper_exited(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    proc_stat = f"/proc/{pid}/stat"
    if os.path.exists(proc_stat):
        try:
            raw = open(proc_stat, encoding="utf-8", errors="replace").read()
            rp = raw.rfind(")")
            if rp > 0:
                fields = raw[rp + 1 :].strip().split()
                if fields and fields[0] in ("Z", "X", "x"):
                    return True
        except OSError:
            pass
        return False
    try:
        res = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            stat_str = (res.stdout or "").strip()
            return (not stat_str) or stat_str.startswith("Z")
    except OSError:
        pass
    return False

while True:
    if os.path.exists(status_file):
        line = read_first_line(status_file)
        if line:
            print(line)
            sys.exit(0)
    if helper_exited(helper_pid):
        if os.path.exists(status_file):
            line = read_first_line(status_file)
            if line:
                print(line)
                sys.exit(0)
        err_msg = read_first_line(err_file) or "lock helper exited unexpectedly before handshake"
        print(f"HELPER_EXITED:{err_msg}")
        sys.exit(0)
    if time.monotonic() >= deadline:
        print("HANDSHAKE_TIMEOUT")
        sys.exit(0)
    time.sleep(0.02)
PY
  )" || lock_status="HANDSHAKE_WAIT_FAILED"

  if [[ "$lock_status" != "ACQUIRED" ]]; then
    if [[ "$WIKI_LOCK_FD_OPEN" -eq 1 ]]; then
      exec 8>&- || true
      WIKI_LOCK_FD_OPEN=0
    fi
    if [[ -n "$WIKI_LOCK_HELPER_PID" ]]; then
      if [[ "$lock_status" == "HANDSHAKE_TIMEOUT" || "$lock_status" == "HANDSHAKE_WAIT_FAILED" ]]; then
        _kill_lock_helper_and_reap "$WIKI_LOCK_HELPER_PID"
      else
        wait "$WIKI_LOCK_HELPER_PID" 2>/dev/null || _kill_lock_helper_and_reap "$WIKI_LOCK_HELPER_PID"
      fi
      WIKI_LOCK_HELPER_PID=""
    fi
    rm -f "$WIKI_LOCK_STATUS_FILE" "$WIKI_LOCK_ERR_FILE" "$WIKI_LOCK_CTL_FIFO"
    WIKI_LOCK_STATUS_FILE=""
    WIKI_LOCK_ERR_FILE=""
    WIKI_LOCK_CTL_FIFO=""
    case "$lock_status" in
      TIMEOUT)
        err "Another Wiki backup is already running for project '${PROJECT_NAME}' (lock=${WIKI_LOCK_FILE}, waited ${max_wait}s)."
        ;;
      ERROR:*)
        err "Failed to open or lock Wiki backup lock '${WIKI_LOCK_FILE}': ${lock_status#ERROR:}"
        ;;
      HELPER_EXITED:*)
        err "Wiki backup lock helper exited before handshake (lock=${WIKI_LOCK_FILE}): ${lock_status#HELPER_EXITED:}"
        ;;
      HANDSHAKE_TIMEOUT)
        err "Timed out waiting for Wiki backup lock helper handshake (lock=${WIKI_LOCK_FILE}, timeout=${handshake_timeout}s)."
        ;;
      *)
        err "Wiki backup lock acquisition failed (lock=${WIKI_LOCK_FILE}, status=${lock_status:-empty})."
        ;;
    esac
    return 1
  fi
  rm -f "$WIKI_LOCK_STATUS_FILE" "$WIKI_LOCK_ERR_FILE"
  WIKI_LOCK_STATUS_FILE=""
  WIKI_LOCK_ERR_FILE=""
  WIKI_LOCK_HELD=1
  return 0
}

release_wiki_backup_lock() {
  if [[ "$WIKI_LOCK_HELD" -eq 1 || "$WIKI_LOCK_FD_OPEN" -eq 1 || -n "$WIKI_LOCK_HELPER_PID" ]]; then
    # Closing fd 8 delivers EOF to the flock helper without risking SIGPIPE.
    if [[ "$WIKI_LOCK_FD_OPEN" -eq 1 ]]; then
      exec 8>&- || true
      WIKI_LOCK_FD_OPEN=0
    fi
    if [[ -n "$WIKI_LOCK_HELPER_PID" ]]; then
      local waited_ms=0
      while kill -0 "$WIKI_LOCK_HELPER_PID" 2>/dev/null && (( waited_ms < 2000 )); do
        sleep 0.05
        waited_ms=$(( waited_ms + 50 ))
      done
      if kill -0 "$WIKI_LOCK_HELPER_PID" 2>/dev/null || kill -0 -- "-${WIKI_LOCK_HELPER_PID}" 2>/dev/null; then
        _kill_lock_helper_and_reap "$WIKI_LOCK_HELPER_PID"
      else
        wait "$WIKI_LOCK_HELPER_PID" 2>/dev/null || true
      fi
      WIKI_LOCK_HELPER_PID=""
    fi
    WIKI_LOCK_HELD=0
  fi
  if [[ -n "$WIKI_LOCK_CTL_FIFO" ]]; then
    rm -f "$WIKI_LOCK_CTL_FIFO" 2>/dev/null || true
    WIKI_LOCK_CTL_FIFO=""
  fi
  if [[ -n "$WIKI_LOCK_STATUS_FILE" ]]; then
    rm -f "$WIKI_LOCK_STATUS_FILE" 2>/dev/null || true
    WIKI_LOCK_STATUS_FILE=""
  fi
  if [[ -n "$WIKI_LOCK_ERR_FILE" ]]; then
    rm -f "$WIKI_LOCK_ERR_FILE" 2>/dev/null || true
    WIKI_LOCK_ERR_FILE=""
  fi
}

stop_wiki_pause_heartbeat() {
  if [[ "$WIKI_HEARTBEAT_FD_OPEN" -eq 1 ]]; then
    exec 9>&- || true
    WIKI_HEARTBEAT_FD_OPEN=0
  fi
  if [[ -n "$WIKI_HEARTBEAT_PID" ]]; then
    kill -TERM "$WIKI_HEARTBEAT_PID" 2>/dev/null || true
    local waited_ms=0
    while kill -0 "$WIKI_HEARTBEAT_PID" 2>/dev/null && (( waited_ms < 2000 )); do
      sleep 0.05
      waited_ms=$(( waited_ms + 50 ))
    done
    if kill -0 "$WIKI_HEARTBEAT_PID" 2>/dev/null; then
      kill -KILL "$WIKI_HEARTBEAT_PID" 2>/dev/null || true
    fi
    wait "$WIKI_HEARTBEAT_PID" 2>/dev/null || true
    WIKI_HEARTBEAT_PID=""
  fi
  if [[ -n "$WIKI_HEARTBEAT_CTL_FIFO" ]]; then
    rm -f "$WIKI_HEARTBEAT_CTL_FIFO" 2>/dev/null || true
    WIKI_HEARTBEAT_CTL_FIFO=""
  fi
}

start_wiki_pause_heartbeat() {
  stop_wiki_pause_heartbeat
  WIKI_HEARTBEAT_FAILED_FILE="${STAGE_DIR}/.wiki-heartbeat-failed"
  WIKI_HEARTBEAT_CTL_FIFO="${STAGE_DIR}/.wiki-hb-ctl.fifo"
  rm -f "$WIKI_HEARTBEAT_FAILED_FILE" "$WIKI_HEARTBEAT_CTL_FIFO"
  mkfifo "$WIKI_HEARTBEAT_CTL_FIFO"
  exec 9<>"$WIKI_HEARTBEAT_CTL_FIFO"
  WIKI_HEARTBEAT_FD_OPEN=1

  local parent_pid="$$"
  local wiki_hb_py
  wiki_hb_py='
import os, select, signal, subprocess, sys, time

parent_pid = int(sys.argv[1])
interval_s = max(0.02, float(sys.argv[2]))
req_timeout_s = max(0.1, float(sys.argv[3]))
failed_file = sys.argv[4]
cmd = sys.argv[5:]

stop_requested = False
active_proc = None

def terminate_proc(proc) -> None:
    if proc is None:
        return
    pgid = proc.pid
    # Always signal the entire process group even if the leader already exited.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        child_done = proc.poll() is not None
        try:
            os.killpg(pgid, 0)
            pg_alive = True
        except ProcessLookupError:
            pg_alive = False
        except PermissionError:
            pg_alive = True
        if child_done and not pg_alive:
            break
        time.sleep(0.02)
    # After grace period, unconditionally SIGKILL the entire process group
    # regardless of whether the group leader has already exited.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass
    kill_deadline = time.monotonic() + 0.5
    while time.monotonic() < kill_deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            break
        time.sleep(0.02)

def on_signal(_signum, _frame) -> None:
    global stop_requested
    stop_requested = True
    terminate_proc(active_proc)

signal.signal(signal.SIGTERM, on_signal)
signal.signal(signal.SIGINT, on_signal)

def parent_alive() -> bool:
    try:
        os.kill(parent_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

def ctl_eof() -> bool:
    try:
        r, _, _ = select.select([sys.stdin], [], [], 0.0)
        if r:
            data = os.read(sys.stdin.fileno(), 64)
            if not data or b"STOP" in data:
                return True
    except (OSError, ValueError):
        return True
    return False

while not stop_requested:
    sleep_deadline = time.monotonic() + interval_s
    while not stop_requested:
        if not parent_alive() or ctl_eof():
            sys.exit(0)
        rem = sleep_deadline - time.monotonic()
        if rem <= 0:
            break
        try:
            r, _, _ = select.select([sys.stdin], [], [], min(0.05, rem))
            if r:
                data = os.read(sys.stdin.fileno(), 64)
                if not data or b"STOP" in data:
                    sys.exit(0)
        except InterruptedError:
            continue
        except (OSError, ValueError):
            sys.exit(0)

    if stop_requested or not parent_alive() or ctl_eof():
        sys.exit(0)

    active_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    req_deadline = time.monotonic() + req_timeout_s
    timed_out = False
    while active_proc.poll() is None:
        if stop_requested or not parent_alive() or ctl_eof():
            terminate_proc(active_proc)
            active_proc = None
            sys.exit(0)
        if time.monotonic() >= req_deadline:
            timed_out = True
            terminate_proc(active_proc)
            break
        try:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if r:
                data = os.read(sys.stdin.fileno(), 64)
                if not data or b"STOP" in data:
                    terminate_proc(active_proc)
                    active_proc = None
                    sys.exit(0)
        except InterruptedError:
            continue
        except (OSError, ValueError):
            terminate_proc(active_proc)
            active_proc = None
            sys.exit(0)

    rc = active_proc.returncode if active_proc is not None else 1
    active_proc = None
    if stop_requested or not parent_alive():
        sys.exit(0)
    if timed_out or rc != 0:
        try:
            with open(failed_file, "w", encoding="utf-8") as ff:
                ff.write("heartbeat_timeout\n" if timed_out else "heartbeat_failed\n")
        except OSError:
            pass
        sys.exit(1)
'
  local hb_sh_script
  hb_sh_script='payload="$(python3 -c "import json,os; print(json.dumps({\"paused\": True, \"owner\": os.environ[\"DALTON_RH_PAUSE_OWNER\"], \"lease_id\": int(os.environ[\"DALTON_RH_PAUSE_LEASE_ID\"]), \"ttl_s\": float(os.environ[\"DALTON_RH_PAUSE_TTL_S\"])}))")" && body="$(curl -sf --connect-timeout "${DALTON_RH_CURL_TIMEOUT_S}" --max-time "${DALTON_RH_CURL_TIMEOUT_S}" -X POST -H "Content-Type: application/json" -H "Authorization: Bearer ${DALTON_RH_ADMIN_TOKEN}" -d "$payload" "$1/admin/wiki/pause-writes")" && printf "%s" "$body" | python3 -c "import json,os,sys; d=json.loads(sys.stdin.read()); sys.exit(0 if isinstance(d, dict) and d.get(\"paused\") is True and d.get(\"expired\") is not True and str(d.get(\"lease_id\")) == os.environ[\"DALTON_RH_PAUSE_LEASE_ID\"] else 1)"'
  DALTON_RH_ADMIN_TOKEN="$WIKI_PAUSE_ADMIN_TOKEN" \
    DALTON_RH_PAUSE_OWNER="$WIKI_PAUSE_OWNER" \
    DALTON_RH_PAUSE_LEASE_ID="$WIKI_PAUSE_LEASE_ID" \
    DALTON_RH_PAUSE_TTL_S="$WIKI_PAUSE_TTL_S" \
    DALTON_RH_CURL_TIMEOUT_S="$WIKI_PAUSE_HEARTBEAT_TIMEOUT_S" \
    python3 -c "$wiki_hb_py" \
      "$parent_pid" \
      "$WIKI_PAUSE_HEARTBEAT_S" \
      "$WIKI_PAUSE_HEARTBEAT_TIMEOUT_S" \
      "$WIKI_HEARTBEAT_FAILED_FILE" \
      env "${ENV_UNSET_ARGS[@]}" docker compose -p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" \
      exec -T -e DALTON_RH_ADMIN_TOKEN -e DALTON_RH_PAUSE_OWNER -e DALTON_RH_PAUSE_LEASE_ID -e DALTON_RH_PAUSE_TTL_S -e DALTON_RH_CURL_TIMEOUT_S \
      research-host sh -c "$hb_sh_script" _ "http://127.0.0.1:${WIKI_PAUSE_PORT}" \
      < "$WIKI_HEARTBEAT_CTL_FIFO" >/dev/null 2>&1 8>&- 9>&- &
  WIKI_HEARTBEAT_PID=$!
}

resume_wiki_writes() {
  stop_wiki_pause_heartbeat
  if [[ "$WIKI_WRITES_PAUSED" -eq 1 && -n "$WIKI_PAUSE_ADMIN_TOKEN" ]]; then
    local resume_state=""
    local resume_sh_script
    resume_sh_script='payload="$(python3 -c "import json,os; p={\"paused\": False, \"owner\": os.environ[\"DALTON_RH_PAUSE_OWNER\"]}; lid=os.environ.get(\"DALTON_RH_PAUSE_LEASE_ID\", \"\").strip(); p.update({\"lease_id\": int(lid)} if lid else {}); print(json.dumps(p))")" && body="$(curl -sf --connect-timeout "${DALTON_RH_CURL_TIMEOUT_S}" --max-time "${DALTON_RH_CURL_TIMEOUT_S}" -X POST -H "Content-Type: application/json" -H "Authorization: Bearer ${DALTON_RH_ADMIN_TOKEN}" -d "$payload" "$1/admin/wiki/pause-writes")" && printf "%s" "$body" | python3 -c "import json,os,sys; d=json.loads(sys.stdin.read()); paused=d.get(\"paused\") if isinstance(d, dict) else None; expired=bool(d.get(\"expired\")) if isinstance(d, dict) else True; cont=d.get(\"continuous\") if isinstance(d, dict) else False; print(\"FAIL\" if paused is not False else (\"EXPIRED\" if (expired or cont is False) else \"OK\"))"'
    set +e
    resume_state="$(exec 8>&- 9>&-; \
      DALTON_RH_ADMIN_TOKEN="$WIKI_PAUSE_ADMIN_TOKEN" \
      DALTON_RH_PAUSE_OWNER="$WIKI_PAUSE_OWNER" \
      DALTON_RH_PAUSE_LEASE_ID="$WIKI_PAUSE_LEASE_ID" \
      DALTON_RH_CURL_TIMEOUT_S="$WIKI_RESUME_TIMEOUT_S" \
      run_with_timeout_pg "$WIKI_RESUME_TIMEOUT_S" \
        env "${ENV_UNSET_ARGS[@]}" docker compose -p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" \
        exec -T -e DALTON_RH_ADMIN_TOKEN -e DALTON_RH_PAUSE_OWNER -e DALTON_RH_PAUSE_LEASE_ID -e DALTON_RH_CURL_TIMEOUT_S \
        research-host sh -c "$resume_sh_script" _ "http://127.0.0.1:${WIKI_PAUSE_PORT}" 2>/dev/null)"
    local exec_rc=$?
    set -e
    if [[ "$exec_rc" -ne 0 || ( "$resume_state" != "OK" && "$resume_state" != "EXPIRED" ) ]]; then
      # Keep WIKI_WRITES_PAUSED=1 so the EXIT cleanup trap can retry resuming once more.
      err "CRITICAL: Failed to resume Wiki writes on research-host (owner=${WIKI_PAUSE_OWNER}, lease_id=${WIKI_PAUSE_LEASE_ID}, exit=${exec_rc}, state=${resume_state:-<empty>}, timeout=${WIKI_RESUME_TIMEOUT_S}s); Wiki writes may remain paused (503) until resumed or lease expires (ttl=${WIKI_PAUSE_TTL_S}s)."
      return 1
    fi
    WIKI_WRITES_PAUSED=0
    if [[ "$resume_state" == "EXPIRED" ]]; then
      err "ERROR: Wiki write-pause lease (owner=${WIKI_PAUSE_OWNER}, lease_id=${WIKI_PAUSE_LEASE_ID}) expired during backup; Wiki may have accepted writes mid-snapshot, discarding incomplete backup."
      return 2
    fi
  fi
  return 0
}

probe_and_resume_pending_wiki_pause() {
  if [[ "$WIKI_PAUSE_PENDING" -ne 1 || "$WIKI_WRITES_PAUSED" -eq 1 || -z "$WIKI_PAUSE_ADMIN_TOKEN" ]]; then
    return 0
  fi
  if [[ -n "$WIKI_PAUSE_CMD_PID" ]]; then
    kill -TERM "$WIKI_PAUSE_CMD_PID" 2>/dev/null || true
    wait "$WIKI_PAUSE_CMD_PID" 2>/dev/null || true
    WIKI_PAUSE_CMD_PID=""
  fi
  if [[ -n "$WIKI_PAUSE_LEASE_TMP" ]]; then
    rm -f "$WIKI_PAUSE_LEASE_TMP" 2>/dev/null || true
    WIKI_PAUSE_LEASE_TMP=""
  fi
  local local_probe_sh='body="$(curl -sf --connect-timeout "${DALTON_RH_CURL_TIMEOUT_S}" --max-time "${DALTON_RH_CURL_TIMEOUT_S}" -H "Authorization: Bearer ${DALTON_RH_ADMIN_TOKEN}" "$1/admin/wiki/pause-writes")" || exit 1; printf "%s" "$body" | python3 -c "import json,os,sys; d=json.loads(sys.stdin.read()); lid=d.get(\"lease_id\") if isinstance(d, dict) else None; ok=isinstance(d, dict) and d.get(\"paused\") is True and str(d.get(\"owner\") or \"\") == os.environ[\"DALTON_RH_PROBE_OWNER\"] and isinstance(lid, int) and lid > 0; print(lid if ok else \"NONE\")"'
  local probe_deadline_s probe_step_cap_s recovered_lease_id="" rem_probe_s probe_step_s probe_out probe_rc probe_sleep_s
  probe_deadline_s="$(exec 8>&- 9>&-; python3 -c 'import sys, time; print(f"{time.monotonic() + max(0.05, float(sys.argv[1])):.6f}")' "$WIKI_PAUSE_RECOVERY_TIMEOUT_S")"
  probe_step_cap_s="$(exec 8>&- 9>&-; python3 -c 'import sys; exec_t=float(sys.argv[1]); total_t=max(0.05, float(sys.argv[2])); print(f"{max(0.05, min(exec_t, total_t / 3.0)):.6f}")' "$WIKI_EXEC_TIMEOUT_S" "$WIKI_PAUSE_RECOVERY_TIMEOUT_S")"
  while true; do
    rem_probe_s="$(exec 8>&- 9>&-; python3 -c 'import sys, time; print(f"{max(0.0, float(sys.argv[1]) - time.monotonic()):.6f}")' "$probe_deadline_s")"
    if [[ "$rem_probe_s" == "0.000000" ]]; then
      break
    fi
    probe_step_s="$(exec 8>&- 9>&-; python3 -c 'import sys; print(f"{max(0.05, min(float(sys.argv[1]), float(sys.argv[2]))):.6f}")' "$probe_step_cap_s" "$rem_probe_s")"
    set +e
    probe_out="$(exec 8>&- 9>&-; \
      DALTON_RH_ADMIN_TOKEN="$WIKI_PAUSE_ADMIN_TOKEN" \
      DALTON_RH_PROBE_OWNER="$WIKI_PAUSE_OWNER" \
      DALTON_RH_CURL_TIMEOUT_S="$probe_step_s" \
      run_with_timeout_pg "$probe_step_s" \
        env "${ENV_UNSET_ARGS[@]}" docker compose -p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" \
        exec -T -e DALTON_RH_ADMIN_TOKEN -e DALTON_RH_PROBE_OWNER -e DALTON_RH_CURL_TIMEOUT_S \
        research-host sh -c "$local_probe_sh" _ "http://127.0.0.1:${WIKI_PAUSE_PORT}" 2>/dev/null)"
    probe_rc=$?
    set -e
    if [[ "$probe_rc" -eq 0 && "$probe_out" =~ ^[0-9]+$ ]]; then
      recovered_lease_id="$probe_out"
      break
    fi
    rem_probe_s="$(exec 8>&- 9>&-; python3 -c 'import sys, time; print(f"{max(0.0, float(sys.argv[1]) - time.monotonic()):.6f}")' "$probe_deadline_s")"
    if [[ "$rem_probe_s" == "0.000000" ]]; then
      break
    fi
    probe_sleep_s="$(exec 8>&- 9>&-; python3 -c 'import sys; print(f"{min(float(sys.argv[1]), float(sys.argv[2])):.6f}")' "$WIKI_PAUSE_RECOVERY_POLL_S" "$rem_probe_s")"
    sleep "$probe_sleep_s" 8>&- 9>&-
  done
  WIKI_PAUSE_PENDING=0
  if [[ "$recovered_lease_id" =~ ^[0-9]+$ ]]; then
    warn "Pause request outcome was ambiguous, but research-host confirmed owner=${WIKI_PAUSE_OWNER} holds active lease ${recovered_lease_id}; resuming Wiki writes before exiting..."
    WIKI_PAUSE_LEASE_ID="$recovered_lease_id"
    WIKI_WRITES_PAUSED=1
    resume_wiki_writes || return 1
  fi
  return 0
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

acquire_publish_lock() {
  local waited=0
  local missing_pid_waited=0
  local missing_pid_grace="${DALTON_PUBLISH_LOCK_MISSING_PID_GRACE_S:-5}"
  if [[ ! "$missing_pid_grace" =~ ^[1-9][0-9]*$ ]]; then
    missing_pid_grace=5
  fi
  local max_wait="${DALTON_PUBLISH_LOCK_WAIT_S:-60}"
  if [[ ! "$max_wait" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    max_wait=60
  fi
  local flock_path="${PUBLISH_LOCK_FILE:-${LOCK_DIR}.flock}"
  PUBLISH_LOCK_HELD="${PUBLISH_LOCK_HELD:-0}"
  PUBLISH_LOCK_FD_OPEN="${PUBLISH_LOCK_FD_OPEN:-0}"
  PUBLISH_LOCK_HELPER_PID="${PUBLISH_LOCK_HELPER_PID:-}"
  PUBLISH_LOCK_TMP_DIR="${PUBLISH_LOCK_TMP_DIR:-}"

  # Acquire an OS-level exclusive advisory lock (fcntl.flock, portable across Linux & macOS)
  # per output directory before inspecting or modifying LOCK_DIR, covering both publish and prune.
  # Kernel releases the lock automatically on crash/exit, eliminating empty-PID stale-lock races.
  PUBLISH_LOCK_TMP_DIR="$(mktemp -d)"
  local ctl_fifo="${PUBLISH_LOCK_TMP_DIR}/ctl.fifo"
  local status_file="${PUBLISH_LOCK_TMP_DIR}/status"
  local err_file="${PUBLISH_LOCK_TMP_DIR}/err"
  mkfifo "$ctl_fifo"
  local pub_lock_py
  pub_lock_py='
import fcntl, os, select, sys, time

try:
    os.setsid()
except OSError:
    pass

lock_path = sys.argv[1]
max_wait_s = float(sys.argv[2])
status_file = sys.argv[3]
parent_pid = int(sys.argv[4])

def write_status(msg: str) -> None:
    tmp = f"{status_file}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as sf:
        sf.write(msg + "\n")
        sf.flush()
    os.replace(tmp, status_file)

def parent_alive() -> bool:
    try:
        os.kill(parent_pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

fd = -1
try:
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.0, max_wait_s)
    acquired = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            break
        except BlockingIOError:
            if time.monotonic() >= deadline or not parent_alive():
                break
            time.sleep(0.02)
    write_status("ACQUIRED" if acquired else "TIMEOUT")
except Exception as exc:
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:
            pass
    write_status(f"ERROR:{type(exc).__name__}: {exc}")
    sys.exit(2)

if not acquired:
    os.close(fd)
    sys.exit(1)

try:
    stdin_fd = sys.stdin.fileno()
    while parent_alive():
        try:
            ready, _, _ = select.select([stdin_fd], [], [], 0.1)
        except InterruptedError:
            continue
        except (OSError, ValueError):
            break
        if ready:
            try:
                chunk = os.read(stdin_fd, 64)
            except OSError:
                break
            if not chunk or b"RELEASE" in chunk:
                break
finally:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)
'
  exec 7<>"$ctl_fifo"
  PUBLISH_LOCK_FD_OPEN=1
  python3 -c "$pub_lock_py" "$flock_path" "$max_wait" "$status_file" "$$" < "$ctl_fifo" >/dev/null 2>"$err_file" 8>&- 9>&- &
  PUBLISH_LOCK_HELPER_PID=$!

  local lock_status=""
  lock_status="$(exec 7>&- 8>&- 9>&-; python3 - "$PUBLISH_LOCK_HELPER_PID" "$status_file" "$err_file" "$max_wait" <<'PY'
import os, subprocess, sys, time

helper_pid = int(sys.argv[1])
status_file = sys.argv[2]
err_file = sys.argv[3]
timeout_s = max(0.5, float(sys.argv[4]) + 5.0)
deadline = time.monotonic() + timeout_s

def read_first_line(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().strip().splitlines()
            return lines[0] if lines else ""
    except OSError:
        return ""

def helper_exited(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    proc_stat = f"/proc/{pid}/stat"
    if os.path.exists(proc_stat):
        try:
            raw = open(proc_stat, encoding="utf-8", errors="replace").read()
            rp = raw.rfind(")")
            if rp > 0:
                fields = raw[rp + 1 :].strip().split()
                if fields and fields[0] in ("Z", "X", "x"):
                    return True
        except OSError:
            pass
        return False
    try:
        res = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
        if res.returncode == 0:
            stat_str = (res.stdout or "").strip()
            return (not stat_str) or stat_str.startswith("Z")
    except OSError:
        pass
    return False

while True:
    if os.path.exists(status_file):
        line = read_first_line(status_file)
        if line:
            print(line)
            sys.exit(0)
    if helper_exited(helper_pid):
        if os.path.exists(status_file):
            line = read_first_line(status_file)
            if line:
                print(line)
                sys.exit(0)
        err_msg = read_first_line(err_file) or "lock helper exited unexpectedly"
        print(f"HELPER_EXITED:{err_msg}")
        sys.exit(0)
    if time.monotonic() >= deadline:
        print("HANDSHAKE_TIMEOUT")
        sys.exit(0)
    time.sleep(0.02)
PY
  )" || lock_status="HANDSHAKE_WAIT_FAILED"

  if [[ "$lock_status" != "ACQUIRED" ]]; then
    release_publish_lock
    err "Failed to acquire OS backup publish lock (${flock_path}): ${lock_status:-unknown}"
    exit 1
  fi
  PUBLISH_LOCK_HELD=1

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
      else
        missing_pid_waited=$(( missing_pid_waited + 1 ))
        if (( missing_pid_waited >= missing_pid_grace )); then
          warn "Removing stale backup lock ${LOCK_DIR} (empty/invalid PID file)."
          rm -f "$pid_file" 2>/dev/null || true
          rmdir "$LOCK_DIR" 2>/dev/null || true
          missing_pid_waited=0
          continue
        fi
      fi
    else
      missing_pid_waited=$(( missing_pid_waited + 1 ))
      if (( missing_pid_waited >= missing_pid_grace )); then
        warn "Removing stale backup lock ${LOCK_DIR} (missing PID file)."
        rmdir "$LOCK_DIR" 2>/dev/null || true
        missing_pid_waited=0
        continue
      fi
    fi
    if (( waited >= 60 )); then
      release_publish_lock
      err "Timed out waiting for backup publish lock: ${LOCK_DIR}"
      exit 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done
  LOCK_HELD=1
  if ! printf '%s\n' "$$" > "${LOCK_DIR}/pid" 2>/dev/null || [[ "$(tr -d '[:space:]' < "${LOCK_DIR}/pid" 2>/dev/null || true)" != "$$" ]]; then
    err "Failed to write PID file for backup publish lock: ${LOCK_DIR}/pid"
    release_publish_lock
    exit 1
  fi
}

release_publish_lock() {
  if [[ "${LOCK_HELD:-0}" -eq 1 && -d "${LOCK_DIR:-}" ]]; then
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
  if [[ "${PUBLISH_LOCK_HELD:-0}" -eq 1 || "${PUBLISH_LOCK_FD_OPEN:-0}" -eq 1 || -n "${PUBLISH_LOCK_HELPER_PID:-}" ]]; then
    if [[ "${PUBLISH_LOCK_FD_OPEN:-0}" -eq 1 ]]; then
      exec 7>&- || true
      PUBLISH_LOCK_FD_OPEN=0
    fi
    if [[ -n "${PUBLISH_LOCK_HELPER_PID:-}" ]]; then
      local pid="$PUBLISH_LOCK_HELPER_PID"
      local waited_ms=0
      while kill -0 "$pid" 2>/dev/null && (( waited_ms < 2000 )); do
        sleep 0.02
        waited_ms=$(( waited_ms + 20 ))
      done
      if kill -0 "$pid" 2>/dev/null || kill -0 -- "-${pid}" 2>/dev/null; then
        kill -TERM -- "-${pid}" 2>/dev/null || true
        kill -TERM "$pid" 2>/dev/null || true
        local term_ms=0
        while (( term_ms < 400 )); do
          if ! kill -0 "$pid" 2>/dev/null && ! kill -0 -- "-${pid}" 2>/dev/null; then
            break
          fi
          sleep 0.02
          term_ms=$(( term_ms + 20 ))
        done
        kill -KILL -- "-${pid}" 2>/dev/null || true
        kill -KILL "$pid" 2>/dev/null || true
      fi
      wait "$pid" 2>/dev/null || true
      PUBLISH_LOCK_HELPER_PID=""
    fi
    PUBLISH_LOCK_HELD=0
  fi
  if [[ -n "${PUBLISH_LOCK_TMP_DIR:-}" ]]; then
    rm -rf "$PUBLISH_LOCK_TMP_DIR" 2>/dev/null || true
    PUBLISH_LOCK_TMP_DIR=""
  fi
}

CONSISTENT_BACKEND_PID=""
PG_DUMP_PID=""
PG_DUMP_CONTAINER_PIDFILE=""
BUNDLE_TAR_PID=""
CTL_IN_FIFO=""
CTL_ERR_FILE=""
BUNDLE_OUT_FIFO=""

terminate_container_pg_dump() {
  if [[ -n "${PG_DUMP_CONTAINER_PIDFILE:-}" ]]; then
    compose_cmd exec -T postgres sh -c '
      pidfile="$1"
      if [ -f "$pidfile" ]; then
        cpid="$(tr -dc "0-9" < "$pidfile" 2>/dev/null || true)"
        if [ -n "$cpid" ] && kill -0 "$cpid" 2>/dev/null; then
          kill -TERM "$cpid" 2>/dev/null || true
          i=0
          while [ "$i" -lt 10 ] && kill -0 "$cpid" 2>/dev/null; do
            sleep 0.1
            i=$((i + 1))
          done
          if kill -0 "$cpid" 2>/dev/null; then
            kill -KILL "$cpid" 2>/dev/null || true
            sleep 0.1
          fi
        fi
        rm -f "$pidfile" 2>/dev/null || true
      fi
    ' _ "$PG_DUMP_CONTAINER_PIDFILE" >/dev/null 2>&1 || true
    PG_DUMP_CONTAINER_PIDFILE=""
  fi
  if [[ -n "${PG_DUMP_PID:-}" ]]; then
    if kill -0 "$PG_DUMP_PID" 2>/dev/null; then
      kill -TERM "$PG_DUMP_PID" 2>/dev/null || true
      sleep 0.2
      kill -KILL "$PG_DUMP_PID" 2>/dev/null || true
    fi
    wait "$PG_DUMP_PID" 2>/dev/null || true
    PG_DUMP_PID=""
  fi
}

stop_consistent_backup_children() {
  terminate_container_pg_dump
  local pid=""
  for pid in "${CONSISTENT_BACKEND_PID:-}" "${BUNDLE_TAR_PID:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
      sleep 0.2
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  CONSISTENT_BACKEND_PID=""
  BUNDLE_TAR_PID=""
  if [[ -n "${CTL_IN_FIFO:-}" ]]; then
    rm -f "$CTL_IN_FIFO" 2>/dev/null || true
    CTL_IN_FIFO=""
  fi
  if [[ -n "${CTL_ERR_FILE:-}" ]]; then
    rm -f "$CTL_ERR_FILE" 2>/dev/null || true
    CTL_ERR_FILE=""
  fi
  if [[ -n "${BUNDLE_OUT_FIFO:-}" ]]; then
    rm -f "$BUNDLE_OUT_FIFO" 2>/dev/null || true
    BUNDLE_OUT_FIFO=""
  fi
}

cleanup() {
  trap '' INT TERM
  stop_consistent_backup_children
  probe_and_resume_pending_wiki_pause || true
  resume_wiki_writes || true
  release_wiki_backup_lock
  release_publish_lock
  if [[ -n "$WIKI_PAUSE_LEASE_TMP" ]]; then
    rm -f "$WIKI_PAUSE_LEASE_TMP" 2>/dev/null || true
    WIKI_PAUSE_LEASE_TMP=""
  fi
  if [[ -d "$STAGE_DIR" ]]; then
    rm -rf "$STAGE_DIR"
  fi
}
trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup EXIT

info "Starting zero-downtime consistent backup into ${FINAL_DIR}..."

# 1 & 2. Consistent PostgreSQL + MinIO + terminal ASR-jobs backup via shared consistent_backup protocol (§3.1):
#   - Resolve ONE unified PostgreSQL connection config via stdlib-only `storage_config.py resolve-pg-config`
#     (honoring DB_* > DATABASE_URL identically to backend and restore, failing fast on conflict)
#     and pass it to BOTH compose-backup control session and in-container pg_dump.
#   - Mount the `dalton-asr-jobs` volume read-only into the one-shot backup container and pass
#     `--asr-jobs-dir /var/lib/dalton/asr_jobs` so terminal ASR jobs enter the same reconciliation.
#   - Enforce ONE shared lock-phase deadline across SNAPSHOT_READY + pg_dump, and terminate the
#     actual in-container pg_dump PID immediately on watchdog `TERMINATE_PG_DUMP` or deadline expiry.
COMPOSE_CFG_TMP="${STAGE_DIR}/.compose-config.json"
COMPOSE_PG_ARGS=(--env-file "$ENV_FILE" --default-host postgres --check-conflict --caller backup)
if compose_cmd --profile transcription config --format json > "$COMPOSE_CFG_TMP" 2>/dev/null && [[ -s "$COMPOSE_CFG_TMP" ]]; then
  COMPOSE_PG_ARGS+=(--compose-config-json "$COMPOSE_CFG_TMP" --service backend)
else
  rm -f "$COMPOSE_CFG_TMP" 2>/dev/null || true
  COMPOSE_CFG_TMP=""
fi

if ! PG_CFG_JSON="$(python3 "${SCRIPT_DIR}/storage_config.py" resolve-pg-config "${COMPOSE_PG_ARGS[@]}")"; then
  exit 1
fi
PG_CFG_LINES="$(printf '%s' "$PG_CFG_JSON" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("host", "dbname", "user", "dsn", "container_dsn"):
    print(d.get(k, ""))
')"
PG_TARGET_HOST="$(printf '%s\n' "$PG_CFG_LINES" | sed -n '1p')"
PG_TARGET_DB="$(printf '%s\n' "$PG_CFG_LINES" | sed -n '2p')"
PG_TARGET_USER="$(printf '%s\n' "$PG_CFG_LINES" | sed -n '3p')"
PG_TARGET_DSN="$(printf '%s\n' "$PG_CFG_LINES" | sed -n '4p')"
PG_DUMP_CONTAINER_DSN="$(printf '%s\n' "$PG_CFG_LINES" | sed -n '5p')"

if [[ "$PG_TARGET_HOST" != "postgres" && "$PG_TARGET_HOST" != "127.0.0.1" && "$PG_TARGET_HOST" != "localhost" ]]; then
  err "External PostgreSQL detected in effective database config (host='${PG_TARGET_HOST}'; expected Compose-managed 'postgres')."
  exit 1
fi
DB_NAME="${PG_TARGET_DB:-$DB_NAME}"
DB_USER="${PG_TARGET_USER:-$DB_USER}"

ASR_JOBS_VOLUME="${PROJECT_NAME}_dalton-asr-jobs"
if [[ -n "$COMPOSE_CFG_TMP" && -s "$COMPOSE_CFG_TMP" ]]; then
  RESOLVED_ASR_VOL="$(python3 - "$COMPOSE_CFG_TMP" "$ASR_JOBS_VOLUME" <<'PY'
import json, sys
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
vol = (cfg.get("volumes") or {}).get("dalton-asr-jobs") or {}
print(str(vol.get("name") or sys.argv[2]))
PY
  )"
  if [[ -n "$RESOLVED_ASR_VOL" ]]; then
    ASR_JOBS_VOLUME="$RESOLVED_ASR_VOL"
  fi
fi
rm -f "$COMPOSE_CFG_TMP" 2>/dev/null || true

info "1-2/5 Running consistent PostgreSQL + MinIO + ASR-jobs snapshot backup (database '${DB_NAME}')..."
LOCK_TIMEOUT_S="${DALTON_BACKUP_LOCK_TIMEOUT_S:-7200}"
SNAP_IMPORT_TIMEOUT_S="${DALTON_BACKUP_SNAPSHOT_IMPORT_TIMEOUT_S:-120}"
PG_DUMP_TIMEOUT_S="${DALTON_BACKUP_PG_DUMP_TIMEOUT_S:-7200}"
CTL_IN_FIFO="${STAGE_DIR}/.consistent-ctl.in"
CTL_ERR_FILE="${STAGE_DIR}/.consistent-ctl.err"
BUNDLE_OUT_FIFO="${STAGE_DIR}/.consistent-bundle.out"
mkfifo "$CTL_IN_FIFO" "$BUNDLE_OUT_FIFO"
: > "$CTL_ERR_FILE"
chmod 600 "$CTL_ERR_FILE"

# Hold write-end of CTL_IN_FIFO open on fd 5 so backend waits for explicit handshake tokens.
# Use a regular file on fd 6 for stderr control lines (portable across Bash 3.2 on macOS
# where `read -t 0.1` is invalid, avoids read-write FIFO EOF stalls, and never back-pressures).
exec 5<>"$CTL_IN_FIFO"
exec 6<"$CTL_ERR_FILE"
CTL_ERR_PARTIAL=""
CTL_LINE=""

read_ctl_err_line() {
  local chunk=""
  if IFS= read -r chunk <&6; then
    CTL_LINE="${CTL_ERR_PARTIAL}${chunk}"
    CTL_ERR_PARTIAL=""
    return 0
  fi
  if [[ -n "$chunk" ]]; then
    CTL_ERR_PARTIAL+="$chunk"
  fi
  return 1
}

# Extract streamed tar bundle (minio-objects.tar.gz + optional asr-jobs.tar.gz) directly into STAGE_DIR.
tar -xf "$BUNDLE_OUT_FIFO" -C "${STAGE_DIR}" &
BUNDLE_TAR_PID=$!

CONSISTENT_BACKEND_ARGS=(
  run --rm -T --no-deps
  -e DALTON_BACKUP_PG_DSN
  -v "${ASR_JOBS_VOLUME}:/var/lib/dalton/asr_jobs:ro"
  backend uv run --no-sync python scripts/standalone/consistent_backup.py compose-backup
  --stream-bundle
  --asr-jobs-dir /var/lib/dalton/asr_jobs
  --lock-timeout-s "$LOCK_TIMEOUT_S"
  --snapshot-import-timeout-s "$SNAP_IMPORT_TIMEOUT_S"
  --pg-dump-timeout-s "$PG_DUMP_TIMEOUT_S"
)
if [[ -n "${DALTON_BACKUP_MIRROR_TIMEOUT_S:-}" ]]; then
  CONSISTENT_BACKEND_ARGS+=(--mirror-timeout-s "${DALTON_BACKUP_MIRROR_TIMEOUT_S}")
fi

LOCK_START_TS="$(date +%s)"
SNAP_READY_DEADLINE=$(( LOCK_START_TS + LOCK_TIMEOUT_S ))
DALTON_BACKUP_PG_DSN="$PG_TARGET_DSN" compose_cmd "${CONSISTENT_BACKEND_ARGS[@]}" <"$CTL_IN_FIFO" >"$BUNDLE_OUT_FIFO" 2>"$CTL_ERR_FILE" &
CONSISTENT_BACKEND_PID=$!

SNAPSHOT_ID=""
BACKUP_SUMMARY_JSON=""
while true; do
  NOW_TS="$(date +%s)"
  if (( NOW_TS >= SNAP_READY_DEADLINE )); then
    err "Timed out waiting for consistent_backup snapshot export (${LOCK_TIMEOUT_S}s)."
    printf 'PG_DUMP_FAILED\n' >&5 || true
    exec 5>&- 6<&- || true
    stop_consistent_backup_children
    exit 1
  fi
  while read_ctl_err_line; do
    ctl_line="$CTL_LINE"
    case "$ctl_line" in
      "@@DALTON_CTL@@ SNAPSHOT_READY "*)
        SNAPSHOT_ID="${ctl_line#@@DALTON_CTL@@ SNAPSHOT_READY }"
        SNAPSHOT_ID="${SNAPSHOT_ID//$'\r'/}"
        break 2
        ;;
      "@@DALTON_CTL@@ TERMINATE_PG_DUMP"*)
        err "consistent_backup lock-phase supervisor requested abort before snapshot export."
        exec 5>&- 6<&- || true
        stop_consistent_backup_children
        exit 1
        ;;
      "@@DALTON_CTL@@ "*)
        ;;
      *)
        printf '%s\n' "$ctl_line" >&2
        ;;
    esac
  done
  if ! kill -0 "$CONSISTENT_BACKEND_PID" 2>/dev/null; then
    while read_ctl_err_line; do
      ctl_line="$CTL_LINE"
      case "$ctl_line" in
        "@@DALTON_CTL@@ SNAPSHOT_READY "*)
          SNAPSHOT_ID="${ctl_line#@@DALTON_CTL@@ SNAPSHOT_READY }"
          SNAPSHOT_ID="${SNAPSHOT_ID//$'\r'/}"
          break 2
          ;;
        "@@DALTON_CTL@@ TERMINATE_PG_DUMP"*)
          err "consistent_backup lock-phase supervisor requested abort before snapshot export."
          exec 5>&- 6<&- || true
          stop_consistent_backup_children
          exit 1
          ;;
        "@@DALTON_CTL@@ "*)
          ;;
        *)
          printf '%s\n' "$ctl_line" >&2
          ;;
      esac
    done
    break
  fi
  sleep 0.05
done

if [[ -z "$SNAPSHOT_ID" ]]; then
  if [[ -n "$CTL_ERR_PARTIAL" ]]; then
    printf '%s\n' "$CTL_ERR_PARTIAL" >&2
    CTL_ERR_PARTIAL=""
  fi
  exec 5>&- 6<&- || true
  wait "$CONSISTENT_BACKEND_PID" 2>/dev/null || true
  CONSISTENT_BACKEND_PID=""
  stop_consistent_backup_children
  err "consistent_backup failed before exporting PostgreSQL snapshot."
  exit 1
fi

info "Exported PostgreSQL snapshot '${SNAPSHOT_ID}'; launching pg_dump --snapshot..."
PG_DUMP_CONTAINER_PIDFILE="/tmp/dalton-pg-dump-$$-$(date +%s).pid"
DALTON_PG_DUMP_DSN="$PG_DUMP_CONTAINER_DSN" compose_cmd exec -T -e DALTON_PG_DUMP_DSN postgres \
  sh -c 'printf "%s\n" "$$" > "$2"; exec pg_dump "$DALTON_PG_DUMP_DSN" -Fc --no-owner --no-acl "$1"' _ \
  "--snapshot=${SNAPSHOT_ID}" "$PG_DUMP_CONTAINER_PIDFILE" \
  > "${STAGE_DIR}/postgres.dump" &
PG_DUMP_PID=$!

# Wait until pg_dump emits custom-format bytes (confirming snapshot import) or exits early.
# SNAP_IMPORT_TIMEOUT_S (default 120s) applies ONLY to snapshot import after SNAPSHOT_READY,
# while staying strictly bounded by the shared lock-phase deadline and watchdog control channel.
IMPORT_DEADLINE=$(( $(date +%s) + SNAP_IMPORT_TIMEOUT_S ))
if (( SNAP_READY_DEADLINE < IMPORT_DEADLINE )); then
  IMPORT_DEADLINE="$SNAP_READY_DEADLINE"
fi
DUMP_IMPORTED=0
while true; do
  if [[ -s "${STAGE_DIR}/postgres.dump" ]]; then
    DUMP_IMPORTED=1
    break
  fi
  while read_ctl_err_line; do
    ctl_line="$CTL_LINE"
    case "$ctl_line" in
      "@@DALTON_CTL@@ TERMINATE_PG_DUMP"*)
        err "consistent_backup lock-phase supervisor requested in-container pg_dump termination during snapshot import."
        exec 5>&- 6<&- || true
        stop_consistent_backup_children
        exit 1
        ;;
      "@@DALTON_CTL@@ "*)
        ;;
      *)
        printf '%s\n' "$ctl_line" >&2
        ;;
    esac
  done
  if ! kill -0 "$PG_DUMP_PID" 2>/dev/null; then
    break
  fi
  if ! kill -0 "$CONSISTENT_BACKEND_PID" 2>/dev/null; then
    err "consistent_backup supervisor exited while waiting for pg_dump snapshot import; terminating in-container pg_dump."
    exec 5>&- 6<&- || true
    stop_consistent_backup_children
    exit 1
  fi
  if (( $(date +%s) >= IMPORT_DEADLINE )); then
    err "pg_dump --snapshot timed out waiting to import snapshot (${SNAP_IMPORT_TIMEOUT_S}s)."
    printf 'PG_DUMP_FAILED\n' >&5 || true
    exec 5>&- 6<&- || true
    stop_consistent_backup_children
    exit 1
  fi
  sleep 0.05
done

if [[ "$DUMP_IMPORTED" -eq 0 ]]; then
  set +e
  wait "$PG_DUMP_PID"
  pg_rc=$?
  set -e
  PG_DUMP_PID=""
  if [[ "$pg_rc" -ne 0 ]] || [[ ! -s "${STAGE_DIR}/postgres.dump" ]]; then
    printf 'PG_DUMP_FAILED\n' >&5 || true
    exec 5>&- 6<&- || true
    stop_consistent_backup_children
    err "pg_dump --snapshot failed before importing snapshot (rc=${pg_rc})."
    exit 1
  fi
fi

# Notify consistent_backup that pg_dump imported the snapshot so it commits the export txn immediately.
printf 'SNAPSHOT_IMPORTED\n' >&5

# Wait for pg_dump to complete within the shared lock-phase deadline AND PG_DUMP_TIMEOUT_S,
# continuously honoring watchdog `TERMINATE_PG_DUMP` signals so in-container pg_dump exits immediately on timeout.
DUMP_DEADLINE=$(( $(date +%s) + PG_DUMP_TIMEOUT_S ))
if (( SNAP_READY_DEADLINE < DUMP_DEADLINE )); then
  DUMP_DEADLINE="$SNAP_READY_DEADLINE"
fi
if [[ -n "$PG_DUMP_PID" ]]; then
  while kill -0 "$PG_DUMP_PID" 2>/dev/null; do
    while read_ctl_err_line; do
      ctl_line="$CTL_LINE"
      case "$ctl_line" in
        "@@DALTON_CTL@@ TERMINATE_PG_DUMP"*)
          err "consistent_backup lock-phase watchdog expired; terminating in-container pg_dump immediately."
          exec 5>&- 6<&- || true
          stop_consistent_backup_children
          exit 1
          ;;
        "@@DALTON_CTL@@ BACKUP_SUMMARY "*)
          BACKUP_SUMMARY_JSON="${ctl_line#@@DALTON_CTL@@ BACKUP_SUMMARY }"
          ;;
        "@@DALTON_CTL@@ "*)
          ;;
        *)
          printf '%s\n' "$ctl_line" >&2
          ;;
      esac
    done
    if (( $(date +%s) >= DUMP_DEADLINE )); then
      err "pg_dump --snapshot exceeded shared lock/dump hard timeout; terminating in-container pg_dump."
      printf 'PG_DUMP_FAILED\n' >&5 || true
      exec 5>&- 6<&- || true
      stop_consistent_backup_children
      exit 1
    fi
    if ! kill -0 "$CONSISTENT_BACKEND_PID" 2>/dev/null; then
      err "consistent_backup lock-phase supervisor exited before pg_dump completed; terminating in-container pg_dump."
      exec 5>&- 6<&- || true
      stop_consistent_backup_children
      exit 1
    fi
    sleep 0.05
  done
  set +e
  wait "$PG_DUMP_PID"
  pg_rc=$?
  set -e
  PG_DUMP_PID=""
  # Remove container pidfile cleanly after pg_dump exits
  if [[ -n "${PG_DUMP_CONTAINER_PIDFILE:-}" ]]; then
    compose_cmd exec -T postgres rm -f "$PG_DUMP_CONTAINER_PIDFILE" >/dev/null 2>&1 || true
    PG_DUMP_CONTAINER_PIDFILE=""
  fi
else
  pg_rc=0
fi

if [[ "$pg_rc" -ne 0 ]] || [[ ! -s "${STAGE_DIR}/postgres.dump" ]]; then
  printf 'PG_DUMP_FAILED\n' >&5 || true
  exec 5>&- 6<&- || true
  stop_consistent_backup_children
  err "PostgreSQL snapshot dump failed (rc=${pg_rc})."
  exit 1
fi

printf 'PG_DUMP_DONE\n' >&5
exec 5>&-

# Drain remaining stderr lines from consistent_backup until it exits.
while kill -0 "$CONSISTENT_BACKEND_PID" 2>/dev/null; do
  while read_ctl_err_line; do
    ctl_line="$CTL_LINE"
    case "$ctl_line" in
      "@@DALTON_CTL@@ BACKUP_SUMMARY "*)
        BACKUP_SUMMARY_JSON="${ctl_line#@@DALTON_CTL@@ BACKUP_SUMMARY }"
        ;;
      "@@DALTON_CTL@@ "*)
        ;;
      *)
        printf '%s\n' "$ctl_line" >&2
        ;;
    esac
  done
  if ! kill -0 "$CONSISTENT_BACKEND_PID" 2>/dev/null; then
    break
  fi
  sleep 0.05
done

set +e
wait "$CONSISTENT_BACKEND_PID"
cb_rc=$?
wait "$BUNDLE_TAR_PID"
bundle_tar_rc=$?
set -e
while read_ctl_err_line; do
  ctl_line="$CTL_LINE"
  case "$ctl_line" in
    "@@DALTON_CTL@@ BACKUP_SUMMARY "*)
      BACKUP_SUMMARY_JSON="${ctl_line#@@DALTON_CTL@@ BACKUP_SUMMARY }"
      ;;
    "@@DALTON_CTL@@ "*)
      ;;
    *)
      printf '%s\n' "$ctl_line" >&2
      ;;
  esac
done
if [[ -n "$CTL_ERR_PARTIAL" ]]; then
  printf '%s\n' "$CTL_ERR_PARTIAL" >&2
  CTL_ERR_PARTIAL=""
fi
exec 6<&-
CONSISTENT_BACKEND_PID=""
BUNDLE_TAR_PID=""
rm -f "$CTL_IN_FIFO" "$CTL_ERR_FILE" "$BUNDLE_OUT_FIFO"
CTL_IN_FIFO=""
CTL_ERR_FILE=""
BUNDLE_OUT_FIFO=""

if [[ "$cb_rc" -ne 0 ]]; then
  err "Consistent object backup / reconciliation failed (rc=${cb_rc})."
  exit 1
fi
if [[ "$bundle_tar_rc" -ne 0 ]] || [[ ! -s "${STAGE_DIR}/minio-objects.tar.gz" ]]; then
  err "Failed extracting consistent backup archive bundle (tar_rc=${bundle_tar_rc})."
  exit 1
fi

if [[ -n "$BACKUP_SUMMARY_JSON" ]]; then
  CONCISE_SUMMARY="$(python3 - "$BACKUP_SUMMARY_JSON" <<'PY'
import json, sys

raw = sys.argv[1]
try:
    data = json.loads(raw)
except Exception:
    print(raw)
    raise SystemExit(0)
if not isinstance(data, dict):
    print(raw)
    raise SystemExit(0)

sample = data.get("missing_referenced_sample")
if not isinstance(sample, list):
    raw_objs = data.get("missing_referenced_objects")
    if isinstance(raw_objs, list):
        sample = []
        seen = set()
        for item in raw_objs:
            if isinstance(item, dict):
                b = str(item.get("bucket") or "").strip()
                k = str(item.get("key") or "").strip().lstrip("/")
                if b and k and (b, k) not in seen:
                    seen.add((b, k))
                    sample.append(f"{b}/{k}")
    else:
        sample = []

clean_sample = [str(x) for x in sample[:10]]
miss_count = int(data.get("missing_referenced_count") or 0)
if not miss_count and isinstance(data.get("missing_referenced_objects"), list):
    miss_count = len(data["missing_referenced_objects"])

compact = {
    "snapshot_id": data.get("snapshot_id"),
    "object_count": data.get("object_count"),
    "missing_referenced_count": miss_count,
    "expired_by_rule_count": int(data.get("expired_by_rule_count") or 0),
    "reconcile_ok": data.get("reconcile_ok"),
}
if data.get("checked_refs") is not None:
    compact["checked_refs"] = data.get("checked_refs")
if miss_count > 0:
    compact["missing_referenced_sample"] = clean_sample
    if miss_count > len(clean_sample):
        compact["missing_referenced_more"] = miss_count - len(clean_sample)
print(json.dumps(compact, ensure_ascii=False))
PY
)"
  info "Consistent backup reconciled: ${CONCISE_SUMMARY}"
fi

if [[ ! -s "${STAGE_DIR}/postgres.dump" ]]; then
  err "PostgreSQL dump is empty."
  exit 1
fi

PG_DUMP_BYTES="$(wc -c < "${STAGE_DIR}/postgres.dump" | tr -d ' ')"
ALEMBIC_UPSTREAM="$(DALTON_PG_DUMP_DSN="$PG_DUMP_CONTAINER_DSN" compose_cmd exec -T -e DALTON_PG_DUMP_DSN postgres sh -c 'psql "$DALTON_PG_DUMP_DSN" -Atqc "SELECT version_num FROM alembic_version;"' 2>/dev/null | tr -d '\r\n' || true)"
ALEMBIC_DALTON="$(DALTON_PG_DUMP_DSN="$PG_DUMP_CONTAINER_DSN" compose_cmd exec -T -e DALTON_PG_DUMP_DSN postgres sh -c 'psql "$DALTON_PG_DUMP_DSN" -Atqc "SELECT version_num FROM alembic_version_dalton;"' 2>/dev/null | tr -d '\r\n' || true)"

PG_TOC_FILE="${STAGE_DIR}/.pg-toc.tmp"
PG_TOC_ENTRIES=0
if [[ "$VERIFY" -eq 1 ]]; then
  compose_cmd exec -T postgres pg_restore --list < "${STAGE_DIR}/postgres.dump" > "$PG_TOC_FILE"
  PG_TOC_ENTRIES="$(grep -vcE '^[[:space:]]*(;|$)' "$PG_TOC_FILE" || true)"
  if (( PG_TOC_ENTRIES == 0 )); then
    err "pg_restore --list returned 0 TOC entries."
    exit 1
  fi
  if ! grep -q 'alembic_version_dalton' "$PG_TOC_FILE"; then
    warn "TOC does not contain alembic_version_dalton (database may not be migrated yet)."
  fi
  ok "PostgreSQL dump verified (${PG_DUMP_BYTES} bytes, ${PG_TOC_ENTRIES} TOC entries; upstream=${ALEMBIC_UPSTREAM:-unknown}, dalton=${ALEMBIC_DALTON:-unknown})."
else
  ok "PostgreSQL dump written (${PG_DUMP_BYTES} bytes; verification skipped)."
fi
rm -f "$PG_TOC_FILE"

MINIO_MANIFEST_FILE="${STAGE_DIR}/.minio-manifest.tmp.json"
tar -xOzf "${STAGE_DIR}/minio-objects.tar.gz" _minio_manifest.json > "$MINIO_MANIFEST_FILE"
MINIO_BUCKET_COUNT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["bucket_count"])' "$MINIO_MANIFEST_FILE")"
MINIO_OBJECT_COUNT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["object_count"])' "$MINIO_MANIFEST_FILE")"
MINIO_TOTAL_BYTES="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["total_bytes"])' "$MINIO_MANIFEST_FILE")"

# Persist missing-referenced-objects alert once on the host (never inside the ephemeral container).
# Alert write failure must NEVER block publishing a verified backup set.
HOST_ALERTS_FALLBACK="${OUTPUT_DIR}/backup-alerts.log"
HOST_ALERTS_FILE="${DALTON_BACKUP_ALERTS_FILE:-}"
if [[ -z "$HOST_ALERTS_FILE" && -f "$ENV_FILE" ]]; then
  HOST_ALERTS_FILE="$(read_env_file_var "$ENV_FILE" "DALTON_BACKUP_ALERTS_FILE" "")"
fi
if [[ -z "$HOST_ALERTS_FILE" || "$HOST_ALERTS_FILE" == *.json || "$HOST_ALERTS_FILE" == *.JSON ]]; then
  HOST_ALERTS_FILE="$HOST_ALERTS_FALLBACK"
fi
MISSING_ALERT_STATUS=""
MISSING_ALERT_RC=0
set +e
MISSING_ALERT_STATUS="$(python3 - "$MINIO_MANIFEST_FILE" "$HOST_ALERTS_FILE" "$BACKUP_NAME" "$HOST_ALERTS_FALLBACK" <<'PY'
import datetime as dt
import json
import os
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
alerts_path = Path(os.path.expanduser(sys.argv[2]))
backup_name = sys.argv[3]
fallback_path = Path(os.path.expanduser(sys.argv[4]))

data = json.loads(manifest_path.read_text(encoding="utf-8"))
raw_missing = data.get("missing_referenced_objects") if isinstance(data, dict) else None
if not isinstance(raw_missing, list) or not raw_missing:
    print("NONE\t0")
    raise SystemExit(0)

seen = set()
unique_items = []
for item in raw_missing:
    if not isinstance(item, dict):
        continue
    b = str(item.get("bucket") or "").strip()
    k = str(item.get("key") or "").strip().lstrip("/")
    c = str(item.get("consumer") or "").strip()
    if not b or not k or (b, k) in seen:
        continue
    seen.add((b, k))
    unique_items.append((b, k, c))

if not unique_items:
    print("NONE\t0")
    raise SystemExit(0)

total = len(unique_items)
sample_items = unique_items[:10]
sample = ", ".join(f"{b}/{k}" + (f" [{c}]" if c else "") for b, k, c in sample_items)
suffix = f" (+{total - len(sample_items)} more)" if total > len(sample_items) else ""
ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
line = (
    f"{ts} [WARN] [standalone_backup] [MISSING_REFERENCED_OBJECTS] "
    f"backup={backup_name} Tolerated {total} missing snapshot-referenced object(s) "
    f"(full list in manifest.json): {sample}{suffix}\n"
)

def _append_line(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line)

try:
    _append_line(alerts_path)
    print(f"OK\t{total}\t{alerts_path}")
except OSError as primary_exc:
    if fallback_path != alerts_path:
        try:
            _append_line(fallback_path)
            print(
                f"FALLBACK\t{total}\t{fallback_path}\t{alerts_path}\t{type(primary_exc).__name__}"
            )
            raise SystemExit(0)
        except OSError as fb_exc:
            print(
                f"FAILED\t{total}\t{alerts_path}\t{type(primary_exc).__name__}\t"
                f"{fallback_path}\t{type(fb_exc).__name__}"
            )
            raise SystemExit(0)
    print(f"FAILED\t{total}\t{alerts_path}\t{type(primary_exc).__name__}")
PY
)"
MISSING_ALERT_RC=$?
set -e
if [[ "$MISSING_ALERT_RC" -ne 0 ]]; then
  err "Failed to record MISSING_REFERENCED_OBJECTS alert to ${HOST_ALERTS_FILE} (exit=${MISSING_ALERT_RC}); continuing backup publication; full list stays in manifest.json."
else
  IFS=$'\t' read -r ALERT_STATE ALERT_COUNT ALERT_DEST ALERT_EXTRA1 ALERT_EXTRA2 ALERT_EXTRA3 <<< "$MISSING_ALERT_STATUS"
  case "$ALERT_STATE" in
    OK)
      if [[ "$ALERT_COUNT" =~ ^[0-9]+$ ]] && (( ALERT_COUNT > 0 )); then
        warn "Recorded MISSING_REFERENCED_OBJECTS alert (${ALERT_COUNT} object(s)) to ${ALERT_DEST}; full list stays in manifest.json."
      fi
      ;;
    FALLBACK)
      err "Failed to write MISSING_REFERENCED_OBJECTS alert to ${ALERT_EXTRA1} (${ALERT_EXTRA2}); fell back to ${ALERT_DEST}."
      warn "Recorded MISSING_REFERENCED_OBJECTS alert (${ALERT_COUNT} object(s)) to ${ALERT_DEST}; full list stays in manifest.json."
      ;;
    FAILED)
      if [[ -n "${ALERT_EXTRA2:-}" ]]; then
        err "Failed to write MISSING_REFERENCED_OBJECTS alert (${ALERT_COUNT} object(s)) to ${ALERT_DEST} (${ALERT_EXTRA1}) and fallback ${ALERT_EXTRA2} (${ALERT_EXTRA3}); continuing backup publication; full list stays in manifest.json."
      else
        err "Failed to write MISSING_REFERENCED_OBJECTS alert (${ALERT_COUNT} object(s)) to ${ALERT_DEST} (${ALERT_EXTRA1}); continuing backup publication; full list stays in manifest.json."
      fi
      ;;
  esac
fi

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

ASR_BACKUP_INCLUDED=0
ASR_FILE_COUNT=0
if [[ -f "${STAGE_DIR}/asr-jobs.tar.gz" ]]; then
  ASR_BACKUP_INCLUDED=1
  if [[ "$VERIFY" -eq 1 ]]; then
    ASR_FILE_COUNT="$(tar -tzf "${STAGE_DIR}/asr-jobs.tar.gz" | awk '!/\/$/ && $0 != "." && $0 != "./" { c++ } END { print c+0 }')"
    ok "Terminal ASR jobs archive verified (${ASR_FILE_COUNT} file(s))."
  else
    ok "Terminal ASR jobs archived (verification skipped)."
  fi
fi

# 3. Caddy certificate & PKI data backup (/data on edge)
info "3/5 Backing up Caddy certificate & PKI data..."
compose_cmd exec -T edge tar -czf - -C /data . > "${STAGE_DIR}/caddy-data.tar.gz"

CADDY_FILE_COUNT=0
if [[ "$VERIFY" -eq 1 ]]; then
  CADDY_FILE_COUNT="$(tar -tzf "${STAGE_DIR}/caddy-data.tar.gz" | awk '!/\/$/ && $0 != "." && $0 != "./" { c++ } END { print c+0 }')"
  ok "Caddy data verified (${CADDY_FILE_COUNT} file(s))."
else
  ok "Caddy data archived (verification skipped)."
fi

# 4. Wiki data volume backup (/var/lib/dalton/wiki on research-host: SQLite DBs via online backup API, Markdown, originals, audit receipts)
WIKI_FILE_COUNT=0
WIKI_BACKUP_INCLUDED=0
if compose_cmd config --services 2>/dev/null | grep -qx "research-host"; then
  info "4/5 Backing up Wiki data volume (/var/lib/dalton/wiki on research-host)..."
  WIKI_BACKUP_PY='
from __future__ import annotations
import os, shutil, sqlite3, sys, tarfile, tempfile
from pathlib import Path

root = Path(os.getenv("DALTON_WIKI_DATA_DIR") or "/var/lib/dalton/wiki")
stage = Path(tempfile.mkdtemp(prefix="wiki-backup-"))
try:
    if root.is_dir():
        for dirpath, _dirnames, filenames in os.walk(root):
            rel_dir = Path(dirpath).relative_to(root)
            dst_dir = stage / rel_dir
            dst_dir.mkdir(parents=True, exist_ok=True)
            for fname in filenames:
                if fname.endswith(("-wal", "-shm", "-journal")):
                    continue
                src_f = Path(dirpath) / fname
                dst_f = dst_dir / fname
                if fname.endswith((".db", ".sqlite", ".sqlite3")):
                    src_conn = sqlite3.connect(str(src_f), timeout=30)
                    dst_conn = sqlite3.connect(str(dst_f))
                    try:
                        src_conn.backup(dst_conn)
                    finally:
                        dst_conn.close()
                        src_conn.close()
                else:
                    shutil.copy2(src_f, dst_f)
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz", format=tarfile.PAX_FORMAT) as tf:
        for item in sorted(stage.rglob("*")):
            rel = item.relative_to(stage).as_posix()
            tf.add(str(item), arcname=rel, recursive=False)
finally:
    shutil.rmtree(stage, ignore_errors=True)
'
  if ! acquire_wiki_backup_lock; then
    exit 1
  fi
  if compose_cmd ps --status running --services 2>/dev/null 8>&- 9>&- | grep -qx "research-host" 8>&- 9>&-; then
    WIKI_PAUSE_PORT="$(exec 8>&- 9>&-; read_env_file_var "$ENV_FILE" "DALTON_RESEARCH_HOST_PORT" "8951")"
    if [[ ! "$WIKI_PAUSE_PORT" =~ ^[0-9]+$ ]]; then
      WIKI_PAUSE_PORT="8951"
    fi
    WIKI_PAUSE_ADMIN_TOKEN="$(exec 8>&- 9>&-; read_env_file_var "$ENV_FILE" "DALTON_RESEARCH_ADMIN_TOKEN" "")"
    if [[ -z "$WIKI_PAUSE_ADMIN_TOKEN" ]]; then
      err "DALTON_RESEARCH_ADMIN_TOKEN is missing in ${ENV_FILE}; cannot pause Wiki writes for a consistent online backup."
      exit 1
    fi
    info "Pausing Wiki writes on research-host (owner=${WIKI_PAUSE_OWNER}, ttl=${WIKI_PAUSE_TTL_S}s, heartbeat=${WIKI_PAUSE_HEARTBEAT_S}s) and waiting for in-flight writes to drain..."
    local_pause_sh='payload="$(python3 -c "import json,os; print(json.dumps({\"paused\": True, \"owner\": os.environ[\"DALTON_RH_PAUSE_OWNER\"], \"ttl_s\": float(os.environ[\"DALTON_RH_PAUSE_TTL_S\"])}))")" && body="$(curl -sf --connect-timeout "${DALTON_RH_CURL_TIMEOUT_S}" --max-time "${DALTON_RH_CURL_TIMEOUT_S}" -X POST -H "Content-Type: application/json" -H "Authorization: Bearer ${DALTON_RH_ADMIN_TOKEN}" -d "$payload" "$1/admin/wiki/pause-writes")" && printf "%s" "$body" | python3 -c "import json,os,sys; d=json.loads(sys.stdin.read()); lid=d.get(\"lease_id\") if isinstance(d, dict) else None; ok=isinstance(d, dict) and d.get(\"paused\") is True and d.get(\"owner\") == os.environ[\"DALTON_RH_PAUSE_OWNER\"] and isinstance(lid, int) and lid > 0; sys.exit(1) if not ok else print(lid)"'
    WIKI_PAUSE_LEASE_TMP="$(mktemp "${TMPDIR:-/tmp}/dalton-wiki-pause-lease.XXXXXX")"
    WIKI_PAUSE_PENDING=1
    set +e
    (
      exec 8>&- 9>&-
      RUN_WITH_TIMEOUT_PG_EXEC=1 \
      DALTON_RH_ADMIN_TOKEN="$WIKI_PAUSE_ADMIN_TOKEN" \
      DALTON_RH_PAUSE_OWNER="$WIKI_PAUSE_OWNER" \
      DALTON_RH_PAUSE_TTL_S="$WIKI_PAUSE_TTL_S" \
      DALTON_RH_CURL_TIMEOUT_S="$WIKI_EXEC_TIMEOUT_S" \
      run_with_timeout_pg "$WIKI_EXEC_TIMEOUT_S" \
        env "${ENV_UNSET_ARGS[@]}" docker compose -p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" \
        exec -T -e DALTON_RH_ADMIN_TOKEN -e DALTON_RH_PAUSE_OWNER -e DALTON_RH_PAUSE_TTL_S -e DALTON_RH_CURL_TIMEOUT_S \
        research-host sh -c "$local_pause_sh" _ "http://127.0.0.1:${WIKI_PAUSE_PORT}"
    ) > "$WIKI_PAUSE_LEASE_TMP" 2>/dev/null &
    WIKI_PAUSE_CMD_PID=$!
    wait "$WIKI_PAUSE_CMD_PID"
    wiki_pause_rc=$?
    WIKI_PAUSE_CMD_PID=""
    WIKI_PAUSE_LEASE_ID="$(tr -d '[:space:]' < "$WIKI_PAUSE_LEASE_TMP" 2>/dev/null || true)"
    rm -f "$WIKI_PAUSE_LEASE_TMP" 2>/dev/null || true
    WIKI_PAUSE_LEASE_TMP=""
    set -e
    if [[ "$wiki_pause_rc" -ne 0 || ! "$WIKI_PAUSE_LEASE_ID" =~ ^[0-9]+$ ]]; then
      # Pause POST may have succeeded (or still be landing) on research-host after a timeout
      # or dropped response; probe status by owner within a bounded deadline and resume if held.
      probe_and_resume_pending_wiki_pause || true
      err "Failed to pause Wiki writes via research-host /admin/wiki/pause-writes (owner=${WIKI_PAUSE_OWNER}, exit=${wiki_pause_rc})."
      release_wiki_backup_lock
      exit 1
    fi
    WIKI_WRITES_PAUSED=1
    WIKI_PAUSE_PENDING=0
    start_wiki_pause_heartbeat
    wiki_drained=0
    wiki_in_flight="-1"
    drain_deadline_s="$(exec 8>&- 9>&-; python3 -c 'import sys, time; print(f"{time.monotonic() + max(0.05, float(sys.argv[1])):.6f}")' "$WIKI_DRAIN_TIMEOUT_S")"
    local_drain_sh='body="$(curl -sf --connect-timeout "${DALTON_RH_CURL_TIMEOUT_S}" --max-time "${DALTON_RH_CURL_TIMEOUT_S}" -H "Authorization: Bearer ${DALTON_RH_ADMIN_TOKEN}" "$1/admin/wiki/pause-writes")" || exit 1; printf "%s" "$body" | python3 -c "import json,os,sys; d=json.loads(sys.stdin.read()); ok=isinstance(d, dict) and d.get(\"paused\") is True and str(d.get(\"owner\") or \"\") == os.environ[\"DALTON_RH_EXPECT_OWNER\"] and str(d.get(\"lease_id\") or \"\") == os.environ[\"DALTON_RH_EXPECT_LEASE_ID\"]; print(int(d.get(\"in_flight\", -1)) if ok else \"LEASE_LOST\")"'
    for (( wiki_drain_poll=1; wiki_drain_poll<=WIKI_DRAIN_MAX_POLLS; wiki_drain_poll++ )); do
      if [[ -n "$WIKI_HEARTBEAT_FAILED_FILE" && -f "$WIKI_HEARTBEAT_FAILED_FILE" ]]; then
        err "Wiki write-pause heartbeat failed while draining in-flight writes (owner=${WIKI_PAUSE_OWNER}, lease_id=${WIKI_PAUSE_LEASE_ID}); aborting backup and resuming writes."
        resume_wiki_writes || true
        release_wiki_backup_lock
        exit 1
      fi
      rem_drain_s="$(exec 8>&- 9>&-; python3 -c 'import sys, time; print(f"{max(0.0, float(sys.argv[1]) - time.monotonic()):.6f}")' "$drain_deadline_s")"
      if [[ "$rem_drain_s" == "0.000000" ]]; then
        break
      fi
      poll_timeout_s="$(exec 8>&- 9>&-; python3 -c 'import sys; print(f"{max(0.05, min(float(sys.argv[1]), float(sys.argv[2]))):.6f}")' "$WIKI_EXEC_TIMEOUT_S" "$rem_drain_s")"
      wiki_in_flight="$(exec 8>&- 9>&-; \
        DALTON_RH_ADMIN_TOKEN="$WIKI_PAUSE_ADMIN_TOKEN" \
        DALTON_RH_EXPECT_OWNER="$WIKI_PAUSE_OWNER" \
        DALTON_RH_EXPECT_LEASE_ID="$WIKI_PAUSE_LEASE_ID" \
        DALTON_RH_CURL_TIMEOUT_S="$poll_timeout_s" \
        run_with_timeout_pg "$poll_timeout_s" \
          env "${ENV_UNSET_ARGS[@]}" docker compose -p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" \
          exec -T -e DALTON_RH_ADMIN_TOKEN -e DALTON_RH_EXPECT_OWNER -e DALTON_RH_EXPECT_LEASE_ID -e DALTON_RH_CURL_TIMEOUT_S \
          research-host sh -c "$local_drain_sh" _ "http://127.0.0.1:${WIKI_PAUSE_PORT}" 2>/dev/null || echo "-1")"
      if [[ -n "$WIKI_HEARTBEAT_FAILED_FILE" && -f "$WIKI_HEARTBEAT_FAILED_FILE" ]]; then
        err "Wiki write-pause heartbeat failed while draining in-flight writes (owner=${WIKI_PAUSE_OWNER}, lease_id=${WIKI_PAUSE_LEASE_ID}); aborting backup and resuming writes."
        resume_wiki_writes || true
        release_wiki_backup_lock
        exit 1
      fi
      if [[ "$wiki_in_flight" == "LEASE_LOST" ]]; then
        err "Wiki write-pause lease (owner=${WIKI_PAUSE_OWNER}, lease_id=${WIKI_PAUSE_LEASE_ID}) is no longer active while draining in-flight writes; aborting backup and resuming writes."
        resume_wiki_writes || true
        release_wiki_backup_lock
        exit 1
      fi
      if [[ "$wiki_in_flight" == "0" ]]; then
        wiki_drained=1
        break
      fi
      rem_drain_s="$(exec 8>&- 9>&-; python3 -c 'import sys, time; print(f"{max(0.0, float(sys.argv[1]) - time.monotonic()):.6f}")' "$drain_deadline_s")"
      if [[ "$rem_drain_s" == "0.000000" ]]; then
        break
      fi
      sleep_s="$(exec 8>&- 9>&-; python3 -c 'import sys; print(f"{min(float(sys.argv[1]), float(sys.argv[2])):.6f}")' "$WIKI_DRAIN_POLL_S" "$rem_drain_s")"
      sleep "$sleep_s" 8>&- 9>&-
    done
    if [[ "$wiki_drained" -ne 1 ]]; then
      err "Timed out waiting for in-flight Wiki writes to finish before backup (in_flight=${wiki_in_flight:-unknown}, timeout=${WIKI_DRAIN_TIMEOUT_S}s)."
      resume_wiki_writes || true
      release_wiki_backup_lock
      exit 1
    fi
    set +e
    compose_cmd exec -T research-host uv run --no-sync python -c "$WIKI_BACKUP_PY" > "${STAGE_DIR}/wiki-data.tar.gz" 8>&- 9>&-
    wiki_backup_rc=$?
    resume_wiki_writes
    wiki_resume_rc=$?
    set -e
    heartbeat_failed=0
    if [[ -n "$WIKI_HEARTBEAT_FAILED_FILE" && -f "$WIKI_HEARTBEAT_FAILED_FILE" ]]; then
      heartbeat_failed=1
      rm -f "$WIKI_HEARTBEAT_FAILED_FILE"
    fi
    release_wiki_backup_lock
    if [[ "$wiki_resume_rc" -eq 2 || "$heartbeat_failed" -eq 1 ]]; then
      rm -f "${STAGE_DIR}/wiki-data.tar.gz"
      err "Backup failed because Wiki write-pause lease ${WIKI_PAUSE_LEASE_ID} did not remain active for the entire backup; removed incomplete Wiki archive."
      exit 1
    fi
    if [[ "$wiki_resume_rc" -ne 0 ]]; then
      rm -f "${STAGE_DIR}/wiki-data.tar.gz"
      err "Backup failed because Wiki writes could not be confirmed resumed (backup_exit=${wiki_backup_rc})."
      exit 1
    fi
    if [[ "$wiki_backup_rc" -ne 0 ]]; then
      rm -f "${STAGE_DIR}/wiki-data.tar.gz"
      err "Wiki online backup command failed (exit=${wiki_backup_rc})."
      exit 1
    fi
  else
    set +e
    compose_cmd run --rm -T --no-deps --entrypoint /bin/sh research-host -c 'uv run --no-sync python -c "$1"' _ "$WIKI_BACKUP_PY" > "${STAGE_DIR}/wiki-data.tar.gz" 8>&- 9>&-
    wiki_backup_rc=$?
    set -e
    release_wiki_backup_lock
    if [[ "$wiki_backup_rc" -ne 0 ]]; then
      err "Wiki offline backup command failed (exit=${wiki_backup_rc})."
      exit 1
    fi
  fi
  WIKI_BACKUP_INCLUDED=1
  if [[ "$VERIFY" -eq 1 ]]; then
    WIKI_FILE_COUNT="$(tar -tzf "${STAGE_DIR}/wiki-data.tar.gz" | awk '!/\/$/ && $0 != "." && $0 != "./" { c++ } END { print c+0 }')"
    ok "Wiki data volume verified (${WIKI_FILE_COUNT} file(s))."
  else
    ok "Wiki data volume archived (verification skipped)."
  fi
else
  info "4/5 Skipping Wiki data volume (research-host service not configured in Compose)."
fi

# 5. .env handling (never copy plaintext .env into backup directory)
ENV_BACKUP_STATUS="excluded"
if [[ -n "$ENV_PASSPHRASE_FILE" ]]; then
  info "5/5 Encrypting .env into env.enc (${ENV_CIPHER} + ${ENV_KDF} ${ENV_KDF_ITER} iter)..."
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
  info "5/5 Skipping plaintext .env (secrets are never stored unencrypted in backup folders)."
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
  "$WIKI_BACKUP_INCLUDED" \
  "$WIKI_FILE_COUNT" \
  "$ENV_BACKUP_STATUS" \
  "$ENV_CIPHER" \
  "$ENV_KDF" \
  "$ENV_KDF_DIGEST" \
  "$ENV_KDF_ITER" \
  "$ASR_BACKUP_INCLUDED" \
  "$ASR_FILE_COUNT" <<'PY'
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
    wiki_included,
    wiki_files,
    env_status,
    env_cipher,
    env_kdf,
    env_digest,
    env_iter,
    asr_included,
    asr_files,
) = sys.argv[1:]

with open(minio_manifest_path, encoding="utf-8") as mfh:
    minio_meta = json.load(mfh)

missing_refs = minio_meta.get("missing_referenced_objects")
if not isinstance(missing_refs, list):
    missing_refs = []
expired_by_rule = minio_meta.get("expired_by_rule_objects")
if not isinstance(expired_by_rule, list):
    expired_by_rule = []

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
    "missing_referenced_objects": missing_refs,
    "missing_referenced_count": len(missing_refs),
    "expired_by_rule_objects": expired_by_rule,
    "expired_by_rule_count": len(expired_by_rule),
    "asr_jobs": (
        {
            "file": "asr-jobs.tar.gz",
            "file_count": int(asr_files),
        }
        if int(asr_included) == 1
        else None
    ),
    "caddy": {
        "file": "caddy-data.tar.gz",
        "file_count": int(caddy_files),
    },
    "wiki": (
        {
            "file": "wiki-data.tar.gz",
            "file_count": int(wiki_files),
        }
        if int(wiki_included) == 1
        else None
    ),
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
  for f in postgres.dump minio-objects.tar.gz asr-jobs.tar.gz caddy-data.tar.gz wiki-data.tar.gz manifest.json env.enc; do
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
  RAND_SUFFIX="$(exec 7>&-; python3 -c 'import secrets; print(secrets.token_hex(3))')"
  BACKUP_NAME="dalton-backup-${TIMESTAMP}-${RAND_SUFFIX}"
  FINAL_DIR="${OUTPUT_DIR}/${BACKUP_NAME}"
done
mv "$STAGE_DIR" "$FINAL_DIR"
chmod 700 "$FINAL_DIR"

# Prune older backups belonging to this instance (must have a valid manifest.json)
PRUNED=0
if (( RETAIN > 0 )); then
  PRUNE_LIST="$(exec 7>&-; python3 - "$OUTPUT_DIR" "$PROJECT_NAME" "$RETAIN" <<'PY'
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
if [[ -n "$ENV_PASSPHRASE_FILE" ]]; then
  RESTORE_HINT_ARGS+=(--env-passphrase-file "$ENV_PASSPHRASE_FILE")
fi
if [[ "$COMPOSE_FILES_EXPLICIT" -eq 1 ]]; then
  RESTORE_HINT_ARGS+=(--compose-files "$COMPOSE_FILES_RAW")
fi
if [[ "$PROJECT_NAME_EXPLICIT" -eq 1 ]]; then
  RESTORE_HINT_ARGS+=(--project-name "$PROJECT_NAME")
fi

printf "\n== Backup Complete ==\n"
printf "  Backup directory : %s\n" "$FINAL_DIR"
printf "  PostgreSQL dump  : %s bytes (%s TOC entries)\n" "$PG_DUMP_BYTES" "$PG_TOC_ENTRIES"
printf "  MinIO objects    : %s object(s) across %s bucket(s)\n" "$MINIO_OBJECT_COUNT" "$MINIO_BUCKET_COUNT"
printf "  Caddy data       : %s file(s)\n" "$CADDY_FILE_COUNT"
if [[ "$WIKI_BACKUP_INCLUDED" -eq 1 ]]; then
  printf "  Wiki data        : %s file(s)\n" "$WIKI_FILE_COUNT"
fi
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
