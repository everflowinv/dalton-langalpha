#!/bin/bash
# portwatch - sample ephemeral TCP port usage and per-process socket owners.
#
# Installed after the 2026-09-07 outage, where the machine exhausted the
# ephemeral port range (49152-65535) and every connect() - including to
# 127.0.0.1:7897 over lo0 - failed with errno 49 (EADDRNOTAVAIL).
#
# Appends one JSON line per run to $DALTON_PORTWATCH_DIR/portwatch.jsonl
# (default ~/Library/Logs/dalton-portwatch).
# When usage crosses ALERT_PCT it also dumps a full snapshot so the leaking
# process can be identified after the fact.

set -u

LOG_DIR="${DALTON_PORTWATCH_DIR:-$HOME/Library/Logs/dalton-portwatch}"
JSONL="$LOG_DIR/portwatch.jsonl"
SNAP_DIR="$LOG_DIR/portwatch-snapshots"
ALERT_PCT=60
MAX_LINES=20000
MAX_SNAPSHOTS=20

mkdir -p "$LOG_DIR" "$SNAP_DIR"

LOW=$(sysctl -n net.inet.ip.portrange.first 2>/dev/null || echo 49152)
HIGH=$(sysctl -n net.inet.ip.portrange.last 2>/dev/null || echo 65535)
RANGE=$(( HIGH - LOW + 1 ))
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

ns=$(netstat -an -p tcp 2>/dev/null)

# A local port inside the ephemeral range is one the kernel handed out.
# netstat renders the local address as addr.port, so the port is the last
# dot-separated component of field 4.
eph=$(printf '%s\n' "$ns" | awk -v lo="$LOW" -v hi="$HIGH" '
  $1 ~ /^tcp/ { n = split($4, a, "."); p = a[n] + 0; if (p >= lo && p <= hi) c++ }
  END { print c + 0 }')

states=$(printf '%s\n' "$ns" | awk '
  $1 ~ /^tcp/ && $NF ~ /^[A-Z_]+$/ { c[$NF]++ }
  END { first = 1; for (k in c) { printf "%s\"%s\":%d", (first ? "" : ","), k, c[k]; first = 0 } }')

total=$(printf '%s\n' "$ns" | awk '$1 ~ /^tcp/ { c++ } END { print c + 0 }')

pct=$(( eph * 100 / RANGE ))

# Socket owners, most sockets first. lsof only sees our own processes without
# root, which covers every service in question here.
owners=$(lsof -nP -iTCP 2>/dev/null | awk '
  NR > 1 { k = $1; gsub(/[^A-Za-z0-9._-]/, "_", k); c[k "/" $2]++ }
  END {
    n = 0
    for (k in c) { keys[++n] = k }
    for (i = 1; i < n; i++) for (j = i + 1; j <= n; j++)
      if (c[keys[j]] > c[keys[i]]) { t = keys[i]; keys[i] = keys[j]; keys[j] = t }
    lim = (n < 8 ? n : 8)
    for (i = 1; i <= lim; i++) printf "%s\"%s\":%d", (i == 1 ? "" : ","), keys[i], c[keys[i]]
  }')

close_wait=$(lsof -nP -iTCP -sTCP:CLOSE_WAIT 2>/dev/null | awk '
  NR > 1 { k = $1; gsub(/[^A-Za-z0-9._-]/, "_", k); c[k "/" $2]++ }
  END { first = 1; for (k in c) { printf "%s\"%s\":%d", (first ? "" : ","), k, c[k]; first = 0 } }')

printf '{"ts":"%s","eph_used":%d,"eph_range":%d,"eph_pct":%d,"tcp_total":%d,"states":{%s},"owners":{%s},"close_wait":{%s}}\n' \
  "$TS" "$eph" "$RANGE" "$pct" "$total" "$states" "$owners" "$close_wait" >> "$JSONL"

if [ "$pct" -ge "$ALERT_PCT" ]; then
  snap="$SNAP_DIR/$(date -u +%Y%m%dT%H%M%SZ)-pct${pct}.txt"
  {
    echo "=== portwatch snapshot $TS  ephemeral ${eph}/${RANGE} (${pct}%) ==="
    echo
    echo "=== sockets by owner ==="
    lsof -nP -iTCP 2>/dev/null | awk 'NR>1{k = $1; gsub(/[^A-Za-z0-9._-]/, "_", k); c[k"/"$2]++} END{for(k in c) print c[k], k}' | sort -rn
    echo
    echo "=== netstat -an -p tcp ==="
    printf '%s\n' "$ns"
    echo
    echo "=== ps aux ==="
    ps aux
  } > "$snap" 2>&1
fi

# Keep the log and snapshot dir bounded.
lines=$(wc -l < "$JSONL" 2>/dev/null || echo 0)
if [ "$lines" -gt "$MAX_LINES" ]; then
  tail -n "$MAX_LINES" "$JSONL" > "$JSONL.tmp" && mv "$JSONL.tmp" "$JSONL"
fi
ls -1t "$SNAP_DIR" 2>/dev/null | tail -n +$(( MAX_SNAPSHOTS + 1 )) | while read -r old; do
  rm -f "$SNAP_DIR/$old"
done
