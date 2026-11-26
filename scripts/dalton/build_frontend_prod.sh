#!/usr/bin/env bash
# Build the Dalton/Everflow frontend production bundle into web/dist using VITE_*
# variables from the repository .env (and web/.env.dalton brand defaults).
#
# Usage:
#   bash scripts/dalton/build_frontend_prod.sh
#   bash scripts/dalton/build_frontend_prod.sh --require-supabase
#   bash scripts/dalton/build_frontend_prod.sh --require-supabase --out-dir web/releases/<build-id>
#   DALTON_ENV_FILE=/path/to/.env bash scripts/dalton/build_frontend_prod.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_DIR="${REPO_ROOT}/web"
DIST_DIR="${WEB_DIR}/dist"
ENV_FILE="${DALTON_ENV_FILE:-${REPO_ROOT}/.env}"
DALTON_WEB_ENV="${WEB_DIR}/.env.dalton"

REQUIRE_SUPABASE=0
OUT_DIR_ARG=""
CUSTOM_OUT_DIR=0
CALLER_PWD="$PWD"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --require-supabase)
      REQUIRE_SUPABASE=1
      ;;
    --out-dir)
      if [[ $# -lt 2 || -z "${2:-}" || "${2:-}" == -* ]]; then
        echo "ERROR: --out-dir requires a directory path argument" >&2
        exit 2
      fi
      OUT_DIR_ARG="$2"
      CUSTOM_OUT_DIR=1
      shift
      ;;
    --out-dir=*)
      OUT_DIR_ARG="${1#--out-dir=}"
      if [[ -z "$OUT_DIR_ARG" || "$OUT_DIR_ARG" == -* ]]; then
        echo "ERROR: --out-dir requires a directory path argument" >&2
        exit 2
      fi
      CUSTOM_OUT_DIR=1
      ;;
    -h|--help)
      sed -n '2,10p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$CUSTOM_OUT_DIR" -eq 1 ]]; then
  case "/${OUT_DIR_ARG}/" in
    */../*)
      echo "ERROR: Refusing --out-dir containing '..': ${OUT_DIR_ARG}" >&2
      exit 2
      ;;
  esac
  if [[ "$OUT_DIR_ARG" == /* ]]; then
    _raw_out_dir="$OUT_DIR_ARG"
  else
    _raw_out_dir="${CALLER_PWD}/${OUT_DIR_ARG}"
  fi
  while [[ "$_raw_out_dir" != "/" && "$_raw_out_dir" == */ ]]; do
    _raw_out_dir="${_raw_out_dir%/}"
  done
  if [[ "$_raw_out_dir" == "/" ]]; then
    echo "ERROR: Refusing unsafe --out-dir: ${_raw_out_dir}" >&2
    exit 2
  fi
  if [[ -e "$_raw_out_dir" || -L "$_raw_out_dir" ]]; then
    echo "ERROR: --out-dir target must be a new non-existent directory: ${_raw_out_dir}" >&2
    exit 2
  fi
  _real_web_dir="$(cd "$WEB_DIR" && pwd -P)"
  _real_releases_dir="${_real_web_dir}/releases"
  if [[ -L "${WEB_DIR}/releases" ]]; then
    echo "ERROR: Refusing symlinked releases directory: ${WEB_DIR}/releases" >&2
    exit 2
  fi
  if [[ -e "${WEB_DIR}/releases" && ! -d "${WEB_DIR}/releases" ]]; then
    echo "ERROR: ${WEB_DIR}/releases exists and is not a directory" >&2
    exit 2
  fi
  _out_parent="$(dirname "$_raw_out_dir")"
  _out_base="$(basename "$_raw_out_dir")"
  if [[ -z "$_out_base" || "$_out_base" == "." || "$_out_base" == ".." || "$_out_base" == "current" ]] \
     || ! [[ "$_out_base" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    echo "ERROR: Invalid release directory name in --out-dir: ${_out_base}" >&2
    exit 2
  fi
  if [[ -L "$_out_parent" ]]; then
    echo "ERROR: Refusing symlinked --out-dir parent: ${_out_parent}" >&2
    exit 2
  fi
  if [[ -d "$_out_parent" ]]; then
    _real_parent="$(cd "$_out_parent" && pwd -P)"
    if [[ "$_real_parent" != "$_real_releases_dir" ]]; then
      echo "ERROR: --out-dir must be a new directory directly under web/releases/ (got ${_raw_out_dir})" >&2
      exit 2
    fi
  elif [[ ! -e "$_out_parent" ]]; then
    _grandparent="$(dirname "$_out_parent")"
    _parent_base="$(basename "$_out_parent")"
    if [[ "$_parent_base" != "releases" || ! -d "$_grandparent" || -L "$_grandparent" ]]; then
      echo "ERROR: --out-dir must be a new directory directly under web/releases/ (got ${_raw_out_dir})" >&2
      exit 2
    fi
    _real_grandparent="$(cd "$_grandparent" && pwd -P)"
    if [[ "$_real_grandparent" != "$_real_web_dir" ]]; then
      echo "ERROR: --out-dir must be a new directory directly under web/releases/ (got ${_raw_out_dir})" >&2
      exit 2
    fi
  else
    echo "ERROR: --out-dir parent is not a directory: ${_out_parent}" >&2
    exit 2
  fi
  DIST_DIR="${_real_releases_dir}/${_out_base}"
  if [[ -e "$DIST_DIR" || -L "$DIST_DIR" ]]; then
    echo "ERROR: --out-dir target already exists: ${DIST_DIR}" >&2
    exit 2
  fi
fi

strip_env_value() {
  local val="$1"
  # Trim leading/trailing whitespace
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  # Handle quoted value + optional trailing comment first, then strip quotes
  if [[ "$val" =~ ^\"([^\"]*)\"([[:space:]]*#.*)?$ ]]; then
    val="${BASH_REMATCH[1]}"
  elif [[ "$val" =~ ^\'([^\']*)\'([[:space:]]*#.*)?$ ]]; then
    val="${BASH_REMATCH[1]}"
  else
    # Strip inline comment for unquoted values
    if [[ "$val" == \#* ]]; then
      val=""
    else
      val="${val%%[[:space:]]#*}"
      val="${val%"${val##*[![:space:]]}"}"
    fi
  fi
  if [[ "$val" == *'${'* || "$val" == *'$('* ]]; then
    echo "ERROR: 不支持变量展开，请在 .env 里写字面值 (${val})" >&2
    exit 1
  fi
  printf '%s' "$val"
}

load_vite_vars_from_file() {
  local file="$1"
  local override_existing="$2"
  [[ -f "$file" ]] || return 0

  while IFS= read -r raw_line || [[ -n "$raw_line" ]]; do
    local line="${raw_line#"${raw_line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    line="${line#export }"
    line="${line#"${line%%[![:space:]]*}"}"
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      local key="${BASH_REMATCH[1]}"
      local raw_val="${BASH_REMATCH[2]}"
      case "$key" in
        VITE_*)
          local val
          val="$(strip_env_value "$raw_val")" || exit 1
          if [[ "$override_existing" == "1" || -z "${!key:-}" ]]; then
            export "$key=$val"
          fi
          ;;
        DALTON_AUTH_MODE)
          _ENV_DALTON_AUTH_MODE="$(strip_env_value "$raw_val")" || exit 1
          ;;
        APP_ENV)
          _ENV_APP_ENV="$(strip_env_value "$raw_val")" || exit 1
          ;;
        DALTON_PROFILE)
          _ENV_DALTON_PROFILE="$(strip_env_value "$raw_val")" || exit 1
          ;;
        BRAND_NAME)
          _ENV_BRAND_NAME="$(strip_env_value "$raw_val")" || exit 1
          ;;
        SUPABASE_URL)
          _ENV_SUPABASE_URL="$(strip_env_value "$raw_val")" || exit 1
          ;;
        SUPABASE_PUBLISHABLE_KEY)
          _ENV_SUPABASE_PUBLISHABLE_KEY="$(strip_env_value "$raw_val")" || exit 1
          ;;
        DOCS_SITE_ORIGIN|DALTON_FRONTEND_URL|PUBLIC_APP_URL|FRONTEND_URL)
          local url_val
          url_val="$(strip_env_value "$raw_val")" || exit 1
          if [[ "$override_existing" == "1" || -z "${!key:-}" ]]; then
            export "$key=$url_val"
          fi
          ;;
      esac
    fi
  done < "$file"
}

_ENV_DALTON_AUTH_MODE=""
_ENV_APP_ENV=""
_ENV_DALTON_PROFILE=""
_ENV_BRAND_NAME=""
_ENV_SUPABASE_URL=""
_ENV_SUPABASE_PUBLISHABLE_KEY=""

# 1. Seed brand defaults from web/.env.dalton (if not already set in caller environment)
load_vite_vars_from_file "$DALTON_WEB_ENV" 0

# 2. Load VITE_* (and backend auth/profile fallbacks) from repo .env; VITE_* in .env
#    unconditionally overrides both web/.env.dalton and caller environment variables.
if [[ -f "$ENV_FILE" ]]; then
  echo "[build_frontend_prod] Loading VITE_* configuration from ${ENV_FILE}"
  load_vite_vars_from_file "$ENV_FILE" 1
else
  echo "[build_frontend_prod] WARN: ${ENV_FILE} not found; using current environment and web/.env.dalton defaults." >&2
fi

trim_whitespace() {
  local val="$1"
  val="${val#"${val%%[![:space:]]*}"}"
  val="${val%"${val##*[![:space:]]}"}"
  printf '%s' "$val"
}

# Resolve ONE authoritative profile strictly from backend APP_ENV / DALTON_PROFILE.
# Legacy VITE_DALTON_PROFILE is ignored as a source and rejected when it conflicts.
_EFFECTIVE_DALTON_PROFILE="$(trim_whitespace "${DALTON_PROFILE:-${_ENV_DALTON_PROFILE:-}}")"
_EFFECTIVE_APP_ENV="$(trim_whitespace "${APP_ENV:-${_ENV_APP_ENV:-}}")"
_LOWER_DALTON_PROFILE="$(printf '%s' "$_EFFECTIVE_DALTON_PROFILE" | tr '[:upper:]' '[:lower:]')"
_LOWER_APP_ENV="$(printf '%s' "$_EFFECTIVE_APP_ENV" | tr '[:upper:]' '[:lower:]')"

if [[ -n "$_LOWER_DALTON_PROFILE" && "$_LOWER_DALTON_PROFILE" != "core" && "$_LOWER_DALTON_PROFILE" != "everflow" ]]; then
  echo "ERROR: Invalid DALTON_PROFILE='${_EFFECTIVE_DALTON_PROFILE}' (expected 'core' or 'everflow')." >&2
  exit 1
fi

_APP_ENV_PROFILE=""
if [[ -n "$_LOWER_APP_ENV" ]]; then
  if [[ "$_LOWER_APP_ENV" == "core" ]]; then
    _APP_ENV_PROFILE="core"
  else
    _APP_ENV_PROFILE="everflow"
  fi
fi

if [[ -n "$_LOWER_DALTON_PROFILE" && -n "$_APP_ENV_PROFILE" && "$_LOWER_DALTON_PROFILE" != "$_APP_ENV_PROFILE" ]]; then
  echo "ERROR: Conflicting backend profile settings: DALTON_PROFILE='${_EFFECTIVE_DALTON_PROFILE}' vs APP_ENV='${_EFFECTIVE_APP_ENV}'." >&2
  exit 1
fi

if [[ -n "$_LOWER_DALTON_PROFILE" ]]; then
  _RESOLVED_PROFILE="$_LOWER_DALTON_PROFILE"
elif [[ -n "$_APP_ENV_PROFILE" ]]; then
  _RESOLVED_PROFILE="$_APP_ENV_PROFILE"
else
  _RESOLVED_PROFILE="everflow"
fi

_LEGACY_VITE_PROFILE="$(trim_whitespace "${VITE_DALTON_PROFILE:-}" | tr '[:upper:]' '[:lower:]')"
if [[ -n "$_LEGACY_VITE_PROFILE" && "$_LEGACY_VITE_PROFILE" != "$_RESOLVED_PROFILE" ]]; then
  echo "ERROR: Legacy VITE_DALTON_PROFILE='${VITE_DALTON_PROFILE}' conflicts with backend profile '${_RESOLVED_PROFILE}' (from APP_ENV/DALTON_PROFILE). Remove VITE_DALTON_PROFILE or align APP_ENV/DALTON_PROFILE." >&2
  exit 1
fi

export DALTON_PROFILE="${_RESOLVED_PROFILE}"
export VITE_DALTON_PROFILE="${_RESOLVED_PROFILE}"

if [[ "$_RESOLVED_PROFILE" == "core" ]]; then
  _RESOLVED_BRAND="${BRAND_NAME:-${_ENV_BRAND_NAME:-}}"
  if [[ -z "$_RESOLVED_BRAND" ]]; then
    if [[ -n "${VITE_BRAND_NAME:-}" && "${VITE_BRAND_NAME}" != "Everflow" ]]; then
      _RESOLVED_BRAND="${VITE_BRAND_NAME}"
    else
      _RESOLVED_BRAND="Dalton"
    fi
  fi
  export BRAND_NAME="${_RESOLVED_BRAND}"
  export VITE_BRAND_NAME="${_RESOLVED_BRAND}"
  if [[ -z "${VITE_BRAND_MARK:-}" || "${VITE_BRAND_MARK}" == "everflow" ]]; then
    export VITE_BRAND_MARK="dalton"
  fi
else
  export VITE_BRAND_NAME="${VITE_BRAND_NAME:-${BRAND_NAME:-${_ENV_BRAND_NAME:-Everflow}}}"
  export BRAND_NAME="${BRAND_NAME:-${_ENV_BRAND_NAME:-${VITE_BRAND_NAME}}}"
  export VITE_BRAND_MARK="${VITE_BRAND_MARK:-everflow}"
fi
export VITE_DALTON_AUTH="${VITE_DALTON_AUTH:-${_ENV_DALTON_AUTH_MODE}}"
export VITE_SUPABASE_URL="${VITE_SUPABASE_URL:-${_ENV_SUPABASE_URL}}"
export VITE_SUPABASE_PUBLISHABLE_KEY="${VITE_SUPABASE_PUBLISHABLE_KEY:-${_ENV_SUPABASE_PUBLISHABLE_KEY}}"
export DOCS_SITE_ORIGIN="${DOCS_SITE_ORIGIN:-${DALTON_FRONTEND_URL:-${PUBLIC_APP_URL:-${FRONTEND_URL:-}}}}"

# 3. Validate auth env consistency (docs/dalton/dev-guide/AUTH-DEPLOY.md)
if [[ "$VITE_DALTON_AUTH" == "supabase" ]]; then
  if [[ -z "$VITE_SUPABASE_URL" || -z "$VITE_SUPABASE_PUBLISHABLE_KEY" ]]; then
    echo "ERROR: VITE_DALTON_AUTH=supabase requires both VITE_SUPABASE_URL and VITE_SUPABASE_PUBLISHABLE_KEY to be non-empty." >&2
    echo "       See docs/dalton/dev-guide/AUTH-DEPLOY.md §1." >&2
    exit 1
  fi
else
  if [[ "$REQUIRE_SUPABASE" -eq 1 ]]; then
    echo "ERROR: --require-supabase specified, but VITE_DALTON_AUTH='${VITE_DALTON_AUTH}' (expected 'supabase')." >&2
    exit 1
  fi
  echo "[build_frontend_prod] WARN: VITE_DALTON_AUTH='${VITE_DALTON_AUTH:-<empty>}' (not 'supabase')." >&2
  echo "[build_frontend_prod] WARN: Bundle will use local no-login mode; set VITE_DALTON_AUTH=supabase in .env before public edge exposure." >&2
fi

key_status="unset"
if [[ -n "$VITE_SUPABASE_PUBLISHABLE_KEY" ]]; then
  key_status="set (len=${#VITE_SUPABASE_PUBLISHABLE_KEY})"
fi

echo "[build_frontend_prod] Build settings:"
echo "  DIST_DIR=${DIST_DIR}"
echo "  DALTON_PROFILE=${DALTON_PROFILE}"
echo "  VITE_DALTON_PROFILE=${VITE_DALTON_PROFILE}"
echo "  VITE_BRAND_NAME=${VITE_BRAND_NAME}"
echo "  VITE_BRAND_MARK=${VITE_BRAND_MARK}"
echo "  VITE_DALTON_AUTH=${VITE_DALTON_AUTH:-<empty>}"
echo "  VITE_SUPABASE_URL=${VITE_SUPABASE_URL:-<empty>}"
echo "  VITE_SUPABASE_PUBLISHABLE_KEY=${key_status}"

# 4. Run production build in web/ (single shared `pnpm build` entrypoint for both default web/dist and --out-dir)
_BUILD_LOCK_OWNED=0
_BUILD_CHILD_PID=""
_BUILD_CHILD_PGID=""
_BUILD_RESERVED_DIR=""
_BUILD_LOCK_PY=""
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  _BUILD_LOCK_PY="${REPO_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  _BUILD_LOCK_PY="$(command -v python3)"
fi

_release_build_lock() {
  if [[ "${_BUILD_LOCK_OWNED:-0}" -eq 1 ]]; then
    exec 9>&-
    _BUILD_LOCK_OWNED=0
  fi
}

_terminate_build_child_pgid() {
  local pid="${_BUILD_CHILD_PID:-}" pgid="${_BUILD_CHILD_PGID:-}"
  [[ -n "$pid" ]] || return 0
  pgid="${pgid:-$pid}"
  kill -TERM -- "-${pgid}" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  if [[ -n "$_BUILD_LOCK_PY" ]]; then
    "$_BUILD_LOCK_PY" - "$pgid" "$pid" 9>&- >/dev/null 2>&1 <<'PY' || true
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
try:
    os.setsid()
except OSError:
    pass
pgid, pid = int(sys.argv[1]), int(sys.argv[2])
def _pgid_alive() -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    if not _pgid_alive():
        sys.exit(0)
    time.sleep(0.05)
for fn, target in ((os.killpg, pgid), (os.kill, pid)):
    try:
        fn(target, signal.SIGKILL)
    except OSError:
        pass
kill_deadline = time.monotonic() + 5.0
while time.monotonic() < kill_deadline:
    if not _pgid_alive():
        sys.exit(0)
    time.sleep(0.05)
PY
  fi
  while :; do
    wait "$pid" 2>/dev/null || true
    kill -0 "$pid" 2>/dev/null || break
  done
  _BUILD_CHILD_PID=""
  _BUILD_CHILD_PGID=""
}

_on_build_interrupt() {
  local sig="${1:-TERM}" code=143
  [[ "$sig" == "INT" ]] && code=130
  trap '' INT TERM
  _terminate_build_child_pgid
  if [[ -n "${_BUILD_RESERVED_DIR:-}" && -d "${_BUILD_RESERVED_DIR}" ]]; then
    rm -rf "${_BUILD_RESERVED_DIR}" 2>/dev/null || true
    _BUILD_RESERVED_DIR=""
  fi
  _release_build_lock
  exit "$code"
}

_run_build_step_in_pgid() {
  local rc=0
  if [[ -n "$_BUILD_LOCK_PY" ]]; then
    "$_BUILD_LOCK_PY" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@" 9>&- &
    _BUILD_CHILD_PID=$!
    _BUILD_CHILD_PGID="$_BUILD_CHILD_PID"
    wait "$_BUILD_CHILD_PID" || rc=$?
    _BUILD_CHILD_PID=""
    _BUILD_CHILD_PGID=""
    return "$rc"
  fi
  "$@" 9>&-
}

if [[ "$CUSTOM_OUT_DIR" -eq 1 && -z "$_BUILD_LOCK_PY" ]]; then
  echo "ERROR: python3 is required to coordinate the deploy lock for --out-dir builds" >&2
  exit 1
fi

if [[ -n "$_BUILD_LOCK_PY" ]]; then
  _DEPLOY_LOCK_FILE="${DALTON_DEPLOY_LOCK_FILE:-${HOME:-/tmp}/Library/Logs/Dalton/deploy_main.lock}"
  mkdir -p "$(dirname "$_DEPLOY_LOCK_FILE")"
  _PARENT_LOCK_VERIFIED=0
  if [[ -n "${DALTON_DEPLOY_LOCK_OWNER_PID:-}" ]]; then
    if "$_BUILD_LOCK_PY" - "$_DEPLOY_LOCK_FILE" "${DALTON_DEPLOY_LOCK_OWNER_PID}" <<'PY'
import fcntl, os, sys
lock_file, owner_raw = sys.argv[1:3]
if os.path.exists(f"{lock_file}.rollback-failed"):
    sys.exit(1)
try:
    owner_pid = int(owner_raw)
    os.kill(owner_pid, 0)
except Exception:
    sys.exit(1)
try:
    fd = os.open(lock_file, os.O_RDWR)
except OSError:
    sys.exit(1)
try:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        sys.exit(1)
    except BlockingIOError:
        pass
    content = os.pread(fd, 512, 0).decode("utf-8", errors="replace")
    if "state=rollback_failed" in content:
        sys.exit(1)
    sys.exit(0 if f"pid={owner_pid}" in content else 1)
finally:
    os.close(fd)
PY
    then
      _PARENT_LOCK_VERIFIED=1
    fi
  fi
  if [[ "$_PARENT_LOCK_VERIFIED" -eq 0 ]]; then
    exec 9<>"$_DEPLOY_LOCK_FILE"
    if ! "$_BUILD_LOCK_PY" - "$_DEPLOY_LOCK_FILE" "$$" "$REPO_ROOT" <<'PY'
import datetime, fcntl, os, sys
lock_file, pid_str, repo_root = sys.argv[1:4]
sentinel = f"{lock_file}.rollback-failed"
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    holder = ""
    try:
        with open(lock_file, encoding="utf-8") as fh:
            holder = fh.read().strip()
    except OSError:
        holder = ""
    detail = f" ({holder})" if holder else ""
    print(f"ERROR: another deploy or frontend release build is already running; lock held on {lock_file}{detail}", file=sys.stderr)
    sys.exit(1)
# Re-check rollback failure state AFTER acquiring flock (closes check-then-lock race).
existing = os.pread(9, 1024, 0).decode("utf-8", errors="replace")
if os.path.exists(sentinel) or "state=rollback_failed" in existing:
    print(f"ERROR: previous first-releases-rollout rollback failed ({sentinel}); resolve manually before building", file=sys.stderr)
    sys.exit(1)
os.ftruncate(9, 0)
os.lseek(9, 0, os.SEEK_SET)
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
os.write(9, f"pid={pid_str} started={ts} repo={repo_root} mode=build_frontend_prod\n".encode("utf-8"))
os.fsync(9)
PY
    then
      exec 9>&-
      exit 1
    fi
    _BUILD_LOCK_OWNED=1
    trap '_release_build_lock' EXIT
  fi
  trap '_on_build_interrupt INT' INT
  trap '_on_build_interrupt TERM' TERM
fi

cd "$WEB_DIR"
if [[ "$CUSTOM_OUT_DIR" -eq 1 ]]; then
  mkdir -p "$(dirname "$DIST_DIR")"
  # Reserve the target release directory atomically so two concurrent builds cannot write into the same path.
  if ! mkdir "$DIST_DIR" 2>/dev/null; then
    echo "ERROR: --out-dir target already exists or was reserved concurrently: ${DIST_DIR}" >&2
    exit 2
  fi
  _BUILD_RESERVED_DIR="$DIST_DIR"
  DALTON_VITE_OUT_DIR="$DIST_DIR" _run_build_step_in_pgid pnpm build
else
  unset DALTON_VITE_OUT_DIR
  _run_build_step_in_pgid pnpm build
fi

# 5. Verify dist artifacts required by Caddy edge gateway
if [[ ! -s "${DIST_DIR}/index.html" ]]; then
  echo "ERROR: ${DIST_DIR}/index.html missing or empty after pnpm build" >&2
  exit 1
fi
if [[ ! -s "${DIST_DIR}/version.json" ]]; then
  echo "ERROR: ${DIST_DIR}/version.json missing or empty after pnpm build" >&2
  exit 1
fi
if [[ ! -d "${DIST_DIR}/assets" ]]; then
  echo "ERROR: ${DIST_DIR}/assets directory missing after pnpm build" >&2
  exit 1
fi

echo "[build_frontend_prod] OK: Production bundle written to ${DIST_DIR}"
echo "[build_frontend_prod] version.json: $(tr -d '\n' < "${DIST_DIR}/version.json")"

# 6. Build static docs site (docs-site/) with matching profile and copy output to web/dist/help/
DOCS_DIR="${REPO_ROOT}/docs-site"
HELP_DIST_DIR="${DIST_DIR}/help"
echo "[build_frontend_prod] Building documentation site in ${DOCS_DIR} (DALTON_PROFILE=${DALTON_PROFILE})"
cd "$DOCS_DIR"
rm -rf "${DOCS_DIR}/.astro" "${DOCS_DIR}/dist"
_run_build_step_in_pgid pnpm install --frozen-lockfile
_run_build_step_in_pgid pnpm build
if [[ ! -s "${DOCS_DIR}/dist/index.html" ]]; then
  echo "ERROR: ${DOCS_DIR}/dist/index.html missing or empty after docs build" >&2
  exit 1
fi
rm -rf "$HELP_DIST_DIR"
mkdir -p "$HELP_DIST_DIR"
cp -R "${DOCS_DIR}/dist/." "$HELP_DIST_DIR/"
echo "[build_frontend_prod] OK: Documentation bundle copied to ${HELP_DIST_DIR}"

# 7. Enrich version.json with docs-site content hash and release identity: keep the app
#    entry chunk filename in `entry` (read by staleBuild.tsx) and encode docs/release
#    identity into `build` so docs-only releases also produce a distinct `build`.
_MANIFEST_PY=""
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  _MANIFEST_PY="${REPO_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  _MANIFEST_PY="$(command -v python3)"
fi
if [[ -n "$_MANIFEST_PY" ]]; then
  "$_MANIFEST_PY" - "$DIST_DIR" "$CUSTOM_OUT_DIR" <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

dist_dir = Path(sys.argv[1])
custom_out = sys.argv[2] == "1"
version_file = dist_dir / "version.json"
payload = json.loads(version_file.read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit(f"ERROR: invalid {version_file}: expected JSON object")
entry_raw = payload.get("entry") or payload.get("build")
if not isinstance(entry_raw, str) or not entry_raw.strip():
    raise SystemExit(f"ERROR: invalid {version_file}: missing build/entry field")
entry_id = entry_raw.strip()

help_dir = dist_dir / "help"
hasher = hashlib.sha256()
if help_dir.is_dir():
    for root, dirs, files in os.walk(help_dir):
        dirs.sort()
        for name in sorted(files):
            fp = Path(root) / name
            if fp.is_symlink() or not fp.is_file():
                continue
            rel = fp.relative_to(help_dir).as_posix()
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(fp.read_bytes())
            hasher.update(b"\0")
help_hash = hasher.hexdigest()[:16]
payload["entry"] = entry_id
payload["help_hash"] = help_hash
if custom_out:
    release_id = dist_dir.name
    composite = f"{entry_id}+help-{help_hash}@{release_id}"
    payload["release_id"] = release_id
    payload["build"] = composite
    payload["release"] = composite
else:
    composite = f"{entry_id}+help-{help_hash}"
    payload["build"] = composite
    payload["release"] = composite

tmp = version_file.with_name(f"version.json.tmp.{os.getpid()}")
tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
os.replace(tmp, version_file)
PY
elif command -v node >/dev/null 2>&1; then
  node - "$DIST_DIR" "$CUSTOM_OUT_DIR" <<'JS'
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const distDir = process.argv[2];
const customOut = process.argv[3] === '1';
const versionFile = path.join(distDir, 'version.json');
const payload = JSON.parse(fs.readFileSync(versionFile, 'utf8'));
const entryRaw = payload && (payload.entry || payload.build);
if (!payload || typeof entryRaw !== 'string' || !entryRaw.trim()) {
  console.error(`ERROR: invalid ${versionFile}: missing build/entry field`);
  process.exit(1);
}
const entryId = entryRaw.trim();
const helpDir = path.join(distDir, 'help');
const hash = crypto.createHash('sha256');
const walk = (dir) => {
  const entries = fs.readdirSync(dir, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name));
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isSymbolicLink()) continue;
    if (ent.isDirectory()) {
      walk(full);
    } else if (ent.isFile()) {
      const rel = path.relative(helpDir, full).split(path.sep).join('/');
      hash.update(rel);
      hash.update('\0');
      hash.update(fs.readFileSync(full));
      hash.update('\0');
    }
  }
};
if (fs.existsSync(helpDir) && fs.statSync(helpDir).isDirectory()) {
  walk(helpDir);
}
const helpHash = hash.digest('hex').slice(0, 16);
payload.entry = entryId;
payload.help_hash = helpHash;
if (customOut) {
  const releaseId = path.basename(distDir);
  const composite = `${entryId}+help-${helpHash}@${releaseId}`;
  payload.release_id = releaseId;
  payload.build = composite;
  payload.release = composite;
} else {
  const composite = `${entryId}+help-${helpHash}`;
  payload.build = composite;
  payload.release = composite;
}
const tmp = path.join(distDir, `version.json.tmp.${process.pid}`);
fs.writeFileSync(tmp, `${JSON.stringify(payload)}\n`, 'utf8');
fs.renameSync(tmp, versionFile);
JS
else
  echo "ERROR: python3 or node is required to finalize ${DIST_DIR}/version.json" >&2
  exit 1
fi
echo "[build_frontend_prod] version.json (final): $(tr -d '\n' < "${DIST_DIR}/version.json")"
