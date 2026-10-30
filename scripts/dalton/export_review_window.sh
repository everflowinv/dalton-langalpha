#!/usr/bin/env bash
# Export a review window of Dalton logs (backlog 四-3, 2026-10-28).
#
# Replaces the out-of-repo ~/Projects/dalton-prodfix/notes/export_review_window.sh:
# reads the researchdata bridge's .err.log (its stdout .log is empty — logging
# goes to stderr) and the web-search bridge logs beside it, plus the backend
# container's docker logs for the same window. Everything exported passes
# through scripts/dalton/log_redaction.py, which strips /mcp/<token> paths,
# bearer tokens, and token= pairs before anything is written out.
#
# Usage:
#   scripts/dalton/export_review_window.sh --since '2026-10-27 22:50' [--until '2026-10-27 23:10'] [-o out.txt]
#
# --since/--until accept docker-logs syntax (RFC3339, '2026-10-27T23:00:00',
# or relative like '90m'); for the bridge files the same value is parsed as a
# plain 'YYYY-MM-DD[ HH:MM:SS]' timestamp to drop lines outside the window, and
# for `docker logs` itself it is re-rendered as RFC3339 with the local offset
# (R2a-3): docker rejects '2026-10-27 09:00' with "parsing time … extra text".
#
# Env overrides: DALTON_BACKEND_CONTAINER (default dalton-langalpha-backend-1),
# DALTON_LOG_DIR (default ~/Library/Logs). This script only reads logs and
# never touches .env, tokens, or the database.
#
# The header names each running version separately (24h review item 22): the
# host repo HEAD, the backend image's id and creation time, the backend
# container's start time, and each bridge's server_version from the newest
# "Listening on …" startup line in its logs ("unknown" when it never logged
# one). When the container started later than --since, the header says the
# backend logs only cover after the container start — the earlier ones were
# lost with the container rebuild. Values lifted out of log lines still pass
# through the redactor before they reach the header.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REDACTOR="$REPO_ROOT/scripts/dalton/log_redaction.py"
BACKEND_CONTAINER="${DALTON_BACKEND_CONTAINER:-dalton-langalpha-backend-1}"
LOG_DIR="${DALTON_LOG_DIR:-$HOME/Library/Logs}"
RESEARCH_ERR_LOG="$LOG_DIR/dalton-researchdata-bridge.err.log"
RESEARCH_OUT_LOG="$LOG_DIR/dalton-researchdata-bridge.log"
WEBSEARCH_ERR_LOG="$LOG_DIR/dalton-web-search-bridge.err.log"
WEBSEARCH_OUT_LOG="$LOG_DIR/dalton-web-search-bridge.log"

SINCE=""
UNTIL=""
OUTFILE="dalton-review-window-$(date +%Y%m%d-%H%M%S).txt"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="${2:?--since needs a value}"; shift 2 ;;
    --until) UNTIL="${2:?--until needs a value}"; shift 2 ;;
    -o|--out) OUTFILE="${2:?-o needs a value}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
if [[ -z "$SINCE" ]]; then
  echo "usage: $0 --since '<timestamp>' [--until '<timestamp>'] [-o out.txt]" >&2
  exit 2
fi

PYTHON_BIN="${DALTON_PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python3

# Bridge-file line filtering wants a plain 'YYYY-MM-DD HH:MM:SS' timestamp in
# local time; log_redaction.py --normalize-window parses RFC3339 (Z / offsets),
# plain timestamps, and docker's relative syntax ('2h', '90m'). Any unparseable
# value fails loudly instead of silently exporting the whole bridge log.
if ! plain_since="$("$PYTHON_BIN" "$REDACTOR" --normalize-window "$SINCE")"; then
  echo "error: invalid --since timestamp: $SINCE" >&2
  exit 2
fi
# R2a-3: docker logs takes only RFC3339 (or a relative duration) — 'YYYY-MM-DD
# HH:MM' fails with "extra text", which silently left the backend logs out of
# every export. Same parsing as the bridge window, re-rendered with the local
# offset (DST-aware).
if ! docker_since="$("$PYTHON_BIN" "$REDACTOR" --docker-window "$SINCE")"; then
  echo "error: invalid --since timestamp: $SINCE" >&2
  exit 2
fi
line_window_args=(--since "$plain_since")
if [[ -n "$UNTIL" ]]; then
  if ! plain_until="$("$PYTHON_BIN" "$REDACTOR" --normalize-window "$UNTIL")"; then
    echo "error: invalid --until timestamp: $UNTIL" >&2
    exit 2
  fi
  if ! docker_until="$("$PYTHON_BIN" "$REDACTOR" --docker-window "$UNTIL")"; then
    echo "error: invalid --until timestamp: $UNTIL" >&2
    exit 2
  fi
  line_window_args+=(--until "$plain_until")
fi

TMPDIR_EXPORT="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_EXPORT"' EXIT

emit_redacted() {
  # emit_redacted <label> <command...>: run the command, capture BOTH stdout
  # and stderr (the backend root logger writes to stderr, logging_config.py:184),
  # merge and sort by leading timestamp, and redact into the output under a
  # section header. The command's own errors land in the export (redacted)
  # rather than aborting it.
  local label="$1"; shift
  {
    echo ""
    echo "===== $label ====="
    if "$@" >"$TMPDIR_EXPORT/raw.out" 2>"$TMPDIR_EXPORT/raw.err"; then
      cat "$TMPDIR_EXPORT/raw.out" "$TMPDIR_EXPORT/raw.err" >"$TMPDIR_EXPORT/raw.merged"
      "$PYTHON_BIN" "$REDACTOR" "$TMPDIR_EXPORT/raw.merged" - --sort "${line_window_args[@]}"
    else
      echo "(source unavailable: exit $?)"
      cat "$TMPDIR_EXPORT/raw.out" "$TMPDIR_EXPORT/raw.err" >"$TMPDIR_EXPORT/cmd.err"
      "$PYTHON_BIN" "$REDACTOR" "$TMPDIR_EXPORT/cmd.err" -
    fi
  } >>"$OUTFILE"
}

emit_redacted_file() {
  # emit_redacted_file <label> <file>: redact the file into the output,
  # preferring the stderr log and falling back to the (usually empty) stdout
  # log when only that exists.
  local label="$1"; shift
  local file="$1"
  {
    echo ""
    echo "===== $label ====="
    if [[ -f "$file" ]]; then
      "$PYTHON_BIN" "$REDACTOR" "$file" - "${line_window_args[@]}"
    else
      echo "(missing: $file)"
    fi
  } >>"$OUTFILE"
}

docker_logs_args=(logs --since "$docker_since")
if [[ -n "$UNTIL" ]]; then
  docker_logs_args+=(--until "$docker_until")
fi
docker_logs_args+=("--timestamps" "$BACKEND_CONTAINER")

commit="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
image_id="$(docker inspect --format '{{.Image}}' "$BACKEND_CONTAINER" 2>/dev/null || echo unknown)"
image_created="unknown"
if [[ "$image_id" != "unknown" ]]; then
  image_created="$(docker image inspect --format '{{.Created}}' "$image_id" 2>/dev/null || echo unknown)"
fi
container_started="$(docker inspect --format '{{.State.StartedAt}}' "$BACKEND_CONTAINER" 2>/dev/null || echo unknown)"

# 24h review item 22: one commit field could not say which version the host
# repo, the backend image and the two bridge processes each ran, and a
# container rebuild that lost the early logs read as an empty window. The
# bridges name their version on every startup line, so the most recent
# "Listening on … server_version=" line answers which build served the
# window's tail; anything read out of a log still passes the redactor.
bridge_server_version() {
  # bridge_server_version <log...>: version from the newest startup line
  # that carries one, compared by each line's own leading timestamp across
  # every file — a restart's record may sit in either log, so file order
  # decides nothing. A line whose head is not a timestamp (an unlogged
  # rewrite, a stray echo) does not compete. Every step is guarded: a
  # missing log file or a failed grep answers "unknown" rather than
  # aborting the export under `set -euo pipefail`.
  # The sed keeps one "timestamp<T>version" word per qualifying line (the
  # date/time separator is normalized to T so the C-locale sort compares
  # instants, not spellings); bash 3.2 takes it from there.
  local picked
  picked="$(
    {
      grep -h 'Listening on' "$@" 2>/dev/null || true
    } | sed -n 's/^\([0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}\)[T ]\([0-9]\{2\}:[0-9]\{2\}:[0-9]\{2\}\)\([.,][0-9]\{1,9\}\)\{0,1\} .*server_version=\([^ ,)]*\).*/\1T\2\3 \4/p' \
    | LC_ALL=C sort -r | head -n 1
  )"
  picked="${picked#* }"
  printf '%s' "${picked:-unknown}"
}
redacted_version() {
  # A value lifted out of a log line is log content until proven otherwise.
  local v out
  v="$(bridge_server_version "$@")"
  if [[ "$v" == "unknown" ]]; then
    printf '%s' "$v"
    return
  fi
  out="$(printf '%s' "$v" | "$PYTHON_BIN" "$REDACTOR" - - 2>/dev/null)" || out=""
  printf '%s' "${out:-unknown}"
}
research_version="$(redacted_version "$RESEARCH_ERR_LOG" "$RESEARCH_OUT_LOG")"
websearch_version="$(redacted_version "$WEBSEARCH_ERR_LOG" "$WEBSEARCH_OUT_LOG")"

backend_coverage="$(
  "$PYTHON_BIN" - "$(dirname "$REDACTOR")" "$container_started" "$docker_since" <<'PY' 2>/dev/null
import sys

sys.path.insert(0, sys.argv[1])
from log_redaction import parse_window_timestamp

started, since = sys.argv[2], sys.argv[3]
try:
    started_dt = parse_window_timestamp(started)
    since_dt = parse_window_timestamp(since)
except ValueError:
    print("unknown (container start or window start unreadable)")
    raise SystemExit(0)
if started_dt > since_dt:
    print(
        f"backend logs only cover after {started} (container start); "
        "logs from before that were lost with the container rebuild"
    )
else:
    print(f"full window (container started {started}, before --since)")
PY
)" || backend_coverage="unknown (coverage comparison failed)"

timezone="$(date +%Z%z) ($(readlink /etc/localtime 2>/dev/null || echo '?'))"

{
  echo "Dalton review-window export"
  echo "since: $SINCE"
  echo "until: ${UNTIL:-<open ended>}"
  echo "commit: $commit"
  echo "backend_container: $BACKEND_CONTAINER"
  echo "backend_image_id: $image_id"
  echo "backend_image_created: $image_created"
  echo "backend_container_started: $container_started"
  echo "backend_log_coverage: $backend_coverage"
  echo "researchdata_bridge_server_version: $research_version"
  echo "websearch_bridge_server_version: $websearch_version"
  echo "timezone: $timezone"
  echo "host: $(hostname)"
  echo "generated: $(date '+%Y-%m-%d %H:%M:%S %Z')"
} >"$OUTFILE"

emit_redacted "backend container logs (docker ${docker_logs_args[*]})" docker "${docker_logs_args[@]}"
# The researchdata bridge logs to stderr: the .log (stdout) is empty, so read
# .err.log first and fall back to .log only when the err file is absent.
if [[ -f "$RESEARCH_ERR_LOG" ]]; then
  emit_redacted_file "researchdata bridge ($RESEARCH_ERR_LOG)" "$RESEARCH_ERR_LOG"
else
  emit_redacted_file "researchdata bridge ($RESEARCH_OUT_LOG, stderr log absent)" "$RESEARCH_OUT_LOG"
fi
if [[ -f "$WEBSEARCH_ERR_LOG" ]]; then
  emit_redacted_file "web search bridge ($WEBSEARCH_ERR_LOG)" "$WEBSEARCH_ERR_LOG"
else
  emit_redacted_file "web search bridge ($WEBSEARCH_OUT_LOG, stderr log absent)" "$WEBSEARCH_OUT_LOG"
fi

echo "wrote $OUTFILE ($(wc -l <"$OUTFILE" | tr -d ' ') lines)" >&2
