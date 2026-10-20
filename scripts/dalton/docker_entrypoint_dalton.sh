#!/bin/bash
# Dalton Docker entrypoint: ensure git is available for L41 version stamping, wait for database,
# run upstream + Dalton migrations, then exec CMD.
set -e

if ! command -v git >/dev/null 2>&1; then
  echo "Installing git in container for startup version check..."
  apt-get update -qq && apt-get install -y -qq --no-install-recommends git >/dev/null
  rm -rf /var/lib/apt/lists/*
fi
git config --global --add safe.directory '*' || true

DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-postgres}"

echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -q; do
  sleep 1
done

echo "Running upstream database migrations..."
uv run alembic upgrade head

if [ -f "src/dalton/alembic.ini" ]; then
  echo "Running Dalton database migrations (alembic_version_dalton)..."
  uv run alembic -c src/dalton/alembic.ini upgrade head
fi

echo "Database ready."
exec "$@"
