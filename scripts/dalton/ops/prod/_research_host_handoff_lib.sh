#!/usr/bin/env bash
# Shared Everflow Research Host cutover handoff library (§6.4b).
#
# Base functions (s34-r1/r2):
#   - rh_paths: canonical cutover/recovery/lock/journal/budget paths
#   - rh_acquire_ops_lock / rh_release_ops_lock: non-blocking flock on FD 8
#     (keeps FD 9 free for deploy_main.sh's deploy lock); verifies kernel flock
#   - rh_persist_recovery_bin / rh_verify_recovery_manifest: atomic sibling
#     symlink swap with fsync + trusted 5-file manifest SHA-256 verification
#   - rh_journal_set: atomic handoff-journal.json update via preflight.py + guidepoint_lock
#   - rh_require_change_allowed: unified change guard (§6.4b (8) row 2) with flock check
#   - rh_require_no_incomplete_journal / rh_require_completed_journal_for_approval
#
# Compatible with macOS Bash 3.2.

RH_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RH_OPS_LOCK_HELD="${RH_OPS_LOCK_HELD:-0}"

rh_paths() {
  export PYTHONDONTWRITEBYTECODE=1

  if [[ -n "${DALTON_REPO_ROOT:-}" ]]; then
    RH_REPO_ROOT="$(cd "${DALTON_REPO_ROOT}" && pwd)"
  else
    # Works both from scripts/dalton/ops/prod and var/dalton/cutover-snapshots/recovery-bin
    RH_REPO_ROOT="$(cd "${RH_LIB_DIR}/../../../.." && pwd)"
  fi

  RH_HOME="${DALTON_HOME:-${HOME:-}}"
  if [[ -n "${DALTON_PYTHON_BIN:-}" && -x "${DALTON_PYTHON_BIN}" ]]; then
    RH_PYTHON_BIN="${DALTON_PYTHON_BIN}"
  elif [[ -x "${RH_REPO_ROOT}/.venv/bin/python" ]]; then
    RH_PYTHON_BIN="${RH_REPO_ROOT}/.venv/bin/python"
  else
    RH_PYTHON_BIN="$(command -v python3)"
  fi

  # Trusted preflight.py owned by this library's directory (never switched to untrusted target_dir)
  RH_PREFLIGHT_PY="${RH_LIB_DIR}/preflight.py"
  if [[ ! -f "${RH_PREFLIGHT_PY}" ]]; then
    RH_PREFLIGHT_PY="${RH_REPO_ROOT}/scripts/dalton/ops/prod/preflight.py"
  fi

  RH_SNAPSHOTS_ROOT="${RH_REPO_ROOT}/var/dalton/cutover-snapshots"
  RH_CUTOVER_DIR="${RH_SNAPSHOTS_ROOT}/research-host"
  RH_RECOVERY_BIN_DIR="${RH_SNAPSHOTS_ROOT}/recovery-bin"
  RH_STAGE_A_SNAPSHOTS_DIR="${RH_SNAPSHOTS_ROOT}/stage-a"
  RH_MEASURE_BUDGETS_SNAPSHOTS_DIR="${RH_SNAPSHOTS_ROOT}/measure-budgets"
  RH_DEPLOY_SNAPSHOTS_DIR="${RH_SNAPSHOTS_ROOT}/deploy-research-host"

  RH_JOURNAL_FILE="${RH_CUTOVER_DIR}/handoff-journal.json"
  RH_ADMISSION_FILE="${RH_REPO_ROOT}/var/dalton/maintenance-admission.json"
  RH_TIMING_BUDGETS_FILE="${DALTON_TIMING_BUDGETS_FILE:-${RH_CUTOVER_DIR}/timing-budgets.env}"
  RH_BASELINE_SNAPSHOT_FILE="${RH_CUTOVER_DIR}/baseline-snapshot.json"
  RH_OPS_LOCK_FILE="${RH_CUTOVER_DIR}/.ops.lock"
}

rh_acquire_ops_lock() {
  rh_paths
  if [[ "${RH_OPS_LOCK_HELD:-0}" -eq 1 && "${DALTON_OPS_LOCK_OWNER_PID:-}" == "$$" ]]; then
    # Verify FD 8 actually holds the kernel flock on RH_OPS_LOCK_FILE (never trust flags alone)
    if PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" lock-verify \
        --lock-file "${RH_OPS_LOCK_FILE}" --lock-fd 8 >/dev/null 2>&1; then
      return 0
    fi
    RH_OPS_LOCK_HELD=0
  fi

  local lock_dir
  lock_dir="$(dirname "${RH_OPS_LOCK_FILE}")"
  if ! mkdir -p "${lock_dir}" 2>/dev/null; then
    echo "ERROR: failed to create ops lock directory ${lock_dir}" >&2
    return 1
  fi
  chmod 0700 "${lock_dir}" 2>/dev/null || true

  if ! exec 8<>"${RH_OPS_LOCK_FILE}"; then
    echo "ERROR: failed to open ops lock file ${RH_OPS_LOCK_FILE}" >&2
    return 1
  fi

  local caller_tag="${1:-${BASH_SOURCE[1]:-unknown}}"
  if ! PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B - "${RH_OPS_LOCK_FILE}" "$$" "${RH_REPO_ROOT}" "${caller_tag}" <<'PY'
import datetime
import fcntl
import os
import sys

lock_file, pid_str, repo_root, caller = sys.argv[1:5]
try:
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    holder = ""
    try:
        with open(lock_file, encoding="utf-8") as fh:
            holder = fh.read().strip()
    except OSError:
        holder = ""
    detail = f" ({holder})" if holder else ""
    print(
        f"ERROR: another research-host ops/deploy process holds {lock_file}{detail}",
        file=sys.stderr,
    )
    sys.exit(1)

os.ftruncate(8, 0)
os.lseek(8, 0, os.SEEK_SET)
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
os.write(
    8,
    f"pid={pid_str} started={ts} repo={repo_root} caller={caller}\n".encode("utf-8"),
)
os.fsync(8)
PY
  then
    { exec 8>&-; } 2>/dev/null || true
    return 1
  fi

  RH_OPS_LOCK_HELD=1
  export DALTON_OPS_LOCK_HELD_FD=8
  export DALTON_OPS_LOCK_FILE="${RH_OPS_LOCK_FILE}"
  export DALTON_OPS_LOCK_OWNER_PID="$$"
  return 0
}

rh_release_ops_lock() {
  if [[ "${RH_OPS_LOCK_HELD:-0}" -eq 1 && "${DALTON_OPS_LOCK_OWNER_PID:-}" == "$$" ]]; then
    { exec 8>&-; } 2>/dev/null || true
    RH_OPS_LOCK_HELD=0
    unset DALTON_OPS_LOCK_HELD_FD DALTON_OPS_LOCK_FILE DALTON_OPS_LOCK_OWNER_PID 2>/dev/null || true
  fi
  return 0
}

rh_persist_recovery_bin() {
  rh_paths
  local src_commit_override="${1:-${DALTON_RECOVERY_SOURCE_COMMIT:-}}"
  local src_dir="${RH_REPO_ROOT}/scripts/dalton/ops/prod"
  local gp_lock_src="${RH_REPO_ROOT}/scripts/dalton/research_runtime/guidepoint_lock.py"

  if [[ ! -f "${src_dir}/switch_research_host.sh" ]]; then
    src_dir="${RH_LIB_DIR}"
  fi
  if [[ ! -f "${gp_lock_src}" && -f "${RH_LIB_DIR}/guidepoint_lock.py" ]]; then
    gp_lock_src="${RH_LIB_DIR}/guidepoint_lock.py"
  fi

  local f
  for f in switch_research_host.sh _research_host_handoff_lib.sh wait_backend_idle.sh preflight.py; do
    if [[ ! -f "${src_dir}/${f}" ]]; then
      echo "ERROR: missing required recovery file: ${src_dir}/${f}" >&2
      return 1
    fi
  done
  if [[ ! -f "${gp_lock_src}" ]]; then
    echo "ERROR: missing required recovery file: ${gp_lock_src}" >&2
    return 1
  fi

  if ! mkdir -p "${RH_SNAPSHOTS_ROOT}" 2>/dev/null; then
    echo "ERROR: failed to create snapshots directory ${RH_SNAPSHOTS_ROOT}" >&2
    return 1
  fi
  chmod 0700 "${RH_SNAPSHOTS_ROOT}" 2>/dev/null || true

  # Build the new immutable release directory as a sibling inside RH_SNAPSHOTS_ROOT
  local new_rel_dir=""
  new_rel_dir="$(mktemp -d "${RH_SNAPSHOTS_ROOT}/.recovery-bin.rel.XXXXXX")" || return 1
  chmod 0700 "${new_rel_dir}"

  local rc=0
  cp "${src_dir}/switch_research_host.sh" "${new_rel_dir}/switch_research_host.sh" && \
  cp "${src_dir}/_research_host_handoff_lib.sh" "${new_rel_dir}/_research_host_handoff_lib.sh" && \
  cp "${src_dir}/wait_backend_idle.sh" "${new_rel_dir}/wait_backend_idle.sh" && \
  cp "${src_dir}/preflight.py" "${new_rel_dir}/preflight.py" && \
  cp "${gp_lock_src}" "${new_rel_dir}/guidepoint_lock.py" || rc=$?

  if [[ "${rc}" -ne 0 ]]; then
    rm -rf "${new_rel_dir}" 2>/dev/null || true
    echo "ERROR: failed copying files into staging recovery-bin release directory" >&2
    return 1
  fi

  chmod 0700 "${new_rel_dir}/switch_research_host.sh" "${new_rel_dir}/wait_backend_idle.sh" "${new_rel_dir}/preflight.py"
  chmod 0600 "${new_rel_dir}/_research_host_handoff_lib.sh" "${new_rel_dir}/guidepoint_lock.py"

  local src_commit="${src_commit_override}"
  if [[ -z "${src_commit}" ]]; then
    src_commit="$(git -C "${src_dir}" rev-parse HEAD 2>/dev/null || git -C "${RH_REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo "unknown")"
  fi

  # Use trusted RH_PREFLIGHT_PY to write & fsync the 5-file manifest, then verify before publishing
  if ! PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" manifest-write \
      --recovery-dir "${new_rel_dir}" \
      --source-commit "${src_commit}" \
      --file switch_research_host.sh \
      --file _research_host_handoff_lib.sh \
      --file wait_backend_idle.sh \
      --file preflight.py \
      --file guidepoint_lock.py; then
    rm -rf "${new_rel_dir}" 2>/dev/null || true
    return 1
  fi

  if ! PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" manifest-verify \
      --recovery-dir "${new_rel_dir}"; then
    rm -rf "${new_rel_dir}" 2>/dev/null || true
    echo "ERROR: staged recovery-bin release failed manifest verification" >&2
    return 1
  fi

  local new_rel_base
  new_rel_base="$(basename "${new_rel_dir}")"
  local tmp_link="${RH_SNAPSHOTS_ROOT}/.recovery-bin.link.$$.${RANDOM:-0}"
  rm -f "${tmp_link}" 2>/dev/null || true
  if ! ln -s "${new_rel_base}" "${tmp_link}"; then
    rm -rf "${new_rel_dir}" 2>/dev/null || true
    echo "ERROR: failed creating sibling symlink for recovery-bin" >&2
    return 1
  fi

  # Atomic sibling symlink swap (single POSIX rename(2) when target is absent or already a symlink)
  if [[ -L "${RH_RECOVERY_BIN_DIR}" || ! -e "${RH_RECOVERY_BIN_DIR}" ]]; then
    local prev_target=""
    if [[ -L "${RH_RECOVERY_BIN_DIR}" ]]; then
      prev_target="$(readlink "${RH_RECOVERY_BIN_DIR}" 2>/dev/null || true)"
    fi
    if ! PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B - "${tmp_link}" "${RH_RECOVERY_BIN_DIR}" "${RH_SNAPSHOTS_ROOT}" <<'PY'
import os
import sys

src_link, dst_link, parent_dir = sys.argv[1:4]
os.replace(src_link, dst_link)
try:
    dfd = os.open(parent_dir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
except OSError:
    pass
PY
    then
      rm -f "${tmp_link}" 2>/dev/null || true
      rm -rf "${new_rel_dir}" 2>/dev/null || true
      echo "ERROR: atomic symlink swap for recovery-bin failed; previous entrypoint preserved" >&2
      return 1
    fi
    # Clean up retired sibling release directory after successful swap
    if [[ -n "${prev_target}" && "${prev_target}" != "${new_rel_base}" ]]; then
      case "${prev_target}" in
        .recovery-bin.rel.*)
          rm -rf "${RH_SNAPSHOTS_ROOT}/${prev_target}" 2>/dev/null || true
          ;;
      esac
    fi
    return 0
  fi

  # Legacy upgrade path: RH_RECOVERY_BIN_DIR exists as a real non-symlink directory.
  # Never ignore rmdir/mv failures; abort and preserve previous entrypoint on any failure.
  local legacy_slot=""
  legacy_slot="$(mktemp -d "${RH_SNAPSHOTS_ROOT}/.recovery-bin.legacy.XXXXXX")" || {
    rm -f "${tmp_link}" 2>/dev/null || true
    rm -rf "${new_rel_dir}" 2>/dev/null || true
    echo "ERROR: failed creating legacy backup slot for recovery-bin migration" >&2
    return 1
  }
  local legacy_backup="${legacy_slot}/old"
  if ! mv "${RH_RECOVERY_BIN_DIR}" "${legacy_backup}"; then
    rm -f "${tmp_link}" 2>/dev/null || true
    rm -rf "${new_rel_dir}" 2>/dev/null || true
    rmdir "${legacy_slot}" 2>/dev/null || true
    echo "ERROR: failed moving legacy recovery-bin directory; previous entrypoint preserved" >&2
    return 1
  fi
  if ! mv "${tmp_link}" "${RH_RECOVERY_BIN_DIR}"; then
    echo "ERROR: failed promoting recovery-bin symlink; attempting to restore legacy directory" >&2
    if ! mv "${legacy_backup}" "${RH_RECOVERY_BIN_DIR}"; then
      echo "ERROR: CRITICAL: failed restoring legacy recovery-bin from ${legacy_backup}" >&2
    else
      rmdir "${legacy_slot}" 2>/dev/null || true
    fi
    rm -f "${tmp_link}" 2>/dev/null || true
    rm -rf "${new_rel_dir}" 2>/dev/null || true
    return 1
  fi
  rm -rf "${legacy_slot}" 2>/dev/null || true
  return 0
}

rh_verify_recovery_manifest() {
  rh_paths
  local target_dir="${1:-${RH_RECOVERY_BIN_DIR}}"
  # Always verify with the trusted caller entrypoint BEFORE executing any code in target_dir
  PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" manifest-verify \
    --recovery-dir "${target_dir}"
}

rh_journal_set() {
  rh_paths
  if [[ "$#" -lt 1 ]]; then
    echo "ERROR: rh_journal_set requires at least one key=value pair" >&2
    return 2
  fi
  PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" journal-set \
    --repo "${RH_REPO_ROOT}" \
    --ownership-file "${RH_JOURNAL_FILE}" \
    --lock-file "${RH_OPS_LOCK_FILE}" \
    --lock-fd 8 \
    "$@"
}

rh_require_change_allowed() {
  rh_paths
  local action="${1:-}"
  if [[ -z "${action}" ]]; then
    echo "ERROR: rh_require_change_allowed requires an action name" >&2
    return 2
  fi
  local lock_held=0
  if [[ "${RH_OPS_LOCK_HELD:-0}" -eq 1 && "${DALTON_OPS_LOCK_OWNER_PID:-}" == "$$" ]]; then
    lock_held=1
  fi
  PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" guard-check \
    --action "${action}" \
    --admission-file "${RH_ADMISSION_FILE}" \
    --journal-file "${RH_JOURNAL_FILE}" \
    --lock-file "${RH_OPS_LOCK_FILE}" \
    --lock-fd 8 \
    --lock-held "${lock_held}"
}

rh_require_no_incomplete_journal() {
  rh_paths
  if [[ ! -e "${RH_JOURNAL_FILE}" ]]; then
    return 0
  fi
  local completed=""
  local rc=0
  completed="$(PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" journal-read \
    --repo "${RH_REPO_ROOT}" \
    --ownership-file "${RH_JOURNAL_FILE}" \
    --key completed 2>&1)" || rc=$?
  if [[ "${rc}" -ne 0 ]]; then
    echo "ERROR: handoff-journal.json is invalid or unreadable; refusing mutating operation (${completed})" >&2
    return 1
  fi
  if [[ "${completed}" == "false" ]]; then
    local op=""
    op="$(PYTHONDONTWRITEBYTECODE=1 "${RH_PYTHON_BIN}" -B "${RH_PREFLIGHT_PY}" journal-read \
      --repo "${RH_REPO_ROOT}" \
      --ownership-file "${RH_JOURNAL_FILE}" \
      --key operation 2>/dev/null || true)"
    if [[ "${op}" == "deploy" ]]; then
      echo "ERROR: incomplete handoff-journal.json (operation=deploy, completed=false). Run deploy_main.sh --recover-deploy first." >&2
    else
      echo "ERROR: incomplete handoff-journal.json (operation=${op:-unknown}, completed=false). Run bash ${RH_RECOVERY_BIN_DIR}/switch_research_host.sh --recover first." >&2
    fi
    return 1
  fi
  if [[ "${completed}" != "true" ]]; then
    echo "ERROR: handoff-journal.json missing boolean completed=true (got '${completed}'); refusing mutating operation" >&2
    return 1
  fi
  return 0
}

rh_require_completed_journal_for_approval() {
  rh_paths
  if [[ ! -f "${RH_JOURNAL_FILE}" ]]; then
    echo "ERROR: --approve-budgets requires existing handoff-journal.json with completed == true (missing ${RH_JOURNAL_FILE})" >&2
    return 1
  fi
  rh_require_no_incomplete_journal
}
