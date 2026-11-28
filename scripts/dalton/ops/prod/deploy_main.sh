#!/bin/bash
# Deploy origin/dalton/main: acquire exclusive flock, wait idle, fetch once and pin
# TARGET=origin/dalton/main, backup (waits for a fresh dump unless reused), ff to TARGET,
# verify protocol handshake and re-exec updated deploy script if changed in TARGET,
# build frontend into web/releases/<build-id>/ (carrying over previous release's hashed
# assets), build channel-gateway if changed, restart backend/crawler (and edge if config
# changed), switch gateway, atomically switch web/releases/current symlink, verify HTTP
# /version.json build id via check_public_edge.sh, and prune old frontend releases (keep 3).
# Usage: deploy_main.sh [--bridge] [--gateway] [--recreate] [--expect-migration dNNN] [--reuse-backup-minutes N]
# --reuse-backup-minutes N: skip the fresh dump only when the last backup-log line is an ' ok '
#             dump at most N minutes old AND pre-deploy DB revisions for both migration chains
#             already match the pinned TARGET's migration heads; otherwise back up as usual
#             (including when DB revisions cannot be read).
# --recreate: recreate the backend container (compose changes such as mem_limit);
#             plain `docker restart` does not re-read docker-compose files.
# --gateway:  build integrations/dalton-channel-gateway/ inside .deploy-releases/<id>/
#             before backend restart, then atomically switch .deploy-current (with dist
#             and node_modules symlinked to .deploy-current/*, migrating legacy real dirs
#             on first run; replaces old .deploy-staging/ and .deploy-prev/), verify
#             import, kickstart com.dalton.channel-gateway, check /health, and record
#             the healthy tree hash in .deploy-deployed-tree.
set -uo pipefail
DEPLOY_RESUME_PROTOCOL=2
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_FILE="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"
WAIT_IDLE_SCRIPT="${DALTON_WAIT_IDLE_SCRIPT:-${SCRIPT_DIR}/wait_backend_idle.sh}"
DEPLOY_LOCK_FILE="${DALTON_DEPLOY_LOCK_FILE:-${HOME:-/tmp}/Library/Logs/Dalton/deploy_main.lock}"
FRONTEND_KEEP_RELEASES=3
EDGE_PORT="${DALTON_EDGE_PORT:-8088}"
EDGE_WAIT_TIMEOUT=60
EDGE_COMPOSE_TIMEOUT="${DALTON_EDGE_COMPOSE_TIMEOUT:-120}"
EDGE_HEALTH_EXPECTED_BODY="dalton-edge-ok releases-v1"
GATEWAY_PORT="${DALTON_GATEWAY_PORT:-8962}"
GATEWAY_WAIT_TIMEOUT="${DALTON_GATEWAY_WAIT_TIMEOUT:-60}"
GATEWAY_POLL_INTERVAL="${DALTON_GATEWAY_POLL_INTERVAL:-1}"
GATEWAY_ENV_FILE="${DALTON_CHANNEL_GATEWAY_ENV_FILE:-${HOME:-/tmp}/.config/dalton/channel-gateway.env}"
CONTAINER_LOG_DIR="${DALTON_CONTAINER_LOG_DIR:-${HOME:-/tmp}/Library/Logs/Dalton/containers}"
CONTAINER_LOG_RETENTION_DAYS=14
BRIDGE=0; GATEWAY=0; RECREATE=0; EXPECT=""; REUSE_BACKUP_MIN=""

usage() {
  cat <<'EOF'
Usage: deploy_main.sh [--bridge] [--gateway] [--recreate] [--expect-migration dNNN] [--reuse-backup-minutes N]
  Waits for backend idle, reads pre-deploy DB revisions, fetches origin once and pins
  TARGET=origin/dalton/main, verifies HEAD is an ancestor of TARGET and migration directories
  have no uncommitted or ignored files (except __pycache__/ and *.pyc), re-checks migration
  directories and heads right before backend restart, compares pre-deploy DB revisions against
  TARGET heads (dalton + upstream) for backup reuse, against post-ff HEAD for migrating status,
  and against PRE_FF before auto-revert (unreadable DB revisions are treated as migrating).
  Builds integrations/dalton-channel-gateway/ in .deploy-releases/<id>/ before backend restart,
  then atomically switches .deploy-current (with dist and node_modules symlinked to
  .deploy-current/*), verifies import, restarts com.dalton.channel-gateway, checks /health
  after backend health passes, and records the deployed tree hash in .deploy-deployed-tree
  whenever integrations/dalton-channel-gateway/ differs from .deploy-deployed-tree or
  PRE_FF..TARGET changes integrations/dalton-channel-gateway/ or --gateway is given.
  --reuse-backup-minutes N  Reuse last ' ok ' dump <= N min old only when DB revisions match TARGET heads.
  --expect-migration dNNN   Verify post-deploy dalton migration starts with dNNN.
  --recreate                Recreate backend/crawler containers instead of docker restart.
  --bridge                  Also kickstart com.dalton.researchdata-bridge.
  --gateway                 Force build and restart of com.dalton.channel-gateway (auto when integrations/dalton-channel-gateway/ changes).
  --first-releases-rollout [--pre-ff <sha>]
                            One-time first rollout from legacy web/dist to web/releases/current after fast-forward.
EOF
}

parse_deploy_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --bridge) BRIDGE=1;;
      --gateway) GATEWAY=1;;
      --recreate) RECREATE=1;;
      --expect-migration)
        if [ $# -lt 2 ] || [ -z "${2:-}" ] || [[ "${2:-}" == -* ]]; then
          echo "ERROR: --expect-migration requires a revision argument (e.g. dNNN)" >&2
          usage >&2
          return 2
        fi
        if ! _is_valid_dalton_rev "$2"; then
          echo "ERROR: invalid --expect-migration revision '$2' (expected dNNN, e.g. d040)" >&2
          usage >&2
          return 2
        fi
        EXPECT="$2"
        shift
        ;;
      --reuse-backup-minutes)
        if [ $# -lt 2 ] || ! [[ "${2:-}" =~ ^[1-9][0-9]*$ ]]; then
          echo "ERROR: --reuse-backup-minutes requires a positive whole number of minutes" >&2
          usage >&2
          return 2
        fi
        REUSE_BACKUP_MIN="$2"
        shift
        ;;
      *)
        echo "ERROR: unknown argument: $1" >&2
        usage >&2
        return 2
        ;;
    esac
    shift
  done
}

_is_valid_dalton_rev() {
  [[ "${1:-}" =~ ^d[0-9]{3,}$ ]]
}

_is_valid_upstream_rev() {
  [[ "${1:-}" =~ ^([0-9]{1,6}|[0-9a-fA-F]{8,40})$ ]]
}

parse_strict_migration_version() {
  local raw="${1-}" chain="${2:-}"
  local candidate=""
  [ -n "$raw" ] || return 1
  # Strip at most one trailing newline from psql -Atc output; any remaining newline
  # indicates multiple rows or trailing blank lines.
  candidate="${raw%$'\n'}"
  case "$candidate" in
    ""|*$'\n'*|*$'\r'*) return 1 ;;
  esac
  # Reject any whitespace anywhere (leading/trailing spaces, tabs, trailing text).
  case "$candidate" in
    *[[:space:]]*) return 1 ;;
  esac
  case "$chain" in
    dalton)
      _is_valid_dalton_rev "$candidate" || return 1
      ;;
    upstream)
      _is_valid_upstream_rev "$candidate" || return 1
      ;;
    *)
      return 1
      ;;
  esac
  printf '%s' "$candidate"
}

_query_alembic_version_table() {
  local table="$1" chain="$2"
  local raw="" psql_rc=0 rev="" attempt=0 max_attempts=3
  local retry_delay="${DALTON_ALEMBIC_QUERY_RETRY_DELAY_S:-1}"
  local query_timeout="${DALTON_ALEMBIC_QUERY_TIMEOUT_S:-10}"
  local statement_timeout_ms=$((query_timeout * 1000))
  while [ "$attempt" -lt "$max_attempts" ]; do
    attempt=$((attempt + 1))
    psql_rc=0
    raw=$(
      perl -e 'my $t = shift; alarm $t; exec @ARGV' "$query_timeout" \
        docker exec -e "PGOPTIONS=-c statement_timeout=${statement_timeout_ms}" dalton-langalpha-postgres-1 \
        psql -U postgres -d postgres -Atc "SELECT version_num FROM ${table}" 2>/dev/null
      printf ':%d' "$?"
    )
    psql_rc="${raw##*:}"
    raw="${raw%:*}"
    if [ "$psql_rc" = "0" ]; then
      break
    fi
    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep "$retry_delay"
    fi
  done
  [ "$psql_rc" = "0" ] || return 1
  rev=$(parse_strict_migration_version "$raw" "$chain") || return 1
  printf '%s' "$rev"
}

get_dalton_alembic_version() {
  _query_alembic_version_table "alembic_version_dalton" "dalton"
}

get_upstream_alembic_version() {
  _query_alembic_version_table "alembic_version" "upstream"
}

health() { curl -sf -m3 --noproxy '*' http://127.0.0.1:8000/health >/dev/null; }
_expected_edge_health_mode() {
  local caddyfile="${1:-${REPO_ROOT}/deploy/dalton/Caddyfile}"
  if [ -f "$caddyfile" ]; then
    if grep -q -- "$EDGE_HEALTH_EXPECTED_BODY" "$caddyfile" 2>/dev/null; then
      printf 'body:%s' "$EDGE_HEALTH_EXPECTED_BODY"
    elif grep -q '/__edge_health' "$caddyfile" 2>/dev/null; then
      printf 'body:ok'
    else
      printf 'root:200'
    fi
  else
    printf 'body:%s' "$EDGE_HEALTH_EXPECTED_BODY"
  fi
}

_record_deferred_signal() {
  DEFERRED_SIGNAL="${1:-TERM}"
}

_wait_pid_ignoring_signals() {
  local pid="${1:-}" wrc=0
  [ -n "$pid" ] || return 0
  while :; do
    wrc=0
    wait "$pid" || wrc=$?
    if kill -0 "$pid" 2>/dev/null; then
      continue
    fi
    return "$wrc"
  done
}

edge_ready() {
  local max_time="${1:-2}" caddyfile="${2:-${REPO_ROOT}/deploy/dalton/Caddyfile}"
  local mode="" expected_body="" url="" probe_out="" raw="" code="" body="" rc=0
  mode=$(_expected_edge_health_mode "$caddyfile")
  case "$mode" in
    body:*)
      expected_body="${mode#body:}"
      url="http://127.0.0.1:${EDGE_PORT}/__edge_health"
      ;;
    root:200)
      url="http://127.0.0.1:${EDGE_PORT}/"
      ;;
    *)
      return 1
      ;;
  esac
  probe_out=$(mktemp "${TMPDIR:-/tmp}/dalton-edge-ready.XXXXXX") || return 1
  ACTIVE_STEP_OUT_FILE="$probe_out"
  if _spawn_in_new_pgid "$probe_out" curl -s --max-time "$max_time" --noproxy '*' -w $'\n%{http_code}' "$url"; then
    if [ "${ROLLBACK_IN_PROGRESS:-0}" = "1" ]; then
      _wait_pid_ignoring_signals "$ACTIVE_STEP_CHILD_PID" || rc=$?
    else
      wait "$ACTIVE_STEP_CHILD_PID" || rc=$?
    fi
    ACTIVE_STEP_CHILD_PID=""
    ACTIVE_STEP_PGID=""
  else
    rc=1
  fi
  raw=$(cat "$probe_out" 2>/dev/null || true)
  rm -f "$probe_out"
  ACTIVE_STEP_OUT_FILE=""
  [ "$rc" -eq 0 ] || return 1
  code="${raw##*$'\n'}"
  body="${raw%$'\n'*}"
  body="${body%$'\r'}"
  if [ "$mode" = "root:200" ]; then
    [ "$code" = "200" ]
  else
    [ "$code" = "200" ] && [ "$body" = "$expected_body" ]
  fi
}

extract_edge_config() {
  awk '
    function lindent(s) {
      match(s, /^[ ]*/)
      return RLENGTH
    }
    function trim(s) {
      sub(/^[[:space:]]+/, "", s)
      sub(/[[:space:]]+$/, "", s)
      return s
    }
    function strip_comment(s) {
      sub(/[[:space:]]+#.*$/, "", s)
      sub(/^#.*$/, "", s)
      return s
    }
    function unquote(s, q) {
      s = trim(strip_comment(s))
      q = sprintf("%c", 39)
      if (length(s) >= 2) {
        if ((substr(s, 1, 1) == "\"" && substr(s, length(s), 1) == "\"") ||
            (substr(s, 1, 1) == q && substr(s, length(s), 1) == q)) {
          s = substr(s, 2, length(s) - 2)
        }
      }
      return s
    }
    function flush_vol() {
      if (vol_source != "") {
        if (vol_type == "volume" || (vol_type == "" && vol_source !~ /^[.\/~]/)) {
          ref_vol[vol_source] = 1
        }
      }
      vol_type = ""
      vol_source = ""
      vol_item_indent = -1
    }
    function extract_section(sec,    k, line, ind, in_sec, entry_indent, capture, printed_hdr, key, entry_name) {
      in_sec = 0; entry_indent = -1; capture = 0; printed_hdr = 0
      for (k = 1; k <= total_lines; k++) {
        line = lines[k]
        if (!in_sec) {
          if (lindent(line) == 0 && line ~ ("^" sec ":[[:space:]]*(#.*)?$")) {
            in_sec = 1
          }
          continue
        }
        if (line ~ /^[[:space:]]*$/ || line ~ /^[[:space:]]*#/) {
          if (capture) print line
          continue
        }
        ind = lindent(line)
        if (ind == 0) {
          in_sec = 0
          capture = 0
          continue
        }
        if (entry_indent == -1) entry_indent = ind
        if (ind <= entry_indent) {
          key = line
          sub(/:.*/, "", key)
          entry_name = unquote(key)
          if ((sec == "networks" && (entry_name in ref_net)) ||
              (sec == "configs" && (entry_name in ref_cfg)) ||
              (sec == "secrets" && (entry_name in ref_sec)) ||
              (sec == "volumes" && (entry_name in ref_vol))) {
            capture = 1
            if (!printed_hdr) {
              print sec ":"
              printed_hdr = 1
            }
            print line
          } else {
            capture = 0
          }
        } else if (capture) {
          print line
        }
      }
    }
    { lines[++total_lines] = $0 }
    END {
      in_services = 0; svc_indent = -1; in_edge = 0; edge_indent = -1; svc_prop_indent = -1
      in_sub = ""; sub_indent = -1
      vol_type = ""; vol_source = ""; vol_item_indent = -1
      cfg_item_indent = -1
      edge_count = 0

      for (i = 1; i <= total_lines; i++) {
        line = lines[i]
        if (!in_services) {
          if (lindent(line) == 0 && line ~ /^services:[[:space:]]*(#.*)?$/) {
            in_services = 1
            svc_indent = -1
          }
          continue
        }
        if (line ~ /^[[:space:]]*$/ || line ~ /^[[:space:]]*#/) {
          if (in_edge) edge_lines[++edge_count] = line
          continue
        }
        ind = lindent(line)
        if (ind == 0) {
          flush_vol()
          in_services = 0; svc_indent = -1; in_edge = 0; in_sub = ""
          continue
        }
        if (svc_indent == -1) svc_indent = ind
        if (!in_edge) {
          if (ind == svc_indent && line ~ /^[[:space:]]+edge:[[:space:]]*(#.*)?$/) {
            edge_indent = ind
            in_edge = 1
            edge_lines[++edge_count] = line
          }
          continue
        }
        if (ind <= edge_indent) {
          flush_vol()
          in_edge = 0; in_sub = ""
          continue
        }
        edge_lines[++edge_count] = line
        if (svc_prop_indent == -1) svc_prop_indent = ind
        if (ind == svc_prop_indent) {
          flush_vol()
          in_sub = ""; sub_indent = -1; cfg_item_indent = -1
          if (line ~ /^[[:space:]]*networks:[[:space:]]*(#.*)?$/) in_sub = "networks"
          else if (line ~ /^[[:space:]]*configs:[[:space:]]*(#.*)?$/) in_sub = "configs"
          else if (line ~ /^[[:space:]]*secrets:[[:space:]]*(#.*)?$/) in_sub = "secrets"
          else if (line ~ /^[[:space:]]*volumes:[[:space:]]*(#.*)?$/) in_sub = "volumes"
          continue
        }
        if (in_sub == "") continue
        if (sub_indent == -1) sub_indent = ind

        if (in_sub == "networks" && ind == sub_indent) {
          if (line ~ /^[[:space:]]*-[[:space:]]+/) {
            rest = line
            sub(/^[[:space:]]*-[[:space:]]+/, "", rest)
            name = unquote(rest)
            if (name != "") ref_net[name] = 1
          } else if (index(line, ":") > 0) {
            key = line
            sub(/:.*/, "", key)
            name = unquote(key)
            if (name != "") ref_net[name] = 1
          }
        } else if (in_sub == "configs" || in_sub == "secrets") {
          if (ind == sub_indent && line ~ /^[[:space:]]*-[[:space:]]+/) {
            cfg_item_indent = -1
            rest = line
            sub(/^[[:space:]]*-[[:space:]]+/, "", rest)
            if (rest ~ /^source:[[:space:]]*/) {
              sub(/^source:[[:space:]]*/, "", rest)
              name = unquote(rest)
              if (name != "") { if (in_sub == "secrets") ref_sec[name] = 1; else ref_cfg[name] = 1 }
            } else if (rest !~ /^[a-zA-Z0-9_.-]+:[[:space:]]+/ && index(rest, ":") == 0) {
              name = unquote(rest)
              if (name != "") { if (in_sub == "secrets") ref_sec[name] = 1; else ref_cfg[name] = 1 }
            }
          } else if (ind > sub_indent) {
            if (cfg_item_indent == -1) cfg_item_indent = ind
            if (ind == cfg_item_indent && line ~ /^[[:space:]]*source:[[:space:]]*/) {
              rest = line
              sub(/^[[:space:]]*source:[[:space:]]*/, "", rest)
              name = unquote(rest)
              if (name != "") { if (in_sub == "secrets") ref_sec[name] = 1; else ref_cfg[name] = 1 }
            }
          }
        } else if (in_sub == "volumes") {
          if (ind == sub_indent && line ~ /^[[:space:]]*-([[:space:]]|$)/) {
            flush_vol()
            rest = line
            sub(/^[[:space:]]*-[[:space:]]*/, "", rest)
            if (rest ~ /^type:[[:space:]]*/) {
              sub(/^type:[[:space:]]*/, "", rest)
              vol_type = unquote(rest)
            } else if (rest ~ /^source:[[:space:]]*/) {
              sub(/^source:[[:space:]]*/, "", rest)
              vol_source = unquote(rest)
            } else if (rest != "" && rest !~ /^[a-zA-Z0-9_.-]+:[[:space:]]+/) {
              split(rest, parts, ":")
              vol_source = unquote(parts[1])
            }
          } else if (ind > sub_indent) {
            if (vol_item_indent == -1) vol_item_indent = ind
            if (ind == vol_item_indent) {
              if (line ~ /^[[:space:]]*type:[[:space:]]*/) {
                rest = line
                sub(/^[[:space:]]*type:[[:space:]]*/, "", rest)
                vol_type = unquote(rest)
              } else if (line ~ /^[[:space:]]*source:[[:space:]]*/) {
                rest = line
                sub(/^[[:space:]]*source:[[:space:]]*/, "", rest)
                vol_source = unquote(rest)
              }
            }
          }
        }
      }
      flush_vol()
      if (edge_count == 0) exit 0
      for (i = 1; i <= edge_count; i++) print edge_lines[i]
      extract_section("networks")
      extract_section("configs")
      extract_section("secrets")
      extract_section("volumes")
    }
  '
}

compose_files_list() {
  local cf="${COMPOSE_FILE:-}" sep="${COMPOSE_PATH_SEPARATOR:-:}" f ovr
  if [ -z "$cf" ] && [ -f "${REPO_ROOT}/.env" ]; then
    cf=$(awk '
      /^[[:space:]]*(export[[:space:]]+)?COMPOSE_FILE=/ {
        line = $0
        sub(/^[[:space:]]*(export[[:space:]]+)?COMPOSE_FILE=[[:space:]]*/, "", line)
        if (line ~ /^"/) {
          sub(/^"/, "", line); sub(/".*$/, "", line)
        } else if (line ~ /^'\''/) {
          sub(/^'\''/, "", line); sub(/'\''.*$/, "", line)
        } else {
          sub(/[[:space:]]*#.*$/, "", line)
          sub(/[[:space:]]+$/, "", line)
        }
        val = line
      }
      END { print val }
    ' "${REPO_ROOT}/.env")
  fi
  if [ -n "$cf" ]; then
    while [ -n "$cf" ]; do
      case "$cf" in
        *"$sep"*)
          f=${cf%%"$sep"*}
          cf=${cf#*"$sep"}
          ;;
        *)
          f=$cf
          cf=""
          ;;
      esac
      [ -n "$f" ] && printf '%s\n' "$f"
    done
    return 0
  fi
  for f in compose.yaml compose.yml docker-compose.yaml docker-compose.yml; do
    if [ -f "${REPO_ROOT}/${f}" ] || git cat-file -e "${PRE_FF}:${f}" 2>/dev/null; then
      printf '%s\n' "$f"
      for ovr in compose.override.yaml compose.override.yml docker-compose.override.yaml docker-compose.override.yml; do
        if [ -f "${REPO_ROOT}/${ovr}" ] || git cat-file -e "${PRE_FF}:${ovr}" 2>/dev/null; then
          printf '%s\n' "$ovr"
          break
        fi
      done
      return 0
    fi
  done
}

edge_compose_changed() {
  [ "$PRE_FF" = "$(git rev-parse HEAD 2>/dev/null)" ] && return 1
  local tmp_dir f old_cfg new_cfg old_edge new_edge old_rc new_rc
  local -a old_f_args=() env_args=()
  tmp_dir=$(mktemp -d) || return 0
  if [ -f "${REPO_ROOT}/.env" ]; then
    ln -s "${REPO_ROOT}/.env" "${tmp_dir}/.env"
    env_args=(--env-file "${REPO_ROOT}/.env")
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    if git cat-file -e "${PRE_FF}:${f}" 2>/dev/null; then
      mkdir -p "$(dirname "${tmp_dir}/${f}")"
      git show "${PRE_FF}:${f}" > "${tmp_dir}/${f}" 2>/dev/null || { rm -rf "$tmp_dir"; return 0; }
      old_f_args+=(-f "${tmp_dir}/${f}")
    fi
  done <<EOF
$(compose_files_list)
EOF
  new_cfg=$(docker compose config 2>/dev/null)
  new_rc=$?
  if [ "$new_rc" -ne 0 ]; then
    rm -rf "$tmp_dir"
    return 0
  fi
  if [ "${#old_f_args[@]}" -eq 0 ]; then
    old_cfg=""
    old_rc=0
  else
    old_cfg=$(docker compose --project-directory "$REPO_ROOT" "${env_args[@]}" "${old_f_args[@]}" config 2>/dev/null)
    old_rc=$?
  fi
  rm -rf "$tmp_dir"
  if [ "$old_rc" -ne 0 ]; then
    return 0
  fi
  old_cfg=${old_cfg//"${tmp_dir}/"/"${REPO_ROOT}/"}
  old_edge=$(printf '%s\n' "$old_cfg" | extract_edge_config)
  new_edge=$(printf '%s\n' "$new_cfg" | extract_edge_config)
  [ "$old_edge" != "$new_edge" ]
}

_edge_compose_project_name() {
  local proj=""
  proj=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' dalton-langalpha-edge-1 2>/dev/null || true)
  proj="${proj%$'\r'}"
  if [ -n "$proj" ] && [ "$proj" != "<no value>" ] && [[ "$proj" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    printf '%s' "$proj"
    return 0
  fi
  if [ -n "${COMPOSE_PROJECT_NAME:-}" ] && [[ "${COMPOSE_PROJECT_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    printf '%s' "${COMPOSE_PROJECT_NAME}"
    return 0
  fi
  if [ -f "${REPO_ROOT}/.env" ]; then
    proj=$(awk '
      /^[[:space:]]*(export[[:space:]]+)?COMPOSE_PROJECT_NAME=/ {
        line = $0
        sub(/^[[:space:]]*(export[[:space:]]+)?COMPOSE_PROJECT_NAME=[[:space:]]*/, "", line)
        if (line ~ /^"/) { sub(/^"/, "", line); sub(/".*$/, "", line) }
        else if (line ~ /^'\''/) { sub(/^'\''/, "", line); sub(/'\''.*$/, "", line) }
        else { sub(/[[:space:]]*#.*$/, "", line); sub(/[[:space:]]+$/, "", line) }
        val = line
      }
      END { print val }
    ' "${REPO_ROOT}/.env")
    if [ -n "$proj" ] && [[ "$proj" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
      printf '%s' "$proj"
      return 0
    fi
  fi
  return 1
}

_edge_compose_files_list() {
  local commit_ref="${1:-}" f="" has_dalton=0 base=""
  local saved_pre_ff="${PRE_FF:-}"
  [ -n "$commit_ref" ] && PRE_FF="$commit_ref"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf '%s\n' "$f"
    base=$(basename "$f")
    [ "$base" = "docker-compose.dalton.yml" ] && has_dalton=1
  done <<EOF
$(compose_files_list)
EOF
  PRE_FF="$saved_pre_ff"
  if [ "$has_dalton" = "0" ]; then
    if [ -n "$commit_ref" ]; then
      if git cat-file -e "${commit_ref}:docker-compose.dalton.yml" 2>/dev/null; then
        printf '%s\n' "docker-compose.dalton.yml"
      fi
    elif [ -f "${REPO_ROOT}/docker-compose.dalton.yml" ]; then
      printf '%s\n' "docker-compose.dalton.yml"
    fi
  fi
}

_resolve_compose_file_path() {
  local f="${1:-}"
  case "$f" in
    /*) printf '%s' "$f" ;;
    *) printf '%s/%s' "$REPO_ROOT" "$f" ;;
  esac
}

_has_explicit_compose_file_setting() {
  [ -n "${COMPOSE_FILE:-}" ] && return 0
  [ -f "${REPO_ROOT}/.env" ] && grep -qE '^[[:space:]]*(export[[:space:]]+)?COMPOSE_FILE=' "${REPO_ROOT}/.env"
}

EDGE_COMPOSE_ARGS=()

_populate_edge_compose_args() {
  local proj="" f="" resolved="" explicit_cf=0
  local -a c_args=() f_args=()
  EDGE_COMPOSE_ARGS=()
  _has_explicit_compose_file_setting && explicit_cf=1
  proj=$(_edge_compose_project_name || true)
  if [ -n "$proj" ]; then
    c_args+=(-p "$proj")
  fi
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    resolved=$(_resolve_compose_file_path "$f")
    if [ -f "$resolved" ]; then
      f_args+=(-f "$resolved")
    elif [ "$explicit_cf" = "1" ]; then
      echo "ERROR: explicit COMPOSE_FILE entry not found: ${resolved}" >&2
      return 1
    fi
  done <<EOF
$(_edge_compose_files_list)
EOF
  if [ "${#f_args[@]}" -gt 0 ]; then
    c_args+=(--project-directory "$REPO_ROOT")
    if [ -f "${REPO_ROOT}/.env" ]; then
      c_args+=(--env-file "${REPO_ROOT}/.env")
    fi
    c_args+=("${f_args[@]}")
  fi
  if [ "${#c_args[@]}" -gt 0 ]; then
    EDGE_COMPOSE_ARGS=("${c_args[@]}")
  fi
  return 0
}

_run_edge_compose() {
  _populate_edge_compose_args || return 1
  if [ "${#EDGE_COMPOSE_ARGS[@]}" -gt 0 ]; then
    docker compose "${EDGE_COMPOSE_ARGS[@]}" "$@"
  else
    docker compose "$@"
  fi
}

_edge_live_running() {
  local state=""
  state=$(docker inspect -f '{{.State.Running}}' dalton-langalpha-edge-1 2>/dev/null || true)
  state="${state%$'\r'}"
  [ "$state" = "true" ]
}

_edge_live_mounts() {
  docker inspect -f '{{range .Mounts}}{{.Type}}|{{.Source}}->{{.Destination}}{{"\n"}}{{end}}' dalton-langalpha-edge-1 2>/dev/null || true
}

# Parse edge service volume mounts from resolved `docker compose config` YAML (or raw compose YAML),
# supporting both short syntax (`- src:dst[:ro]`) and long syntax (`- type: bind / source: ... / target: ...`).
# Prints `type|resolved_source|target` lines.
_parse_edge_mounts_from_compose_yaml() {
  local yaml_text="${1:-}" py=""
  [ -n "$yaml_text" ] || return 1
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$REPO_ROOT" "$yaml_text" <<'PY'
import os
import re
import sys

repo_root, text = sys.argv[1:3]

def unquote(val: str) -> str:
    val = re.sub(r"\s+#.*$", "", val).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return val.strip()

def resolve_src(mtype: str, src: str) -> str:
    if not src:
        return ""
    if mtype != "bind":
        return src
    if os.path.isabs(src):
        return os.path.realpath(src)
    return os.path.realpath(os.path.join(repo_root, src))

lines = text.splitlines()
mounts = []
in_volumes = False
vol_indent = -1
item_indent = -1
cur_type = ""
cur_src = ""
cur_dst = ""

def flush():
    global cur_type, cur_src, cur_dst
    if cur_dst:
        mtype = cur_type or ("bind" if (cur_src.startswith((".", "/", "~")) or "/" in cur_src) else "volume")
        mounts.append((mtype.lower(), resolve_src(mtype.lower(), cur_src), cur_dst))
    cur_type = ""
    cur_src = ""
    cur_dst = ""

for raw in lines:
    if not raw.strip() or raw.lstrip().startswith("#"):
        continue
    ind = len(raw) - len(raw.lstrip(" "))
    stripped = raw.strip()
    if not in_volumes:
        if re.match(r"^volumes:\s*(#.*)?$", stripped):
            in_volumes = True
            vol_indent = ind
            item_indent = -1
        continue
    if ind <= vol_indent:
        flush()
        in_volumes = False
        continue
    if stripped.startswith("- ") or stripped == "-":
        flush()
        item_indent = ind
        rest = stripped[1:].strip()
        if not rest:
            continue
        if rest.startswith("type:"):
            cur_type = unquote(rest.split(":", 1)[1])
        elif rest.startswith("source:"):
            cur_src = unquote(rest.split(":", 1)[1])
        elif rest.startswith("target:"):
            cur_dst = unquote(rest.split(":", 1)[1])
        elif not re.match(r"^[A-Za-z0-9_.-]+:\s+", rest):
            short = unquote(rest)
            parts = short.split(":")
            if len(parts) >= 2:
                src, dst = parts[0].strip(), parts[1].strip()
                mtype = "bind" if (src.startswith((".", "/", "~")) or "/" in src) else "volume"
                mounts.append((mtype, resolve_src(mtype, src), dst))
    elif item_indent >= 0 and ind > item_indent:
        if stripped.startswith("type:"):
            cur_type = unquote(stripped.split(":", 1)[1])
        elif stripped.startswith("source:"):
            cur_src = unquote(stripped.split(":", 1)[1])
        elif stripped.startswith("target:"):
            cur_dst = unquote(stripped.split(":", 1)[1])

flush()
for mtype, src, dst in mounts:
    print(f"{mtype}|{src}|{dst}")
PY
}

# Print all parsed edge mounts (`type|resolved_source|destination` lines) from resolved
# `docker compose config` (falling back to docker-compose.dalton.yml only if config has no edge block).
_edge_resolved_target_mounts() {
  local cfg="" edge_cfg=""
  cfg=$(_run_edge_compose config 2>/dev/null || true)
  if [ -n "$cfg" ]; then
    edge_cfg=$(printf '%s\n' "$cfg" | extract_edge_config)
  fi
  if [ -z "$edge_cfg" ] && [ -f "${REPO_ROOT}/docker-compose.dalton.yml" ]; then
    edge_cfg=$(extract_edge_config < "${REPO_ROOT}/docker-compose.dalton.yml")
  fi
  [ -n "$edge_cfg" ] || return 1
  _parse_edge_mounts_from_compose_yaml "$edge_cfg" 2>/dev/null
}

# Print `type|resolved_source|destination` for edge's web root mount (/srv-releases or /srv)
# as resolved by `docker compose config`.
_edge_target_web_mount() {
  local parsed="" line="" found_srv=""
  parsed=$(_edge_resolved_target_mounts 2>/dev/null || true)
  [ -n "$parsed" ] || return 1
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    case "$line" in
      *"|/srv-releases")
        printf '%s' "$line"
        return 0
        ;;
      *"|/srv")
        found_srv="$line"
        ;;
    esac
  done <<<"$parsed"
  if [ -n "$found_srv" ]; then
    printf '%s' "$found_srv"
    return 0
  fi
  return 1
}

_edge_target_uses_releases() {
  local spec=""
  spec=$(_edge_target_web_mount 2>/dev/null || true)
  case "$spec" in
    *"|/srv-releases") return 0 ;;
  esac
  return 1
}

_edge_mounts_check() {
  local mode="$1" expected_src="$2" expected_dst="$3" expected_type="${4:-bind}" mounts="" py=""
  mounts=$(_edge_live_mounts)
  printf '%s\n' "$mounts" | grep -q -- '->' || return 2
  py=$(_resolve_deploy_python) || return 2
  "$py" - "$mode" "$expected_src" "$expected_dst" "$expected_type" "$REPO_ROOT" "$mounts" <<'PY'
import os
import sys

mode, expected_src, expected_dst, expected_type, repo_root, mounts_raw = sys.argv[1:7]
expected_type = (expected_type or "bind").strip().lower()
mounts = []
for raw in mounts_raw.splitlines():
    line = raw.strip()
    if "->" not in line:
        continue
    if "|" in line and line.index("|") < line.index("->"):
        mtype, rest = line.split("|", 1)
    else:
        mtype, rest = "bind", line
    src, dst = rest.rsplit("->", 1)
    mounts.append((mtype.strip().lower(), src.strip(), dst.strip()))

if not mounts:
    sys.exit(2)

expected_real = os.path.realpath(expected_src) if (expected_src and expected_type == "bind") else expected_src
std_caddy_real = os.path.realpath(os.path.join(repo_root, "deploy/dalton/Caddyfile"))
has_releases = any(dst == "/srv-releases" for _, _, dst in mounts)
has_legacy_srv = any(dst == "/srv" for _, _, dst in mounts)

def _src_eq(mtype: str, src: str, target_real: str) -> bool:
    if mtype == "bind":
        return os.path.realpath(src) == target_real
    return src == target_real

if mode == "exact_legacy_dist":
    ok = (not has_releases) and any(
        mtype == "bind" and dst == expected_dst and _src_eq(mtype, src, expected_real)
        for mtype, src, dst in mounts
    )
    sys.exit(0 if ok else 1)
elif mode == "any_legacy_srv":
    ok = has_legacy_srv and not has_releases
    sys.exit(0 if ok else 1)
elif mode == "matches_target":
    web_ok = any(
        mtype == expected_type and dst == expected_dst and _src_eq(mtype, src, expected_real)
        for mtype, src, dst in mounts
    )
    caddy_live = [(mtype, src, dst) for mtype, src, dst in mounts if dst == "/etc/caddy/Caddyfile"]
    caddy_ok = True
    if caddy_live:
        caddy_ok = any(
            mtype == "bind" and _src_eq(mtype, src, std_caddy_real)
            for mtype, src, _ in caddy_live
        )
    sys.exit(0 if (web_ok and caddy_ok) else 1)
sys.exit(2)
PY
}

_edge_live_mounts_legacy_dist() {
  _edge_live_running || return 1
  _edge_mounts_check "exact_legacy_dist" "${REPO_ROOT}/web/dist" "/srv" "bind"
}

_edge_live_mounts_any_legacy_srv() {
  _edge_mounts_check "any_legacy_srv" "" "/srv" "bind"
}

# Enforce the two supported production edge mount layouts after `docker compose config`:
#   1) Legacy:   bind ${REPO_ROOT}/web/dist      -> /srv
#   2) Releases: bind ${REPO_ROOT}/web/releases  -> /srv-releases
# And when /etc/caddy/Caddyfile is mounted, it must be bind ${REPO_ROOT}/deploy/dalton/Caddyfile.
# Rejects any non-standard mount (override to another dir, tmpfs, volume, etc.) before recreate.
_validate_edge_standard_mounts() {
  local require_dest="${1:-}" cfg="" edge_cfg="" parsed="" py=""
  cfg=$(_run_edge_compose config 2>/dev/null || true)
  if [ -n "$cfg" ]; then
    edge_cfg=$(printf '%s\n' "$cfg" | extract_edge_config)
  fi
  if [ -z "$edge_cfg" ] && [ -f "${REPO_ROOT}/docker-compose.dalton.yml" ]; then
    edge_cfg=$(extract_edge_config < "${REPO_ROOT}/docker-compose.dalton.yml")
  fi
  # Minimal unit-test repos with no edge service definition are skipped unless require_dest is explicit.
  if [ -z "$edge_cfg" ]; then
    [ -z "$require_dest" ] && return 0
    echo "ERROR: edge service definition missing from compose configuration (required ${require_dest})" >&2
    return 1
  fi
  parsed=$(_parse_edge_mounts_from_compose_yaml "$edge_cfg" 2>/dev/null || true)
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$REPO_ROOT" "$require_dest" "$parsed" <<'PY'
import os
import sys

repo_root, require_dest, mounts_raw = sys.argv[1:4]
std_releases = os.path.realpath(os.path.join(repo_root, "web/releases"))
std_dist = os.path.realpath(os.path.join(repo_root, "web/dist"))
std_caddy = os.path.realpath(os.path.join(repo_root, "deploy/dalton/Caddyfile"))

mounts = []
for line in mounts_raw.splitlines():
    parts = line.strip().split("|")
    if len(parts) == 3:
        mounts.append((parts[0].lower(), parts[1], parts[2]))

web_mounts = [(t, s, d) for t, s, d in mounts if d in ("/srv", "/srv-releases")]
caddy_mounts = [(t, s, d) for t, s, d in mounts if d == "/etc/caddy/Caddyfile"]

supported_msg = (
    f"Supported standard edge mounts are bind {std_dist}->/srv (legacy) or "
    f"bind {std_releases}->/srv-releases (releases), with bind {std_caddy}->/etc/caddy/Caddyfile."
)

if len(web_mounts) != 1:
    print(f"ERROR: unsupported edge web-root mounts ({web_mounts}). {supported_msg}", file=sys.stderr)
    sys.exit(1)

wtype, wsrc, wdst = web_mounts[0]
expected_src = std_releases if wdst == "/srv-releases" else std_dist
if wtype != "bind" or not wsrc or os.path.realpath(wsrc) != expected_src:
    print(
        f"ERROR: unsupported edge web-root mount {wtype}|{wsrc}->{wdst} (must be bind {expected_src}->{wdst}). {supported_msg}",
        file=sys.stderr,
    )
    sys.exit(1)

if require_dest and wdst != require_dest:
    print(f"ERROR: expected edge web-root mount destination {require_dest}, got {wdst}. {supported_msg}", file=sys.stderr)
    sys.exit(1)

for ctype, csrc, cdst in caddy_mounts:
    if ctype != "bind" or not csrc or os.path.realpath(csrc) != std_caddy:
        print(
            f"ERROR: unsupported edge Caddyfile mount {ctype}|{csrc}->{cdst} (must be bind {std_caddy}->/etc/caddy/Caddyfile). {supported_msg}",
            file=sys.stderr,
        )
        sys.exit(1)

print(f"{wtype}|{expected_src}|{wdst}")
PY
}

_commit_mounts_legacy_dist() {
  local commit_sha="${1:-}" cfg="" edge_cfg="" parsed="" py=""
  [ -n "$commit_sha" ] || return 1
  git cat-file -e "${commit_sha}:docker-compose.dalton.yml" 2>/dev/null || return 1
  git cat-file -e "${commit_sha}:deploy/dalton/Caddyfile" 2>/dev/null || return 1
  cfg=$(git show "${commit_sha}:docker-compose.dalton.yml" 2>/dev/null || true)
  [ -n "$cfg" ] || return 1
  edge_cfg=$(printf '%s\n' "$cfg" | extract_edge_config)
  [ -n "$edge_cfg" ] || return 1
  parsed=$(_parse_edge_mounts_from_compose_yaml "$edge_cfg" 2>/dev/null || true)
  [ -n "$parsed" ] || return 1
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$REPO_ROOT" "$parsed" <<'PY'
import os
import sys

repo_root, mounts_raw = sys.argv[1:3]
std_dist = os.path.realpath(os.path.join(repo_root, "web/dist"))
mounts = [tuple(line.strip().split("|")) for line in mounts_raw.splitlines() if len(line.strip().split("|")) == 3]
has_legacy = any(t == "bind" and d == "/srv" and os.path.realpath(s) == std_dist for t, s, d in mounts)
has_releases = any(d == "/srv-releases" for _, _, d in mounts)
sys.exit(0 if (has_legacy and not has_releases) else 1)
PY
}

_commit_requires_releases_mount() {
  local commit_sha="${1:-}" has_compose=0 has_caddy=0 cfg="" edge_cfg="" parsed="" caddy_cfg=""
  [ -n "$commit_sha" ] || return 1
  git cat-file -e "${commit_sha}:docker-compose.dalton.yml" 2>/dev/null && has_compose=1
  git cat-file -e "${commit_sha}:deploy/dalton/Caddyfile" 2>/dev/null && has_caddy=1
  if [ "$has_compose" = "1" ]; then
    cfg=$(git show "${commit_sha}:docker-compose.dalton.yml" 2>/dev/null || true)
    edge_cfg=$(printf '%s\n' "$cfg" | extract_edge_config)
    if [ -n "$edge_cfg" ]; then
      parsed=$(_parse_edge_mounts_from_compose_yaml "$edge_cfg" 2>/dev/null || true)
      if printf '%s\n' "$parsed" | grep -q '|/srv-releases$'; then
        return 0
      fi
    fi
  fi
  if [ "$has_caddy" = "1" ]; then
    caddy_cfg=$(git show "${commit_sha}:deploy/dalton/Caddyfile" 2>/dev/null || true)
    if printf '%s\n' "$caddy_cfg" | grep -q '/srv-releases/current'; then
      return 0
    fi
  fi
  if [ "$has_compose" = "0" ] && [ "$has_caddy" = "0" ]; then
    _edge_target_uses_releases
    return $?
  fi
  return 1
}

_edge_live_mount_mismatches_target() {
  local spec="" target_type="" target_src="" target_dst="" rc=0 mounts="" py=""
  spec=$(_edge_target_web_mount 2>/dev/null || true)
  if [ -n "$spec" ]; then
    IFS='|' read -r target_type target_src target_dst <<<"$spec"
    _edge_mounts_check "matches_target" "$target_src" "$target_dst" "$target_type" || rc=$?
  elif _edge_target_uses_releases; then
    _edge_mounts_check "matches_target" "${REPO_ROOT}/web/releases" "/srv-releases" "bind" || rc=$?
  else
    _edge_mounts_check "matches_target" "${REPO_ROOT}/web/dist" "/srv" "bind" || rc=$?
  fi
  # rc=2 means no mount info available (minimal mock); rc=1 means web-root drift!
  [ "$rc" -eq 1 ] && return 0
  [ "$rc" -eq 2 ] && return 1
  # Also check /etc/caddy/Caddyfile live bind source when reported by docker inspect.
  mounts=$(_edge_live_mounts)
  if printf '%s\n' "$mounts" | grep -q -- '->/etc/caddy/Caddyfile$'; then
    py=$(_resolve_deploy_python) || return 1
    if ! "$py" - "$REPO_ROOT" "$mounts" <<'PY'
import os, sys
repo_root, mounts_raw = sys.argv[1:3]
std_caddy = os.path.realpath(os.path.join(repo_root, "deploy/dalton/Caddyfile"))
for raw in mounts_raw.splitlines():
    line = raw.strip()
    if "->" not in line:
        continue
    mtype, rest = line.split("|", 1) if ("|" in line and line.index("|") < line.index("->")) else ("bind", line)
    src, dst = rest.rsplit("->", 1)
    if dst.strip() == "/etc/caddy/Caddyfile":
        if mtype.strip().lower() != "bind" or os.path.realpath(src.strip()) != std_caddy:
            sys.exit(1)
sys.exit(0)
PY
    then
      return 0
    fi
  fi
  return 1
}

cleanup_old_container_logs() {
  [ -d "$CONTAINER_LOG_DIR" ] || return 0
  find "$CONTAINER_LOG_DIR" -maxdepth 1 -type f \( \
    -name 'backend-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz' \
    -o -name 'crawler-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz' \
    -o -name 'edge-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z.log.gz' \
  \) -mtime +"$CONTAINER_LOG_RETENTION_DAYS" -delete 2>/dev/null || true
  return 0
}

save_container_logs() {
  local service="${1:-}" container="${2:-}"
  local ts out_file tmp_file
  [ -n "$service" ] && [ -n "$container" ] || return 0
  if ! docker ps -aq --filter "name=${container}" 2>/dev/null | grep -q .; then
    return 0
  fi
  if ! mkdir -p "$CONTAINER_LOG_DIR" 2>/dev/null; then
    echo "WARN: failed to create container log dir $CONTAINER_LOG_DIR; continuing deploy" >&2
    return 0
  fi
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  out_file="${CONTAINER_LOG_DIR}/${service}-${ts}.log.gz"
  tmp_file="${out_file}.tmp.$$"
  if docker logs --timestamps "$container" 2>&1 | gzip -c > "$tmp_file"; then
    if ! mv -f "$tmp_file" "$out_file"; then
      rm -f "$tmp_file"
      echo "WARN: failed to save container logs for ${service} (${container}); continuing deploy" >&2
    fi
  else
    rm -f "$tmp_file"
    echo "WARN: failed to save container logs for ${service} (${container}); continuing deploy" >&2
  fi
  cleanup_old_container_logs
  return 0
}

apply_backend() {
  local out rc
  if [ "$RECREATE" = "1" ]; then
    save_container_logs "backend" "dalton-langalpha-backend-1"
    out=$(docker compose up -d --no-deps backend 2>&1)
    rc=$?
    [ -n "$out" ] && printf '%s\n' "$out" | tail -2
    return "$rc"
  fi
  docker restart dalton-langalpha-backend-1 >/dev/null
}

apply_crawler() {
  local out rc
  if [ "$RECREATE" = "1" ]; then
    save_container_logs "crawler" "dalton-langalpha-crawler-1"
    out=$(docker compose up -d --no-deps --no-build crawler 2>&1)
    rc=$?
    [ -n "$out" ] && printf '%s\n' "$out" | tail -1
    return "$rc"
  elif docker ps -q --filter name=dalton-langalpha-crawler-1 | grep -q .; then
    docker restart dalton-langalpha-crawler-1 >/dev/null
  else
    save_container_logs "crawler" "dalton-langalpha-crawler-1"
    out=$(docker compose up -d --no-deps --no-build crawler 2>&1)
    rc=$?
    [ -n "$out" ] && printf '%s\n' "$out" | tail -1
    return "$rc"
  fi
}

recreate_edge() {
  local up_out="" out="" rc=0
  _populate_edge_compose_args || return 1
  save_container_logs "edge" "dalton-langalpha-edge-1"
  up_out=$(mktemp "${TMPDIR:-/tmp}/dalton-edge-up.XXXXXX") || return 1
  if [ "${#EDGE_COMPOSE_ARGS[@]}" -gt 0 ]; then
    _run_cmd_in_pgid_with_timeout "$EDGE_COMPOSE_TIMEOUT" 0 "$up_out" docker compose "${EDGE_COMPOSE_ARGS[@]}" up -d --no-deps --force-recreate edge || rc=$?
  else
    _run_cmd_in_pgid_with_timeout "$EDGE_COMPOSE_TIMEOUT" 0 "$up_out" docker compose up -d --no-deps --force-recreate edge || rc=$?
  fi
  out=$(cat "$up_out" 2>/dev/null || true)
  rm -f "$up_out"
  [ -n "$out" ] && printf '%s\n' "$out" | tail -2
  if [ "$rc" -eq 124 ]; then
    echo "ERROR: docker compose edge recreate timed out after ${EDGE_COMPOSE_TIMEOUT}s" >&2
  fi
  return "$rc"
}

wait_backend_health() {
  local ok=0 code rc i
  for i in $(seq 1 80); do
    if health; then
      ok=1
      break
    fi
    [ "$i" -lt 80 ] && sleep 3
  done
  code=$(curl -s -m3 --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health)
  rc=$?
  echo "backend health ${code:-000}"
  [ "$ok" = "1" ] && [ "$rc" -eq 0 ] && [ "$code" = "200" ]
}

wait_crawler_health() {
  local ok=0 out rc i
  # the crawler takes ~15s to start serving; retry up to 45s before calling it unreachable
  for i in $(seq 1 15); do
    if docker exec dalton-langalpha-backend-1 sh -c 'curl -sf -m3 -o /dev/null http://crawler:8100/health' 2>/dev/null; then
      ok=1
      break
    fi
    [ "$i" -lt 15 ] && sleep 3
  done
  out=$(docker exec dalton-langalpha-backend-1 sh -c 'curl -sf -m5 -o /dev/null -w "crawler health %{http_code}\n" http://crawler:8100/health' 2>/dev/null)
  rc=$?
  if [ "$rc" -eq 0 ] && [ -n "$out" ]; then
    printf '%s\n' "$out"
  else
    echo "crawler health: unreachable"
  fi
  [ "$ok" = "1" ] && [ "$rc" -eq 0 ]
}

wait_edge_ready() {
  local caddyfile="${1:-${REPO_ROOT}/deploy/dalton/Caddyfile}"
  local ok=0 deadline now remaining max_time i
  deadline=$(( $(date +%s) + EDGE_WAIT_TIMEOUT ))
  for i in $(seq 1 "$EDGE_WAIT_TIMEOUT"); do
    now=$(date +%s)
    remaining=$(( deadline - now ))
    [ "$remaining" -le 0 ] && break
    max_time=2
    [ "$remaining" -lt "$max_time" ] && max_time="$remaining"
    if edge_ready "$max_time" "$caddyfile"; then
      if [ "$(date +%s)" -le "$deadline" ]; then
        ok=1
      fi
      break
    fi
    [ "$(date +%s)" -ge "$deadline" ] && break
    [ "$i" -lt "$EDGE_WAIT_TIMEOUT" ] && sleep 1
  done
  [ "$ok" = "1" ]
}

_gateway_dir() {
  printf '%s/integrations/dalton-channel-gateway' "$REPO_ROOT"
}

_gateway_deployed_tree_file() {
  printf '%s/.deploy-deployed-tree' "$(_gateway_dir)"
}

_gateway_pending_file() {
  printf '%s/.deploy-gateway-pending' "$(_gateway_dir)"
}

_write_gateway_pending() {
  local val="${1:-pending}" f="" tmp=""
  f=$(_gateway_pending_file)
  [ -d "$(dirname "$f")" ] || return 1
  tmp="${f}.tmp.$$"
  if ! printf '%s\n' "$val" > "$tmp"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! mv -f "$tmp" "$f"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  return 0
}

_clear_gateway_pending() {
  local f=""
  f=$(_gateway_pending_file)
  if [ -e "$f" ] || [ -L "$f" ]; then
    rm -f "$f" || return 1
  fi
  return 0
}

_read_gateway_deployed_tree() {
  local f="" val=""
  f=$(_gateway_deployed_tree_file)
  [ -f "$f" ] && [ -r "$f" ] || return 1
  IFS= read -r val < "$f" || [ -n "$val" ] || return 1
  val="${val%$'\r'}"
  case "$val" in
    ""|*[[:space:]]*) return 1 ;;
  esac
  printf '%s' "$val"
}

_read_release_tree_file() {
  local f="${1:-}" val=""
  [ -n "$f" ] && [ -f "$f" ] && [ -r "$f" ] || return 1
  IFS= read -r val < "$f" || [ -n "$val" ] || return 1
  val="${val%$'\r'}"
  case "$val" in
    ""|*[[:space:]]*) return 1 ;;
  esac
  printf '%s' "$val"
}

_write_gateway_deployed_tree() {
  local tree_hash="${1:-}" f="" tmp=""
  [ -n "$tree_hash" ] || return 1
  f=$(_gateway_deployed_tree_file)
  [ -d "$(dirname "$f")" ] || return 1
  tmp="${f}.tmp.$$"
  if ! printf '%s\n' "$tree_hash" > "$tmp"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  if ! mv -f "$tmp" "$f"; then
    rm -f "$tmp" 2>/dev/null || true
    return 1
  fi
  return 0
}

_atomic_replace_symlink() {
  local target_rel="$1" current_link="$2"
  local tmp_link="${current_link}.tmp.$$" py=""
  py=$(_resolve_deploy_python) || return 1
  rm -f "$tmp_link" || return 1
  if ! ln -s "$target_rel" "$tmp_link"; then
    rm -f "$tmp_link" 2>/dev/null || true
    return 1
  fi
  if ! "$py" -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$tmp_link" "$current_link"; then
    rm -f "$tmp_link" 2>/dev/null || true
    return 1
  fi
  return 0
}

_atomic_swap_paths() {
  local path_a="${1:-}" path_b="${2:-}" py=""
  [ -n "$path_a" ] && [ -n "$path_b" ] || return 1
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$path_a" "$path_b" <<'PY'
import ctypes
import errno
import os
import sys

a = os.fsencode(sys.argv[1])
b = os.fsencode(sys.argv[2])
if sys.platform == "darwin":
    RENAME_SWAP = 0x00000002
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    libc.renamex_np.restype = ctypes.c_int
    rc = libc.renamex_np(a, b, RENAME_SWAP)
    if rc != 0:
        err = ctypes.get_errno()
        print(
            f"ERROR: renamex_np(RENAME_SWAP) failed ({sys.argv[1]} <-> {sys.argv[2]}): [Errno {err}] {os.strerror(err)}",
            file=sys.stderr,
        )
        sys.exit(1)
elif sys.platform.startswith("linux"):
    AT_FDCWD = -100
    RENAME_EXCHANGE = 2
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "renameat2"):
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if not hasattr(libc, "renameat2"):
        print("ERROR: renameat2 not available in libc on Linux", file=sys.stderr)
        sys.exit(1)
    libc.renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    libc.renameat2.restype = ctypes.c_int
    rc = libc.renameat2(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE)
    if rc != 0:
        err = ctypes.get_errno()
        if err == errno.ENOSYS:
            print("ERROR: renameat2(RENAME_EXCHANGE) returned ENOSYS", file=sys.stderr)
        else:
            print(
                f"ERROR: renameat2(RENAME_EXCHANGE) failed ({sys.argv[1]} <-> {sys.argv[2]}): [Errno {err}] {os.strerror(err)}",
                file=sys.stderr,
            )
        sys.exit(1)
else:
    print(f"ERROR: unsupported platform for _atomic_swap_paths: {sys.platform}", file=sys.stderr)
    sys.exit(1)
PY
}

_frontend_releases_dir() {
  printf '%s/web/releases' "$REPO_ROOT"
}

_frontend_current_link() {
  printf '%s/current' "$(_frontend_releases_dir)"
}

_is_valid_frontend_release_id() {
  local rel_id="${1:-}"
  [ -n "$rel_id" ] || return 1
  case "$rel_id" in
    .|..|current|*/*|.*) return 1 ;;
  esac
  [[ "$rel_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

_read_frontend_current_release() {
  local cur_link="" target=""
  cur_link=$(_frontend_current_link)
  [ -L "$cur_link" ] || return 1
  target=$(readlink "$cur_link" 2>/dev/null || true)
  _is_valid_frontend_release_id "$target" || return 1
  printf '%s' "$target"
}

_read_frontend_build_id() {
  local rel_dir="${1:-}" py=""
  [ -n "$rel_dir" ] && [ -s "${rel_dir}/version.json" ] || return 1
  py=$(_resolve_deploy_python) || return 1
  "$py" -c 'import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
build = data.get("build") if isinstance(data, dict) else None
if not isinstance(build, str) or not build.strip():
    sys.exit(1)
print(build.strip(), end="")' "${rel_dir}/version.json" 2>/dev/null
}

_read_frontend_release_tag() {
  local rel_dir="${1:-}" py=""
  [ -n "$rel_dir" ] && [ -s "${rel_dir}/version.json" ] || return 1
  py=$(_resolve_deploy_python) || return 1
  "$py" -c 'import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
rel = data.get("release") if isinstance(data, dict) else None
if not isinstance(rel, str) or not rel.strip():
    sys.exit(1)
print(rel.strip(), end="")' "${rel_dir}/version.json" 2>/dev/null
}

_is_servable_legacy_dist_dir() {
  local rel_dir="${1:-}"
  [ -n "$rel_dir" ] && [ -d "$rel_dir" ] && [ ! -L "$rel_dir" ] \
    && [ -s "${rel_dir}/index.html" ] && [ -d "${rel_dir}/assets" ] && [ -s "${rel_dir}/help/index.html" ]
}

_is_servable_frontend_release_dir() {
  local rel_dir="${1:-}"
  _is_servable_legacy_dist_dir "$rel_dir" && [ -s "${rel_dir}/version.json" ]
}

_is_complete_frontend_release_dir() {
  _is_servable_frontend_release_dir "${1:-}"
}

_finalize_frontend_version_manifest() {
  local rel_dir="${1:-}" rel_id="${2:-}" legacy_mode="${3:-0}" py=""
  [ -n "$rel_dir" ] && [ -d "$rel_dir" ] || return 1
  rel_id="${rel_id:-$(basename "$rel_dir")}"
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$rel_dir" "$rel_id" "$legacy_mode" <<'PY'
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

rel_dir = Path(sys.argv[1])
rel_id = sys.argv[2].strip()
legacy_mode = sys.argv[3] == "1"
version_file = rel_dir / "version.json"
index_file = rel_dir / "index.html"
assets_dir = rel_dir / "assets"
help_dir = rel_dir / "help"

payload = {}
if version_file.is_file():
    try:
        loaded = json.loads(version_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    except Exception:
        payload = {}

entry_raw = payload.get("entry") or payload.get("build")
if not isinstance(entry_raw, str) or not entry_raw.strip():
    if not legacy_mode:
        print(f"ERROR: missing valid build/entry id in {version_file}", file=sys.stderr)
        sys.exit(1)
    legacy_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    legacy_tag = rel_id if rel_id.startswith("legacy-") else f"legacy-{legacy_ts}"
    detected_entry = ""
    if index_file.is_file():
        html = index_file.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r'src=["\']/assets/([^"\'?#]+)["\']', html):
            cand = match.group(1).strip()
            if cand and "/" not in cand and (assets_dir / cand).is_file():
                detected_entry = cand
                break
    entry_id = detected_entry or legacy_tag
    payload["legacy_generated"] = True
    payload["legacy_id"] = legacy_tag
else:
    entry_id = entry_raw.strip()

hasher = hashlib.sha256()
if help_dir.is_dir():
    for root, dirs, files in os.walk(help_dir):
        dirs.sort()
        for name in sorted(files):
            fp = Path(root) / name
            if fp.is_symlink() or not fp.is_file():
                continue
            rel = fp.relative_to(help_dir).as_posix()
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(fp.read_bytes())
            hasher.update(b"\0")
help_hash = hasher.hexdigest()[:16]

composite_build = f"{entry_id}+help-{help_hash}@{rel_id}"
payload["entry"] = entry_id
# Legacy copied JS reads only `build`; keep `build == entry` during legacy migration so
# existing browsers do not show false stale-build prompts, while `release` still carries
# the unique legacy release identity verified by check_public_edge.sh.
payload["build"] = entry_id if legacy_mode else composite_build
payload["help_hash"] = help_hash
payload["release_id"] = rel_id
payload["release"] = composite_build

tmp = version_file.with_name(f"version.json.tmp.{os.getpid()}")
tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
os.replace(tmp, version_file)
PY
}

_is_active_frontend_release_dir() {
  local dir="${1:-}" rel_dir="" cur_id="" base=""
  [ -n "$dir" ] || return 1
  rel_dir=$(_frontend_releases_dir)
  cur_id=$(_read_frontend_current_release || true)
  [ -n "$cur_id" ] || return 1
  base=$(basename "$dir")
  if [ "$dir" = "${rel_dir}/${cur_id}" ] || [ "$base" = "$cur_id" ]; then
    return 0
  fi
  return 1
}

# One-time migration from legacy web/dist into web/releases/legacy-<ts>-<pid> with
# web/releases/current pointing at it. Keeps web/dist intact on disk so rollback to a
# pre-releases compose/Caddyfile still works without rebuilding.
ensure_frontend_releases_initialized() {
  local rel_dir="" cur_link="" cur_id="" legacy_dist="" legacy_id="" legacy_dir=""
  rel_dir=$(_frontend_releases_dir)
  cur_link=$(_frontend_current_link)
  if [ -L "$rel_dir" ]; then
    echo "ERROR: ${rel_dir} must be a real directory, not a symlink" >&2
    return 1
  fi
  if [ -e "$rel_dir" ] && [ ! -d "$rel_dir" ]; then
    echo "ERROR: ${rel_dir} exists and is not a directory" >&2
    return 1
  fi
  mkdir -p "$rel_dir" || return 1
  cur_id=$(_read_frontend_current_release || true)
  if [ -n "$cur_id" ] && _is_servable_frontend_release_dir "${rel_dir}/${cur_id}"; then
    PREV_FRONTEND_RELEASE_ID="$cur_id"
    return 0
  fi
  legacy_dist="${REPO_ROOT}/web/dist"
  if _is_servable_legacy_dist_dir "$legacy_dist"; then
    legacy_id="legacy-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    legacy_dir="${rel_dir}/${legacy_id}"
    if ! mkdir "$legacy_dir" || ! cp -Rp "${legacy_dist}/." "${legacy_dir}/"; then
      rm -rf "$legacy_dir" 2>/dev/null || true
      echo "ERROR: failed to migrate ${legacy_dist} into ${legacy_dir}" >&2
      return 1
    fi
    if ! _finalize_frontend_version_manifest "$legacy_dir" "$legacy_id" 1; then
      rm -rf "$legacy_dir" 2>/dev/null || true
      echo "ERROR: failed to finalize version.json for migrated release ${legacy_dir}" >&2
      return 1
    fi
    if ! _record_own_frontend_assets "$legacy_dir"; then
      rm -rf "$legacy_dir" 2>/dev/null || true
      echo "ERROR: failed to record asset manifest for migrated release ${legacy_dir}" >&2
      return 1
    fi
    if ! _is_servable_frontend_release_dir "$legacy_dir"; then
      rm -rf "$legacy_dir" 2>/dev/null || true
      echo "ERROR: migrated frontend release incomplete: ${legacy_dir}" >&2
      return 1
    fi
    if ! _atomic_replace_symlink "$legacy_id" "$cur_link"; then
      rm -rf "$legacy_dir" 2>/dev/null || true
      echo "ERROR: failed to set ${cur_link} -> ${legacy_id}" >&2
      return 1
    fi
    PREV_FRONTEND_RELEASE_ID="$legacy_id"
    echo "$(date '+%T') migrated legacy web/dist into web/releases/${legacy_id} (web/dist preserved)"
  fi
  return 0
}

_record_own_frontend_assets() {
  local rel_dir="${1:-}" py=""
  [ -n "$rel_dir" ] && [ -d "${rel_dir}/assets" ] || return 1
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$rel_dir" <<'PY'
import os
import sys
from pathlib import Path

rel_dir = Path(sys.argv[1])
pagefind_hashed_suffixes = (".pf_fragment", ".pf_index", ".pf_meta", ".pf_filter")
own = []
for sub in ("assets", "help/_astro", "help/pagefind"):
    root_dir = rel_dir / sub
    if not root_dir.is_dir() or root_dir.is_symlink():
        continue
    for cur_root, dirs, files in os.walk(root_dir):
        dirs.sort()
        for name in sorted(files):
            fp = Path(cur_root) / name
            if fp.is_symlink() or not fp.is_file():
                continue
            if sub == "help/pagefind" and not name.endswith(pagefind_hashed_suffixes):
                continue
            own.append(fp.relative_to(rel_dir).as_posix())
own.sort()
manifest = rel_dir / ".deploy-own-assets"
with manifest.open("w", encoding="utf-8") as fh:
    fh.write("# format=v2\n")
    for item in own:
        fh.write(f"{item}\n")
PY
}

# Copy (hard-link with copy fallback) a release's own hashed files under assets/,
# help/_astro/, and content-addressed help/pagefind/ files (*.pf_fragment, *.pf_index,
# *.pf_meta, *.pf_filter) into target_dir without overwriting target files, so open tabs
# loading old (or rolled-back new) lazy chunks/search shards never hit 404 and assets do
# not accumulate unboundedly across generations.
_carry_over_hashed_assets() {
  local src_dir="${1:-}" dst_dir="${2:-}" py=""
  [ -n "$src_dir" ] && [ -n "$dst_dir" ] || return 0
  [ "$src_dir" != "$dst_dir" ] || return 0
  [ -d "$src_dir" ] && [ -d "$dst_dir" ] || return 0
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$src_dir" "$dst_dir" <<'PY'
import os
import re
import shutil
import sys
from pathlib import Path

src_dir = Path(sys.argv[1])
dst_dir = Path(sys.argv[2])
# Match Vite (-<hash>.<ext>) or Astro/hashed (.<hash>.<ext>) filenames in top-level assets/.
top_asset_hashed_re = re.compile(r"(?:-[A-Za-z0-9_-]+|\.[A-Za-z0-9_-]{6,})\.[A-Za-z0-9]+$")
pagefind_hashed_suffixes = (".pf_fragment", ".pf_index", ".pf_meta", ".pf_filter")


def _scan_sub(sub: str, only_pagefind_hashed: bool = False) -> list[str]:
    found = []
    sub_dir = src_dir / sub
    if not sub_dir.is_dir() or sub_dir.is_symlink():
        return found
    for cur_root, dirs, files in os.walk(sub_dir):
        dirs.sort()
        for name in sorted(files):
            fp = Path(cur_root) / name
            if fp.is_symlink() or not fp.is_file():
                continue
            if only_pagefind_hashed and not name.endswith(pagefind_hashed_suffixes):
                continue
            found.append(fp.relative_to(src_dir).as_posix())
    return found


manifest = src_dir / ".deploy-own-assets"
candidates = []
if manifest.is_file():
    lines = [raw.strip() for raw in manifest.read_text(encoding="utf-8").splitlines() if raw.strip()]
    is_v2 = bool(lines and lines[0] == "# format=v2")
    for line in lines:
        if line.startswith("#"):
            continue
        if "/" not in line:
            candidates.append(f"assets/{line}")
        else:
            candidates.append(line)
    # Legacy r2 manifests only listed assets/ files; supplement with help/_astro and hashed help/pagefind.
    if not is_v2:
        candidates.extend(_scan_sub("help/_astro"))
        candidates.extend(_scan_sub("help/pagefind", only_pagefind_hashed=True))
else:
    candidates.extend(_scan_sub("assets"))
    candidates.extend(_scan_sub("help/_astro"))
    candidates.extend(_scan_sub("help/pagefind", only_pagefind_hashed=True))

seen = set()
for rel in candidates:
    if not rel or rel in seen or rel.startswith("/") or "\\" in rel:
        continue
    seen.add(rel)
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        continue
    if parts[0] == "assets":
        if len(parts) == 2 and not top_asset_hashed_re.search(parts[1]):
            continue
    elif len(parts) >= 3 and parts[0] == "help" and parts[1] == "_astro":
        pass
    elif len(parts) >= 3 and parts[0] == "help" and parts[1] == "pagefind":
        if not parts[-1].endswith(pagefind_hashed_suffixes):
            continue
    else:
        continue
    src = src_dir / rel
    dst = dst_dir / rel
    if src.is_symlink() or not src.is_file():
        continue
    if dst.exists() or dst.is_symlink():
        continue
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        tmp_dst = dst.with_name(f".{dst.name}.tmp.{os.getpid()}")
        try:
            shutil.copy2(src, tmp_dst)
            os.replace(tmp_dst, dst)
        except Exception:
            try:
                if tmp_dst.exists() or tmp_dst.is_symlink():
                    tmp_dst.unlink()
            except OSError:
                pass
            raise
PY
}

_carry_over_prev_hashed_assets() {
  _carry_over_hashed_assets "${1:-}" "${2:-}"
}

cleanup_frontend_staging() {
  local rel_dir="" rc=0
  rel_dir=$(_frontend_releases_dir)
  rm -f "${rel_dir}/current.tmp.$$" 2>/dev/null || true
  if [ -n "${ACTIVE_FRONTEND_RELEASE_DIR:-}" ] && { [ -e "${ACTIVE_FRONTEND_RELEASE_DIR}" ] || [ -L "${ACTIVE_FRONTEND_RELEASE_DIR}" ]; }; then
    if _is_active_frontend_release_dir "${ACTIVE_FRONTEND_RELEASE_DIR}"; then
      ACTIVE_FRONTEND_RELEASE_DIR=""
    else
      rm -rf "${ACTIVE_FRONTEND_RELEASE_DIR}" || rc=$?
    fi
  fi
  if [ "$rc" -eq 0 ]; then
    ACTIVE_FRONTEND_RELEASE_DIR=""
  fi
  return "$rc"
}

switch_frontend_from_staging() {
  local rel_dir="" cur_link="" new_id="${NEW_FRONTEND_RELEASE_ID:-}" new_dir="" cur_id=""
  rel_dir=$(_frontend_releases_dir)
  cur_link=$(_frontend_current_link)
  new_dir="${ACTIVE_FRONTEND_RELEASE_DIR:-${rel_dir}/${new_id}}"
  if ! _is_valid_frontend_release_id "$new_id" || ! _is_servable_frontend_release_dir "$new_dir"; then
    echo "ERROR: frontend release directory incomplete: ${new_dir:-unset}" >&2
    return 1
  fi
  cur_id=$(_read_frontend_current_release || true)
  if [ -n "$cur_id" ] && [ "$cur_id" != "$new_id" ]; then
    PREV_FRONTEND_RELEASE_ID="$cur_id"
  fi
  # Reuse the single atomic symlink replacement helper shared with channel-gateway
  if ! _atomic_replace_symlink "$new_id" "$cur_link"; then
    echo "ERROR: failed to atomically switch ${cur_link} -> ${new_id}" >&2
    return 1
  fi
  ACTIVE_FRONTEND_RELEASE_DIR=""
  FRONTEND_SWITCHED=1
  return 0
}

restore_frontend_from_prev() {
  local rel_dir="" cur_link="" prev_id="${PREV_FRONTEND_RELEASE_ID:-}" active_id=""
  rel_dir=$(_frontend_releases_dir)
  cur_link=$(_frontend_current_link)
  FRONTEND_CARRY_OVER_FAILED=0
  if ! _is_valid_frontend_release_id "$prev_id" || ! _is_servable_frontend_release_dir "${rel_dir}/${prev_id}"; then
    echo "ERROR: previous frontend release unavailable for rollback: ${prev_id:-unset}" >&2
    FRONTEND_REVERT_STATE="restore_failed"
    return 1
  fi
  # Before switching current back, carry over the outgoing new release's hashed assets
  # (assets/, help/_astro/, and hashed help/pagefind/) into prev_id so browsers that fetched
  # the new HTML during public edge checks keep receiving 200 after rollback.
  active_id=$(_read_frontend_current_release || printf '%s' "${NEW_FRONTEND_RELEASE_ID:-}")
  if [ -n "$active_id" ] && [ "$active_id" != "$prev_id" ] && [ -d "${rel_dir}/${active_id}" ]; then
    if ! _carry_over_hashed_assets "${rel_dir}/${active_id}" "${rel_dir}/${prev_id}"; then
      FRONTEND_CARRY_OVER_FAILED=1
      echo "WARN: failed to carry over outgoing release (${active_id}) hashed assets into ${prev_id}; restoring current to ${prev_id} and preserving ${active_id}" >&2
    fi
  fi
  if ! _atomic_replace_symlink "$prev_id" "$cur_link"; then
    echo "ERROR: failed to atomically restore ${cur_link} -> ${prev_id}" >&2
    FRONTEND_REVERT_STATE="restore_failed"
    return 1
  fi
  FRONTEND_SWITCHED=0
  FRONTEND_REVERT_STATE="reverted"
  return 0
}

prune_old_frontend_releases() {
  local keep="${1:-$FRONTEND_KEEP_RELEASES}" rel_dir="" cur_id="" prev_id="" py="" local_lock=0 rc=0
  _ensure_deploy_lock_held || return 1
  local_lock="$_ENSURED_LOCAL_LOCK"
  if [ "$local_lock" = "1" ]; then
    trap '_on_deploy_interrupt_signal INT' INT
    trap '_on_deploy_interrupt_signal TERM' TERM
  fi
  rel_dir=$(_frontend_releases_dir)
  if [ ! -d "$rel_dir" ]; then
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 0
  fi
  cur_id=$(_read_frontend_current_release || true)
  prev_id="${PREV_FRONTEND_RELEASE_ID:-}"
  if ! py=$(_resolve_deploy_python); then
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  fi
  "$py" - "$rel_dir" "$keep" "$cur_id" "$prev_id" <<'PY' || rc=$?
import os
import shutil
import sys

rel_dir, keep_raw, cur_id, prev_id = sys.argv[1:5]
keep = max(1, int(keep_raw))
pinned = {x for x in (cur_id, prev_id) if x}
protected = set()
pin_dir = os.path.join(rel_dir, ".pinned-releases")
if os.path.isdir(pin_dir):
    for pin_name in os.listdir(pin_dir):
        if pin_name and not pin_name.startswith("."):
            protected.add(pin_name)

dirs = []
for name in os.listdir(rel_dir):
    if name == "current" or name.startswith("."):
        continue
    full = os.path.join(rel_dir, name)
    if os.path.islink(full) or not os.path.isdir(full):
        continue
    st = os.stat(full)
    dirs.append((st.st_mtime_ns, name, full))

dirs.sort(key=lambda item: (item[0], item[1]), reverse=True)
kept = set(pinned)
for _mtime, name, _full in dirs:
    if len(kept) >= keep:
        break
    kept.add(name)
kept.update(protected)

for _mtime, name, full in dirs:
    if name not in kept:
        shutil.rmtree(full)
PY
  if [ "$local_lock" = "1" ]; then
    trap - INT TERM
    release_deploy_lock || true
  fi
  return "$rc"
}

manual_switch_frontend_release() {
  local rel_id="${1:-}" rel_dir="" cur_link="" cur_id="" local_lock=0 rc=0
  _ensure_deploy_lock_held || return 1
  local_lock="$_ENSURED_LOCAL_LOCK"
  if [ "$local_lock" = "1" ]; then
    trap '_on_deploy_interrupt_signal INT' INT
    trap '_on_deploy_interrupt_signal TERM' TERM
  fi
  rel_dir=$(_frontend_releases_dir)
  cur_link=$(_frontend_current_link)
  if ! _is_valid_frontend_release_id "$rel_id" || ! _is_servable_frontend_release_dir "${rel_dir}/${rel_id}"; then
    echo "ERROR: manual_switch_frontend_release: invalid or incomplete release '${rel_id}' under ${rel_dir}" >&2
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  fi
  cur_id=$(_read_frontend_current_release || true)
  if [ -n "$cur_id" ] && [ "$cur_id" != "$rel_id" ] && [ -d "${rel_dir}/${cur_id}" ]; then
    if ! _carry_over_hashed_assets "${rel_dir}/${cur_id}" "${rel_dir}/${rel_id}"; then
      echo "ERROR: manual_switch_frontend_release: failed to carry over hashed assets from ${cur_id} into ${rel_id}; aborting switch" >&2
      if [ "$local_lock" = "1" ]; then
        trap - INT TERM
        release_deploy_lock || true
      fi
      return 1
    fi
  fi
  if ! _atomic_replace_symlink "$rel_id" "$cur_link"; then
    rc=1
  else
    echo "switched ${cur_link} -> ${rel_id}"
  fi
  if [ "$local_lock" = "1" ]; then
    trap - INT TERM
    release_deploy_lock || true
  fi
  return "$rc"
}

manual_build_frontend_release() {
  local rel_dir="" cur_id="" short_head="" new_id="" new_dir="" local_lock=0 build_out_file="" build_rc=0
  _ensure_deploy_lock_held || return 1
  local_lock="$_ENSURED_LOCAL_LOCK"
  if [ "$local_lock" = "1" ]; then
    trap '_on_deploy_interrupt_signal INT' INT
    trap '_on_deploy_interrupt_signal TERM' TERM
  fi
  if ! cd "$REPO_ROOT"; then
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  fi
  if ! ensure_frontend_releases_initialized; then
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  fi
  rel_dir=$(_frontend_releases_dir)
  cur_id=$(_read_frontend_current_release || true)
  short_head=$(git rev-parse --short=12 HEAD 2>/dev/null || echo "manual")
  new_id="${short_head}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  new_dir="${rel_dir}/${new_id}"
  build_out_file=$(mktemp "${TMPDIR:-/tmp}/dalton-manual-fe-build.XXXXXX") || {
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  }
  ACTIVE_FRONTEND_RELEASE_DIR="$new_dir"
  ACTIVE_STEP_OUT_FILE="$build_out_file"
  build_rc=0
  if _spawn_in_new_pgid "$build_out_file" bash scripts/dalton/build_frontend_prod.sh --require-supabase --out-dir "$new_dir"; then
    wait "$ACTIVE_STEP_CHILD_PID" || build_rc=$?
    ACTIVE_STEP_CHILD_PID=""
    ACTIVE_STEP_PGID=""
  else
    build_rc=1
  fi
  cat "$build_out_file" 2>/dev/null || true
  rm -f "$build_out_file"
  ACTIVE_STEP_OUT_FILE=""
  if [ "$build_rc" -ne 0 ] || ! _is_servable_frontend_release_dir "$new_dir"; then
    rm -rf "$new_dir" 2>/dev/null || true
    ACTIVE_FRONTEND_RELEASE_DIR=""
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  fi
  if ! _finalize_frontend_version_manifest "$new_dir" "$new_id" 0 \
     || ! _record_own_frontend_assets "$new_dir"; then
    rm -rf "$new_dir" 2>/dev/null || true
    ACTIVE_FRONTEND_RELEASE_DIR=""
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  fi
  if [ -n "$cur_id" ] && [ -d "${rel_dir}/${cur_id}" ]; then
    if ! _carry_over_hashed_assets "${rel_dir}/${cur_id}" "$new_dir"; then
      rm -rf "$new_dir" 2>/dev/null || true
      ACTIVE_FRONTEND_RELEASE_DIR=""
      if [ "$local_lock" = "1" ]; then
        trap - INT TERM
        release_deploy_lock || true
      fi
      return 1
    fi
  fi
  if ! _atomic_replace_symlink "$new_id" "$(_frontend_current_link)"; then
    rm -rf "$new_dir" 2>/dev/null || true
    ACTIVE_FRONTEND_RELEASE_DIR=""
    if [ "$local_lock" = "1" ]; then
      trap - INT TERM
      release_deploy_lock || true
    fi
    return 1
  fi
  ACTIVE_FRONTEND_RELEASE_DIR=""
  echo "built and activated web/releases/${new_id}"
  if [ "$local_lock" = "1" ]; then
    trap - INT TERM
    release_deploy_lock || true
  fi
  return 0
}

_is_active_gateway_release_path() {
  local dir="${1:-}" gw_dir="" live_rel="" live_dir="" cand_real="" live_real="" rel_from_gw=""
  [ -n "$dir" ] || return 1
  gw_dir=$(_gateway_dir)
  [ -L "${gw_dir}/.deploy-current" ] || return 1
  live_rel=$(readlink "${gw_dir}/.deploy-current" 2>/dev/null || true)
  [ -n "$live_rel" ] || return 1
  if [ -n "${STAGED_GATEWAY_RELEASE_ID:-}" ] && [ ".deploy-releases/${STAGED_GATEWAY_RELEASE_ID}" = "$live_rel" ]; then
    return 0
  fi
  rel_from_gw="${dir#"${gw_dir}/"}"
  if [ "$rel_from_gw" = "$live_rel" ]; then
    return 0
  fi
  case "$live_rel" in
    /*) live_dir="$live_rel" ;;
    *) live_dir="${gw_dir}/${live_rel}" ;;
  esac
  if [ -d "$dir" ] && [ -d "$live_dir" ]; then
    cand_real=$(cd "$dir" 2>/dev/null && pwd -P || true)
    live_real=$(cd "$live_dir" 2>/dev/null && pwd -P || true)
    if [ -n "$cand_real" ] && [ "$cand_real" = "$live_real" ]; then
      return 0
    fi
  fi
  return 1
}

_join_by_comma_space() {
  local out="" item=""
  for item in "$@"; do
    if [ -z "$out" ]; then
      out="$item"
    else
      out="${out}, ${item}"
    fi
  done
  printf '%s' "$out"
}

_describe_gateway_disk_state() {
  local _prefer_prev="${1:-0}" _cur_head="${2:-}"
  local gw_dir="" current_target="" d="" known_tree="" pending_state="否"
  local -a missing=() complete_releases=() other_saved=()
  local missing_str="" complete_str="无" other_str="无" layout_desc="" dist_kind="" nm_kind=""
  gw_dir=$(_gateway_dir)
  if [ -L "${gw_dir}/.deploy-current" ]; then
    current_target=$(readlink "${gw_dir}/.deploy-current" 2>/dev/null || true)
  fi
  if [ ! -f "${gw_dir}/dist/index.mjs" ]; then
    missing+=("dist")
  fi
  if [ ! -d "${gw_dir}/node_modules" ]; then
    missing+=("node_modules")
  fi
  if [ -L "${gw_dir}/dist" ] && [ "$(readlink "${gw_dir}/dist" 2>/dev/null)" = ".deploy-current/dist" ] \
     && [ -L "${gw_dir}/node_modules" ] && [ "$(readlink "${gw_dir}/node_modules" 2>/dev/null)" = ".deploy-current/node_modules" ]; then
    layout_desc="symlink -> .deploy-current/*"
  elif [ -d "${gw_dir}/dist" ] && [ ! -L "${gw_dir}/dist" ] \
       && [ -d "${gw_dir}/node_modules" ] && [ ! -L "${gw_dir}/node_modules" ]; then
    layout_desc="真实目录 dist 与 node_modules"
  else
    dist_kind="缺失"
    nm_kind="缺失"
    if [ -L "${gw_dir}/dist" ]; then
      dist_kind="symlink($(readlink "${gw_dir}/dist" 2>/dev/null || true))"
    elif [ -e "${gw_dir}/dist" ]; then
      dist_kind="真实目录"
    fi
    if [ -L "${gw_dir}/node_modules" ]; then
      nm_kind="symlink($(readlink "${gw_dir}/node_modules" 2>/dev/null || true))"
    elif [ -e "${gw_dir}/node_modules" ]; then
      nm_kind="真实目录"
    fi
    layout_desc="非标准布局(dist=${dist_kind}, node_modules=${nm_kind})"
  fi
  case "$current_target" in
    .deploy-releases/*)
      known_tree=$(_read_release_tree_file "${gw_dir}/${current_target}/.deploy-tree" || true)
      ;;
  esac
  if [ -z "$known_tree" ]; then
    known_tree=$(_read_gateway_deployed_tree || true)
  fi
  if [ -f "$(_gateway_pending_file)" ]; then
    pending_state="是"
  fi
  for d in "${gw_dir}"/.deploy-releases/*; do
    if [ -d "$d" ]; then
      if [ -f "${d}/dist/index.mjs" ] && [ -d "${d}/node_modules" ]; then
        complete_releases+=("${d#"${REPO_ROOT}/"}")
      else
        other_saved+=("${d#"${REPO_ROOT}/"}")
      fi
    fi
  done
  for d in "${gw_dir}"/.deploy-legacy-backup-* "${gw_dir}/.deploy-prev" "${gw_dir}"/.deploy-failed.*; do
    if [ -d "$d" ]; then
      other_saved+=("${d#"${REPO_ROOT}/"}")
    fi
  done
  if [ "${#complete_releases[@]}" -gt 0 ]; then
    complete_str=$(_join_by_comma_space "${complete_releases[@]}")
  fi
  if [ "${#other_saved[@]}" -gt 0 ]; then
    other_str=$(_join_by_comma_space "${other_saved[@]}")
  fi
  if [ "${#missing[@]}" -gt 0 ]; then
    missing_str=$(_join_by_comma_space "${missing[@]}")
    printf '不完整（缺失: %s；.deploy-current=%s；布局: %s；已知树哈希: %s；pending=%s；可恢复完整 release: %s；其他保留路径: %s）' \
      "$missing_str" "${current_target:-未设置}" "$layout_desc" "${known_tree:-未知}" "$pending_state" "$complete_str" "$other_str"
    return 0
  fi
  printf '完整（dist/index.mjs 与 node_modules 均存在；.deploy-current=%s；布局: %s；已知树哈希: %s；pending=%s；可恢复完整 release: %s；其他保留路径: %s）' \
    "${current_target:-未设置}" "$layout_desc" "${known_tree:-未知}" "$pending_state" "$complete_str" "$other_str"
}

cleanup_gateway_staging() {
  local gw_dir="" rc=0
  gw_dir=$(_gateway_dir)
  if [ -n "${ACTIVE_GATEWAY_STAGING_DIR:-}" ] && [ -e "${ACTIVE_GATEWAY_STAGING_DIR}" ]; then
    if _is_active_gateway_release_path "${ACTIVE_GATEWAY_STAGING_DIR}"; then
      ACTIVE_GATEWAY_STAGING_DIR=""
      STAGED_GATEWAY_RELEASE_ID=""
    else
      rm -rf "${ACTIVE_GATEWAY_STAGING_DIR}" || rc=$?
    fi
  fi
  if [ -e "${gw_dir}/.deploy-staging" ]; then
    rm -rf "${gw_dir}/.deploy-staging" || rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    ACTIVE_GATEWAY_STAGING_DIR=""
    STAGED_GATEWAY_RELEASE_ID=""
  fi
  return "$rc"
}

ACTIVE_STEP_WATCHDOG_PID=""
ACTIVE_STEP_TIMEOUT_MARKER=""

_stop_active_step_watchdog() {
  if [ -n "${ACTIVE_STEP_WATCHDOG_PID:-}" ]; then
    kill -KILL "${ACTIVE_STEP_WATCHDOG_PID}" 2>/dev/null || true
    _wait_pid_ignoring_signals "${ACTIVE_STEP_WATCHDOG_PID}" || true
    ACTIVE_STEP_WATCHDOG_PID=""
  fi
  if [ -n "${ACTIVE_STEP_TIMEOUT_MARKER:-}" ]; then
    rm -f "${ACTIVE_STEP_TIMEOUT_MARKER}" 2>/dev/null || true
    ACTIVE_STEP_TIMEOUT_MARKER=""
  fi
}

_spawn_in_new_pgid() {
  local out_file="$1" py=""
  shift
  py=$(_resolve_deploy_python) || return 1
  "$py" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "$@" 9>&- >"$out_file" 2>&1 &
  ACTIVE_STEP_CHILD_PID=$!
  ACTIVE_STEP_PGID="$ACTIVE_STEP_CHILD_PID"
}

_run_cmd_in_pgid_with_timeout() {
  local timeout_sec="${1:-120}" ignore_signals="${2:-0}" out_file="$3"
  shift 3
  local py="" marker="" rc=0 timed_out=0 child_pid="" child_pgid=""
  py=$(_resolve_deploy_python) || return 1
  marker=$(mktemp "${TMPDIR:-/tmp}/dalton-step-timeout.XXXXXX") || return 1
  rm -f "$marker"
  ACTIVE_STEP_OUT_FILE="$out_file"
  ACTIVE_STEP_TIMEOUT_MARKER="$marker"
  if ! _spawn_in_new_pgid "$out_file" "$@"; then
    ACTIVE_STEP_OUT_FILE=""
    ACTIVE_STEP_TIMEOUT_MARKER=""
    return 1
  fi
  child_pid="$ACTIVE_STEP_CHILD_PID"
  child_pgid="${ACTIVE_STEP_PGID:-$child_pid}"
  "$py" - "$timeout_sec" "$child_pgid" "$child_pid" "$marker" 9>&- >/dev/null 2>&1 <<'PY' &
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
try:
    os.setsid()
except OSError:
    pass
timeout_sec = float(sys.argv[1])
pgid = int(sys.argv[2])
pid = int(sys.argv[3])
marker = sys.argv[4]

def _alive() -> bool:
    for fn, target in ((os.killpg, pgid), (os.kill, pid)):
        try:
            fn(target, 0)
            return True
        except ProcessLookupError:
            pass
        except PermissionError:
            return True
    return False

deadline = time.monotonic() + timeout_sec
while time.monotonic() < deadline:
    if not _alive():
        sys.exit(0)
    time.sleep(min(0.05, max(0.01, deadline - time.monotonic())))

if not _alive():
    sys.exit(0)
try:
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write("timeout\n")
except OSError:
    pass
for fn, target in ((os.killpg, pgid), (os.kill, pid)):
    try:
        fn(target, signal.SIGTERM)
    except OSError:
        pass
term_deadline = time.monotonic() + 2.0
while time.monotonic() < term_deadline:
    if not _alive():
        sys.exit(0)
    time.sleep(0.05)
for fn, target in ((os.killpg, pgid), (os.kill, pid)):
    try:
        fn(target, signal.SIGKILL)
    except OSError:
        pass
kill_deadline = time.monotonic() + 5.0
while time.monotonic() < kill_deadline:
    if not _alive():
        sys.exit(0)
    time.sleep(0.05)
PY
  ACTIVE_STEP_WATCHDOG_PID=$!

  if [ "$ignore_signals" = "1" ]; then
    _wait_pid_ignoring_signals "$child_pid" || rc=$?
  else
    wait "$child_pid" || rc=$?
  fi

  if [ -f "$marker" ]; then
    timed_out=1
  fi
  _stop_active_step_watchdog
  if [ "$timed_out" = "1" ]; then
    _terminate_active_step_pgid "$child_pid" "$child_pgid"
    rc=124
  fi
  ACTIVE_STEP_CHILD_PID=""
  ACTIVE_STEP_PGID=""
  ACTIVE_STEP_OUT_FILE=""
  return "$rc"
}

_terminate_active_step_pgid() {
  local pid="${1:-}" pgid="${2:-}" py="" killer_pid=""
  [ -n "$pid" ] || return 0
  pgid="${pgid:-$pid}"
  kill -TERM -- "-${pgid}" 2>/dev/null || true
  kill -TERM "$pid" 2>/dev/null || true
  py=$(_resolve_deploy_python 2>/dev/null || true)
  if [ -n "$py" ]; then
    "$py" - "$pgid" "$pid" 9>&- >/dev/null 2>&1 <<'PY' &
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
try:
    os.setsid()
except OSError:
    pass
pgid, pid = int(sys.argv[1]), int(sys.argv[2])
time.sleep(5.0)
for fn, target in ((os.killpg, pgid), (os.kill, pid)):
    try:
        fn(target, signal.SIGKILL)
    except OSError:
        pass
PY
    killer_pid=$!
  fi
  _wait_pid_ignoring_signals "$pid" || true
  if [ -n "$killer_pid" ]; then
    kill -KILL "$killer_pid" 2>/dev/null || true
    _wait_pid_ignoring_signals "$killer_pid" || true
  fi
  if [ -n "$py" ]; then
    # Run PGID reaper in its own session ignoring INT/TERM so a second multicast signal
    # to the caller's process group cannot abort waiting for build descendants to exit.
    "$py" - "$pgid" 9>&- >/dev/null 2>&1 <<'PY' &
import os, signal, sys, time
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
try:
    os.setsid()
except OSError:
    pass
pgid = int(sys.argv[1])

def _pgid_alive() -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

if not _pgid_alive():
    sys.exit(0)
try:
    os.killpg(pgid, signal.SIGTERM)
except OSError:
    pass
deadline = time.monotonic() + 3.0
while time.monotonic() < deadline:
    if not _pgid_alive():
        sys.exit(0)
    time.sleep(0.05)
try:
    os.killpg(pgid, signal.SIGKILL)
except OSError:
    pass
kill_deadline = time.monotonic() + 5.0
while time.monotonic() < kill_deadline:
    if not _pgid_alive():
        sys.exit(0)
    time.sleep(0.05)
PY
    local reaper_pid=$!
    _wait_pid_ignoring_signals "$reaper_pid" || true
  fi
}

_on_deploy_interrupt_signal() {
  local sig="${1:-TERM}" code=143 gw_dir="" live_rel="" fe_cur=""
  # Defer any subsequent INT/TERM signals so PGID cleanup and first-rollout rollback
  # finish atomically instead of being aborted mid-way by a second signal.
  trap '_record_deferred_signal INT' INT
  trap '_record_deferred_signal TERM' TERM
  [ "$sig" = "INT" ] && code=130
  if [ -n "${ACTIVE_REEXEC_CHILD_PID:-}" ]; then
    # Backgrounded children in non-interactive shells ignore SIGINT, so always forward SIGTERM.
    kill -TERM "${ACTIVE_REEXEC_CHILD_PID}" 2>/dev/null || true
    _wait_pid_ignoring_signals "${ACTIVE_REEXEC_CHILD_PID}" || true
    ACTIVE_REEXEC_CHILD_PID=""
    if [ -n "${ACTIVE_REEXEC_TMP_DIR:-}" ] && [ -e "${ACTIVE_REEXEC_TMP_DIR}" ]; then
      rm -rf "${ACTIVE_REEXEC_TMP_DIR}" 2>/dev/null || true
      ACTIVE_REEXEC_TMP_DIR=""
    fi
    release_deploy_lock || true
    echo "ERROR: deploy interrupted by SIG${sig} while re-executed child was running" >&2
    exit "$code"
  fi
  _stop_active_step_watchdog
  if [ -n "${ACTIVE_STEP_CHILD_PID:-}" ]; then
    _terminate_active_step_pgid "${ACTIVE_STEP_CHILD_PID}" "${ACTIVE_STEP_PGID:-$ACTIVE_STEP_CHILD_PID}"
    ACTIVE_STEP_CHILD_PID=""
    ACTIVE_STEP_PGID=""
  fi
  if [ -n "${ACTIVE_STEP_OUT_FILE:-}" ] && [ -e "${ACTIVE_STEP_OUT_FILE}" ]; then
    rm -f "${ACTIVE_STEP_OUT_FILE}" 2>/dev/null || true
    ACTIVE_STEP_OUT_FILE=""
  fi
  if [ "${FIRST_ROLLOUT_EDGE_TOUCHED:-0}" = "1" ] && [ -n "${FIRST_ROLLOUT_ROLLBACK_PRE_FF:-}" ]; then
    echo "ERROR: first-releases-rollout interrupted by SIG${sig} after edge recreate started; rolling edge back to web/dist (PRE_FF=${FIRST_ROLLOUT_ROLLBACK_PRE_FF})" >&2
    ROLLBACK_IN_PROGRESS=1
    if _rollback_first_rollout_edge "${FIRST_ROLLOUT_ROLLBACK_PRE_FF}"; then
      FIRST_ROLLOUT_EDGE_TOUCHED=0
      ROLLBACK_IN_PROGRESS=0
      FIRST_ROLLOUT_ROLLBACK_PRE_FF=""
      echo "first-releases-rollout rollback: edge restored onto web/dist after SIG${sig}"
      release_deploy_lock || true
      [ "${DEFERRED_SIGNAL:-}" = "INT" ] && code=130
      [ "${DEFERRED_SIGNAL:-}" = "TERM" ] && code=143
      exit "$code"
    else
      ROLLBACK_IN_PROGRESS=0
      _mark_rollback_failed_lock "${FIRST_ROLLOUT_ROLLBACK_PRE_FF}" "interrupt_rollback_failed_SIG${sig}"
      echo "ERROR: first-releases-rollout rollback failed after SIG${sig} (PRE_FF=${FIRST_ROLLOUT_ROLLBACK_PRE_FF}); preserving failure lock and rollback snapshot" >&2
      _print_first_rollout_manual_recovery_steps "${FIRST_ROLLOUT_ROLLBACK_PRE_FF}"
      exit "$code"
    fi
  fi
  gw_dir=$(_gateway_dir)
  rm -f "$(_frontend_releases_dir)/current.tmp.$$" 2>/dev/null || true
  if [ -n "${ACTIVE_FRONTEND_RELEASE_DIR:-}" ] && { [ -e "${ACTIVE_FRONTEND_RELEASE_DIR}" ] || [ -L "${ACTIVE_FRONTEND_RELEASE_DIR}" ]; }; then
    if _is_active_frontend_release_dir "${ACTIVE_FRONTEND_RELEASE_DIR}"; then
      fe_cur=$(_read_frontend_current_release || printf '%s' "${ACTIVE_FRONTEND_RELEASE_DIR}")
      ACTIVE_FRONTEND_RELEASE_DIR=""
      echo "WARN: deploy interrupted by SIG${sig}; preserving active frontend release ${fe_cur}" >&2
    elif rm -rf "${ACTIVE_FRONTEND_RELEASE_DIR}" 2>/dev/null; then
      ACTIVE_FRONTEND_RELEASE_DIR=""
    else
      echo "WARN: deploy interrupted by SIG${sig}; failed to clean up unswitched frontend release dir ${ACTIVE_FRONTEND_RELEASE_DIR}" >&2
    fi
  else
    ACTIVE_FRONTEND_RELEASE_DIR=""
  fi
  if [ -n "${ACTIVE_GATEWAY_IMPORT_TMP:-}" ] && [ -e "${ACTIVE_GATEWAY_IMPORT_TMP}" ]; then
    if rm -rf "${ACTIVE_GATEWAY_IMPORT_TMP}" 2>/dev/null; then
      ACTIVE_GATEWAY_IMPORT_TMP=""
    else
      echo "WARN: deploy interrupted by SIG${sig}; failed to clean up gateway import temp dir ${ACTIVE_GATEWAY_IMPORT_TMP}" >&2
    fi
  else
    ACTIVE_GATEWAY_IMPORT_TMP=""
  fi
  if [ -n "${ACTIVE_GATEWAY_STAGING_DIR:-}" ] && [ -e "${ACTIVE_GATEWAY_STAGING_DIR}" ]; then
    if _is_active_gateway_release_path "${ACTIVE_GATEWAY_STAGING_DIR}"; then
      live_rel=$(readlink "${gw_dir}/.deploy-current" 2>/dev/null || printf '%s' "${ACTIVE_GATEWAY_STAGING_DIR}")
      ACTIVE_GATEWAY_STAGING_DIR=""
      STAGED_GATEWAY_RELEASE_ID=""
      release_deploy_lock || true
      echo "ERROR: deploy interrupted by SIG${sig}; preserving active gateway release ${live_rel}" >&2
      exit "$code"
    fi
    if rm -rf "${ACTIVE_GATEWAY_STAGING_DIR}" 2>/dev/null; then
      ACTIVE_GATEWAY_STAGING_DIR=""
      STAGED_GATEWAY_RELEASE_ID=""
      echo "ERROR: deploy interrupted by SIG${sig}; cleaned up unswitched gateway staging release" >&2
    else
      echo "ERROR: deploy interrupted by SIG${sig}; failed to clean up unswitched gateway staging release ${ACTIVE_GATEWAY_STAGING_DIR}" >&2
    fi
    release_deploy_lock || true
    exit "$code"
  fi
  ACTIVE_GATEWAY_STAGING_DIR=""
  STAGED_GATEWAY_RELEASE_ID=""
  release_deploy_lock || true
  echo "ERROR: deploy interrupted by SIG${sig}" >&2
  exit "$code"
}

build_gateway_staging() {
  local gw_dir="" release_id="" staging="" out="" rc=0 item="" rel_tree=""
  gw_dir=$(_gateway_dir)
  if [ ! -d "$gw_dir" ]; then
    echo "ERROR: gateway directory not found: ${gw_dir}" >&2
    return 1
  fi
  rm -rf "${gw_dir}/.deploy-staging" 2>/dev/null || true
  GATEWAY_RELEASE_SEQ=$(( ${GATEWAY_RELEASE_SEQ:-0} + 1 ))
  release_id="rel-$(date -u +%Y%m%dT%H%M%SZ)-$$-${GATEWAY_RELEASE_SEQ}"
  staging="${gw_dir}/.deploy-releases/${release_id}"
  ACTIVE_GATEWAY_STAGING_DIR="$staging"
  STAGED_GATEWAY_RELEASE_ID="$release_id"
  if ! mkdir -p "$staging"; then
    ACTIVE_GATEWAY_STAGING_DIR=""
    STAGED_GATEWAY_RELEASE_ID=""
    return 1
  fi
  for item in package.json pnpm-lock.yaml pnpm-workspace.yaml build.mjs tsconfig.json src; do
    # Older revisions have no pnpm-workspace.yaml; pnpm 11+ then refuses the install (ERR_PNPM_IGNORED_BUILDS),
    # so staging gets the same build-script approvals the current gateway declares.
    if [ "$item" = "pnpm-workspace.yaml" ] && [ ! -e "${gw_dir}/${item}" ]; then
      if ! printf 'allowBuilds:\n  esbuild: true\n  protobufjs: false\n' > "${staging}/${item}"; then
        echo "ERROR: failed to write default pnpm-workspace.yaml into gateway staging" >&2
        cleanup_gateway_staging || true
        return 1
      fi
      continue
    fi
    if [ ! -e "${gw_dir}/${item}" ]; then
      echo "ERROR: gateway staging source missing: ${gw_dir}/${item}" >&2
      cleanup_gateway_staging || true
      return 1
    fi
    if ! cp -Rp "${gw_dir}/${item}" "${staging}/${item}"; then
      echo "ERROR: failed to copy ${item} into gateway staging" >&2
      cleanup_gateway_staging || true
      return 1
    fi
  done
  out=$(cd "$staging" && pnpm install --frozen-lockfile 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: gateway pnpm install failed in staging" >&2
    [ -n "$out" ] && printf '%s\n' "$out" | tail -20 >&2
    cleanup_gateway_staging || true
    return 1
  fi
  out=$(cd "$staging" && node build.mjs 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: gateway build (node build.mjs) failed in staging" >&2
    [ -n "$out" ] && printf '%s\n' "$out" | tail -20 >&2
    cleanup_gateway_staging || true
    return 1
  fi
  if [ ! -d "${staging}/dist" ] || [ ! -d "${staging}/node_modules" ] || [ ! -f "${staging}/dist/index.mjs" ]; then
    echo "ERROR: gateway staging missing dist/ or node_modules/ after build" >&2
    cleanup_gateway_staging || true
    return 1
  fi
  rel_tree="${TARGET_GATEWAY_TREE:-}"
  if [ -z "$rel_tree" ]; then
    rel_tree=$(git rev-parse HEAD:integrations/dalton-channel-gateway 2>/dev/null || true)
  fi
  if [ -n "$rel_tree" ]; then
    if ! printf '%s\n' "$rel_tree" > "${staging}/.deploy-tree"; then
      echo "ERROR: failed to write release tree hash to ${staging}/.deploy-tree" >&2
      cleanup_gateway_staging || true
      return 1
    fi
  fi
  return 0
}

verify_gateway_release_import() {
  local rel_dir="${1:-}" abs_rel="" tmp_iso="" out="" rc=0
  if [ -z "$rel_dir" ] || [ ! -f "${rel_dir}/dist/index.mjs" ]; then
    echo "ERROR: gateway release bundle missing: ${rel_dir}/dist/index.mjs" >&2
    return 1
  fi
  abs_rel=$(cd "$rel_dir" 2>/dev/null && pwd -P) || {
    echo "ERROR: gateway release directory inaccessible: ${rel_dir}" >&2
    return 1
  }
  tmp_iso=$(mktemp -d "${TMPDIR:-/tmp}/dalton-gw-import.XXXXXX") || {
    echo "ERROR: failed to create isolated temp dir for gateway release import check" >&2
    return 1
  }
  ACTIVE_GATEWAY_IMPORT_TMP="$tmp_iso"
  if ! cp -R "${abs_rel}/dist" "${tmp_iso}/dist"; then
    rm -rf "$tmp_iso"
    ACTIVE_GATEWAY_IMPORT_TMP=""
    return 1
  fi
  if [ -d "${abs_rel}/node_modules" ]; then
    if ! ln -s "${abs_rel}/node_modules" "${tmp_iso}/node_modules"; then
      rm -rf "$tmp_iso"
      ACTIVE_GATEWAY_IMPORT_TMP=""
      return 1
    fi
  fi
  # The entry path goes in through the environment, not argv: the gateway starts itself when
  # process.argv[1] ends with index.mjs, and an import check must only load the module.
  out=$(DALTON_GATEWAY_IMPORT_PATH="${tmp_iso}/dist/index.mjs" node --input-type=module -e 'import { resolve } from "node:path"; import { pathToFileURL } from "node:url"; await import(pathToFileURL(resolve(process.env.DALTON_GATEWAY_IMPORT_PATH)).href);' 2>&1) || rc=$?
  rm -rf "$tmp_iso"
  ACTIVE_GATEWAY_IMPORT_TMP=""
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: gateway release import check failed in ${rel_dir}" >&2
    [ -n "$out" ] && printf '%s\n' "$out" | tail -20 >&2
    return 1
  fi
  return 0
}

switch_gateway_from_staging() {
  local gw_dir="" staging="" release_id="" new_rel="" current_target=""
  local prev_id="" prev_rel="" prev_dir="" prev_tree="" legacy_backup=""
  local dist_is_cur_link=0 nm_is_cur_link=0 swapped_legacy_dist=0 created_dist_link=0 restore_err=0
  local saved_staging="" saved_release_id=""
  gw_dir=$(_gateway_dir)
  GATEWAY_SWITCH_RESTORE_FAILED=0
  staging="${ACTIVE_GATEWAY_STAGING_DIR:-}"
  release_id="${STAGED_GATEWAY_RELEASE_ID:-}"
  if [ -z "$staging" ] && [ -n "$release_id" ]; then
    staging="${gw_dir}/.deploy-releases/${release_id}"
  fi
  if [ -z "$staging" ] || [ -z "$release_id" ] || [ ! -f "${staging}/dist/index.mjs" ] || [ ! -d "${staging}/node_modules" ]; then
    echo "ERROR: gateway staging release missing dist/index.mjs or node_modules/: ${staging:-unset}" >&2
    return 1
  fi
  new_rel=".deploy-releases/${release_id}"
  if ! verify_gateway_release_import "$staging"; then
    return 1
  fi

  if [ -L "${gw_dir}/dist" ] && [ "$(readlink "${gw_dir}/dist" 2>/dev/null)" = ".deploy-current/dist" ]; then
    dist_is_cur_link=1
  fi
  if [ -L "${gw_dir}/node_modules" ] && [ "$(readlink "${gw_dir}/node_modules" 2>/dev/null)" = ".deploy-current/node_modules" ]; then
    nm_is_cur_link=1
  fi
  if [ -L "${gw_dir}/.deploy-current" ]; then
    current_target=$(readlink "${gw_dir}/.deploy-current" 2>/dev/null || true)
  fi

  prev_tree="${PREV_DEPLOYED_GATEWAY_TREE:-${PRE_FF_GATEWAY_TREE:-}}"
  if [ "$dist_is_cur_link" = "1" ] && [ "$nm_is_cur_link" = "1" ] && [ -n "$current_target" ]; then
    if [ -f "${gw_dir}/${current_target}/dist/index.mjs" ] && [ -d "${gw_dir}/${current_target}/node_modules" ]; then
      PREV_GATEWAY_RELEASE_REL="$current_target"
      if [ ! -f "${gw_dir}/${current_target}/.deploy-tree" ] && [ -n "$prev_tree" ]; then
        printf '%s\n' "$prev_tree" > "${gw_dir}/${current_target}/.deploy-tree" 2>/dev/null || true
      fi
    fi
  else
    if [ -n "$current_target" ] && [ -f "${gw_dir}/${current_target}/dist/index.mjs" ] && [ -d "${gw_dir}/${current_target}/node_modules" ]; then
      PREV_GATEWAY_RELEASE_REL="$current_target"
      if [ ! -f "${gw_dir}/${current_target}/.deploy-tree" ] && [ -n "$prev_tree" ]; then
        printf '%s\n' "$prev_tree" > "${gw_dir}/${current_target}/.deploy-tree" 2>/dev/null || true
      fi
    elif [ -d "${gw_dir}/dist" ] || [ -d "${gw_dir}/node_modules" ]; then
      prev_id="prev-$(date -u +%Y%m%dT%H%M%SZ)-$$-${GATEWAY_RELEASE_SEQ:-1}"
      prev_rel=".deploy-releases/${prev_id}"
      prev_dir="${gw_dir}/${prev_rel}"
      mkdir -p "$prev_dir" || return 1
      if [ -d "${gw_dir}/dist" ]; then
        if ! cp -Rp "${gw_dir}/dist" "${prev_dir}/dist"; then
          rm -rf "$prev_dir"
          return 1
        fi
      fi
      if [ -d "${gw_dir}/node_modules" ]; then
        if ! cp -Rp "${gw_dir}/node_modules" "${prev_dir}/node_modules"; then
          rm -rf "$prev_dir"
          return 1
        fi
      fi
      if [ ! -f "${prev_dir}/dist/index.mjs" ] || [ ! -d "${prev_dir}/node_modules" ]; then
        rm -rf "$prev_dir"
        return 1
      fi
      if [ -n "$prev_tree" ]; then
        if ! printf '%s\n' "$prev_tree" > "${prev_dir}/.deploy-tree"; then
          rm -rf "$prev_dir"
          return 1
        fi
      fi
      if ! _atomic_replace_symlink "$prev_rel" "${gw_dir}/.deploy-current"; then
        rm -rf "$prev_dir"
        return 1
      fi
      PREV_GATEWAY_RELEASE_REL="$prev_rel"
    else
      if ! _atomic_replace_symlink "$new_rel" "${gw_dir}/.deploy-current"; then
        return 1
      fi
    fi

    legacy_backup="${gw_dir}/.deploy-legacy-backup-$$"
    rm -rf "$legacy_backup" || return 1
    mkdir -p "$legacy_backup" || return 1
    if [ "$dist_is_cur_link" != "1" ]; then
      if [ -e "${gw_dir}/dist" ] || [ -L "${gw_dir}/dist" ]; then
        if ! ln -s ".deploy-current/dist" "${legacy_backup}/dist"; then
          rm -rf "$legacy_backup" 2>/dev/null || true
          return 1
        fi
        if ! _atomic_swap_paths "${legacy_backup}/dist" "${gw_dir}/dist"; then
          rm -rf "$legacy_backup" 2>/dev/null || true
          return 1
        fi
        swapped_legacy_dist=1
      else
        if ! _atomic_replace_symlink ".deploy-current/dist" "${gw_dir}/dist"; then
          rm -rf "$legacy_backup" 2>/dev/null || true
          return 1
        fi
        created_dist_link=1
      fi
    fi

    if [ "$nm_is_cur_link" != "1" ]; then
      if [ -e "${gw_dir}/node_modules" ] || [ -L "${gw_dir}/node_modules" ]; then
        if ! ln -s ".deploy-current/node_modules" "${legacy_backup}/node_modules" \
           || ! _atomic_swap_paths "${legacy_backup}/node_modules" "${gw_dir}/node_modules"; then
          if [ "$swapped_legacy_dist" = "1" ]; then
            if ! _atomic_swap_paths "${legacy_backup}/dist" "${gw_dir}/dist"; then
              restore_err=1
            fi
          elif [ "$created_dist_link" = "1" ]; then
            rm -f "${gw_dir}/dist" || restore_err=1
          fi
          if [ "$restore_err" -ne 0 ]; then
            GATEWAY_SWITCH_RESTORE_FAILED=1
            echo "ERROR: failed to restore ${gw_dir}/dist from ${legacy_backup}/dist after node_modules swap failure; preserving backup directories" >&2
          else
            rm -rf "$legacy_backup" 2>/dev/null || true
          fi
          return 1
        fi
      else
        if ! _atomic_replace_symlink ".deploy-current/node_modules" "${gw_dir}/node_modules"; then
          if [ "$swapped_legacy_dist" = "1" ]; then
            if ! _atomic_swap_paths "${legacy_backup}/dist" "${gw_dir}/dist"; then
              restore_err=1
            fi
          elif [ "$created_dist_link" = "1" ]; then
            rm -f "${gw_dir}/dist" || restore_err=1
          fi
          if [ "$restore_err" -ne 0 ]; then
            GATEWAY_SWITCH_RESTORE_FAILED=1
            echo "ERROR: failed to restore ${gw_dir}/dist from ${legacy_backup}/dist; preserving backup directories" >&2
          else
            rm -rf "$legacy_backup" 2>/dev/null || true
          fi
          return 1
        fi
      fi
    fi

    if [ -L "${gw_dir}/dist" ] && [ -L "${gw_dir}/node_modules" ] \
       && [ -f "${gw_dir}/dist/index.mjs" ] && [ -d "${gw_dir}/node_modules" ]; then
      rm -rf "$legacy_backup"
    fi
  fi

  saved_staging="$ACTIVE_GATEWAY_STAGING_DIR"
  saved_release_id="$STAGED_GATEWAY_RELEASE_ID"
  ACTIVE_GATEWAY_STAGING_DIR=""
  STAGED_GATEWAY_RELEASE_ID=""
  if ! _atomic_replace_symlink "$new_rel" "${gw_dir}/.deploy-current"; then
    ACTIVE_GATEWAY_STAGING_DIR="$saved_staging"
    STAGED_GATEWAY_RELEASE_ID="$saved_release_id"
    return 1
  fi
  CURRENT_GATEWAY_RELEASE_REL="$new_rel"
  GATEWAY_SWITCHED=1
  return 0
}

prune_old_gateway_releases() {
  local gw_dir="" current_rel="" prev_rel="" current_name="" prev_name="" d="" base=""
  gw_dir=$(_gateway_dir)
  if [ -L "${gw_dir}/.deploy-current" ]; then
    current_rel=$(readlink "${gw_dir}/.deploy-current" 2>/dev/null || true)
  fi
  current_rel="${current_rel:-${CURRENT_GATEWAY_RELEASE_REL:-}}"
  prev_rel="${PREV_GATEWAY_RELEASE_REL:-}"
  current_name="${current_rel#.deploy-releases/}"
  prev_name="${prev_rel#.deploy-releases/}"
  [ -n "$current_name" ] || return 0
  if [ -d "${gw_dir}/.deploy-releases" ]; then
    for d in "${gw_dir}/.deploy-releases"/*; do
      [ -e "$d" ] || continue
      base=$(basename "$d")
      if [ "$base" = "$current_name" ] || { [ -n "$prev_name" ] && [ "$base" = "$prev_name" ]; }; then
        continue
      fi
      rm -rf "$d"
    done
  fi
  rm -rf "${gw_dir}/.deploy-staging" "${gw_dir}/.deploy-prev" 2>/dev/null || true
  return 0
}

restore_gateway_from_prev() {
  local gw_dir="" prev_rel="" current_rel=""
  local dist_is_cur_link=0 nm_is_cur_link=0 tmp_bad="" moved_new_dist=0 moved_new_nm=0 restore_err=0
  local rel_tree_val="" tree_to_record=""
  gw_dir=$(_gateway_dir)
  ACTUAL_RESTORED_GATEWAY_TREE=""
  if [ -L "${gw_dir}/.deploy-current" ]; then
    current_rel=$(readlink "${gw_dir}/.deploy-current" 2>/dev/null || true)
  fi
  if [ -n "${PREV_GATEWAY_RELEASE_REL:-}" ] && [ "${PREV_GATEWAY_RELEASE_REL}" != "$current_rel" ]; then
    prev_rel="$PREV_GATEWAY_RELEASE_REL"
  fi
  if [ -z "$prev_rel" ] || [ ! -f "${gw_dir}/${prev_rel}/dist/index.mjs" ] || [ ! -d "${gw_dir}/${prev_rel}/node_modules" ]; then
    echo "WARN: no valid previous gateway release (with dist/index.mjs and node_modules) confirmed for rollback; cannot restore previous gateway" >&2
    return 1
  fi
  if ! verify_gateway_release_import "${gw_dir}/${prev_rel}"; then
    echo "ERROR: previous gateway release (${prev_rel}) failed pre-restore import check; refusing rollback switch" >&2
    return 1
  fi

  if [ -L "${gw_dir}/dist" ] && [ "$(readlink "${gw_dir}/dist" 2>/dev/null)" = ".deploy-current/dist" ]; then
    dist_is_cur_link=1
  fi
  if [ -L "${gw_dir}/node_modules" ] && [ "$(readlink "${gw_dir}/node_modules" 2>/dev/null)" = ".deploy-current/node_modules" ]; then
    nm_is_cur_link=1
  fi

  if [ "$dist_is_cur_link" != "1" ] || [ "$nm_is_cur_link" != "1" ]; then
    tmp_bad="${gw_dir}/.deploy-failed.$$"
    rm -rf "$tmp_bad" || return 1
    mkdir -p "$tmp_bad" || return 1
    if [ "$dist_is_cur_link" != "1" ]; then
      if [ -e "${gw_dir}/dist" ] || [ -L "${gw_dir}/dist" ]; then
        if ! mv "${gw_dir}/dist" "${tmp_bad}/dist"; then
          rmdir "$tmp_bad" 2>/dev/null || true
          return 1
        fi
        moved_new_dist=1
      fi
      if ! _atomic_replace_symlink ".deploy-current/dist" "${gw_dir}/dist"; then
        if [ "$moved_new_dist" = "1" ]; then
          if ! mv "${tmp_bad}/dist" "${gw_dir}/dist"; then
            echo "ERROR: failed to restore ${gw_dir}/dist from ${tmp_bad}/dist; preserving ${tmp_bad}" >&2
            return 1
          fi
        fi
        rmdir "$tmp_bad" 2>/dev/null || true
        return 1
      fi
    fi
    if [ "$nm_is_cur_link" != "1" ]; then
      if [ -e "${gw_dir}/node_modules" ] || [ -L "${gw_dir}/node_modules" ]; then
        if ! mv "${gw_dir}/node_modules" "${tmp_bad}/node_modules"; then
          if [ "$moved_new_dist" = "1" ]; then
            rm -f "${gw_dir}/dist" || restore_err=1
            if ! mv "${tmp_bad}/dist" "${gw_dir}/dist"; then
              restore_err=1
            fi
          fi
          if [ "$restore_err" -ne 0 ]; then
            echo "ERROR: failed to restore ${gw_dir}/dist from ${tmp_bad}/dist; preserving ${tmp_bad}" >&2
          else
            rmdir "$tmp_bad" 2>/dev/null || true
          fi
          return 1
        fi
        moved_new_nm=1
      fi
      if ! _atomic_replace_symlink ".deploy-current/node_modules" "${gw_dir}/node_modules"; then
        if [ "$moved_new_nm" = "1" ]; then
          if ! mv "${tmp_bad}/node_modules" "${gw_dir}/node_modules"; then
            restore_err=1
          fi
        fi
        if [ "$moved_new_dist" = "1" ]; then
          rm -f "${gw_dir}/dist" || restore_err=1
          if ! mv "${tmp_bad}/dist" "${gw_dir}/dist"; then
            restore_err=1
          fi
        fi
        if [ "$restore_err" -ne 0 ]; then
          echo "ERROR: failed to restore directories from ${tmp_bad}; preserving ${tmp_bad}" >&2
        else
          rmdir "$tmp_bad" 2>/dev/null || true
        fi
        return 1
      fi
    fi
  fi

  if ! _atomic_replace_symlink "$prev_rel" "${gw_dir}/.deploy-current"; then
    if [ -n "$tmp_bad" ] && [ -d "$tmp_bad" ]; then
      if [ "$moved_new_nm" = "1" ]; then
        rm -f "${gw_dir}/node_modules" || restore_err=1
        if ! mv "${tmp_bad}/node_modules" "${gw_dir}/node_modules"; then
          restore_err=1
        fi
      fi
      if [ "$moved_new_dist" = "1" ]; then
        rm -f "${gw_dir}/dist" || restore_err=1
        if ! mv "${tmp_bad}/dist" "${gw_dir}/dist"; then
          restore_err=1
        fi
      fi
      if [ "$restore_err" -ne 0 ]; then
        echo "ERROR: failed to restore directories from ${tmp_bad} after .deploy-current switch failure; preserving ${tmp_bad}" >&2
      else
        rmdir "$tmp_bad" 2>/dev/null || true
      fi
    fi
    return 1
  fi

  rel_tree_val=$(_read_release_tree_file "${gw_dir}/${prev_rel}/.deploy-tree" || true)
  tree_to_record="${rel_tree_val:-${PREV_DEPLOYED_GATEWAY_TREE:-${PRE_FF_GATEWAY_TREE:-}}}"
  if [ -z "$tree_to_record" ] && [ -n "${PRE_FF:-}" ]; then
    if ! tree_to_record=$(git -C "$REPO_ROOT" rev-parse "${PRE_FF}:integrations/dalton-channel-gateway" 2>/dev/null) || [ -z "$tree_to_record" ]; then
      _write_gateway_pending "${TARGET_GATEWAY_TREE:-pending}" 2>/dev/null || true
      echo "ERROR: failed to resolve PRE_FF gateway tree hash during rollback" >&2
      return 1
    fi
  fi
  if [ -z "$tree_to_record" ] || ! _write_gateway_deployed_tree "$tree_to_record"; then
    _write_gateway_pending "${TARGET_GATEWAY_TREE:-pending}" 2>/dev/null || true
    echo "ERROR: failed to write restored gateway tree hash to $(_gateway_deployed_tree_file)" >&2
    return 1
  fi
  ACTUAL_RESTORED_GATEWAY_TREE="$tree_to_record"

  [ -n "$tmp_bad" ] && [ -d "$tmp_bad" ] && rm -rf "$tmp_bad"
  if [ -n "$current_rel" ] && [ "$current_rel" != "$prev_rel" ]; then
    case "$current_rel" in
      .deploy-releases/*) rm -rf "${gw_dir}/${current_rel}" ;;
    esac
  fi
  CURRENT_GATEWAY_RELEASE_REL="$prev_rel"
  PREV_GATEWAY_RELEASE_REL=""
  return 0
}

manual_switch_gateway_release() {
  local rel_path="${1:-}" gw_dir="" backup_dir="" rel_tree=""
  local orig_prev_current="" orig_had_prev_current=0
  local dist_was_real_dir=0 nm_was_real_dir=0
  local dist_was_link=0 nm_was_link=0 orig_dist_link="" orig_nm_link=""
  local swapped_dist=0 swapped_nm=0 replaced_dist_link=0 replaced_nm_link=0
  local temp_prev_id="" temp_prev_rel="" temp_prev_dir=""
  gw_dir=$(_gateway_dir)
  case "$rel_path" in
    .deploy-releases/*) ;;
    *)
      echo "ERROR: manual_switch_gateway_release requires a path under .deploy-releases/ (got '${rel_path}')" >&2
      return 1
      ;;
  esac
  case "$rel_path" in
    *..*|*/.*)
      echo "ERROR: manual_switch_gateway_release rejects traversal in '${rel_path}'" >&2
      return 1
      ;;
  esac
  if [ ! -f "${gw_dir}/${rel_path}/dist/index.mjs" ] || [ ! -d "${gw_dir}/${rel_path}/node_modules" ]; then
    echo "ERROR: manual_switch_gateway_release: target release incomplete: ${gw_dir}/${rel_path}" >&2
    return 1
  fi
  if ! verify_gateway_release_import "${gw_dir}/${rel_path}"; then
    return 1
  fi

  if [ -L "${gw_dir}/.deploy-current" ]; then
    orig_prev_current=$(readlink "${gw_dir}/.deploy-current" 2>/dev/null || true)
    [ -n "$orig_prev_current" ] && orig_had_prev_current=1
  fi
  if [ -L "${gw_dir}/dist" ]; then
    dist_was_link=1
    orig_dist_link=$(readlink "${gw_dir}/dist" 2>/dev/null || true)
  elif [ -d "${gw_dir}/dist" ]; then
    dist_was_real_dir=1
  fi
  if [ -L "${gw_dir}/node_modules" ]; then
    nm_was_link=1
    orig_nm_link=$(readlink "${gw_dir}/node_modules" 2>/dev/null || true)
  elif [ -d "${gw_dir}/node_modules" ]; then
    nm_was_real_dir=1
  fi

  _rollback_manual_switch() {
    local rollback_ok=1
    if [ "$orig_had_prev_current" = "1" ]; then
      if ! _atomic_replace_symlink "$orig_prev_current" "${gw_dir}/.deploy-current"; then
        echo "ERROR: manual_switch_gateway_release rollback: failed to restore .deploy-current to ${orig_prev_current}" >&2
        rollback_ok=0
      fi
    elif [ -n "$temp_prev_rel" ] && [ -d "${gw_dir}/${temp_prev_rel}" ]; then
      if ! _atomic_replace_symlink "$temp_prev_rel" "${gw_dir}/.deploy-current"; then
        echo "ERROR: manual_switch_gateway_release rollback: failed to restore .deploy-current to ${temp_prev_rel}" >&2
        rollback_ok=0
      fi
    fi
    if [ "$swapped_nm" = "1" ]; then
      if [ ! -e "${backup_dir}/node_modules" ] || ! _atomic_swap_paths "${backup_dir}/node_modules" "${gw_dir}/node_modules"; then
        echo "ERROR: manual_switch_gateway_release rollback: failed to swap back ${gw_dir}/node_modules from ${backup_dir}/node_modules; preserving ${backup_dir}" >&2
        rollback_ok=0
      fi
    elif [ "$replaced_nm_link" = "1" ]; then
      if [ "$nm_was_link" = "1" ] && [ -n "$orig_nm_link" ]; then
        if ! _atomic_replace_symlink "$orig_nm_link" "${gw_dir}/node_modules"; then
          echo "ERROR: manual_switch_gateway_release rollback: failed to restore ${gw_dir}/node_modules symlink" >&2
          rollback_ok=0
        fi
      else
        if ! rm -f "${gw_dir}/node_modules"; then
          echo "ERROR: manual_switch_gateway_release rollback: failed to remove temporary ${gw_dir}/node_modules symlink" >&2
          rollback_ok=0
        fi
      fi
    fi
    if [ "$swapped_dist" = "1" ]; then
      if [ ! -e "${backup_dir}/dist" ] || ! _atomic_swap_paths "${backup_dir}/dist" "${gw_dir}/dist"; then
        echo "ERROR: manual_switch_gateway_release rollback: failed to swap back ${gw_dir}/dist from ${backup_dir}/dist; preserving ${backup_dir}" >&2
        rollback_ok=0
      fi
    elif [ "$replaced_dist_link" = "1" ]; then
      if [ "$dist_was_link" = "1" ] && [ -n "$orig_dist_link" ]; then
        if ! _atomic_replace_symlink "$orig_dist_link" "${gw_dir}/dist"; then
          echo "ERROR: manual_switch_gateway_release rollback: failed to restore ${gw_dir}/dist symlink" >&2
          rollback_ok=0
        fi
      else
        if ! rm -f "${gw_dir}/dist"; then
          echo "ERROR: manual_switch_gateway_release rollback: failed to remove temporary ${gw_dir}/dist symlink" >&2
          rollback_ok=0
        fi
      fi
    fi
    if [ "$orig_had_prev_current" != "1" ] && [ "$rollback_ok" = "1" ]; then
      rm -f "${gw_dir}/.deploy-current" 2>/dev/null || true
      [ -n "$temp_prev_dir" ] && [ -d "$temp_prev_dir" ] && rm -rf "$temp_prev_dir" 2>/dev/null || true
    fi
    if [ "$rollback_ok" = "1" ]; then
      [ -n "$backup_dir" ] && [ -d "$backup_dir" ] && rm -rf "$backup_dir" 2>/dev/null || true
    fi
  }

  backup_dir="${gw_dir}/.deploy-legacy-backup-$$"
  rm -rf "$backup_dir" || return 1

  # Step 3: If either top-level dist or node_modules is a real directory, back up / swap before moving .deploy-current to target
  if [ "$dist_was_real_dir" = "1" ] || [ "$nm_was_real_dir" = "1" ]; then
    if [ "$orig_had_prev_current" != "1" ] && [ -f "${gw_dir}/dist/index.mjs" ] && [ -d "${gw_dir}/node_modules" ]; then
      temp_prev_id="prev-manual-$(date -u +%Y%m%dT%H%M%SZ)-$$"
      temp_prev_rel=".deploy-releases/${temp_prev_id}"
      temp_prev_dir="${gw_dir}/${temp_prev_rel}"
      mkdir -p "$temp_prev_dir" || return 1
      if ! cp -Rp "${gw_dir}/dist" "${temp_prev_dir}/dist" \
         || ! cp -Rp "${gw_dir}/node_modules" "${temp_prev_dir}/node_modules"; then
        rm -rf "$temp_prev_dir"
        return 1
      fi
      if ! _atomic_replace_symlink "$temp_prev_rel" "${gw_dir}/.deploy-current"; then
        rm -rf "$temp_prev_dir"
        return 1
      fi
    fi
    mkdir -p "$backup_dir" || {
      [ -n "$temp_prev_dir" ] && rm -rf "$temp_prev_dir" 2>/dev/null || true
      [ "$orig_had_prev_current" != "1" ] && rm -f "${gw_dir}/.deploy-current" 2>/dev/null || true
      return 1
    }
    if [ "$dist_was_real_dir" = "1" ]; then
      if ! ln -s ".deploy-current/dist" "${backup_dir}/dist" \
         || ! _atomic_swap_paths "${backup_dir}/dist" "${gw_dir}/dist"; then
        _rollback_manual_switch
        unset -f _rollback_manual_switch
        return 1
      fi
      swapped_dist=1
    fi
    if [ "$nm_was_real_dir" = "1" ]; then
      if ! ln -s ".deploy-current/node_modules" "${backup_dir}/node_modules" \
         || ! _atomic_swap_paths "${backup_dir}/node_modules" "${gw_dir}/node_modules"; then
        _rollback_manual_switch
        unset -f _rollback_manual_switch
        return 1
      fi
      swapped_nm=1
    fi
  fi

  # Step 4: Repair any missing or wrong top-level symlink BEFORE switching .deploy-current to target rel_path
  if [ "$dist_was_real_dir" != "1" ]; then
    if [ "$dist_was_link" != "1" ] || [ "$orig_dist_link" != ".deploy-current/dist" ]; then
      if ! _atomic_replace_symlink ".deploy-current/dist" "${gw_dir}/dist"; then
        _rollback_manual_switch
        unset -f _rollback_manual_switch
        return 1
      fi
      replaced_dist_link=1
    fi
  fi
  if [ "$nm_was_real_dir" != "1" ]; then
    if [ "$nm_was_link" != "1" ] || [ "$orig_nm_link" != ".deploy-current/node_modules" ]; then
      if ! _atomic_replace_symlink ".deploy-current/node_modules" "${gw_dir}/node_modules"; then
        _rollback_manual_switch
        unset -f _rollback_manual_switch
        return 1
      fi
      replaced_nm_link=1
    fi
  fi

  # Now atomically switch .deploy-current to target release
  if ! _atomic_replace_symlink "$rel_path" "${gw_dir}/.deploy-current"; then
    _rollback_manual_switch
    unset -f _rollback_manual_switch
    return 1
  fi

  # Step 5: Verify production import; if it fails, roll back .deploy-current and top-level paths
  if ! verify_gateway_production_import; then
    _rollback_manual_switch
    unset -f _rollback_manual_switch
    return 1
  fi

  [ -d "$backup_dir" ] && rm -rf "$backup_dir"
  unset -f _rollback_manual_switch

  # Step 6: Update .deploy-deployed-tree only after everything passes; never clear .deploy-gateway-pending here
  rel_tree=$(_read_release_tree_file "${gw_dir}/${rel_path}/.deploy-tree" || true)
  if [ -n "$rel_tree" ]; then
    if ! _write_gateway_deployed_tree "$rel_tree"; then
      echo "ERROR: manual_switch_gateway_release: failed to write $(_gateway_deployed_tree_file)" >&2
      return 1
    fi
  fi
  CURRENT_GATEWAY_RELEASE_REL="$rel_path"
  return 0
}

manual_rebuild_gateway_release() {
  local head_gw_tree=""
  cd "$REPO_ROOT" || return 1
  head_gw_tree=$(git rev-parse HEAD:integrations/dalton-channel-gateway 2>/dev/null) || {
    echo "ERROR: manual_rebuild_gateway_release: failed to resolve HEAD:integrations/dalton-channel-gateway" >&2
    return 1
  }
  TARGET_GATEWAY_TREE="$head_gw_tree"
  PREV_DEPLOYED_GATEWAY_TREE=$(_read_gateway_deployed_tree || true)
  if ! build_gateway_staging; then
    return 1
  fi
  if ! verify_gateway_release_import "$ACTIVE_GATEWAY_STAGING_DIR"; then
    cleanup_gateway_staging || true
    return 1
  fi
  if ! switch_gateway_from_staging; then
    return 1
  fi
  if ! verify_gateway_production_import; then
    return 1
  fi
  if ! _write_gateway_deployed_tree "$head_gw_tree"; then
    echo "ERROR: manual_rebuild_gateway_release: failed to write $(_gateway_deployed_tree_file)" >&2
    return 1
  fi
  if ! _clear_gateway_pending; then
    echo "ERROR: manual_rebuild_gateway_release: failed to clear $(_gateway_pending_file)" >&2
    return 1
  fi
  return 0
}

verify_gateway_production_import() {
  local gw_dir="" out="" rc=0
  gw_dir=$(_gateway_dir)
  if [ ! -f "${gw_dir}/dist/index.mjs" ]; then
    echo "ERROR: gateway production bundle missing: ${gw_dir}/dist/index.mjs" >&2
    return 1
  fi
  out=$(cd "$gw_dir" && node --input-type=module -e 'import { resolve } from "node:path"; import { pathToFileURL } from "node:url"; await import(pathToFileURL(resolve("dist/index.mjs")).href);' 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: gateway production import check failed" >&2
    [ -n "$out" ] && printf '%s\n' "$out" | tail -20 >&2
    return 1
  fi
  return 0
}

_read_gateway_service_token() {
  local _xtrace_was_on=0 rc=0
  case "$-" in
    *x*) _xtrace_was_on=1; set +x ;;
  esac
  local env_file="${GATEWAY_ENV_FILE:-$HOME/.config/dalton/channel-gateway.env}"
  if [ ! -f "$env_file" ] || [ ! -r "$env_file" ]; then
    [ "$_xtrace_was_on" = "1" ] && set -x
    return 1
  fi
  awk '
    {
      line = $0
      sub(/\r$/, "", line)
      sub(/^[[:space:]]+/, "", line)
      sub(/[[:space:]]+$/, "", line)
      if (line == "" || line ~ /^#/) next
      if (line ~ /^export[[:space:]]+/) {
        sub(/^export[[:space:]]+/, "", line)
      }
      idx = index(line, "=")
      if (idx <= 1) next
      key = substr(line, 1, idx - 1)
      sub(/[[:space:]]+$/, "", key)
      if (key != "INTERNAL_SERVICE_TOKEN") next
      val = substr(line, idx + 1)
      sub(/^[[:space:]]+/, "", val)
      sub(/[[:space:]]+$/, "", val)
      q = sprintf("%c", 39)
      if (length(val) >= 2 && ((substr(val, 1, 1) == "\"" && substr(val, length(val), 1) == "\"") || (substr(val, 1, 1) == q && substr(val, length(val), 1) == q))) {
        val = substr(val, 2, length(val) - 2)
      }
      found = 1
      token = val
    }
    END {
      if (found && token != "") printf "%s", token
      else exit 1
    }
  ' "$env_file" || rc=$?
  [ "$_xtrace_was_on" = "1" ] && set -x
  return "$rc"
}

_gateway_health_body_ok() {
  local body="${1:-}" py=""
  [ -n "$body" ] || return 1
  py=$(_resolve_deploy_python) || return 1
  "$py" -c 'import json, sys
try:
    data = json.loads(sys.stdin.read())
except Exception:
    sys.exit(1)
if not isinstance(data, dict):
    sys.exit(1)
if "ready" in data:
    ok = data["ready"] is True
else:
    ok = data.get("ok") is True
sys.exit(0 if ok else 1)' <<<"$body"
}

_gateway_probe_with_token() {
  local max_time="${1:-5}" token="${2:-}" body=""
  [ -n "$token" ] || return 1
  body=$(curl -q -sf --max-time "$max_time" --noproxy '*' -H @- "http://127.0.0.1:${GATEWAY_PORT}/health" <<<"X-Service-Token: ${token}" 2>/dev/null) || return 1
  _gateway_health_body_ok "$body"
}

gateway_ready() {
  local _xtrace_was_on=0
  case "$-" in
    *x*) _xtrace_was_on=1; set +x ;;
  esac
  local max_time="${1:-5}" token="${2:-}" rc=1
  if [ -z "$token" ]; then
    token=$(_read_gateway_service_token) || {
      [ "$_xtrace_was_on" = "1" ] && set -x
      return 1
    }
  fi
  if _gateway_probe_with_token "$max_time" "$token"; then
    rc=0
  fi
  token=""
  [ "$_xtrace_was_on" = "1" ] && set -x
  return "$rc"
}

wait_gateway_health() {
  local _xtrace_was_on=0
  case "$-" in
    *x*) _xtrace_was_on=1; set +x ;;
  esac
  local timeout="${1:-$GATEWAY_WAIT_TIMEOUT}"
  local ok=0 deadline now remaining max_time sleep_s token=""
  if ! token=$(_read_gateway_service_token); then
    echo "ERROR: cannot read INTERNAL_SERVICE_TOKEN from ${GATEWAY_ENV_FILE}" >&2
    token=""
    [ "$_xtrace_was_on" = "1" ] && set -x
    return 1
  fi
  deadline=$(( $(date +%s) + timeout ))
  while :; do
    now=$(date +%s)
    remaining=$(( deadline - now ))
    if [ "$remaining" -le 0 ]; then
      break
    fi
    max_time=5
    if [ "$remaining" -lt "$max_time" ]; then
      max_time="$remaining"
    fi
    if _gateway_probe_with_token "$max_time" "$token"; then
      if [ "$(date +%s)" -lt "$deadline" ]; then
        ok=1
      fi
      break
    fi
    now=$(date +%s)
    remaining=$(( deadline - now ))
    if [ "$remaining" -le 0 ]; then
      break
    fi
    sleep_s="${GATEWAY_POLL_INTERVAL}"
    if [ "$sleep_s" -gt 0 ] 2>/dev/null; then
      if [ "$remaining" -lt "$sleep_s" ]; then
        sleep_s="$remaining"
      fi
      sleep "$sleep_s"
    fi
  done
  token=""
  if [ "$ok" = "1" ]; then
    echo "gateway health ok"
    [ "$_xtrace_was_on" = "1" ] && set -x
    return 0
  fi
  echo "gateway health: failed (not ready within ${timeout}s)"
  [ "$_xtrace_was_on" = "1" ] && set -x
  return 1
}

_print_gateway_manual_restore_steps() {
  local gw_dir="" cur_head=""
  gw_dir=$(_gateway_dir)
  cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
  echo "网关人工恢复步骤（在部署检出根目录执行: cd ${REPO_ROOT}）："
  echo "  # 当前网关磁盘状态: $(_describe_gateway_disk_state 0 "$cur_head")"
  echo "  # 1) 若 ${gw_dir}/.deploy-releases/ 下存在上一版完整 release（同时含 dist/index.mjs 与 node_modules），执行以下辅助函数校验导入、修复顶层链接并原子切换 .deploy-current（切勿先执行 rm -rf）："
  echo "  ls -la integrations/dalton-channel-gateway/.deploy-releases"
  echo "  DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && manual_switch_gateway_release \".deploy-releases/<上一版release目录名>\"'"
  echo "  # 2) 或在当前检出代码上使用独立 release 目录重建并原子切换（绝不在网关根目录直接跑 node build.mjs）："
  echo "  DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && manual_rebuild_gateway_release'"
  echo "  # 确认产物就绪后再重启网关："
  echo "  launchctl kickstart -k gui/\$(id -u)/com.dalton.channel-gateway"
}

_print_gateway_rollback_instructions() {
  local cur_head="$1" restore_status="$2" kickstart_status="$3" health_status="$4" disk_version="$5"
  local target_ref="${TARGET:-$cur_head}"
  echo "GATEWAY ROLLBACK — 渠道网关部署失败，仅处理网关回滚，后端与数据库保持新版未回退。"
  echo "部署记录: PRE_FF=${PRE_FF:-未知}, TARGET=${target_ref:-未知}, HEAD=${cur_head}, backend_touched=${BACKEND_TOUCHED}, crawler_touched=${CRAWLER_TOUCHED}, edge_touched=${EDGE_TOUCHED}, bridge_touched=${BRIDGE_TOUCHED}, gateway_switched=${GATEWAY_SWITCHED}"
  echo "文件换回: ${restore_status}"
  echo "进程重启: ${kickstart_status}"
  echo "健康复查: ${health_status}"
  echo "磁盘当前版本: ${disk_version}"
  echo "注意: 后端已是新版（HEAD=${cur_head}），若网关运行上一版 dist 与 node_modules，新后端与旧网关之间可能存在接口不匹配。"
  echo "人工处理说明（以下命令都在部署检出根目录执行: cd ${REPO_ROOT}）："
  echo "1. 查看网关日志与 launchd 状态："
  echo "     tail -n 100 ~/Library/Logs/Dalton/dalton-channel-gateway.err.log ~/Library/Logs/Dalton/dalton-channel-gateway.out.log"
  echo "     launchctl list | grep com.dalton.channel-gateway"
  echo "2. 修复网关问题并推送到 origin/dalton/main 后，重新构建并重启网关："
  echo "     bash scripts/dalton/ops/prod/deploy_main.sh --gateway"
  echo "3. 如文件换回失败需手工恢复网关产物，可先检查 ${REPO_ROOT}/integrations/dalton-channel-gateway/.deploy-releases/ 中的可恢复完整 release 并调用辅助函数修复顶层链接与原子切换，或在独立目录重建："
  echo "     DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && manual_switch_gateway_release \".deploy-releases/<上一版release目录名>\"'"
  echo "     # 或在独立 release 目录重建并原子切换："
  echo "     DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && manual_rebuild_gateway_release'"
  echo "     launchctl kickstart -k gui/\$(id -u)/com.dalton.channel-gateway"
  echo "4. 如需核对后端与网关健康接口："
  echo "     curl -fsS -m3 http://127.0.0.1:8000/health"
  echo "     # 网关 /health 需携带 ${GATEWAY_ENV_FILE} 中的 INTERNAL_SERVICE_TOKEN 作为 X-Service-Token 请求 http://127.0.0.1:${GATEWAY_PORT}/health"
}

rollback_gateway_only() {
  local need_restart="${1:-1}"
  local cur_head="" restore_ok=0 kickstart_ok=0 health_ok=0
  local restore_status="" kickstart_status="" health_status="" disk_version=""
  cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
  echo "$(date '+%T') rolling back channel gateway to previous dist and node_modules"
  if restore_gateway_from_prev; then
    restore_ok=1
    restore_status="成功（已将 .deploy-current 原子切换回上一版 release）"
  else
    echo "WARN: failed to restore gateway to previous release; skipping gateway kickstart" >&2
    restore_status="失败（未能将网关切换回上一版 release）"
  fi
  if [ "$need_restart" = "0" ]; then
    kickstart_status="跳过（导入校验在重启前失败，原网关进程未重启）"
    health_status="跳过（原网关进程未重启）"
  elif [ "$restore_ok" = "1" ]; then
    if launchctl kickstart -k "gui/$(id -u)/com.dalton.channel-gateway" >/dev/null 2>&1; then
      kickstart_ok=1
      kickstart_status="成功"
    else
      echo "WARN: gateway rollback kickstart failed" >&2
      kickstart_status="失败"
    fi
    if [ "$kickstart_ok" = "1" ]; then
      if wait_gateway_health; then
        health_ok=1
        health_status="成功"
      else
        echo "WARN: gateway rollback health check failed" >&2
        health_status="失败"
      fi
    else
      health_status="跳过（进程重启失败）"
    fi
  else
    kickstart_status="跳过（文件换回失败，未重启网关）"
    health_status="跳过（文件换回失败）"
  fi
  disk_version=$(_describe_gateway_disk_state "$restore_ok" "$cur_head")
  _print_gateway_rollback_instructions "$cur_head" "$restore_status" "$kickstart_status" "$health_status" "$disk_version"
  return 1
}

run_public_edge_check() {
  local expected_build="${1:-}" dist_dir="${2:-}" expected_release="${3:-}" out="" rc=0 out_file=""
  local -a check_args=()
  if [ -n "$expected_build" ]; then
    check_args+=(--expected-build "$expected_build")
  fi
  if [ -n "$expected_release" ]; then
    check_args+=(--expected-release "$expected_release")
  fi
  if [ -n "$dist_dir" ]; then
    check_args+=(--dist-dir "$dist_dir")
  elif [ -L "$(_frontend_current_link)" ]; then
    check_args+=(--dist-dir "$(_frontend_current_link)")
  fi
  out_file=$(mktemp "${TMPDIR:-/tmp}/dalton-edge-check.XXXXXX") || return 1
  ACTIVE_STEP_OUT_FILE="$out_file"
  if [ "${#check_args[@]}" -gt 0 ]; then
    _spawn_in_new_pgid "$out_file" bash scripts/dalton/check_public_edge.sh "${check_args[@]}" || rc=$?
  else
    _spawn_in_new_pgid "$out_file" bash scripts/dalton/check_public_edge.sh || rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    wait "$ACTIVE_STEP_CHILD_PID" || rc=$?
  fi
  ACTIVE_STEP_CHILD_PID=""
  ACTIVE_STEP_PGID=""
  out=$(cat "$out_file" 2>/dev/null || true)
  rm -f "$out_file"
  ACTIVE_STEP_OUT_FILE=""
  if [ "$rc" -eq 0 ]; then
    [ -n "$out" ] && printf '%s\n' "$out" | tail -1
  else
    [ -n "$out" ] && printf '%s\n' "$out"
  fi
  return "$rc"
}

PRE_FF="${PRE_FF:-}"
TARGET="${TARGET:-}"
PRE_FF_DALTON_REV="${PRE_FF_DALTON_REV:-}"
PRE_FF_UPSTREAM_REV="${PRE_FF_UPSTREAM_REV:-}"
MIGRATING_DEPLOY="${MIGRATING_DEPLOY:-0}"
BACKEND_TOUCHED="${BACKEND_TOUCHED:-0}"
CRAWLER_TOUCHED="${CRAWLER_TOUCHED:-0}"
EDGE_TOUCHED="${EDGE_TOUCHED:-0}"
BRIDGE_TOUCHED="${BRIDGE_TOUCHED:-0}"
GATEWAY_SWITCHED="${GATEWAY_SWITCHED:-0}"
GATEWAY_BUILD_FAILED="${GATEWAY_BUILD_FAILED:-0}"
GATEWAY_REVERT_STATE="${GATEWAY_REVERT_STATE:-not_switched}"
GATEWAY_REVERT_KICKSTART_OK="${GATEWAY_REVERT_KICKSTART_OK:-0}"
GATEWAY_REVERT_HEALTH_OK="${GATEWAY_REVERT_HEALTH_OK:-0}"
GATEWAY_SWITCH_RESTORE_FAILED="${GATEWAY_SWITCH_RESTORE_FAILED:-0}"
HAD_PREEXISTING_GATEWAY_PENDING="${HAD_PREEXISTING_GATEWAY_PENDING:-0}"
ACTUAL_RESTORED_GATEWAY_TREE="${ACTUAL_RESTORED_GATEWAY_TREE:-}"
ACTIVE_GATEWAY_STAGING_DIR="${ACTIVE_GATEWAY_STAGING_DIR:-}"
ACTIVE_GATEWAY_IMPORT_TMP="${ACTIVE_GATEWAY_IMPORT_TMP:-}"
STAGED_GATEWAY_RELEASE_ID="${STAGED_GATEWAY_RELEASE_ID:-}"
CURRENT_GATEWAY_RELEASE_REL="${CURRENT_GATEWAY_RELEASE_REL:-}"
PREV_GATEWAY_RELEASE_REL="${PREV_GATEWAY_RELEASE_REL:-}"
TARGET_GATEWAY_TREE="${TARGET_GATEWAY_TREE:-}"
PRE_FF_GATEWAY_TREE="${PRE_FF_GATEWAY_TREE:-}"
PREV_DEPLOYED_GATEWAY_TREE="${PREV_DEPLOYED_GATEWAY_TREE:-}"
FRONTEND_SWITCHED="${FRONTEND_SWITCHED:-0}"
FRONTEND_REVERT_STATE="${FRONTEND_REVERT_STATE:-not_switched}"
FRONTEND_CARRY_OVER_FAILED="${FRONTEND_CARRY_OVER_FAILED:-0}"
ACTIVE_FRONTEND_RELEASE_DIR="${ACTIVE_FRONTEND_RELEASE_DIR:-}"
NEW_FRONTEND_RELEASE_ID="${NEW_FRONTEND_RELEASE_ID:-}"
PREV_FRONTEND_RELEASE_ID="${PREV_FRONTEND_RELEASE_ID:-}"
FIRST_ROLLOUT_EDGE_TOUCHED=0
FIRST_ROLLOUT_ROLLBACK_PRE_FF=""
DEFERRED_SIGNAL=""
ROLLBACK_IN_PROGRESS=0
DEPLOY_BACKUP_STATUS="${DEPLOY_BACKUP_STATUS:-}"
DEPLOY_TARGET_HEADS="${DEPLOY_TARGET_HEADS:-}"
DEPLOY_FF_DONE="${DEPLOY_FF_DONE:-0}"
DEPLOY_LOCK_OWNED=0
_ENSURED_LOCAL_LOCK=0
ACTIVE_REEXEC_CHILD_PID=""
ACTIVE_REEXEC_TMP_DIR=""
ACTIVE_STEP_CHILD_PID=""
ACTIVE_STEP_PGID=""
ACTIVE_STEP_OUT_FILE=""

_print_manual_revert_instructions() {
  local cur_head="$1" cur_dalton_rev="$2" cur_upstream_rev="$3"
  local target_ref="${TARGET:-$cur_head}"
  local pre_ff_heads="" pre_ff_dalton="" pre_ff_upstream=""
  local target_heads="" target_dalton="" target_upstream=""
  local db_readable=0 pre_ff_matches=0 target_matches=0
  local reason="" recommended_commit="" recommended_label="" reset_target=""
  local actual_gw_tree="" pending_note=""

  if [ -n "${PRE_FF:-}" ]; then
    if pre_ff_heads=$(target_migration_heads "$PRE_FF" 2>/dev/null); then
      IFS=' ' read -r pre_ff_dalton pre_ff_upstream <<<"$pre_ff_heads"
    fi
  fi
  if [ -n "$target_ref" ] && [ "$target_ref" != "unknown" ]; then
    if target_heads=$(target_migration_heads "$target_ref" 2>/dev/null); then
      IFS=' ' read -r target_dalton target_upstream <<<"$target_heads"
    fi
  fi

  if [ -n "$cur_dalton_rev" ] && [ -n "$cur_upstream_rev" ]; then
    db_readable=1
    if [ -n "$pre_ff_dalton" ] && [ -n "$pre_ff_upstream" ] \
      && [ "$pre_ff_dalton" = "$cur_dalton_rev" ] && [ "$pre_ff_upstream" = "$cur_upstream_rev" ]; then
      pre_ff_matches=1
    fi
    if [ -n "$target_dalton" ] && [ -n "$target_upstream" ] \
      && [ "$target_dalton" = "$cur_dalton_rev" ] && [ "$target_upstream" = "$cur_upstream_rev" ]; then
      target_matches=1
    fi
  fi

  if [ -z "${PRE_FF_DALTON_REV:-}" ] || [ -z "${PRE_FF_UPSTREAM_REV:-}" ] || [ "$db_readable" = "0" ]; then
    reason="数据库迁移版本读不到（部署前或当前版本未能读取），无法确认是否存在待执行迁移；后端可能已经或将会执行迁移。"
  elif [ "$MIGRATING_DEPLOY" = "1" ]; then
    if [ "$cur_dalton_rev" != "$PRE_FF_DALTON_REV" ] || [ "$cur_upstream_rev" != "$PRE_FF_UPSTREAM_REV" ]; then
      reason="本次发布已执行数据库迁移（数据库版本从 ${PRE_FF_DALTON_REV}/${PRE_FF_UPSTREAM_REV} 变为 ${cur_dalton_rev}/${cur_upstream_rev}）；后端可能已经或将会执行迁移。"
    elif [ -n "$target_dalton" ] && [ -n "$target_upstream" ] \
      && { [ "$target_dalton" != "$cur_dalton_rev" ] || [ "$target_upstream" != "$cur_upstream_rev" ]; }; then
      reason="本次发布包含待执行数据库迁移（数据库=${cur_dalton_rev}/${cur_upstream_rev}，TARGET=${target_dalton}/${target_upstream}）；后端可能已经或将会执行迁移。"
    else
      reason="本次发布按含数据库迁移处理（快进后校验或迁移判定失败）；后端可能已经或将会执行迁移。"
    fi
  elif [ -z "$pre_ff_dalton" ] || [ -z "$pre_ff_upstream" ]; then
    reason="回退目标 PRE_FF 的迁移版本未能解析（版本读不到），无法确认回退到 PRE_FF 是否安全。"
  else
    reason="回退目标 PRE_FF 的迁移版本与数据库不一致（PRE_FF=${pre_ff_dalton}/${pre_ff_upstream}，数据库=${cur_dalton_rev}/${cur_upstream_rev}），直接回到 PRE_FF 会引入未执行的迁移或与数据库失配。"
  fi

  if [ "$pre_ff_matches" = "1" ] && [ "$target_matches" = "1" ]; then
    recommended_commit="$PRE_FF"
    recommended_label="PRE_FF (${PRE_FF})（PRE_FF 与 TARGET 的两条链 head 均等于当前数据库版本 ${cur_dalton_rev}/${cur_upstream_rev}）"
  elif [ "$pre_ff_matches" = "1" ]; then
    recommended_commit="$PRE_FF"
    recommended_label="PRE_FF (${PRE_FF})（两条链 head 均等于当前数据库版本 ${cur_dalton_rev}/${cur_upstream_rev}）"
  elif [ "$target_matches" = "1" ]; then
    recommended_commit="$target_ref"
    recommended_label="TARGET (${target_ref})（两条链 head 均等于当前数据库版本 ${cur_dalton_rev}/${cur_upstream_rev}；PRE_FF 与当前数据库不一致）"
  else
    recommended_commit=""
    recommended_label="无（PRE_FF 与 TARGET 均未满足“两条链 head 都等于数据库版本”；先不要重启后端）"
  fi
  reset_target="${recommended_commit:-<已核对一致的提交>}"

  actual_gw_tree="${ACTUAL_RESTORED_GATEWAY_TREE:-$(_read_gateway_deployed_tree || true)}"
  if [ -f "$(_gateway_pending_file)" ]; then
    pending_note="pending=是"
  else
    pending_note="pending=否"
  fi

  if [ "${GATEWAY_REVERT_STATE:-not_switched}" = "reverted" ]; then
    if [ "${GATEWAY_REVERT_KICKSTART_OK:-0}" = "1" ] && [ "${GATEWAY_REVERT_HEALTH_OK:-0}" = "1" ]; then
      echo "AUTO-REVERT ABORTED — 原因：${reason}脚本不执行 git reset、不降级、不停后端与数据库服务，保持当前代码（HEAD=${cur_head}）现状（渠道网关已在回退阶段自动换回至上一版并重启且健康检查通过）。"
    elif [ "${GATEWAY_REVERT_KICKSTART_OK:-0}" = "1" ]; then
      echo "AUTO-REVERT ABORTED — 原因：${reason}脚本不执行 git reset、不降级、不停后端与数据库服务，保持当前代码（HEAD=${cur_head}）现状（渠道网关已在回退阶段自动换回至上一版并重启，但网关健康复查失败）。"
    else
      echo "AUTO-REVERT ABORTED — 原因：${reason}脚本不执行 git reset、不降级、不停后端与数据库服务，保持当前代码（HEAD=${cur_head}）现状（渠道网关已在回退阶段自动换回至上一版，但 kickstart 重启失败）。"
    fi
  elif [ "${GATEWAY_REVERT_STATE:-not_switched}" = "restore_failed" ]; then
    echo "AUTO-REVERT ABORTED — 原因：${reason}脚本不执行 git reset、不降级、不停服务，保持当前代码（HEAD=${cur_head}）现状（渠道网关在回退阶段自动换回上一版失败，需按下方步骤手工恢复）。"
  else
    echo "AUTO-REVERT ABORTED — 原因：${reason}脚本不执行 git reset、不降级、不停服务，保持当前代码（HEAD=${cur_head}）与各服务现状。"
  fi
  echo "部署记录: PRE_FF=${PRE_FF:-未知}, TARGET=${target_ref:-未知}, HEAD=${cur_head}, backend_touched=${BACKEND_TOUCHED}, crawler_touched=${CRAWLER_TOUCHED}, edge_touched=${EDGE_TOUCHED}, bridge_touched=${BRIDGE_TOUCHED}, gateway_switched=${GATEWAY_SWITCHED}, gateway_revert_state=${GATEWAY_REVERT_STATE:-not_switched}, gateway_revert_health_ok=${GATEWAY_REVERT_HEALTH_OK:-0}"
  echo "  Dalton 链 (alembic_version_dalton): 部署前=${PRE_FF_DALTON_REV:-未能读取}, 刚才读到=${cur_dalton_rev:-未能读取}"
  echo "  上游链   (alembic_version):        部署前=${PRE_FF_UPSTREAM_REV:-未能读取}, 刚才读到=${cur_upstream_rev:-未能读取}"
  echo "  PRE_FF (${PRE_FF:-未知}) 迁移链 head: dalton=${pre_ff_dalton:-未能解析}, upstream=${pre_ff_upstream:-未能解析}"
  echo "  TARGET (${target_ref:-未知}) 迁移链 head: dalton=${target_dalton:-未能解析}, upstream=${target_upstream:-未能解析}"
  echo "  推荐复位目标: ${recommended_label}"
  if [ -z "$recommended_commit" ]; then
    echo "  【重要】PRE_FF 与 TARGET 均不满足“两条链 head 都等于当前数据库版本”：先不要重启后端！请先逐条核对数据库版本与候选提交，确认要部署哪个提交后再继续："
    echo "     docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc \"SELECT version_num FROM alembic_version_dalton\""
    echo "     docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc \"SELECT version_num FROM alembic_version\""
    if [ -n "${PRE_FF:-}" ]; then
      echo "     DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && target_migration_heads ${PRE_FF}'"
    fi
    if [ -n "$target_ref" ] && [ "$target_ref" != "unknown" ]; then
      echo "     DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && target_migration_heads ${target_ref}'"
    fi
    echo "     git log --oneline -n 10"
  fi
  echo "  注意：后端仍可能在运行或被看门狗重启，上面“刚才读到”的值随时可能变化，只作参考。"
  echo "以下命令都在部署检出根目录执行： cd ${REPO_ROOT}"
  echo "1. 排查（只读）："
  echo "     docker ps -a --filter name=dalton-langalpha-"
  echo "     docker logs --tail 100 dalton-langalpha-backend-1"
  if [ "${GATEWAY_REVERT_STATE:-not_switched}" = "reverted" ]; then
    if [ "${GATEWAY_REVERT_KICKSTART_OK:-0}" = "1" ] && [ "${GATEWAY_REVERT_HEALTH_OK:-0}" = "1" ]; then
      echo "2. 选择方案。方案 A（推荐）：后端能正常运行时保持后端当前版本（渠道网关已在回退阶段自动换回至上一版并重启且健康检查通过；如需让网关也更新到当前 HEAD，可在确认无误后执行 bash scripts/dalton/ops/prod/deploy_main.sh --gateway），修复问题后重新部署。方案 B：确需手工复位或恢复时，严格按 3–9 步顺序执行，任何一步不满足就停下。"
    elif [ "${GATEWAY_REVERT_KICKSTART_OK:-0}" = "1" ]; then
      echo "2. 选择方案。方案 A（推荐）：后端能正常运行时保持后端当前版本（渠道网关已在回退阶段自动换回至上一版并重启，但健康复查未通过，请先排查网关日志与状态），修复问题后重新部署。方案 B：确需手工复位或恢复时，严格按 3–9 步顺序执行，任何一步不满足就停下。"
    else
      echo "2. 选择方案。方案 A（推荐）：后端能正常运行时保持后端当前版本（渠道网关已在回退阶段自动换回至上一版，但 kickstart 失败，需先执行 launchctl kickstart -k gui/\$(id -u)/com.dalton.channel-gateway），修复问题后重新部署。方案 B：确需手工复位或恢复时，严格按 3–9 步顺序执行，任何一步不满足就停下。"
    fi
  else
    echo "2. 选择方案。方案 A（推荐）：后端能正常运行时保持当前版本，修复问题后重新部署。方案 B：确需手工复位或恢复时，严格按 3–9 步顺序执行，任何一步不满足就停下。"
  fi
  echo "3. 【方案 B】先停所有写入并暂停看门狗（docs/dalton/OPERATIONS.md §4），再确认后端已停："
  echo "     launchctl bootout gui/\$(id -u)/com.dalton.backend-watchdog"
  echo "     launchctl bootout gui/\$(id -u)/com.dalton.monitor-feeds"
  echo "     launchctl bootout gui/\$(id -u)/com.dalton.researchdata-bridge"
  echo "     launchctl bootout gui/\$(id -u)/com.dalton.channel-gateway"
  echo "     docker stop dalton-langalpha-edge-1 dalton-langalpha-crawler-1 dalton-langalpha-backend-1"
  echo "     docker inspect -f '{{.State.Running}}' dalton-langalpha-backend-1   # 必须输出 false"
  echo "     docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc \"SELECT count(*) FROM pg_stat_activity WHERE datname='postgres' AND state <> 'idle' AND pid <> pg_backend_pid()\"   # 应为 0（原后端的迁移会话已结束）；不为 0 就等待后再查"
  echo "4. 【方案 B】核实数据库目标：生产后端使用 compose 内 postgres 服务的 postgres 库。下面输出的 DB_HOST 不是 postgres、或 DB_NAME 不是 postgres（或未显示）时，本说明的版本读取、备份与降级命令都不适用，停下来人工处理："
  echo "     docker compose config backend | grep -E 'DB_(HOST|PORT|NAME)'"
  echo "5. 【方案 B】备份并确认完成，然后在后端已停的状态下读取实际版本："
  echo "     launchctl kickstart gui/\$(id -u)/com.dalton.pg-backup"
  echo "     tail -n 3 ~/Library/Logs/dalton-pg-backup.log   # 等末行出现新的 ' ok ' 记录，且其时间晚于第 3 步停写之后才继续（早于停写开始的备份不算）"
  echo "     docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc \"SELECT version_num FROM alembic_version_dalton\""
  echo "     docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc \"SELECT version_num FROM alembic_version\""
  if [ -z "$PRE_FF_DALTON_REV" ] || [ -z "$PRE_FF_UPSTREAM_REV" ]; then
    echo "   部署前版本未能读取：用部署前的备份（本次部署开始时生成的那份 dump）核实两条链的版本后再决定降级目标；不要仅凭 git 文件推断。"
  fi
  echo "6. 【方案 B】仅对读数高于部署前版本且决定回退迁移的链执行降级（在维护容器里用当前新代码的迁移文件；后端已停，不用 docker exec）。"
  echo "   降级会执行沿途每个迁移的 downgrade()，可能删除新版本已写入的表或列：先逐个阅读这些 downgrade()，需要保留的数据先导出，再执行："
  echo "     docker compose run --rm --no-deps --entrypoint \"\" backend sh -c \"cd /app && uv run alembic -c src/dalton/alembic.ini downgrade ${PRE_FF_DALTON_REV:-<部署前Dalton版本>}\""
  echo "     docker compose run --rm --no-deps --entrypoint \"\" backend sh -c \"cd /app && uv run alembic -c alembic.ini downgrade ${PRE_FF_UPSTREAM_REV:-<部署前上游版本>}\""
  echo "   降级后再读一次两条链的版本，必须等于目标版本；不等就停下，切勿复位代码。"
  echo "   降级后，执行 RESET_SHA=<两条链 head 等于降级后数据库版本的提交>（通常是 PRE_FF），跳过 7a，只执行 7b 核对，一致后再执行第 8 步。"
  echo "7a.【方案 B】未执行第 6 步降级时，设定复位提交（执行过第 6 步的，按第 6 步末尾设定，跳过本步）："
  echo "     RESET_SHA=${reset_target}"
  echo "7b.【方案 B】打印 RESET_SHA 的两条链 head，与当前数据库版本（第 6 步降级后以降级后的读数为准，否则用第 5 步）比对一致后再继续（不等就停下，先不要重启后端）："
  echo "     DALTON_DEPLOY_LIB_ONLY=1 RESET_SHA=\"\$RESET_SHA\" bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && target_migration_heads \"\$RESET_SHA\"'"
  if [ -n "$recommended_commit" ] && [ -n "${PRE_FF:-}" ] && [ "$recommended_commit" != "$PRE_FF" ]; then
    echo "   如第 6 步已将数据库降级回部署前版本，执行 RESET_SHA=${PRE_FF}，只执行 7b 核对，一致后再执行第 8 步。"
  elif [ -z "$recommended_commit" ]; then
    echo "   请把 RESET_SHA 设为两条链 head 均等于数据库版本的提交（替换 <已核对一致的提交>）；确认一致前先不要重启后端。"
  fi
  echo "8. 【方案 B】确认 7b 比对一致后，先将当前部署脚本备份到 /tmp/dalton_deploy_main.sh（防止复位到较早的 RESET_SHA 后工作区脚本尚无前端/网关辅助函数），再复位代码、切换或重建前端版本目录、处理渠道网关产物，并强制重建容器（后端仍处于停止状态才执行）："
  echo "     cp scripts/dalton/ops/prod/deploy_main.sh /tmp/dalton_deploy_main.sh && git reset --keep \"\$RESET_SHA\" && (cd web && pnpm install --frozen-lockfile)"
  echo "   前端版本目录状态: current=$(_read_frontend_current_release || echo 未设置), prev=${PREV_FRONTEND_RELEASE_ID:-无}, new=${NEW_FRONTEND_RELEASE_ID:-无}（目录位于 web/releases/，旧版本已保留）。"
  echo "   若 web/releases/ 中已有与 \$RESET_SHA 对应的版本目录，直接原子切换 current；否则在复位后的代码上新建版本目录并切换（若 \$RESET_SHA 仍使用旧版 web/dist 挂载，则改跑 bash scripts/dalton/build_frontend_prod.sh --require-supabase）："
  echo "     DALTON_REPO_ROOT=\"${REPO_ROOT}\" DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source /tmp/dalton_deploy_main.sh && manual_switch_frontend_release \"<版本目录名>\"'"
  echo "     # 或在复位后的代码上新建版本目录并原子切换："
  echo "     DALTON_REPO_ROOT=\"${REPO_ROOT}\" DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source /tmp/dalton_deploy_main.sh && manual_build_frontend_release'"
  if [ "${GATEWAY_REVERT_STATE:-not_switched}" = "reverted" ]; then
    echo "   渠道网关已在回退阶段自动换回至上一版 release（gateway_revert_state=reverted；实际恢复的网关树哈希=${actual_gw_tree:-未知}；${pending_note}）：请对比 git rev-parse \"\${RESET_SHA}:integrations/dalton-channel-gateway\" 与该实际恢复树哈希 (${actual_gw_tree:-未知})；仅当实际恢复树哈希已知、两者相等且无 .deploy-gateway-pending 时无需重建，否则在复位后的代码上使用独立目录重建并原子切换（加载 reset 前备份的 /tmp/dalton_deploy_main.sh）："
    echo "     DALTON_REPO_ROOT=\"${REPO_ROOT}\" DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source /tmp/dalton_deploy_main.sh && manual_rebuild_gateway_release'"
  elif [ "${GATEWAY_REVERT_STATE:-not_switched}" = "restore_failed" ]; then
    echo "   本次发布已切换渠道网关（gateway_switched=1），但自动换回上一版失败（gateway_revert_state=restore_failed；当前磁盘状态: $(_describe_gateway_disk_state 0 "$cur_head")）：在启动网关之前，先检查 integrations/dalton-channel-gateway/.deploy-releases/ 下是否存在完整且树哈希匹配 git rev-parse \"\${RESET_SHA}:integrations/dalton-channel-gateway\" 的 release；若存在可调用辅助函数修复顶层链接并原子切换（切勿先删目录；加载 reset 前备份的 /tmp/dalton_deploy_main.sh）："
    echo "     DALTON_REPO_ROOT=\"${REPO_ROOT}\" DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source /tmp/dalton_deploy_main.sh && manual_switch_gateway_release \".deploy-releases/<上一版release目录名>\"'"
    echo "   否则在复位后的代码上使用独立目录重建并原子切换："
    echo "     DALTON_REPO_ROOT=\"${REPO_ROOT}\" DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source /tmp/dalton_deploy_main.sh && manual_rebuild_gateway_release'"
  else
    echo "   在启动网关之前，请对比 git rev-parse \"\${RESET_SHA}:integrations/dalton-channel-gateway\" 与当前网关已知树哈希 (${actual_gw_tree:-未知}；${pending_note})；若未知、不相等或存在 .deploy-gateway-pending，请在复位后的代码上使用独立目录重建并原子切换（加载 reset 前备份的 /tmp/dalton_deploy_main.sh）："
    echo "     DALTON_REPO_ROOT=\"${REPO_ROOT}\" DALTON_DEPLOY_LIB_ONLY=1 bash -c 'source /tmp/dalton_deploy_main.sh && manual_rebuild_gateway_release'"
  fi
  echo "     docker compose up -d --no-deps --force-recreate backend crawler edge"
  echo "9. 【方案 B】恢复宿主机服务与看门狗，并验证："
  echo "     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.dalton.monitor-feeds.plist"
  echo "     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.dalton.researchdata-bridge.plist"
  echo "     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.dalton.channel-gateway.plist"
  echo "     ok=0; for i in \$(seq 1 60); do curl -fsS -m3 http://127.0.0.1:8000/health >/dev/null && { ok=1; break; }; sleep 5; done; [ \$ok = 1 ] && echo backend ok || echo 'BACKEND NOT HEALTHY'"
  echo "     ok=0; for i in \$(seq 1 20); do docker exec dalton-langalpha-backend-1 sh -c 'curl -fsS -m5 http://crawler:8100/health' >/dev/null 2>&1 && { ok=1; break; }; sleep 3; done; [ \$ok = 1 ] && echo crawler ok || echo 'CRAWLER NOT HEALTHY'"
  echo "     for s in researchdata-bridge channel-gateway monitor-feeds; do launchctl list | grep -q \"com.dalton.\$s\" && echo \"\$s loaded\" || echo \"\$s NOT LOADED\"; done"
  echo "     bash scripts/dalton/check_public_edge.sh"
  echo "   以上全部通过后再恢复看门狗："
  echo "     launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.dalton.backend-watchdog.plist"
}

# Auto-revert is safe only when the deploy itself had no pending migrations (MIGRATING_DEPLOY=0)
# AND the revert target PRE_FF's two migration heads both equal the pre-deploy DB revisions.
_can_auto_revert_to_pre_ff() {
  local pre_ff_heads="" pre_ff_dalton="" pre_ff_upstream=""
  [ "$MIGRATING_DEPLOY" = "0" ] || return 1
  [ -n "${PRE_FF:-}" ] || return 1
  if [ -z "${PRE_FF_DALTON_REV:-}" ] || [ -z "${PRE_FF_UPSTREAM_REV:-}" ]; then
    echo "WARN: auto-revert blocked: pre-deploy DB version unreadable (dalton=${PRE_FF_DALTON_REV:-未能读取}, upstream=${PRE_FF_UPSTREAM_REV:-未能读取})" >&2
    return 1
  fi
  if ! pre_ff_heads=$(target_migration_heads "$PRE_FF"); then
    echo "WARN: auto-revert blocked: failed to resolve migration heads for PRE_FF=${PRE_FF}" >&2
    return 1
  fi
  IFS=' ' read -r pre_ff_dalton pre_ff_upstream <<<"$pre_ff_heads"
  if [ "$PRE_FF_DALTON_REV" != "$pre_ff_dalton" ] || [ "$PRE_FF_UPSTREAM_REV" != "$pre_ff_upstream" ]; then
    echo "WARN: auto-revert blocked: PRE_FF (${PRE_FF}) heads (${pre_ff_dalton}/${pre_ff_upstream}) differ from pre-deploy DB revisions (${PRE_FF_DALTON_REV}/${PRE_FF_UPSTREAM_REV})" >&2
    return 1
  fi
  return 0
}

# Put code, web dependencies and the served bundle back together, so a failed step never leaves
# a new frontend (or new code waiting for the next restart) in front of the running backend.
# Because migration directories are bind-mounted into containers, auto-revert runs only when
# MIGRATING_DEPLOY=0 and PRE_FF's migration heads match the pre-deploy DB revisions; otherwise
# it aborts automatic code reset and prints manual instructions.
revert_deploy() {
  local need_post_health=0 post_ok=1 cur_head gw_dir="" rel_dir="" revert_expected_build=""
  local cur_dalton_rev="" cur_upstream_rev="" fe_was_switched="${FRONTEND_SWITCHED:-0}"
  local -a failed_steps=()

  gw_dir=$(_gateway_dir)
  rel_dir=$(_frontend_releases_dir)
  GATEWAY_REVERT_STATE="not_switched"
  GATEWAY_REVERT_KICKSTART_OK=0
  GATEWAY_REVERT_HEALTH_OK=0
  FRONTEND_REVERT_STATE="not_switched"

  # Restore frontend symlink BEFORE checking _can_auto_revert_to_pre_ff and BEFORE
  # git reset --keep, so neither migration protection nor a git reset failure can leave
  # an unverified new release live or delete the previous release directory.
  if [ "$fe_was_switched" = "1" ]; then
    echo "$(date '+%T') reverting frontend web/releases/current"
    if restore_frontend_from_prev; then
      echo "frontend current atomically restored to web/releases/${PREV_FRONTEND_RELEASE_ID}"
    else
      echo "WARN: frontend restore to previous release failed (preserving web/releases/${PREV_FRONTEND_RELEASE_ID:-unknown} and web/releases/${NEW_FRONTEND_RELEASE_ID:-unknown})"
      post_ok=0
      failed_steps+=("frontend_revert")
    fi
  elif [ -n "${ACTIVE_FRONTEND_RELEASE_DIR:-}" ]; then
    if ! cleanup_frontend_staging; then
      echo "WARN: failed to remove unswitched frontend release directory"
      post_ok=0
      failed_steps+=("frontend_staging_cleanup")
    fi
  fi

  if [ "$GATEWAY_SWITCHED" = "1" ]; then
    echo "$(date '+%T') reverting channel gateway"
    if restore_gateway_from_prev; then
      GATEWAY_REVERT_STATE="reverted"
      if launchctl kickstart -k "gui/$(id -u)/com.dalton.channel-gateway" >/dev/null 2>&1; then
        GATEWAY_REVERT_KICKSTART_OK=1
      else
        echo "WARN: gateway revert kickstart failed"
        post_ok=0
        failed_steps+=("gateway_revert")
      fi
    else
      GATEWAY_REVERT_STATE="restore_failed"
      echo "WARN: gateway restore to previous release failed; skipping gateway kickstart"
      post_ok=0
      failed_steps+=("gateway_restore")
      _print_gateway_manual_restore_steps
    fi
  elif [ -n "${ACTIVE_GATEWAY_STAGING_DIR:-}" ] || [ -e "${gw_dir}/.deploy-staging" ]; then
    if ! cleanup_gateway_staging; then
      echo "WARN: failed to remove gateway staging directory"
      post_ok=0
      failed_steps+=("gateway_staging_cleanup")
    fi
  fi

  if ! _can_auto_revert_to_pre_ff; then
    # Preserve both previous and new frontend release directories for operator inspection/choice.
    if [ "$GATEWAY_SWITCHED" = "1" ] && [ "$GATEWAY_REVERT_STATE" = "reverted" ] && [ "$GATEWAY_REVERT_KICKSTART_OK" = "1" ]; then
      if wait_gateway_health; then
        GATEWAY_REVERT_HEALTH_OK=1
        echo "POST-REVERT CHECK: gateway health OK"
      else
        GATEWAY_REVERT_HEALTH_OK=0
        echo "POST-REVERT CHECK: gateway health FAILED"
      fi
    fi
    cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    cur_dalton_rev=$(get_dalton_alembic_version || true)
    cur_upstream_rev=$(get_upstream_alembic_version || true)
    _print_manual_revert_instructions "$cur_head" "$cur_dalton_rev" "$cur_upstream_rev"
    return 1
  fi

  if [ "$BACKEND_TOUCHED" = "1" ] || [ "$CRAWLER_TOUCHED" = "1" ] || [ "$EDGE_TOUCHED" = "1" ] || [ "$GATEWAY_SWITCHED" = "1" ] || [ "$fe_was_switched" = "1" ]; then
    need_post_health=1
  fi
  git update-index -q --refresh
  if ! git reset -q --keep "$PRE_FF"; then
    cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    echo "CODE REVERT FAILED — 代码未能恢复，需人工处理 (HEAD=${cur_head}, PRE_FF=${PRE_FF}; frontend_current=$(_read_frontend_current_release || echo 未设置), prev_release=${PREV_FRONTEND_RELEASE_ID:-无}; manual: git update-index --refresh && git reset --keep $PRE_FF)"
    return 1
  fi
  cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
  if [ "$cur_head" != "$PRE_FF" ]; then
    echo "CODE REVERT FAILED — 代码未能恢复，需人工处理 (HEAD=${cur_head}, PRE_FF=${PRE_FF})"
    return 1
  fi
  if ! uv sync --frozen -q >/dev/null 2>&1; then
    echo "WARN: host venv re-sync failed"
    post_ok=0
    failed_steps+=("uv_sync")
  fi
  if ! (cd web && pnpm install --frozen-lockfile) >/dev/null 2>&1; then
    echo "WARN: pnpm install on revert failed"
    post_ok=0
    failed_steps+=("pnpm_install")
  fi
  if [ "$fe_was_switched" = "1" ]; then
    if [ "$FRONTEND_REVERT_STATE" = "reverted" ]; then
      if [ "${FRONTEND_CARRY_OVER_FAILED:-0}" = "1" ]; then
        echo "WARN: preserving web/releases/${NEW_FRONTEND_RELEASE_ID:-unknown} because carrying over outgoing assets into ${PREV_FRONTEND_RELEASE_ID} failed"
        post_ok=0
        failed_steps+=("frontend_asset_carryover")
      elif [ -n "${NEW_FRONTEND_RELEASE_ID:-}" ] && [ "${NEW_FRONTEND_RELEASE_ID}" != "${PREV_FRONTEND_RELEASE_ID:-}" ] \
         && ! _is_active_frontend_release_dir "${rel_dir}/${NEW_FRONTEND_RELEASE_ID}"; then
        rm -rf "${rel_dir}/${NEW_FRONTEND_RELEASE_ID}" 2>/dev/null || true
      fi
      echo "reverted to $(git rev-parse --short=9 HEAD), frontend current restored to web/releases/${PREV_FRONTEND_RELEASE_ID}"
    else
      echo "OLD FRONTEND RESTORE FAILED"
    fi
  else
    echo "reverted to $(git rev-parse --short=9 HEAD), previous frontend release preserved"
  fi
  if [ "$BACKEND_TOUCHED" = "1" ]; then
    echo "$(date '+%T') reverting backend"
    if ! apply_backend; then
      echo "WARN: backend revert failed"
      post_ok=0
      failed_steps+=("backend_revert")
    fi
  fi
  if [ "$CRAWLER_TOUCHED" = "1" ]; then
    echo "$(date '+%T') reverting crawler"
    if ! apply_crawler; then
      echo "WARN: crawler revert failed"
      post_ok=0
      failed_steps+=("crawler_revert")
    fi
  fi
  if [ "$EDGE_TOUCHED" = "1" ]; then
    echo "$(date '+%T') reverting edge"
    if ! recreate_edge; then
      echo "WARN: edge revert failed"
      post_ok=0
      failed_steps+=("edge_revert")
    fi
  fi
  if [ "$BRIDGE_TOUCHED" = "1" ]; then
    if ! launchctl kickstart -k "gui/$(id -u)/com.dalton.researchdata-bridge" >/dev/null 2>&1; then
      echo "WARN: bridge revert kickstart failed"
      post_ok=0
      failed_steps+=("bridge_revert")
    fi
  fi
  if [ "$need_post_health" = "1" ]; then
    echo "$(date '+%T') running post-revert health checks"
    if wait_backend_health; then
      echo "POST-REVERT CHECK: backend health OK"
    else
      echo "POST-REVERT CHECK: backend health FAILED"
      post_ok=0
      failed_steps+=("backend_health")
    fi
    if [ "$GATEWAY_SWITCHED" = "1" ] && [ "$GATEWAY_REVERT_STATE" = "reverted" ] && [ "$GATEWAY_REVERT_KICKSTART_OK" = "1" ]; then
      if wait_gateway_health; then
        GATEWAY_REVERT_HEALTH_OK=1
        echo "POST-REVERT CHECK: gateway health OK"
      else
        GATEWAY_REVERT_HEALTH_OK=0
        echo "POST-REVERT CHECK: gateway health FAILED"
        post_ok=0
        failed_steps+=("gateway_health")
      fi
    fi
    if wait_crawler_health; then
      echo "POST-REVERT CHECK: crawler health OK"
    else
      echo "POST-REVERT CHECK: crawler health FAILED"
      post_ok=0
      failed_steps+=("crawler_health")
    fi
    if [ "$EDGE_TOUCHED" = "1" ]; then
      if wait_edge_ready; then
        echo "POST-REVERT CHECK: edge ready OK"
      else
        echo "POST-REVERT CHECK: edge ready FAILED"
        post_ok=0
        failed_steps+=("edge_ready")
      fi
    fi
    revert_expected_build=""
    local revert_expected_release=""
    if [ -n "${PREV_FRONTEND_RELEASE_ID:-}" ] && [ -s "${rel_dir}/${PREV_FRONTEND_RELEASE_ID}/version.json" ]; then
      revert_expected_build=$(_read_frontend_build_id "${rel_dir}/${PREV_FRONTEND_RELEASE_ID}" || true)
      revert_expected_release=$(_read_frontend_release_tag "${rel_dir}/${PREV_FRONTEND_RELEASE_ID}" || true)
    fi
    if run_public_edge_check "$revert_expected_build" "" "$revert_expected_release"; then
      echo "POST-REVERT CHECK: public edge check OK"
    else
      echo "POST-REVERT CHECK: public edge check FAILED"
      post_ok=0
      failed_steps+=("public_edge_check")
    fi
  fi
  if [ "$post_ok" = "1" ]; then
    if [ -z "$PRE_FF_GATEWAY_TREE" ] && [ -n "${PRE_FF:-}" ]; then
      PRE_FF_GATEWAY_TREE=$(git rev-parse "${PRE_FF}:integrations/dalton-channel-gateway" 2>/dev/null || true)
    fi
    if [ "${HAD_PREEXISTING_GATEWAY_PENDING:-0}" = "0" ] \
       && [ "${GATEWAY:-0}" != "1" ] \
       && [ "${GATEWAY_BUILD_FAILED:-0}" != "1" ]; then
      if [ "$GATEWAY_SWITCHED" = "1" ]; then
        if [ "$GATEWAY_REVERT_STATE" = "reverted" ] && [ "$GATEWAY_REVERT_KICKSTART_OK" = "1" ] && [ "$GATEWAY_REVERT_HEALTH_OK" = "1" ] \
           && [ -n "$ACTUAL_RESTORED_GATEWAY_TREE" ] && [ -n "$PRE_FF_GATEWAY_TREE" ] \
           && [ "$ACTUAL_RESTORED_GATEWAY_TREE" = "$PRE_FF_GATEWAY_TREE" ]; then
          _clear_gateway_pending || true
        fi
      elif [ -n "$PREV_DEPLOYED_GATEWAY_TREE" ] && [ -n "$PRE_FF_GATEWAY_TREE" ] \
           && [ "$PREV_DEPLOYED_GATEWAY_TREE" = "$PRE_FF_GATEWAY_TREE" ]; then
        _clear_gateway_pending || true
      fi
    fi
    if [ "$need_post_health" = "1" ]; then
      echo "POST-REVERT HEALTH REPORT: all revert steps and post-revert checks passed"
    elif [ "${GATEWAY_BUILD_FAILED:-0}" = "1" ]; then
      echo "POST-REVERT REPORT: code reverted, old frontend preserved (网关未切换，生产目录未改动)"
    else
      echo "POST-REVERT REPORT: all revert steps passed"
    fi
    return 0
  else
    echo "POST-REVERT REPORT: FAILED (failed steps: ${failed_steps[*]})"
    return 1
  fi
}

# Last backup-log line if it is an ' ok ' dump/backup-set at most $2 minutes old (relative to $3,
# epoch seconds); prints nothing and returns 1 otherwise.
# Accepts both legacy ("YYYY-mm-dd HH:MM:SS ok  <size> <path>") and consistent backup-set
# ("YYYY-mm-dd HH:MM:SS ok  <size> <path> set=<id>") formats.
recent_ok_backup() {
  local log="$1" max_min="$2" now="$3" line ts epoch
  line=$(tail -1 "$log" 2>/dev/null) || return 1
  case "$line" in *" ok "*) ;; *) return 1;; esac
  ts="${line:0:19}"
  epoch=$(date -j -f '%Y-%m-%d %H:%M:%S' "$ts" +%s 2>/dev/null || date -d "$ts" +%s 2>/dev/null) || return 1
  [ -n "$epoch" ] && [ "$epoch" -le "$now" ] && [ $(( now - epoch )) -le $(( max_min * 60 )) ] || return 1
  printf '%s\n' "$line"
}

# True when a dalton-pg-backup.log line represents a completed backup result (ok or failure),
# ignoring intermediate progress lines prefixed with "[backup]".
_is_terminal_pg_backup_line() {
  local line="$1"
  [ -n "$line" ] || return 1
  case "$line" in
    "[backup]"*) return 1 ;;
    *) return 0 ;;
  esac
}

# True when launchd reports com.dalton.pg-backup is actively running (has an active PID / state = running).
_pg_backup_job_running() {
  local out=""
  out=$(launchctl print "gui/$(id -u)/com.dalton.pg-backup" 2>/dev/null) || return 1
  printf '%s\n' "$out" | grep -Eq '^[[:space:]]*(state = running|pid = [0-9]+)'
}

# Find the terminal dalton-pg-backup.log line matching a waited-on backup ID (set=<id>).
# When wait_id is non-empty, ONLY a terminal line with exact token "set=<wait_id>" matches
# (never falls back to lines without set= or with another set=). When wait_id is empty,
# falls back to the latest terminal line strictly after $3.
_find_completed_backup_for_wait() {
  local log_file="$1" wait_id="$2" before_line="$3"
  [ -f "$log_file" ] || return 1
  local found=""
  if [ -n "$wait_id" ]; then
    found=$(awk -v target="set=${wait_id}" '
      !/^\[backup\]/ {
        for (i = 1; i <= NF; i++) {
          if ($i == target) {
            last = $0
            break
          }
        }
      }
      END { if (last != "") print last }
    ' "$log_file" 2>/dev/null || true)
    if [ -n "$found" ] && [ "$found" != "$before_line" ] && _is_terminal_pg_backup_line "$found"; then
      printf '%s\n' "$found"
      return 0
    fi
    return 1
  fi
  local candidate=""
  if [ -n "$before_line" ]; then
    local last_before_ln=""
    last_before_ln=$(grep -n -F -x -- "$before_line" "$log_file" 2>/dev/null | tail -1 | cut -d: -f1 || true)
    if [ -n "$last_before_ln" ]; then
      candidate=$(tail -n "+$((last_before_ln + 1))" "$log_file" 2>/dev/null | grep -v '^\[backup\]' | tail -1 || true)
    else
      candidate=$(grep -v '^\[backup\]' "$log_file" 2>/dev/null | tail -1 || true)
    fi
  else
    candidate=$(grep -v '^\[backup\]' "$log_file" 2>/dev/null | tail -1 || true)
  fi
  [ -n "$candidate" ] || return 1
  if [ "$candidate" = "$before_line" ]; then
    return 1
  fi
  if ! _is_terminal_pg_backup_line "$candidate"; then
    return 1
  fi
  printf '%s\n' "$candidate"
  return 0
}

# Verify that a completed consistent backup-set log line ("... ok <size> <dump_path> set=<id>")
# has its backup-set metadata AND every referenced artifact (PG dump, object manifest + pool
# objects, and ASR archive when recorded) intact on disk with matching SHA-256 digests.
# Legacy mock lines without "set=<id>" pass unless DALTON_DEPLOY_VERIFY_LEGACY_DUMP_FILE=1.
_verify_completed_backup_artifact() {
  local line="$1"
  [ -n "$line" ] || return 1
  local set_id="" dump_path=""
  case "$line" in
    *" set="*)
      set_id="${line##* set=}"
      set_id="${set_id%%[[:space:]]*}"
      ;;
  esac
  # Log format: YYYY-mm-dd HH:MM:SS ok <size> <dump_path> [set=<id>]
  dump_path="$(printf '%s\n' "$line" | awk '{print $5}')"
  if [ -n "$set_id" ]; then
    if [ -z "$dump_path" ] || [ ! -f "$dump_path" ] || [ ! -s "$dump_path" ]; then
      echo "backup: verified dump file missing or empty for set=${set_id} (path=${dump_path:-<empty>})" >&2
      return 1
    fi
    local parent_dir="" backups_root="" set_json="" py=""
    parent_dir="$(dirname "$dump_path")"
    backups_root="$(dirname "$parent_dir")"
    set_json="${backups_root}/sets/backup-set-${set_id}.json"
    if [ ! -f "$set_json" ] || [ ! -s "$set_json" ]; then
      echo "backup: backup-set metadata missing or empty for set=${set_id} (${set_json})" >&2
      return 1
    fi
    py=$(_resolve_deploy_python) || return 1
    "$py" - "$set_id" "$dump_path" "$set_json" "$backups_root" <<'PY'
import hashlib
import json
import os
import re
import sys
from pathlib import Path

set_id, log_dump_path, set_json_path, backups_root = sys.argv[1:5]
hex64_re = re.compile(r"^[0-9a-f]{64}$")

def fail(msg: str) -> None:
    print(f"backup: {msg}", file=sys.stderr)
    sys.exit(1)

def sha256_and_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    total = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
    return h.hexdigest(), total

try:
    meta = json.loads(Path(set_json_path).read_text(encoding="utf-8"))
except Exception as exc:
    fail(f"failed reading backup-set metadata {set_json_path}: {exc}")

if not isinstance(meta, dict):
    fail(f"invalid backup-set metadata structure in {set_json_path}")
if str(meta.get("backup_set_id") or "").strip() != set_id:
    fail(
        f"backup-set id mismatch in {set_json_path}: "
        f"got={meta.get('backup_set_id')!r}, expected={set_id!r}"
    )
if meta.get("reconcile_ok") is not True:
    fail(f"backup-set {set_id} is not marked reconcile_ok=true")

# 1. Verify PostgreSQL dump exists, matches log path, size, and SHA-256.
meta_dump_raw = str(meta.get("dump_path") or "").strip()
meta_dump_sha = str(meta.get("dump_sha256") or "").strip().lower()
if not meta_dump_raw:
    fail(f"backup-set {set_id} metadata missing dump_path")
if not hex64_re.fullmatch(meta_dump_sha):
    fail(f"backup-set {set_id} metadata missing valid dump_sha256")
meta_dump = Path(meta_dump_raw)
if not meta_dump.is_file() or meta_dump.is_symlink():
    fail(f"backup-set {set_id} dump file missing: {meta_dump}")
if os.path.realpath(str(meta_dump)) != os.path.realpath(log_dump_path):
    fail(
        f"backup-set {set_id} dump_path mismatch: "
        f"metadata={meta_dump}, log={log_dump_path}"
    )
actual_dump_sha, actual_dump_size = sha256_and_size(meta_dump)
if actual_dump_size <= 0:
    fail(f"backup-set {set_id} dump file is empty: {meta_dump}")
if meta.get("dump_size_bytes") is not None and actual_dump_size != int(meta["dump_size_bytes"]):
    fail(
        f"backup-set {set_id} dump size mismatch: "
        f"actual={actual_dump_size}, expected={meta['dump_size_bytes']}"
    )
if actual_dump_sha != meta_dump_sha:
    fail(
        f"backup-set {set_id} dump SHA-256 mismatch: "
        f"actual={actual_dump_sha}, expected={meta_dump_sha}"
    )

# 2. Verify object manifest exists, optional manifest_sha256 matches, and every
#    referenced mirror-pool object exists with matching size and archived_sha256.
manifest_raw = str(meta.get("manifest_path") or "").strip()
if not manifest_raw:
    fail(f"backup-set {set_id} metadata missing manifest_path")
manifest_path = Path(manifest_raw)
if not manifest_path.is_file() or manifest_path.is_symlink():
    fail(f"backup-set {set_id} object manifest missing: {manifest_path}")
meta_manifest_sha = str(meta.get("manifest_sha256") or "").strip().lower()
if meta_manifest_sha:
    if not hex64_re.fullmatch(meta_manifest_sha):
        fail(f"backup-set {set_id} metadata has invalid manifest_sha256")
    actual_manifest_sha, _ = sha256_and_size(manifest_path)
    if actual_manifest_sha != meta_manifest_sha:
        fail(
            f"backup-set {set_id} object manifest SHA-256 mismatch: "
            f"actual={actual_manifest_sha}, expected={meta_manifest_sha}"
        )
try:
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
except Exception as exc:
    fail(f"backup-set {set_id} failed reading object manifest {manifest_path}: {exc}")
if not isinstance(manifest_data, dict) or not isinstance(manifest_data.get("objects"), list):
    fail(f"backup-set {set_id} object manifest {manifest_path} missing valid 'objects' list")
objects = manifest_data["objects"]
if meta.get("object_count") is not None and len(objects) != int(meta["object_count"]):
    fail(
        f"backup-set {set_id} object_count mismatch: "
        f"manifest={len(objects)}, metadata={meta['object_count']}"
    )
pool_dir = Path(backups_root) / "minio-mirror" / "objects"
verified_pool: dict[str, int] = {}
seen_pairs: set[tuple[str, str]] = set()
for obj in objects:
    if not isinstance(obj, dict):
        fail(f"backup-set {set_id} manifest contains non-object entry")
    bkt = str(obj.get("bucket") or "")
    key = str(obj.get("key") or "").lstrip("/")
    if not bkt or not key:
        fail(f"backup-set {set_id} manifest entry missing bucket/key")
    pair = (bkt, key)
    if pair in seen_pairs:
        fail(f"backup-set {set_id} manifest contains duplicate object {bkt}/{key}")
    seen_pairs.add(pair)
    sha = str(obj.get("archived_sha256") or "").strip().lower()
    if not hex64_re.fullmatch(sha):
        fail(f"backup-set {set_id} manifest entry {bkt}/{key} missing valid archived_sha256")
    raw_sz = obj.get("size")
    if raw_sz is None or int(raw_sz) < 0:
        fail(f"backup-set {set_id} manifest entry {bkt}/{key} missing valid size")
    exp_sz = int(raw_sz)
    if sha in verified_pool:
        if verified_pool[sha] != exp_sz:
            fail(f"backup-set {set_id} mirror object size mismatch for {bkt}/{key}")
        continue
    obj_path = pool_dir / sha[:2] / sha
    if not obj_path.is_file() or obj_path.is_symlink():
        fail(f"backup-set {set_id} mirror object missing for {bkt}/{key}: {obj_path}")
    got_sha, got_sz = sha256_and_size(obj_path)
    if got_sha != sha:
        fail(
            f"backup-set {set_id} mirror object SHA-256 mismatch for {bkt}/{key}: "
            f"actual={got_sha}, expected={sha}"
        )
    if got_sz != exp_sz:
        fail(
            f"backup-set {set_id} mirror object size mismatch for {bkt}/{key}: "
            f"actual={got_sz}, expected={exp_sz}"
        )
    verified_pool[sha] = got_sz

# 3. Verify ASR jobs archive when recorded in metadata.
asr_raw = str(meta.get("asr_archive_path") or "").strip()
asr_sha = str(meta.get("asr_archive_sha256") or "").strip().lower()
asr_size = meta.get("asr_archive_size_bytes")
if asr_raw or asr_sha or asr_size is not None:
    if not asr_raw:
        fail(f"backup-set {set_id} metadata has ASR digest/size but missing asr_archive_path")
    if not hex64_re.fullmatch(asr_sha):
        fail(f"backup-set {set_id} metadata missing valid asr_archive_sha256")
    asr_path = Path(asr_raw)
    if not asr_path.is_file() or asr_path.is_symlink():
        fail(f"backup-set {set_id} ASR archive missing: {asr_path}")
    actual_asr_sha, actual_asr_size = sha256_and_size(asr_path)
    if asr_size is not None and actual_asr_size != int(asr_size):
        fail(
            f"backup-set {set_id} ASR archive size mismatch: "
            f"actual={actual_asr_size}, expected={asr_size}"
        )
    if actual_asr_sha != asr_sha:
        fail(
            f"backup-set {set_id} ASR archive SHA-256 mismatch: "
            f"actual={actual_asr_sha}, expected={asr_sha}"
        )
PY
    return $?
  elif [ "${DALTON_DEPLOY_VERIFY_LEGACY_DUMP_FILE:-0}" = "1" ]; then
    if [ -z "$dump_path" ] || [ ! -s "$dump_path" ]; then
      echo "backup: dump file missing or empty (path=${dump_path:-<empty>})" >&2
      return 1
    fi
  fi
  return 0
}

_write_deploy_backup_wait_state() {
  local state_file="$1" wait_id="$2" wait_head="$3" wait_target="$4" wait_before="$5"
  mkdir -p "$(dirname "$state_file")" 2>/dev/null || true
  local tmp_file="${state_file}.tmp.$$"
  {
    printf 'WAIT_BACKUP_ID=%s\n' "$wait_id"
    printf 'WAIT_PRE_FF=%s\n' "$wait_head"
    printf 'WAIT_TARGET=%s\n' "$wait_target"
    printf 'WAIT_BEFORE_LINE=%s\n' "$wait_before"
  } > "$tmp_file" && mv -f "$tmp_file" "$state_file"
}

# Prefer repo .venv/bin/python; fall back to python3 on PATH when absent.
# Require Python >= 3.11.
_resolve_deploy_python() {
  local py="" ver=""
  if [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
    py="${REPO_ROOT}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    py=$(command -v python3)
  else
    echo "ERROR: target_migration_heads: Python >= 3.11 not found (checked ${REPO_ROOT}/.venv/bin/python and python3 on PATH)" >&2
    return 1
  fi
  if ! "$py" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    ver=$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "unknown")
    echo "ERROR: target_migration_heads: Python >= 3.11 required, got ${py} (${ver})" >&2
    return 1
  fi
  printf '%s' "$py"
}

# Reject untracked, ignored (except __pycache__/ and *.pyc), modified, or staged files in either
# migration directory before backup/ff and again right before backend restart.
check_clean_migration_dirs() {
  local raw="" dirty="" line="" status="" path="" unquoted="" nl=$'\n'
  if ! raw=$(git status --porcelain --untracked-files=all --ignored -- migrations/versions src/dalton/migrations/versions); then
    echo "ERROR: failed to inspect migration directories via git status, abort" >&2
    return 1
  fi
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    status="${line:0:2}"
    path="${line:3}"
    unquoted="${path#\"}"
    unquoted="${unquoted%\"}"
    if [ "$status" = "!!" ] || [ "$status" = "??" ]; then
      # Alembic loads only *.py, so untracked or ignored files of any other kind
      # (.DS_Store, editor backups, __pycache__/) cannot change what runs.
      case "$unquoted" in
        */__pycache__/*) continue ;;
        *.py) ;;
        *) continue ;;
      esac
    fi
    dirty="${dirty:+${dirty}${nl}}${line}"
  done <<<"$raw"
  if [ -n "$dirty" ]; then
    echo "ERROR: 迁移目录下存在未提交或被忽略的文件（未跟踪、被忽略、已修改或已暂存），先清理或提交后再部署:" >&2
    printf '%s\n' "$dirty" >&2
    return 1
  fi
  return 0
}

# Print "<dalton_head> <upstream_head>" for git commit $1 by reading revision and down_revision
# from git history via git ls-tree / git show (no container alembic, no working-tree ff required).
# Fails if $1 is invalid, a chain has no migration files, a file is missing revision/down_revision
# or duplicated, any down_revision is missing from its chain, either chain does not resolve to a
# single unique head, or walking down_revision from the head misses revisions / hits a cycle.
target_migration_heads() {
  local commit="${1:-}" py=""
  if [ -z "$commit" ]; then
    echo "ERROR: target_migration_heads requires a git commit/ref" >&2
    return 1
  fi
  if ! git rev-parse --verify "${commit}^{commit}" >/dev/null 2>&1; then
    echo "ERROR: target_migration_heads: invalid git commit '${commit}'" >&2
    return 1
  fi
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$commit" <<'PY'
import ast
import subprocess
import sys

if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

commit = sys.argv[1]
chains = (
    ("dalton", "src/dalton/migrations/versions"),
    ("upstream", "migrations/versions"),
)


def _extract_rev_and_down(src: str, path: str):
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        raise RuntimeError(f"syntax error in {path}: {exc}") from exc

    rev = None
    down = None
    has_rev = False
    has_down = False
    for node in tree.body:
        names = []
        value_node = None
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            names = [node.target.id]
            value_node = node.value

        if "revision" in names:
            has_rev = True
            try:
                rev = ast.literal_eval(value_node)
            except Exception as exc:
                raise RuntimeError(f"cannot evaluate revision in {path}: {exc}") from exc
        if "down_revision" in names:
            has_down = True
            try:
                down = ast.literal_eval(value_node)
            except Exception as exc:
                raise RuntimeError(f"cannot evaluate down_revision in {path}: {exc}") from exc

    if not has_rev or not isinstance(rev, str) or not rev or any(ch.isspace() for ch in rev):
        raise RuntimeError(f"missing or invalid revision in {path}: {rev!r}")
    if not has_down:
        raise RuntimeError(f"missing down_revision in {path}")

    parents = []
    if down is None:
        pass
    elif isinstance(down, str):
        if not down:
            raise RuntimeError(f"empty down_revision in {path}")
        parents.append(down)
    elif isinstance(down, (tuple, list)):
        if not down:
            raise RuntimeError(f"empty down_revision in {path}")
        for item in down:
            if not isinstance(item, str) or not item:
                raise RuntimeError(f"invalid down_revision entry in {path}: {down!r}")
            parents.append(item)
    else:
        raise RuntimeError(f"invalid down_revision in {path}: {down!r}")

    return rev, parents


def _chain_head(label: str, directory: str) -> str:
    proc = subprocess.run(
        ["git", "ls-tree", "-z", commit, f"{directory}/"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git ls-tree failed for {commit}:{directory}: {proc.stderr.strip()}")

    paths = []
    for entry in proc.stdout.split("\0"):
        if not entry or "\t" not in entry:
            continue
        meta, path = entry.split("\t", 1)
        parts = meta.split()
        if len(parts) < 2 or parts[1] != "blob":
            continue
        basename = path.rsplit("/", 1)[-1]
        # Alembic's own filter (1.18): it skips __init__* and editor lock files (.#*).
        if not basename.endswith(".py") or basename.startswith(("__init__", ".#")):
            continue
        paths.append(path)

    if not paths:
        raise RuntimeError(f"{label} chain ({directory}) in {commit} has no migration files")

    revs = {}
    rev_downs = {}
    parents = set()
    for path in sorted(paths):
        try:
            show = subprocess.run(
                ["git", "show", f"{commit}:{path}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        except UnicodeDecodeError as exc:
            raise RuntimeError(f"cannot decode {commit}:{path} as UTF-8: {exc}") from exc
        if show.returncode != 0:
            raise RuntimeError(f"git show failed for {commit}:{path}: {show.stderr.strip()}")
        rev, down_list = _extract_rev_and_down(show.stdout, path)
        if rev in revs:
            raise RuntimeError(
                f"duplicate revision {rev!r} in {label} chain ({revs[rev]} and {path})"
            )
        revs[rev] = path
        rev_downs[rev] = down_list
        parents.update(down_list)

    missing = sorted(parents - set(revs))
    if missing:
        details = ", ".join(
            f"{m} (referenced by {', '.join(sorted(revs[r] for r, ds in rev_downs.items() if m in ds))})"
            for m in missing
        )
        raise RuntimeError(
            f"{label} chain ({directory}) in {commit} has missing down_revision(s): {details}"
        )

    heads = sorted(set(revs) - parents)
    if len(heads) != 1:
        raise RuntimeError(
            f"{label} chain ({directory}) in {commit} expected 1 head, found {len(heads)}: {', '.join(heads) or 'none'}"
        )

    head = heads[0]
    state = {}  # 1 = visiting, 2 = visited
    stack = [(head, 0)]
    while stack:
        node, idx = stack[-1]
        if idx == 0:
            state[node] = 1
        downs = rev_downs[node]
        if idx < len(downs):
            stack[-1] = (node, idx + 1)
            parent = downs[idx]
            pstate = state.get(parent, 0)
            if pstate == 1:
                raise RuntimeError(
                    f"{label} chain ({directory}) in {commit} has incomplete migration graph (图不完整): cycle involving {node} -> {parent}"
                )
            if pstate == 0:
                stack.append((parent, 0))
        else:
            state[node] = 2
            stack.pop()

    unreachable = sorted(set(revs) - set(state))
    if unreachable:
        details = ", ".join(f"{r} ({revs[r]})" for r in unreachable)
        raise RuntimeError(
            f"{label} chain ({directory}) in {commit} has incomplete migration graph (图不完整): unreachable revision(s): {details}"
        )

    roots = sorted(r for r, ds in rev_downs.items() if not ds)
    if len(roots) != 1:
        details = ", ".join(f"{r} ({revs[r]})" for r in roots) or "none"
        raise RuntimeError(
            f"{label} chain ({directory}) in {commit} expected 1 root (down_revision = None), found {len(roots)}: {details}"
        )

    return head


try:
    result = [_chain_head(label, directory) for label, directory in chains]
except RuntimeError as exc:
    print(f"ERROR: target_migration_heads: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"{result[0]} {result[1]}")
PY
}

# True when pre-deploy DB revisions for both chains already match the unique heads in $1.
# Returns 1 (pending migrations) when either DB revision is unreadable or differs.
no_pending_migrations() {
  local commit="${1:-}" heads="" target_dalton="" target_upstream=""
  [ -n "$commit" ] || return 1
  heads=$(target_migration_heads "$commit") || return 1
  IFS=' ' read -r target_dalton target_upstream <<<"$heads"
  if [ -z "${PRE_FF_DALTON_REV:-}" ] || [ -z "${PRE_FF_UPSTREAM_REV:-}" ]; then
    echo "WARN: pre-deploy DB version unreadable (dalton=${PRE_FF_DALTON_REV:-未能读取}, upstream=${PRE_FF_UPSTREAM_REV:-未能读取}); assuming pending migrations" >&2
    return 1
  fi
  [ "$PRE_FF_DALTON_REV" = "$target_dalton" ] && [ "$PRE_FF_UPSTREAM_REV" = "$target_upstream" ]
}

# Compare pre-deploy DB revisions against HEAD (or $1) migration heads to set MIGRATING_DEPLOY.
# Unreadable DB revisions are treated as migrating (MIGRATING_DEPLOY=1).
compute_migrating_deploy() {
  local commit="${1:-HEAD}" heads="" target_dalton="" target_upstream=""
  if ! heads=$(target_migration_heads "$commit"); then
    MIGRATING_DEPLOY=1
    return 1
  fi
  IFS=' ' read -r target_dalton target_upstream <<<"$heads"
  if [ -z "${PRE_FF_DALTON_REV:-}" ] || [ -z "${PRE_FF_UPSTREAM_REV:-}" ]; then
    echo "WARN: pre-deploy DB version unreadable (dalton=${PRE_FF_DALTON_REV:-未能读取}, upstream=${PRE_FF_UPSTREAM_REV:-未能读取}); treating as migrating deploy" >&2
    MIGRATING_DEPLOY=1
    return 0
  fi
  if [ "$PRE_FF_DALTON_REV" != "$target_dalton" ] || [ "$PRE_FF_UPSTREAM_REV" != "$target_upstream" ]; then
    MIGRATING_DEPLOY=1
  else
    MIGRATING_DEPLOY=0
  fi
  return 0
}

# Ensure Everflow production .env declares DALTON_HOST_PLATFORM=macos.
# When the key is missing or empty, writes DALTON_HOST_PLATFORM=macos atomically
# (preserving existing permissions) and forces RECREATE=1 so docker compose
# recreates the backend container with the updated .env snapshot (§5.2 E16).
# Rejects any explicit non-macos value because Everflow production is fixed on macOS.
ensure_everflow_host_platform_env() {
  local env_file="${1:-${REPO_ROOT}/.env}"
  local cur_val="" normalized="" tmp_file="" orig_mode=""
  [ -f "$env_file" ] || return 0
  cur_val=$(awk '
    /^[[:space:]]*(export[[:space:]]+)?DALTON_HOST_PLATFORM[[:space:]]*=/ {
      line = $0
      sub(/^[[:space:]]*(export[[:space:]]+)?DALTON_HOST_PLATFORM[[:space:]]*=[[:space:]]*/, "", line)
      if (line ~ /^"/) {
        sub(/^"/, "", line); sub(/".*$/, "", line)
      } else if (line ~ /^'\''/) {
        sub(/^'\''/, "", line); sub(/'\''.*$/, "", line)
      } else {
        sub(/[[:space:]]*#.*$/, "", line)
        sub(/[[:space:]]+$/, "", line)
      }
      val = line
    }
    END { print val }
  ' "$env_file")
  normalized=$(printf '%s' "$cur_val" | tr '[:upper:]' '[:lower:]')
  if [ "$normalized" = "macos" ]; then
    return 0
  fi
  if [ -n "$normalized" ]; then
    echo "ERROR: Everflow production requires DALTON_HOST_PLATFORM=macos in ${env_file}, got '${cur_val}'" >&2
    return 1
  fi
  orig_mode=$(stat -f '%Lp' "$env_file" 2>/dev/null || stat -c '%a' "$env_file" 2>/dev/null || echo "600")
  tmp_file=$(mktemp "$(dirname "$env_file")/.env.hostplat.XXXXXX") || return 1
  awk '
    BEGIN { replaced = 0 }
    /^[[:space:]]*(export[[:space:]]+)?DALTON_HOST_PLATFORM[[:space:]]*=/ {
      if (!replaced) {
        print "DALTON_HOST_PLATFORM=macos"
        replaced = 1
      }
      next
    }
    { print $0 }
    END {
      if (!replaced) {
        print "DALTON_HOST_PLATFORM=macos"
      }
    }
  ' "$env_file" > "$tmp_file" || { rm -f "$tmp_file"; return 1; }
  chmod "$orig_mode" "$tmp_file" 2>/dev/null || chmod 600 "$tmp_file"
  mv -f "$tmp_file" "$env_file" || { rm -f "$tmp_file"; return 1; }
  echo "env: wrote DALTON_HOST_PLATFORM=macos to ${env_file}; enabling backend recreate"
  RECREATE=1
  return 0
}

_acquire_deploy_lock() {
  local lock_file="${DEPLOY_LOCK_FILE}" lock_dir="" py=""
  if [ -z "$lock_file" ]; then
    echo "ERROR: DEPLOY_LOCK_FILE is empty" >&2
    return 1
  fi
  if [ -f "${lock_file}.rollback-failed" ]; then
    echo "ERROR: deploy blocked because previous first-releases-rollout rollback failed (${lock_file}.rollback-failed):" >&2
    cat "${lock_file}.rollback-failed" >&2 2>/dev/null || true
    echo "Resolve edge recovery manually and remove ${lock_file}.rollback-failed before re-running deploy." >&2
    return 1
  fi
  lock_dir=$(dirname "$lock_file")
  if ! mkdir -p "$lock_dir" 2>/dev/null; then
    echo "ERROR: failed to create deploy lock directory ${lock_dir}" >&2
    return 1
  fi
  py=$(_resolve_deploy_python) || return 1
  if ! exec 9<>"$lock_file"; then
    echo "ERROR: failed to open deploy lock file ${lock_file}" >&2
    return 1
  fi
  if ! "$py" - "$lock_file" "$$" "$REPO_ROOT" <<'PY'
import datetime
import fcntl
import os
import sys

lock_file, pid_str, repo_root = sys.argv[1:4]
sentinel = f"{lock_file}.rollback-failed"
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    holder = ""
    try:
        with open(lock_file, encoding="utf-8") as fh:
            holder = fh.read().strip()
    except OSError:
        holder = ""
    detail = f" ({holder})" if holder else ""
    print(f"ERROR: another deploy is already running; lock held on {lock_file}{detail}", file=sys.stderr)
    sys.exit(1)

# Re-check rollback failure state AFTER acquiring flock so a concurrent process cannot
# slip past the pre-lock check while a failed rollback is writing the failure marker.
existing = os.pread(9, 1024, 0).decode("utf-8", errors="replace")
if os.path.exists(sentinel) or "state=rollback_failed" in existing:
    print(f"ERROR: deploy blocked because previous first-releases-rollout rollback failed ({sentinel}):", file=sys.stderr)
    if os.path.exists(sentinel):
        try:
            print(open(sentinel, encoding="utf-8").read().strip(), file=sys.stderr)
        except OSError:
            pass
    elif existing.strip():
        print(existing.strip(), file=sys.stderr)
    print(f"Resolve edge recovery manually and remove {sentinel} (and clear {lock_file}) before re-running deploy.", file=sys.stderr)
    sys.exit(1)

os.ftruncate(9, 0)
os.lseek(9, 0, os.SEEK_SET)
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
os.write(9, f"pid={pid_str} started={ts} repo={repo_root}\n".encode("utf-8"))
os.fsync(9)
PY
  then
    exec 9>&-
    return 1
  fi
  DEPLOY_LOCK_OWNED=1
  export DALTON_DEPLOY_LOCK_HELD_FD=9
  export DALTON_DEPLOY_LOCK_FILE="$lock_file"
  export DALTON_DEPLOY_LOCK_OWNER_PID="$$"
  return 0
}

_mark_rollback_failed_lock() {
  local pre_ff_sha="${1:-unknown}" reason="${2:-rollback_failed}"
  local lock_file="${DEPLOY_LOCK_FILE}" sentinel="" rollback_dir="" ts="" py="" payload="" write_err=0
  sentinel="${lock_file}.rollback-failed"
  rollback_dir="${FIRST_ROLLOUT_ROLLBACK_DIR:-$(_first_rollout_rollbacks_root)}"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
  payload=$(printf 'state=rollback_failed\ntimestamp=%s\npid=%s\nrepo=%s\npre_ff=%s\nreason=%s\nrollback_dir=%s\n' \
    "$ts" "$$" "$REPO_ROOT" "$pre_ff_sha" "$reason" "$rollback_dir")
  # Persist rollback-failed state directly into the already-open lock FD 9 first (resilient
  # even if creating a new sentinel file fails), then also write the .rollback-failed sentinel.
  py=$(_resolve_deploy_python 2>/dev/null || true)
  if [ -n "$py" ]; then
    if ! "$py" - "$payload" <<'PY'
import os, sys
data = (sys.argv[1] + "\n").encode("utf-8")
os.ftruncate(9, 0)
os.lseek(9, 0, os.SEEK_SET)
os.write(9, data)
os.fsync(9)
PY
    then
      echo "ERROR: failed to persist rollback_failed state to lock fd 9 (${lock_file})" >&2
      write_err=1
    fi
  fi
  if ! mkdir -p "$(dirname "$sentinel")" 2>/dev/null || ! printf '%s\n' "$payload" > "$sentinel"; then
    echo "ERROR: failed to write rollback failure sentinel ${sentinel}; manual recovery required before unlocking ${lock_file}" >&2
    write_err=1
  fi
  return "$write_err"
}

_print_first_rollout_manual_recovery_steps() {
  local pre_ff_sha="${1:-unknown}" rollback_dir="" sentinel="" proj="" proj_flag=""
  rollback_dir="${FIRST_ROLLOUT_ROLLBACK_DIR:-$(_first_rollout_rollbacks_root)}"
  sentinel="${DEPLOY_LOCK_FILE}.rollback-failed"
  if [ -s "${rollback_dir}/project.name" ]; then
    proj=$(cat "${rollback_dir}/project.name" 2>/dev/null || true)
  fi
  [ -n "$proj" ] && proj_flag="-p ${proj} "
  echo "FIRST-ROLLOUT ROLLBACK FAILED — 保留故障锁 (${sentinel} 与 ${DEPLOY_LOCK_FILE}) 及已解析回退快照 (${rollback_dir}/resolved-compose.yml)，请按以下步骤人工恢复："
  echo "1. 检查 edge 容器状态与日志："
  echo "     docker ps -a --filter name=dalton-langalpha-edge-1"
  echo "     docker logs --tail 100 dalton-langalpha-edge-1"
  echo "2. 在部署检出根目录 (${REPO_ROOT}) 使用冻结的解析后快照把 edge 挂回 web/dist（web/dist 保持完整）："
  echo "     cd ${REPO_ROOT}"
  echo "     docker compose ${proj_flag}--project-directory ${REPO_ROOT} -f ${rollback_dir}/resolved-compose.yml up -d --no-deps --force-recreate edge"
  echo "3. 验证旧站点 HTTP 正常后，移除故障锁标记与锁文件再继续后续部署："
  echo "     curl -fsS -m3 http://127.0.0.1:${EDGE_PORT}/ >/dev/null && echo edge-ok"
  echo "     rm -f ${sentinel} ${DEPLOY_LOCK_FILE}"
}

_verify_inherited_deploy_lock() {
  local lock_file="${DEPLOY_LOCK_FILE}" py=""
  if [ "${DALTON_DEPLOY_LOCK_HELD_FD:-}" != "9" ] || [ -z "$lock_file" ]; then
    echo "ERROR: re-executed deploy missing inherited deploy lock (DALTON_DEPLOY_LOCK_HELD_FD=${DALTON_DEPLOY_LOCK_HELD_FD:-unset})" >&2
    return 1
  fi
  if [ -z "${DALTON_DEPLOY_LOCK_OWNER_PID:-}" ]; then
    echo "ERROR: re-executed deploy missing DALTON_DEPLOY_LOCK_OWNER_PID from parent script; aborting resume — please re-run deploy_main.sh directly." >&2
    return 1
  fi
  py=$(_resolve_deploy_python) || return 1
  "$py" - "$lock_file" "${DALTON_DEPLOY_LOCK_OWNER_PID}" <<'PY'
import os
import sys

lock_file, owner_raw = sys.argv[1:3]
try:
    owner_pid = int(owner_raw)
    os.kill(owner_pid, 0)
except Exception as exc:
    print(f"ERROR: inherited DALTON_DEPLOY_LOCK_OWNER_PID={owner_raw!r} invalid: {exc}; please re-run deploy_main.sh directly.", file=sys.stderr)
    sys.exit(1)
try:
    fd_st = os.fstat(9)
    path_st = os.stat(lock_file)
except OSError as exc:
    print(f"ERROR: inherited deploy lock fd 9 invalid for {lock_file}: {exc}", file=sys.stderr)
    sys.exit(1)
if (fd_st.st_dev, fd_st.st_ino) != (path_st.st_dev, path_st.st_ino):
    print(f"ERROR: inherited deploy lock fd 9 does not match {lock_file}", file=sys.stderr)
    sys.exit(1)
content = os.pread(9, 1024, 0).decode("utf-8", errors="replace")
if f"pid={owner_pid}" not in content:
    print(f"ERROR: inherited deploy lock fd 9 does not match DALTON_DEPLOY_LOCK_OWNER_PID={owner_pid}; please re-run deploy_main.sh directly.", file=sys.stderr)
    sys.exit(1)
PY
}

release_deploy_lock() {
  if [ "${DEPLOY_LOCK_OWNED:-0}" = "1" ]; then
    exec 9>&-
    DEPLOY_LOCK_OWNED=0
    unset DALTON_DEPLOY_LOCK_HELD_FD
    unset DALTON_DEPLOY_LOCK_OWNER_PID
  fi
  return 0
}

_ensure_deploy_lock_held() {
  _ENSURED_LOCAL_LOCK=0
  if [ "${DEPLOY_LOCK_OWNED:-0}" = "1" ]; then
    return 0
  fi
  if [ "${DALTON_DEPLOY_LOCK_HELD_FD:-}" = "9" ] && _verify_inherited_deploy_lock 2>/dev/null; then
    return 0
  fi
  _acquire_deploy_lock || return 1
  _ENSURED_LOCAL_LOCK=1
  return 0
}

_file_sha256() {
  local f="${1:-}" out="" py=""
  [ -n "$f" ] && [ -f "$f" ] && [ -r "$f" ] || return 1
  if command -v shasum >/dev/null 2>&1; then
    out=$(shasum -a 256 "$f" 2>/dev/null) || return 1
    out="${out%%[[:space:]]*}"
    [ -n "$out" ] || return 1
    printf '%s' "$out"
    return 0
  elif command -v sha256sum >/dev/null 2>&1; then
    out=$(sha256sum "$f" 2>/dev/null) || return 1
    out="${out%%[[:space:]]*}"
    [ -n "$out" ] || return 1
    printf '%s' "$out"
    return 0
  fi
  py=$(_resolve_deploy_python) || return 1
  "$py" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest(), end="")' "$f"
}

_write_deploy_reexec_state() {
  local state_file="${1:-}"
  [ -n "$state_file" ] || return 1
  {
    printf 'DEPLOY_RESUME_PROTOCOL=%s\n' "${DEPLOY_RESUME_PROTOCOL}"
    printf 'DEPLOY_FF_DONE=%s\n' "${DEPLOY_FF_DONE:-1}"
    printf 'PRE_FF=%s\n' "${PRE_FF:-}"
    printf 'TARGET=%s\n' "${TARGET:-}"
    printf 'PRE_FF_DALTON_REV=%s\n' "${PRE_FF_DALTON_REV:-}"
    printf 'PRE_FF_UPSTREAM_REV=%s\n' "${PRE_FF_UPSTREAM_REV:-}"
    printf 'DEPLOY_TARGET_HEADS=%s\n' "${DEPLOY_TARGET_HEADS:-}"
    printf 'DEPLOY_BACKUP_STATUS=%s\n' "${DEPLOY_BACKUP_STATUS:-}"
    printf 'HAD_PREEXISTING_GATEWAY_PENDING=%s\n' "${HAD_PREEXISTING_GATEWAY_PENDING:-0}"
    printf 'PREV_DEPLOYED_GATEWAY_TREE=%s\n' "${PREV_DEPLOYED_GATEWAY_TREE:-}"
    printf 'TARGET_GATEWAY_TREE=%s\n' "${TARGET_GATEWAY_TREE:-}"
    printf 'PRE_FF_GATEWAY_TREE=%s\n' "${PRE_FF_GATEWAY_TREE:-}"
    printf 'DEPLOY_GATEWAY=%s\n' "${DEPLOY_GATEWAY:-0}"
  } > "$state_file"
}

_load_deploy_reexec_state() {
  local state_file="${1:-}" line="" key="" val="" state_proto=""
  if [ -z "$state_file" ] || [ ! -f "$state_file" ] || [ ! -r "$state_file" ]; then
    echo "ERROR: re-exec state file missing or unreadable: ${state_file:-unset}" >&2
    return 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    [ -n "$line" ] || continue
    case "$line" in
      *=*) ;;
      *)
        echo "ERROR: malformed line in re-exec state file: ${line}" >&2
        return 1
        ;;
    esac
    key="${line%%=*}"
    val="${line#*=}"
    case "$key" in
      DEPLOY_RESUME_PROTOCOL) state_proto="$val" ;;
      DEPLOY_FF_DONE) DEPLOY_FF_DONE="$val" ;;
      PRE_FF) PRE_FF="$val" ;;
      TARGET) TARGET="$val" ;;
      PRE_FF_DALTON_REV) PRE_FF_DALTON_REV="$val" ;;
      PRE_FF_UPSTREAM_REV) PRE_FF_UPSTREAM_REV="$val" ;;
      DEPLOY_TARGET_HEADS) DEPLOY_TARGET_HEADS="$val" ;;
      DEPLOY_BACKUP_STATUS) DEPLOY_BACKUP_STATUS="$val" ;;
      HAD_PREEXISTING_GATEWAY_PENDING) HAD_PREEXISTING_GATEWAY_PENDING="$val" ;;
      PREV_DEPLOYED_GATEWAY_TREE) PREV_DEPLOYED_GATEWAY_TREE="$val" ;;
      TARGET_GATEWAY_TREE) TARGET_GATEWAY_TREE="$val" ;;
      PRE_FF_GATEWAY_TREE) PRE_FF_GATEWAY_TREE="$val" ;;
      DEPLOY_GATEWAY) DEPLOY_GATEWAY="$val" ;;
      *)
        echo "ERROR: unknown key in re-exec state file: ${key}" >&2
        return 1
        ;;
    esac
  done < "$state_file"
  if [ "$state_proto" != "$DEPLOY_RESUME_PROTOCOL" ]; then
    echo "ERROR: re-exec state protocol '${state_proto:-missing}' does not match expected '${DEPLOY_RESUME_PROTOCOL}'" >&2
    return 1
  fi
  if [ "${DEPLOY_FF_DONE:-0}" != "1" ] || [ -z "${PRE_FF:-}" ] || [ -z "${TARGET:-}" ] || [ -z "${DEPLOY_TARGET_HEADS:-}" ]; then
    echo "ERROR: incomplete re-exec state file ${state_file} (ff_done=${DEPLOY_FF_DONE:-0}, pre_ff=${PRE_FF:-}, target=${TARGET:-})" >&2
    return 1
  fi
  return 0
}

validate_deploy_resume_handshake() {
  local state_file="${1:-}"
  if [ -z "$state_file" ]; then
    echo "ERROR: --validate-resume requires a state file path" >&2
    return 2
  fi
  shift
  if [ "${1:-}" = "--" ]; then
    shift
  fi
  _load_deploy_reexec_state "$state_file" || return 1
  parse_deploy_args "$@" || return $?
  return 0
}

# Returns: 0 = changed (re-exec needed), 1 = unchanged (no re-exec), 2 = hash read error.
_deploy_scripts_changed_in_target() {
  local running_deploy_sha="${1:-}" wait_idle_in_script_dir="${2:-0}" running_wait_idle_sha="${3:-}"
  local repo_deploy="${REPO_ROOT}/scripts/dalton/ops/prod/deploy_main.sh"
  local repo_wait_idle="${REPO_ROOT}/scripts/dalton/ops/prod/wait_backend_idle.sh"
  local target_deploy_sha="" target_wait_idle_sha=""
  [ -f "$repo_deploy" ] || return 1
  if [ -z "$running_deploy_sha" ]; then
    echo "ERROR: missing startup sha256 for running deploy_main.sh" >&2
    return 2
  fi
  if ! target_deploy_sha=$(_file_sha256 "$repo_deploy"); then
    echo "ERROR: failed to compute sha256 for ${repo_deploy}" >&2
    return 2
  fi
  if [ "$running_deploy_sha" != "$target_deploy_sha" ]; then
    return 0
  fi
  if [ "$wait_idle_in_script_dir" = "1" ] && [ -f "$repo_wait_idle" ]; then
    if [ -z "$running_wait_idle_sha" ]; then
      echo "ERROR: missing startup sha256 for running wait_backend_idle.sh" >&2
      return 2
    fi
    if ! target_wait_idle_sha=$(_file_sha256 "$repo_wait_idle"); then
      echo "ERROR: failed to compute sha256 for ${repo_wait_idle}" >&2
      return 2
    fi
    if [ "$running_wait_idle_sha" != "$target_wait_idle_sha" ]; then
      return 0
    fi
  elif [ "$WAIT_IDLE_SCRIPT" = "${SCRIPT_DIR}/wait_backend_idle.sh" ] && [ ! -f "$WAIT_IDLE_SCRIPT" ] && [ -f "$repo_wait_idle" ]; then
    return 0
  fi
  return 1
}

_load_backend_busy_threads_sql() {
  local sql_file="" candidate=""
  for candidate in \
    "${DALTON_BACKEND_BUSY_THREADS_SQL:-}" \
    "${SCRIPT_DIR}/backend_busy_threads.sql" \
    "${REPO_ROOT}/scripts/dalton/ops/prod/backend_busy_threads.sql" \
    "$(dirname "$WAIT_IDLE_SCRIPT")/backend_busy_threads.sql"; do
    if [ -n "$candidate" ] && [ -f "$candidate" ]; then
      sql_file="$candidate"
      break
    fi
  done
  if [ -z "$sql_file" ]; then
    echo "ERROR: backend_busy_threads.sql not found" >&2
    return 1
  fi
  tr '\n' ' ' < "$sql_file" | sed 's/  */ /g; s/^ //; s/ $//'
}

_pre_restart_busy_sql() {
  local busy_threads_sql=""
  busy_threads_sql="$(_load_backend_busy_threads_sql)" || return 1
  printf "SELECT (%s) + (SELECT count(*) FROM subagent_runs WHERE status='in_progress')" "$busy_threads_sql"
}

_pre_restart_busy() {
  local total_sql=""
  total_sql="$(_pre_restart_busy_sql)" || return 0
  [ "$(docker exec "${WAIT_BACKEND_IDLE_POSTGRES_CONTAINER:-dalton-langalpha-postgres-1}" psql -U postgres -d postgres -Atc "$total_sql")" != "0" ]
}

_stop_on_reexec_handshake_failure() {
  local msg="${1:-re-exec handshake failed}" cur_head="" cur_dalton_rev="" cur_upstream_rev=""
  echo "ERROR: ${msg}; stopping without executing target deploy_main.sh (services and frontend untouched)" >&2
  cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
  cur_dalton_rev=$(get_dalton_alembic_version || true)
  cur_upstream_rev=$(get_upstream_alembic_version || true)
  _print_manual_revert_instructions "$cur_head" "$cur_dalton_rev" "$cur_upstream_rev"
  release_deploy_lock || true
  trap - INT TERM
  return 1
}

run_deploy_main() {
  local LOG before after out build_rc cur reused target_heads post_ff_head pre_restart_heads gw_diff_rc gw_rev_rc gw_tree_write_ok=0
  local running_deploy_sha="" running_wait_idle_sha="" wait_idle_in_script_dir=0 scripts_changed_rc=0
  local reexec_tmp="" state_file="" reexec_rc=0 child_proto="" short_target="" expected_build="" expected_release="" rel_dir="" cur_fe_rel=""
  local -a orig_args=("$@")

  # Anti-reentry marker and state file must always be paired.
  if { [ -n "${DALTON_DEPLOY_REEXECED:-}" ] && [ -z "${DALTON_DEPLOY_STATE_FILE:-}" ]; } \
     || { [ -z "${DALTON_DEPLOY_REEXECED:-}" ] && [ -n "${DALTON_DEPLOY_STATE_FILE:-}" ]; }; then
    echo "ERROR: DALTON_DEPLOY_REEXECED and DALTON_DEPLOY_STATE_FILE must be set together (got reexeced='${DALTON_DEPLOY_REEXECED:-}', state_file='${DALTON_DEPLOY_STATE_FILE:-}')" >&2
    exit 2
  fi

  parse_deploy_args "$@" || exit $?
  cd "$REPO_ROOT" || exit 1
  ACTIVE_FRONTEND_RELEASE_DIR=""
  NEW_FRONTEND_RELEASE_ID=""
  PREV_FRONTEND_RELEASE_ID=""
  FRONTEND_SWITCHED=0
  FRONTEND_REVERT_STATE="not_switched"
  ACTIVE_GATEWAY_STAGING_DIR=""
  ACTIVE_GATEWAY_IMPORT_TMP=""
  STAGED_GATEWAY_RELEASE_ID=""
  ACTIVE_REEXEC_CHILD_PID=""
  ACTIVE_REEXEC_TMP_DIR=""
  DEPLOY_LOCK_OWNED=0
  trap '_on_deploy_interrupt_signal INT' INT
  trap '_on_deploy_interrupt_signal TERM' TERM

  if [ -n "${DALTON_DEPLOY_REEXECED:-}" ] && [ -n "${DALTON_DEPLOY_STATE_FILE:-}" ]; then
    _verify_inherited_deploy_lock || exit 1
    _load_deploy_reexec_state "$DALTON_DEPLOY_STATE_FILE" || exit 1
    if [ "$DALTON_DEPLOY_REEXECED" != "$TARGET" ]; then
      echo "ERROR: DALTON_DEPLOY_REEXECED (${DALTON_DEPLOY_REEXECED}) does not match state TARGET (${TARGET})" >&2
      exit 1
    fi
    post_ff_head=$(git rev-parse HEAD 2>/dev/null || true)
    if [ "$post_ff_head" != "$TARGET" ]; then
      echo "ERROR: resumed HEAD (${post_ff_head:-unknown}) does not match TARGET (${TARGET})" >&2
      exit 1
    fi
    target_heads="$DEPLOY_TARGET_HEADS"
    MIGRATING_DEPLOY=1
    BACKEND_TOUCHED=0
    CRAWLER_TOUCHED=0
    EDGE_TOUCHED=0
    BRIDGE_TOUCHED=0
    GATEWAY_SWITCHED=0
    GATEWAY_BUILD_FAILED=0
    GATEWAY_REVERT_STATE="not_switched"
    GATEWAY_REVERT_KICKSTART_OK=0
    GATEWAY_REVERT_HEALTH_OK=0
    GATEWAY_SWITCH_RESTORE_FAILED=0
    ACTUAL_RESTORED_GATEWAY_TREE=""
    echo "$(date '+%T') resumed re-executed deploy script for ${TARGET} (protocol=${DEPLOY_RESUME_PROTOCOL}, PRE_FF=${PRE_FF}, backup=${DEPLOY_BACKUP_STATUS:-done})"
  else
    _acquire_deploy_lock || exit 1
    if [ -f "$SCRIPT_FILE" ]; then
      if ! running_deploy_sha=$(_file_sha256 "$SCRIPT_FILE"); then
        echo "ERROR: failed to compute startup sha256 for ${SCRIPT_FILE}" >&2
        release_deploy_lock || true
        exit 1
      fi
    fi
    if [ -f "$WAIT_IDLE_SCRIPT" ] && [ "$(cd "$(dirname "$WAIT_IDLE_SCRIPT")" 2>/dev/null && pwd)" = "$SCRIPT_DIR" ]; then
      wait_idle_in_script_dir=1
      if ! running_wait_idle_sha=$(_file_sha256 "$WAIT_IDLE_SCRIPT"); then
        echo "ERROR: failed to compute startup sha256 for ${WAIT_IDLE_SCRIPT}" >&2
        release_deploy_lock || true
        exit 1
      fi
    fi
    cleanup_old_container_logs
    TARGET=""
    DEPLOY_BACKUP_STATUS=""
    DEPLOY_TARGET_HEADS=""
    DEPLOY_FF_DONE=0
    bash "$WAIT_IDLE_SCRIPT" || { echo "still busy, abort"; release_deploy_lock || true; exit 1; }
    PRE_FF_DALTON_REV=$(get_dalton_alembic_version || true)
    PRE_FF_UPSTREAM_REV=$(get_upstream_alembic_version || true)
    echo "pre-deploy migrations: dalton=${PRE_FF_DALTON_REV:-未能读取}, upstream=${PRE_FF_UPSTREAM_REV:-未能读取}"
    if [ -z "$PRE_FF_DALTON_REV" ] || [ -z "$PRE_FF_UPSTREAM_REV" ]; then
      echo "pre-deploy migrations: DB version unreadable; treating as migrating deploy (fresh backup required, no auto-revert)"
    fi
    git fetch -q origin || { echo "git fetch failed, abort"; release_deploy_lock || true; exit 1; }
    TARGET=$(git rev-parse --verify origin/dalton/main 2>/dev/null) || { echo "cannot resolve origin/dalton/main, abort"; release_deploy_lock || true; exit 1; }
    echo "deploy target: ${TARGET} ($(git log -1 --oneline "$TARGET" 2>/dev/null || printf '%s' "$TARGET"))"
    if ! git merge-base --is-ancestor HEAD "$TARGET" 2>/dev/null; then
      echo "ERROR: 生产检出上有不在 origin/dalton/main 里的提交，先处理再部署 (HEAD=$(git rev-parse --short=9 HEAD 2>/dev/null || echo unknown), TARGET=${TARGET})" >&2
      release_deploy_lock || true
      exit 1
    fi
    # Transitional guard BEFORE fast-forward: when running edge still mounts legacy /srv
    # while TARGET requires /srv-releases, stop cleanly with exit code 3 leaving HEAD,
    # Compose, Caddyfile, working tree, and all services untouched.
    if _edge_live_mounts_any_legacy_srv && _commit_requires_releases_mount "$TARGET"; then
      local cur_head_pre_ff="" rollout_hint="bash scripts/dalton/ops/prod/deploy_main.sh --first-releases-rollout"
      cur_head_pre_ff=$(git rev-parse HEAD 2>/dev/null || true)
      if [ -n "$cur_head_pre_ff" ] && _commit_mounts_legacy_dist "$cur_head_pre_ff"; then
        rollout_hint="${rollout_hint} --pre-ff ${cur_head_pre_ff}"
      fi
      echo "ERROR: running dalton-langalpha-edge-1 still mounts legacy web/dist on /srv while TARGET (${TARGET}) requires /srv-releases; aborting before fast-forward so HEAD (${cur_head_pre_ff:-unknown}), Compose, Caddyfile, and all services stay untouched. Fast-forward to ${TARGET} and run '${rollout_hint}' for the one-time transition." >&2
      release_deploy_lock || true
      trap - INT TERM
      exit 3
    fi
    check_clean_migration_dirs || { release_deploy_lock || true; exit 1; }
    target_heads=$(target_migration_heads "$TARGET") || { echo "ERROR: failed to resolve unique migration heads for target ${TARGET}, abort" >&2; release_deploy_lock || true; exit 1; }
    echo "target migration heads: ${target_heads}"
    LOG="${DALTON_PG_BACKUP_LOG:-$HOME/Library/Logs/dalton-pg-backup.log}"; before=$(tail -1 "$LOG" 2>/dev/null)
    local cur_head_for_backup=""
    cur_head_for_backup=$(git rev-parse HEAD 2>/dev/null || true)
    local backup_wait_state_file="${DALTON_DEPLOY_BACKUP_WAIT_STATE_FILE:-$HOME/Library/Logs/.dalton-deploy-backup-wait.state}"
    local backup_next_id_file="${DALTON_PG_BACKUP_NEXT_ID_FILE:-$HOME/Library/Logs/.dalton-pg-backup.next-set-id}"
    local backup_active_id_file="${DALTON_PG_BACKUP_ACTIVE_ID_FILE:-$HOME/Library/Logs/.dalton-pg-backup.active-set-id}"
    local tracked_wait_id="" tracked_wait_head="" tracked_wait_target="" tracked_wait_before=""
    local resumed_completed_backup="" resuming_active_wait=0
    if [ -f "$backup_wait_state_file" ] && [ ! -L "$backup_wait_state_file" ]; then
      while IFS= read -r _wline; do
        case "$_wline" in
          WAIT_BACKUP_ID=*) tracked_wait_id="${_wline#WAIT_BACKUP_ID=}" ;;
          WAIT_PRE_FF=*) tracked_wait_head="${_wline#WAIT_PRE_FF=}" ;;
          WAIT_TARGET=*) tracked_wait_target="${_wline#WAIT_TARGET=}" ;;
          WAIT_BEFORE_LINE=*) tracked_wait_before="${_wline#WAIT_BEFORE_LINE=}" ;;
        esac
      done < "$backup_wait_state_file"
      if [ -n "$tracked_wait_head" ] && [ "$tracked_wait_head" = "$cur_head_for_backup" ]; then
        local tracked_done_line=""
        if tracked_done_line=$(_find_completed_backup_for_wait "$LOG" "$tracked_wait_id" "$tracked_wait_before"); then
          case "$tracked_done_line" in
            *" ok "*)
              if _verify_completed_backup_artifact "$tracked_done_line"; then
                resumed_completed_backup="$tracked_done_line"
              else
                echo "backup: tracked completed backup (id=${tracked_wait_id:-tracked}) artifact is no longer available; taking a fresh backup"
                rm -f "$backup_wait_state_file" 2>/dev/null || true
                tracked_wait_id=""
                tracked_wait_before=""
              fi
              ;;
            *)
              rm -f "$backup_wait_state_file" 2>/dev/null || true
              tracked_wait_id=""
              tracked_wait_before=""
              ;;
          esac
        elif _pg_backup_job_running; then
          resuming_active_wait=1
        else
          rm -f "$backup_wait_state_file" 2>/dev/null || true
          tracked_wait_id=""
          tracked_wait_before=""
        fi
      else
        rm -f "$backup_wait_state_file" 2>/dev/null || true
        tracked_wait_id=""
        tracked_wait_before=""
      fi
    fi

    reused=""
    local reuse_verify_failed=0
    if [ -z "$resumed_completed_backup" ] && [ "$resuming_active_wait" -eq 0 ] && [ -n "$REUSE_BACKUP_MIN" ] && no_pending_migrations "$TARGET"; then
      reused=$(recent_ok_backup "$LOG" "$REUSE_BACKUP_MIN" "$(date +%s)") || reused=""
      if [ -n "$reused" ]; then
        local reuse_verify_err=""
        if ! reuse_verify_err=$(_verify_completed_backup_artifact "$reused" 2>&1); then
          reuse_verify_failed=1
          local reuse_reason=""
          reuse_reason=$(printf '%s\n' "$reuse_verify_err" | tr '\n' ' ' | sed -e 's/[[:space:]]*$//')
          echo "backup: cannot reuse candidate backup (artifact verification failed: ${reuse_reason:-unknown}); taking a fresh one"
          reused=""
        fi
      fi
    fi
    if [ -n "$resumed_completed_backup" ]; then
      DEPLOY_BACKUP_STATUS="fresh"
      rm -f "$backup_wait_state_file" 2>/dev/null || true
      echo "backup: resuming completed deploy backup (id=${tracked_wait_id:-tracked}): $resumed_completed_backup"
    elif [ -n "$reused" ]; then
      DEPLOY_BACKUP_STATUS="reused"
      echo "backup: reusing (no migrations to deploy, dump within ${REUSE_BACKUP_MIN} min): $reused"
    else
      if [ -n "$REUSE_BACKUP_MIN" ] && [ "$reuse_verify_failed" -eq 0 ]; then
        echo "backup: cannot reuse (migrations to deploy or no ok dump within ${REUSE_BACKUP_MIN} min); taking a fresh one"
      fi
      local backup_tag_file="${DALTON_PG_BACKUP_TAG_FILE:-$HOME/Library/Logs/.dalton-pg-backup.next-tag}"
      # Default wait covers lock phase (2h) + mirror phase (4h) + reconcile/publish margin (1h) = 7h (25200s).
      local _lock_wait_t="${DALTON_BACKUP_LOCK_TIMEOUT_S:-7200}"
      local _mirror_wait_t="${DALTON_BACKUP_MIRROR_TIMEOUT_S:-14400}"
      local _default_backup_wait_s=25200
      if [[ "$_lock_wait_t" =~ ^[0-9]+$ ]] && [[ "$_mirror_wait_t" =~ ^[0-9]+$ ]]; then
        _default_backup_wait_s=$(( _lock_wait_t + _mirror_wait_t + 3600 ))
      fi
      local backup_wait_s="${DALTON_DEPLOY_BACKUP_WAIT_S:-${_default_backup_wait_s}}"
      local backup_poll_s="${DALTON_DEPLOY_BACKUP_POLL_S:-5}"
      local backup_polls=1
      if [ "$backup_poll_s" -gt 0 ] 2>/dev/null; then
        backup_polls=$(( (backup_wait_s + backup_poll_s - 1) / backup_poll_s ))
      fi
      [ "$backup_polls" -lt 1 ] && backup_polls=1

      local wait_backup_id=""
      local wait_before_line="$before"
      if [ "$resuming_active_wait" -eq 1 ]; then
        wait_backup_id="$tracked_wait_id"
        wait_before_line="$tracked_wait_before"
        echo "backup: com.dalton.pg-backup is already running (resuming tracked wait id=${wait_backup_id:-unknown}); waiting without re-triggering"
      elif _pg_backup_job_running; then
        if [ -f "$backup_active_id_file" ] && [ ! -L "$backup_active_id_file" ]; then
          wait_backup_id="$(tr -d '[:space:]' < "$backup_active_id_file" 2>/dev/null || true)"
        fi
        _write_deploy_backup_wait_state "$backup_wait_state_file" "$wait_backup_id" "$cur_head_for_backup" "$TARGET" "$wait_before_line"
        echo "backup: com.dalton.pg-backup is already running (id=${wait_backup_id:-active}); waiting for it instead of triggering a duplicate run"
      else
        wait_backup_id="$(date +%Y%m%d-%H%M%S)-deploy-$$"
        mkdir -p "$(dirname "$backup_tag_file")" "$(dirname "$backup_next_id_file")" 2>/dev/null || true
        printf 'deploy\n' > "$backup_tag_file" 2>/dev/null || true
        printf '%s\n' "$wait_backup_id" > "$backup_next_id_file" 2>/dev/null || true
        _write_deploy_backup_wait_state "$backup_wait_state_file" "$wait_backup_id" "$cur_head_for_backup" "$TARGET" "$wait_before_line"
        launchctl kickstart "gui/$(id -u)/com.dalton.pg-backup"
      fi

      local backup_done=0
      local matched_bak_line=""
      for i in $(seq 1 "$backup_polls"); do
        if matched_bak_line=$(_find_completed_backup_for_wait "$LOG" "$wait_backup_id" "$wait_before_line"); then
          backup_done=1
          break
        fi
        sleep "$backup_poll_s"
      done  # default 7h (25200s): covers 2h lock phase + 4h object mirror + 1h reconcile/publish
      if [ "$backup_done" -eq 1 ]; then
        after="$matched_bak_line"
        echo "backup: $after"
        rm -f "$backup_wait_state_file" 2>/dev/null || true
        case "$after" in
          *" ok "*)
            if [ "$after" = "$wait_before_line" ]; then
              echo "no fresh backup, abort"; release_deploy_lock || true; exit 1
            fi
            if ! _verify_completed_backup_artifact "$after"; then
              echo "backup artifact unavailable, abort"; release_deploy_lock || true; exit 1
            fi
            ;;
          *)
            echo "backup failed, abort"; release_deploy_lock || true; exit 1
            ;;
        esac
        DEPLOY_BACKUP_STATUS="fresh"
      elif _pg_backup_job_running; then
        # Refresh tracked active ID if backup.sh published one after kickstart, and KEEP state file for retry.
        if [ -z "$wait_backup_id" ] && [ -f "$backup_active_id_file" ] && [ ! -L "$backup_active_id_file" ]; then
          wait_backup_id="$(tr -d '[:space:]' < "$backup_active_id_file" 2>/dev/null || true)"
          _write_deploy_backup_wait_state "$backup_wait_state_file" "$wait_backup_id" "$cur_head_for_backup" "$TARGET" "$wait_before_line"
        fi
        echo "backup: still running after ${backup_wait_s}s wait (id=${wait_backup_id:-active}; not failed; leaving running job intact and deferring deploy without re-triggering)"
        release_deploy_lock || true
        exit 75
      else
        rm -f "$backup_tag_file" "$backup_next_id_file" "$backup_wait_state_file" 2>/dev/null || true
        if ! matched_bak_line=$(_find_completed_backup_for_wait "$LOG" "$wait_backup_id" "$wait_before_line"); then
          echo "backup: no matching terminal record found for id=${wait_backup_id:-unknown} (last log line: $(tail -1 "$LOG" 2>/dev/null || true)); abort"
          release_deploy_lock || true
          exit 1
        fi
        after="$matched_bak_line"
        echo "backup: $after"
        case "$after" in
          *" ok "*)
            if [ "$after" = "$wait_before_line" ]; then
              echo "no fresh backup, abort"; release_deploy_lock || true; exit 1
            fi
            if ! _verify_completed_backup_artifact "$after"; then
              echo "backup artifact unavailable, abort"; release_deploy_lock || true; exit 1
            fi
            DEPLOY_BACKUP_STATUS="fresh"
            ;;
          *)
            echo "backup failed, abort"; release_deploy_lock || true; exit 1
            ;;
        esac
      fi
    fi
    # ff and staged builds only after the idle wait and backup, right before the restart, so the new
    # frontend never goes live against the old backend and running code is not swapped under a busy backend.
    PRE_FF=$(git rev-parse HEAD)
    MIGRATING_DEPLOY=1
    BACKEND_TOUCHED=0
    CRAWLER_TOUCHED=0
    EDGE_TOUCHED=0
    BRIDGE_TOUCHED=0
    GATEWAY_SWITCHED=0
    GATEWAY_BUILD_FAILED=0
    GATEWAY_REVERT_STATE="not_switched"
    GATEWAY_REVERT_KICKSTART_OK=0
    GATEWAY_REVERT_HEALTH_OK=0
    GATEWAY_SWITCH_RESTORE_FAILED=0
    HAD_PREEXISTING_GATEWAY_PENDING=0
    ACTUAL_RESTORED_GATEWAY_TREE=""
    TARGET_GATEWAY_TREE=""
    PRE_FF_GATEWAY_TREE=""
    PREV_DEPLOYED_GATEWAY_TREE=""
    DEPLOY_GATEWAY=0
    if [ -f "$(_gateway_pending_file)" ]; then
      HAD_PREEXISTING_GATEWAY_PENDING=1
    fi
    PREV_DEPLOYED_GATEWAY_TREE=$(_read_gateway_deployed_tree || true)
    if [ -d "$(_gateway_dir)" ] || [ -n "$PREV_DEPLOYED_GATEWAY_TREE" ] || [ "$HAD_PREEXISTING_GATEWAY_PENDING" = "1" ] || [ "$GATEWAY" = "1" ]; then
      gw_rev_rc=0
      TARGET_GATEWAY_TREE=$(git rev-parse "${TARGET}:integrations/dalton-channel-gateway" 2>/dev/null) || gw_rev_rc=$?
      if [ "$gw_rev_rc" -ne 0 ] || [ -z "$TARGET_GATEWAY_TREE" ]; then
        echo "ERROR: git rev-parse failed while checking ${TARGET}:integrations/dalton-channel-gateway (exit ${gw_rev_rc}): aborting before fast-forward (backend not restarted)" >&2
        release_deploy_lock || true
        exit 1
      fi
      gw_rev_rc=0
      PRE_FF_GATEWAY_TREE=$(git rev-parse "${PRE_FF}:integrations/dalton-channel-gateway" 2>/dev/null) || gw_rev_rc=$?
      if [ "$gw_rev_rc" -ne 0 ] || [ -z "$PRE_FF_GATEWAY_TREE" ]; then
        echo "ERROR: git rev-parse failed while checking ${PRE_FF}:integrations/dalton-channel-gateway (exit ${gw_rev_rc}): aborting before fast-forward (backend not restarted)" >&2
        release_deploy_lock || true
        exit 1
      fi
    fi
    if [ "$GATEWAY" = "1" ] || [ "$HAD_PREEXISTING_GATEWAY_PENDING" = "1" ]; then
      DEPLOY_GATEWAY=1
    else
      gw_diff_rc=0
      git diff --quiet "$PRE_FF" "$TARGET" -- integrations/dalton-channel-gateway/ 2>/dev/null || gw_diff_rc=$?
      if [ "$gw_diff_rc" -ne 0 ] && [ "$gw_diff_rc" -ne 1 ]; then
        echo "ERROR: git diff failed while checking integrations/dalton-channel-gateway/ (exit ${gw_diff_rc}): aborting before fast-forward (backend not restarted)" >&2
        release_deploy_lock || true
        exit 1
      fi
      if [ "$gw_diff_rc" -eq 1 ]; then
        DEPLOY_GATEWAY=1
      elif [ -n "$PREV_DEPLOYED_GATEWAY_TREE" ] && [ "$PREV_DEPLOYED_GATEWAY_TREE" != "$TARGET_GATEWAY_TREE" ]; then
        DEPLOY_GATEWAY=1
      fi
    fi
    if [ "$DEPLOY_GATEWAY" = "1" ]; then
      if ! _write_gateway_pending "${TARGET_GATEWAY_TREE:-pending}"; then
        echo "ERROR: failed to write gateway pending marker to $(_gateway_pending_file): aborting before fast-forward (backend not restarted)" >&2
        release_deploy_lock || true
        exit 1
      fi
      if [ -z "$PREV_DEPLOYED_GATEWAY_TREE" ] && [ -n "$PRE_FF_GATEWAY_TREE" ] && [ "$PRE_FF_GATEWAY_TREE" != "$TARGET_GATEWAY_TREE" ]; then
        if ! _write_gateway_deployed_tree "$PRE_FF_GATEWAY_TREE"; then
          echo "ERROR: failed to write pre-deploy gateway tree hash to $(_gateway_deployed_tree_file): aborting before fast-forward (backend not restarted)" >&2
          release_deploy_lock || true
          exit 1
        fi
        PREV_DEPLOYED_GATEWAY_TREE="$PRE_FF_GATEWAY_TREE"
      fi
    fi
    git merge -q --ff-only "$TARGET" || { echo "ff failed, abort"; release_deploy_lock || true; exit 1; }
    post_ff_head=$(git rev-parse HEAD 2>/dev/null || true)
    if [ "$post_ff_head" != "$TARGET" ]; then
      echo "ERROR: post-ff HEAD (${post_ff_head:-unknown}) does not match TARGET (${TARGET}): reverting" >&2
      MIGRATING_DEPLOY=1
      revert_deploy
      release_deploy_lock || true
      exit 1
    fi
    DEPLOY_TARGET_HEADS="$target_heads"
    DEPLOY_FF_DONE=1

    # If TARGET updated deploy_main.sh or wait_backend_idle.sh, copy the new scripts to a fresh
    # temp dir, verify resume protocol + args + state compatibility handshake, and hand off
    # under the same flock fd so fixes in TARGET take effect on the same deploy.
    scripts_changed_rc=0
    _deploy_scripts_changed_in_target "$running_deploy_sha" "$wait_idle_in_script_dir" "$running_wait_idle_sha" || scripts_changed_rc=$?
    if [ "$scripts_changed_rc" -eq 2 ]; then
      _stop_on_reexec_handshake_failure "failed to hash deploy scripts after fast-forward"
      exit 1
    elif [ "$scripts_changed_rc" -eq 0 ]; then
      reexec_tmp=$(mktemp -d "${TMPDIR:-/tmp}/dalton-deploy-reexec.XXXXXX") || {
        _stop_on_reexec_handshake_failure "failed to create temp dir for deploy script re-exec"
        exit 1
      }
      if ! cp "${REPO_ROOT}/scripts/dalton/ops/prod/deploy_main.sh" "${reexec_tmp}/deploy_main.sh"; then
        rm -rf "$reexec_tmp"
        _stop_on_reexec_handshake_failure "failed to copy updated deploy_main.sh to ${reexec_tmp}"
        exit 1
      fi
      if [ -f "${REPO_ROOT}/scripts/dalton/ops/prod/wait_backend_idle.sh" ]; then
        if ! cp "${REPO_ROOT}/scripts/dalton/ops/prod/wait_backend_idle.sh" "${reexec_tmp}/wait_backend_idle.sh"; then
          rm -rf "$reexec_tmp"
          _stop_on_reexec_handshake_failure "failed to copy updated wait_backend_idle.sh to ${reexec_tmp}"
          exit 1
        fi
      fi
      if [ -f "${REPO_ROOT}/scripts/dalton/ops/prod/backend_busy_threads.sql" ]; then
        if ! cp "${REPO_ROOT}/scripts/dalton/ops/prod/backend_busy_threads.sql" "${reexec_tmp}/backend_busy_threads.sql"; then
          rm -rf "$reexec_tmp"
          _stop_on_reexec_handshake_failure "failed to copy updated backend_busy_threads.sql to ${reexec_tmp}"
          exit 1
        fi
      fi
      if ! bash -n "${reexec_tmp}/deploy_main.sh" || { [ -f "${reexec_tmp}/wait_backend_idle.sh" ] && ! bash -n "${reexec_tmp}/wait_backend_idle.sh"; }; then
        rm -rf "$reexec_tmp"
        _stop_on_reexec_handshake_failure "updated deploy scripts in ${TARGET} failed bash -n syntax check"
        exit 1
      fi
      child_proto=$(DALTON_DEPLOY_LIB_ONLY=0 DALTON_REPO_ROOT="$REPO_ROOT" bash "${reexec_tmp}/deploy_main.sh" --resume-protocol 2>/dev/null || true)
      if [ "$child_proto" != "$DEPLOY_RESUME_PROTOCOL" ]; then
        rm -rf "$reexec_tmp"
        _stop_on_reexec_handshake_failure "re-exec protocol mismatch: target deploy_main.sh reports '${child_proto:-unsupported}', expected '${DEPLOY_RESUME_PROTOCOL}'"
        exit 1
      fi
      state_file="${reexec_tmp}/deploy_state.env"
      if ! _write_deploy_reexec_state "$state_file"; then
        rm -rf "$reexec_tmp"
        _stop_on_reexec_handshake_failure "failed to write re-exec state file ${state_file}"
        exit 1
      fi
      if [ "${#orig_args[@]}" -gt 0 ]; then
        if ! DALTON_DEPLOY_LIB_ONLY=0 DALTON_REPO_ROOT="$REPO_ROOT" bash "${reexec_tmp}/deploy_main.sh" --validate-resume "$state_file" -- "${orig_args[@]}"; then
          rm -rf "$reexec_tmp"
          _stop_on_reexec_handshake_failure "target deploy_main.sh rejected state format or CLI arguments during resume handshake"
          exit 1
        fi
      else
        if ! DALTON_DEPLOY_LIB_ONLY=0 DALTON_REPO_ROOT="$REPO_ROOT" bash "${reexec_tmp}/deploy_main.sh" --validate-resume "$state_file"; then
          rm -rf "$reexec_tmp"
          _stop_on_reexec_handshake_failure "target deploy_main.sh rejected state format during resume handshake"
          exit 1
        fi
      fi
      echo "$(date '+%T') deploy script changed in ${TARGET}; handshake ok (protocol=${DEPLOY_RESUME_PROTOCOL}); re-executing updated script from ${reexec_tmp}"
      ACTIVE_REEXEC_TMP_DIR="$reexec_tmp"
      reexec_rc=0
      if [ "$wait_idle_in_script_dir" = "1" ]; then
        if [ "${#orig_args[@]}" -gt 0 ]; then
          DALTON_WAIT_IDLE_SCRIPT="" \
          DALTON_DEPLOY_REEXECED="$TARGET" \
          DALTON_DEPLOY_STATE_FILE="$state_file" \
          DALTON_DEPLOY_LIB_ONLY=0 \
          DALTON_REPO_ROOT="$REPO_ROOT" \
          bash "${reexec_tmp}/deploy_main.sh" "${orig_args[@]}" &
        else
          DALTON_WAIT_IDLE_SCRIPT="" \
          DALTON_DEPLOY_REEXECED="$TARGET" \
          DALTON_DEPLOY_STATE_FILE="$state_file" \
          DALTON_DEPLOY_LIB_ONLY=0 \
          DALTON_REPO_ROOT="$REPO_ROOT" \
          bash "${reexec_tmp}/deploy_main.sh" &
        fi
      else
        if [ "${#orig_args[@]}" -gt 0 ]; then
          DALTON_DEPLOY_REEXECED="$TARGET" \
          DALTON_DEPLOY_STATE_FILE="$state_file" \
          DALTON_DEPLOY_LIB_ONLY=0 \
          DALTON_REPO_ROOT="$REPO_ROOT" \
          bash "${reexec_tmp}/deploy_main.sh" "${orig_args[@]}" &
        else
          DALTON_DEPLOY_REEXECED="$TARGET" \
          DALTON_DEPLOY_STATE_FILE="$state_file" \
          DALTON_DEPLOY_LIB_ONLY=0 \
          DALTON_REPO_ROOT="$REPO_ROOT" \
          bash "${reexec_tmp}/deploy_main.sh" &
        fi
      fi
      ACTIVE_REEXEC_CHILD_PID=$!
      wait "$ACTIVE_REEXEC_CHILD_PID" || reexec_rc=$?
      ACTIVE_REEXEC_CHILD_PID=""
      rm -rf "$reexec_tmp"
      ACTIVE_REEXEC_TMP_DIR=""
      release_deploy_lock || true
      trap - INT TERM
      return "$reexec_rc"
    fi
  fi

  if ! compute_migrating_deploy; then
    echo "ERROR: failed to determine migrating_deploy for HEAD; treating as migrating deploy" >&2
    MIGRATING_DEPLOY=1
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  echo "$(date '+%T') ff to $(git log --oneline -1) (migrating_deploy=${MIGRATING_DEPLOY})"
  if ! _validate_edge_standard_mounts >/dev/null; then
    echo "ERROR: aborting normal deploy due to unsupported edge mount configuration (keeping existing services untouched)" >&2
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if _edge_target_uses_releases && _edge_live_mounts_any_legacy_srv; then
    local rollout_hint="bash scripts/dalton/ops/prod/deploy_main.sh --first-releases-rollout"
    if [ -n "${PRE_FF:-}" ] && _commit_mounts_legacy_dist "${PRE_FF}"; then
      rollout_hint="${rollout_hint} --pre-ff ${PRE_FF}"
    fi
    echo "ERROR: running dalton-langalpha-edge-1 still mounts legacy web/dist on /srv; normal deploy leaves the fast-forwarded checkout (${TARGET}) and existing edge/services untouched. Run '${rollout_hint}' to complete the one-time transition." >&2
    release_deploy_lock || true
    trap - INT TERM
    exit 3
  fi
  RECREATE_EDGE=0
  if ! git diff --quiet "$PRE_FF" HEAD -- deploy/dalton/Caddyfile 2>/dev/null \
     || edge_compose_changed \
     || _edge_live_mount_mismatches_target; then
    RECREATE_EDGE=1
  fi
  # Initialize web/releases/current from legacy web/dist on first rollout (leaving web/dist intact).
  if ! ensure_frontend_releases_initialized; then
    echo "ERROR: failed to initialize web/releases/: reverting (backend not restarted)" >&2
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  # Host jobs (conversation archive) run on this checkout's .venv, not the container: keep it on the
  # lockfile, or a new dependency breaks them silently (2026-10-28..11-01 archive failures).
  uv sync --frozen -q 2>&1 | tail -2 || echo "WARN: host venv sync failed (host jobs may break)"
  # The build does not install: keep web/node_modules on the lockfile (sync5 changed it).
  (cd web && pnpm install --frozen-lockfile) >/dev/null 2>&1 || { echo "pnpm install failed: reverting"; revert_deploy; release_deploy_lock || true; exit 1; }
  rel_dir=$(_frontend_releases_dir)
  short_target="${TARGET:0:12}"
  short_target="${short_target:-deploy}"
  NEW_FRONTEND_RELEASE_ID="${short_target}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  ACTIVE_FRONTEND_RELEASE_DIR="${rel_dir}/${NEW_FRONTEND_RELEASE_ID}"
  if [ -e "$ACTIVE_FRONTEND_RELEASE_DIR" ] || [ -L "$ACTIVE_FRONTEND_RELEASE_DIR" ]; then
    echo "ERROR: frontend release directory already exists: ${ACTIVE_FRONTEND_RELEASE_DIR}: reverting (backend not restarted)" >&2
    ACTIVE_FRONTEND_RELEASE_DIR=""
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  local build_out_file=""
  build_out_file=$(mktemp "${TMPDIR:-/tmp}/dalton-fe-build.XXXXXX") || {
    echo "ERROR: failed to create temp output file for frontend build: reverting (backend not restarted)" >&2
    cleanup_frontend_staging || true
    revert_deploy
    release_deploy_lock || true
    exit 1
  }
  ACTIVE_STEP_OUT_FILE="$build_out_file"
  build_rc=0
  if _spawn_in_new_pgid "$build_out_file" bash scripts/dalton/build_frontend_prod.sh --require-supabase --out-dir "$ACTIVE_FRONTEND_RELEASE_DIR"; then
    wait "$ACTIVE_STEP_CHILD_PID" || build_rc=$?
    ACTIVE_STEP_CHILD_PID=""
    ACTIVE_STEP_PGID=""
  else
    build_rc=1
  fi
  out=$(cat "$build_out_file" 2>/dev/null || true)
  rm -f "$build_out_file"
  ACTIVE_STEP_OUT_FILE=""
  printf '%s\n' "$out" | grep -E "VITE_DALTON_AUTH=|OK:|ERROR|OVER BUDGET" || true
  # Here-string, not a pipe: under pipefail, grep -q stopping early on a long build log
  # SIGPIPEs printf and reads as a failed build (2026-11-17, #380 deploy).
  if [ "$build_rc" -ne 0 ] || ! grep -q "OK: Production bundle" <<<"$out" \
     || ! _is_servable_frontend_release_dir "$ACTIVE_FRONTEND_RELEASE_DIR"; then
    echo "frontend build failed: cleaning staging and reverting (backend not restarted)"
    cleanup_frontend_staging || true
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if ! _finalize_frontend_version_manifest "$ACTIVE_FRONTEND_RELEASE_DIR" "$NEW_FRONTEND_RELEASE_ID" 0; then
    echo "ERROR: failed to finalize frontend version.json manifest: reverting (backend not restarted)" >&2
    cleanup_frontend_staging || true
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if ! _record_own_frontend_assets "$ACTIVE_FRONTEND_RELEASE_DIR"; then
    echo "ERROR: failed to record frontend asset manifest: reverting (backend not restarted)" >&2
    cleanup_frontend_staging || true
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if [ -n "${PREV_FRONTEND_RELEASE_ID:-}" ] && [ -d "${rel_dir}/${PREV_FRONTEND_RELEASE_ID}" ]; then
    if ! _carry_over_hashed_assets "${rel_dir}/${PREV_FRONTEND_RELEASE_ID}" "$ACTIVE_FRONTEND_RELEASE_DIR"; then
      echo "ERROR: failed to carry over previous hashed assets: reverting (backend not restarted)" >&2
      cleanup_frontend_staging || true
      revert_deploy
      release_deploy_lock || true
      exit 1
    fi
  fi
  if [ "$DEPLOY_GATEWAY" = "1" ]; then
    echo "$(date '+%T') building channel gateway in staging"
    if ! build_gateway_staging; then
      GATEWAY_BUILD_FAILED=1
      echo "gateway build failed (网关未切换，生产目录未改动): cleaning staging and reverting (backend not restarted)"
      revert_deploy
      release_deploy_lock || true
      exit 1
    fi
  fi
  # Re-check right before the restart: the backup and build take minutes, and a turn
  # started in that gap would be cut off (2026-10-28: a new EDU turn died this way).
  # Uses the exact same thread-busy SQL file (backend_busy_threads.sql) as wait_backend_idle.sh.
  local pre_restart_busy_sql=""
  if ! pre_restart_busy_sql="$(_pre_restart_busy_sql)"; then
    echo "ERROR: failed to load backend_busy_threads.sql: reverting (backend not restarted)" >&2
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  busy() { [ "$(docker exec "${WAIT_BACKEND_IDLE_POSTGRES_CONTAINER:-dalton-langalpha-postgres-1}" psql -U postgres -d postgres -Atc "$pre_restart_busy_sql")" != "0" ]; }
  while busy; do echo "$(date '+%T') work started during backup/build; waiting for idle again"; bash "$WAIT_IDLE_SCRIPT" || { echo "still busy: reverting (backend not restarted)"; revert_deploy; release_deploy_lock || true; exit 1; }; done

  if ! check_clean_migration_dirs; then
    echo "ERROR: pre-restart migration directory check failed (migration directories changed after pre-backup check): reverting (backend not restarted)" >&2
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if ! pre_restart_heads=$(target_migration_heads HEAD); then
    echo "ERROR: pre-restart migration head check failed for HEAD: reverting (backend not restarted)" >&2
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if [ "$pre_restart_heads" != "$target_heads" ]; then
    echo "ERROR: pre-restart migration heads (${pre_restart_heads}) differ from pre-backup target heads (${target_heads}): reverting (backend not restarted)" >&2
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi

  if ! ensure_everflow_host_platform_env "${REPO_ROOT}/.env"; then
    echo "ERROR: DALTON_HOST_PLATFORM check failed: reverting (backend not restarted)" >&2
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi

  BACKEND_TOUCHED=1
  if ! apply_backend; then
    echo "ERROR: backend restart/recreate failed: reverting"
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  # P6b crawler service: same image as the backend, code from bind mounts, so a restart picks up the ff.
  # --recreate also re-reads its compose definition; --no-build because it never has its own build.
  if docker compose config --services 2>/dev/null | grep -qx crawler; then
    CRAWLER_TOUCHED=1
    if ! apply_crawler; then
      echo "ERROR: crawler restart/recreate failed: reverting"
      revert_deploy
      release_deploy_lock || true
      exit 1
    fi
  fi
  if [ "$RECREATE_EDGE" = "1" ]; then
    local pre_rec_mount="" pre_rec_rc=0 target_rel_type="" target_rel_src="" target_rel_dst="" target_cur_link="" target_cur_id=""
    pre_rec_mount=$(_validate_edge_standard_mounts) || pre_rec_rc=$?
    if [ "$pre_rec_rc" -eq 1 ]; then
      echo "ERROR: refusing to recreate edge due to unsupported mount configuration (keeping existing edge mount): reverting" >&2
      revert_deploy
      release_deploy_lock || true
      exit 1
    fi
    if [ "$pre_rec_rc" -eq 0 ] && [ -n "$pre_rec_mount" ]; then
      IFS='|' read -r target_rel_type target_rel_src target_rel_dst <<<"$pre_rec_mount"
      if [ "$target_rel_dst" = "/srv-releases" ]; then
        target_cur_link="${target_rel_src}/current"
        target_cur_id=""
        if [ -L "$target_cur_link" ]; then
          target_cur_id=$(readlink "$target_cur_link" 2>/dev/null || true)
          _is_valid_frontend_release_id "$target_cur_id" || target_cur_id=""
        fi
        if [ -z "$target_cur_id" ] || ! _is_servable_frontend_release_dir "${target_rel_src}/${target_cur_id}"; then
          echo "ERROR: refusing to recreate edge onto /srv-releases because ${target_cur_link} is missing or unservable (keeping existing edge mount): reverting" >&2
          revert_deploy
          release_deploy_lock || true
          exit 1
        fi
      fi
    fi
    echo "$(date '+%T') edge config or live mount changed; recreating edge"
    EDGE_TOUCHED=1
    if ! recreate_edge; then
      echo "ERROR: edge recreate failed: reverting"
      revert_deploy
      release_deploy_lock || true
      exit 1
    fi
  fi

  if ! wait_backend_health; then
    echo "ERROR: backend health check failed: reverting"
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  cur=$(docker exec dalton-langalpha-backend-1 sh -c "cd /app && uv run alembic -c src/dalton/alembic.ini current 2>/dev/null" | tail -1 || true); echo "dalton migration: $cur"
  [ -n "$EXPECT" ] && { case "$cur" in "$EXPECT"*) echo "migration ok";; *) echo "MIGRATION MISMATCH (expected $EXPECT)";; esac; }
  if [ "$BRIDGE" = "1" ]; then
    BRIDGE_TOUCHED=1
    launchctl kickstart -k "gui/$(id -u)/com.dalton.researchdata-bridge" || {
      echo "ERROR: researchdata-bridge kickstart failed: reverting"
      revert_deploy
      release_deploy_lock || true
      exit 1
    }
    sleep 5
    launchctl list | grep researchdata-bridge || true
  fi
  if [ "$DEPLOY_GATEWAY" = "1" ]; then
    echo "$(date '+%T') switching channel gateway from staging"
    if ! switch_gateway_from_staging; then
      cleanup_frontend_staging || true
      cur_head=$(git rev-parse HEAD 2>/dev/null || echo unknown)
      if [ "${GATEWAY_SWITCH_RESTORE_FAILED:-0}" = "1" ] || [ ! -f "$(_gateway_dir)/dist/index.mjs" ] || [ ! -d "$(_gateway_dir)/node_modules" ]; then
        echo "ERROR: channel-gateway switch from staging failed and recovery left incomplete state: preserving staging and backup directories"
        _print_gateway_rollback_instructions \
          "$cur_head" \
          "失败（切换中途失败且内部恢复未完成，已保留构建与备份目录）" \
          "跳过（未重启网关）" \
          "跳过（未重启网关）" \
          "$(_describe_gateway_disk_state 0 "$cur_head")"
      else
        echo "ERROR: channel-gateway switch from staging failed (网关未切换): cleaning staging"
        cleanup_gateway_staging || true
        _print_gateway_rollback_instructions \
          "$cur_head" \
          "跳过（切换未完成，生产目录保持上一版完整状态）" \
          "跳过（未重启网关）" \
          "跳过（未重启网关）" \
          "$(_describe_gateway_disk_state 1 "$cur_head")"
      fi
      release_deploy_lock || true
      exit 1
    fi
    if ! verify_gateway_production_import; then
      cleanup_frontend_staging || true
      echo "ERROR: channel-gateway production import check failed: rolling back gateway only"
      rollback_gateway_only 0
      release_deploy_lock || true
      exit 1
    fi
    echo "$(date '+%T') restarting channel gateway"
    if ! launchctl kickstart -k "gui/$(id -u)/com.dalton.channel-gateway"; then
      cleanup_frontend_staging || true
      echo "ERROR: channel-gateway kickstart failed: rolling back gateway only"
      rollback_gateway_only 1
      release_deploy_lock || true
      exit 1
    fi
    if ! wait_gateway_health; then
      cleanup_frontend_staging || true
      echo "ERROR: channel-gateway health check failed: rolling back gateway only"
      rollback_gateway_only 1
      release_deploy_lock || true
      exit 1
    fi
    if [ -n "$TARGET_GATEWAY_TREE" ]; then
      if _write_gateway_deployed_tree "$TARGET_GATEWAY_TREE"; then
        gw_tree_write_ok=1
      else
        echo "WARN: failed to record deployed gateway tree hash in $(_gateway_deployed_tree_file); keeping $(_gateway_pending_file)" >&2
      fi
    fi
    prune_old_gateway_releases
  fi
  docker inspect dalton-langalpha-backend-1 -f 'backend mem_limit={{.HostConfig.Memory}}'
  if ! wait_crawler_health; then
    echo "ERROR: crawler health check failed: reverting"
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if [ "$RECREATE_EDGE" = "1" ]; then
    if ! wait_edge_ready; then
      echo "ERROR: edge not responding with '${EDGE_HEALTH_EXPECTED_BODY}' (HTTP 200) on 127.0.0.1:${EDGE_PORT}/__edge_health within ${EDGE_WAIT_TIMEOUT}s: reverting"
      revert_deploy
      release_deploy_lock || true
      exit 1
    fi
  fi
  expected_build=$(_read_frontend_build_id "$ACTIVE_FRONTEND_RELEASE_DIR" || true)
  expected_release=$(_read_frontend_release_tag "$ACTIVE_FRONTEND_RELEASE_DIR" || true)
  if [ -z "$expected_build" ] || [ -z "$expected_release" ]; then
    echo "ERROR: cannot read build/release id from ${ACTIVE_FRONTEND_RELEASE_DIR}/version.json: reverting" >&2
    cleanup_frontend_staging || true
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  echo "$(date '+%T') atomically switching web/releases/current -> ${NEW_FRONTEND_RELEASE_ID} (build=${expected_build}, release=${expected_release})"
  if ! switch_frontend_from_staging; then
    echo "ERROR: frontend switch failed: reverting"
    cleanup_frontend_staging || true
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  if ! run_public_edge_check "$expected_build" "$(_frontend_current_link)" "$expected_release"; then
    echo "ERROR: public edge check failed: reverting"
    revert_deploy
    release_deploy_lock || true
    exit 1
  fi
  prune_old_frontend_releases "$FRONTEND_KEEP_RELEASES" || echo "WARN: failed to prune old frontend releases" >&2
  if [ "$DEPLOY_GATEWAY" = "1" ] && [ "$gw_tree_write_ok" = "1" ]; then
    if ! _clear_gateway_pending; then
      echo "WARN: failed to remove $(_gateway_pending_file)" >&2
    fi
  fi
  release_deploy_lock || true
  trap - INT TERM
}

_resolve_first_rollout_pre_ff() {
  local explicit_sha="${1:-}" cand=""
  if [ -n "$explicit_sha" ]; then
    if ! cand=$(git rev-parse --verify "${explicit_sha}^{commit}" 2>/dev/null); then
      echo "ERROR: --pre-ff commit not found: ${explicit_sha}" >&2
      return 1
    fi
    if ! _commit_mounts_legacy_dist "$cand"; then
      echo "ERROR: --pre-ff commit ${cand} does not bind-mount this checkout's web/dist -> /srv (with deploy/dalton/Caddyfile) in docker-compose.dalton.yml" >&2
      return 1
    fi
    printf '%s' "$cand"
    return 0
  fi
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    if _commit_mounts_legacy_dist "$cand"; then
      printf '%s' "$cand"
      return 0
    fi
  done <<EOF
$(git rev-list -n 50 HEAD~1 2>/dev/null || true)
EOF
  echo "ERROR: could not find a PRE_FF ancestor bind-mounting web/dist -> /srv (pass --pre-ff <sha>)" >&2
  return 1
}

_extract_edge_image_from_compose_yaml() {
  local edge_cfg="${1:-}" py=""
  [ -n "$edge_cfg" ] || { printf '%s' "caddy:2.9.1-alpine"; return 0; }
  py=$(_resolve_deploy_python 2>/dev/null || true)
  if [ -z "$py" ]; then
    printf '%s' "caddy:2.9.1-alpine"
    return 0
  fi
  "$py" - "$edge_cfg" <<'PY'
import re, sys
for line in sys.argv[1].splitlines():
    m = re.match(r'^\s*image:\s*["\']?([^"\'\s#]+)["\']?\s*(?:#.*)?$', line)
    if m:
        print(m.group(1), end="")
        sys.exit(0)
print("caddy:2.9.1-alpine", end="")
PY
}

_validate_caddyfile_with_edge_image() {
  local cf="${1:-}" edge_image="${2:-caddy:2.9.1-alpine}" out="" rc=0
  [ -s "$cf" ] || return 1
  out=$(docker run --rm --entrypoint caddy -v "${cf}:/etc/caddy/Caddyfile:ro" "$edge_image" validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1) || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: rollback Caddyfile failed 'caddy validate' with ${edge_image}: ${out}" >&2
    return 1
  fi
  return 0
}

FIRST_ROLLOUT_ROLLBACK_DIR=""

_first_rollout_rollbacks_root() {
  printf '%s/.first-rollout-rollback' "$(_frontend_releases_dir)"
}

_allocate_first_rollout_rollback_dir() {
  local rb_root="" ts="" seq=1 cand=""
  rb_root=$(_first_rollout_rollbacks_root)
  mkdir -p "$rb_root" || return 1
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  while [ "$seq" -le 999 ]; do
    cand="${rb_root}/attempt-${ts}-$$-${seq}"
    if mkdir "$cand" 2>/dev/null; then
      printf '%s' "$cand"
      return 0
    fi
    seq=$((seq + 1))
  done
  echo "ERROR: failed to allocate unique rollback snapshot directory under ${rb_root}" >&2
  return 1
}

# Returns 0 if any container (running or stopped) bind-mounts a path inside `target_dir`,
# OR if container mount inspection fails (fail-closed so active snapshots are never deleted).
# Returns 1 only when inspection succeeds and confirms NO container binds `target_dir`.
_first_rollout_rollback_dir_is_bound() {
  local target_dir="${1:-}" cids="" mounts="" py="" rc=0
  [ -n "$target_dir" ] || return 1
  cids=$(docker ps -aq 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then
    return 0
  fi
  [ -n "$cids" ] || return 1
  # shellcheck disable=SC2086
  mounts=$(docker inspect -f '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' $cids 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then
    return 0
  fi
  [ -n "$mounts" ] || return 1
  py=$(_resolve_deploy_python 2>/dev/null || true)
  [ -n "$py" ] || return 0
  "$py" - "$target_dir" "$mounts" <<'PY'
import os
import sys

target_real = os.path.realpath(sys.argv[1])
for raw in sys.argv[2].splitlines():
    src = raw.strip()
    if not src:
        continue
    # Support mock inspect lines formatted as `type|src->dst` or `src->dst` as well as plain `src`.
    if "->" in src:
        src = src.rsplit("->", 1)[0]
        if "|" in src:
            src = src.split("|", 1)[1]
    src = src.strip()
    if not src:
        continue
    src_real = os.path.realpath(src)
    try:
        if os.path.commonpath([src_real, target_real]) == target_real:
            sys.exit(0)
    except ValueError:
        continue
sys.exit(1)
PY
}

_cleanup_unused_first_rollout_rollbacks() {
  local rb_root="" cand=""
  rb_root=$(_first_rollout_rollbacks_root)
  [ -d "$rb_root" ] || return 0
  for cand in "${rb_root}"/attempt-*; do
    [ -e "$cand" ] || continue
    if ! _first_rollout_rollback_dir_is_bound "$cand"; then
      rm -rf "$cand" 2>/dev/null || true
    fi
  done
  return 0
}

_prepare_first_rollout_rollback() {
  local pre_ff_sha="${1:-}" rollback_dir="" attempt_name="" snap_rel="" f="" py="" proj="" ext_idx=0 rel_f="" dest_f=""
  local resolved_cfg="" edge_cfg="" parsed_mounts="" explicit_cf=0 rollback_edge_image=""
  local -a pre_args=() copied_compose_files=() frozen_args=()
  _has_explicit_compose_file_setting && explicit_cf=1
  if ! rollback_dir=$(_allocate_first_rollout_rollback_dir); then
    echo "ERROR: failed to create rollback snapshot directory under $(_first_rollout_rollbacks_root)" >&2
    return 1
  fi
  FIRST_ROLLOUT_ROLLBACK_DIR="$rollback_dir"
  attempt_name=$(basename "$rollback_dir")
  snap_rel="./web/releases/.first-rollout-rollback/${attempt_name}/Caddyfile"
  if ! git show "${pre_ff_sha}:deploy/dalton/Caddyfile" > "${rollback_dir}/Caddyfile" 2>/dev/null \
     || [ ! -s "${rollback_dir}/Caddyfile" ]; then
    echo "ERROR: failed to extract ${pre_ff_sha}:deploy/dalton/Caddyfile for edge rollback" >&2
    return 1
  fi
  proj=$(_edge_compose_project_name || true)
  if [ -n "$proj" ]; then
    pre_args+=(-p "$proj")
    frozen_args+=(-p "$proj")
    printf '%s\n' "$proj" > "${rollback_dir}/project.name" || return 1
  fi
  pre_args+=(--project-directory "$REPO_ROOT")
  frozen_args+=(--project-directory "$REPO_ROOT")
  if [ -f "${REPO_ROOT}/.env" ]; then
    pre_args+=(--env-file "${REPO_ROOT}/.env")
  fi
  py=$(_resolve_deploy_python) || return 1
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    dest_f=""
    case "$f" in
      /*)
        rel_f=$("$py" - "$REPO_ROOT" "$f" <<'PY'
import os, sys
root = os.path.realpath(sys.argv[1])
path = os.path.realpath(sys.argv[2])
try:
    rel = os.path.relpath(path, root)
except ValueError:
    rel = ""
if rel and not rel.startswith("..") and not os.path.isabs(rel):
    print(rel, end="")
PY
        )
        if [ -n "$rel_f" ] && git cat-file -e "${pre_ff_sha}:${rel_f}" 2>/dev/null; then
          dest_f="${rollback_dir}/${rel_f}"
          mkdir -p "$(dirname "$dest_f")" || return 1
          git show "${pre_ff_sha}:${rel_f}" > "$dest_f" 2>/dev/null || return 1
        elif [ -f "$f" ] && [ -r "$f" ]; then
          ext_idx=$((ext_idx + 1))
          dest_f="${rollback_dir}/ext-${ext_idx}-$(basename "$f")"
          cp "$f" "$dest_f" || return 1
        else
          echo "ERROR: explicit compose file unavailable for rollback snapshot: ${f}" >&2
          return 1
        fi
        ;;
      *)
        if git cat-file -e "${pre_ff_sha}:${f}" 2>/dev/null; then
          dest_f="${rollback_dir}/${f}"
          mkdir -p "$(dirname "$dest_f")" || return 1
          git show "${pre_ff_sha}:${f}" > "$dest_f" 2>/dev/null || return 1
        elif [ "$explicit_cf" = "1" ]; then
          echo "ERROR: explicit compose file missing in PRE_FF (${pre_ff_sha}): ${f}" >&2
          return 1
        fi
        ;;
    esac
    if [ -n "$dest_f" ]; then
      [ -s "$dest_f" ] || return 1
      pre_args+=(-f "$dest_f")
      copied_compose_files+=("$dest_f")
    fi
  done <<EOF
$(_edge_compose_files_list "$pre_ff_sha")
EOF
  if [ "${#copied_compose_files[@]}" -eq 0 ]; then
    echo "ERROR: rollback snapshot has no compose files for PRE_FF=${pre_ff_sha}" >&2
    return 1
  fi
  # Rewrite BOTH short syntax and long syntax /etc/caddy/Caddyfile bind mounts across all snapshot compose files.
  "$py" - "$snap_rel" "${copied_compose_files[@]}" <<'PY' || return 1
from pathlib import Path
import re
import sys

snap_rel = sys.argv[1]

for cf_path in sys.argv[2:]:
    p = Path(cf_path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m_short = re.match(r'^(\s*-\s*["\']?)([^"\':\s]+Caddyfile)(:/etc/caddy/Caddyfile.*)$', line)
        if m_short:
            out.append(f"{m_short.group(1)}{snap_rel}{m_short.group(3)}\n")
            i += 1
            continue
        m_item = re.match(r'^(\s*)-\s*(.*)$', line)
        if m_item:
            item_ind = len(m_item.group(1))
            block = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == "" or nxt.lstrip().startswith("#"):
                    block.append(nxt)
                    j += 1
                    continue
                nxt_ind = len(nxt) - len(nxt.lstrip(" "))
                if nxt_ind <= item_ind:
                    break
                block.append(nxt)
                j += 1
            block_text = "".join(block)
            if re.search(r'(?m)^\s*(?:-\s*)?target:\s*["\']?/etc/caddy/Caddyfile["\']?\s*(?:#.*)?$', block_text):
                rewritten = []
                for bl in block:
                    m_src = re.match(r'^(\s*(?:-\s*)?source:\s*["\']?)([^"\'\s#]+)(["\']?.*)$', bl)
                    if m_src:
                        nl = "\n" if bl.endswith("\n") else ""
                        rewritten.append(f"{m_src.group(1)}{snap_rel}{m_src.group(3)}{nl}")
                    else:
                        rewritten.append(bl)
                out.extend(rewritten)
            else:
                out.extend(block)
            i = j
            continue
        out.append(line)
        i += 1
    p.write_text("".join(out), encoding="utf-8")
PY
  if ! resolved_cfg=$(docker compose "${pre_args[@]}" config 2>&1); then
    echo "ERROR: pre-rollout rollback compose validation failed for PRE_FF=${pre_ff_sha}: ${resolved_cfg}" >&2
    return 1
  fi
  edge_cfg=$(printf '%s\n' "$resolved_cfg" | extract_edge_config)
  parsed_mounts=$(_parse_edge_mounts_from_compose_yaml "$edge_cfg" 2>/dev/null || true)
  if ! "$py" - "$REPO_ROOT" "${rollback_dir}/Caddyfile" "$parsed_mounts" <<'PY'
import os
import sys

repo_root, snap_caddy, mounts_raw = sys.argv[1:4]
expected_caddy = os.path.realpath(snap_caddy)
expected_dist = os.path.realpath(os.path.join(repo_root, "web/dist"))
mounts = []
for line in mounts_raw.splitlines():
    parts = line.strip().split("|")
    if len(parts) == 3:
        mounts.append((parts[0], parts[1], parts[2]))

has_snap_caddy = any(t == "bind" and dst == "/etc/caddy/Caddyfile" and os.path.realpath(src) == expected_caddy for t, src, dst in mounts)
has_legacy_dist = any(t == "bind" and dst == "/srv" and os.path.realpath(src) == expected_dist for t, src, dst in mounts)
has_releases = any(dst == "/srv-releases" for _, _, dst in mounts)
if not has_snap_caddy or not has_legacy_dist or has_releases:
    print(
        f"ERROR: resolved rollback compose mounts invalid (snap_caddy={has_snap_caddy}, legacy_dist={has_legacy_dist}, has_releases={has_releases}, mounts={mounts})",
        file=sys.stderr,
    )
    sys.exit(1)
PY
  then
    return 1
  fi
  # Validate rollback Caddyfile with the old edge image (`caddy validate`) BEFORE touching running edge.
  rollback_edge_image=$(_extract_edge_image_from_compose_yaml "$edge_cfg")
  if ! _validate_caddyfile_with_edge_image "${rollback_dir}/Caddyfile" "$rollback_edge_image"; then
    return 1
  fi
  # Freeze the resolved Compose YAML so automatic rollback and manual recovery reuse the exact
  # validated configuration without re-reading .env or recomputing project/file lists.
  printf '%s\n' "$resolved_cfg" > "${rollback_dir}/resolved-compose.yml" || return 1
  frozen_args+=(-f "${rollback_dir}/resolved-compose.yml")
  printf '%s\n' "${frozen_args[@]}" > "${rollback_dir}/compose.args" || return 1
  return 0
}

_verify_legacy_dist_http_ok() {
  local rollback_caddyfile="${1:-}" probe_out="" raw="" code="" rc=0
  if ! wait_edge_ready "$rollback_caddyfile"; then
    echo "ERROR: rolled-back edge readiness probe failed against ${rollback_caddyfile}" >&2
    return 1
  fi
  probe_out=$(mktemp "${TMPDIR:-/tmp}/dalton-legacy-http.XXXXXX") || return 1
  if _spawn_in_new_pgid "$probe_out" curl -s --max-time 3 --noproxy '*' -w $'\n%{http_code}' "http://127.0.0.1:${EDGE_PORT}/"; then
    _wait_pid_ignoring_signals "$ACTIVE_STEP_CHILD_PID" || rc=$?
    ACTIVE_STEP_CHILD_PID=""
    ACTIVE_STEP_PGID=""
  else
    rc=1
  fi
  raw=$(cat "$probe_out" 2>/dev/null || true)
  rm -f "$probe_out"
  [ "$rc" -eq 0 ] || return 1
  code="${raw##*$'\n'}"
  [ "$code" = "200" ]
}

_rollback_first_rollout_edge() {
  local pre_ff_sha="${1:-}" rollback_dir="" arg="" out="" rc=0
  local -a pre_args=()
  rollback_dir="${FIRST_ROLLOUT_ROLLBACK_DIR:-}"
  if [ -z "$rollback_dir" ] || [ ! -s "${rollback_dir}/Caddyfile" ] || [ ! -s "${rollback_dir}/compose.args" ]; then
    _prepare_first_rollout_rollback "$pre_ff_sha" || return 1
    rollback_dir="${FIRST_ROLLOUT_ROLLBACK_DIR}"
  fi
  while IFS= read -r arg; do
    [ -n "$arg" ] && pre_args+=("$arg")
  done < "${rollback_dir}/compose.args"
  [ "${#pre_args[@]}" -gt 0 ] || return 1
  save_container_logs "edge" "dalton-langalpha-edge-1"
  local up_out=""
  up_out=$(mktemp "${TMPDIR:-/tmp}/dalton-rb-up.XXXXXX") || return 1
  _run_cmd_in_pgid_with_timeout "$EDGE_COMPOSE_TIMEOUT" 1 "$up_out" docker compose "${pre_args[@]}" up -d --no-deps --force-recreate edge || rc=$?
  out=$(cat "$up_out" 2>/dev/null || true)
  rm -f "$up_out"
  [ -n "$out" ] && printf '%s\n' "$out" | tail -3
  if [ "$rc" -eq 124 ]; then
    echo "ERROR: rollback docker compose up timed out after ${EDGE_COMPOSE_TIMEOUT}s" >&2
  fi
  [ "$rc" -eq 0 ] || return "$rc"
  if ! _verify_legacy_dist_http_ok "${rollback_dir}/Caddyfile"; then
    echo "ERROR: rolled-back edge on web/dist failed HTTP 200 verification" >&2
    return 1
  fi
  return 0
}

run_first_releases_rollout() {
  local explicit_pre_ff="" rollout_pre_ff="" rel_dir="" cur_link="" legacy_dist="" legacy_id="" legacy_dir=""
  local expected_build="" expected_release="" exit_rc=1
  while [ $# -gt 0 ]; do
    case "$1" in
      --pre-ff)
        if [ $# -lt 2 ] || [ -z "${2:-}" ] || [[ "${2:-}" == -* ]]; then
          echo "ERROR: --pre-ff requires a git commit argument" >&2
          return 2
        fi
        explicit_pre_ff="$2"
        shift
        ;;
      --pre-ff=*)
        explicit_pre_ff="${1#--pre-ff=}"
        if [ -z "$explicit_pre_ff" ] || [[ "$explicit_pre_ff" == -* ]]; then
          echo "ERROR: --pre-ff requires a git commit argument" >&2
          return 2
        fi
        ;;
      *)
        echo "ERROR: unknown argument for --first-releases-rollout: $1" >&2
        return 2
        ;;
    esac
    shift
  done

  cd "$REPO_ROOT" || return 1
  DEPLOY_LOCK_OWNED=0
  FIRST_ROLLOUT_EDGE_TOUCHED=0
  FIRST_ROLLOUT_ROLLBACK_PRE_FF=""
  FIRST_ROLLOUT_ROLLBACK_DIR=""
  DEFERRED_SIGNAL=""
  ROLLBACK_IN_PROGRESS=0
  ACTIVE_FRONTEND_RELEASE_DIR=""
  ACTIVE_STEP_CHILD_PID=""
  ACTIVE_STEP_PGID=""
  ACTIVE_STEP_OUT_FILE=""
  ACTIVE_STEP_WATCHDOG_PID=""
  ACTIVE_STEP_TIMEOUT_MARKER=""
  trap '_on_deploy_interrupt_signal INT' INT
  trap '_on_deploy_interrupt_signal TERM' TERM
  _acquire_deploy_lock || return 1

  if ! _validate_edge_standard_mounts "/srv-releases" >/dev/null \
     || [ ! -f "${REPO_ROOT}/deploy/dalton/Caddyfile" ] \
     || ! grep -q '/srv-releases/current' "${REPO_ROOT}/deploy/dalton/Caddyfile" \
     || ! grep -q 'dalton-edge-ok' "${REPO_ROOT}/deploy/dalton/Caddyfile"; then
    echo "ERROR: checkout must already be fast-forwarded to the standard web/releases compose and Caddyfile before running --first-releases-rollout" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi

  if ! _edge_live_mounts_legacy_dist; then
    echo "ERROR: --first-releases-rollout requires running dalton-langalpha-edge-1 to currently bind-mount ${REPO_ROOT}/web/dist on /srv (running=$(docker inspect -f '{{.State.Running}}' dalton-langalpha-edge-1 2>/dev/null || echo unknown), mounts=$(_edge_live_mounts | tr '\n' ' '))" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi

  if ! rollout_pre_ff=$(_resolve_first_rollout_pre_ff "$explicit_pre_ff"); then
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi

  legacy_dist="${REPO_ROOT}/web/dist"
  if ! _is_servable_legacy_dist_dir "$legacy_dist"; then
    echo "ERROR: ${legacy_dist} must contain index.html, assets/, and help/index.html before --first-releases-rollout" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi

  rel_dir=$(_frontend_releases_dir)
  cur_link=$(_frontend_current_link)
  if [ -L "$rel_dir" ] || { [ -e "$rel_dir" ] && [ ! -d "$rel_dir" ]; }; then
    echo "ERROR: ${rel_dir} must be a real directory" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi
  mkdir -p "$rel_dir" || { release_deploy_lock || true; trap - INT TERM; return 1; }

  # Prepare, freeze, and validate PRE_FF rollback compose + Caddyfile in an isolated per-attempt
  # directory BEFORE touching edge, leaving any previous attempt's snapshot intact for running edge.
  if ! _prepare_first_rollout_rollback "$rollout_pre_ff"; then
    [ -n "${FIRST_ROLLOUT_ROLLBACK_DIR:-}" ] && rm -rf "${FIRST_ROLLOUT_ROLLBACK_DIR}" 2>/dev/null || true
    FIRST_ROLLOUT_ROLLBACK_DIR=""
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi

  legacy_id="legacy-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  legacy_dir="${rel_dir}/${legacy_id}"
  ACTIVE_FRONTEND_RELEASE_DIR="$legacy_dir"
  if ! mkdir "$legacy_dir" || ! cp -Rp "${legacy_dist}/." "${legacy_dir}/"; then
    rm -rf "$legacy_dir" 2>/dev/null || true
    [ -n "${FIRST_ROLLOUT_ROLLBACK_DIR:-}" ] && rm -rf "${FIRST_ROLLOUT_ROLLBACK_DIR}" 2>/dev/null || true
    FIRST_ROLLOUT_ROLLBACK_DIR=""
    ACTIVE_FRONTEND_RELEASE_DIR=""
    echo "ERROR: failed to copy ${legacy_dist} into ${legacy_dir}" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi
  if ! _finalize_frontend_version_manifest "$legacy_dir" "$legacy_id" 1 \
     || ! _record_own_frontend_assets "$legacy_dir"; then
    rm -rf "$legacy_dir" 2>/dev/null || true
    [ -n "${FIRST_ROLLOUT_ROLLBACK_DIR:-}" ] && rm -rf "${FIRST_ROLLOUT_ROLLBACK_DIR}" 2>/dev/null || true
    FIRST_ROLLOUT_ROLLBACK_DIR=""
    ACTIVE_FRONTEND_RELEASE_DIR=""
    echo "ERROR: failed to finalize version.json or asset manifest for ${legacy_dir}" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi
  if ! _is_servable_frontend_release_dir "$legacy_dir"; then
    rm -rf "$legacy_dir" 2>/dev/null || true
    [ -n "${FIRST_ROLLOUT_ROLLBACK_DIR:-}" ] && rm -rf "${FIRST_ROLLOUT_ROLLBACK_DIR}" 2>/dev/null || true
    FIRST_ROLLOUT_ROLLBACK_DIR=""
    ACTIVE_FRONTEND_RELEASE_DIR=""
    echo "ERROR: migrated release ${legacy_dir} is not servable (requires index.html, version.json, assets/, help/index.html)" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi
  if ! _atomic_replace_symlink "$legacy_id" "$cur_link"; then
    rm -rf "$legacy_dir" 2>/dev/null || true
    [ -n "${FIRST_ROLLOUT_ROLLBACK_DIR:-}" ] && rm -rf "${FIRST_ROLLOUT_ROLLBACK_DIR}" 2>/dev/null || true
    FIRST_ROLLOUT_ROLLBACK_DIR=""
    ACTIVE_FRONTEND_RELEASE_DIR=""
    echo "ERROR: failed to atomically set ${cur_link} -> ${legacy_id}" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi
  ACTIVE_FRONTEND_RELEASE_DIR=""

  if ! _is_servable_frontend_release_dir "${rel_dir}/$(_read_frontend_current_release || true)"; then
    [ -n "${FIRST_ROLLOUT_ROLLBACK_DIR:-}" ] && rm -rf "${FIRST_ROLLOUT_ROLLBACK_DIR}" 2>/dev/null || true
    FIRST_ROLLOUT_ROLLBACK_DIR=""
    echo "ERROR: web/releases/current failed post-link servability check" >&2
    release_deploy_lock || true
    trap - INT TERM
    return 1
  fi

  expected_build=$(_read_frontend_build_id "$legacy_dir" || true)
  expected_release=$(_read_frontend_release_tag "$legacy_dir" || true)
  echo "$(date '+%T') first-releases-rollout: migrated web/dist -> web/releases/${legacy_id} (build=${expected_build}, release=${expected_release}; web/dist preserved)"

  FIRST_ROLLOUT_ROLLBACK_PRE_FF="$rollout_pre_ff"
  FIRST_ROLLOUT_EDGE_TOUCHED=1
  if ! recreate_edge \
     || ! wait_edge_ready \
     || ! run_public_edge_check "$expected_build" "$cur_link" "$expected_release"; then
    echo "ERROR: first-releases-rollout edge switch or verification failed; rolling edge back to web/dist using PRE_FF=${rollout_pre_ff} (web/dist preserved)" >&2
    # Defer INT/TERM from rollback start until legacy HTTP verification finishes.
    trap '_record_deferred_signal INT' INT
    trap '_record_deferred_signal TERM' TERM
    ROLLBACK_IN_PROGRESS=1
    if _rollback_first_rollout_edge "$rollout_pre_ff"; then
      FIRST_ROLLOUT_EDGE_TOUCHED=0
      ROLLBACK_IN_PROGRESS=0
      FIRST_ROLLOUT_ROLLBACK_PRE_FF=""
      echo "first-releases-rollout rollback: edge recreated onto web/dist using PRE_FF=${rollout_pre_ff}"
      release_deploy_lock || true
      trap - INT TERM
      exit_rc=1
      [ "${DEFERRED_SIGNAL:-}" = "INT" ] && exit_rc=130
      [ "${DEFERRED_SIGNAL:-}" = "TERM" ] && exit_rc=143
      return "$exit_rc"
    else
      ROLLBACK_IN_PROGRESS=0
      _mark_rollback_failed_lock "$rollout_pre_ff" "failure_rollback_failed"
      echo "ERROR: first-releases-rollout rollback failed to recreate edge onto web/dist (PRE_FF=${rollout_pre_ff}); preserving failure lock and rollback snapshot" >&2
      _print_first_rollout_manual_recovery_steps "$rollout_pre_ff"
      trap - INT TERM
      exit_rc=1
      [ "${DEFERRED_SIGNAL:-}" = "INT" ] && exit_rc=130
      [ "${DEFERRED_SIGNAL:-}" = "TERM" ] && exit_rc=143
      return "$exit_rc"
    fi
  fi

  FIRST_ROLLOUT_EDGE_TOUCHED=0
  FIRST_ROLLOUT_ROLLBACK_PRE_FF=""
  _cleanup_unused_first_rollout_rollbacks
  FIRST_ROLLOUT_ROLLBACK_DIR=""
  echo "$(date '+%T') first-releases-rollout completed: edge serving web/releases/current -> ${legacy_id} (web/dist preserved)"
  release_deploy_lock || true
  trap - INT TERM
  return 0
}

if [ "${DALTON_DEPLOY_LIB_ONLY:-0}" != "1" ]; then
  case "${1:-}" in
    --resume-protocol)
      printf '%s\n' "$DEPLOY_RESUME_PROTOCOL"
      exit 0
      ;;
    --validate-resume)
      shift
      validate_deploy_resume_handshake "$@"
      exit $?
      ;;
    --first-releases-rollout)
      shift
      run_first_releases_rollout "$@"
      exit $?
      ;;
  esac
  run_deploy_main "$@"
fi
