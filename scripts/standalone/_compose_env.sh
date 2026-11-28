#!/usr/bin/env bash
# Shared Standalone Core Docker Compose project-name and file-chain helpers.
#
# Sourced by scripts/standalone/init.sh, scripts/standalone/upgrade.sh, and
# standalone container-recreation flows so every caller resolves the exact same
# COMPOSE_PROJECT_NAME and TLS/IPv6 compose file chain from the instance `.env`.

standalone_validate_project_name() {
  local name="$1"
  local source_label="${2:---project-name}"
  local LC_ALL=C
  if [[ -z "$name" || "$name" == *$'\n'* || ! "$name" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
    if declare -F err >/dev/null 2>&1; then
      err "Invalid ${source_label} '${name}': must match ^[a-z0-9][a-z0-9_-]*$ (lowercase ASCII letters, digits, hyphens, underscores; must start with a letter or digit)."
    else
      echo "ERROR: Invalid ${source_label} '${name}': must match ^[a-z0-9][a-z0-9_-]*$" >&2
    fi
    return 2
  fi
  return 0
}

standalone_normalize_env_project_name() {
  local raw="$1"
  printf '%s' "$raw" | LC_ALL=C tr '[:upper:]' '[:lower:]'
}

standalone_sanitize_project_name() {
  local raw="$1"
  local cleaned
  cleaned="$(printf '%s' "$raw" | LC_ALL=C tr '[:upper:]' '[:lower:]' | LC_ALL=C sed -E 's/[^a-z0-9_-]+/-/g; s/^[^a-z0-9]+//; s/[-_]+$//')"
  printf '%s' "${cleaned:-dalton-core}"
}

standalone_read_env_file_var() {
  local env_path="$1" key="$2" default_val="${3:-}"
  if [[ ! -f "$env_path" ]]; then
    printf '%s' "$default_val"
    return 0
  fi
  local val
  val="$(python3 - "$env_path" "$key" <<'PY'
import re, sys

env_path, target_key = sys.argv[1], sys.argv[2]
line_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*([=:])(.*)$")
found = None
with open(env_path, encoding="utf-8") as fh:
    for raw in fh:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = line_re.match(raw.rstrip("\r\n"))
        if not m or m.group(1) != target_key:
            continue
        val = m.group(3).strip()
        if val and val[0] in ("'", '"'):
            end = val.find(val[0], 1)
            val = val[1:end] if end > 0 else val[1:]
        elif val:
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        found = val
if found is not None:
    sys.stdout.write(found)
PY
)"
  if [[ -n "$val" ]]; then
    printf '%s' "$val"
  else
    printf '%s' "$default_val"
  fi
}

standalone_resolve_tls_overlay_file() {
  local domain="${1:-}" effective_bind="${2:-}"
  if [[ "$domain" == *:* || "$effective_bind" == *:* ]]; then
    printf '%s' "deploy/standalone/docker-compose.tls.ipv6.yml"
  else
    printf '%s' "deploy/standalone/docker-compose.tls.yml"
  fi
}

standalone_build_compose_file_value() {
  local tls_mode="${1:-loopback}" domain="${2:-localhost}" effective_bind="${3:-}"
  if [[ "$tls_mode" == "loopback" ]]; then
    printf '%s' "docker-compose.yml:docker-compose.core.yml"
    return 0
  fi
  local tls_overlay
  tls_overlay="$(standalone_resolve_tls_overlay_file "$domain" "$effective_bind")"
  printf '%s' "docker-compose.yml:docker-compose.core.yml:${tls_overlay}"
}

# Populate PROJECT_NAME, DOMAIN, TLS_MODE, COMPOSE_FILES_RAW, COMPOSE_FILE_ARGS,
# NORMALIZED_COMPOSE_FILES, COMPOSE_FILES_JOINED, and COMPOSE_PRINT_ARGS from
# <work_dir> and <env_file> (with optional explicit project name / compose list).
standalone_load_compose_env() {
  local work_dir="$1"
  local env_file="${2:-${work_dir}/.env}"
  local explicit_project="${3:-}"
  local explicit_compose_raw="${4:-}"

  work_dir="$(cd "$work_dir" && pwd)"

  if [[ -n "$explicit_project" ]]; then
    PROJECT_NAME="$explicit_project"
    standalone_validate_project_name "$PROJECT_NAME" "--project-name" || return $?
  else
    local raw_env_project
    raw_env_project="$(standalone_read_env_file_var "$env_file" "COMPOSE_PROJECT_NAME" "")"
    if [[ -n "$raw_env_project" ]]; then
      PROJECT_NAME="$(standalone_normalize_env_project_name "$raw_env_project")"
    else
      PROJECT_NAME="$(standalone_sanitize_project_name "$(basename "$work_dir")")"
    fi
    standalone_validate_project_name "$PROJECT_NAME" "COMPOSE_PROJECT_NAME" || return $?
  fi

  DOMAIN="$(standalone_read_env_file_var "$env_file" "DOMAIN" "localhost")"
  TLS_MODE="$(standalone_read_env_file_var "$env_file" "DALTON_TLS_MODE" "loopback")"
  local edge_tls_bind
  edge_tls_bind="$(standalone_read_env_file_var "$env_file" "DALTON_EDGE_TLS_BIND" "")"

  if [[ -n "$explicit_compose_raw" ]]; then
    COMPOSE_FILES_RAW="$explicit_compose_raw"
  else
    local env_compose_file
    env_compose_file="$(standalone_read_env_file_var "$env_file" "COMPOSE_FILE" "")"
    if [[ -n "$env_compose_file" ]]; then
      COMPOSE_FILES_RAW="$env_compose_file"
    elif [[ "$TLS_MODE" != "loopback" ]]; then
      local overlay_rel
      overlay_rel="$(standalone_resolve_tls_overlay_file "$DOMAIN" "$edge_tls_bind")"
      if [[ -f "${work_dir}/${overlay_rel}" ]]; then
        COMPOSE_FILES_RAW="${work_dir}/docker-compose.yml:${work_dir}/docker-compose.core.yml:${work_dir}/${overlay_rel}"
      elif [[ -f "${work_dir}/deploy/standalone/docker-compose.tls.yml" ]]; then
        COMPOSE_FILES_RAW="${work_dir}/docker-compose.yml:${work_dir}/docker-compose.core.yml:${work_dir}/deploy/standalone/docker-compose.tls.yml"
      else
        COMPOSE_FILES_RAW="${work_dir}/docker-compose.yml:${work_dir}/docker-compose.core.yml"
      fi
    else
      COMPOSE_FILES_RAW="${work_dir}/docker-compose.yml:${work_dir}/docker-compose.core.yml"
    fi
  fi

  local -a compose_files_arr=()
  IFS=':' read -r -a compose_files_arr <<< "$COMPOSE_FILES_RAW"
  COMPOSE_FILE_ARGS=()
  NORMALIZED_COMPOSE_FILES=()
  local cf
  for cf in "${compose_files_arr[@]}"; do
    [[ -z "$cf" ]] && continue
    if [[ "$cf" != /* ]]; then
      cf="${work_dir}/${cf}"
    fi
    if [[ ! -f "$cf" ]]; then
      if declare -F err >/dev/null 2>&1; then
        err "Compose file not found: ${cf}"
      else
        echo "ERROR: Compose file not found: ${cf}" >&2
      fi
      return 1
    fi
    COMPOSE_FILE_ARGS+=(-f "$cf")
    NORMALIZED_COMPOSE_FILES+=("$cf")
  done
  COMPOSE_FILES_JOINED="$(IFS=':'; echo "${NORMALIZED_COMPOSE_FILES[*]}")"
  COMPOSE_PRINT_ARGS=(-p "$PROJECT_NAME" "${COMPOSE_FILE_ARGS[@]}" --env-file "$env_file")
  return 0
}
