#!/usr/bin/env bash
# Standalone Core one-shot database migration runner.
#
# Serializes concurrent runs via a PostgreSQL session advisory lock, then runs:
#   1. Upstream migrations (`alembic upgrade head`)
#   2. Dalton migrations (`alembic -c src/dalton/alembic.ini upgrade head`)
#
# Re-running is idempotent; exits 0 when both migration chains are at head.
set -euo pipefail

DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-postgres}"

echo "[migrate] Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -q; do
  sleep 1
done

exec uv run --no-sync python - <<'PY'
import os
import subprocess
import time
import sys

import psycopg

LOCK_KEY = 724109845123456789

conninfo = {
    "host": os.getenv("DB_HOST", "postgres"),
    # Empty values fall back like the PostgreSQL entrypoint's ${VAR:-default}.
    "port": int(os.getenv("DB_PORT") or "5432"),
    "user": os.getenv("DB_USER") or "postgres",
    # PostgreSQL refuses to start with an empty DB_PASSWORD (docker-compose.core.yml),
    # and Alembic/backend read it verbatim, so there is no default here.
    "password": os.environ["DB_PASSWORD"],
    "dbname": os.getenv("DB_NAME") or "postgres",
    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
}


def run_step(label: str, cmd: list[str]) -> None:
    print(f"[migrate] {label}...", flush=True)
    subprocess.run(cmd, check=True)


with psycopg.connect(**conninfo, autocommit=True) as conn:
    with conn.cursor() as cur:
        print("[migrate] Acquiring PostgreSQL migration advisory lock...", flush=True)
        # Poll instead of blocking in pg_advisory_lock: a backend waiting inside that
        # statement holds a snapshot, and the migrations' CREATE INDEX CONCURRENTLY
        # waits for every such snapshot, so two concurrent runs would deadlock.
        while True:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
            if cur.fetchone()[0]:
                break
            time.sleep(1)
    try:
        run_step("Running upstream database migrations", ["uv", "run", "--no-sync", "alembic", "upgrade", "head"])
        dalton_ini = "src/dalton/alembic.ini"
        if not os.path.isfile(dalton_ini):
            print(
                f"[migrate] ERROR: Required Dalton Alembic config not found: {dalton_ini}",
                file=sys.stderr,
                flush=True,
            )
            sys.exit(1)
        run_step(
            "Running Dalton database migrations (alembic_version_dalton)",
            ["uv", "run", "--no-sync", "alembic", "-c", dalton_ini, "upgrade", "head"],
        )
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        except Exception:
            pass

print("[migrate] Both migration chains are at head.", flush=True)
sys.exit(0)
PY
