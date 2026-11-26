#!/usr/bin/env bash
# Export a clean standalone-core release directory and/or tarball from a git ref
# using an explicit allowlist via `git archive` (never mutates the working tree).
#
# Usage:
#   bash scripts/standalone/package_release.sh --output /tmp/dalton-core-dist
#   bash scripts/standalone/package_release.sh --ref HEAD --tar /tmp/dalton-core.tar.gz
#   bash scripts/standalone/package_release.sh --ref HEAD --include-ci --output /tmp/dist --report /tmp/scan.txt
set -euo pipefail

export COPYFILE_DISABLE=1

TAR_VERSION_STR="$(tar --version 2>&1 || true)"
TAR_CREATE_FLAGS=()
TAR_EXTRACT_FLAGS=()
if [[ "$TAR_VERSION_STR" == *bsdtar* || "$TAR_VERSION_STR" == *libarchive* ]]; then
  TAR_CREATE_FLAGS=(--no-xattrs --no-mac-metadata --no-fflags)
  TAR_EXTRACT_FLAGS=(--no-xattrs --no-mac-metadata --no-fflags)
elif [[ "$TAR_VERSION_STR" == *"GNU tar"* ]]; then
  TAR_CREATE_FLAGS=(--no-xattrs)
  TAR_EXTRACT_FLAGS=(--no-xattrs)
fi

CALLER_PWD="$PWD"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GIT_REF="HEAD"
OUTPUT_DIR=""
OUTPUT_SET=0
TAR_PATH=""
REPORT_PATH=""
INCLUDE_CI=0

usage() {
  cat <<'EOF'
Usage: scripts/standalone/package_release.sh [options]

Options:
  --repo <dir>        Git repository root to export from (default: this repo)
  --ref <git-ref>     Git commit/tag/branch to export from (default: HEAD)
  --output <dir>      Export clean release tree into this directory (created fresh)
  --tar <file.tar.gz> Write compressed release tarball to this path
  --report <file>     Also write the release verification report to this path
  --include-ci        Also export CI compose overlay & standalone test harness
                      (docker-compose.ci.yml, tests/fakes, tests/standalone)
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
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --output requires a directory path" >&2
        exit 2
      fi
      OUTPUT_DIR="$2"
      OUTPUT_SET=1
      shift 2
      ;;
    --output=*)
      OUTPUT_DIR="${1#--output=}"
      OUTPUT_SET=1
      shift
      ;;
    --tar)
      TAR_PATH="$2"
      shift 2
      ;;
    --report)
      REPORT_PATH="$2"
      shift 2
      ;;
    --include-ci)
      INCLUDE_CI=1
      shift
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

if [[ "$OUTPUT_SET" -eq 0 && -z "$TAR_PATH" ]]; then
  echo "ERROR: Provide at least one of --output <dir> or --tar <file.tar.gz>" >&2
  usage >&2
  exit 2
fi

OUTPUT_DIR_ABS=""
if [[ "$OUTPUT_SET" -eq 1 ]]; then
  OUTPUT_DIR_ABS="$(python3 - "$OUTPUT_DIR" "$CALLER_PWD" "$REPO_ROOT" "${HOME:-}" <<'PY'
import os
import sys
from pathlib import Path

raw_out, caller_pwd, repo_root, home_dir = sys.argv[1:5]
if not raw_out or not raw_out.strip():
    print("ERROR: --output must not be empty", file=sys.stderr)
    sys.exit(2)

raw_stripped = raw_out.strip()
if raw_stripped in (".", "./", "/"):
    print(f"ERROR: Refusing unsafe --output directory: {raw_out!r}", file=sys.stderr)
    sys.exit(2)

base_pwd = Path(caller_pwd).resolve()
candidate = Path(raw_stripped)
if not candidate.is_absolute():
    candidate = base_pwd / candidate
target = candidate.resolve(strict=False)

protected_roots = [Path("/"), base_pwd, Path(repo_root).resolve()]
if home_dir:
    protected_roots.append(Path(home_dir).resolve())

forbidden: set[Path] = set()
for root in protected_roots:
    forbidden.add(root)
    forbidden.update(root.parents)

if target in forbidden:
    print(
        f"ERROR: Refusing unsafe --output directory {str(target)!r} "
        "(cannot be '.', '/', repository root, $HOME, or any of their parent directories)",
        file=sys.stderr,
    )
    sys.exit(2)

if os.path.lexists(str(target)):
    print(
        f"ERROR: Output directory already exists: {str(target)!r} "
        "(refusing to overwrite existing path)",
        file=sys.stderr,
    )
    sys.exit(1)

print(str(target))
PY
)"
fi

cd "$REPO_ROOT"

if ! git rev-parse --verify "${GIT_REF}^{commit}" >/dev/null 2>&1; then
  echo "ERROR: Invalid git ref: ${GIT_REF}" >&2
  exit 1
fi

COMMIT_SHA="$(git rev-parse "${GIT_REF}^{commit}")"
SHORT_SHA="${COMMIT_SHA:0:9}"
RELEASE_TOP_DIR="dalton-core-${SHORT_SHA}"

# Explicit allowlist of paths required to build and run Standalone Core.
# Host-only Everflow runtime, ops, launchd, channel gateway, and *.dalton.*
# deployment overlays are excluded by omission and post-extract denylist.
ALLOWLIST_REQUIRED=(
  "LICENSE"
  "README.md"
  "Makefile"
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
  "scripts/configure.sh"
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
  "docs/dalton/dev-guide/STANDALONE-DEPLOY.md"
  "deploy/standalone"
  "scripts/standalone"
  "plugins/dalton_sec"
  "plugins/dalton_macro"
  "plugins/dalton_company_wiki"
  "plugins/dalton_research_all"
)

# Exported only when `--include-ci` is explicitly passed so customer release
# tarballs never ship test fixtures or CI compose overlays.
ALLOWLIST_CI=(
  "docker-compose.ci.yml"
  "tests/fakes"
  "tests/standalone"
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

if [[ "$INCLUDE_CI" -eq 1 ]]; then
  for path in "${ALLOWLIST_CI[@]}"; do
    if ! git cat-file -e "${COMMIT_SHA}:${path}" 2>/dev/null; then
      echo "ERROR: Required CI allowlist path missing in ${GIT_REF}: ${path}" >&2
      exit 1
    fi
    ARCHIVE_PATHS+=("$path")
  done
fi

STAGE_DIR="$(mktemp -d)"
SIBLING_TMP=""
cleanup() {
  rm -rf "$STAGE_DIR"
  if [[ -n "$SIBLING_TMP" && -e "$SIBLING_TMP" ]]; then
    rm -rf "$SIBLING_TMP"
  fi
}
trap cleanup EXIT

EXPORT_ROOT="${STAGE_DIR}/release"
mkdir -p "$EXPORT_ROOT"

git archive --format=tar "$COMMIT_SHA" -- "${ARCHIVE_PATHS[@]}" | tar ${TAR_EXTRACT_FLAGS[@]+"${TAR_EXTRACT_FLAGS[@]}"} -xf - -C "$EXPORT_ROOT"

# Defense-in-depth post-extract denylist inside allowlisted parent directories
# (e.g. `web/`, `src/`, `docs-site/`). Never touches the working tree.
# `docs-site/astro.config.mjs` omits the Everflow sidebar section and Feishu guide
# under Core, so stripping them keeps the Core release clean.
DENY_PATHS=(
  "plugins/dalton_data"
  "scripts/dalton/researchdata_bridge.py"
  "scripts/dalton/research_runtime"
  "scripts/dalton/ops"
  "scripts/dalton/launchd"
  "scripts/standalone/package_release.sh"
  "web/.env.dalton"
  "docs-site/src/content/docs/everflow"
  "docs-site/src/content/docs/explore/deliver-to-feishu.mdx"
  "docs-site/src/content/docs/explore/deliver-to-feishu.md"
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

# Out-of-package release gate check (when invoked from full repo checkout with ops checker present)
SCAN_OUTPUT="$STAGE_DIR/scan_report.txt"
TOOL_REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
OUT_OF_PKG_CHECKER=""
if [[ -f "${TOOL_REPO_ROOT}/scripts/dalton/ops/check_core_release_package.py" ]]; then
  OUT_OF_PKG_CHECKER="${TOOL_REPO_ROOT}/scripts/dalton/ops/check_core_release_package.py"
elif [[ -f "${REPO_ROOT}/scripts/dalton/ops/check_core_release_package.py" ]]; then
  OUT_OF_PKG_CHECKER="${REPO_ROOT}/scripts/dalton/ops/check_core_release_package.py"
fi
if [[ -n "$OUT_OF_PKG_CHECKER" ]]; then
  python3 "$OUT_OF_PKG_CHECKER" --export-dir "$EXPORT_ROOT" --repo-root "$REPO_ROOT" --report-only > "$SCAN_OUTPUT" 2>&1 || true
else
  {
    echo "=== Standalone Core Release Scan Report (ref=${GIT_REF}, sha=${COMMIT_SHA:0:12}) ==="
    echo "Exported clean allowlisted Standalone Core release tree."
  } > "$SCAN_OUTPUT"
fi

cat "$SCAN_OUTPUT"

if [[ -n "$REPORT_PATH" ]]; then
  mkdir -p "$(dirname "$REPORT_PATH")"
  cp "$SCAN_OUTPUT" "$REPORT_PATH"
fi

if [[ -n "$OUTPUT_DIR_ABS" ]]; then
  out_parent="$(dirname "$OUTPUT_DIR_ABS")"
  out_base="$(basename "$OUTPUT_DIR_ABS")"
  mkdir -p "$out_parent"
  SIBLING_TMP="$(mktemp -d "${out_parent}/.${out_base}.tmp.XXXXXX")"
  tar ${TAR_CREATE_FLAGS[@]+"${TAR_CREATE_FLAGS[@]}"} -cf - -C "$EXPORT_ROOT" . | tar ${TAR_EXTRACT_FLAGS[@]+"${TAR_EXTRACT_FLAGS[@]}"} -xf - -C "$SIBLING_TMP"
  if [[ -e "$OUTPUT_DIR_ABS" || -L "$OUTPUT_DIR_ABS" ]]; then
    echo "ERROR: Output directory already exists: ${OUTPUT_DIR_ABS}" >&2
    exit 1
  fi
  mv "$SIBLING_TMP" "$OUTPUT_DIR_ABS"
  SIBLING_TMP=""
  echo "[package_release] Exported clean release directory: ${OUTPUT_DIR_ABS}"
fi

if [[ -n "$TAR_PATH" ]]; then
  mkdir -p "$(dirname "$TAR_PATH")"
  TAR_STAGE_DIR="${STAGE_DIR}/${RELEASE_TOP_DIR}"
  mv "$EXPORT_ROOT" "$TAR_STAGE_DIR"
  tar ${TAR_CREATE_FLAGS[@]+"${TAR_CREATE_FLAGS[@]}"} -czf "$TAR_PATH" -C "$STAGE_DIR" "$RELEASE_TOP_DIR"
  if ! python3 "${SCRIPT_DIR}/verify_release_tarball.py" --expected-top-dir "$RELEASE_TOP_DIR" "$TAR_PATH"; then
    rm -f "$TAR_PATH"
    exit 1
  fi
  echo "[package_release] Wrote clean release tarball: ${TAR_PATH} (top-level dir: ${RELEASE_TOP_DIR}/)"
fi
