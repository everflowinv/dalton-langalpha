#!/usr/bin/env bash
# Plan, apply, and rollback migration of the token usage collector and ledger
# from OpenClaw (ai.openclaw.model-usage-collector / ~/.openclaw/usage-accounting)
# to Dalton (com.dalton.usage-collector / ~/Library/Application Support/Everflow/usage-accounting).
# Uses ~/.config/dalton/usage-state-dir as the active ledger pointer (mode 0600).
# Default mode is --plan (read-only). Compatible with macOS Bash 3.2.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../.." && pwd)}"

OLD_LABEL="${DALTON_USAGE_OLD_LABEL:-ai.openclaw.model-usage-collector}"
NEW_LABEL="${DALTON_USAGE_NEW_LABEL:-com.dalton.usage-collector}"

RAW_SRC_STATE_DIR="${DALTON_USAGE_SRC_STATE_DIR:-${HOME}/.openclaw/usage-accounting}"
RAW_DST_STATE_DIR="${DALTON_USAGE_STATE_DIR:-${HOME}/Library/Application Support/Everflow/usage-accounting}"
POINTER_FILE="${DALTON_USAGE_STATE_POINTER_FILE:-${HOME}/.config/dalton/usage-state-dir}"
LAUNCH_AGENTS_DIR="${DALTON_LAUNCH_AGENTS_DIR:-${HOME}/Library/LaunchAgents}"
LOGS_DIR="${DALTON_LOGS_DIR:-${HOME}/Library/Logs}"

SKILL_DIR="${DALTON_USAGE_SKILL_DIR:-${REPO_ROOT}/scripts/dalton/data_assets/skills/model-usage-cost}"
SKILL_SCRIPTS_DIR="${SKILL_DIR}/scripts"
HOMEBREW_PY312="${DALTON_HOMEBREW_PYTHON_312:-/opt/homebrew/bin/python3.12}"

LAUNCHCTL_CMD="${LAUNCHCTL_BIN:-launchctl}"
PLUTIL_CMD="${PLUTIL_BIN:-plutil}"
PYTHON_OVERRIDE="${DALTON_USAGE_PYTHON:-}"
UID_NUM="${DALTON_LAUNCHD_UID:-$(id -u)}"

UNLOAD_TIMEOUT_S="${USAGE_COLLECTOR_UNLOAD_TIMEOUT_S:-15}"
HEALTH_TIMEOUT_S="${USAGE_COLLECTOR_HEALTH_TIMEOUT_S:-300}"
HEALTH_INTERVAL_S="${USAGE_COLLECTOR_HEALTH_INTERVAL_S:-2}"

TEMPLATE_PATH="${REPO_ROOT}/scripts/dalton/launchd/${NEW_LABEL}.plist.template"
OLD_PLIST="${LAUNCH_AGENTS_DIR}/${OLD_LABEL}.plist"
NEW_PLIST="${LAUNCH_AGENTS_DIR}/${NEW_LABEL}.plist"

MODE="plan"
FORCE=0
TMP_ARTIFACTS=()

cleanup() {
  local rc=$?
  if [[ "${#TMP_ARTIFACTS[@]}" -gt 0 ]]; then
    local f
    for f in "${TMP_ARTIFACTS[@]}"; do
      [[ -n "${f}" && -e "${f}" ]] && rm -f "${f}" 2>/dev/null || true
    done
  fi
  return "${rc}"
}
trap cleanup EXIT INT TERM

usage() {
  cat <<'EOF'
Usage: usage_collector_switch.sh [--plan | --apply [--force] | --rollback]

Options:
  --plan       Validate collector python, print pointer/state paths, sizes, launchd status, and switch steps (default).
  --apply      Stop/disable old collector, sqlite3 backup() + integrity_check ledger, write active pointer,
               render/enable/bootstrap new collector, and wait <=300s for ok:true.
  --force      With --apply: overwrite existing destination evidence.sqlite after renaming it to
               evidence.sqlite.pre-apply.<timestamp>.
  --rollback   Bootout/disable new collector, backup() new ledger back to old dir (preserving pre-rollback),
               restore pointer, enable/bootstrap old collector, and wait <=300s for ok:true.
  -h, --help   Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --plan)
      MODE="plan"
      shift
      ;;
    --apply)
      MODE="apply"
      shift
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --rollback)
      MODE="rollback"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${FORCE}" -eq 1 && "${MODE}" != "apply" ]]; then
  echo "ERROR: --force can only be combined with --apply" >&2
  exit 2
fi

resolve_collector_python() {
  local venv_py="${SKILL_DIR}/venv/bin/python"
  local candidate=""
  if [[ -n "${PYTHON_OVERRIDE}" ]]; then
    candidate="${PYTHON_OVERRIDE}"
  elif [[ -x "${venv_py}" ]]; then
    candidate="${venv_py}"
  elif [[ -x "${HOMEBREW_PY312}" ]]; then
    candidate="${HOMEBREW_PY312}"
  else
    echo "ERROR: collector Python not found (checked ${venv_py} and ${HOMEBREW_PY312}; refusing /usr/bin/python3)" >&2
    return 1
  fi
  if [[ ! -x "${candidate}" ]]; then
    echo "ERROR: collector Python is not executable: ${candidate}" >&2
    return 1
  fi
  if [[ "${candidate}" == "/usr/bin/python3" ]]; then
    echo "ERROR: refusing system Python /usr/bin/python3" >&2
    return 1
  fi
  printf '%s\n' "${candidate}"
}

normalize_abs_path() {
  local helper_py="$1"
  local raw_path="$2"
  "${helper_py}" - "${raw_path}" <<'PY'
import os
import sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PY
}

file_size_bytes() {
  local p="$1"
  if [[ ! -f "${p}" ]]; then
    printf '%s' "missing"
    return 0
  fi
  local sz=""
  sz="$(stat -f%z "${p}" 2>/dev/null || stat -c%s "${p}" 2>/dev/null || wc -c < "${p}" 2>/dev/null | tr -d '[:space:]')"
  printf '%s bytes' "${sz:-0}"
}

launchd_label_loaded() {
  local label="$1"
  "${LAUNCHCTL_CMD}" print "gui/${UID_NUM}/${label}" >/dev/null 2>&1
}

launchd_label_status() {
  local label="$1"
  if launchd_label_loaded "${label}"; then
    printf '%s' "loaded"
  else
    printf '%s' "not_loaded"
  fi
}

wait_label_unloaded() {
  local label="$1"
  local helper_py="$2"
  local deadline_epoch now_epoch
  now_epoch="$(date +%s)"
  deadline_epoch=$((now_epoch + UNLOAD_TIMEOUT_S))
  while true; do
    if ! launchd_label_loaded "${label}"; then
      return 0
    fi
    now_epoch="$(date +%s)"
    if [[ "${now_epoch}" -ge "${deadline_epoch}" ]]; then
      return 1
    fi
    "${helper_py}" -c "import time; time.sleep(0.5)" 2>/dev/null || sleep 1
  done
}

utc_now_iso() {
  local helper_py="$1"
  "${helper_py}" -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).isoformat())"
}

utc_stamp() {
  local helper_py="$1"
  "${helper_py}" -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))"
}

read_pointer_dir() {
  local helper_py="$1"
  "${helper_py}" - "${SKILL_SCRIPTS_DIR}" "${POINTER_FILE}" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from usage_state import read_usage_state_pointer

pointed = read_usage_state_pointer(sys.argv[2])
if pointed is not None:
    print(str(pointed))
PY
}

write_pointer_dir() {
  local helper_py="$1"
  local target_dir="$2"
  "${helper_py}" - "${SKILL_SCRIPTS_DIR}" "${POINTER_FILE}" "${target_dir}" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from usage_state import write_usage_state_pointer

write_usage_state_pointer(sys.argv[3], pointer_file=sys.argv[2])
PY
}

render_plist_template() {
  local helper_py="$1"
  local template_path="$2"
  local dest_path="$3"
  local dst_state_dir="$4"
  local usage_python="${5:-}"
  local tmp_path="${dest_path}.tmp.$$"
  TMP_ARTIFACTS+=("${tmp_path}")
  if ! "${helper_py}" - "${template_path}" "${tmp_path}" "${REPO_ROOT}" "${HOME}" "${LOGS_DIR}" "${dst_state_dir}" "${usage_python}" <<'PY'
import re
import sys
from xml.sax.saxutils import escape

tpl_path, out_path, repo, home, log_dir, state_dir, usage_py = sys.argv[1:8]
with open(tpl_path, "r", encoding="utf-8") as f:
    text = f.read()

replacements = {
    "__REPO__": escape(repo),
    "__HOME__": escape(home),
    "__LOG_DIR__": escape(log_dir),
    "__STATE_DIR__": escape(state_dir),
}
for k, v in replacements.items():
    text = text.replace(k, v)

if usage_py:
    text = text.replace("__USAGE_PYTHON__", escape(usage_py))
else:
    text = re.sub(
        r"\n[ \t]*<key>DALTON_USAGE_PYTHON</key>\s*\n[ \t]*<string>__USAGE_PYTHON__</string>",
        "",
        text,
    )

with open(out_path, "w", encoding="utf-8") as f:
    f.write(text)
PY
  then
    echo "[error] rendering ${template_path} failed" >&2
    return 1
  fi
  chmod 0644 "${tmp_path}" && mv -f "${tmp_path}" "${dest_path}" || return 1
}

backup_sqlite_and_status() {
  local helper_py="$1"
  local src_dir="$2"
  local dst_dir="$3"
  local archive_tag="$4"  # "" (require absent), "pre-apply", or "pre-rollback"

  local src_db="${src_dir}/evidence.sqlite"
  local dst_db="${dst_dir}/evidence.sqlite"
  local src_status="${src_dir}/collector-status.json"
  local dst_status="${dst_dir}/collector-status.json"

  if [[ ! -f "${src_db}" ]]; then
    echo "[error] source ledger missing: ${src_db}" >&2
    return 1
  fi

  mkdir -p "${dst_dir}"
  chmod 0700 "${dst_dir}" 2>/dev/null || true

  local tmp_db="${dst_db}.tmp.$$"
  TMP_ARTIFACTS+=("${tmp_db}")
  rm -f "${tmp_db}"

  local stamp=""
  stamp="$(utc_stamp "${helper_py}")"

  if ! "${helper_py}" - "${src_db}" "${tmp_db}" "${dst_db}" "${archive_tag}" "${stamp}" <<'PY'
import os
import sqlite3
import sys

src_path, tmp_path, dst_path, archive_tag, stamp = sys.argv[1:6]

src_conn = sqlite3.connect(src_path, timeout=30)
try:
    dst_conn = sqlite3.connect(tmp_path)
    try:
        src_conn.backup(dst_conn)
        row = dst_conn.execute("PRAGMA integrity_check;").fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError(f"integrity_check failed: {row!r}")
    finally:
        dst_conn.close()
finally:
    src_conn.close()

os.chmod(tmp_path, 0o600)

if os.path.exists(dst_path):
    if not archive_tag:
        raise FileExistsError(f"destination already exists: {dst_path}")
    # Flush any existing WAL before archiving the previous database file.
    try:
        old_conn = sqlite3.connect(dst_path, timeout=30)
        try:
            old_conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        finally:
            old_conn.close()
    except sqlite3.Error:
        pass
    for suffix in ("-wal", "-shm"):
        sidecar = dst_path + suffix
        if os.path.exists(sidecar):
            os.unlink(sidecar)
    backup_name = f"{dst_path}.{archive_tag}.{stamp}"
    os.replace(dst_path, backup_name)
    os.chmod(backup_name, 0o600)

os.replace(tmp_path, dst_path)
os.chmod(dst_path, 0o600)
PY
  then
    rm -f "${tmp_db}" 2>/dev/null || true
    echo "[error] sqlite3 backup/integrity_check failed: ${src_db} -> ${dst_db}" >&2
    return 1
  fi

  if [[ -f "${src_status}" ]]; then
    local tmp_status="${dst_status}.tmp.$$"
    TMP_ARTIFACTS+=("${tmp_status}")
    rm -f "${tmp_status}"
    if ! cp "${src_status}" "${tmp_status}" || ! chmod 0600 "${tmp_status}" || ! mv -f "${tmp_status}" "${dst_status}"; then
      rm -f "${tmp_status}" 2>/dev/null || true
      echo "[error] failed copying ${src_status} -> ${dst_status}" >&2
      return 1
    fi
  fi
  return 0
}

check_collector_ready() {
  local helper_py="$1"
  local state_dir="$2"
  local since_utc="$3"
  local status_file="${state_dir}/collector-status.json"
  if [[ ! -s "${status_file}" ]]; then
    return 1
  fi
  "${helper_py}" - "${status_file}" "${since_utc}" <<'PY'
import datetime
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
start_utc = sys.argv[2]

try:
    data = json.loads(status_path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

if not isinstance(data, dict) or data.get("ok") is not True:
    sys.exit(1)

collected_at = str(data.get("collected_at") or "").strip()
if not collected_at:
    sys.exit(1)

def parse_iso(raw: str) -> datetime.datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt_obj = datetime.datetime.fromisoformat(raw)
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=datetime.timezone.utc)
    return dt_obj

try:
    dt_collected = parse_iso(collected_at)
    dt_start = parse_iso(start_utc)
except Exception:
    sys.exit(1)

sys.exit(0 if dt_collected > dt_start else 1)
PY
}

wait_for_collector_ready() {
  local helper_py="$1"
  local state_dir="$2"
  local since_utc="$3"
  local deadline_epoch now_epoch
  now_epoch="$(date +%s)"
  deadline_epoch=$((now_epoch + HEALTH_TIMEOUT_S))
  while true; do
    if check_collector_ready "${helper_py}" "${state_dir}" "${since_utc}"; then
      return 0
    fi
    now_epoch="$(date +%s)"
    if [[ "${now_epoch}" -ge "${deadline_epoch}" ]]; then
      return 1
    fi
    "${helper_py}" -c "import time; time.sleep(${HEALTH_INTERVAL_S})" 2>/dev/null || sleep 1
  done
}

rollback_to_old() {
  local helper_py="$1"
  local src_dir="$2"
  local fallback_dst_dir="${3:-}"
  local step_reports=()
  local failed_steps=()
  local cur_ptr=""
  cur_ptr="$(read_pointer_dir "${helper_py}" || true)"
  local preserved_dir="${cur_ptr:-${fallback_dst_dir:-none}}"

  emit_rollback_summary() {
    echo "回滚分步结果 (steps): [${step_reports[*]}]" >&2
    echo "回滚失败项 (failed): [${failed_steps[*]:-none}]" >&2
    echo "新位置文件已保留 (preserved): ${preserved_dir}" >&2
  }

  # Rollback step 1: bootout & disable new collector, confirm stopped.
  local stop_new_ok=1
  if launchd_label_loaded "${NEW_LABEL}"; then
    if ! "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${NEW_LABEL}"; then
      echo "[rollback][error] bootout ${NEW_LABEL} failed" >&2
      stop_new_ok=0
    elif ! wait_label_unloaded "${NEW_LABEL}" "${helper_py}"; then
      echo "[rollback][error] ${NEW_LABEL} did not unload within ${UNLOAD_TIMEOUT_S}s" >&2
      stop_new_ok=0
    fi
  fi
  if [[ "${stop_new_ok}" -eq 1 ]]; then
    if ! "${LAUNCHCTL_CMD}" disable "gui/${UID_NUM}/${NEW_LABEL}"; then
      echo "[rollback][error] disable ${NEW_LABEL} failed" >&2
      stop_new_ok=0
    fi
  fi
  if [[ "${stop_new_ok}" -ne 1 ]]; then
    step_reports+=("step1_stop_new:failed(${NEW_LABEL})")
    failed_steps+=("step1_stop_new:${NEW_LABEL}")
    step_reports+=("step2_copy_back:skipped(step1_failed)")
    step_reports+=("step3_restore_pointer:skipped(step1_failed)")
    step_reports+=("step4_restore_old:skipped(step1_failed)")
    echo "[rollback][error] ${NEW_LABEL} could not be stopped; halting rollback to avoid running both collectors simultaneously." >&2
    emit_rollback_summary
    return 1
  fi
  step_reports+=("step1_stop_new:ok(${NEW_LABEL})")

  # Rollback step 2: determine new ledger solely from active pointer; copy back only if switched.
  if [[ -z "${cur_ptr}" || "${cur_ptr}" == "${src_dir}" ]]; then
    echo "[rollback] step 2: pointer ${POINTER_FILE} is ${cur_ptr:-absent} (not switched); skipping copy back to ${src_dir}"
    step_reports+=("step2_copy_back:skipped(not_switched)")
  elif [[ ! -f "${cur_ptr}/evidence.sqlite" ]]; then
    echo "[rollback] step 2: no ledger at pointed dir ${cur_ptr}/evidence.sqlite; skipping copy back"
    step_reports+=("step2_copy_back:skipped(new_ledger_missing)")
  else
    echo "[rollback] step 2: sqlite3 backup() ${cur_ptr}/evidence.sqlite -> ${src_dir}/evidence.sqlite (preserving pre-rollback)"
    if backup_sqlite_and_status "${helper_py}" "${cur_ptr}" "${src_dir}" "pre-rollback"; then
      step_reports+=("step2_copy_back:ok(${cur_ptr}->${src_dir})")
    else
      step_reports+=("step2_copy_back:failed(${cur_ptr}->${src_dir})")
      failed_steps+=("step2_copy_back")
      step_reports+=("step3_restore_pointer:skipped(step2_failed)")
      step_reports+=("step4_restore_old:skipped(step2_failed)")
      echo "[rollback][error] backup() failed; ${NEW_LABEL} is stopped and pointer ${POINTER_FILE} still points to ${cur_ptr} so reports keep reading the complete new ledger. Manual recovery: verify ${cur_ptr}/evidence.sqlite, copy via sqlite3 backup() to ${src_dir}/evidence.sqlite, update ${POINTER_FILE} to ${src_dir}, then enable & bootstrap ${OLD_LABEL}." >&2
      emit_rollback_summary
      return 1
    fi
  fi

  # Rollback step 3: restore pointer to old dir (when pointer exists).
  if [[ -e "${POINTER_FILE}" ]]; then
    if write_pointer_dir "${helper_py}" "${src_dir}"; then
      step_reports+=("step3_restore_pointer:ok(${src_dir})")
    else
      echo "[rollback][error] failed restoring pointer ${POINTER_FILE} -> ${src_dir}" >&2
      step_reports+=("step3_restore_pointer:failed")
      failed_steps+=("step3_restore_pointer")
      step_reports+=("step4_restore_old:skipped(step3_failed)")
      emit_rollback_summary
      return 1
    fi
  else
    step_reports+=("step3_restore_pointer:skipped(pointer_absent)")
  fi

  # Rollback step 4: enable & bootstrap old collector, wait for one ok=true run.
  local rollback_start_utc=""
  rollback_start_utc="$(utc_now_iso "${helper_py}")"
  local old_ok=1
  if ! "${LAUNCHCTL_CMD}" enable "gui/${UID_NUM}/${OLD_LABEL}"; then
    echo "[rollback][error] enable ${OLD_LABEL} failed" >&2
    old_ok=0
  fi
  if [[ "${old_ok}" -eq 1 ]] && ! launchd_label_loaded "${OLD_LABEL}"; then
    if [[ ! -f "${OLD_PLIST}" ]]; then
      echo "[rollback][error] missing old plist: ${OLD_PLIST}" >&2
      old_ok=0
    elif ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${OLD_PLIST}"; then
      echo "[rollback][error] bootstrap ${OLD_PLIST} failed" >&2
      old_ok=0
    fi
  fi
  if [[ "${old_ok}" -eq 1 ]]; then
    echo "[rollback] step 4: waiting <=${HEALTH_TIMEOUT_S}s for ${OLD_LABEL} ok=true & collected_at > ${rollback_start_utc}"
    if ! wait_for_collector_ready "${helper_py}" "${src_dir}" "${rollback_start_utc}"; then
      echo "[rollback][error] ${OLD_LABEL} did not produce ok=true within ${HEALTH_TIMEOUT_S}s" >&2
      old_ok=0
    fi
  fi
  if [[ "${old_ok}" -eq 1 ]]; then
    step_reports+=("step4_restore_old:ok(${OLD_LABEL})")
  else
    step_reports+=("step4_restore_old:failed(${OLD_LABEL})")
    failed_steps+=("step4_restore_old:${OLD_LABEL}")
  fi

  # Rollback step 5: report every step clearly; preserve new directory files.
  emit_rollback_summary

  if [[ "${#failed_steps[@]}" -gt 0 ]]; then
    return 1
  fi
  return 0
}

run_plan() {
  local collector_py=""
  collector_py="$(resolve_collector_python)" || exit 1
  local src_dir dst_dir cur_ptr
  src_dir="$(normalize_abs_path "${collector_py}" "${RAW_SRC_STATE_DIR}")"
  dst_dir="$(normalize_abs_path "${collector_py}" "${RAW_DST_STATE_DIR}")"
  cur_ptr="$(read_pointer_dir "${collector_py}" || true)"

  echo "=== Dalton Token Usage Collector Switch Plan (dry-run) ==="
  echo "REPO_ROOT:         ${REPO_ROOT}"
  echo "COLLECTOR_PYTHON:  ${collector_py}"
  echo "POINTER_FILE:      ${POINTER_FILE} (current=${cur_ptr:-unset -> ${src_dir}})"
  echo "OLD_LABEL:         ${OLD_LABEL} ($(launchd_label_status "${OLD_LABEL}"), plist=$(if [[ -f "${OLD_PLIST}" ]]; then echo present; else echo missing; fi))"
  echo "NEW_LABEL:         ${NEW_LABEL} ($(launchd_label_status "${NEW_LABEL}"), plist=$(if [[ -f "${NEW_PLIST}" ]]; then echo present; else echo missing; fi))"
  echo "TEMPLATE_PATH:     ${TEMPLATE_PATH}"
  echo "NEW_PLIST:         ${NEW_PLIST}"
  echo "LOGS_DIR:          ${LOGS_DIR} (dalton-usage-collector.{log,err.log})"
  echo "SRC_STATE_DIR:     ${src_dir} (read-only copy; never deleted or moved)"
  echo "  - evidence.sqlite:       $(file_size_bytes "${src_dir}/evidence.sqlite")"
  echo "  - collector-status.json: $(file_size_bytes "${src_dir}/collector-status.json")"
  echo "DST_STATE_DIR:     ${dst_dir}"
  echo "  - evidence.sqlite:       $(file_size_bytes "${dst_dir}/evidence.sqlite")"
  echo "  - collector-status.json: $(file_size_bytes "${dst_dir}/collector-status.json")"
  echo ""
  echo "Apply sequence (--apply):"
  echo "  0. pre-check: if pointer==${dst_dir} and ${NEW_LABEL} loaded -> exit 0; if ${dst_dir}/evidence.sqlite exists without --force -> refuse"
  echo "  1. bootout & disable ${OLD_LABEL} (wait <=${UNLOAD_TIMEOUT_S}s)"
  echo "  2. sqlite3 backup() + PRAGMA integrity_check: ${src_dir}/evidence.sqlite -> ${dst_dir}/evidence.sqlite (0600), copy collector-status.json (0600)"
  echo "  3. write active pointer ${POINTER_FILE} -> ${dst_dir} (atomic, 0600)"
  echo "  4. render ${TEMPLATE_PATH} (DALTON_USAGE_STATE_DIR=${dst_dir}) -> ${NEW_PLIST}, plutil -lint, enable & bootstrap ${NEW_LABEL}"
  echo "  5. wait <=${HEALTH_TIMEOUT_S}s for ${dst_dir}/collector-status.json ok=true & collected_at > switch_start"
  echo ""
  echo "Failure / Rollback behavior:"
  echo "  - Rollback stops/disables ${NEW_LABEL}, copies new ledger back to ${src_dir} (if pointer==${dst_dir}, preserving pre-rollback), restores pointer, enables/bootstraps ${OLD_LABEL} & waits <=${HEALTH_TIMEOUT_S}s for ok=true."
  echo "  - Manual rollback command: bash scripts/dalton/ops/usage_collector_switch.sh --rollback"
}

run_apply() {
  local collector_py=""
  collector_py="$(resolve_collector_python)" || exit 1
  local src_dir dst_dir cur_ptr
  src_dir="$(normalize_abs_path "${collector_py}" "${RAW_SRC_STATE_DIR}")"
  dst_dir="$(normalize_abs_path "${collector_py}" "${RAW_DST_STATE_DIR}")"
  cur_ptr="$(read_pointer_dir "${collector_py}" || true)"

  # Pre-check 1: already switched and new collector is loaded -> no-op exit 0.
  if [[ -n "${cur_ptr}" && "${cur_ptr}" == "${dst_dir}" ]] && launchd_label_loaded "${NEW_LABEL}"; then
    echo "已切换：指针 (${POINTER_FILE}) 已指向 ${dst_dir} 且 ${NEW_LABEL} 已加载，不做任何改动。"
    exit 0
  fi

  # Pre-check 2: refuse overwriting existing destination ledger unless --force is given.
  if [[ -e "${dst_dir}/evidence.sqlite" && "${FORCE}" -eq 0 ]]; then
    echo "ERROR: 目标目录已存在账本 ${dst_dir}/evidence.sqlite，拒绝覆盖。如确认要以旧账本覆盖并保留原文件备份，请加 --force。" >&2
    exit 1
  fi

  echo "=== Dalton Token Usage Collector Switch (--apply) ==="
  mkdir -p "${LAUNCH_AGENTS_DIR}" "${LOGS_DIR}"

  echo "[step 1] bootout & disable ${OLD_LABEL}"
  if launchd_label_loaded "${OLD_LABEL}"; then
    if ! "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${OLD_LABEL}"; then
      echo "[error] bootout ${OLD_LABEL} failed; rolling back..." >&2
      rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
      exit 1
    fi
    if ! wait_label_unloaded "${OLD_LABEL}" "${collector_py}"; then
      echo "[error] ${OLD_LABEL} did not unload within ${UNLOAD_TIMEOUT_S}s; rolling back..." >&2
      rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
      exit 1
    fi
  fi
  if ! "${LAUNCHCTL_CMD}" disable "gui/${UID_NUM}/${OLD_LABEL}"; then
    echo "[error] disable ${OLD_LABEL} failed; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi

  local archive_tag=""
  if [[ "${FORCE}" -eq 1 ]]; then
    archive_tag="pre-apply"
  fi
  echo "[step 2] sqlite3 backup() + integrity_check: ${src_dir}/evidence.sqlite -> ${dst_dir}/evidence.sqlite"
  if ! backup_sqlite_and_status "${collector_py}" "${src_dir}" "${dst_dir}" "${archive_tag}"; then
    echo "[error] step 2 failed; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi

  echo "[step 3] write active pointer ${POINTER_FILE} -> ${dst_dir} (0600)"
  if ! write_pointer_dir "${collector_py}" "${dst_dir}"; then
    echo "[error] step 3 failed writing pointer; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi

  echo "[step 4] rendering plist (DALTON_USAGE_STATE_DIR=${dst_dir}), plutil -lint, enable & bootstrap ${NEW_LABEL}"
  if [[ ! -f "${TEMPLATE_PATH}" ]]; then
    echo "[error] missing template: ${TEMPLATE_PATH}; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi
  if ! render_plist_template "${collector_py}" "${TEMPLATE_PATH}" "${NEW_PLIST}" "${dst_dir}" "${PYTHON_OVERRIDE}" || ! "${PLUTIL_CMD}" -lint "${NEW_PLIST}"; then
    echo "[error] plist render/lint failed; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi
  if ! "${LAUNCHCTL_CMD}" enable "gui/${UID_NUM}/${NEW_LABEL}"; then
    echo "[error] enable ${NEW_LABEL} failed; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi
  if launchd_label_loaded "${NEW_LABEL}"; then
    if ! "${LAUNCHCTL_CMD}" bootout "gui/${UID_NUM}/${NEW_LABEL}" || ! wait_label_unloaded "${NEW_LABEL}" "${collector_py}"; then
      echo "[error] clearing existing ${NEW_LABEL} failed; rolling back..." >&2
      rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
      exit 1
    fi
  fi

  local switch_start_utc
  switch_start_utc="$(utc_now_iso "${collector_py}")"
  if ! "${LAUNCHCTL_CMD}" bootstrap "gui/${UID_NUM}" "${NEW_PLIST}"; then
    echo "[error] bootstrap ${NEW_PLIST} failed; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi

  echo "[step 5] waiting <=${HEALTH_TIMEOUT_S}s for ${NEW_LABEL} ok=true & collected_at > ${switch_start_utc}"
  if ! wait_for_collector_ready "${collector_py}" "${dst_dir}" "${switch_start_utc}"; then
    echo "[error] ${NEW_LABEL} did not produce ok=true within ${HEALTH_TIMEOUT_S}s; rolling back..." >&2
    rollback_to_old "${collector_py}" "${src_dir}" "${dst_dir}" || true
    exit 1
  fi

  echo "[ok] switched collector to ${NEW_LABEL}; state=${dst_dir}; pointer=${POINTER_FILE}; legacy preserved at ${src_dir}"
}

run_rollback() {
  local collector_py=""
  collector_py="$(resolve_collector_python)" || exit 1
  local src_dir
  src_dir="$(normalize_abs_path "${collector_py}" "${RAW_SRC_STATE_DIR}")"

  echo "=== Dalton Token Usage Collector Rollback (--rollback) ==="
  if rollback_to_old "${collector_py}" "${src_dir}"; then
    echo "[ok] rolled back to ${OLD_LABEL}"
    exit 0
  fi
  echo "[error] rollback completed with failures" >&2
  exit 1
}

case "${MODE}" in
  plan)
    run_plan
    ;;
  apply)
    run_apply
    ;;
  rollback)
    run_rollback
    ;;
esac
