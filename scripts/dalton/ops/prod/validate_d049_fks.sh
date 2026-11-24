#!/usr/bin/env bash
# Everflow production post-deploy FK validator for d049_weixin_multi_account.
# Delegates to shared Python logic in src/dalton/database/weixin_fk_validate.py.
# Run manually after backend startup (not wired into deploy_main.sh in Batch A).
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

exec docker exec -T "${CONTAINER_NAME}" \
  uv run --no-sync python -m src.dalton.database.weixin_fk_validate \
    --lock-timeout "${LOCK_TIMEOUT}"
