#!/usr/bin/env bash
# Standalone Core E2E check runner.
#
# Scans `tests/standalone/checks/b*.py`, groups scripts by their `# auth: <mode>`
# file header (`oss`, `local`), brings up a clean Docker Compose stack per auth
# group (migrated to current Alembic heads), runs every matching check script,
# collects logs/artifacts, and tears the stack down between groups.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

WORK_DIR="$PWD"
CHECKS_DIR="${SCRIPT_DIR}/checks"
CI_COMPOSE_FILE="${REPO_ROOT}/docker-compose.ci.yml"
ARTIFACTS_DIR="${WORK_DIR}/ci-artifacts"
AUTH_FILTER="all"
KEEP_UP=0

usage() {
  cat <<'EOF'
Usage: tests/standalone/run_checks.sh [options]

Options:
  --work-dir <dir>        Directory with docker-compose.yml, docker-compose.core.yml, .env (default: $PWD)
  --checks-dir <dir>      Directory containing b*.py check scripts (default: tests/standalone/checks)
  --ci-compose <file>     Path to docker-compose.ci.yml (default: <repo>/docker-compose.ci.yml)
  --artifacts-dir <dir>   Directory for logs and exported baseline dumps (default: <work-dir>/ci-artifacts)
  --auth <all|oss|local>  Run only the named auth group (default: all)
  --keep-up               Do not tear down the last stack on exit
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-dir)
      WORK_DIR="$2"
      shift 2
      ;;
    --checks-dir)
      CHECKS_DIR="$2"
      shift 2
      ;;
    --ci-compose)
      CI_COMPOSE_FILE="$2"
      shift 2
      ;;
    --artifacts-dir)
      ARTIFACTS_DIR="$2"
      shift 2
      ;;
    --auth)
      AUTH_FILTER="$2"
      shift 2
      ;;
    --keep-up)
      KEEP_UP=1
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

WORK_DIR="$(cd "$WORK_DIR" && pwd)"
CHECKS_DIR="$(cd "$CHECKS_DIR" && pwd)"
CI_COMPOSE_FILE="$(cd "$(dirname "$CI_COMPOSE_FILE")" && pwd)/$(basename "$CI_COMPOSE_FILE")"
mkdir -p "$ARTIFACTS_DIR"
ARTIFACTS_DIR="$(cd "$ARTIFACTS_DIR" && pwd)"

BASE_COMPOSE="${WORK_DIR}/docker-compose.yml"
CORE_COMPOSE="${WORK_DIR}/docker-compose.core.yml"

for required in "$BASE_COMPOSE" "$CORE_COMPOSE" "$CI_COMPOSE_FILE"; do
  if [[ ! -f "$required" ]]; then
    echo "ERROR: Required compose file not found: $required" >&2
    exit 1
  fi
done

PYTHON_BIN="${PYTHON_BIN:-python3}"

# Discover and group check scripts by `# auth: <mode>` header.
GROUP_MANIFEST="$("$PYTHON_BIN" - "$CHECKS_DIR" "$AUTH_FILTER" <<'PY'
import json
import sys
from pathlib import Path

checks_dir = Path(sys.argv[1])
auth_filter = sys.argv[2]
sys.path.insert(0, str(checks_dir))
import _lib

groups = _lib.discover_checks(checks_dir)
if auth_filter != "all":
    groups = {k: v for k, v in groups.items() if k == auth_filter}

for mode, paths in groups.items():
    print(f"{mode}\t" + "\t".join(str(p) for p in paths))
PY
)"

if [[ -z "$GROUP_MANIFEST" ]]; then
  echo "ERROR: No check scripts matched (checks_dir=${CHECKS_DIR}, auth=${AUTH_FILTER})" >&2
  exit 1
fi

export CI_REPO_ROOT="$REPO_ROOT"
export STANDALONE_WORK_DIR="$WORK_DIR"
export STANDALONE_ARTIFACTS_DIR="$ARTIFACTS_DIR"
export STANDALONE_COMPOSE_FILES="${BASE_COMPOSE}:${CORE_COMPOSE}:${CI_COMPOSE_FILE}"

compose_run() {
  docker compose \
    --project-directory "$WORK_DIR" \
    -f "$BASE_COMPOSE" \
    -f "$CORE_COMPOSE" \
    -f "$CI_COMPOSE_FILE" \
    "$@"
}

collect_logs() {
  local mode="$1"
  local log_file="${ARTIFACTS_DIR}/compose-logs-${mode}.txt"
  {
    echo "=== docker compose ps (auth=${mode}) ==="
    compose_run ps -a || true
    echo ""
    echo "=== docker compose logs (auth=${mode}) ==="
    compose_run logs --no-color --timestamps || true
  } > "$log_file" 2>&1 || true
  echo "[run_checks] Saved stack logs to ${log_file}"
}

OVERALL_RC=0

while IFS=$'\t' read -r auth_mode scripts_rest; do
  [[ -z "$auth_mode" ]] && continue
  echo "================================================================"
  echo "[run_checks] Starting clean stack for auth group: ${auth_mode}"
  echo "================================================================"

  export APP_ENV="core"
  export DALTON_PROFILE="core"
  export DALTON_AUTH_MODE="$auth_mode"
  export STANDALONE_AUTH_MODE="$auth_mode"
  if [[ "$auth_mode" == "oss" ]]; then
    export HOST_MODE="oss"
  else
    export HOST_MODE="oss"
  fi

  # Guarantee a completely fresh empty database for each auth group.
  compose_run down -v --remove-orphans || true

  if ! compose_run up -d; then
    echo "ERROR: Failed to start compose stack for auth=${auth_mode}" >&2
    collect_logs "$auth_mode"
    if [[ "$KEEP_UP" -eq 0 ]]; then
      compose_run down -v --remove-orphans || true
    fi
    exit 1
  fi

  IFS=$'\t' read -r -a check_scripts <<< "$scripts_rest"
  group_rc=0
  for script in "${check_scripts[@]}"; do
    [[ -z "$script" ]] && continue
    echo "[run_checks] Running $(basename "$script") (auth=${auth_mode})..."
    if ! "$PYTHON_BIN" "$script"; then
      echo "ERROR: Check failed: $script (auth=${auth_mode})" >&2
      group_rc=1
      break
    fi
  done

  collect_logs "$auth_mode"

  if [[ "$KEEP_UP" -eq 0 ]]; then
    compose_run down -v --remove-orphans || true
  fi

  if [[ "$group_rc" -ne 0 ]]; then
    OVERALL_RC="$group_rc"
    break
  fi
done <<< "$GROUP_MANIFEST"

if [[ "$OVERALL_RC" -ne 0 ]]; then
  echo "[run_checks] FAILED (exit=${OVERALL_RC})" >&2
  exit "$OVERALL_RC"
fi

echo "[run_checks] ALL AUTH GROUPS PASSED."
