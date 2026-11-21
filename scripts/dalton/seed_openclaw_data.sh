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
ANTIGRAVITY_SERIES_MODULE_SRC="${REPO_ROOT}/src/dalton/llm/antigravity_series.py"
ANTIGRAVITY_SERIES_MODULE_REL="skills/model-usage-cost/scripts/antigravity_series.py"
ANTIGRAVITY_SERIES_JSON_REL="skills/model-usage-cost/references/antigravity_series.json"

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
  if [[ -f "${ANTIGRAVITY_SERIES_MODULE_SRC}" ]]; then
    echo "[seed_openclaw_data] DRY-RUN asset: ${ANTIGRAVITY_SERIES_MODULE_SRC} -> ${TARGET_DIR}/${ANTIGRAVITY_SERIES_MODULE_REL}"
    echo "[seed_openclaw_data] DRY-RUN export: ${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}"
  fi
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
SERIES_JSON_TMP=""
cleanup_tmps() {
  [[ -z "${LAUNCHER_TMP}" ]] || rm -f "${LAUNCHER_TMP}"
  [[ -z "${SERIES_JSON_TMP}" ]] || rm -f "${SERIES_JSON_TMP}"
}
trap cleanup_tmps EXIT

if [[ -f "${ANTIGRAVITY_SERIES_MODULE_SRC}" ]]; then
  mkdir -p "$(dirname "${TARGET_DIR}/${ANTIGRAVITY_SERIES_MODULE_REL}")"
  cp -p "${ANTIGRAVITY_SERIES_MODULE_SRC}" "${TARGET_DIR}/${ANTIGRAVITY_SERIES_MODULE_REL}"
  echo "[seed_openclaw_data] Applied asset: ${ANTIGRAVITY_SERIES_MODULE_SRC} -> ${TARGET_DIR}/${ANTIGRAVITY_SERIES_MODULE_REL}"

  SERIES_PYTHON="python3"
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    SERIES_PYTHON="${REPO_ROOT}/.venv/bin/python"
  fi
  mkdir -p "$(dirname "${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}")"
  SERIES_JSON_TMP="$(mktemp "${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}.XXXXXX")"
  "${SERIES_PYTHON}" - "${REPO_ROOT}" "${ANTIGRAVITY_SERIES_MODULE_SRC}" "${SERIES_JSON_TMP}" <<'PYEOF'
import copy
import importlib.util
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
module_path = Path(sys.argv[2])
out_path = Path(sys.argv[3])

spec = importlib.util.spec_from_file_location("antigravity_series", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load {module_path}")
ag_mod = importlib.util.module_from_spec(spec)
sys.modules["antigravity_series"] = ag_mod
spec.loader.exec_module(ag_mod)


def deep_merge(target, patch):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            deep_merge(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


merged_models = {}
for ov_path in (
    repo_root / "src" / "dalton" / "llm" / "native_overlay.json",
    repo_root / "var" / "dalton" / "catalog" / "native_sync_overlay.json",
):
    if not ov_path.is_file():
        continue
    raw = json.loads(ov_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        continue
    for key, entry in (raw.get("models") or {}).items():
        if key not in merged_models and isinstance(entry, dict):
            merged_models[key] = copy.deepcopy(entry)
    for key, patch in (raw.get("model_overrides") or {}).items():
        if isinstance(patch, dict) and isinstance(merged_models.get(key), dict):
            deep_merge(merged_models[key], patch)

dalton_yaml = repo_root / "src" / "dalton" / "dalton.yaml"
series = ag_mod.build_antigravity_series_catalog(
    merged_models,
    dalton_yaml_path=dalton_yaml if dalton_yaml.is_file() else None,
)
out_path.write_text(json.dumps(series, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PYEOF
  chmod 644 "${SERIES_JSON_TMP}"
  mv -f "${SERIES_JSON_TMP}" "${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}"
  SERIES_JSON_TMP=""
  echo "[seed_openclaw_data] Exported catalog: ${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}"
fi

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
