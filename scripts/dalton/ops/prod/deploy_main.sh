#!/bin/bash
# Deploy origin/dalton/main: wait idle, fetch once and pin TARGET=origin/dalton/main,
# backup (waits for a fresh dump unless reused), ff to TARGET, frontend build,
# backend restart (runs Dalton migrations), optional bridge/edge recreate, checks.
# Usage: deploy_main.sh [--bridge] [--recreate] [--expect-migration dNNN] [--reuse-backup-minutes N]
# --reuse-backup-minutes N: skip the fresh dump only when the last backup-log line is an ' ok '
#             dump at most N minutes old AND pre-deploy DB revisions for both migration chains
#             already match the pinned TARGET's migration heads; otherwise back up as usual
#             (including when DB revisions cannot be read).
# --recreate: recreate the backend container (compose changes such as mem_limit);
#             plain `docker restart` does not re-read docker-compose files.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${DALTON_REPO_ROOT:-$(cd "${SCRIPT_DIR}/../../../.." && pwd)}"
WAIT_IDLE_SCRIPT="${SCRIPT_DIR}/wait_backend_idle.sh"
EDGE_PORT="${DALTON_EDGE_PORT:-8088}"
EDGE_WAIT_TIMEOUT=60
CONTAINER_LOG_DIR="${DALTON_CONTAINER_LOG_DIR:-$HOME/Library/Logs/Dalton/containers}"
CONTAINER_LOG_RETENTION_DAYS=14
BRIDGE=0; RECREATE=0; EXPECT=""; REUSE_BACKUP_MIN=""

usage() {
  cat <<'EOF'
Usage: deploy_main.sh [--bridge] [--recreate] [--expect-migration dNNN] [--reuse-backup-minutes N]
  Waits for backend idle, reads pre-deploy DB revisions, fetches origin once and pins
  TARGET=origin/dalton/main, verifies HEAD is an ancestor of TARGET and migration directories
  have no uncommitted or ignored files (except __pycache__/ and *.pyc), re-checks migration
  directories and heads right before backend restart, compares pre-deploy DB revisions against
  TARGET heads (dalton + upstream) for backup reuse, against post-ff HEAD for migrating status,
  and against PRE_FF before auto-revert (unreadable DB revisions are treated as migrating).
  --reuse-backup-minutes N  Reuse last ' ok ' dump <= N min old only when DB revisions match TARGET heads.
  --expect-migration dNNN   Verify post-deploy dalton migration starts with dNNN.
  --recreate                Recreate backend/crawler containers instead of docker restart.
  --bridge                  Also kickstart com.dalton.researchdata-bridge.
EOF
}

parse_deploy_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --bridge) BRIDGE=1;;
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

_print_manual_revert_instructions() {
  local cur_head="$1" cur_dalton_rev="$2" cur_upstream_rev="$3"
  local target_ref="${TARGET:-$cur_head}"
  local pre_ff_heads="" pre_ff_dalton="" pre_ff_upstream=""
  local target_heads="" target_dalton="" target_upstream=""
  local db_readable=0 pre_ff_matches=0 target_matches=0
  local reason="" recommended_commit="" recommended_label="" reset_target=""

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

  echo "AUTO-REVERT ABORTED — 原因：${reason}脚本不执行 git reset、不降级、不停服务，保持当前代码（HEAD=${cur_head}）与各服务现状。"
  echo "部署记录: PRE_FF=${PRE_FF:-未知}, TARGET=${target_ref:-未知}, HEAD=${cur_head}, backend_touched=${BACKEND_TOUCHED}, crawler_touched=${CRAWLER_TOUCHED}, edge_touched=${EDGE_TOUCHED}, bridge_touched=${BRIDGE_TOUCHED}"
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
  echo "2. 选择方案。方案 A（推荐）：后端能正常运行时保持当前版本，修复问题后重新部署。方案 B：确需手工复位或恢复时，严格按 3–9 步顺序执行，任何一步不满足就停下。"
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
  echo "   降级后，把 RESET_SHA 改为两条链 head 等于降级后数据库版本的那个提交（通常是 PRE_FF），重新执行第 7 步核对，一致后再执行第 8 步。"
  echo "7. 【方案 B】设定复位提交 RESET_SHA 并打印其两条链 head，与第 5/6 步读到的数据库版本比对一致后再继续（不等就停下，先不要重启后端）："
  echo "     RESET_SHA=${reset_target}"
  echo "     DALTON_DEPLOY_LIB_ONLY=1 RESET_SHA=\"\$RESET_SHA\" bash -c 'source scripts/dalton/ops/prod/deploy_main.sh && target_migration_heads \"\$RESET_SHA\"'"
  if [ -n "$recommended_commit" ] && [ -n "${PRE_FF:-}" ] && [ "$recommended_commit" != "$PRE_FF" ]; then
    echo "   如第 6 步已将数据库降级回部署前版本，把 RESET_SHA 改为对应历史提交（如 RESET_SHA=${PRE_FF}），重新执行第 7 步核对，一致后再执行第 8 步。"
  elif [ -z "$recommended_commit" ]; then
    echo "   请把 RESET_SHA 设为两条链 head 均等于数据库版本的提交（替换 <已核对一致的提交>）；确认一致前先不要重启后端。"
  fi
  echo "8. 【方案 B】确认第 7 步比对一致后，复位代码、重建前端，并强制重建容器（后端仍处于停止状态才执行）："
  echo "     git reset --keep \"\$RESET_SHA\" && (cd web && pnpm install --frozen-lockfile) && bash scripts/dalton/build_frontend_prod.sh --require-supabase"
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
  local need_post_health=0 build_out build_rc post_ok=1 cur_head
  local cur_dalton_rev="" cur_upstream_rev=""
  local -a failed_steps=()

  if ! _can_auto_revert_to_pre_ff; then
    cur_head=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    cur_dalton_rev=$(get_dalton_alembic_version || true)
    cur_upstream_rev=$(get_upstream_alembic_version || true)
    _print_manual_revert_instructions "$cur_head" "$cur_dalton_rev" "$cur_upstream_rev"
    return 1
  fi

  if [ "$BACKEND_TOUCHED" = "1" ] || [ "$CRAWLER_TOUCHED" = "1" ] || [ "$EDGE_TOUCHED" = "1" ]; then
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
    if [ "$need_post_health" = "1" ]; then
      echo "POST-REVERT HEALTH REPORT: all revert steps and post-revert checks passed"
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
      case "$unquoted" in
        */__pycache__|*/__pycache__/|*/__pycache__/*|*.pyc)
          continue
          ;;
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
        if not basename.endswith(".py") or basename == "__init__.py":
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

run_deploy_main() {
  local LOG before after out build_rc cur reused target_heads post_ff_head pre_restart_heads
  parse_deploy_args "$@" || exit $?
  cd "$REPO_ROOT" || exit 1
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
}

if [ "${DALTON_DEPLOY_LIB_ONLY:-0}" != "1" ]; then
  run_deploy_main "$@"
fi
