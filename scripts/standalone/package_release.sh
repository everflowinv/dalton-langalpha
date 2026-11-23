#!/usr/bin/env bash
# Export a clean standalone-core release directory and/or tarball from a git ref
# using an explicit allowlist via `git archive` (never mutates the working tree).
#
# Usage:
#   bash scripts/standalone/package_release.sh --output /tmp/dalton-core-dist
#   bash scripts/standalone/package_release.sh --ref HEAD --tar /tmp/dalton-core.tar.gz
#   bash scripts/standalone/package_release.sh --ref HEAD --output /tmp/dist --report /tmp/scan.txt
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_REF="HEAD"
OUTPUT_DIR=""
TAR_PATH=""
REPORT_PATH=""

usage() {
  cat <<'EOF'
Usage: scripts/standalone/package_release.sh [options]

Options:
  --repo <dir>        Git repository root to export from (default: this repo)
  --ref <git-ref>     Git commit/tag/branch to export from (default: HEAD)
  --output <dir>      Export clean release tree into this directory (created fresh)
  --tar <file.tar.gz> Write compressed release tarball to this path
  --report <file>     Also write the proprietary-term scan report to this path
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      REPO_ROOT="$2"
      shift 2
      ;;
    --ref)
      GIT_REF="$2"
      shift 2
      ;;
    --output)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --tar)
      TAR_PATH="$2"
      shift 2
      ;;
    --report)
      REPORT_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$OUTPUT_DIR" && -z "$TAR_PATH" ]]; then
  echo "ERROR: Provide at least one of --output <dir> or --tar <file.tar.gz>" >&2
  usage >&2
  exit 2
fi

cd "$REPO_ROOT"

if ! git rev-parse --verify "${GIT_REF}^{commit}" >/dev/null 2>&1; then
  echo "ERROR: Invalid git ref: ${GIT_REF}" >&2
  exit 1
fi

COMMIT_SHA="$(git rev-parse "${GIT_REF}^{commit}")"

# Explicit allowlist of paths required to build and run Standalone Core.
# Proprietary / Everflow-only paths are excluded by omission:
#   - plugins/dalton_data/  (excluded in Batch 1: backend starts cleanly because
#       src/dalton/data_map/health.py:539 lazy-imports dalton_sec_mcp_server inside
#       fault-isolated _run_single_probe; Batch 2 extracts open-source plugins/dalton_sec/)
#   - scripts/dalton/researchdata_bridge.py
#   - scripts/dalton/research_runtime/
#   - scripts/dalton/ops/
#   - scripts/dalton/launchd/
#   - scripts/dalton/data_assets/
#   - scripts/dalton/monitor_feeds/
#   - scripts/dalton/realrun/
#   - integrations/dalton-channel-gateway/
#   - deploy/dalton/
#   - agent_config.dalton.yaml, config.dalton.yaml, docker-compose.dalton.yml
#   - web/.env.dalton
ALLOWLIST_REQUIRED=(
  "LICENSE"
  "README.md"
  "pyproject.toml"
  "uv.lock"
  "server.py"
  "alembic.ini"
  "config.yaml"
  "config.core.yaml"
  "agent_config.yaml"
  "agent_config.core.yaml"
  "docker-compose.yml"
  ".env.example"
  ".dockerignore"
  "Dockerfile.sandbox"
  "src"
  "libs/market-protocol"
  "libs/chartkit"
  "libs/edgar-engine"
  "migrations"
  "mcp_servers"
  "plugins/README.md"
  "plugins/alternative_data"
  "plugins/dalton"
  "plugins/firecrawl"
  "plugins/langalpha_deliverables"
  "plugins/langalpha_market_data"
  "plugins/langalpha_research"
  "plugins/langalpha_service"
  "plugins/yfinance"
  "workflows"
  "config/dalton/.gitkeep"
  "config/dalton/README.md"
  "deploy/Dockerfile.backend"
  "deploy/Dockerfile.dev"
  "deploy/Dockerfile.web"
  "scripts/docker_entrypoint.sh"
  "scripts/_errors.py"
  "scripts/dalton/Dockerfile.dalton"
  "scripts/dalton/Dockerfile.postgres"
  "scripts/dalton/docker_entrypoint_dalton.sh"
  "scripts/dalton/configure_minio.sh"
  "scripts/dalton/log_redaction.py"
  "web"
  "docs-site"
)

# Paths added across Batch 1b / Batch 2 / Batch 6 that are included whenever
# present in the target git ref.
ALLOWLIST_OPTIONAL=(
  "docker-compose.core.yml"
  ".env.core.example"
  "NOTICE"
  "deploy/standalone"
  "scripts/standalone"
  "plugins/dalton_sec"
)

ARCHIVE_PATHS=()
for path in "${ALLOWLIST_REQUIRED[@]}"; do
  if ! git cat-file -e "${COMMIT_SHA}:${path}" 2>/dev/null; then
    echo "ERROR: Required allowlist path missing in ${GIT_REF}: ${path}" >&2
    exit 1
  fi
  ARCHIVE_PATHS+=("$path")
done

for path in "${ALLOWLIST_OPTIONAL[@]}"; do
  if git cat-file -e "${COMMIT_SHA}:${path}" 2>/dev/null; then
    ARCHIVE_PATHS+=("$path")
  fi
done

STAGE_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

EXPORT_ROOT="${STAGE_DIR}/release"
mkdir -p "$EXPORT_ROOT"

git archive --format=tar "$COMMIT_SHA" -- "${ARCHIVE_PATHS[@]}" | tar -xf - -C "$EXPORT_ROOT"

# Defense-in-depth post-extract denylist inside allowlisted parent directories
# (e.g. `web/`, `docs-site/`, `src/`). Never touches the working tree.
DENY_PATHS=(
  "plugins/dalton_data"
  "scripts/dalton/researchdata_bridge.py"
  "scripts/dalton/research_runtime"
  "scripts/dalton/ops"
  "web/.env.dalton"
  "docs-site/src/content/docs/everflow"
)

for deny in "${DENY_PATHS[@]}"; do
  if [[ -e "${EXPORT_ROOT}/${deny}" ]]; then
    rm -rf "${EXPORT_ROOT:?}/${deny}"
  fi
done

# Strip any *.dalton.yaml file anywhere in the exported tree
find "$EXPORT_ROOT" -type f -name '*.dalton.yaml' -delete

# Record release provenance metadata (clean, no working-tree state)
printf '%s\n' "$COMMIT_SHA" > "${EXPORT_ROOT}/.release_git_sha"

# Scan exported tree for proprietary/Everflow terms.
# Batch 1: report-only (never fails exit code). Batch 2/3 will promote hits to errors.
SCAN_TERMS=(
  "everflowinv"
  "Proprietary"
  "researchdata_bridge"
  "alphaengine"
  "guidepoint"
)

SCAN_OUTPUT="$STAGE_DIR/scan_report.txt"
{
  echo "=== Standalone Core Release Scan Report (ref=${GIT_REF}, sha=${COMMIT_SHA:0:12}) ==="
  echo "Mode: report-only (Batch 1; promoted to hard gate in Batch 2/3)"
  for term in "${SCAN_TERMS[@]}"; do
    echo ""
    echo "--- Term: ${term} ---"
    hits="$(grep -rnI --binary-files=without-match -F "$term" "$EXPORT_ROOT" 2>/dev/null || true)"
    if [[ -z "$hits" ]]; then
      echo "(0 matches)"
    else
      count="$(printf '%s\n' "$hits" | wc -l | tr -d ' ')"
      echo "Total matches: ${count}"
      printf '%s\n' "$hits" | sed "s|${EXPORT_ROOT}/||g" | head -n 40
      if [[ "$count" -gt 40 ]]; then
        echo "... ($((count - 40)) more lines truncated)"
      fi
    fi
  done
} > "$SCAN_OUTPUT"

cat "$SCAN_OUTPUT"

if [[ -n "$REPORT_PATH" ]]; then
  mkdir -p "$(dirname "$REPORT_PATH")"
  cp "$SCAN_OUTPUT" "$REPORT_PATH"
fi

if [[ -n "$OUTPUT_DIR" ]]; then
  rm -rf "$OUTPUT_DIR"
  mkdir -p "$(dirname "$OUTPUT_DIR")"
  cp -a "$EXPORT_ROOT" "$OUTPUT_DIR"
  echo "[package_release] Exported clean release directory: ${OUTPUT_DIR}"
fi

if [[ -n "$TAR_PATH" ]]; then
  mkdir -p "$(dirname "$TAR_PATH")"
  tar -czf "$TAR_PATH" -C "$EXPORT_ROOT" .
  echo "[package_release] Wrote clean release tarball: ${TAR_PATH}"
fi
