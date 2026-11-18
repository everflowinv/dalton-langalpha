#!/usr/bin/env bash
# Plan, stage, verify, rebuild venvs, and switch research runtime from ~/.openclaw/workspace
# into $DALTON_RESEARCH_HOME. Compatible with macOS Bash 3.2.
# Default mode is --plan (read-only, prints plan only). Does NOT modify ~/.openclaw.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

MODE="plan"
SRC_ROOT="${DALTON_OCW_ROOT:-${HOME}/.openclaw/workspace}"
DST_ROOT="${DALTON_RESEARCH_HOME:-${HOME}/Library/Application Support/Everflow/research}"
CODE_ROOT="${DALTON_RESEARCH_CODE:-${REPO_ROOT}/scripts/dalton/research_runtime}"
BRIDGE_ENV_FILE="${DALTON_BRIDGE_ENV_FILE:-${HOME}/.config/dalton/researchdata-bridge.env}"
PREPARE_CONFIG="${DALTON_MARKET_DIGEST_PREPARE_CONFIG:-${HOME}/.config/dalton/market-digest-prepare.json}"
OUTPUT_CONFIG="${DALTON_MARKET_DIGEST_OUTPUT_CONFIG:-${HOME}/.config/dalton/market-digest-output.json}"
PAUSED_WRITERS=0
DO_REBUILD_VENVS=0
DO_SWITCH=0

STAGE_DIR=""
BACKUP_DIR=""
SWAP_IN_PROGRESS=0
TMP_ARTIFACTS=()

cleanup() {
    local rc=$?
    if [[ "${SWAP_IN_PROGRESS}" -eq 1 && -n "${BACKUP_DIR}" && -d "${BACKUP_DIR}" && ! -e "${DST_ROOT}" ]]; then
        mv "${BACKUP_DIR}" "${DST_ROOT}" 2>/dev/null || true
    fi
    if [[ -n "${STAGE_DIR}" && -d "${STAGE_DIR}" ]]; then
        rm -rf "${STAGE_DIR}" 2>/dev/null || true
    fi
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
Usage: research_runtime_migrate.sh [--plan | --apply --i-have-paused-writers | --rebuild-venvs | --switch]
                                   [--src DIR] [--dst DIR] [--code DIR]
                                   [--bridge-env FILE] [--prepare-config FILE] [--output-config FILE]

Options:
  --plan, --dry-run         Print migration plan, sizes, stop-write list, and commands (default).
  --apply                   Stage copy + sqlite3 .backup + integrity_check + queue path rewrite, then atomically swap into --dst.
  --i-have-paused-writers   Required confirmation for --apply that all stop-write automations/services are paused.
  --rebuild-venvs           Rebuild all 6 skill venvs under $DALTON_RESEARCH_HOME/venvs/<skill>.
  --switch                  Verify EDGAR_IDENTITY & runtime, backup/rewrite market-digest JSONs, and run install_bridge.sh --research-runtime=new.
  --src DIR                 Source workspace (default: $DALTON_OCW_ROOT or ~/.openclaw/workspace).
  --dst DIR                 Target DALTON_RESEARCH_HOME (default: ~/Library/Application Support/Everflow/research).
  --code DIR                Code directory DALTON_RESEARCH_CODE (default: <repo>/scripts/dalton/research_runtime).
  --bridge-env FILE         Bridge env file (default: ~/.config/dalton/researchdata-bridge.env).
  --prepare-config FILE     market-digest-prepare.json path (default: ~/.config/dalton/market-digest-prepare.json).
  --output-config FILE      market-digest-output.json path (default: ~/.config/dalton/market-digest-output.json).
  -h, --help                Show this help message.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --plan|--dry-run)
            MODE="plan"
            shift
            ;;
        --apply)
            MODE="apply"
            shift
            ;;
        --rebuild-venvs)
            if [[ "${MODE}" == "plan" ]]; then
                MODE="rebuild-venvs"
            fi
            DO_REBUILD_VENVS=1
            shift
            ;;
        --switch)
            if [[ "${MODE}" == "plan" ]]; then
                MODE="switch"
            fi
            DO_SWITCH=1
            shift
            ;;
        --i-have-paused-writers)
            PAUSED_WRITERS=1
            shift
            ;;
        --src)
            SRC_ROOT="${2:?missing --src value}"
            shift 2
            ;;
        --dst)
            DST_ROOT="${2:?missing --dst value}"
            shift 2
            ;;
        --code)
            CODE_ROOT="${2:?missing --code value}"
            shift 2
            ;;
        --bridge-env)
            BRIDGE_ENV_FILE="${2:?missing --bridge-env value}"
            shift 2
            ;;
        --prepare-config)
            PREPARE_CONFIG="${2:?missing --prepare-config value}"
            shift 2
            ;;
        --output-config)
            OUTPUT_CONFIG="${2:?missing --output-config value}"
            shift 2
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

human_size() {
    local target="$1"
    if [[ -e "${target}" ]]; then
        du -sh "${target}" 2>/dev/null | awk '{print $1}'
    else
        printf '%s' "missing"
    fi
}

kb_size() {
    local target="$1"
    if [[ -e "${target}" ]]; then
        du -sk "${target}" 2>/dev/null | awk '{print $1}'
    else
        printf '0'
    fi
}

find_pinned_python() {
    local ver="$1"
    local cand
    for cand in \
        "/opt/homebrew/bin/python${ver}" \
        "/opt/homebrew/opt/python@${ver}/bin/python${ver}" \
        "/usr/local/bin/python${ver}" \
        "/usr/bin/python${ver}" \
        "${HOME}/.local/bin/python${ver}"; do
        if [[ -x "${cand}" ]]; then
            printf '%s\n' "${cand}"
            return 0
        fi
    done
    if command -v "python${ver}" >/dev/null 2>&1; then
        command -v "python${ver}"
        return 0
    fi
    return 1
}

read_skill_pyvenv_mm() {
    local cached_file="$1"
    local legacy_cfg="$2"
    local cfg
    for cfg in "${cached_file}" "${legacy_cfg}"; do
        if [[ -f "${cfg}" ]]; then
            local ver=""
            if [[ "${cfg}" == *.python_version ]]; then
                ver="$(tr -d '[:space:]' < "${cfg}" 2>/dev/null || true)"
            else
                ver="$(sed -n -E 's/^[[:space:]]*version(_info)?[[:space:]]*=[[:space:]]*([0-9]+\.[0-9]+).*$/\2/p' "${cfg}" 2>/dev/null | sed -n '1p' || true)"
            fi
            if [[ "${ver}" =~ ^[0-9]+\.[0-9]+$ ]]; then
                printf '%s\n' "${ver}"
                return 0
            fi
        fi
    done
    return 1
}

read_consensus_pyvenv_mm() {
    read_skill_pyvenv_mm \
        "${DST_ROOT}/state/venvs/consensus-estimates.python_version" \
        "${SRC_ROOT}/state/venvs/consensus-estimates/pyvenv.cfg"
}

hash_file() {
    local f="$1"
    if command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$f" | awk '{print $1}'
    elif command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$f" | awk '{print $1}'
    else
        stat -f "%m:%z" "$f" 2>/dev/null || stat -c "%Y:%s" "$f"
    fi
}

resolve_helper_python() {
    if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
        printf '%s\n' "${REPO_ROOT}/.venv/bin/python"
    elif [[ -x "/opt/homebrew/bin/python3" ]]; then
        printf '%s\n' "/opt/homebrew/bin/python3"
    else
        command -v python3
    fi
}

DIR_PAIRS=(
    "wiki|wiki|company-wiki markdown docs (excludes vectors.db*)"
    "skills/market-digest/output|skills/market-digest/output|market-digest batch outputs"
    "state/sales-notes|state/sales-notes|manual sales-notes full texts, originals, and manifest"
    "state/funda-ai|state/funda-ai|FUNDA AI originals (excludes index.sqlite*)"
    "state/research-images|state/research-images|research image assets and queue (excludes index.sqlite*)"
    "state/tmtb-alphaengine|state/tmtb-alphaengine|TMTB AlphaEngine sync state"
    "skills/company-filings-alert/state|state/company-filings-alert|company-filings-alert state & enrich cache"
    "market-memory|market-memory|market-digest daily markdown logs"
)

SQLITE_ITEMS=(
    "wiki/vectors.db"
    "skills/market-digest/vectors.db"
    "state/funda-ai/index.sqlite"
    "state/research-images/index.sqlite"
)

CONSENSUS_REQ_SRC="${SRC_ROOT}/scripts/consensus_estimates_requirements.txt"
CONSENSUS_REQ_CODE="${CODE_ROOT}/scripts/consensus_estimates_requirements.txt"
CONSENSUS_REQ_REL="state/venvs/consensus-estimates.requirements.txt"
CONSENSUS_VENV_DIR="${SRC_ROOT}/state/venvs/consensus-estimates"

print_stop_write_list() {
    local uid_num
    uid_num="$(id -u)"
    echo "   - Dalton 自动化: \"市场早报\" / \"市场晚报\" (写入 skills/market-digest/{vectors.db,output}, state/funda-ai, state/research-images, state/tmtb-alphaengine, market-memory)"
    echo "   - Dalton 自动化: \"Discord wiki 频道资料自动入库\" / \"Discord eve_wiki 资料自动入库\" (写入 wiki/vectors.db, wiki/{companies,sectors}, state/research-images)"
    echo "   - Dalton Bridge 写入端点: POST /wiki/ingest, POST /sales-notes/ingest"
    echo "   - 建议维护窗口停服命令: launchctl bootout gui/${uid_num}/com.dalton.researchdata-bridge"
    echo "   - OpenClaw 侧确认保持停用: market-digest-am, market-digest-pm, wiki-auto-ingest"
}

check_active_writers() {
    local uid_num
    uid_num="$(id -u)"
    local launchctl_cmd="${LAUNCHCTL_BIN:-launchctl}"
    local default_src="${HOME}/.openclaw/workspace"
    local strict_check="${DALTON_MIGRATE_STRICT_WRITER_CHECK:-0}"
    local active=()

    if [[ -n "${LAUNCHCTL_BIN:-}" || "${SRC_ROOT}" == "${default_src}" || "${strict_check}" == "1" ]]; then
        if "${launchctl_cmd}" print "gui/${uid_num}/com.dalton.researchdata-bridge" >/dev/null 2>&1; then
            active+=("launchd service gui/${uid_num}/com.dalton.researchdata-bridge is still loaded")
        fi
    fi

    local ps_out=""
    ps_out="$("${DALTON_MIGRATE_PS_BIN:-ps}" -axo pid=,command= 2>/dev/null || true)"
    if [[ -n "${ps_out}" ]]; then
        local writer_hits=""
        writer_hits="$(printf '%s\n' "${ps_out}" | awk '
            /company_wiki_cli\.py|market_digest_cli\.py|funda_ai\.py|research_image_ingest\.py|tmtb_alphaengine_sync\.py/ && !/awk/ {
                print $1 " " $2
            }
        ' || true)"
        if [[ -n "${writer_hits}" ]]; then
            active+=("active writer process(es): ${writer_hits}")
        fi
        if [[ "${SRC_ROOT}" == "${default_src}" || "${strict_check}" == "1" ]]; then
            local bridge_hits=""
            bridge_hits="$(printf '%s\n' "${ps_out}" | awk '/researchdata_bridge\.py/ && !/awk/ && !/--check-runtime/ {print $1 " " $2}' || true)"
            if [[ -n "${bridge_hits}" ]]; then
                active+=("running researchdata_bridge.py process(es): ${bridge_hits}")
            fi
        fi
    fi

    if [[ "${#active[@]}" -gt 0 ]]; then
        echo "ERROR: active writers/services detected (pause all stop-write services before --apply):" >&2
        local item
        for item in "${active[@]}"; do
            echo "  - ${item}" >&2
        done
        return 1
    fi
    return 0
}

check_disk_space() {
    local dst_probe="${DST_ROOT}"
    while [[ ! -e "${dst_probe}" && "${dst_probe}" != "/" ]]; do
        dst_probe="$(dirname "${dst_probe}")"
    done
    local avail_kb
    avail_kb="$(df -Pk "${dst_probe}" 2>/dev/null | awk 'NR==2 {print $4}')"
    if [[ -z "${avail_kb}" || ! "${avail_kb}" =~ ^[0-9]+$ ]]; then
        echo "ERROR: unable to determine available disk space for ${dst_probe}" >&2
        return 1
    fi

    local total_kb=0
    local entry rel_src rel_dst desc kb
    for entry in "${DIR_PAIRS[@]}"; do
        IFS='|' read -r rel_src rel_dst desc <<< "${entry}"
        kb="$(kb_size "${SRC_ROOT}/${rel_src}")"
        total_kb=$((total_kb + kb))
    done

    local required_kb=$(( total_kb + total_kb / 5 + 65536 ))
    if [[ -n "${DALTON_MIGRATE_MIN_FREE_KB:-}" && "${DALTON_MIGRATE_MIN_FREE_KB}" =~ ^[0-9]+$ ]]; then
        if [[ "${DALTON_MIGRATE_MIN_FREE_KB}" -gt "${required_kb}" ]]; then
            required_kb="${DALTON_MIGRATE_MIN_FREE_KB}"
        fi
    fi

    if [[ "${avail_kb}" -lt "${required_kb}" ]]; then
        local avail_mb=$((avail_kb / 1024))
        local req_mb=$((required_kb / 1024))
        echo "ERROR: insufficient disk space on ${dst_probe}: available=${avail_mb} MiB, required=${req_mb} MiB" >&2
        return 1
    fi
    return 0
}

print_plan() {
    local dst_probe="${DST_ROOT}"
    while [[ ! -e "${dst_probe}" && "${dst_probe}" != "/" ]]; do
        dst_probe="$(dirname "${dst_probe}")"
    done
    local disk_avail
    disk_avail="$(df -h "${dst_probe}" 2>/dev/null | awk 'NR==2 {print $4 " available on " $1}')"
    local consensus_mm
    consensus_mm="$(read_consensus_pyvenv_mm || true)"

    echo "================================================================================"
    echo "Dalton Research Runtime Migration Plan (mode=${MODE})"
    echo "================================================================================"
    echo "Source (OpenClaw workspace): ${SRC_ROOT}"
    echo "Target (DALTON_RESEARCH_HOME): ${DST_ROOT}"
    echo "Code   (DALTON_RESEARCH_CODE): ${CODE_ROOT}"
    echo "Env    (bridge env): ${BRIDGE_ENV_FILE}"
    echo "Disk   (target volume): ${disk_avail:-unknown}"
    echo ""

    echo "1. 迁移前必须停写的自动化与服务名单（保持停写直到切换完成）:"
    print_stop_write_list
    echo ""

    echo "2. 待复制数据目录与文件（及当前体积）:"
    local total_kb=0
    local entry rel_src rel_dst desc sz kb
    for entry in "${DIR_PAIRS[@]}"; do
        IFS='|' read -r rel_src rel_dst desc <<< "${entry}"
        sz="$(human_size "${SRC_ROOT}/${rel_src}")"
        kb="$(kb_size "${SRC_ROOT}/${rel_src}")"
        total_kb=$((total_kb + kb))
        printf '   - %-34s %-8s -> %s/%s  (%s)\n' "${rel_src}" "[${sz}]" "${DST_ROOT}" "${rel_dst}" "${desc}"
    done
    printf '   - %-34s %-8s -> %s/%s  (requirements reference; code ships %s)\n' \
        "scripts/consensus_estimates_requirements.txt" \
        "[$(human_size "${CONSENSUS_REQ_SRC}")]" \
        "${DST_ROOT}" \
        "${CONSENSUS_REQ_REL}" \
        "${CONSENSUS_REQ_CODE}"
    printf '   - %-34s %-8s    (venv 不搬，按 --rebuild-venvs 在 \$DALTON_RESEARCH_HOME/venvs/consensus-estimates 重建; 检测到版本: %s)\n' \
        "state/venvs/consensus-estimates" \
        "[$(human_size "${CONSENSUS_VENV_DIR}")]" \
        "${consensus_mm:-python3 fallback}"
    printf '   - %-34s %-8s    (下载临时目录，不复制)\n' \
        "temp/alphaengine-mcp-downloads" \
        "[$(human_size "${SRC_ROOT}/temp/alphaengine-mcp-downloads")]"
    local total_mb=$((total_kb / 1024))
    echo "   * 合计目录估算体积: ~${total_mb} MiB (含 SQLite 文件大小; --apply 前会校验磁盘剩余空间)"
    echo ""

    echo "3. SQLite 数据库一致性复制命令（Python sqlite3 backup(dst) 先写入临时文件并通过 PRAGMA integrity_check 后再原子切换）:"
    local db_rel db_sz
    for db_rel in "${SQLITE_ITEMS[@]}"; do
        db_sz="$(human_size "${SRC_ROOT}/${db_rel}")"
        echo "   # ${db_rel} [${db_sz}]"
        echo "   python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' \"${SRC_ROOT}/${db_rel}\" \"<stage>/${db_rel}.tmp\" && sqlite3 \"<stage>/${db_rel}.tmp\" \"PRAGMA integrity_check;\""
    done
    echo ""

    echo "4. 非 SQLite 目录复制、个人配置迁移与队列路径改写:"
    for entry in "${DIR_PAIRS[@]}"; do
        IFS='|' read -r rel_src rel_dst desc <<< "${entry}"
        echo "   rsync -a --exclude='*.db' --exclude='*.sqlite' --exclude='*-wal' --exclude='*-shm' --exclude='*-journal' --exclude='*.lock' --exclude='venv/' \"${SRC_ROOT}/${rel_src}/\" \"<stage>/${rel_dst}/\""
    done
    echo "   # 复制个人配置文件与凭据/状态到 \$DALTON_RESEARCH_HOME/ (0600, 不读取内容):"
    echo "   #   skills/market-digest/config.json -> <stage>/config/config.json"
    echo "   #   config/eve_tracking_mirror.json -> <stage>/config/eve_tracking_mirror.json"
    echo "   #   config/guidepoint_mcp.env -> <stage>/config/guidepoint_mcp.env"
    echo "   #   config/guidepoint_mcp_tokens.json -> <stage>/config/guidepoint_mcp_tokens.json"
    echo "   #   state/alphaengine-desktop-watchdog.json -> <stage>/state/alphaengine-desktop-watchdog.json (存在才复制)"
    echo "   #   state/alphaengine-desktop-watchdog.last-notified -> <stage>/state/alphaengine-desktop-watchdog.last-notified (存在才复制)"
    echo "   # 改写 state/research-images/queue/*.json 中指向 ${SRC_ROOT} 的绝对路径为 ${DST_ROOT} 并校验引用文件存在"
    echo ""

    echo "5. 执行数据迁移、重建 venv 与切换步骤:"
    echo "   # Step A: 数据迁移（要求确认已停写，先写入临时目录校验，再原子切换，覆盖旧目标前保留可恢复副本）"
    echo "   bash \"${SCRIPT_DIR}/research_runtime_migrate.sh\" --apply --i-have-paused-writers"
    echo "   # Step B: 重建 6 个 skill venv 到 \$DALTON_RESEARCH_HOME/venvs/<skill>"
    echo "   bash \"${SCRIPT_DIR}/research_runtime_migrate.sh\" --rebuild-venvs"
    echo "   # Step C: 切换前先确认 ${BRIDGE_ENV_FILE} 已配置 EDGAR_IDENTITY（及 GEMINI_API_KEY）"
    echo "   # Step D: 执行切换（校验 EDGAR_IDENTITY 与 --check-runtime，备份并改写 market-digest-prepare.json / market-digest-output.json，执行 install_bridge.sh --research-runtime=new；若 com.dalton.* 常驻服务已加载，同步执行 host_services_switch.sh --apply --rerender-only）"
    echo "   bash \"${SCRIPT_DIR}/research_runtime_migrate.sh\" --switch"
    echo "================================================================================"
}

rewrite_queue_json_paths() {
    local stage_root="$1"
    local src_root="$2"
    local dst_root="$3"
    local helper_py
    helper_py="$(resolve_helper_python)"
    "${helper_py}" - "${stage_root}" "${src_root}" "${dst_root}" <<'PY'
import json
import os
import sys
import tempfile
from pathlib import Path

stage_root = Path(sys.argv[1]).resolve()
src_raw = str(Path(sys.argv[2]))
src_real = Path(sys.argv[2]).resolve()
dst_real = Path(os.path.abspath(sys.argv[3]))

queue_dir = stage_root / "state" / "research-images" / "queue"
if not queue_dir.is_dir():
    sys.exit(0)

src_prefixes = []
for cand in (str(src_real), src_raw.rstrip("/")):
    if cand and cand not in src_prefixes:
        src_prefixes.append(cand)


def rewrite_path(val: str) -> tuple[str, bool]:
    for prefix in src_prefixes:
        if val == prefix:
            return str(dst_real), True
        if val.startswith(prefix + "/"):
            rel = val[len(prefix) + 1 :]
            return str(dst_real / rel), True
    return val, False


def stage_check_path(rewritten: str) -> Path:
    dst_str = str(dst_real)
    if rewritten == dst_str:
        return stage_root
    if rewritten.startswith(dst_str + "/"):
        return stage_root / rewritten[len(dst_str) + 1 :]
    return Path(rewritten)


rewritten_jobs = 0
for qfile in sorted(queue_dir.glob("*.json")):
    if qfile.is_symlink():
        print(f"ERROR: symlink not allowed in queue: {qfile}", file=sys.stderr)
        sys.exit(1)
    try:
        job = json.loads(qfile.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: invalid queue JSON {qfile}: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(job, dict):
        continue

    changed = False
    referenced_paths: list[str] = []
    status = str(job.get("status") or "")
    kind = str(job.get("kind") or "")

    payload = job.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("path"), str) and payload["path"]:
        new_p, did = rewrite_path(payload["path"])
        if did:
            payload["path"] = new_p
            changed = True
        if kind == "file" and (did or status != "complete"):
            referenced_paths.append(new_p)

    items = job.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("path"), str) and item["path"]:
                new_p, did = rewrite_path(item["path"])
                if did:
                    item["path"] = new_p
                    changed = True
                if item.get("kind") == "file" and (did or status != "complete"):
                    referenced_paths.append(new_p)

    for ref in referenced_paths:
        target_on_stage = stage_check_path(ref)
        if not target_on_stage.is_file():
            print(
                f"ERROR: queue job {qfile.name} references missing file after migration: {ref} (checked {target_on_stage})",
                file=sys.stderr,
            )
            sys.exit(1)

    if changed:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{qfile.name}.", dir=str(qfile.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, qfile)
        rewritten_jobs += 1

print(f"[queue] verified {len(list(queue_dir.glob('*.json')))} queue job(s), rewrote paths in {rewritten_jobs}")
PY
}

apply_migration() {
    command -v sqlite3 >/dev/null 2>&1 || { echo "ERROR: sqlite3 not found" >&2; exit 1; }
    command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync not found" >&2; exit 1; }
    if [[ ! -d "${SRC_ROOT}" ]]; then
        echo "ERROR: source directory not found: ${SRC_ROOT}" >&2
        exit 1
    fi
    if [[ "${PAUSED_WRITERS}" -ne 1 ]]; then
        echo "ERROR: --apply requires --i-have-paused-writers confirmation." >&2
        echo "Please confirm all stop-write automations/services are paused:" >&2
        print_stop_write_list >&2
        exit 2
    fi

    check_active_writers
    check_disk_space

    local dst_parent
    dst_parent="$(dirname "${DST_ROOT}")"
    mkdir -p "${dst_parent}"
    STAGE_DIR="$(mktemp -d "${dst_parent}/.research_migrate_stage.XXXXXX")"
    chmod 700 "${STAGE_DIR}" || true

    local entry rel_src rel_dst desc src_dir dst_dir
    for entry in "${DIR_PAIRS[@]}"; do
        IFS='|' read -r rel_src rel_dst desc <<< "${entry}"
        src_dir="${SRC_ROOT}/${rel_src}"
        dst_dir="${STAGE_DIR}/${rel_dst}"
        if [[ ! -d "${src_dir}" ]]; then
            echo "[skip] missing source dir: ${src_dir}"
            continue
        fi
        mkdir -p "${dst_dir}"
        echo "[rsync] ${src_dir}/ -> ${dst_dir}/"
        rsync -a \
            --exclude='*.db' \
            --exclude='*.sqlite' \
            --exclude='*-wal' \
            --exclude='*-shm' \
            --exclude='*-journal' \
            --exclude='*.lock' \
            --exclude='venv/' \
            "${src_dir}/" "${dst_dir}/"
    done

    local helper_py
    helper_py="$(resolve_helper_python)"
    local db_rel src_db dst_db tmp_db chk_out chk_line
    for db_rel in "${SQLITE_ITEMS[@]}"; do
        src_db="${SRC_ROOT}/${db_rel}"
        dst_db="${STAGE_DIR}/${db_rel}"
        if [[ ! -f "${src_db}" ]]; then
            echo "[skip] missing sqlite db: ${src_db}"
            continue
        fi
        mkdir -p "$(dirname "${dst_db}")"
        tmp_db="${dst_db}.tmp.$$"
        TMP_ARTIFACTS+=("${tmp_db}")
        rm -f "${tmp_db}"
        echo "[sqlite3 backup] ${src_db} -> ${dst_db}"
        "${helper_py}" - "${src_db}" "${tmp_db}" <<'PY'
import sqlite3
import sys

src_conn = sqlite3.connect(sys.argv[1])
try:
    dst_conn = sqlite3.connect(sys.argv[2])
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
finally:
    src_conn.close()
PY
        chk_out="$(sqlite3 "${tmp_db}" "PRAGMA integrity_check;")"
        chk_line="${chk_out%%$'\n'*}"
        if [[ "${chk_line}" != "ok" ]]; then
            rm -f "${tmp_db}"
            echo "ERROR: sqlite integrity_check failed for ${dst_db}: ${chk_line}" >&2
            exit 1
        fi
        chmod 600 "${tmp_db}" || true
        mv "${tmp_db}" "${dst_db}"
    done

    rewrite_queue_json_paths "${STAGE_DIR}" "${SRC_ROOT}" "${DST_ROOT}"

    mkdir -p "${STAGE_DIR}/config" "${STAGE_DIR}/state"
    local cfg_pair src_cfg_rel dst_cfg_rel src_cfg_path dst_cfg_path
    for cfg_pair in \
        "skills/market-digest/config.json|config/config.json" \
        "config/eve_tracking_mirror.json|config/eve_tracking_mirror.json" \
        "config/guidepoint_mcp.env|config/guidepoint_mcp.env" \
        "config/guidepoint_mcp_tokens.json|config/guidepoint_mcp_tokens.json"; do
        IFS='|' read -r src_cfg_rel dst_cfg_rel <<< "${cfg_pair}"
        src_cfg_path="${SRC_ROOT}/${src_cfg_rel}"
        dst_cfg_path="${STAGE_DIR}/${dst_cfg_rel}"
        if [[ -L "${src_cfg_path}" ]]; then
            echo "ERROR: refusing to copy symlink config: ${src_cfg_path}" >&2
            exit 1
        fi
        if [[ -f "${src_cfg_path}" ]]; then
            cp -p "${src_cfg_path}" "${dst_cfg_path}"
            chmod 600 "${dst_cfg_path}"
            echo "[copy] ${src_cfg_path} -> ${dst_cfg_path} (0600)"
        else
            echo "[skip] missing personal config: ${src_cfg_path}"
        fi
    done

    local watchdog_rel src_watchdog_path dst_watchdog_path
    for watchdog_rel in \
        "state/alphaengine-desktop-watchdog.json" \
        "state/alphaengine-desktop-watchdog.last-notified"; do
        src_watchdog_path="${SRC_ROOT}/${watchdog_rel}"
        dst_watchdog_path="${STAGE_DIR}/${watchdog_rel}"
        if [[ -L "${src_watchdog_path}" ]]; then
            echo "ERROR: refusing to copy symlink state file: ${src_watchdog_path}" >&2
            exit 1
        fi
        if [[ -f "${src_watchdog_path}" ]]; then
            cp -p "${src_watchdog_path}" "${dst_watchdog_path}"
            chmod 600 "${dst_watchdog_path}"
            echo "[copy] ${src_watchdog_path} -> ${dst_watchdog_path}"
        fi
    done

    local req_dst="${STAGE_DIR}/${CONSENSUS_REQ_REL}"
    mkdir -p "$(dirname "${req_dst}")"
    if [[ -f "${CONSENSUS_REQ_SRC}" ]]; then
        cp -p "${CONSENSUS_REQ_SRC}" "${req_dst}"
        echo "[copy] ${CONSENSUS_REQ_SRC} -> ${req_dst}"
    elif [[ -f "${CONSENSUS_REQ_CODE}" ]]; then
        cp -p "${CONSENSUS_REQ_CODE}" "${req_dst}"
        echo "[copy] ${CONSENSUS_REQ_CODE} -> ${req_dst}"
    fi
    if [[ -x "${CONSENSUS_VENV_DIR}/bin/pip" ]]; then
        "${CONSENSUS_VENV_DIR}/bin/pip" freeze > "${STAGE_DIR}/state/venvs/consensus-estimates.freeze.txt" 2>/dev/null || true
    fi
    local consensus_mm=""
    consensus_mm="$(read_consensus_pyvenv_mm || true)"
    if [[ -n "${consensus_mm}" ]]; then
        printf '%s\n' "${consensus_mm}" > "${STAGE_DIR}/state/venvs/consensus-estimates.python_version"
    fi
    local filings_mm=""
    filings_mm="$(read_skill_pyvenv_mm \
        "${STAGE_DIR}/state/venvs/company-filings-alert.python_version" \
        "${SRC_ROOT}/skills/company-filings-alert/venv/pyvenv.cfg" || true)"
    if [[ -n "${filings_mm}" ]]; then
        printf '%s\n' "${filings_mm}" > "${STAGE_DIR}/state/venvs/company-filings-alert.python_version"
    fi

    # Atomic swap into DST_ROOT, preserving recoverable backup if DST_ROOT exists.
    if [[ -e "${DST_ROOT}" ]]; then
        local stamp
        stamp="$(date +%Y%m%dT%H%M%S)"
        BACKUP_DIR="${DST_ROOT}.pre-migrate.${stamp}"
        while [[ -e "${BACKUP_DIR}" ]]; do
            BACKUP_DIR="${DST_ROOT}.pre-migrate.${stamp}.$$"
        done
        SWAP_IN_PROGRESS=1
        mv "${DST_ROOT}" "${BACKUP_DIR}"
        mv "${STAGE_DIR}" "${DST_ROOT}"
        SWAP_IN_PROGRESS=0
        STAGE_DIR=""
        echo "[backup] Previous target preserved for recovery at: ${BACKUP_DIR}"
    else
        mv "${STAGE_DIR}" "${DST_ROOT}"
        STAGE_DIR=""
    fi

    echo "[done] Data migration to ${DST_ROOT} completed (source ${SRC_ROOT} left untouched)."
}

rebuild_single_venv() {
    local skill_name="$1"
    local req_file="$2"
    local py_ver="$3"
    local marker_kind="$4" # sha256 | deps_ok | none
    local venv_dir="${DST_ROOT}/venvs/${skill_name}"

    if [[ ! -f "${req_file}" ]]; then
        echo "ERROR: requirements file not found for ${skill_name}: ${req_file}" >&2
        return 1
    fi

    local base_py=""
    if [[ "${py_ver}" == "auto" ]]; then
        local legacy_mm
        legacy_mm="$(read_consensus_pyvenv_mm || true)"
        if [[ -n "${legacy_mm}" ]]; then
            base_py="$(find_pinned_python "${legacy_mm}" || true)"
        fi
        if [[ -z "${base_py}" ]]; then
            if [[ -x "/opt/homebrew/bin/python3" ]]; then
                base_py="/opt/homebrew/bin/python3"
            else
                base_py="$(command -v python3 || true)"
            fi
        fi
    else
        base_py="$(find_pinned_python "${py_ver}" || true)"
    fi

    if [[ -z "${base_py}" || ! -x "${base_py}" ]]; then
        echo "ERROR: Python ${py_ver} not found for ${skill_name} venv" >&2
        return 1
    fi

    echo "[venv] Rebuilding ${skill_name} using ${base_py} -> ${venv_dir}"
    rm -rf "${venv_dir}"
    mkdir -p "$(dirname "${venv_dir}")"
    "${base_py}" -m venv "${venv_dir}"
    "${venv_dir}/bin/python" -m pip install --upgrade pip --quiet
    "${venv_dir}/bin/python" -m pip install -r "${req_file}" --quiet

    if [[ "${marker_kind}" == "sha256" ]]; then
        hash_file "${req_file}" > "${venv_dir}/.requirements.sha256"
    elif [[ "${marker_kind}" == "deps_ok" ]]; then
        touch "${venv_dir}/.deps_ok"
    fi
}

rebuild_venvs() {
    if [[ ! -d "${CODE_ROOT}" ]]; then
        echo "ERROR: code directory not found: ${CODE_ROOT}" >&2
        exit 1
    fi
    mkdir -p "${DST_ROOT}/venvs"
    rebuild_single_venv "findata-analyst" "${CODE_ROOT}/skills/findata-analyst/requirements.txt" "3.12" "none"
    rebuild_single_venv "cn-hk-findata" "${CODE_ROOT}/skills/cn-hk-findata/requirements.txt" "3.12" "deps_ok"
    rebuild_single_venv "company-wiki" "${CODE_ROOT}/skills/company-wiki/requirements.txt" "3.14" "sha256"
    rebuild_single_venv "market-digest" "${CODE_ROOT}/skills/market-digest/requirements.txt" "3.13" "sha256"
    rebuild_single_venv "employee-reviews" "${CODE_ROOT}/skills/employee-reviews/scripts/requirements.txt" "3.14" "sha256"
    rebuild_single_venv "consensus-estimates" "${CODE_ROOT}/scripts/consensus_estimates_requirements.txt" "auto" "deps_ok"
    local filings_ver="3.12"
    filings_ver="$(read_skill_pyvenv_mm \
        "${DST_ROOT}/state/venvs/company-filings-alert.python_version" \
        "${SRC_ROOT}/skills/company-filings-alert/venv/pyvenv.cfg" || printf '3.12')"
    rebuild_single_venv "company-filings-alert" "${CODE_ROOT}/skills/company-filings-alert/requirements.txt" "${filings_ver}" "sha256"
    echo "[done] Rebuilt all 7 venvs under ${DST_ROOT}/venvs"
}

switch_runtime() {
    local helper_py
    helper_py="$(resolve_helper_python)"

    # 1. Pre-switch check: EDGAR_IDENTITY must exist and be non-empty in bridge env file.
    "${helper_py}" - "${BRIDGE_ENV_FILE}" <<'PY'
import sys
from pathlib import Path

env_path = Path(sys.argv[1]).expanduser()
if env_path.is_symlink() or not env_path.is_file():
    print("ERROR: missing bridge env file; required variable: EDGAR_IDENTITY", file=sys.stderr)
    sys.exit(1)

found_edgar = False
for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() == "EDGAR_IDENTITY":
        val = v.strip().strip("'").strip('"').strip()
        if val:
            found_edgar = True
        break

if not found_edgar:
    print("ERROR: missing required variable in bridge env file: EDGAR_IDENTITY", file=sys.stderr)
    sys.exit(1)
PY

    # 2. Verify migrated data & runtime before switching bridge env/plist.
    if [[ ! -d "${DST_ROOT}" ]]; then
        echo "ERROR: target DALTON_RESEARCH_HOME does not exist: ${DST_ROOT} (run --apply first)" >&2
        exit 1
    fi
    DALTON_BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE}" \
    DALTON_RESEARCH_CODE="${CODE_ROOT}" \
    DALTON_RESEARCH_HOME="${DST_ROOT}" \
        "${helper_py}" "${REPO_ROOT}/scripts/dalton/researchdata_bridge.py" --check-runtime >/dev/null || {
        echo "ERROR: --check-runtime failed for target ${DST_ROOT}; aborting switch" >&2
        exit 1
    }

    # 3. Validate both JSON configs first, prepare temp files for both, then backup and atomically replace.
    local rollback_manifest
    rollback_manifest="$(mktemp)"
    chmod 600 "${rollback_manifest}" || true
    TMP_ARTIFACTS+=("${rollback_manifest}")

    "${helper_py}" - "${PREPARE_CONFIG}" "${OUTPUT_CONFIG}" "${SRC_ROOT}" "${DST_ROOT}" "${CODE_ROOT}" "${rollback_manifest}" <<'PY'
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

prepare_path = Path(sys.argv[1]).expanduser()
output_path = Path(sys.argv[2]).expanduser()
src_raw = sys.argv[3].rstrip("/")
src_real = str(Path(sys.argv[3]).resolve())
dst_real = str(Path(os.path.abspath(sys.argv[4])))
code_real = str(Path(os.path.abspath(sys.argv[5])))
manifest_path = Path(sys.argv[6])

src_prefixes = []
for cand in (src_real, src_raw):
    if cand and cand not in src_prefixes:
        src_prefixes.append(cand)

CODE_KEYS = {"skill_dir", "tmtb_script"}


def rewrite_value(key: str, val: object) -> object:
    if not isinstance(val, str):
        return val
    for prefix in src_prefixes:
        if val == prefix or val.startswith(prefix + "/"):
            rel = val[len(prefix) :].lstrip("/")
            if key in CODE_KEYS:
                return code_real if not rel else str(Path(code_real) / rel)
            if key == "python" and rel.startswith("skills/"):
                parts = Path(rel).parts
                # Map <src>/skills/<skill>/venv/... -> <dst>/venvs/<skill>/...
                if len(parts) >= 3 and parts[2] == "venv":
                    rest = Path(*parts[3:]) if len(parts) > 3 else Path()
                    return str(Path(dst_real) / "venvs" / parts[1] / rest)
            return dst_real if not rel else str(Path(dst_real) / rel)
    return val


def restore_entries(entries: list[tuple[Path, Path]]) -> None:
    for cfg_path, backup in entries:
        if not backup.is_file():
            continue
        fd, tmp_name = tempfile.mkstemp(prefix=f".restore.{cfg_path.name}.", dir=str(cfg_path.parent))
        os.close(fd)
        tmp_restore = Path(tmp_name)
        shutil.copy2(backup, tmp_restore)
        os.chmod(tmp_restore, 0o600)
        os.replace(tmp_restore, cfg_path)


# Phase 1: Read and validate BOTH configs before creating any backup or temp file.
parsed_configs: list[tuple[Path, dict[str, object]]] = []
for cfg_path in (prepare_path, output_path):
    if cfg_path.is_symlink():
        print(f"ERROR: refusing to rewrite symlink config: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    if not cfg_path.is_file():
        continue
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: failed to parse JSON in {cfg_path}: {type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(raw, dict):
        print(f"ERROR: invalid JSON object in {cfg_path}", file=sys.stderr)
        sys.exit(1)
    parsed_configs.append((cfg_path, raw))

# Phase 2: Generate rewritten temp files for both configs before modifying originals.
stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
prepared: list[tuple[Path, Path, Path]] = []
try:
    for cfg_path, raw in parsed_configs:
        updated = {k: rewrite_value(k, v) for k, v in raw.items()}
        fd, tmp_name = tempfile.mkstemp(prefix=f".{cfg_path.name}.", dir=str(cfg_path.parent))
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        backup = cfg_path.with_name(f"{cfg_path.name}.bak.{stamp}")
        prepared.append((cfg_path, tmp_path, backup))
except Exception as exc:
    for _, tmp_path, _ in prepared:
        tmp_path.unlink(missing_ok=True)
    print(f"ERROR: failed preparing updated config: {exc}", file=sys.stderr)
    sys.exit(1)

# Phase 3: Create .bak.<timestamp> and atomically swap each config; roll back on error.
swapped: list[tuple[Path, Path]] = []
try:
    for cfg_path, tmp_path, backup in prepared:
        shutil.copy2(cfg_path, backup)
        os.chmod(backup, 0o600)
        os.replace(tmp_path, cfg_path)
        swapped.append((cfg_path, backup))
        print(f"[switch] updated {cfg_path} (backup: {backup})")
    manifest_lines = [f"{orig}\t{bak}" for orig, bak in swapped]
    manifest_path.write_text("\n".join(manifest_lines) + ("\n" if manifest_lines else ""), encoding="utf-8")
except Exception as exc:
    restore_entries(swapped)
    for _, tmp_path, _ in prepared:
        tmp_path.unlink(missing_ok=True)
    print(f"ERROR: failed writing updated configs (restored): {exc}", file=sys.stderr)
    sys.exit(1)
PY

    # 4. Explicitly switch bridge env & launchd plist to new runtime mode; restore everything on failure.
    # Snapshot the bridge env file and plist as they are now, so a failed install is undone
    # byte-for-byte instead of relying on a legacy reinstall that needs DALTON_OCW_ROOT in the env file.
    local bridge_plist="${DALTON_LAUNCH_AGENTS_DIR:-${HOME}/Library/LaunchAgents}/com.dalton.researchdata-bridge.plist"
    local env_snapshot="" plist_snapshot=""
    if [[ -f "${BRIDGE_ENV_FILE}" ]]; then
        env_snapshot="$(mktemp)"; TMP_ARTIFACTS+=("${env_snapshot}")
        cp -p "${BRIDGE_ENV_FILE}" "${env_snapshot}"; chmod 600 "${env_snapshot}"
    fi
    if [[ -f "${bridge_plist}" ]]; then
        plist_snapshot="$(mktemp)"; TMP_ARTIFACTS+=("${plist_snapshot}")
        cp -p "${bridge_plist}" "${plist_snapshot}"; chmod 600 "${plist_snapshot}"
    fi
    _rollback_switch() {
        local restored_summary=""
        restored_summary="$("${helper_py}" - "${rollback_manifest}" <<'PY'
import os
import shutil
import sys
import tempfile
from pathlib import Path

manifest = Path(sys.argv[1])
restored: list[str] = []
if manifest.is_file():
    for raw_line in manifest.read_text(encoding="utf-8").splitlines():
        if "\t" not in raw_line:
            continue
        orig_s, bak_s = raw_line.split("\t", 1)
        orig_p = Path(orig_s)
        bak_p = Path(bak_s)
        if not bak_p.is_file():
            continue
        fd, tmp_name = tempfile.mkstemp(prefix=f".rollback.{orig_p.name}.", dir=str(orig_p.parent))
        os.close(fd)
        tmp_p = Path(tmp_name)
        shutil.copy2(bak_p, tmp_p)
        os.chmod(tmp_p, 0o600)
        os.replace(tmp_p, orig_p)
        restored.append(f"{orig_p} <- {bak_p}")
print("; ".join(restored))
PY
)"
        if [[ -n "${restored_summary}" ]]; then
            echo "[rollback] restored configs: ${restored_summary}" >&2
        fi
        if [[ -n "${env_snapshot}" && -n "${plist_snapshot}" ]]; then
            cp -p "${env_snapshot}" "${BRIDGE_ENV_FILE}.rollback.$$" && mv -f "${BRIDGE_ENV_FILE}.rollback.$$" "${BRIDGE_ENV_FILE}"
            cp -p "${plist_snapshot}" "${bridge_plist}.rollback.$$" && mv -f "${bridge_plist}.rollback.$$" "${bridge_plist}"
            local launchctl_cmd="${LAUNCHCTL_BIN:-launchctl}"
            "${launchctl_cmd}" bootout "gui/$(id -u)/com.dalton.researchdata-bridge" 2>/dev/null || true
            if "${launchctl_cmd}" bootstrap "gui/$(id -u)" "${bridge_plist}"; then
                echo "[rollback] restored bridge env file and plist to their pre-switch contents and reloaded the bridge" >&2
            else
                echo "ERROR: restored bridge env file and plist, but reloading launchd failed; run: launchctl bootstrap gui/$(id -u) ${bridge_plist}" >&2
            fi
        else
            echo "ERROR: no pre-switch bridge snapshot found; legacy rollback removed" >&2
        fi
    }

    if ! (
        DALTON_BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE}" \
        DALTON_RESEARCH_CODE="${CODE_ROOT}" \
        DALTON_RESEARCH_HOME="${DST_ROOT}" \
            bash "${REPO_ROOT}/scripts/dalton/install_bridge.sh" --research-runtime=new
    ); then
        echo "ERROR: install_bridge.sh --research-runtime=new failed; rolling back configs and bridge snapshot" >&2
        _rollback_switch
        exit 1
    fi

    # 5. If any com.dalton.* host research service is already loaded, rerender and restart it onto the new data root.
    local launchctl_cmd="${LAUNCHCTL_BIN:-launchctl}"
    local uid_num
    uid_num="$(id -u)"
    local need_host_rerender=0
    local svc_lbl
    for svc_lbl in \
        "com.dalton.alphaengine-mcp" \
        "com.dalton.alphaengine-desktop-watchdog" \
        "com.dalton.guidepoint-mcp-proxy"; do
        if "${launchctl_cmd}" print "gui/${uid_num}/${svc_lbl}" >/dev/null 2>&1; then
            need_host_rerender=1
            break
        fi
    done
    if [[ "${need_host_rerender}" -eq 1 ]]; then
        echo "[switch] detected loaded com.dalton.* host service(s); running host_services_switch.sh --apply --rerender-only"
        if ! (
            DALTON_BRIDGE_ENV_FILE="${BRIDGE_ENV_FILE}" \
            DALTON_RESEARCH_CODE="${CODE_ROOT}" \
            DALTON_RESEARCH_HOME="${DST_ROOT}" \
                bash "${SCRIPT_DIR}/host_services_switch.sh" --apply --rerender-only
        ); then
            echo "ERROR: host_services_switch.sh --apply --rerender-only failed; rolling back configs and bridge" >&2
            _rollback_switch
            exit 1
        fi
    fi
}

if [[ "${MODE}" == "plan" && "${DO_REBUILD_VENVS}" -eq 0 && "${DO_SWITCH}" -eq 0 ]]; then
    print_plan
    exit 0
fi

if [[ "${MODE}" == "apply" ]]; then
    apply_migration
fi
if [[ "${DO_REBUILD_VENVS}" -eq 1 ]]; then
    rebuild_venvs
fi
if [[ "${DO_SWITCH}" -eq 1 ]]; then
    switch_runtime
fi
