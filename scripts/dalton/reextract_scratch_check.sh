#!/usr/bin/env bash
# Lead-run only: verify reextract_conversations.py apply + rollback against a real Postgres copy
# of dalton_conversation_cards, never against the app database itself.
#
# 1. Creates a scratch database in the running postgres container (dropping an old one of the
#    same name), with the vector and pg_trgm extensions.
# 2. Copies dalton_conversation_cards into it with pg_dump --table (schema with the PK, UNIQUE
#    content_hash, CHECKs, the self-referencing supersedes FK and the indexes, plus all data).
#    The app database is only read.
# 3. Runs scripts/dalton/reextract_scratch_check.py against the scratch database: synthetic
#    staged run -> apply (dry run, then --yes) -> rollback, then compares row count, an md5 over
#    all columns (embedding/timestamps/arrays as text; files_written_at excluded because
#    rollback clears it on purpose on the run's dates, while rows on every other date must keep
#    it), constraint definitions and FK/unique integrity.
# 4. Drops the scratch database unless KEEP_SCRATCH=1.
#
# Usage (from the deploy checkout, with the stack running):
#   bash scripts/dalton/reextract_scratch_check.sh [--cards 200]
# Env: PG_CONTAINER (default dalton-langalpha-postgres-1), SCRATCH_DB (default
#   dalton_reextract_scratch; must contain "scratch"), APP_DB (default: the container's
#   POSTGRES_DB), SCRATCH_DB_HOST / SCRATCH_DB_PORT (host-side address of the container if
#   .env's DB_HOST is not reachable from the host), KEEP_SCRATCH=1.
# Credentials come from .env (loaded by the Python check) and the container's own environment;
# nothing is printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PG_CONTAINER="${PG_CONTAINER:-dalton-langalpha-postgres-1}"
SCRATCH_DB="${SCRATCH_DB:-dalton_reextract_scratch}"

if [[ "${SCRATCH_DB}" != *scratch* || ! "${SCRATCH_DB}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "SCRATCH_DB must be [A-Za-z0-9_]+ and contain 'scratch'" >&2
    exit 1
fi
APP_DB="${APP_DB:-$(docker exec "${PG_CONTAINER}" sh -c 'printf %s "$POSTGRES_DB"')}"
if [[ -z "${APP_DB}" || "${APP_DB}" == "${SCRATCH_DB}" || ! "${APP_DB}" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "refusing: APP_DB is empty, invalid, or equals SCRATCH_DB" >&2
    exit 1
fi

in_pg() {
    # Runs inside the container as its POSTGRES_USER over the local socket.
    docker exec -i "${PG_CONTAINER}" sh -c "$1"
}

echo "creating scratch database ${SCRATCH_DB} from ${APP_DB}.dalton_conversation_cards in ${PG_CONTAINER}"
in_pg "dropdb -U \"\$POSTGRES_USER\" --if-exists ${SCRATCH_DB}"
in_pg "createdb -U \"\$POSTGRES_USER\" ${SCRATCH_DB}"
in_pg "psql -v ON_ERROR_STOP=1 -q -U \"\$POSTGRES_USER\" -d ${SCRATCH_DB} -c 'CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;'"
in_pg "pg_dump -U \"\$POSTGRES_USER\" -d ${APP_DB} --table=public.dalton_conversation_cards --no-owner --no-privileges | psql -v ON_ERROR_STOP=1 -q -U \"\$POSTGRES_USER\" -d ${SCRATCH_DB}"
in_pg "psql -q -t -A -U \"\$POSTGRES_USER\" -d ${SCRATCH_DB} -c 'SELECT COUNT(*) FROM dalton_conversation_cards'" | sed 's/^/scratch rows: /'

cleanup() {
    if [[ "${KEEP_SCRATCH:-0}" != "1" ]]; then
        in_pg "dropdb -U \"\$POSTGRES_USER\" --if-exists ${SCRATCH_DB}" || true
    else
        echo "kept scratch database ${SCRATCH_DB}"
    fi
}
trap cleanup EXIT

# .env is not sourced here: a value may hold unquoted spaces, and `source` runs the rest of
# such a line as a command (exit 127 under set -e with macOS bash 3.2, even behind `|| true`).
# The Python check loads it with python-dotenv instead (which reads KEY=VALUE lines in process,
# though unlike compose's env_file it expands $VAR).

if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
else
    PYTHON_BIN="$(command -v python3)"
fi

cd "${REPO_ROOT}"
"${PYTHON_BIN}" scripts/dalton/reextract_scratch_check.py --scratch-db "${SCRATCH_DB}" "$@"
