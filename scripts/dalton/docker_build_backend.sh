#!/usr/bin/env bash
# Build and optionally restart the Dalton backend container with host BUILD_GIT_SHA and BUILD_GIT_DIRTY injected (P0J Round-4 Must-Fix #7, Review Item 8).
# Usage:
#   ./scripts/dalton/docker_build_backend.sh           # build + up -d backend
#   ./scripts/dalton/docker_build_backend.sh --build-only

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BUILD_GIT_SHA="$(git rev-parse --short=8 HEAD)"
if [[ -n "$(git status --porcelain)" ]]; then
  BUILD_GIT_DIRTY="true"
else
  BUILD_GIT_DIRTY="false"
fi

export BUILD_GIT_SHA
export BUILD_GIT_DIRTY

echo "[docker_build_backend] Building backend with BUILD_GIT_SHA=${BUILD_GIT_SHA} BUILD_GIT_DIRTY=${BUILD_GIT_DIRTY}"

if [[ "${1:-}" == "--build-only" ]]; then
  docker compose -f docker-compose.yml -f docker-compose.dalton.yml build backend
else
  docker compose -f docker-compose.yml -f docker-compose.dalton.yml up -d --build backend
fi
