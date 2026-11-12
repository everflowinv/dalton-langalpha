#!/usr/bin/env bash
# Bootstrap seed sync for OC-1 data tasks (Design §6.2).
# Syncs ONLY the allowlisted scripts (25), skills (12), config, and data paths
# (Design §6.2, plus the batch 3 and batch 4 files those tasks read) from ~/.openclaw/workspace/ to
# <repo>/var/dalton/openclaw-data/, excluding venv/, .venv/, cos-venv/,
# pending.json, *inflight*, .git/, and .dalton-freeze/, and creates the
# state/sentiment-dashboard/cos-venv/bin/python launcher for /app/.venv/bin/python.
#
# Code (scripts, skill code, config) is updated on every --apply.
# State and data are copied only when the target file is missing
# (rsync --ignore-existing): state/, research-outputs/, temp/, skill
# state/data/temp/events/model_cards directories, market-memory markdown, and the other
# data paths listed below. An existing target file is left unchanged.
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
  --apply            Perform the sync and create the cos-venv python launcher.
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

# Phase 1 scripts, plus batch 3 and batch 4 scripts the tasks actually run.
# feishu_send_card.py stays: the overlay also ships a copy, and the seed list
# already had the workspace script. Do not add ~/.openclaw/openclaw.json
# (ETF credential file).
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
  "scripts/etf_monthly_flows_tracker.py"
  "scripts/eve_tracking_mirror.py"
  "scripts/fde_hiring_discord_digest.py"
  "scripts/fde_hiring_weekly_report.sh"
  "scripts/gs_pb_weekly.py"
  "scripts/semi_bottom_checklist.py"
  "scripts/semi_rescue_alert.py"
  "scripts/a_share_volume_watch.py"
  "scripts/china_2c_monitor.py"
  "scripts/e_watch_monitor.py"
  "scripts/pt_watchlist_monitor.py"
)

# Config is code: a later --apply replaces these files.
# Do not add ~/.openclaw/openclaw.json (ETF credential file).
SEED_CONFIGS=(
  "config/etf_characteristics.json"
  "config/eve_tracking_mirror.json"
  "config/semi_rescue_alert.json"
  "config/a_share_volume_watch.json"
)

# Phase 1 skills, plus batch 3 skills. Skill code (including config/ and
# references/) is updated on every --apply. Directories named state, data,
# temp, events, or model_cards inside a skill are state: copied only when the target file is missing.
# venv/, .venv/, and cos-venv/ stay excluded below.
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
  "skills/ai-chain"
  "skills/fde-hiring-tracker"
  "skills/hedging-analysis"
)

# State and generated data. A later --apply fills gaps and does not replace
# a file the target already has, including the phase 1 paths below.
SEED_STATE_PATHS=(
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
  "state/gs-pb-weekly/state.json"
  "state/gs-pb-weekly/last-run.json"
  "research-outputs/gs-pb-weekly/gs_pb_charts_daily_latest.csv"
  "research-outputs/gs-pb-weekly/gs_pb_core_weekly_latest.csv"
  "research-outputs/gs-pb-weekly/issues/*.json"
  "state/semi-rescue-alert/last.json"
  "state/semi-rescue-alert/news_seen.json"
  "state/semi-rescue-alert/earnings_cache.json"
  "state/semi-rescue-alert/gpu_index.json"
  "state/semi-rescue-alert/ramp_ai_index.json"
  "state/semi-rescue-alert/cftc_nq.json"
  "state/semi-rescue-alert/ai_short.json"
  "state/semi-bottom-checklist/state.json"
  "research-outputs/semi-rescue-alert/*/snapshot.json"
  "state/e_watch_universe.json"
  "state/e_watch_signal_state.json"
  "state/e_watch_daily_history.json"
  "state/pt_watchlist_signal_state.json"
  "state/china_2c_universe_hk.json"
  "state/china_2c_universe_us.json"
  "state/china_2c_monitor_state.json"
  # ai-chain daily_run reads market-memory/<today>.md, or every top-level
  # *.md when that file is absent. Subdirectories and non-markdown files
  # are not read.
  "market-memory/*.md"
  # semi_rescue_alert.py opens this database read-only. The rest of
  # skills/market-digest is not an input of the seeded tasks.
  "skills/market-digest/vectors.db"
  # ai-chain filings adapter reads this file only, not last_check.json.
  "skills/company-filings-alert/state/filings.json"
  # T38 redirects stderr here. The directory must exist before Python starts.
  # Files already in the target (stderr.log, runtime json) are not replaced.
  "state/a-share-volume-watch"
)

SOURCE_DIR="${SOURCE_DIR%/}"
TARGET_DIR="${TARGET_DIR%/}"

if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "ERROR: Source directory does not exist: ${SOURCE_DIR}" >&2
  exit 1
fi

CODE_FILES_TMP="$(mktemp "${TMPDIR:-/tmp}/dalton_seed_code.XXXXXX")"
STATE_FILES_TMP="$(mktemp "${TMPDIR:-/tmp}/dalton_seed_state.XXXXXX")"
RSYNC_OUT_TMP="$(mktemp "${TMPDIR:-/tmp}/dalton_seed_rsync.XXXXXX")"
LAUNCHER_TMP=""
trap 'rm -f "${CODE_FILES_TMP}" "${STATE_FILES_TMP}" "${RSYNC_OUT_TMP}"; [[ -z "${LAUNCHER_TMP}" ]] || rm -f "${LAUNCHER_TMP}"' EXIT

(
  cd "${SOURCE_DIR}"
  shopt -s nullglob
  for pattern in "${SEED_SCRIPTS[@]}" "${SEED_CONFIGS[@]}" "${SEED_SKILLS[@]}"; do
    for match in $pattern; do
      if [[ -e "${match}" || -L "${match}" ]]; then
        printf '%s\n' "${match}"
      fi
    done
  done
) | LC_ALL=C sort -u > "${CODE_FILES_TMP}"

(
  cd "${SOURCE_DIR}"
  shopt -s nullglob
  for pattern in "${SEED_STATE_PATHS[@]}"; do
    for match in $pattern; do
      if [[ -e "${match}" || -L "${match}" ]]; then
        printf '%s\n' "${match}"
      fi
    done
  done
  for pattern in "${SEED_SKILLS[@]}"; do
    for match in $pattern; do
      if [[ -d "${match}" ]]; then
        # -H follows a skill directory that is itself a symlink (copy-dirlinks).
        find -H "${match}" \
          \( -name venv -o -name .venv -o -name cos-venv -o -name .git -o -name .dalton-freeze \) -prune \
          -o -type d \( -name state -o -name data -o -name temp -o -name events -o -name model_cards \) -print
      fi
    done
  done
) | LC_ALL=C sort -u > "${STATE_FILES_TMP}"

# $1 is the files-from list. Extra arguments are rsync options for that class.
seed_rsync() {
  local files_from="$1"
  shift
  if [[ ! -s "${files_from}" ]]; then
    return 0
  fi
  local -a args=(
    -ar
    --copy-dirlinks
    --stats
    "--exclude=venv/"
    "--exclude=.venv/"
    "--exclude=cos-venv/"
    "--exclude=pending.json"
    "--exclude=*inflight*"
    "--exclude=.git/"
    "--exclude=.dalton-freeze/"
  )
  if [[ "${MODE}" == "dry-run" ]]; then
    args+=(--dry-run --itemize-changes)
  fi
  args+=("$@")
  args+=("--files-from=${files_from}")
  rsync "${args[@]}" "${SOURCE_DIR}/" "${TARGET_DIR}/"
}

LAUNCHER_REL="state/sentiment-dashboard/cos-venv/bin/python"
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
  echo "[seed_openclaw_data] Mode: DRY-RUN (pass --apply to write changes)"
  echo "[seed_openclaw_data] Source: ${SOURCE_DIR}/"
  echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
  if [[ -s "${CODE_FILES_TMP}" || -s "${STATE_FILES_TMP}" ]]; then
    {
      # Skill state/data/temp/events/model_cards are mutable: the state pass copies them only when missing.
      seed_rsync "${CODE_FILES_TMP}" --exclude=state/ --exclude=data/ --exclude=temp/ --exclude=events/ --exclude=model_cards/
      seed_rsync "${STATE_FILES_TMP}" --ignore-existing
    } | tee "${RSYNC_OUT_TMP}"
    TOTAL_SIZE_LINE="$(grep -E -i '^(Total file size:|total size is )' "${RSYNC_OUT_TMP}" | tail -n 1 || true)"
  else
    TOTAL_SIZE_LINE="Total file size: 0 B"
  fi
  for rel_overlay in "${OVERLAY_FILES[@]}"; do
    echo "[seed_openclaw_data] DRY-RUN overlay: ${OVERLAY_DIR}/${rel_overlay} -> ${TARGET_DIR}/${rel_overlay}"
  done
  echo "[seed_openclaw_data] DRY-RUN ensure dir: ${TARGET_DIR}/state/a-share-volume-watch"
  echo "[seed_openclaw_data] DRY-RUN launcher: ${TARGET_DIR}/${LAUNCHER_REL} executes /app/.venv/bin/python"
  echo "[seed_openclaw_data] DRY-RUN total size (${TOTAL_SIZE_LINE:-Total file size: 0 B})"
  exit 0
fi

echo "[seed_openclaw_data] Mode: APPLY"
echo "[seed_openclaw_data] Source: ${SOURCE_DIR}/"
echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
mkdir -p "${TARGET_DIR}"
seed_rsync "${CODE_FILES_TMP}" --exclude=state/ --exclude=data/ --exclude=temp/ --exclude=events/ --exclude=model_cards/
seed_rsync "${STATE_FILES_TMP}" --ignore-existing
mkdir -p "${TARGET_DIR}/state/a-share-volume-watch"

for rel_overlay in "${OVERLAY_FILES[@]}"; do
  mkdir -p "$(dirname "${TARGET_DIR}/${rel_overlay}")"
  cp -p "${OVERLAY_DIR}/${rel_overlay}" "${TARGET_DIR}/${rel_overlay}"
  echo "[seed_openclaw_data] Applied overlay: ${OVERLAY_DIR}/${rel_overlay} -> ${TARGET_DIR}/${rel_overlay}"
done

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
