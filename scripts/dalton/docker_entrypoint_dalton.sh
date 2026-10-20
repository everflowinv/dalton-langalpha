#!/bin/bash
# Dalton Docker entrypoint: wait for database, run upstream + Dalton migrations, then exec CMD.
# Git is pre-installed in scripts/dalton/Dockerfile.dalton (no runtime apt-get).
set -e

if [ ! -s /app/.build_git_sha ] && [ -f /app/.build_files.sha256 ] && [ -d /host_repo/.git ]; then
  if (cd /host_repo && sha256sum --status -c /app/.build_files.sha256 2>/dev/null); then
    git -c safe.directory='*' --no-optional-locks -C /host_repo rev-parse --short=8 HEAD > /app/.build_git_sha 2>/dev/null || true
  fi
fi

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
