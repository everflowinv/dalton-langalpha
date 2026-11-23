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
import sys

import psycopg

LOCK_KEY = 724109845123456789

conninfo = {
    "host": os.getenv("DB_HOST", "postgres"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "postgres"),
    "dbname": os.getenv("DB_NAME", "postgres"),
    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
}


def run_step(label: str, cmd: list[str]) -> None:
    print(f"[migrate] {label}...", flush=True)
    subprocess.run(cmd, check=True)


with psycopg.connect(**conninfo, autocommit=True) as conn:
    with conn.cursor() as cur:
        print("[migrate] Acquiring PostgreSQL migration advisory lock...", flush=True)
        cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
    try:
        run_step("Running upstream database migrations", ["uv", "run", "--no-sync", "alembic", "upgrade", "head"])
        if os.path.isfile("src/dalton/alembic.ini"):
            run_step(
                "Running Dalton database migrations (alembic_version_dalton)",
                ["uv", "run", "--no-sync", "alembic", "-c", "src/dalton/alembic.ini", "upgrade", "head"],
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
