#!/bin/bash
# Run Colima in the foreground under launchd (com.dalton.colima), so it never belongs to a
# terminal session. Its data lives on the external SSD (~/.colima -> /Volumes/EveSSD/...):
# wait for the volume first, or `colima start` would fail at login before the disk mounts.
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
COLIMA_WAIT_DIR="${DALTON_COLIMA_WAIT_DIR:-/Volumes/EveSSD/Dalton/colima/_lima}"
for i in $(seq 1 120); do
  [ -d "$COLIMA_WAIT_DIR" ] && break
  [ "$i" = 1 ] && echo "$(date '+%F %T') waiting for ${COLIMA_WAIT_DIR}"
  sleep 5
done
[ -d "$COLIMA_WAIT_DIR" ] || { echo "$(date '+%F %T') ${COLIMA_WAIT_DIR} not mounted, giving up"; exit 1; }
echo "$(date '+%F %T') starting colima"
exec colima start -f
