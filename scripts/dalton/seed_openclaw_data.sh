#!/usr/bin/env bash
# Deploy Dalton-owned data-task scripts, skills, and helpers from
# <repo>/scripts/dalton/data_assets/ into <repo>/var/dalton/openclaw-data/,
# excluding credential filenames, ensuring state/a-share-volume-watch exists,
# and creating state/sentiment-dashboard/cos-venv/bin/python -> /app/.venv/bin/python.
#
# Defaults to --dry-run (safe preview); pass --apply to execute.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MODE="dry-run"
TARGET_DIR="${REPO_ROOT}/var/dalton/openclaw-data"

usage() {
  cat <<'EOF'
Usage: scripts/dalton/seed_openclaw_data.sh [--dry-run | --apply] [--target <dir>]

Options:
  --dry-run          Preview asset deployment without writing (default).
  --apply            Install Dalton data assets and create the cos-venv python launcher.
  --target <dir>     Target Dalton data root directory (default: <repo>/var/dalton/openclaw-data).
  -h, --help         Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      MODE="dry-run"
      shift
      ;;
    --apply)
      MODE="apply"
      shift
      ;;
    --source)
      echo "ERROR: --source sync from OpenClaw workspace has been removed; manage assets under scripts/dalton/data_assets/" >&2
      exit 2
      ;;
    --target)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --target requires a directory argument" >&2
        exit 2
      fi
      TARGET_DIR="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

TARGET_DIR="${TARGET_DIR%/}"

CREDENTIAL_NAME_PATTERNS=(
  ".env"
  "*.env"
  ".env.*"
  "*secret*"
  "*credential*"
  "*.pem"
  "*.key"
  "*.p12"
  "id_rsa*"
  "*token*.json"
)

is_credential_component() {
  local name="$1"
  local pattern
  for pattern in "${CREDENTIAL_NAME_PATTERNS[@]}"; do
    # The pattern is a filename glob, so it must stay unquoted.
    # shellcheck disable=SC2254
    case "${name}" in
      ${pattern})
        return 0
        ;;
    esac
  done
  return 1
}

is_credential_relpath() {
  local rest="$1"
  local part
  while [[ -n "${rest}" ]]; do
    part="${rest%%/*}"
    if is_credential_component "${part}"; then
      return 0
    fi
    if [[ "${rest}" == */* ]]; then
      rest="${rest#*/}"
    else
      break
    fi
  done
  return 1
}

LAUNCHER_REL="state/sentiment-dashboard/cos-venv/bin/python"
ASSETS_DIR="${SCRIPT_DIR}/data_assets"

ASSET_FILES=()
if [[ -d "${ASSETS_DIR}" ]]; then
  while IFS= read -r rel_asset; do
    [[ -n "${rel_asset}" ]] && ASSET_FILES+=("${rel_asset}")
  done < <(
    cd "${ASSETS_DIR}"
    find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | sed 's|^\./||' | LC_ALL=C sort
  )
fi

if [[ "${MODE}" == "dry-run" ]]; then
  echo "[seed_openclaw_data] Mode: DRY-RUN (pass --apply to write changes)"
  echo "[seed_openclaw_data] Assets: ${ASSETS_DIR}/"
  echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
  for rel_asset in "${ASSET_FILES[@]}"; do
    if is_credential_relpath "${rel_asset}"; then
      continue
    fi
    echo "[seed_openclaw_data] DRY-RUN asset: ${ASSETS_DIR}/${rel_asset} -> ${TARGET_DIR}/${rel_asset}"
  done
  echo "[seed_openclaw_data] DRY-RUN ensure dir: ${TARGET_DIR}/state/a-share-volume-watch"
  echo "[seed_openclaw_data] DRY-RUN launcher: ${TARGET_DIR}/${LAUNCHER_REL} executes /app/.venv/bin/python"
  exit 0
fi

echo "[seed_openclaw_data] Mode: APPLY"
echo "[seed_openclaw_data] Assets: ${ASSETS_DIR}/"
echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
mkdir -p "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}/state/a-share-volume-watch"

for rel_asset in "${ASSET_FILES[@]}"; do
  if is_credential_relpath "${rel_asset}"; then
    continue
  fi
  mkdir -p "$(dirname "${TARGET_DIR}/${rel_asset}")"
  cp -p "${ASSETS_DIR}/${rel_asset}" "${TARGET_DIR}/${rel_asset}"
  echo "[seed_openclaw_data] Applied asset: ${ASSETS_DIR}/${rel_asset} -> ${TARGET_DIR}/${rel_asset}"
done

LAUNCHER_TMP=""
trap '[[ -z "${LAUNCHER_TMP}" ]] || rm -f "${LAUNCHER_TMP}"' EXIT
mkdir -p "$(dirname "${TARGET_DIR}/${LAUNCHER_REL}")"
LAUNCHER_TMP="$(mktemp "${TARGET_DIR}/${LAUNCHER_REL}.XXXXXX")"
cat > "${LAUNCHER_TMP}" <<'EOF'
#!/bin/sh
exec /app/.venv/bin/python "$@"
EOF
chmod 755 "${LAUNCHER_TMP}"
mv -f "${LAUNCHER_TMP}" "${TARGET_DIR}/${LAUNCHER_REL}"
LAUNCHER_TMP=""
echo "[seed_openclaw_data] Created launcher: ${TARGET_DIR}/${LAUNCHER_REL} executes /app/.venv/bin/python"
