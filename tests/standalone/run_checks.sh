#!/usr/bin/env bash
# Standalone Core E2E check runner.
#
# Scans `tests/standalone/checks/b*.py`, groups scripts by their `# auth: <mode>`
# file header (`oss`, `local`), brings up a clean Docker Compose stack per auth
# group (migrated to current Alembic heads), verifies the backend container's
# active DALTON_AUTH_MODE matches the group, runs every matching check script,
# collects logs/artifacts (with secret-leak verification), and tears the stack
# down between groups.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

WORK_DIR="$PWD"
CHECKS_DIR="${SCRIPT_DIR}/checks"
CI_COMPOSE_FILE=""
ARTIFACTS_DIR=""
AUTH_FILTER="all"
KEEP_UP=0
PROJECT_NAME=""

usage() {
  cat <<'EOF'
Usage: tests/standalone/run_checks.sh [options]

Options:
  --work-dir <dir>        Directory with docker-compose.yml, docker-compose.core.yml, .env (default: $PWD)
  --checks-dir <dir>      Directory containing b*.py check scripts (default: tests/standalone/checks)
  --ci-compose <file>     Path to docker-compose.ci.yml (default: <work-dir>/docker-compose.ci.yml)
  --artifacts-dir <dir>   Directory for logs and exported baseline dumps (default: <work-dir>/ci-artifacts)
  --auth <all|oss|local>  Run only the named auth group (default: all)
  --project-name <name>   Override generated isolated Compose project name
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
    --project-name)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "ERROR: --project-name requires a non-empty value" >&2
        exit 2
      fi
      PROJECT_NAME="$2"
      shift 2
      ;;
    --project-name=*)
      PROJECT_NAME="${1#--project-name=}"
      if [[ -z "$PROJECT_NAME" ]]; then
        echo "ERROR: --project-name requires a non-empty value" >&2
        exit 2
      fi
      shift
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
if [[ -z "$CI_COMPOSE_FILE" ]]; then
  if [[ -f "${WORK_DIR}/docker-compose.ci.yml" ]]; then
    CI_COMPOSE_FILE="${WORK_DIR}/docker-compose.ci.yml"
  else
    CI_COMPOSE_FILE="${REPO_ROOT}/docker-compose.ci.yml"
  fi
fi
CI_COMPOSE_FILE="$(cd "$(dirname "$CI_COMPOSE_FILE")" && pwd)/$(basename "$CI_COMPOSE_FILE")"
if [[ -z "$ARTIFACTS_DIR" ]]; then
  ARTIFACTS_DIR="${WORK_DIR}/ci-artifacts"
fi
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

# Resolve isolated Compose project name so checks never touch an existing
# deployment's containers, networks, or volumes.
if [[ -z "$PROJECT_NAME" ]]; then
  _ts="$(date -u +%Y%m%d%H%M%S)"
  _rand4="$("$PYTHON_BIN" -c 'import secrets; print(secrets.token_hex(2))')"
  PROJECT_NAME="dalton-core-check-${_ts}-${_rand4}"
fi

# Refuse to run if the target project already has containers or volumes so
# `down -v` can never delete existing deployment data.
existing_containers="$(docker ps -a -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
existing_volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT_NAME}")"
if [[ -n "$existing_containers" || -n "$existing_volumes" ]]; then
  echo "ERROR: Compose project '${PROJECT_NAME}' already has existing Docker containers or volumes; refusing to run so existing deployment data is not deleted." >&2
  if [[ -n "$existing_containers" ]]; then
    echo "  Existing containers: $(printf '%s' "$existing_containers" | tr '\n' ' ')" >&2
  fi
  if [[ -n "$existing_volumes" ]]; then
    echo "  Existing volumes: $(printf '%s' "$existing_volumes" | tr '\n' ' ')" >&2
  fi
  exit 1
fi

# Resolve CI mount root from the directory containing docker-compose.ci.yml so
# exported `--include-ci` trees are 100% self-contained.
export CI_REPO_ROOT="$(cd "$(dirname "$CI_COMPOSE_FILE")" && pwd)"
export COMPOSE_PROJECT_NAME="$PROJECT_NAME"
export SANDBOX_DOCKER_NETWORK="${PROJECT_NAME}_sandbox"
export STANDALONE_WORK_DIR="$WORK_DIR"
export STANDALONE_ARTIFACTS_DIR="$ARTIFACTS_DIR"
export STANDALONE_COMPOSE_FILES="${BASE_COMPOSE}:${CORE_COMPOSE}:${CI_COMPOSE_FILE}"

echo "[run_checks] Using isolated Compose project '${PROJECT_NAME}' (sandbox network: '${SANDBOX_DOCKER_NETWORK}')"

compose_run() {
  docker compose \
    -p "$PROJECT_NAME" \
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

verify_backend_auth_mode() {
  local expected_mode="$1"
  local actual_mode
  actual_mode="$(compose_run exec -T backend uv run --no-sync python -c \
    "from src.dalton.settings import get_dalton_auth_mode; print(get_dalton_auth_mode())" \
    | tr -d '\r\n' | tail -n 1)"
  if [[ "$actual_mode" != "$expected_mode" ]]; then
    echo "ERROR: Backend DALTON_AUTH_MODE mismatch: expected '${expected_mode}', actual '${actual_mode}'" >&2
    return 1
  fi
  echo "[run_checks] Verified backend active DALTON_AUTH_MODE=${actual_mode}"
}

OVERALL_RC=0
STACK_CREATED=0

while IFS=$'\t' read -r auth_mode scripts_rest; do
  [[ -z "$auth_mode" ]] && continue
  echo "================================================================"
  echo "[run_checks] Starting clean stack for auth group: ${auth_mode}"
  echo "================================================================"

  export APP_ENV="core"
  export DALTON_PROFILE="core"
  export DALTON_AUTH_MODE="$auth_mode"
  # The core profile accepts "oss" only with the explicit trial flag.
  # For "local", explicitly export empty DALTON_ALLOW_DEV_OSS_AUTH so it overrides .env,
  # disable Secure cookie flag for loopback HTTP CI, expose the fake-llm operator allowlist
  # to host-side check scripts, and enable the market-data WS route for WS ticket checks.
  if [[ "$auth_mode" == "oss" ]]; then
    export DALTON_ALLOW_DEV_OSS_AUTH=1
    export DALTON_AUTH_COOKIE_SECURE=""
    export CI_GINLIX_DATA_URL=""
  else
    export DALTON_ALLOW_DEV_OSS_AUTH=""
    export DALTON_AUTH_COOKIE_SECURE="false"
    export DALTON_OPERATOR_LLM_ENDPOINTS="${DALTON_OPERATOR_LLM_ENDPOINTS:-http://fake-llm:8099/v1}"
    export CI_GINLIX_DATA_URL="${CI_GINLIX_DATA_URL:-http://header-echo:8098}"
  fi
  export STANDALONE_AUTH_MODE="$auth_mode"
  export HOST_MODE="oss"

  # Only tear down stacks created by this run (when transitioning between auth groups).
  if [[ "$STACK_CREATED" -eq 1 ]]; then
    compose_run down -v --remove-orphans || true
    STACK_CREATED=0
  fi

  STACK_CREATED=1
  if ! compose_run up -d; then
    echo "ERROR: Failed to start compose stack for auth=${auth_mode}" >&2
    collect_logs "$auth_mode"
    if [[ "$KEEP_UP" -eq 0 ]]; then
      compose_run down -v --remove-orphans || true
      STACK_CREATED=0
    fi
    exit 1
  fi

  if ! verify_backend_auth_mode "$auth_mode"; then
    collect_logs "$auth_mode"
    if [[ "$KEEP_UP" -eq 0 ]]; then
      compose_run down -v --remove-orphans || true
      STACK_CREATED=0
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
    STACK_CREATED=0
  fi

  if [[ "$group_rc" -ne 0 ]]; then
    OVERALL_RC="$group_rc"
    break
  fi
done <<< "$GROUP_MANIFEST"

# Fail if any collected log/report artifact contains a secret from .env.
if ! "$PYTHON_BIN" - "$CHECKS_DIR" "$ARTIFACTS_DIR" "${WORK_DIR}/.env" <<'PY'
from pathlib import Path
import sys

checks_dir, artifacts_dir, env_path = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(checks_dir))
import _lib

leaks = _lib.scan_artifacts_for_secrets(artifacts_dir, env_path)
if leaks:
    print("ERROR: Secret leak detected in collected artifacts:", file=sys.stderr)
    for hit in leaks:
        print(f"  - {hit}", file=sys.stderr)
    sys.exit(1)
print("[run_checks] Secret scan passed (0 leaked secrets in artifacts).")
PY
then
  OVERALL_RC=1
fi

if [[ "$OVERALL_RC" -ne 0 ]]; then
  echo "[run_checks] FAILED (exit=${OVERALL_RC})" >&2
  exit "$OVERALL_RC"
fi

echo "[run_checks] ALL AUTH GROUPS PASSED."
