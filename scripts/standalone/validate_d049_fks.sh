#!/usr/bin/env bash
# Standalone Core post-deploy FK validator for d049_weixin_multi_account.
# Delegates to shared Python logic in src/dalton/database/weixin_fk_validate.py.
# Run manually after upgrade/startup (not wired into upgrade.sh in Batch A).
#
# Default DSN resolution:
# - If DALTON_FK_VALIDATE_DSN is set on the host, connects directly with that DSN.
# - Otherwise runs inside the compose `backend` service (`docker compose ... exec -T backend`)
#   where `src.server.database.pool.get_db_connection_string()` reads DB_HOST/DB_PORT/DB_NAME/
#   DB_USER/DB_PASSWORD (or DATABASE_URL) loaded from --env-file (default: ${WORK_DIR}/.env).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="${DALTON_WORK_DIR:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
ENV_FILE="${DALTON_ENV_FILE:-${WORK_DIR}/.env}"
PROJECT_NAME="${DALTON_PROJECT_NAME:-}"
LOCK_TIMEOUT="${DALTON_FK_VALIDATE_LOCK_TIMEOUT:-5s}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --project-name) PROJECT_NAME="$2"; shift 2 ;;
    --lock-timeout) LOCK_TIMEOUT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

cd "${WORK_DIR}"

if [[ -n "${DALTON_FK_VALIDATE_DSN:-}" ]]; then
  exec uv run --no-sync python -m src.dalton.database.weixin_fk_validate \
    --dsn "${DALTON_FK_VALIDATE_DSN}" \
    --lock-timeout "${LOCK_TIMEOUT}"
fi

COMPOSE_ARGS=(-f docker-compose.yml -f docker-compose.core.yml --env-file "${ENV_FILE}")
if [[ -n "${PROJECT_NAME}" ]]; then
  COMPOSE_ARGS=(-p "${PROJECT_NAME}" "${COMPOSE_ARGS[@]}")
fi

exec docker compose "${COMPOSE_ARGS[@]}" exec -T backend \
  uv run --no-sync python -m src.dalton.database.weixin_fk_validate \
    --lock-timeout "${LOCK_TIMEOUT}"
