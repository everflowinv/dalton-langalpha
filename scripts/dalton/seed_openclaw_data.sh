#!/usr/bin/env bash
# Bootstrap seed sync for OC-1 data tasks (Design §6.2).
# Syncs ONLY the required scripts (14), skills (9), and historical data paths
# from Design §6.2 table (lines 724-739) from ~/.openclaw/workspace/ to
# <repo>/var/dalton/openclaw-data/, excluding venv/, .venv/, cos-venv/,
# pending.json, *inflight*, .git/, and .dalton-freeze/, and creates the
# state/sentiment-dashboard/cos-venv/bin/python -> /app/.venv/bin/python symlink.
#
# Defaults to --dry-run (safe preview + total size summary); pass --apply to execute.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

MODE="dry-run"
SOURCE_DIR="${HOME}/.openclaw/workspace"
TARGET_DIR="${REPO_ROOT}/var/dalton/openclaw-data"

usage() {
  cat <<'EOF'
Usage: scripts/dalton/seed_openclaw_data.sh [--dry-run | --apply] [--source <dir>] [--target <dir>]

Options:
  --dry-run          Preview rsync changes without writing (default), printing total size at the end.
  --apply            Perform the sync and create the cos-venv python symlink.
  --source <dir>     Source OpenClaw workspace directory (default: ~/.openclaw/workspace).
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
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --source requires a directory argument" >&2
        exit 2
      fi
      SOURCE_DIR="$2"
      shift 2
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

# 14 scripts used by the 19 OC-1 Phase 1 tasks (verified against TASK_BASELINE.md and .agy/oc1/openclaw/scripts/)
SEED_SCRIPTS=(
  "scripts/asia_leverage_crossview.py"
  "scripts/bbg_export_to_csv.py"
  "scripts/bbg_remote_refresh.sh"
  "scripts/cron_command_tasks.py"
  "scripts/feishu_send_card.py"
  "scripts/megacap_relative_signal_monitor.py"
  "scripts/quantum_ew_daily_v4.py"
  "scripts/quantum_ew_monitor.sh"
  "scripts/run_json_task.sh"
  "scripts/sentiment_dashboard_cos_upload.py"
  "scripts/sentiment_dashboard_digest.sh"
  "scripts/softbank_nav_monitor.py"
  "scripts/tsmc_adr_premium.py"
  "scripts/update_gs_prime_positioning.py"
)

# 9 skill directories used by the 19 OC-1 Phase 1 tasks (excluding venv/ and .venv/)
SEED_SKILLS=(
  "skills/ah-premium-monitor"
  "skills/cn-leverage-monitor"
  "skills/index-crowding-research"
  "skills/jp-leverage-monitor"
  "skills/kr-leverage-monitor"
  "skills/sentiment-dashboard"
  "skills/tw-leverage-monitor"
  "skills/us-bottom-monitor"
  "skills/us-top-monitor"
)

# Historical data and state paths from Design §6.2 table (lines 728-739)
SEED_DATA_PATHS=(
  "research-outputs/sentiment-dashboard/signals.json"
  "research-outputs/index-crowding/official-all.json"
  "state/cn-leverage/pipeline_status.json"
  "state/cn-leverage/quant_unwind_signals.csv"
  "temp/bbg_data_latest.csv"
  "temp/bbg_data_remote.xlsx"
  "state/cn-leverage/*.csv"
  "state/cn-leverage/cffex_futures_v2_months_done.json"
  "research-outputs/jp-leverage/data/*.csv"
  "research-outputs/kr-leverage/data/*.csv"
  "research-outputs/kr-leverage/data/etf_watchlist.json"
  "research-outputs/tw-leverage/data/*.csv"
  "temp/tw-leverage/state_*.json"
  "temp/tw-leverage/progress_*.json"
  "temp/asia-leverage-crossview/composite_history.csv"
  "temp/asia-leverage-crossview/card_latest.md"
  "temp/asia-leverage-crossview/latest.json"
  "research-outputs/index-crowding-research/optimization-2023-summary.json"
  "research-outputs/index-crowding-research/optimization-2023-metrics.csv"
  "research-outputs/index-crowding-research/optimization-2023-yearly.csv"
  "research-outputs/index-crowding-research/optimization-2023-selections.csv"
  "research-outputs/index-crowding-research/optimization-2023-candidates.csv"
  "research-outputs/index-crowding-research/optimization-2023-daily.csv"
  "state/sentiment-dashboard/index_history"
  "state/sentiment-dashboard/msci"
  "state/sentiment-dashboard/yfinance_prices.pkl"
  "state/sentiment-dashboard/yfinance_ohlc.pkl"
  "state/sentiment-dashboard/gs_prime_positioning.json"
  "temp/mag7_compare_20261026/gs_exposure.pkl"
  "state/cron-command/us-topbottom-dm-v23/last-run.json"
  "state/cron-command/softbank-nav-monitor/last-run.json"
  "state/cron-command/tsmc-adr-premium/last-run.json"
)

SOURCE_DIR="${SOURCE_DIR%/}"
TARGET_DIR="${TARGET_DIR%/}"

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "ERROR: Source directory does not exist: ${SOURCE_DIR}" >&2
  exit 1
fi

FILES_FROM_TMP="$(mktemp "${TMPDIR:-/tmp}/dalton_seed_files.XXXXXX")"
RSYNC_OUT_TMP="$(mktemp "${TMPDIR:-/tmp}/dalton_seed_rsync.XXXXXX")"
trap 'rm -f "${FILES_FROM_TMP}" "${RSYNC_OUT_TMP}"' EXIT

(
  cd "${SOURCE_DIR}"
  shopt -s nullglob
  for pattern in "${SEED_SCRIPTS[@]}" "${SEED_SKILLS[@]}" "${SEED_DATA_PATHS[@]}"; do
    for match in $pattern; do
      if [[ -e "${match}" || -L "${match}" ]]; then
        printf '%s\n' "${match}"
      fi
    done
  done
) | LC_ALL=C sort -u > "${FILES_FROM_TMP}"

RSYNC_ARGS=(
  -ar
  --stats
  "--exclude=venv/"
  "--exclude=.venv/"
  "--exclude=cos-venv/"
  "--exclude=pending.json"
  "--exclude=*inflight*"
  "--exclude=.git/"
  "--exclude=.dalton-freeze/"
  "--files-from=${FILES_FROM_TMP}"
)

SYMLINK_REL="state/sentiment-dashboard/cos-venv/bin/python"
SYMLINK_TARGET="/app/.venv/bin/python"
OVERLAY_DIR="${SCRIPT_DIR}/openclaw_overlays"

OVERLAY_FILES=()
if [[ -d "${OVERLAY_DIR}" ]]; then
  while IFS= read -r rel_overlay; do
    [[ -n "${rel_overlay}" ]] && OVERLAY_FILES+=("${rel_overlay}")
  done < <(
    cd "${OVERLAY_DIR}"
    find . -type f | sed 's|^\./||' | LC_ALL=C sort
  )
fi

if [[ "${MODE}" == "dry-run" ]]; then
  RSYNC_ARGS+=(--dry-run --itemize-changes)
  echo "[seed_openclaw_data] Mode: DRY-RUN (pass --apply to write changes)"
  echo "[seed_openclaw_data] Source: ${SOURCE_DIR}/"
  echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
  if [[ -s "${FILES_FROM_TMP}" ]]; then
    rsync "${RSYNC_ARGS[@]}" "${SOURCE_DIR}/" "${TARGET_DIR}/" | tee "${RSYNC_OUT_TMP}"
    TOTAL_SIZE_LINE="$(grep -E -i '^(Total file size:|total size is )' "${RSYNC_OUT_TMP}" | tail -n 1 || true)"
  else
    TOTAL_SIZE_LINE="Total file size: 0 B"
  fi
  for rel_overlay in "${OVERLAY_FILES[@]}"; do
    echo "[seed_openclaw_data] DRY-RUN overlay: ${OVERLAY_DIR}/${rel_overlay} -> ${TARGET_DIR}/${rel_overlay}"
  done
  echo "[seed_openclaw_data] DRY-RUN symlink: ${TARGET_DIR}/${SYMLINK_REL} -> ${SYMLINK_TARGET}"
  echo "[seed_openclaw_data] DRY-RUN total size (${TOTAL_SIZE_LINE:-Total file size: 0 B})"
  exit 0
fi

echo "[seed_openclaw_data] Mode: APPLY"
echo "[seed_openclaw_data] Source: ${SOURCE_DIR}/"
echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
mkdir -p "${TARGET_DIR}"
if [[ -s "${FILES_FROM_TMP}" ]]; then
  rsync "${RSYNC_ARGS[@]}" "${SOURCE_DIR}/" "${TARGET_DIR}/"
fi

for rel_overlay in "${OVERLAY_FILES[@]}"; do
  mkdir -p "$(dirname "${TARGET_DIR}/${rel_overlay}")"
  cp -p "${OVERLAY_DIR}/${rel_overlay}" "${TARGET_DIR}/${rel_overlay}"
  echo "[seed_openclaw_data] Applied overlay: ${OVERLAY_DIR}/${rel_overlay} -> ${TARGET_DIR}/${rel_overlay}"
done

mkdir -p "$(dirname "${TARGET_DIR}/${SYMLINK_REL}")"
ln -sfn "${SYMLINK_TARGET}" "${TARGET_DIR}/${SYMLINK_REL}"
echo "[seed_openclaw_data] Created symlink: ${TARGET_DIR}/${SYMLINK_REL} -> ${SYMLINK_TARGET}"
