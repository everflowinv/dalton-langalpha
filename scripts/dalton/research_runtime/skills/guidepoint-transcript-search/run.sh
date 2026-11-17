#!/bin/bash
# Guidepoint transcript search CLI wrapper.
# Usage:
#   bash skills/guidepoint-transcript-search/run.sh self-test
#   bash skills/guidepoint-transcript-search/run.sh search --query "..." [--keywords k1 k2] [--size 10] [--json]
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$DIR/scripts/gp_search.py" "$@"
