#!/bin/bash
# Daily Dalton Postgres backup: full pg_dump (custom format), tiered retention via prune_backups.py (daily 14, weekly 8, deploy 5; manual dumps kept).
set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"
BACKUP_DIR="${DALTON_BACKUP_DIR:-$HOME/Backups/dalton}"
PRUNE_SCRIPT="${DALTON_PRUNE_SCRIPT:-${BACKUP_DIR}/prune_backups.py}"
cd "$REPO_ROOT"
mkdir -p "$BACKUP_DIR"
OUT="${BACKUP_DIR}/dalton-pg-$(date +%Y%m%d-%H%M%S).dump"
umask 077
docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$OUT.tmp"
[ -s "$OUT.tmp" ] && mv "$OUT.tmp" "$OUT"
/usr/bin/python3 "$PRUNE_SCRIPT" --apply >/dev/null
echo "$(date '+%F %T') ok $(du -h "$OUT" | cut -f1) $OUT"
