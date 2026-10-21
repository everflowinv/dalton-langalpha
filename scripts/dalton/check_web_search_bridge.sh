#!/usr/bin/env bash
# End-to-end verification of the host-side Dalton OpenClaw web search HTTP bridge.
# Never echoes DALTON_WEB_SEARCH_BRIDGE_TOKEN or broker HMAC secrets.
set -euo pipefail

ENV_FILE="${DALTON_WEB_SEARCH_BRIDGE_ENV_FILE:-${HOME}/.config/dalton/web-search-bridge.env}"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: ${ENV_FILE} not found; run scripts/dalton/install_web_search_bridge.sh first." >&2
    exit 1
fi

PYTHON_BIN="${DALTON_OCW_PYTHON:-/opt/homebrew/bin/python3}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "ERROR: Homebrew Python not found at ${PYTHON_BIN}" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"${PYTHON_BIN}" - "${ENV_FILE}" "${SCRIPT_DIR}" <<'PY'
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

script_dir = Path(sys.argv[2]).resolve()
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from web_search_broker_protocol import (
    DEFAULT_QUEUE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_MS,
    compute_http_timeout_seconds,
)

env_file = Path(sys.argv[1])
cfg = {}
for line in env_file.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip("'").strip('"')

host = cfg.get("DALTON_WEB_SEARCH_BRIDGE_HOST", "127.0.0.1")
port = int(cfg.get("DALTON_WEB_SEARCH_BRIDGE_PORT", "8961"))
token = cfg.get("DALTON_WEB_SEARCH_BRIDGE_TOKEN", "")
if not token:
    raise SystemExit("ERROR: DALTON_WEB_SEARCH_BRIDGE_TOKEN missing in env file")

base_url = f"http://{host}:{port}"
search_url = f"{base_url}/search"

# 1. Healthz check
with urllib.request.urlopen(f"{base_url}/healthz", timeout=10) as resp:
    health = json.loads(resp.read().decode("utf-8"))
assert health.get("ok") is True, f"healthz failed: {health}"
print(
    f"[1/3] GET /healthz OK: socket_ready={health.get('broker_socket_ready')} "
    f"key_ready={health.get('broker_key_ready')} max_inflight={health.get('max_inflight')}"
)

# 2. Bad token -> 401
bad_req = urllib.request.Request(
    search_url,
    data=b'{"query":"test","count":1}',
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer invalid-token-probe",
    },
    method="POST",
)
try:
    urllib.request.urlopen(bad_req, timeout=5)
    raise AssertionError("Expected HTTP 401 for bad token, got 200")
except urllib.error.HTTPError as e:
    assert e.code == 401, f"Expected 401 for bad token, got {e.code}"
print("[2/3] Bad token probe returned HTTP 401 as expected")

# 3. Authenticated search check
timeout_ms = int(cfg.get("DALTON_WEB_SEARCH_DEFAULT_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
queue_timeout_s = float(
    cfg.get("DALTON_WEB_SEARCH_BRIDGE_QUEUE_TIMEOUT_S", str(DEFAULT_QUEUE_TIMEOUT_SECONDS))
)
http_timeout = compute_http_timeout_seconds(
    timeout_ms,
    queue_timeout_seconds=queue_timeout_s,
)
good_req = urllib.request.Request(
    search_url,
    data=json.dumps({"query": "Apple Inc SEC CIK", "count": 3}).encode("utf-8"),
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    },
    method="POST",
)
with urllib.request.urlopen(good_req, timeout=http_timeout) as resp:
    reply = json.loads(resp.read().decode("utf-8"))
assert reply.get("ok") is True, f"search failed: {reply.get('error')}"
print(
    f"[3/3] POST /search OK: provider={reply.get('provider')} "
    f"requestId={reply.get('providerRequestId')}"
)
print("ALL WEB SEARCH BRIDGE CHECKS PASSED.")
PY
