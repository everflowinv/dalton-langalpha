#!/usr/bin/env bash
# Standalone Core smooth in-place upgrade utility.
#
# Pipeline (Compose-managed PostgreSQL only):
#   1. Take a full zero-downtime pre-upgrade backup using the EXISTING (old)
#      scripts/standalone/backup.sh BEFORE modifying any release files.
#   2. Tag the existing `backend`, `edge`, and `crawler` images as
#      `pre-upgrade-<timestamp>` and snapshot the old release files into
#      `<work-dir>/.upgrade-backup/<timestamp>/`.
#   3. Extract the new release into a temporary directory, validate required
#      files, and then replace the release files in `--work-dir` (preserving
#      `.env`, `backups/`, `.upgrade-backup/`, custom certs, and extra snippets).
#   4. Build updated images (unless `--skip-build`), run `migrate` + `minio-init`,
#      rolling-replace `backend` (wait `/health`), `crawler`, and `edge`, run a
#      plain `docker compose up -d` to converge all services, and verify health
#      and Alembic heads (supporting `--ca-file`).
#
# On failure at any step, prints shell-escaped (`%q`) rollback commands using
# the preserved `pre-upgrade-<timestamp>` image tags, release snapshot, backup
# directory, and exact `--compose-files`.
#
# Usage:
#   bash scripts/standalone/upgrade.sh --release-tar /path/to/dalton-core-v1.2.0.tar.gz
#   bash scripts/standalone/upgrade.sh --release-dir /tmp/dalton-core-next --backup-dir /var/backups/dalton
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
RELEASE_TAR=""
RELEASE_DIR=""
BACKUP_OUTPUT_DIR=""
RETAIN=7
RETAIN_MAX=3650
ENV_PASSPHRASE_FILE="${DALTON_BACKUP_PASSPHRASE_FILE:-}"
COMPOSE_FILES_RAW=""
CA_FILE=""
SKIP_BUILD=0
BUILD_ARGS=()

usage() {
  cat <<'EOF'
Usage: bash scripts/standalone/upgrade.sh (--release-tar <file.tar.gz> | --release-dir <dir>) [OPTIONS]

Options:
  --release-tar <file.tar.gz> New release tarball produced by package_release.sh.
  --release-dir <dir>         Already-extracted new release directory.
  --work-dir <dir>            Active deployment directory (default: repo / release root).
  --backup-dir <dir>          Parent directory for the pre-upgrade backup (default: <work-dir>/backups).
  --retain <N>                Backup retention count passed to backup.sh (default: 7; max: 3650).
  --env-passphrase-file <f>   Passphrase file passed to backup.sh to encrypt .env into env.enc.
  --compose-files <f1:f2>     Colon-separated Docker Compose files
                              (default: from .env COMPOSE_FILE / DALTON_TLS_MODE).
  --ca-file <path>            CA certificate PEM file used to verify HTTPS in custom/auto mode.
  --build-arg <KEY=VAL>       Extra --build-arg passed to `docker compose build` (repeatable).
  --skip-build                Skip `docker compose build` (use when new images are already loaded).
  -h, --help                  Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-tar)
      RELEASE_TAR="$2"
      shift 2
      ;;
    --release-tar=*)
      RELEASE_TAR="${1#--release-tar=}"
      shift
      ;;
    --release-dir)
      RELEASE_DIR="$2"
      shift 2
      ;;
    --release-dir=*)
      RELEASE_DIR="${1#--release-dir=}"
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
    --backup-dir)
      BACKUP_OUTPUT_DIR="$2"
      shift 2
      ;;
    --backup-dir=*)
      BACKUP_OUTPUT_DIR="${1#--backup-dir=}"
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
    --build-arg)
      BUILD_ARGS+=("--build-arg" "$2")
      shift 2
      ;;
    --build-arg=*)
      BUILD_ARGS+=("--build-arg" "${1#--build-arg=}")
      shift
      ;;
    --skip-build)
      SKIP_BUILD=1
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

info() { printf "[upgrade] %s\n" "$1"; }
ok()   { printf "[upgrade] OK: %s\n" "$1"; }
warn() { printf "[upgrade] WARN: %s\n" "$1" >&2; }
err()  { printf "[upgrade] ERROR: %s\n" "$1" >&2; }

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

if [[ -n "$RELEASE_TAR" && -n "$RELEASE_DIR" ]]; then
  err "Specify only one of --release-tar or --release-dir."
  exit 2
fi
if [[ -z "$RELEASE_TAR" && -z "$RELEASE_DIR" ]]; then
  err "Provide --release-tar <file.tar.gz> or --release-dir <dir>."
  usage >&2
  exit 2
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
if [[ -n "$CA_FILE" && ( ! -r "$CA_FILE" || ! -s "$CA_FILE" ) ]]; then
  err "CA file is missing, unreadable, or empty: ${CA_FILE}"
  exit 1
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

read_env_var() {
  local key="$1" default_val="${2:-}"
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
    return 0
  fi
  read_env_file_var "$ENV_FILE" "$key" "$default_val"
}

# External PostgreSQL guard: refuse non-Compose databases before modifying anything.
DB_HOST="$(read_env_var "DB_HOST" "postgres")"
if [[ "$DB_HOST" != "postgres" ]]; then
  err "External PostgreSQL detected (DB_HOST='${DB_HOST}'; expected Compose-managed 'postgres')."
  err "upgrade.sh requires Compose-managed PostgreSQL for pre-upgrade backup and rollback. For external databases, back up with pg_dump and upgrade manually."
  exit 1
fi

TLS_MODE="$(read_env_var "DALTON_TLS_MODE" "loopback")"

if [[ -z "$BACKUP_OUTPUT_DIR" ]]; then
  BACKUP_OUTPUT_DIR="${WORK_DIR}/backups"
fi
if [[ ! -d "$BACKUP_OUTPUT_DIR" ]]; then
  mkdir -p "$BACKUP_OUTPUT_DIR"
  chmod 700 "$BACKUP_OUTPUT_DIR"
fi
BACKUP_OUTPUT_DIR="$(cd "$BACKUP_OUTPUT_DIR" && pwd)"

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
NORMALIZED_COMPOSE_FILES=()
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
  NORMALIZED_COMPOSE_FILES+=("$cf")
done
COMPOSE_FILES_JOINED="$(IFS=':'; echo "${NORMALIZED_COMPOSE_FILES[*]}")"

compose_cmd() {
  docker compose "${COMPOSE_ARGS[@]}" "$@"
}

if [[ -n "$RELEASE_TAR" && ! -f "$RELEASE_TAR" ]]; then
  err "Release tarball not found: ${RELEASE_TAR}"
  exit 1
fi
if [[ -n "$RELEASE_DIR" && ! -d "$RELEASE_DIR" ]]; then
  err "Release directory not found: ${RELEASE_DIR}"
  exit 1
fi

UPGRADE_TS="$(date -u +%Y%m%d-%H%M%SZ)"
PRE_UPGRADE_TAG="pre-upgrade-${UPGRADE_TS}"
SNAPSHOT_DIR="${WORK_DIR}/.upgrade-backup/${UPGRADE_TS}"

PREV_RELEASE_SHA=""
if [[ -f "${WORK_DIR}/.release_git_sha" ]]; then
  PREV_RELEASE_SHA="$(tr -d '[:space:]' < "${WORK_DIR}/.release_git_sha")"
fi

BACKEND_IMAGE="$(read_env_var "DALTON_BACKEND_IMAGE" "dalton-core-backend:local")"
EDGE_IMAGE="$(read_env_var "DALTON_EDGE_IMAGE" "dalton-core-edge:local")"
BACKEND_REPO="${BACKEND_IMAGE%:*}"
EDGE_REPO="${EDGE_IMAGE%:*}"

PRE_BACKEND_TAG=""
PRE_EDGE_TAG=""
PRE_CRAWLER_TAG=""
SNAPSHOT_CREATED=0
FILES_REPLACED=0
IMAGES_BUILT=0
DB_MIGRATED=0

STAGE_RELEASE=""
PRE_UPGRADE_BACKUP=""
UPGRADE_SUCCEEDED=0

print_rollback_steps() {
  printf "\n==================== UPGRADE FAILED — ROLLBACK GUIDE ====================\n" >&2
  if [[ "$FILES_REPLACED" -eq 0 && "$IMAGES_BUILT" -eq 0 && "$DB_MIGRATED" -eq 0 ]]; then
    printf "  Failure occurred BEFORE release files, container images, or the database were modified.\n" >&2
    if [[ -n "$PRE_UPGRADE_BACKUP" && -d "$PRE_UPGRADE_BACKUP" ]]; then
      printf "  Pre-upgrade backup : %s (no restore required)\n" "$PRE_UPGRADE_BACKUP" >&2
    fi
    printf "  Do NOT run restore.sh --replace-data; your running stack and data were not changed.\n" >&2
    printf "  Inspect the error above and check:\n" >&2
    printf "    %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" ps)" >&2
    printf "=========================================================================\n" >&2
    return 0
  fi

  if [[ -n "$PRE_UPGRADE_BACKUP" && -d "$PRE_UPGRADE_BACKUP" ]]; then
    printf "  Pre-upgrade backup : %s\n" "$PRE_UPGRADE_BACKUP" >&2
  fi
  if [[ "$SNAPSHOT_CREATED" -eq 1 && -d "$SNAPSHOT_DIR" ]]; then
    printf "  Release snapshot   : %s\n" "$SNAPSHOT_DIR" >&2
  fi
  if [[ "$DB_MIGRATED" -eq 1 ]]; then
    printf "  NOTE: Database migrations had already started. Writes acknowledged after the pre-upgrade backup are not in that backup.\n\n" >&2
  else
    printf "  NOTE: Failure occurred BEFORE database migrations ran. Do NOT run restore.sh --replace-data\n" >&2
    printf "        (doing so would discard writes made after the backup even though the database was untouched).\n\n" >&2
  fi

  printf "  To roll back safely:\n" >&2
  local step_no=1
  if [[ "$FILES_REPLACED" -eq 1 && "$SNAPSHOT_CREATED" -eq 1 && -d "$SNAPSHOT_DIR" ]]; then
    local rsync_cmd=(
      rsync -a --delete
      --exclude=/.env
      --exclude=/backups
      --exclude=/.upgrade-backup
      --exclude=/.restore-backup
      --exclude=/ci-artifacts
      --exclude=/.git
      --exclude=/deploy/standalone/certs
      --exclude=/deploy/standalone/extra
    )
    if [[ "$DB_MIGRATED" -eq 1 && ! -f "${SNAPSHOT_DIR}/scripts/standalone/restore.sh" ]]; then
      rsync_cmd+=(--exclude=/scripts/standalone/restore.sh)
    fi
    rsync_cmd+=("${SNAPSHOT_DIR}/" "${WORK_DIR}/")
    printf "    %d. Restore the previous release files (removes files newly added by the failed release;\n" "$step_no" >&2
    printf "       preserves .env, backups, .upgrade-backup, .restore-backup, certs, and extra snippets):\n" >&2
    printf "         %s\n" "$(fmt_cmd "${rsync_cmd[@]}")" >&2
    step_no=$(( step_no + 1 ))
  fi
  if [[ -n "$PRE_BACKEND_TAG" || -n "$PRE_EDGE_TAG" || -n "$PRE_CRAWLER_TAG" ]]; then
    printf "    %d. Restore the pre-upgrade container image tags:\n" "$step_no" >&2
    if [[ -n "$PRE_BACKEND_TAG" ]]; then
      printf "         %s\n" "$(fmt_cmd docker tag "$PRE_BACKEND_TAG" "$BACKEND_IMAGE")" >&2
    fi
    if [[ -n "$PRE_EDGE_TAG" ]]; then
      printf "         %s\n" "$(fmt_cmd docker tag "$PRE_EDGE_TAG" "$EDGE_IMAGE")" >&2
    fi
    if [[ -n "$PRE_CRAWLER_TAG" && "$PRE_CRAWLER_TAG" != "$PRE_BACKEND_TAG" ]]; then
      printf "         # Crawler image tag preserved at: %s\n" "$PRE_CRAWLER_TAG" >&2
    fi
    step_no=$(( step_no + 1 ))
  fi
  if [[ "$DB_MIGRATED" -eq 1 && -n "$PRE_UPGRADE_BACKUP" && -d "$PRE_UPGRADE_BACKUP" ]]; then
    local restore_cmd=(
      bash "${WORK_DIR}/scripts/standalone/restore.sh"
      --work-dir "$WORK_DIR"
      --backup-dir "$PRE_UPGRADE_BACKUP"
      --compose-files "$COMPOSE_FILES_JOINED"
      --replace-data
      --skip-edge-build
    )
    if [[ -n "$CA_FILE" ]]; then
      restore_cmd+=(--ca-file "$CA_FILE")
    fi
    printf "    %d. Restore the pre-upgrade database, MinIO objects, and Caddy data:\n" "$step_no" >&2
    printf "         %s\n" "$(fmt_cmd "${restore_cmd[@]}")" >&2
    step_no=$(( step_no + 1 ))
  fi
  printf "    %d. Check service status and logs:\n" "$step_no" >&2
  printf "         %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" ps)" >&2
  printf "         %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" logs --tail=100 backend edge)" >&2
  printf "=========================================================================\n" >&2
}

cleanup() {
  local rc=$?
  if [[ -n "$STAGE_RELEASE" && -d "$STAGE_RELEASE" ]]; then
    rm -rf "$STAGE_RELEASE"
  fi
  if [[ "$rc" -ne 0 && "$UPGRADE_SUCCEEDED" -ne 1 ]]; then
    print_rollback_steps
  fi
}
trap cleanup EXIT

# Pre-stage and validate the new release package in a temporary directory BEFORE running
# backup or modifying WORK_DIR (also supplies backup.sh on first upgrade from Batch 1 / #480).
info "Staging and validating new release package before backing up or modifying ${WORK_DIR}..."
RELEASE_SRC=""
if [[ -n "$RELEASE_TAR" ]]; then
  STAGE_RELEASE="$(mktemp -d)"
  tar -xzf "$RELEASE_TAR" -C "$STAGE_RELEASE"
  if [[ -f "${STAGE_RELEASE}/docker-compose.core.yml" ]]; then
    RELEASE_SRC="$STAGE_RELEASE"
  else
    shopt -s nullglob
    top_dirs=("${STAGE_RELEASE}"/*/)
    shopt -u nullglob
    if [[ "${#top_dirs[@]}" -eq 1 && -f "${top_dirs[0]}docker-compose.core.yml" ]]; then
      RELEASE_SRC="${top_dirs[0]%/}"
    else
      err "Invalid release tarball: docker-compose.core.yml not found at archive root."
      exit 1
    fi
  fi
else
  RELEASE_SRC="$(cd "$RELEASE_DIR" && pwd)"
fi

for req in docker-compose.yml docker-compose.core.yml scripts/standalone/migrate.sh scripts/standalone/backup.sh scripts/standalone/restore.sh deploy/standalone/Caddyfile; do
  if [[ ! -f "${RELEASE_SRC}/${req}" ]]; then
    err "Release package is missing required file: ${req}"
    exit 1
  fi
done
ok "Validated new release package."

# Step 1: Take a full zero-downtime pre-upgrade backup using the OLD backup.sh when present,
# or fall back to the new release package's backup.sh on first upgrade from Batch 1 (#480).
BACKUP_SCRIPT=""
if [[ -f "${WORK_DIR}/scripts/standalone/backup.sh" ]]; then
  BACKUP_SCRIPT="${WORK_DIR}/scripts/standalone/backup.sh"
  info "1/4 Running pre-upgrade backup using existing scripts/standalone/backup.sh..."
else
  BACKUP_SCRIPT="${RELEASE_SRC}/scripts/standalone/backup.sh"
  info "1/4 Existing deployment has no scripts/standalone/backup.sh (first upgrade from #480); running backup.sh from the new release package against the old stack..."
fi
BACKUP_CMD=(
  bash "$BACKUP_SCRIPT"
  --work-dir "$WORK_DIR"
  --output-dir "$BACKUP_OUTPUT_DIR"
  --retain "$RETAIN"
  --compose-files "$COMPOSE_FILES_JOINED"
)
if [[ -n "$ENV_PASSPHRASE_FILE" ]]; then
  BACKUP_CMD+=(--env-passphrase-file "$ENV_PASSPHRASE_FILE")
fi

BACKUP_LOG="$(mktemp)"
if ! "${BACKUP_CMD[@]}" | tee "$BACKUP_LOG"; then
  rm -f "$BACKUP_LOG"
  err "Pre-upgrade backup failed; aborting upgrade before modifying any files or containers."
  exit 1
fi
PRE_UPGRADE_BACKUP="$(awk -F': ' '/Backup directory[[:space:]]*:/ {print $2; exit}' "$BACKUP_LOG" | tr -d '\r\n')"
rm -f "$BACKUP_LOG"
if [[ -z "$PRE_UPGRADE_BACKUP" || ! -d "$PRE_UPGRADE_BACKUP" ]]; then
  err "Could not determine pre-upgrade backup directory from backup.sh output."
  exit 1
fi
ok "Pre-upgrade backup ready at ${PRE_UPGRADE_BACKUP}."

# Step 2: Tag old backend/edge/crawler images as pre-upgrade-<ts> and snapshot old release files
info "2/4 Tagging pre-upgrade images (${PRE_UPGRADE_TAG}) and snapshotting old release files..."
tag_service_image() {
  local svc="$1" target_tag="$2"
  local img_id
  img_id="$(compose_cmd images -q "$svc" 2>/dev/null | head -n 1 || true)"
  if [[ -n "$img_id" ]]; then
    docker tag "$img_id" "$target_tag"
    printf '%s' "$target_tag"
  fi
}

PRE_BACKEND_TAG="$(tag_service_image "backend" "${BACKEND_REPO}:${PRE_UPGRADE_TAG}")"
PRE_EDGE_TAG="$(tag_service_image "edge" "${EDGE_REPO}:${PRE_UPGRADE_TAG}")"
PRE_CRAWLER_TAG="$(tag_service_image "crawler" "dalton-core-crawler:${PRE_UPGRADE_TAG}")"
ok "Tagged pre-upgrade images: backend=${PRE_BACKEND_TAG:-none}, edge=${PRE_EDGE_TAG:-none}, crawler=${PRE_CRAWLER_TAG:-none}."

mkdir -p "${WORK_DIR}/.upgrade-backup"
chmod 700 "${WORK_DIR}/.upgrade-backup"
mkdir -p "$SNAPSHOT_DIR"
chmod 700 "$SNAPSHOT_DIR"
(
  cd "$WORK_DIR"
  tar -cf - \
    --exclude='./.env' \
    --exclude='.env' \
    --exclude='./backups' \
    --exclude='backups' \
    --exclude='./.upgrade-backup' \
    --exclude='.upgrade-backup' \
    --exclude='./.restore-backup' \
    --exclude='.restore-backup' \
    --exclude='./ci-artifacts' \
    --exclude='ci-artifacts' \
    --exclude='./.git' \
    --exclude='.git' \
    --exclude='./deploy/standalone/certs' \
    --exclude='deploy/standalone/certs' \
    --exclude='./deploy/standalone/extra' \
    --exclude='deploy/standalone/extra' \
    .
) | tar -xf - -C "$SNAPSHOT_DIR"
SNAPSHOT_CREATED=1
ok "Snapshotted previous release files to ${SNAPSHOT_DIR}."

# Step 3: Replace release files in WORK_DIR from validated RELEASE_SRC
info "3/4 Applying validated new release files to ${WORK_DIR}..."
if [[ "$RELEASE_SRC" != "$WORK_DIR" ]]; then
  FILES_REPLACED=1
  (
    cd "$RELEASE_SRC"
    tar -cf - \
      --exclude='./.env' \
      --exclude='.env' \
      --exclude='./backups' \
      --exclude='backups' \
      --exclude='./.upgrade-backup' \
      --exclude='.upgrade-backup' \
      --exclude='./.restore-backup' \
      --exclude='.restore-backup' \
      --exclude='./ci-artifacts' \
      --exclude='ci-artifacts' \
      --exclude='./.git' \
      --exclude='.git' \
      --exclude='./deploy/standalone/certs' \
      --exclude='deploy/standalone/certs' \
      --exclude='./deploy/standalone/extra' \
      --exclude='deploy/standalone/extra' \
      .
  ) | tar -xf - -C "$WORK_DIR"
  mkdir -p "${WORK_DIR}/deploy/standalone/certs" "${WORK_DIR}/deploy/standalone/extra"
  ok "Replaced release files in ${WORK_DIR} (.env, backups, .upgrade-backup, .restore-backup, certs, and extra snippets preserved)."
else
  info "Release directory is already ${WORK_DIR}; continuing in place."
fi

NEW_RELEASE_SHA=""
if [[ -f "${WORK_DIR}/.release_git_sha" ]]; then
  NEW_RELEASE_SHA="$(tr -d '[:space:]' < "${WORK_DIR}/.release_git_sha")"
fi

# Step 4: Build, migrate, rolling-replace services, converge with `up -d`, and verify health
info "4/4 Building, migrating, replacing services, and verifying health..."
if [[ "$SKIP_BUILD" -eq 0 ]]; then
  info "Building updated service images (migrate, backend, crawler, edge)..."
  IMAGES_BUILT=1
  build_sha="${NEW_RELEASE_SHA:-standalone}"
  BUILD_GIT_SHA="$build_sha" BUILD_GIT_DIRTY="false" \
    compose_cmd build "${BUILD_ARGS[@]}" migrate backend crawler edge
  ok "Updated images built."
else
  info "Skipping image build (--skip-build)."
fi

info "Running new version database migrations..."
DB_MIGRATED=1
compose_cmd run --rm migrate
compose_cmd run --rm minio-init
ok "Database migrations and bucket check succeeded."

info "Performing rolling replacement of backend, crawler, and edge..."
compose_cmd up -d --no-deps --force-recreate backend

info "Waiting for new backend container to pass /health..."
backend_healthy=0
for _ in $(seq 1 60); do
  if compose_cmd exec -T backend curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    backend_healthy=1
    break
  fi
  sleep 2
done
if [[ "$backend_healthy" -ne 1 ]]; then
  err "New backend failed /health check within 120s."
  exit 1
fi
ok "New backend is healthy."

compose_cmd up -d --no-deps --force-recreate crawler
compose_cmd up -d --no-deps --force-recreate edge
ok "Replaced crawler and edge containers."

# Converge all services (picks up infrastructure image bumps or newly added services)
info "Running plain 'docker compose up -d' to converge all services..."
compose_cmd up -d
ok "All services converged."

# Post-upgrade health & schema verification (same edge probe criteria as restore.sh)
DB_USER="$(read_env_var "DB_USER" "postgres")"
DB_NAME="$(read_env_var "DB_NAME" "postgres")"
DOMAIN="$(read_env_var "DOMAIN" "localhost")"
TLS_MODE="$(read_env_var "DALTON_TLS_MODE" "loopback")"
EDGE_PORT="$(read_env_var "DALTON_EDGE_PORT" "8088")"
EDGE_HTTPS_PORT="$(read_env_var "DALTON_EDGE_HTTPS_PORT" "18443")"

if ! compose_cmd ps --status running --services | grep -qx "edge"; then
  err "Edge container is not running after upgrade."
  exit 1
fi

probe_domain="${DOMAIN#[}"
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
      if curl -sf "${probe_curl_extra[@]}" --cacert "$tmp_ca" --resolve "${probe_url_host}:${EDGE_HTTPS_PORT}:${probe_connect_ip}" \
          "https://${probe_url_host}:${EDGE_HTTPS_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
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
    if curl -sf "${probe_curl_extra[@]}" "${curl_ca_args[@]}" --resolve "${probe_url_host}:${EDGE_HTTPS_PORT}:${probe_connect_ip}" \
        "https://${probe_url_host}:${EDGE_HTTPS_PORT}/api/v1/auth/cli" >/dev/null 2>&1; then
      edge_ok=1
      break
    fi
    sleep 2
  done
fi

if [[ "$edge_ok" -ne 1 ]]; then
  err "Edge gateway failed post-upgrade health probe (mode=${TLS_MODE})."
  if [[ "$TLS_MODE" == "custom" && -z "$CA_FILE" ]]; then
    err "For certificates signed by an enterprise/private CA, pass --ca-file <ca.pem>."
  elif [[ "$TLS_MODE" == "auto" ]]; then
    err "For public ACME mode, verify DNS resolution and inbound TCP ports 80/443."
  fi
  exit 1
fi
ok "Edge gateway probe passed (mode=${TLS_MODE})."

UPSTREAM_VER="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version;" | tr -d '\r\n')"
DALTON_VER="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version_dalton;" | tr -d '\r\n')"
ok "Schema versions at head (upstream=${UPSTREAM_VER}, dalton=${DALTON_VER})."

UPGRADE_SUCCEEDED=1

printf "\n== Upgrade Complete ==\n"
printf "  Release SHA      : %s -> %s\n" "${PREV_RELEASE_SHA:-unknown}" "${NEW_RELEASE_SHA:-unknown}"
printf "  Pre-upgrade bak  : %s\n" "$PRE_UPGRADE_BACKUP"
printf "  Release snapshot : %s\n" "$SNAPSHOT_DIR"
printf "  Pre-upgrade tags : backend=%s edge=%s crawler=%s\n" \
  "${PRE_BACKEND_TAG:-none}" "${PRE_EDGE_TAG:-none}" "${PRE_CRAWLER_TAG:-none}"
printf "  Schema versions  : upstream=%s, dalton=%s\n" "$UPSTREAM_VER" "$DALTON_VER"
printf "  Edge TLS mode    : %s\n" "$TLS_MODE"
printf "\n  Next steps:\n"
printf "  - Verify running containers: %s\n" "$(fmt_cmd docker compose "${COMPOSE_ARGS[@]}" ps)"
printf "  - Keep %s and %s until you confirm normal operation.\n" "$PRE_UPGRADE_BACKUP" "$SNAPSHOT_DIR"
