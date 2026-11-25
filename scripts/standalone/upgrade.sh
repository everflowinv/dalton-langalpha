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
PROJECT_NAME_ARG=""
PROJECT_NAME_EXPLICIT=0
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
  --project-name <name>       Explicit Docker Compose project name (overrides .env
                              COMPOSE_PROJECT_NAME and work-dir basename).
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

# Read deployment settings strictly from .env (never shell environment).
DB_HOST="$(read_env_file_var "$ENV_FILE" "DB_HOST" "postgres")"
if [[ "$DB_HOST" != "postgres" ]]; then
  err "External PostgreSQL detected (DB_HOST='${DB_HOST}'; expected Compose-managed 'postgres')."
  err "upgrade.sh requires Compose-managed PostgreSQL for pre-upgrade backup and rollback. For external databases, back up with pg_dump and upgrade manually."
  exit 1
fi

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

if [[ -z "$BACKUP_OUTPUT_DIR" ]]; then
  BACKUP_OUTPUT_DIR="${WORK_DIR}/backups"
fi
if [[ ! -d "$BACKUP_OUTPUT_DIR" ]]; then
  mkdir -p "$BACKUP_OUTPUT_DIR"
  chmod 700 "$BACKUP_OUTPUT_DIR"
fi
BACKUP_OUTPUT_DIR="$(cd "$BACKUP_OUTPUT_DIR" && pwd)"

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
  COMPOSE_FILE_ARGS+=(-f "$cf")
  NORMALIZED_COMPOSE_FILES+=("$cf")
done
COMPOSE_FILES_JOINED="$(IFS=':'; echo "${NORMALIZED_COMPOSE_FILES[*]}")"
COMPOSE_PRINT_ARGS=(-p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE")

# Unset .env keys, compose-interpolated keys, and COMPOSE_*/DB_* variables so
# shell exports never shadow .env (ensuring migrate and psql share the same DB_NAME).
ENV_UNSET_ARGS=()
while IFS= read -r env_key; do
  [[ -n "$env_key" ]] && ENV_UNSET_ARGS+=(-u "$env_key")
done < <(python3 - "$ENV_FILE" "${NORMALIZED_COMPOSE_FILES[@]}" <<'PY'
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

BACKEND_IMAGE="$(read_env_file_var "$ENV_FILE" "DALTON_BACKEND_IMAGE" "dalton-core-backend:local")"
EDGE_IMAGE="$(read_env_file_var "$ENV_FILE" "DALTON_EDGE_IMAGE" "dalton-core-edge:local")"
CRAWLER_IMAGE="$(read_env_file_var "$ENV_FILE" "DALTON_CRAWLER_IMAGE" "$BACKEND_IMAGE")"
BACKEND_REPO="${BACKEND_IMAGE%:*}"
EDGE_REPO="${EDGE_IMAGE%:*}"

PRE_BACKEND_TAG=""
PRE_EDGE_TAG=""
PRE_CRAWLER_TAG=""
SNAPSHOT_CREATED=0
FILES_REPLACED=0
IMAGES_BUILT=0
# Migration state:
#   "before"      - failure occurred strictly BEFORE `migrate` was invoked
#   "rolled_back" - `migrate` failed, versions stayed identical, upstream was already
#                   at head and Dalton was at d046 attempting only pure-transactional d049
#   "migrated"    - both pre/post versions read cleanly and at least one version changed
#   "unknown"     - `migrate` was invoked and either pre/post versions could not be read
#                   OR versions stayed identical without proving pure-transactional d049 only
MIGRATION_STATE="before"
AUTO_RECOVERY_ATTEMPTED=0
AUTO_FILES_RESTORED=0
AUTO_UP_RC=0
AUTO_HEALTH_OK=0
STOPPED_SERVICES=()
PRE_MIGRATE_UP=""
PRE_MIGRATE_DA=""
POST_MIGRATE_UP=""
POST_MIGRATE_DA=""

STAGE_RELEASE=""
PRE_UPGRADE_BACKUP=""
UPGRADE_SUCCEEDED=0
MANUAL_OVERWRITE_REF="docs/dalton/dev-guide/STANDALONE-DEPLOY.md (§6.2 在已有实例上覆盖恢复)"

print_rollback_steps() {
  printf "\n==================== UPGRADE FAILED — ROLLBACK GUIDE ====================\n" >&2
  if [[ "$AUTO_RECOVERY_ATTEMPTED" -eq 1 && "$AUTO_FILES_RESTORED" -eq 1 ]]; then
    if [[ "$MIGRATION_STATE" == "rolled_back" ]]; then
      printf "  Database migration failed cleanly before advancing any Alembic version (upstream was\n" >&2
      printf "  already at head and Dalton was at d046 attempting only pure-transactional d049).\n" >&2
    else
      printf "  Upgrade aborted BEFORE database migrations started (database was not modified).\n" >&2
    fi
    if [[ "$AUTO_UP_RC" -eq 0 && "$AUTO_HEALTH_OK" -eq 1 ]]; then
      printf "  upgrade.sh has ALREADY restored the pre-upgrade release files, container image tags,\n" >&2
      printf "  and restarted the previous services (%s) — backend /health passed.\n" "${STOPPED_SERVICES[*]:-backend crawler}" >&2
    else
      printf "  WARNING: Pre-upgrade release files and container image tags WERE restored, but\n" >&2
      printf "           previous services (%s) did NOT start cleanly or failed /health\n" "${STOPPED_SERVICES[*]:-backend crawler}" >&2
      printf "           (compose up exit=%s, health_ok=%s). Do NOT wipe volumes or restore data.\n" "$AUTO_UP_RC" "$AUTO_HEALTH_OK" >&2
      printf "  Manually restart and inspect the restored previous services:\n" >&2
      printf "    1. %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" up -d "${STOPPED_SERVICES[@]:-backend}")" >&2
      printf "    2. %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" logs --tail=100 backend edge)" >&2
    fi
    if [[ -n "$PRE_UPGRADE_BACKUP" && -d "$PRE_UPGRADE_BACKUP" ]]; then
      printf "  Pre-upgrade backup : %s (no data restore required)\n" "$PRE_UPGRADE_BACKUP" >&2
    fi
    if [[ "$SNAPSHOT_CREATED" -eq 1 && -d "$SNAPSHOT_DIR" ]]; then
      printf "  Release snapshot   : %s\n" "$SNAPSHOT_DIR" >&2
    fi
    printf "  Verify service status and inspect the error above:\n" >&2
    printf "    %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" ps)" >&2
    printf "=========================================================================\n" >&2
    return 0
  fi
  if [[ "$FILES_REPLACED" -eq 0 && "$IMAGES_BUILT" -eq 0 && "$MIGRATION_STATE" == "before" ]]; then
    printf "  Failure occurred BEFORE release files, container images, or the database were modified.\n" >&2
    if [[ -n "$PRE_UPGRADE_BACKUP" && -d "$PRE_UPGRADE_BACKUP" ]]; then
      printf "  Pre-upgrade backup : %s (no data restore required)\n" "$PRE_UPGRADE_BACKUP" >&2
    fi
    printf "  Do NOT wipe volumes or restore data; your running stack and data were not changed.\n" >&2
    printf "  Inspect the error above and check:\n" >&2
    printf "    %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" ps)" >&2
    printf "=========================================================================\n" >&2
    return 0
  fi

  if [[ -n "$PRE_UPGRADE_BACKUP" && -d "$PRE_UPGRADE_BACKUP" ]]; then
    printf "  Pre-upgrade backup : %s\n" "$PRE_UPGRADE_BACKUP" >&2
  fi
  if [[ "$SNAPSHOT_CREATED" -eq 1 && -d "$SNAPSHOT_DIR" ]]; then
    printf "  Release snapshot   : %s\n" "$SNAPSHOT_DIR" >&2
  fi

  if [[ "$MIGRATION_STATE" == "migrated" ]]; then
    printf "  NOTE: Database schema versions changed during migration\n" >&2
    printf "        (upstream: %s -> %s, dalton: %s -> %s).\n" \
      "${PRE_MIGRATE_UP:-unknown}" "${POST_MIGRATE_UP:-unknown}" \
      "${PRE_MIGRATE_DA:-unknown}" "${POST_MIGRATE_DA:-unknown}" >&2
    printf "        Rolling back requires restoring release files/images AND following the manual\n" >&2
    printf "        reset-and-restore steps in %s.\n" "$MANUAL_OVERWRITE_REF" >&2
    printf "        Writes acknowledged after the pre-upgrade backup are not in that backup.\n\n" >&2
  elif [[ "$MIGRATION_STATE" == "unknown" ]]; then
    printf "  NOTE: Database migration state is UNCERTAIN (migrate was started, and Alembic versions were unreadable or unchanged — partial DDL such as autocommit_block may still have committed).\n" >&2
    printf "        Do NOT assume migrations already ran and do NOT wipe volumes yet.\n" >&2
    printf "        First manually check current Alembic versions and schema objects, and compare with %s/manifest.json:\n" "${PRE_UPGRADE_BACKUP:-<pre-upgrade-backup>}" >&2
    printf "          %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" exec -T postgres psql -U "${MIG_DB_USER:-postgres}" -d "${MIG_DB_NAME:-postgres}" -Atqc "SELECT 'upstream=' || version_num FROM alembic_version UNION ALL SELECT 'dalton=' || version_num FROM alembic_version_dalton;")" >&2
    printf "        - If both versions match manifest.json and no partial DDL broke the old code: only restore release files & image tags and run '%s' (do NOT touch data).\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" up -d)" >&2
    printf "        - If either version or schema changed incompatibly: restore release files & image tags, then follow %s.\n\n" "$MANUAL_OVERWRITE_REF" >&2
  else
    printf "  NOTE: Failure occurred BEFORE database migrations started. Do NOT wipe volumes or restore data\n" >&2
    printf "        (only restore release files and container image tags so writes made after the backup are kept).\n\n" >&2
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
    if [[ "$MIGRATION_STATE" != "before" && ! -f "${SNAPSHOT_DIR}/scripts/standalone/restore.sh" ]]; then
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
      printf "         %s\n" "$(fmt_cmd docker tag "$PRE_CRAWLER_TAG" "$CRAWLER_IMAGE")" >&2
    fi
    step_no=$(( step_no + 1 ))
  fi
  if [[ "$MIGRATION_STATE" == "migrated" && -n "$PRE_UPGRADE_BACKUP" && -d "$PRE_UPGRADE_BACKUP" ]]; then
    local restore_cmd=(
      bash "${WORK_DIR}/scripts/standalone/restore.sh"
      --work-dir "$WORK_DIR"
      --backup-dir "$PRE_UPGRADE_BACKUP"
      --compose-files "$COMPOSE_FILES_JOINED"
      --skip-edge-build
    )
    if [[ "$PROJECT_NAME_EXPLICIT" -eq 1 ]]; then
      restore_cmd+=(--project-name "$PROJECT_NAME")
    fi
    if [[ -n "$CA_FILE" ]]; then
      restore_cmd+=(--ca-file "$CA_FILE")
    fi
    printf "    %d. Follow %s to reset and restore from the pre-upgrade backup:\n" "$step_no" "$MANUAL_OVERWRITE_REF" >&2
    printf "         a) Verify backup integrity: (cd %s && sha256sum -c SHA256SUMS)\n" "$(printf '%q' "$PRE_UPGRADE_BACKUP")" >&2
    printf "         b) List and confirm ALL volumes belonging to project '%s':\n" "$PROJECT_NAME" >&2
    printf "            %s\n" "$(fmt_cmd docker volume ls --filter "label=com.docker.compose.project=${PROJECT_NAME}")" >&2
    printf "            Then remove this project's containers and volumes:\n" >&2
    printf "            %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" down -v)" >&2
    printf "         c) Restore into the now-empty instance:\n" >&2
    printf "            %s\n" "$(fmt_cmd "${restore_cmd[@]}")" >&2
    step_no=$(( step_no + 1 ))
  elif [[ "$MIGRATION_STATE" == "before" ]]; then
    printf "    %d. Restart services with the restored files and image tags (without touching data):\n" "$step_no" >&2
    printf "         %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" up -d)" >&2
    step_no=$(( step_no + 1 ))
  fi
  printf "    %d. Check service status and logs:\n" "$step_no" >&2
  printf "         %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" ps)" >&2
  printf "         %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" logs --tail=100 backend edge)" >&2
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
  --project-name "$PROJECT_NAME"
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
  info "Building updated service images (backend, edge)..."
  IMAGES_BUILT=1
  build_sha="${NEW_RELEASE_SHA:-standalone}"
  compose_cmd build --build-arg "BUILD_GIT_SHA=${build_sha}" --build-arg "BUILD_GIT_DIRTY=false" ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} backend edge
  ok "Updated images built."
else
  info "Skipping image build (--skip-build)."
fi

run_with_timeout() {
  local secs="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout -k 2 "$secs" "$@"
  else
    python3 - "$secs" "$@" <<'PY'
import os, signal, subprocess, sys

secs = float(sys.argv[1])
cmd = sys.argv[2:]
proc = subprocess.Popen(cmd, start_new_session=True)
try:
    rc = proc.wait(timeout=secs)
    sys.exit(rc)
except subprocess.TimeoutExpired:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    sys.exit(124)
PY
  fi
}

compose_cmd_timeout() {
  local secs="$1"
  shift
  run_with_timeout "$secs" env "${ENV_UNSET_ARGS[@]}" docker compose -p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$ENV_FILE" --project-directory "$WORK_DIR" "$@"
}

read_alembic_versions() {
  local q_timeout="${DALTON_ALEMBIC_QUERY_TIMEOUT_S:-10}"
  if [[ ! "$q_timeout" =~ ^[0-9]+$ || "$q_timeout" -lt 1 ]]; then
    q_timeout=10
  fi
  local up da
  if ! up="$(compose_cmd_timeout "$q_timeout" exec -T postgres psql -U "$MIG_DB_USER" -d "$MIG_DB_NAME" -v ON_ERROR_STOP=1 -Atqc "SELECT version_num FROM alembic_version ORDER BY version_num;" 2>/dev/null)"; then
    return 1
  fi
  if ! da="$(compose_cmd_timeout "$q_timeout" exec -T postgres psql -U "$MIG_DB_USER" -d "$MIG_DB_NAME" -v ON_ERROR_STOP=1 -Atqc "SELECT version_num FROM alembic_version_dalton ORDER BY version_num;" 2>/dev/null)"; then
    return 1
  fi
  up="$(printf '%s' "$up" | tr -d '[:space:]')"
  da="$(printf '%s' "$da" | tr -d '[:space:]')"
  if [[ -z "$up" || -z "$da" ]]; then
    return 1
  fi
  printf '%s\t%s\n' "$up" "$da"
}

resolve_alembic_head() {
  local mig_dir="$1"
  if [[ ! -d "$mig_dir" ]]; then
    return 1
  fi
  python3 - "$mig_dir" <<'PY' 2>/dev/null
import ast, glob, os, sys

mig_dir = sys.argv[1]
revs: set[str] = set()
parents: set[str] = set()
for path in sorted(glob.glob(os.path.join(mig_dir, "*.py"))):
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except Exception:
        continue
    rev = None
    down = None
    for node in tree.body:
        targets = []
        val = None
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            val = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            val = node.value
        if val is None:
            continue
        if "revision" in targets and isinstance(val, ast.Constant) and isinstance(val.value, str):
            rev = val.value.strip()
        elif "down_revision" in targets:
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                down = [val.value.strip()]
            elif isinstance(val, (ast.Tuple, ast.List)):
                down = [
                    el.value.strip()
                    for el in val.elts
                    if isinstance(el, ast.Constant) and isinstance(el.value, str)
                ]
    if rev:
        revs.add(rev)
        if down:
            parents.update(down)
heads = sorted(revs - parents)
if len(heads) == 1:
    sys.stdout.write(heads[0])
else:
    sys.exit(1)
PY
}

restore_pre_upgrade_files_and_images() {
  # Explicit per-step exit checks so failures propagate even when invoked under `set +e`.
  if [[ "$FILES_REPLACED" -eq 1 && "$SNAPSHOT_CREATED" -eq 1 && -d "$SNAPSHOT_DIR" ]]; then
    if command -v rsync >/dev/null 2>&1; then
      rsync -aI --delete \
        --exclude=/.env \
        --exclude=/backups \
        --exclude=/.upgrade-backup \
        --exclude=/.restore-backup \
        --exclude=/ci-artifacts \
        --exclude=/.git \
        --exclude=/deploy/standalone/certs \
        --exclude=/deploy/standalone/extra \
        "${SNAPSHOT_DIR}/" "${WORK_DIR}/" || return 1
    else
      # Clean replacement without rsync: extract snapshot into a fresh temp stage,
      # remove non-preserved files from WORK_DIR (purging any newly added files from
      # the failed release), then move the clean snapshot tree into WORK_DIR.
      local restore_stage
      restore_stage="$(mktemp -d)" || return 1
      (
        cd "$SNAPSHOT_DIR" || exit 1
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
      ) | tar -xf - -C "$restore_stage"
      local tar_pipe=("${PIPESTATUS[@]}")
      if [[ "${tar_pipe[0]:-1}" -ne 0 || "${tar_pipe[1]:-1}" -ne 0 ]]; then
        rm -rf "$restore_stage"
        return 1
      fi
      python3 - "$WORK_DIR" "$restore_stage" <<'PY'
import os, shutil, sys

work_dir = os.path.abspath(sys.argv[1])
stage_dir = os.path.abspath(sys.argv[2])
preserved_top = {
    ".env",
    "backups",
    ".upgrade-backup",
    ".restore-backup",
    "ci-artifacts",
    ".git",
}
preserved_rel = {
    os.path.normpath("deploy/standalone/certs"),
    os.path.normpath("deploy/standalone/extra"),
}

def _purge_non_preserved(cur_dir: str, rel_prefix: str = "") -> None:
    for name in os.listdir(cur_dir):
        rel = os.path.normpath(os.path.join(rel_prefix, name)) if rel_prefix else name
        full = os.path.join(cur_dir, name)
        if not rel_prefix and name in preserved_top:
            continue
        if rel in preserved_rel:
            continue
        if os.path.isdir(full) and not os.path.islink(full):
            if any(p.startswith(rel + os.sep) for p in preserved_rel):
                _purge_non_preserved(full, rel)
            else:
                shutil.rmtree(full)
        else:
            os.unlink(full)

def _merge_stage(src_dir: str, dst_dir: str) -> None:
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        s_path = os.path.join(src_dir, name)
        d_path = os.path.join(dst_dir, name)
        if os.path.isdir(s_path) and not os.path.islink(s_path):
            _merge_stage(s_path, d_path)
        else:
            if os.path.lexists(d_path):
                if os.path.isdir(d_path) and not os.path.islink(d_path):
                    shutil.rmtree(d_path)
                else:
                    os.unlink(d_path)
            shutil.move(s_path, d_path)

_purge_non_preserved(work_dir)
_merge_stage(stage_dir, work_dir)
PY
      local py_rc=$?
      rm -rf "$restore_stage"
      if [[ "$py_rc" -ne 0 ]]; then
        return 1
      fi
    fi
  fi
  if [[ -n "$PRE_BACKEND_TAG" ]]; then
    docker tag "$PRE_BACKEND_TAG" "$BACKEND_IMAGE" || return 1
  fi
  if [[ -n "$PRE_EDGE_TAG" ]]; then
    docker tag "$PRE_EDGE_TAG" "$EDGE_IMAGE" || return 1
  fi
  if [[ -n "$PRE_CRAWLER_TAG" ]]; then
    docker tag "$PRE_CRAWLER_TAG" "$CRAWLER_IMAGE" || return 1
  fi
  return 0
}

probe_service_health_once() {
  local svc="$1"
  local crawler_port="$2"
  local gw_port="$3"
  local gw_token="$4"
  case "$svc" in
    backend)
      compose_cmd_timeout 10 exec -T backend curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1
      ;;
    crawler)
      compose_cmd_timeout 10 exec -T crawler curl -sf "http://127.0.0.1:${crawler_port}/health" >/dev/null 2>&1
      ;;
    channel-gateway)
      if [[ -z "$gw_token" ]]; then
        return 1
      fi
      # R13/R14 (Codex #1, #9 / Grok high): Gateway readiness is signaled solely by GET /health
      # returning HTTP 200 with JSON `{"ok": true, "ready": true}` (startup snapshot applied
      # and write gate open). Missing `ready` or `ready: false` is treated as not ready until
      # timeout; do not fall back to /agent/settings or substring matching.
      DALTON_HEALTH_TOKEN="$gw_token" compose_cmd_timeout 10 exec -T -e DALTON_HEALTH_TOKEN channel-gateway sh -c \
        'if [ -z "${DALTON_HEALTH_TOKEN:-}" ]; then exit 1; fi; if command -v node >/dev/null 2>&1; then node -e "const base=process.argv[1]; const hdr={\"X-Service-Token\": process.env.DALTON_HEALTH_TOKEN || \"\"}; (async()=>{const r=await fetch(base+\"/health\",{headers:hdr}); if(!r.ok) process.exit(1); const d=await r.json(); process.exit(d&&d.ok===true&&d.ready===true?0:1);})().catch(()=>process.exit(1));" "$1"; elif command -v curl >/dev/null 2>&1 && command -v python3 >/dev/null 2>&1; then body="$(curl -sf -H "X-Service-Token: ${DALTON_HEALTH_TOKEN}" "$1/health")" || exit 1; printf "%s" "$body" | python3 -c "import json,sys; d=json.loads(sys.stdin.read(), parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c))); sys.exit(0 if isinstance(d, dict) and d.get(\"ok\") is True and d.get(\"ready\") is True else 1)" 2>/dev/null; else exit 1; fi' \
        _ "http://127.0.0.1:${gw_port}" >/dev/null 2>&1
      ;;
    *)
      return 0
      ;;
  esac
}

wait_services_healthy() {
  local retries="$1"
  local sleep_s="$2"
  shift 2
  local svcs=("$@")
  LAST_UNHEALTHY_SERVICES=""
  local crawler_port gw_port gw_token
  crawler_port="$(read_env_file_var "$ENV_FILE" "CRAWL_SERVICE_PORT" "8100")"
  if [[ ! "$crawler_port" =~ ^[0-9]+$ ]]; then crawler_port=8100; fi
  gw_port="$(read_env_file_var "$ENV_FILE" "GATEWAY_PORT" "8962")"
  if [[ ! "$gw_port" =~ ^[0-9]+$ ]]; then gw_port=8962; fi
  gw_token="$(read_env_file_var "$ENV_FILE" "INTERNAL_SERVICE_TOKEN" "")"

  for _ in $(seq 1 "$retries"); do
    local running_svcs=""
    running_svcs="$(compose_cmd_timeout 10 ps --status running --services 2>/dev/null || true)"
    local unhealthy_svcs=""
    local svc
    for svc in "${svcs[@]}"; do
      if ! grep -qx "$svc" <<<"$running_svcs"; then
        unhealthy_svcs="${unhealthy_svcs:+${unhealthy_svcs} }${svc}(not-running)"
        continue
      fi
      if ! probe_service_health_once "$svc" "$crawler_port" "$gw_port" "$gw_token"; then
        unhealthy_svcs="${unhealthy_svcs:+${unhealthy_svcs} }${svc}(/health)"
      fi
    done
    if [[ -z "$unhealthy_svcs" ]]; then
      LAST_UNHEALTHY_SERVICES=""
      return 0
    fi
    LAST_UNHEALTHY_SERVICES="$unhealthy_svcs"
    if [[ "$sleep_s" -gt 0 ]]; then
      sleep "$sleep_s"
    fi
  done
  return 1
}

recover_previous_services_after_abort() {
  local target_state="$1"
  AUTO_RECOVERY_ATTEMPTED=1
  MIGRATION_STATE="$target_state"
  set +e
  restore_pre_upgrade_files_and_images
  local restore_rc=$?
  set -e
  if [[ "$restore_rc" -ne 0 ]]; then
    err "Failed to restore pre-upgrade files or image tags (exit ${restore_rc})."
    return 1
  fi
  AUTO_FILES_RESTORED=1

  set +e
  compose_cmd up -d "${STOPPED_SERVICES[@]}"
  AUTO_UP_RC=$?
  set -e
  if [[ "$AUTO_UP_RC" -ne 0 ]]; then
    err "Pre-upgrade files and image tags were restored, but restarting previous services failed (exit ${AUTO_UP_RC})."
    return 1
  fi

  local health_retries="${DALTON_RECOVERY_HEALTH_RETRIES:-15}"
  local health_sleep="${DALTON_RECOVERY_HEALTH_SLEEP_S:-2}"
  if [[ ! "$health_retries" =~ ^[0-9]+$ || "$health_retries" -lt 1 ]]; then
    health_retries=15
  fi
  if [[ ! "$health_sleep" =~ ^[0-9]+$ ]]; then
    health_sleep=2
  fi
  AUTO_HEALTH_OK=0
  info "Verifying restored previous services (${STOPPED_SERVICES[*]}) are running and pass their /health endpoints (up to ${health_retries} attempts)..."
  if wait_services_healthy "$health_retries" "$health_sleep" "${STOPPED_SERVICES[@]}"; then
    AUTO_HEALTH_OK=1
    ok "Restored previous services (${STOPPED_SERVICES[*]}) are healthy."
    return 0
  fi
  err "Restored previous services did not pass health check after ${health_retries} attempts (unhealthy/not running: ${LAST_UNHEALTHY_SERVICES:-unknown})."
  return 1
}

info "Stopping application services before database migrations (prevents lock contention on hot tables)..."
MIG_DB_USER="$(read_env_file_var "$ENV_FILE" "DB_USER" "postgres")"
MIG_DB_NAME="$(read_env_file_var "$ENV_FILE" "DB_NAME" "postgres")"
CONFIG_SERVICES_LIST="$(compose_cmd config --services 2>/dev/null || true)"
STOP_BEFORE_MIGRATE_SERVICES=()
for candidate_svc in channel-gateway backend crawler; do
  if grep -qx "$candidate_svc" <<<"$CONFIG_SERVICES_LIST"; then
    STOP_BEFORE_MIGRATE_SERVICES+=("$candidate_svc")
  fi
done
if [[ "${#STOP_BEFORE_MIGRATE_SERVICES[@]}" -eq 0 ]]; then
  STOP_BEFORE_MIGRATE_SERVICES=(backend crawler)
fi
compose_cmd stop "${STOP_BEFORE_MIGRATE_SERVICES[@]}"
STOPPED_SERVICES=("${STOP_BEFORE_MIGRATE_SERVICES[@]}")
ok "Stopped services before migrate: ${STOPPED_SERVICES[*]}."

MIGRATE_DRAIN_TIMEOUT_S="${DALTON_MIGRATE_DRAIN_TIMEOUT_S:-30}"
if [[ ! "$MIGRATE_DRAIN_TIMEOUT_S" =~ ^[0-9]+$ || "$MIGRATE_DRAIN_TIMEOUT_S" -lt 1 ]]; then
  MIGRATE_DRAIN_TIMEOUT_S=30
fi
MIGRATE_DRAIN_EXEC_TIMEOUT_S="${DALTON_MIGRATE_DRAIN_EXEC_TIMEOUT_S:-10}"
if [[ ! "$MIGRATE_DRAIN_EXEC_TIMEOUT_S" =~ ^[0-9]+$ || "$MIGRATE_DRAIN_EXEC_TIMEOUT_S" -lt 1 ]]; then
  MIGRATE_DRAIN_EXEC_TIMEOUT_S=10
fi
info "Waiting up to ${MIGRATE_DRAIN_TIMEOUT_S}s (wall-clock total) for database connections to drain (per-query cap ${MIGRATE_DRAIN_EXEC_TIMEOUT_S}s)..."
db_drained=0
active_conns=""
drain_start_ts="$(date +%s)"
drain_deadline=$((drain_start_ts + MIGRATE_DRAIN_TIMEOUT_S))
while true; do
  now_ts="$(date +%s)"
  remaining=$((drain_deadline - now_ts))
  if [[ "$remaining" -le 0 ]]; then
    break
  fi
  q_timeout="$MIGRATE_DRAIN_EXEC_TIMEOUT_S"
  if [[ "$remaining" -lt "$q_timeout" ]]; then
    q_timeout="$remaining"
  fi
  active_conns="$(compose_cmd_timeout "$q_timeout" exec -T postgres psql -U "$MIG_DB_USER" -d "$MIG_DB_NAME" -v ON_ERROR_STOP=1 -Atqc "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND state IS NOT NULL;" 2>/dev/null | tr -d '[:space:]' || true)"
  if [[ "$active_conns" == "0" ]]; then
    db_drained=1
    break
  fi
  if [[ ! "$active_conns" =~ ^[0-9]+$ ]]; then
    # The count could not be read as a non-negative integer; stop waiting immediately.
    break
  fi
  now_ts="$(date +%s)"
  if [[ "$now_ts" -ge "$drain_deadline" ]]; then
    break
  fi
  sleep 1
done
if [[ "$db_drained" -eq 1 ]]; then
  ok "Database connections drained cleanly."
else
  err "Could not confirm database connections drained to 0 (last count=${active_conns:-unreadable}); aborting before migrate and restoring previous services..."
  recover_previous_services_after_abort "before" || true
  exit 1
fi

info "Running new version database migrations..."
NEW_UPSTREAM_HEAD="$(resolve_alembic_head "${WORK_DIR}/migrations/versions" || true)"
NEW_DALTON_HEAD="$(resolve_alembic_head "${WORK_DIR}/src/dalton/migrations/versions" || true)"
PRE_MIGRATE_OK=0
if pre_vers="$(read_alembic_versions)"; then
  IFS=$'\t' read -r PRE_MIGRATE_UP PRE_MIGRATE_DA <<< "$pre_vers"
  PRE_MIGRATE_OK=1
else
  warn "Could not read Alembic schema versions before running migrate."
fi

set +e
compose_cmd run --rm migrate
MIGRATE_RC=$?
set -e

POST_MIGRATE_OK=0
if post_vers="$(read_alembic_versions)"; then
  IFS=$'\t' read -r POST_MIGRATE_UP POST_MIGRATE_DA <<< "$post_vers"
  POST_MIGRATE_OK=1
else
  warn "Could not read Alembic schema versions after running migrate."
fi

# Determine migration state once `migrate` has been invoked:
# - If both pre/post versions read cleanly and either changed -> "migrated".
# - Otherwise (unreadable OR unchanged — partial DDL e.g. autocommit_block may
#   have committed before version_num advanced) -> "unknown" (require manual check).
if [[ "$PRE_MIGRATE_OK" -eq 1 && "$POST_MIGRATE_OK" -eq 1 && ( "$POST_MIGRATE_UP" != "$PRE_MIGRATE_UP" || "$POST_MIGRATE_DA" != "$PRE_MIGRATE_DA" ) ]]; then
  MIGRATION_STATE="migrated"
else
  MIGRATION_STATE="unknown"
fi

if [[ "$MIGRATE_RC" -ne 0 ]]; then
  err "Database migrations failed (exit ${MIGRATE_RC})."
  # Safe automatic recovery (§6.3, R7 Decision #5): ONLY when ALL hold:
  #   1) pre & post Alembic versions read cleanly and stayed identical;
  #   2) upstream was ALREADY at the new release's upstream head (no upstream migrations ran);
  #   3) pre-upgrade Dalton version was EXACTLY d046 AND new release Dalton head is d049
  #      (confirming ONLY pure-transactional d049 was attempted and rolled back cleanly).
  # All other cases (e.g. upgrading from <= d044 with autocommit_block migrations) stay
  # MIGRATION_STATE="unknown" and require manual inspection / backup restore.
  if [[ "$PRE_MIGRATE_OK" -eq 1 && "$POST_MIGRATE_OK" -eq 1 \
        && "$POST_MIGRATE_UP" == "$PRE_MIGRATE_UP" \
        && "$POST_MIGRATE_DA" == "$PRE_MIGRATE_DA" \
        && -n "$NEW_UPSTREAM_HEAD" \
        && "$PRE_MIGRATE_UP" == "$NEW_UPSTREAM_HEAD" \
        && "$PRE_MIGRATE_DA" == "d046" \
        && "$NEW_DALTON_HEAD" == "d049" ]]; then
    warn "Alembic versions unchanged (upstream at head ${NEW_UPSTREAM_HEAD}, Dalton d046 -> d049 only); automatically restoring pre-upgrade files, image tags, and services..."
    recover_previous_services_after_abort "rolled_back" || true
  fi
  exit "$MIGRATE_RC"
fi
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

# Post-upgrade application service /health verification (crawler + configured channel-gateway)
POST_UPGRADE_HEALTH_SERVICES=(crawler)
if grep -qx "channel-gateway" <<<"$CONFIG_SERVICES_LIST"; then
  POST_UPGRADE_HEALTH_SERVICES+=(channel-gateway)
fi
POST_UPGRADE_HEALTH_RETRIES="${DALTON_POST_UPGRADE_HEALTH_RETRIES:-30}"
POST_UPGRADE_HEALTH_SLEEP_S="${DALTON_POST_UPGRADE_HEALTH_SLEEP_S:-2}"
if [[ ! "$POST_UPGRADE_HEALTH_RETRIES" =~ ^[0-9]+$ || "$POST_UPGRADE_HEALTH_RETRIES" -lt 1 ]]; then
  POST_UPGRADE_HEALTH_RETRIES=30
fi
if [[ ! "$POST_UPGRADE_HEALTH_SLEEP_S" =~ ^[0-9]+$ ]]; then
  POST_UPGRADE_HEALTH_SLEEP_S=2
fi
info "Verifying upgraded services (${POST_UPGRADE_HEALTH_SERVICES[*]}) are running and pass /health..."
if ! wait_services_healthy "$POST_UPGRADE_HEALTH_RETRIES" "$POST_UPGRADE_HEALTH_SLEEP_S" "${POST_UPGRADE_HEALTH_SERVICES[@]}"; then
  err "Upgraded services failed post-upgrade /health check (unhealthy/not running: ${LAST_UNHEALTHY_SERVICES:-unknown})."
  exit 1
fi
ok "Upgraded services (${POST_UPGRADE_HEALTH_SERVICES[*]}) are healthy."

# Post-upgrade health & schema verification (strictly from .env, same DB_NAME as migrate)
DB_USER="$(read_env_file_var "$ENV_FILE" "DB_USER" "postgres")"
DB_NAME="$(read_env_file_var "$ENV_FILE" "DB_NAME" "postgres")"
DOMAIN="$(read_env_file_var "$ENV_FILE" "DOMAIN" "localhost")"
TLS_MODE="$(read_env_file_var "$ENV_FILE" "DALTON_TLS_MODE" "loopback")"
EDGE_PORT="$(read_env_file_var "$ENV_FILE" "DALTON_EDGE_PORT" "8088")"
EDGE_HTTPS_PORT="$(read_env_file_var "$ENV_FILE" "DALTON_EDGE_HTTPS_PORT" "18443")"
EDGE_TLS_BIND="$(read_env_file_var "$ENV_FILE" "DALTON_EDGE_TLS_BIND" "")"

if ! compose_cmd ps --status running --services | grep -qx "edge"; then
  err "Edge container is not running after upgrade."
  exit 1
fi

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

probe_domain="${DOMAIN#[}"
probe_domain="${probe_domain%]}"
probe_url_host="$probe_domain"
probe_curl_extra=()
if [[ "$probe_domain" == *:* ]]; then
  probe_url_host="[${probe_domain}]"
  probe_curl_extra=(-g)
else
  probe_resolve_addr="$(resolve_probe_bind_addr "$EDGE_TLS_BIND")"
  probe_curl_extra=(--resolve "${probe_url_host}:${EDGE_HTTPS_PORT}:${probe_resolve_addr}")
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
      if curl -sf "${probe_curl_extra[@]}" --cacert "$tmp_ca" \
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
    if curl -sf "${probe_curl_extra[@]}" ${curl_ca_args[@]+"${curl_ca_args[@]}"} \
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

UPSTREAM_VER="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version ORDER BY version_num;" | tr -d '\r\n')"
DALTON_VER="$(compose_cmd exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atqc "SELECT version_num FROM alembic_version_dalton ORDER BY version_num;" | tr -d '\r\n')"
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
printf "  - Verify running containers: %s\n" "$(fmt_cmd docker compose "${COMPOSE_PRINT_ARGS[@]}" ps)"
printf "  - Keep %s and %s until you confirm normal operation.\n" "$PRE_UPGRADE_BACKUP" "$SNAPSHOT_DIR"
