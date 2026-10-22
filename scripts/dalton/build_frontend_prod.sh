#!/usr/bin/env bash
# Build the Dalton/Everflow frontend production bundle into web/dist using VITE_*
# variables from the repository .env (and web/.env.dalton brand defaults).
#
# Usage:
#   bash scripts/dalton/build_frontend_prod.sh
#   bash scripts/dalton/build_frontend_prod.sh --require-supabase
#   DALTON_ENV_FILE=/path/to/.env bash scripts/dalton/build_frontend_prod.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_DIR="${REPO_ROOT}/web"
DIST_DIR="${WEB_DIR}/dist"
ENV_FILE="${DALTON_ENV_FILE:-${REPO_ROOT}/.env}"
DALTON_WEB_ENV="${WEB_DIR}/.env.dalton"

REQUIRE_SUPABASE=0
for arg in "$@"; do
  case "$arg" in
    --require-supabase)
      REQUIRE_SUPABASE=1
      ;;
    -h|--help)
      sed -n '2,9p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

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
        SUPABASE_URL)
          _ENV_SUPABASE_URL="$(strip_env_value "$raw_val")" || exit 1
          ;;
        SUPABASE_PUBLISHABLE_KEY)
          _ENV_SUPABASE_PUBLISHABLE_KEY="$(strip_env_value "$raw_val")" || exit 1
          ;;
      esac
    fi
  done < "$file"
}

_ENV_DALTON_AUTH_MODE=""
_ENV_SUPABASE_URL=""
_ENV_SUPABASE_PUBLISHABLE_KEY=""

# 1. Seed brand defaults from web/.env.dalton (if not already set in caller environment)
load_vite_vars_from_file "$DALTON_WEB_ENV" 0

# 2. Load VITE_* (and backend auth fallbacks) from repo .env; VITE_* in .env
#    unconditionally overrides both web/.env.dalton and caller environment variables.
if [[ -f "$ENV_FILE" ]]; then
  echo "[build_frontend_prod] Loading VITE_* configuration from ${ENV_FILE}"
  load_vite_vars_from_file "$ENV_FILE" 1
else
  echo "[build_frontend_prod] WARN: ${ENV_FILE} not found; using current environment and web/.env.dalton defaults." >&2
fi

export VITE_BRAND_NAME="${VITE_BRAND_NAME:-Everflow}"
export VITE_BRAND_MARK="${VITE_BRAND_MARK:-everflow}"
export VITE_DALTON_AUTH="${VITE_DALTON_AUTH:-${_ENV_DALTON_AUTH_MODE}}"
export VITE_SUPABASE_URL="${VITE_SUPABASE_URL:-${_ENV_SUPABASE_URL}}"
export VITE_SUPABASE_PUBLISHABLE_KEY="${VITE_SUPABASE_PUBLISHABLE_KEY:-${_ENV_SUPABASE_PUBLISHABLE_KEY}}"

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
echo "  VITE_BRAND_NAME=${VITE_BRAND_NAME}"
echo "  VITE_BRAND_MARK=${VITE_BRAND_MARK}"
echo "  VITE_DALTON_AUTH=${VITE_DALTON_AUTH:-<empty>}"
echo "  VITE_SUPABASE_URL=${VITE_SUPABASE_URL:-<empty>}"
echo "  VITE_SUPABASE_PUBLISHABLE_KEY=${key_status}"

# 4. Run production build in web/
cd "$WEB_DIR"
pnpm build

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
