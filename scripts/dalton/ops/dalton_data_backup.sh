#!/usr/bin/env bash
# Thin compatibility forwarder -> scripts/dalton/ops/prod/data_backup.sh (§3.1 step 4).
set -euo pipefail
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${OPS_DIR}/prod/data_backup.sh" "$@"
