#!/usr/bin/env bash
# B2: Sync Dalton host/deployment configs into a local git repo and push to private GitHub remote.
# Runs every 6 hours via launchd. Files are AES-256-CBC encrypted (.enc) by default using
# ~/.config/dalton/backup.key; only the narrow source/extension allowlist stays plaintext
# (with fallback secret scanning and a pre-commit guard blocking non-allowlisted plaintext).

SCRIPT_NAME="dalton_config_git_backup"
OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/dalton/ops/_common.sh
source "${OPS_DIR}/_common.sh"

TMP_DIR=""
cleanup() {
    if [[ -n "${TMP_DIR:-}" ]] && [[ -d "${TMP_DIR}" ]]; then
        rm -rf "${TMP_DIR}" 2>/dev/null || true
    fi
}
install_failure_trap cleanup

load_offsite_backup_env

REPO_DIR="${DALTON_CONFIG_BACKUP_REPO_DIR:-${HOME}/Backups/dalton-config-repo}"
PROD_CHECKOUT_DIR="${DALTON_PROD_CHECKOUT_DIR:-${HOME}/Projects/dalton-langalpha}"
PROD_VAR_DIR="${DALTON_PROD_VAR_DIR:-${PROD_CHECKOUT_DIR}/var/dalton}"
USER_CONFIG_DIR="${DALTON_USER_CONFIG_DIR:-${HOME}/.config/dalton}"
LAUNCHAGENTS_DIR="${DALTON_LAUNCHAGENTS_DIR:-${HOME}/Library/LaunchAgents}"
BACKUP_SCRIPTS_DIR="${DALTON_BACKUP_SCRIPTS_DIR:-${HOME}/Backups/dalton}"
COLIMA_DIR="${DALTON_COLIMA_DIR:-${HOME}/.colima}"

MAX_FILE_MB="${DALTON_CONFIG_MAX_FILE_MB:-20}"
VAR_EXCLUDES="${DALTON_CONFIG_VAR_EXCLUDES:-openclaw-data/temp,openclaw-data/research-outputs,pr_baseline,tmp,.dalton-runs,backups,*.dump,*.sqlite,*.sqlite-wal,*.sqlite-shm}"
USER_EXCLUDES="${DALTON_CONFIG_USER_EXCLUDES:-*.log,*.pid,*.sock}"
LAUNCHAGENTS_GLOBS="${DALTON_LAUNCHAGENTS_GLOBS:-com.dalton.*.plist}"
BACKUP_SCRIPTS_GLOBS="${DALTON_BACKUP_SCRIPTS_GLOBS:-*.sh,*.py}"
COLIMA_EXCLUDES="${DALTON_CONFIG_COLIMA_EXCLUDES:-_disks,_lima/_disks,*.img,*.qcow2,*.raw,*.vmdk,*.sock,*.pid,*.log,*.stdout,*.stderr,cidata.iso,diffdisk,datadisk}"
PROD_OVERRIDE_PATTERNS="${DALTON_CONFIG_PROD_OVERRIDE_PATTERNS:-.env,.env.local,.env.production,.env.*.overlay,*.production.yaml,agent_config.dalton*.yaml,config.dalton*.yaml,docker-compose.prod.yml,docker-compose.override.yml,data_routing.yaml,scripts/load_env.sh}"
PUSH_RETRY_DELAY="${DALTON_GIT_PUSH_RETRY_DELAY_SEC:-5}"

if [[ ! -d "${REPO_DIR}" ]] || ! git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    fail_with_alert "REPO_MISSING" "Config backup git repo missing or not initialized: ${REPO_DIR}"
fi

if ! git -C "${REPO_DIR}" remote get-url origin >/dev/null 2>&1; then
    fail_with_alert "REMOTE_MISSING" "Config backup git repo has no origin remote configured: ${REPO_DIR}"
fi

ensure_backup_key || exit 1

PY_BIN="$(resolve_python_bin)"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/dalton-cfg-backup.XXXXXX")"
STAGE_DIR="${TMP_DIR}/stage"
STAGE_LOG="${TMP_DIR}/stage.stderr"

if ! STAGE_JSON="$("${PY_BIN}" "${BACKUP_LIB}" stage-config \
    --stage-dir "${STAGE_DIR}" \
    --repo-dir "${REPO_DIR}" \
    --key-file "${DALTON_BACKUP_KEY_FILE}" \
    --max-file-mb "${MAX_FILE_MB}" \
    --prod-var-dir "${PROD_VAR_DIR}" \
    --var-excludes "${VAR_EXCLUDES}" \
    --user-config-dir "${USER_CONFIG_DIR}" \
    --user-excludes "${USER_EXCLUDES}" \
    --launchagents-dir "${LAUNCHAGENTS_DIR}" \
    --launchagents-globs "${LAUNCHAGENTS_GLOBS}" \
    --backup-scripts-dir "${BACKUP_SCRIPTS_DIR}" \
    --backup-scripts-globs "${BACKUP_SCRIPTS_GLOBS}" \
    --colima-dir "${COLIMA_DIR}" \
    --colima-excludes "${COLIMA_EXCLUDES}" \
    --prod-checkout-dir "${PROD_CHECKOUT_DIR}" \
    --prod-override-patterns "${PROD_OVERRIDE_PATTERNS}" \
    2>"${STAGE_LOG}")"; then
    STAGE_ERR="$(cat "${STAGE_LOG}" 2>/dev/null || true)"
    fail_with_alert "STAGE_FAILED" "Config staging/encryption failed: ${STAGE_ERR}"
fi

if [[ -s "${STAGE_LOG}" ]]; then
    while IFS= read -r sline; do
        [[ -n "${sline}" ]] || continue
        log_msg "INFO" "${sline}"
    done < "${STAGE_LOG}"
fi

for subdir in var_dalton dot_config_dalton launch_agents backup_scripts colima prod_overrides; do
    mkdir -p "${REPO_DIR}/${subdir}"
    rsync -a --checksum --delete "${STAGE_DIR}/${subdir}/" "${REPO_DIR}/${subdir}/" \
        || fail_with_alert "RSYNC_FAILED" "rsync failed for subdir ${subdir}"
done

cp "${STAGE_DIR}/.backup-enc-manifest.json" "${REPO_DIR}/.backup-enc-manifest.json"

# Hard guard: backup.key must never appear anywhere in the repo.
KEY_BASENAME="$(basename "${DALTON_BACKUP_KEY_FILE}")"
if find "${REPO_DIR}" -path "${REPO_DIR}/.git" -prune -o \( -name "backup.key" -o -name "backup.key.*" -o -name "${KEY_BASENAME}" \) -print | grep -q .; then
    fail_with_alert "KEY_FILE_IN_REPO" "Refusing commit: backup key file detected inside ${REPO_DIR}"
fi

# Pre-commit plaintext secret scan across all non-.enc files in the repo.
SCAN_LOG="${TMP_DIR}/scan.stderr"
if ! "${PY_BIN}" "${BACKUP_LIB}" scan-plaintext --repo-dir "${REPO_DIR}" 2>"${SCAN_LOG}"; then
    SCAN_MSG="$(tr '\n' '; ' < "${SCAN_LOG}" | sed 's/; $//')"
    fail_with_alert "PLAINTEXT_SECRET_DETECTED" "Aborted commit due to plaintext secret match: ${SCAN_MSG}"
fi

CHANGES_PORCELAIN="$(git -C "${REPO_DIR}" status --porcelain)"
DID_COMMIT=0
CHANGED_COUNT=0
if [[ -n "${CHANGES_PORCELAIN}" ]]; then
    git -C "${REPO_DIR}" add -A
    CHANGED_COUNT="$(git -C "${REPO_DIR}" status --porcelain | wc -l | tr -d ' ')"
    COMMIT_STAMP="$(date '+%Y-%m-%d %H:%M:%S')"
    COMMIT_MSG="config backup ${COMMIT_STAMP}：${CHANGED_COUNT} 个文件变化"

    git -C "${REPO_DIR}" commit -m "${COMMIT_MSG}" \
        || fail_with_alert "GIT_COMMIT_FAILED" "git commit failed in ${REPO_DIR}"
    DID_COMMIT=1
fi

BRANCH="$(git -C "${REPO_DIR}" rev-parse --abbrev-ref HEAD)"
AHEAD_COUNT=0
if git -C "${REPO_DIR}" rev-parse --verify HEAD >/dev/null 2>&1; then
    if git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
        AHEAD_COUNT="$(git -C "${REPO_DIR}" rev-list --count '@{u}..HEAD')"
    elif git -C "${REPO_DIR}" rev-parse --verify "origin/${BRANCH}" >/dev/null 2>&1; then
        AHEAD_COUNT="$(git -C "${REPO_DIR}" rev-list --count "origin/${BRANCH}..HEAD")"
    else
        AHEAD_COUNT="$(git -C "${REPO_DIR}" rev-list --count HEAD)"
    fi
fi

if [[ "${DID_COMMIT}" -eq 0 ]] && [[ "${AHEAD_COUNT}" -eq 0 ]]; then
    log_msg "INFO" "OK no changes (${STAGE_JSON})"
    finish_backup
fi

push_once() {
    if git -C "${REPO_DIR}" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
        git -C "${REPO_DIR}" push
    else
        git -C "${REPO_DIR}" push -u origin "${BRANCH}"
    fi
}

if ! push_once >/dev/null 2>"${TMP_DIR}/push1.err"; then
    PUSH_ERR1="$(tr '\n' ' ' < "${TMP_DIR}/push1.err" || true)"
    log_msg "WARN" "First git push failed (${PUSH_ERR1}); retrying in ${PUSH_RETRY_DELAY}s"
    sleep "${PUSH_RETRY_DELAY}"
    if ! push_once >/dev/null 2>"${TMP_DIR}/push2.err"; then
        PUSH_ERR2="$(tr '\n' ' ' < "${TMP_DIR}/push2.err" || true)"
        fail_with_alert "GIT_PUSH_FAILED" "git push failed after retry (local commit preserved): ${PUSH_ERR2}"
    fi
fi

if [[ "${DID_COMMIT}" -eq 1 ]]; then
    log_msg "INFO" "OK committed and pushed (${CHANGED_COUNT} files; ${STAGE_JSON})"
else
    log_msg "INFO" "OK pushed ${AHEAD_COUNT} previously unpushed commit(s) (${STAGE_JSON})"
fi
finish_backup
