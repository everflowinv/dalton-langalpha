#!/usr/bin/env bash
# Deploy Dalton-owned data-task scripts, skills, and helpers from
# <repo>/scripts/dalton/data_assets/ into <repo>/var/dalton/openclaw-data/,
# excluding credential filenames and ensuring state/a-share-volume-watch exists.
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
  --apply            Install Dalton data assets into the target directory.
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

ASSETS_DIR="${SCRIPT_DIR}/data_assets"
EVERFLOW_ONLY_LIST="${ASSETS_DIR}/everflow_only.txt"
ANTIGRAVITY_SERIES_MODULE_SRC="${REPO_ROOT}/src/dalton/llm/antigravity_series.py"
ANTIGRAVITY_SERIES_MODULE_REL="skills/model-usage-cost/scripts/antigravity_series.py"
ANTIGRAVITY_SERIES_JSON_REL="skills/model-usage-cost/references/antigravity_series.json"

resolve_deployment_profile() {
  local py_bin="python3"
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    py_bin="${REPO_ROOT}/.venv/bin/python"
  fi
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" "${py_bin}" - "${REPO_ROOT}" <<'PY'
import os
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
dotenv_file = repo_root / ".env"

# Load installation .env via the single-source shared loader when python-dotenv
# is installed, falling back to stdlib parsing of APP_ENV/DALTON_PROFILE on
# minimal Core hosts running system python3 without .venv.
try:
    from src.dalton.config_env import load_shared_dotenv

    if dotenv_file.is_file():
        load_shared_dotenv(dotenv_file)
except Exception:
    disabled = os.environ.get("PYTHON_DOTENV_DISABLED", "").strip().lower() in (
        "1",
        "true",
        "t",
        "yes",
        "y",
    )
    if not disabled and dotenv_file.is_file():
        parsed: dict[str, str] = {}
        for raw_line in dotenv_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k:
                parsed[k] = v
        for k in ("APP_ENV", "DALTON_PROFILE"):
            if k in parsed and k not in os.environ:
                os.environ[k] = parsed[k]

from src.dalton.profile import resolve_profile

print(resolve_profile().value)
PY
}

DEPLOYMENT_PROFILE="$(resolve_deployment_profile)"

# Required configuration keys for Everflow-only data-task scripts.
# Each entry is `CANONICAL_KEY|ALIAS1|ALIAS2...`.
# Preflight ONLY accepts keys persisted in container-mounted / bridge-readable
# config files under <TARGET_DIR> (i.e. <TARGET_DIR>/config/data_tasks.env, or
# $DALTON_DATA_TASKS_CONFIG when it resolves inside <TARGET_DIR>). Keys set
# only in backend `.env` or the seed shell environment are NOT accepted.
EVERFLOW_REQUIRED_KEY_SPECS=(
  "DALTON_DATA_TASK_BBG_HOST|DALTON_BBG_HOST"
  "DALTON_DATA_TASK_BBG_CSV_REMOTE|DALTON_BBG_CSV_REMOTE"
  "DALTON_DATA_TASK_BBG_XLSX_REMOTE|DALTON_BBG_XLSX_REMOTE"
  "DALTON_DATA_TASK_BBG_REFRESH_CMD|DALTON_BBG_REFRESH_CMD"
  "DALTON_DATA_TASK_FEISHU_TRACKING_CHAT_ID|DALTON_FEISHU_TRACKING_CHAT_ID"
  "DALTON_DATA_TASK_FEISHU_SEMI_CHAT_ID|DALTON_FEISHU_SEMI_CHAT_ID"
  "DALTON_DATA_TASK_FEISHU_ASIA_TARGET|DALTON_FEISHU_ASIA_TARGET"
  "DALTON_DATA_TASK_FEISHU_MEGACAP_TARGET|DALTON_MEGACAP_FEISHU_TARGET"
  "DALTON_DATA_TASK_FEISHU_SENTIMENT_CHAT_ID|DALTON_SENTIMENT_DIGEST_CHAT_ID|SENTIMENT_DIGEST_CHAT_ID"
  "DALTON_DATA_TASK_FEISHU_A_SHARE_DM_TARGET"
  "DALTON_DATA_TASK_DISCORD_DM_TARGET"
  "DALTON_DATA_TASK_DISCORD_DEV_CHANNEL"
  "DALTON_DATA_TASK_DISCORD_SEMI_BOTTOM_CHANNEL"
  "DALTON_SEC_USER_AGENT|DALTON_DATA_TASK_SEC_USER_AGENT|SEC_USER_AGENT"
  "DALTON_DATA_TASK_GS_PB_PYTHON"
)

_read_cfg_key_from_file() {
  local key="$1" file="$2" line lhs val last_val="" found=0
  [[ -f "$file" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ "$line" == export\ * ]]; then
      line="${line#export }"
      line="${line#"${line%%[![:space:]]*}"}"
    fi
    [[ "$line" == *=* ]] || continue
    lhs="${line%%=*}"
    lhs="${lhs%"${lhs##*[![:space:]]}"}"
    if [[ "$lhs" == "$key" ]]; then
      val="${line#*=}"
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
      if [[ ${#val} -ge 2 ]]; then
        if [[ "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then
          val="${val:1:${#val}-2}"
        elif [[ "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then
          val="${val:1:${#val}-2}"
        fi
      fi
      last_val="$val"
      found=1
    fi
  done < "$file"
  if [[ "$found" -eq 1 ]]; then
    printf '%s' "$last_val"
  fi
}

_resolve_path_inside_target() {
  local raw="$1"
  [[ -n "$raw" ]] || return 0
  local py_bin="python3"
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    py_bin="${REPO_ROOT}/.venv/bin/python"
  fi
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    DALTON_DATA_TASK_CONTAINER_WORKSPACE="${LIVE_BACKEND_CONTAINER_WS:-${DALTON_DATA_TASK_CONTAINER_WORKSPACE:-}}" \
    "${py_bin}" - "${REPO_ROOT}/src/dalton/data_jobs/task_config.py" "$TARGET_DIR" "$raw" "${LIVE_BACKEND_VAR_DIR:-/app/var/dalton}" <<'PY'
import importlib.util
import sys
from pathlib import Path

task_config_path = Path(sys.argv[1])
target = Path(sys.argv[2])
raw = sys.argv[3]
backend_var_dir = Path(sys.argv[4])

spec = importlib.util.spec_from_file_location("_seed_task_config", task_config_path)
if spec is None or spec.loader is None:
    sys.exit(0)
task_config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_config)

resolved = task_config.resolve_data_tasks_config_host_path(
    raw,
    host_data_root=target,
    backend_data_root=backend_var_dir / "openclaw-data",
)
if resolved is not None:
    print(str(resolved))
PY
}

BACKEND_ENV_FILE="${REPO_ROOT}/.env"
BACKEND_CONTAINER="${DALTON_BACKEND_CONTAINER:-dalton-langalpha-backend-1}"
SEED_CUSTOM_CFG_RAW="${DALTON_DATA_TASKS_CONFIG:-}"
SEED_CUSTOM_CFG_RAW="${SEED_CUSTOM_CFG_RAW#"${SEED_CUSTOM_CFG_RAW%%[![:space:]]*}"}"
SEED_CUSTOM_CFG_RAW="${SEED_CUSTOM_CFG_RAW%"${SEED_CUSTOM_CFG_RAW##*[![:space:]]}"}"
DOTENV_CUSTOM_CFG_RAW=""
if [[ -f "${BACKEND_ENV_FILE}" ]]; then
  DOTENV_CUSTOM_CFG_RAW="$(_read_cfg_key_from_file "DALTON_DATA_TASKS_CONFIG" "${BACKEND_ENV_FILE}")"
fi

BACKEND_LIVE_STATUS="unavailable"
LIVE_BACKEND_CUSTOM_CFG_RAW=""
LIVE_BACKEND_CONTAINER_WS=""
LIVE_BACKEND_VAR_DIR=""
_inspect_backend_container_env() {
  BACKEND_LIVE_STATUS="unavailable"
  LIVE_BACKEND_CUSTOM_CFG_RAW=""
  LIVE_BACKEND_CONTAINER_WS=""
  LIVE_BACKEND_VAR_DIR=""
  command -v docker >/dev/null 2>&1 || return 0
  local inspect_out="" inspect_rc=0 line lhs val
  if inspect_out="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${BACKEND_CONTAINER}" 2>&1)"; then
    BACKEND_LIVE_STATUS="inspected"
    while IFS= read -r line || [[ -n "$line" ]]; do
      line="${line%$'\r'}"
      [[ "$line" == *=* ]] || continue
      lhs="${line%%=*}"
      val="${line#*=}"
      val="${val#"${val%%[![:space:]]*}"}"
      val="${val%"${val##*[![:space:]]}"}"
      case "$lhs" in
        DALTON_DATA_TASKS_CONFIG)
          LIVE_BACKEND_CUSTOM_CFG_RAW="$val"
          ;;
        DALTON_DATA_TASK_CONTAINER_WORKSPACE)
          LIVE_BACKEND_CONTAINER_WS="$val"
          ;;
        DALTON_BACKEND_VAR_DIR)
          LIVE_BACKEND_VAR_DIR="$val"
          ;;
      esac
    done <<< "$inspect_out"
  else
    inspect_rc=$?
    if [[ "$inspect_out" == *"No such"* || "$inspect_out" == *"not found"* ]]; then
      BACKEND_LIVE_STATUS="not_found"
    else
      BACKEND_LIVE_STATUS="unavailable"
    fi
  fi
  return 0
}

PREFLIGHT_CONFIG_ERROR=""
ACTIVE_PREFLIGHT_CFG=""
_select_preflight_config_file() {
  PREFLIGHT_CONFIG_ERROR=""
  ACTIVE_PREFLIGHT_CFG=""
  _inspect_backend_container_env

  local effective_backend_cfg=""

  if [[ "${BACKEND_LIVE_STATUS}" == "inspected" ]]; then
    # Running/existing backend container holds the exact env task containers inherit.
    # Editing .env (or running `docker restart`) does NOT refresh container env.
    if [[ "${DOTENV_CUSTOM_CFG_RAW}" != "${LIVE_BACKEND_CUSTOM_CFG_RAW}" ]]; then
      PREFLIGHT_CONFIG_ERROR="${BACKEND_ENV_FILE} 中的 DALTON_DATA_TASKS_CONFIG（${DOTENV_CUSTOM_CFG_RAW:-<未设置>}）与运行中后端容器（${BACKEND_CONTAINER}）实际持有的值（${LIVE_BACKEND_CUSTOM_CFG_RAW:-<未设置>}）不一致；仅写入 .env 或执行 docker restart 不会更新后端进程环境，需要重建后端（docker compose up -d --no-deps --force-recreate backend）后再执行 --apply"
      return 1
    fi
    effective_backend_cfg="${LIVE_BACKEND_CUSTOM_CFG_RAW}"
  else
    # Backend container could not be inspected (stopped/missing or docker unreachable).
    if [[ -n "${DOTENV_CUSTOM_CFG_RAW}" ]]; then
      if [[ "${BACKEND_LIVE_STATUS}" == "not_found" ]]; then
        effective_backend_cfg="${DOTENV_CUSTOM_CFG_RAW}"
      else
        PREFLIGHT_CONFIG_ERROR="${BACKEND_ENV_FILE} 设置了 DALTON_DATA_TASKS_CONFIG=${DOTENV_CUSTOM_CFG_RAW}，但当前无法读取后端容器（${BACKEND_CONTAINER}）的实际进程环境，无法确认该路径已生效；若仅写入了 .env，需要重建后端（docker compose up -d --no-deps --force-recreate backend）并确保可检查后端容器后再执行 --apply"
        return 1
      fi
    fi
  fi

  # No custom path in either seed shell or backend -> container reads only default config/data_tasks.env
  if [[ -z "${SEED_CUSTOM_CFG_RAW}" && -z "${effective_backend_cfg}" ]]; then
    ACTIVE_PREFLIGHT_CFG="${TARGET_DIR}/config/data_tasks.env"
    return 0
  fi

  # Custom path passed only to seed shell without being held by backend -> container will not receive it
  if [[ -n "${SEED_CUSTOM_CFG_RAW}" && -z "${effective_backend_cfg}" ]]; then
    PREFLIGHT_CONFIG_ERROR="自定义配置路径 DALTON_DATA_TASKS_CONFIG=${SEED_CUSTOM_CFG_RAW} 仅传给了 seed，后端进程与环境文件（${BACKEND_ENV_FILE}）均未持有该值；后端启动任务容器时拿不到该路径（写入 .env 后需要重建后端）"
    return 1
  fi

  local resolved_backend=""
  resolved_backend="$(_resolve_path_inside_target "${effective_backend_cfg}")"
  if [[ -z "${resolved_backend}" ]]; then
    PREFLIGHT_CONFIG_ERROR="后端持有的 DALTON_DATA_TASKS_CONFIG=${effective_backend_cfg} 未指向 ${TARGET_DIR} 挂载目录内的有效文件"
    return 1
  fi

  if [[ -n "${SEED_CUSTOM_CFG_RAW}" ]]; then
    local resolved_seed=""
    resolved_seed="$(_resolve_path_inside_target "${SEED_CUSTOM_CFG_RAW}")"
    if [[ -z "${resolved_seed}" ]]; then
      PREFLIGHT_CONFIG_ERROR="seed 传入的 DALTON_DATA_TASKS_CONFIG=${SEED_CUSTOM_CFG_RAW} 未指向 ${TARGET_DIR} 挂载目录内的有效文件"
      return 1
    fi
    if [[ "${resolved_seed}" != "${resolved_backend}" ]]; then
      PREFLIGHT_CONFIG_ERROR="seed 传入的 DALTON_DATA_TASKS_CONFIG（${resolved_seed}）与后端实际持有的路径（${resolved_backend}）不一致"
      return 1
    fi
  fi

  ACTIVE_PREFLIGHT_CFG="${resolved_backend}"
  return 0
}

_resolve_preflight_key() {
  local key="$1" val=""
  if [[ -n "${ACTIVE_PREFLIGHT_CFG}" && -f "${ACTIVE_PREFLIGHT_CFG}" ]]; then
    val="$(_read_cfg_key_from_file "$key" "${ACTIVE_PREFLIGHT_CFG}")"
    if [[ -n "$val" ]]; then
      printf '%s' "$val"
      return 0
    fi
  fi
}

MISSING_PREFLIGHT_KEYS=()
run_everflow_preflight() {
  MISSING_PREFLIGHT_KEYS=()
  [[ "${DEPLOYMENT_PROFILE}" == "core" ]] && return 0
  if ! _select_preflight_config_file; then
    return 1
  fi
  local spec candidate canonical found
  local -a aliases
  for spec in "${EVERFLOW_REQUIRED_KEY_SPECS[@]}"; do
    IFS='|' read -r -a aliases <<< "$spec"
    canonical="${aliases[0]}"
    found=""
    for candidate in "${aliases[@]}"; do
      found="$(_resolve_preflight_key "$candidate")"
      if [[ -n "$found" ]]; then
        break
      fi
    done
    if [[ -z "$found" ]]; then
      MISSING_PREFLIGHT_KEYS+=("$canonical")
    fi
  done
  [[ ${#MISSING_PREFLIGHT_KEYS[@]} -eq 0 ]]
}

EVERFLOW_ONLY_ASSETS=()
if [[ -f "${EVERFLOW_ONLY_LIST}" ]]; then
  while IFS= read -r raw_line || [[ -n "${raw_line}" ]]; do
    raw_line="${raw_line%$'\r'}"
    entry="${raw_line%%#*}"
    entry="${entry#"${entry%%[![:space:]]*}"}"
    entry="${entry%"${entry##*[![:space:]]}"}"
    [[ -n "${entry}" ]] && EVERFLOW_ONLY_ASSETS+=("${entry}")
  done < "${EVERFLOW_ONLY_LIST}"
fi

is_everflow_only_relpath() {
  local candidate="$1"
  local item
  for item in "${EVERFLOW_ONLY_ASSETS[@]+"${EVERFLOW_ONLY_ASSETS[@]}"}"; do
    if [[ "${candidate}" == "${item}" || "${candidate}" == "${item}/"* ]]; then
      return 0
    fi
  done
  return 1
}

ASSET_FILES=()
if [[ -d "${ASSETS_DIR}" ]]; then
  while IFS= read -r rel_asset; do
    [[ -n "${rel_asset}" ]] && ASSET_FILES+=("${rel_asset}")
  done < <(
    cd "${ASSETS_DIR}"
    find . -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | sed 's|^\./||' | LC_ALL=C sort
  )
fi

SHOULD_SYNC_ANTIGRAVITY_SERIES=0
if [[ "${DEPLOYMENT_PROFILE}" != "core" && -f "${ANTIGRAVITY_SERIES_MODULE_SRC}" && -f "${ASSETS_DIR}/skills/model-usage-cost/scripts/usage_sources.py" ]]; then
  SHOULD_SYNC_ANTIGRAVITY_SERIES=1
fi

if [[ "${MODE}" == "dry-run" ]]; then
  echo "[seed_openclaw_data] Mode: DRY-RUN (pass --apply to write changes)"
  echo "[seed_openclaw_data] Profile: ${DEPLOYMENT_PROFILE}"
  echo "[seed_openclaw_data] Assets: ${ASSETS_DIR}/"
  echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
  if [[ "${DEPLOYMENT_PROFILE}" != "core" ]]; then
    if run_everflow_preflight; then
      echo "[seed_openclaw_data] DRY-RUN preflight: OK (${#EVERFLOW_REQUIRED_KEY_SPECS[@]} required keys present in ${ACTIVE_PREFLIGHT_CFG})"
    elif [[ -n "${PREFLIGHT_CONFIG_ERROR}" ]]; then
      echo "[seed_openclaw_data] DRY-RUN preflight: ${PREFLIGHT_CONFIG_ERROR}"
    else
      echo "[seed_openclaw_data] DRY-RUN preflight: missing ${#MISSING_PREFLIGHT_KEYS[@]} required key(s) in ${ACTIVE_PREFLIGHT_CFG}: ${MISSING_PREFLIGHT_KEYS[*]}"
    fi
  fi
  for rel_asset in "${ASSET_FILES[@]}"; do
    if [[ "${rel_asset}" == "everflow_only.txt" ]] || is_credential_relpath "${rel_asset}"; then
      continue
    fi
    if [[ "${DEPLOYMENT_PROFILE}" == "core" ]] && is_everflow_only_relpath "${rel_asset}"; then
      echo "[seed_openclaw_data] DRY-RUN skip (core profile): ${rel_asset}"
      continue
    fi
    echo "[seed_openclaw_data] DRY-RUN asset: ${ASSETS_DIR}/${rel_asset} -> ${TARGET_DIR}/${rel_asset}"
  done
  if [[ "${SHOULD_SYNC_ANTIGRAVITY_SERIES}" -eq 1 ]]; then
    echo "[seed_openclaw_data] DRY-RUN asset: ${ANTIGRAVITY_SERIES_MODULE_SRC} -> ${TARGET_DIR}/${ANTIGRAVITY_SERIES_MODULE_REL}"
    echo "[seed_openclaw_data] DRY-RUN export: ${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}"
  elif [[ "${DEPLOYMENT_PROFILE}" == "core" ]]; then
    echo "[seed_openclaw_data] DRY-RUN skip (core profile): ${ANTIGRAVITY_SERIES_MODULE_REL}"
  fi
  echo "[seed_openclaw_data] DRY-RUN ensure dir: ${TARGET_DIR}/state/a-share-volume-watch"
  exit 0
fi

if ! run_everflow_preflight; then
  if [[ -n "${PREFLIGHT_CONFIG_ERROR}" ]]; then
    echo "ERROR: [seed_openclaw_data] 预检失败，拒绝覆盖 ${TARGET_DIR}/scripts/：${PREFLIGHT_CONFIG_ERROR}" >&2
    echo "若使用自定义配置文件，请先将该路径写入后端环境文件（${BACKEND_ENV_FILE}）且确保文件位于 ${TARGET_DIR} 挂载目录内、包含全部必填键后再执行 --apply。" >&2
  else
    echo "ERROR: [seed_openclaw_data] 预检失败，拒绝覆盖 ${TARGET_DIR}/scripts/：容器实际读取的配置文件（${ACTIVE_PREFLIGHT_CFG}）缺少以下必填配置键（共 ${#MISSING_PREFLIGHT_KEYS[@]} 项）：" >&2
    for missing_key in "${MISSING_PREFLIGHT_KEYS[@]}"; do
      echo "  - ${missing_key}" >&2
    done
    echo "请先将上述键全部写入该单一配置文件（${ACTIVE_PREFLIGHT_CFG}，不与默认文件合并）后再执行 --apply；仅写在后端 .env、当前 shell 环境或其他文件中的键预检不予放行。" >&2
  fi
  exit 1
fi

echo "[seed_openclaw_data] Mode: APPLY"
echo "[seed_openclaw_data] Profile: ${DEPLOYMENT_PROFILE}"
echo "[seed_openclaw_data] Assets: ${ASSETS_DIR}/"
echo "[seed_openclaw_data] Target: ${TARGET_DIR}/"
mkdir -p "${TARGET_DIR}"
mkdir -p "${TARGET_DIR}/state/a-share-volume-watch"

ASSET_TMP=""
SERIES_JSON_TMP=""
cleanup_tmps() {
  [[ -z "${ASSET_TMP}" ]] || rm -f "${ASSET_TMP}"
  [[ -z "${SERIES_JSON_TMP}" ]] || rm -f "${SERIES_JSON_TMP}"
}
trap cleanup_tmps EXIT

atomic_install_file() {
  local src="$1"
  local dst="$2"
  mkdir -p "$(dirname "${dst}")"
  ASSET_TMP="$(mktemp "${dst}.XXXXXX")"
  cp -p "${src}" "${ASSET_TMP}"
  mv -f "${ASSET_TMP}" "${dst}"
  ASSET_TMP=""
}

for rel_asset in "${ASSET_FILES[@]}"; do
  if [[ "${rel_asset}" == "everflow_only.txt" ]] || is_credential_relpath "${rel_asset}"; then
    continue
  fi
  if [[ "${DEPLOYMENT_PROFILE}" == "core" ]] && is_everflow_only_relpath "${rel_asset}"; then
    echo "[seed_openclaw_data] Skip (core profile): ${rel_asset}"
    continue
  fi
  atomic_install_file "${ASSETS_DIR}/${rel_asset}" "${TARGET_DIR}/${rel_asset}"
  echo "[seed_openclaw_data] Applied asset: ${ASSETS_DIR}/${rel_asset} -> ${TARGET_DIR}/${rel_asset}"
done

if [[ "${SHOULD_SYNC_ANTIGRAVITY_SERIES}" -eq 1 ]]; then
  atomic_install_file "${ANTIGRAVITY_SERIES_MODULE_SRC}" "${TARGET_DIR}/${ANTIGRAVITY_SERIES_MODULE_REL}"
  echo "[seed_openclaw_data] Applied asset: ${ANTIGRAVITY_SERIES_MODULE_SRC} -> ${TARGET_DIR}/${ANTIGRAVITY_SERIES_MODULE_REL}"

  SERIES_PYTHON="python3"
  if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
    SERIES_PYTHON="${REPO_ROOT}/.venv/bin/python"
  fi
  mkdir -p "$(dirname "${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}")"
  SERIES_JSON_TMP="$(mktemp "${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}.XXXXXX")"
  "${SERIES_PYTHON}" - "${REPO_ROOT}" "${ASSETS_DIR}/skills/model-usage-cost/scripts" "${ANTIGRAVITY_SERIES_MODULE_SRC}" "${SERIES_JSON_TMP}" <<'PYEOF'
import importlib.util
import json
import sys
from pathlib import Path

repo_root = Path(sys.argv[1])
skill_scripts_dir = Path(sys.argv[2])
module_path = Path(sys.argv[3])
out_path = Path(sys.argv[4])

sys.path.insert(0, str(skill_scripts_dir))
import usage_sources  # noqa: E402

spec = importlib.util.spec_from_file_location("antigravity_series", module_path)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load {module_path}")
ag_mod = importlib.util.module_from_spec(spec)
sys.modules["antigravity_series"] = ag_mod
spec.loader.exec_module(ag_mod)

series = usage_sources.build_antigravity_series_from_overlays(
    (
        repo_root / "src" / "dalton" / "llm" / "native_overlay.json",
        repo_root / "var" / "dalton" / "catalog" / "native_sync_overlay.json",
    ),
    dalton_yaml_path=repo_root / "src" / "dalton" / "dalton.yaml",
    ag_mod=ag_mod,
)
out_path.write_text(json.dumps(series, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PYEOF
  chmod 644 "${SERIES_JSON_TMP}"
  mv -f "${SERIES_JSON_TMP}" "${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}"
  SERIES_JSON_TMP=""
  echo "[seed_openclaw_data] Exported catalog: ${TARGET_DIR}/${ANTIGRAVITY_SERIES_JSON_REL}"
fi
