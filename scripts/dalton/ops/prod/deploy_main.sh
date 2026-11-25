#!/bin/bash
# Deploy origin/dalton/main: wait idle, fetch once and pin TARGET=origin/dalton/main,
# backup (waits for a fresh dump unless reused), ff to TARGET, frontend build,
# channel-gateway build (automatic when integrations/dalton-channel-gateway/ changes
# between PRE_FF and TARGET, or forced with --gateway), backend restart (runs Dalton
# migrations), optional bridge/gateway restart and edge recreate, health checks.
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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"
WAIT_IDLE_SCRIPT="${SCRIPT_DIR}/wait_backend_idle.sh"
EDGE_PORT="${DALTON_EDGE_PORT:-8088}"
EDGE_WAIT_TIMEOUT=60
GATEWAY_PORT="${DALTON_GATEWAY_PORT:-8962}"
GATEWAY_WAIT_TIMEOUT="${DALTON_GATEWAY_WAIT_TIMEOUT:-60}"
GATEWAY_POLL_INTERVAL="${DALTON_GATEWAY_POLL_INTERVAL:-1}"
GATEWAY_ENV_FILE="${DALTON_CHANNEL_GATEWAY_ENV_FILE:-$HOME/.config/dalton/channel-gateway.env}"
CONTAINER_LOG_DIR="${DALTON_CONTAINER_LOG_DIR:-$HOME/Library/Logs/Dalton/containers}"
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
edge_ready() {
  local max_time="${1:-2}" code
  code=$(curl -s --max-time "$max_time" --noproxy '*' -o /dev/null -w '%{http_code}' "http://127.0.0.1:${EDGE_PORT}/") || return 1
  [ "$code" = "200" ]
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
  local out rc
  save_container_logs "edge" "dalton-langalpha-edge-1"
  out=$(docker compose up -d --no-deps --force-recreate edge 2>&1)
  rc=$?
  [ -n "$out" ] && printf '%s\n' "$out" | tail -2
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
  local ok=0 deadline now remaining max_time i
  deadline=$(( $(date +%s) + EDGE_WAIT_TIMEOUT ))
  for i in $(seq 1 "$EDGE_WAIT_TIMEOUT"); do
    now=$(date +%s)
    remaining=$(( deadline - now ))
    [ "$remaining" -le 0 ] && break
    max_time=2
    [ "$remaining" -lt "$max_time" ] && max_time="$remaining"
    if edge_ready "$max_time"; then
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

_on_deploy_interrupt_signal() {
  local sig="${1:-TERM}" code=143 gw_dir="" live_rel=""
  trap - INT TERM
  [ "$sig" = "INT" ] && code=130
  gw_dir=$(_gateway_dir)
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
    exit "$code"
  fi
  ACTIVE_GATEWAY_STAGING_DIR=""
  STAGED_GATEWAY_RELEASE_ID=""
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
  out=$(node --input-type=module -e 'import { resolve } from "node:path"; import { pathToFileURL } from "node:url"; await import(pathToFileURL(resolve(process.argv[1])).href);' "${tmp_iso}/dist/index.mjs" 2>&1) || rc=$?
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
  local out rc
  out=$(bash scripts/dalton/check_public_edge.sh 2>&1)
  rc=$?
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
  echo "8. 【方案 B】确认 7b 比对一致后，先将当前部署脚本备份到 /tmp/dalton_deploy_main.sh（防止复位到较早的 RESET_SHA 后工作区脚本尚无网关辅助函数），再复位代码、重建前端、处理渠道网关产物，并强制重建容器（后端仍处于停止状态才执行）："
  echo "     cp scripts/dalton/ops/prod/deploy_main.sh /tmp/dalton_deploy_main.sh && git reset --keep \"\$RESET_SHA\" && (cd web && pnpm install --frozen-lockfile) && bash scripts/dalton/build_frontend_prod.sh --require-supabase"
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
  local need_post_health=0 build_out build_rc post_ok=1 cur_head gw_dir=""
  local cur_dalton_rev="" cur_upstream_rev=""
  local -a failed_steps=()

  gw_dir=$(_gateway_dir)
  GATEWAY_REVERT_STATE="not_switched"
  GATEWAY_REVERT_KICKSTART_OK=0
  GATEWAY_REVERT_HEALTH_OK=0
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

  if [ "$BACKEND_TOUCHED" = "1" ] || [ "$CRAWLER_TOUCHED" = "1" ] || [ "$EDGE_TOUCHED" = "1" ] || [ "$GATEWAY_SWITCHED" = "1" ]; then
    need_post_health=1
  fi
  git update-index -q --refresh
  if ! git reset -q --keep "$PRE_FF"; then
    cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    echo "CODE REVERT FAILED — 代码未能恢复，需人工处理 (HEAD=${cur_head}, PRE_FF=${PRE_FF}; manual: git update-index --refresh && git reset --keep $PRE_FF)"
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
  build_out=$(bash scripts/dalton/build_frontend_prod.sh --require-supabase 2>&1)
  build_rc=$?
  if [ "$build_rc" -eq 0 ] && grep -q "OK: Production bundle" <<<"$build_out"; then
    echo "reverted to $(git rev-parse --short=9 HEAD), old frontend rebuilt"
  else
    echo "OLD FRONTEND REBUILD FAILED"
    post_ok=0
    failed_steps+=("frontend_build")
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
    if run_public_edge_check; then
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
      echo "POST-REVERT REPORT: code and frontend reverted (网关未切换，生产目录未改动)"
    else
      echo "POST-REVERT REPORT: all revert steps passed"
    fi
    return 0
  else
    echo "POST-REVERT REPORT: FAILED (failed steps: ${failed_steps[*]})"
    return 1
  fi
}

# Last backup-log line if it is an ' ok ' dump at most $2 minutes old (relative to $3, epoch
# seconds); prints nothing and returns 1 otherwise. Log line: "YYYY-mm-dd HH:MM:SS ok  <size> <path>".
recent_ok_backup() {
  local log="$1" max_min="$2" now="$3" line ts epoch
  line=$(tail -1 "$log" 2>/dev/null) || return 1
  case "$line" in *" ok "*) ;; *) return 1;; esac
  ts="${line:0:19}"
  epoch=$(date -j -f '%Y-%m-%d %H:%M:%S' "$ts" +%s 2>/dev/null || date -d "$ts" +%s 2>/dev/null) || return 1
  [ -n "$epoch" ] && [ "$epoch" -le "$now" ] && [ $(( now - epoch )) -le $(( max_min * 60 )) ] || return 1
  printf '%s\n' "$line"
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

run_deploy_main() {
  local LOG before after out build_rc cur reused target_heads post_ff_head pre_restart_heads gw_diff_rc gw_rev_rc gw_tree_write_ok=0
  parse_deploy_args "$@" || exit $?
  cd "$REPO_ROOT" || exit 1
  ACTIVE_GATEWAY_STAGING_DIR=""
  ACTIVE_GATEWAY_IMPORT_TMP=""
  STAGED_GATEWAY_RELEASE_ID=""
  trap '_on_deploy_interrupt_signal INT' INT
  trap '_on_deploy_interrupt_signal TERM' TERM
  cleanup_old_container_logs
  TARGET=""
  bash "$WAIT_IDLE_SCRIPT" || { echo "still busy, abort"; exit 1; }
  PRE_FF_DALTON_REV=$(get_dalton_alembic_version || true)
  PRE_FF_UPSTREAM_REV=$(get_upstream_alembic_version || true)
  echo "pre-deploy migrations: dalton=${PRE_FF_DALTON_REV:-未能读取}, upstream=${PRE_FF_UPSTREAM_REV:-未能读取}"
  if [ -z "$PRE_FF_DALTON_REV" ] || [ -z "$PRE_FF_UPSTREAM_REV" ]; then
    echo "pre-deploy migrations: DB version unreadable; treating as migrating deploy (fresh backup required, no auto-revert)"
  fi
  git fetch -q origin || { echo "git fetch failed, abort"; exit 1; }
  TARGET=$(git rev-parse --verify origin/dalton/main 2>/dev/null) || { echo "cannot resolve origin/dalton/main, abort"; exit 1; }
  echo "deploy target: ${TARGET} ($(git log -1 --oneline "$TARGET" 2>/dev/null || printf '%s' "$TARGET"))"
  if ! git merge-base --is-ancestor HEAD "$TARGET" 2>/dev/null; then
    echo "ERROR: 生产检出上有不在 origin/dalton/main 里的提交，先处理再部署 (HEAD=$(git rev-parse --short=9 HEAD 2>/dev/null || echo unknown), TARGET=${TARGET})" >&2
    exit 1
  fi
  check_clean_migration_dirs || exit 1
  target_heads=$(target_migration_heads "$TARGET") || { echo "ERROR: failed to resolve unique migration heads for target ${TARGET}, abort" >&2; exit 1; }
  echo "target migration heads: ${target_heads}"
  LOG="${DALTON_PG_BACKUP_LOG:-$HOME/Library/Logs/dalton-pg-backup.log}"; before=$(tail -1 "$LOG" 2>/dev/null)
  reused=""
  if [ -n "$REUSE_BACKUP_MIN" ] && no_pending_migrations "$TARGET"; then
    reused=$(recent_ok_backup "$LOG" "$REUSE_BACKUP_MIN" "$(date +%s)") || reused=""
  fi
  if [ -n "$reused" ]; then
    echo "backup: reusing (no migrations to deploy, dump within ${REUSE_BACKUP_MIN} min): $reused"
  else
    [ -n "$REUSE_BACKUP_MIN" ] && echo "backup: cannot reuse (migrations to deploy or no ok dump within ${REUSE_BACKUP_MIN} min); taking a fresh one"
    launchctl kickstart "gui/$(id -u)/com.dalton.pg-backup"
    for i in $(seq 1 360); do [ "$(tail -1 "$LOG" 2>/dev/null)" != "$before" ] && break; sleep 5; done  # 30 min: dumps of a 7.5G db take over 10 min
    after=$(tail -1 "$LOG"); echo "backup: $after"
    case "$after" in *" ok "*) [ "$after" != "$before" ] || { echo "no fresh backup, abort"; exit 1; };; *) echo "backup failed, abort"; exit 1;; esac
  fi
  # ff and frontend build only after the idle wait and backup, right before the restart, so the new
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
      exit 1
    fi
    gw_rev_rc=0
    PRE_FF_GATEWAY_TREE=$(git rev-parse "${PRE_FF}:integrations/dalton-channel-gateway" 2>/dev/null) || gw_rev_rc=$?
    if [ "$gw_rev_rc" -ne 0 ] || [ -z "$PRE_FF_GATEWAY_TREE" ]; then
      echo "ERROR: git rev-parse failed while checking ${PRE_FF}:integrations/dalton-channel-gateway (exit ${gw_rev_rc}): aborting before fast-forward (backend not restarted)" >&2
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
      exit 1
    fi
    if [ -z "$PREV_DEPLOYED_GATEWAY_TREE" ] && [ -n "$PRE_FF_GATEWAY_TREE" ] && [ "$PRE_FF_GATEWAY_TREE" != "$TARGET_GATEWAY_TREE" ]; then
      if ! _write_gateway_deployed_tree "$PRE_FF_GATEWAY_TREE"; then
        echo "ERROR: failed to write pre-deploy gateway tree hash to $(_gateway_deployed_tree_file): aborting before fast-forward (backend not restarted)" >&2
        exit 1
      fi
      PREV_DEPLOYED_GATEWAY_TREE="$PRE_FF_GATEWAY_TREE"
    fi
  fi
  git merge -q --ff-only "$TARGET" || { echo "ff failed, abort"; exit 1; }
  post_ff_head=$(git rev-parse HEAD 2>/dev/null || true)
  if [ "$post_ff_head" != "$TARGET" ]; then
    echo "ERROR: post-ff HEAD (${post_ff_head:-unknown}) does not match TARGET (${TARGET}): reverting" >&2
    MIGRATING_DEPLOY=1
    revert_deploy
    exit 1
  fi
  if ! compute_migrating_deploy; then
    echo "ERROR: failed to determine migrating_deploy for HEAD; treating as migrating deploy" >&2
    MIGRATING_DEPLOY=1
    revert_deploy
    exit 1
  fi
  echo "$(date '+%T') ff to $(git log --oneline -1) (migrating_deploy=${MIGRATING_DEPLOY})"
  RECREATE_EDGE=0
  if ! git diff --quiet "$PRE_FF" HEAD -- deploy/dalton/Caddyfile 2>/dev/null || edge_compose_changed; then
    RECREATE_EDGE=1
  fi
  # Host jobs (conversation archive) run on this checkout's .venv, not the container: keep it on the
  # lockfile, or a new dependency breaks them silently (2026-10-28..11-01 archive failures).
  uv sync --frozen -q 2>&1 | tail -2 || echo "WARN: host venv sync failed (host jobs may break)"
  # The build does not install: keep web/node_modules on the lockfile (sync5 changed it).
  (cd web && pnpm install --frozen-lockfile) >/dev/null 2>&1 || { echo "pnpm install failed: reverting"; revert_deploy; exit 1; }
  out=$(bash scripts/dalton/build_frontend_prod.sh --require-supabase 2>&1)
  build_rc=$?
  printf '%s\n' "$out" | grep -E "VITE_DALTON_AUTH=|OK:|ERROR|OVER BUDGET" || true
  # Here-string, not a pipe: under pipefail, grep -q stopping early on a long build log
  # SIGPIPEs printf and reads as a failed build (2026-11-17, #380 deploy).
  if [ "$build_rc" -ne 0 ] || ! grep -q "OK: Production bundle" <<<"$out"; then
    echo "frontend build failed: reverting (backend not restarted)"
    revert_deploy
    exit 1
  fi
  if [ "$DEPLOY_GATEWAY" = "1" ]; then
    echo "$(date '+%T') building channel gateway in staging"
    if ! build_gateway_staging; then
      GATEWAY_BUILD_FAILED=1
      echo "gateway build failed (网关未切换，生产目录未改动): cleaning staging and reverting (backend not restarted)"
      revert_deploy
      exit 1
    fi
  fi
  # Re-check right before the restart: the backup and build take minutes, and a turn
  # started in that gap would be cut off (2026-10-28: a new EDU turn died this way).
  busy() { [ "$(docker exec dalton-langalpha-postgres-1 psql -U postgres -d postgres -Atc "select (select count(*) from conversation_threads where current_status='in_progress')+(select count(*) from subagent_runs where status='in_progress')")" != "0" ]; }
  while busy; do echo "$(date '+%T') work started during backup/build; waiting for idle again"; bash "$WAIT_IDLE_SCRIPT" || { echo "still busy: reverting (backend not restarted)"; revert_deploy; exit 1; }; done

  if ! check_clean_migration_dirs; then
    echo "ERROR: pre-restart migration directory check failed (migration directories changed after pre-backup check): reverting (backend not restarted)" >&2
    revert_deploy
    exit 1
  fi
  if ! pre_restart_heads=$(target_migration_heads HEAD); then
    echo "ERROR: pre-restart migration head check failed for HEAD: reverting (backend not restarted)" >&2
    revert_deploy
    exit 1
  fi
  if [ "$pre_restart_heads" != "$target_heads" ]; then
    echo "ERROR: pre-restart migration heads (${pre_restart_heads}) differ from pre-backup target heads (${target_heads}): reverting (backend not restarted)" >&2
    revert_deploy
    exit 1
  fi

  if ! ensure_everflow_host_platform_env "${REPO_ROOT}/.env"; then
    echo "ERROR: DALTON_HOST_PLATFORM check failed: reverting (backend not restarted)" >&2
    revert_deploy
    exit 1
  fi

  BACKEND_TOUCHED=1
  if ! apply_backend; then
    echo "ERROR: backend restart/recreate failed: reverting"
    revert_deploy
    exit 1
  fi
  # P6b crawler service: same image as the backend, code from bind mounts, so a restart picks up the ff.
  # --recreate also re-reads its compose definition; --no-build because it never has its own build.
  if docker compose config --services 2>/dev/null | grep -qx crawler; then
    CRAWLER_TOUCHED=1
    if ! apply_crawler; then
      echo "ERROR: crawler restart/recreate failed: reverting"
      revert_deploy
      exit 1
    fi
  fi
  if [ "$RECREATE_EDGE" = "1" ]; then
    echo "$(date '+%T') edge config changed; recreating edge"
    EDGE_TOUCHED=1
    if ! recreate_edge; then
      echo "ERROR: edge recreate failed: reverting"
      revert_deploy
      exit 1
    fi
  fi

  if ! wait_backend_health; then
    echo "ERROR: backend health check failed: reverting"
    revert_deploy
    exit 1
  fi
  cur=$(docker exec dalton-langalpha-backend-1 sh -c "cd /app && uv run alembic -c src/dalton/alembic.ini current 2>/dev/null" | tail -1 || true); echo "dalton migration: $cur"
  [ -n "$EXPECT" ] && { case "$cur" in "$EXPECT"*) echo "migration ok";; *) echo "MIGRATION MISMATCH (expected $EXPECT)";; esac; }
  if [ "$BRIDGE" = "1" ]; then
    BRIDGE_TOUCHED=1
    launchctl kickstart -k "gui/$(id -u)/com.dalton.researchdata-bridge" || {
      echo "ERROR: researchdata-bridge kickstart failed: reverting"
      revert_deploy
      exit 1
    }
    sleep 5
    launchctl list | grep researchdata-bridge || true
  fi
  if [ "$DEPLOY_GATEWAY" = "1" ]; then
    echo "$(date '+%T') switching channel gateway from staging"
    if ! switch_gateway_from_staging; then
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
      exit 1
    fi
    if ! verify_gateway_production_import; then
      echo "ERROR: channel-gateway production import check failed: rolling back gateway only"
      rollback_gateway_only 0
      exit 1
    fi
    echo "$(date '+%T') restarting channel gateway"
    if ! launchctl kickstart -k "gui/$(id -u)/com.dalton.channel-gateway"; then
      echo "ERROR: channel-gateway kickstart failed: rolling back gateway only"
      rollback_gateway_only 1
      exit 1
    fi
    if ! wait_gateway_health; then
      echo "ERROR: channel-gateway health check failed: rolling back gateway only"
      rollback_gateway_only 1
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
    exit 1
  fi
  if [ "$RECREATE_EDGE" = "1" ]; then
    if ! wait_edge_ready; then
      echo "ERROR: edge not responding with HTTP 200 on 127.0.0.1:${EDGE_PORT} within ${EDGE_WAIT_TIMEOUT}s: reverting"
      revert_deploy
      exit 1
    fi
  fi
  if ! run_public_edge_check; then
    echo "ERROR: public edge check failed: reverting"
    revert_deploy
    exit 1
  fi
  if [ "$DEPLOY_GATEWAY" = "1" ] && [ "$gw_tree_write_ok" = "1" ]; then
    if ! _clear_gateway_pending; then
      echo "WARN: failed to remove $(_gateway_pending_file)" >&2
    fi
  fi
  trap - INT TERM
}

if [ "${DALTON_DEPLOY_LIB_ONLY:-0}" != "1" ]; then
  run_deploy_main "$@"
fi
