#!/usr/bin/env bash
# Everflow production post-deploy FK validator for d049_weixin_multi_account.
# Delegates to shared Python logic in src/dalton/database/weixin_fk_validate.py.
# Run manually after backend startup (not wired into deploy_main.sh in Batch A).
#
# Default DSN resolution:
# - If DALTON_FK_VALIDATE_DSN is set on the host, connects directly with that DSN.
# - Otherwise runs inside the running backend container (default: dalton-langalpha-backend-1,
#   matching deploy_main.sh's `docker exec` convention) where
#   `src.server.database.pool.get_db_connection_string()` reads DB_HOST/DB_PORT/DB_NAME/
#   DB_USER/DB_PASSWORD (or DATABASE_URL) from the backend container environment.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$REPO_ROOT"

CONTAINER_NAME="${DALTON_BACKEND_CONTAINER:-dalton-langalpha-backend-1}"
LOCK_TIMEOUT="${DALTON_FK_VALIDATE_LOCK_TIMEOUT:-5s}"

if [[ -n "${DALTON_FK_VALIDATE_DSN:-}" ]]; then
  exec uv run --no-sync python -m src.dalton.database.weixin_fk_validate \
    --dsn "${DALTON_FK_VALIDATE_DSN}" \
    --lock-timeout "${LOCK_TIMEOUT}"
fi

exec docker exec "${CONTAINER_NAME}" \
  uv run --no-sync python -m src.dalton.database.weixin_fk_validate \
    --lock-timeout "${LOCK_TIMEOUT}"
