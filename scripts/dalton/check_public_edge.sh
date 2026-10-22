#!/usr/bin/env bash
# Read-only self-check script for the Dalton public edge gateway (Caddy on 127.0.0.1:8088).
# Strictly sends requests ONLY to loopback 127.0.0.1:${DALTON_EDGE_PORT:-8088} and never modifies web/dist.
#
# Checks:
#   1. Static homepage (GET /) -> 200 + SPA fallback + security headers + no-store cache
#   2. Read-only /version.json & /assets/* routing -> /__edge_probe_missing__.json SPA fallback 200 vs /version.json (200 JSON when present, 404 when absent); /assets/<entry> immutable; missing /assets/* -> 404
#   3. /api/v1/users/me without token -> 401
#   4. /api/v1/users/me with forged X-Service-Token + X-User-Id -> header stripped (401 with detail == "Missing authentication")
#   5. Non-proxied backend paths (/health, /openapi.json, /docs) and non-GET static requests -> 404
#   6. WebSocket handshake (/ws/v1/market-data/aggregates/stock) upgrades through Caddy to backend
#
# Usage:
#   bash scripts/dalton/check_public_edge.sh
#   DALTON_WS_TEST_TOKEN="<valid-supabase-jwt>" bash scripts/dalton/check_public_edge.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EDGE_PORT="${DALTON_EDGE_PORT:-8088}"
EDGE_HOST="127.0.0.1"
DIST_DIR="${REPO_ROOT}/web/dist"

PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

"$PYTHON_BIN" - "$EDGE_HOST" "$EDGE_PORT" "$DIST_DIR" <<'PY'
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import sys
from pathlib import Path
from typing import NoReturn


def fail(msg: str) -> NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


host = sys.argv[1]
port = int(sys.argv[2])
dist_dir = Path(sys.argv[3])

if host != "127.0.0.1":
    fail(f"check_public_edge.sh must only target 127.0.0.1, got {host!r}")


def request(
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, body
    except OSError as exc:
        fail(
            f"Cannot connect to http://{host}:{port}{path} ({exc}). "
            "Ensure the edge and backend containers are running."
        )
    finally:
        conn.close()


print(f"=== Dalton Public Edge Self-Check (target=http://{host}:{port}) ===")

# ---------------------------------------------------------------------------
# 1. Static homepage (GET /) + SPA fallback + security headers + cache headers
# ---------------------------------------------------------------------------
status, hdrs, body = request("GET", "/")
if status != 200:
    fail(f"Expected GET / -> 200, got {status}")
text = body.decode("utf-8", errors="replace")
if '<div id="root">' not in text and "<!doctype html>" not in text.lower():
    fail("GET / response does not look like the SPA index.html shell")
cc = hdrs.get("cache-control", "")
if "no-store" not in cc or "no-cache" not in cc:
    fail(f"Expected GET / Cache-Control to include no-cache, no-store; got {cc!r}")
if hdrs.get("x-content-type-options") != "nosniff":
    fail(f"Missing/wrong X-Content-Type-Options: {hdrs.get('x-content-type-options')!r}")
if hdrs.get("referrer-policy") != "strict-origin-when-cross-origin":
    fail(f"Missing/wrong Referrer-Policy: {hdrs.get('referrer-policy')!r}")
if hdrs.get("x-frame-options") != "SAMEORIGIN":
    fail(f"Missing/wrong X-Frame-Options: {hdrs.get('x-frame-options')!r}")
if "strict-transport-security" in hdrs:
    fail("Strict-Transport-Security must not be set on Caddy (TLS terminates at Tailscale Funnel)")

spa_status, spa_hdrs, _ = request("GET", "/reset-password")
if spa_status != 200:
    fail(f"Expected SPA route GET /reset-password -> 200, got {spa_status}")
if "no-store" not in spa_hdrs.get("cache-control", ""):
    fail(f"Expected SPA fallback Cache-Control to include no-store; got {spa_hdrs.get('cache-control')!r}")
print("[1/6] Static homepage (GET /) & SPA fallback (/reset-password) -> 200 OK (security + no-store headers verified)")

# ---------------------------------------------------------------------------
# 2. Read-only /version.json & /assets/* routing verification (never modifies web/dist)
# ---------------------------------------------------------------------------
# A missing non-version.json path falls back to index.html (200 text/html), contrasting with /version.json's dedicated route.
probe_json_status, probe_json_hdrs, probe_json_body = request("GET", "/__edge_probe_missing__.json")
if probe_json_status != 200:
    fail(f"Expected GET /__edge_probe_missing__.json -> 200 (SPA fallback), got {probe_json_status}")
probe_json_text = probe_json_body.decode("utf-8", errors="replace")
if '<div id="root">' not in probe_json_text and "<!doctype html>" not in probe_json_text.lower():
    fail("Expected GET /__edge_probe_missing__.json to return SPA index.html shell")
if "no-store" not in probe_json_hdrs.get("cache-control", ""):
    fail(
        f"Expected GET /__edge_probe_missing__.json Cache-Control to include no-store, "
        f"got {probe_json_hdrs.get('cache-control')!r}"
    )

version_file = dist_dir / "version.json"
if version_file.exists():
    v_status, v_hdrs, v_body = request("GET", "/version.json")
    if v_status != 200:
        fail(f"Expected GET /version.json -> 200 when file exists, got {v_status}")
    if "application/json" not in v_hdrs.get("content-type", ""):
        fail(f"Expected /version.json Content-Type application/json, got {v_hdrs.get('content-type')!r}")
    if "no-store" not in v_hdrs.get("cache-control", ""):
        fail(f"Expected /version.json Cache-Control no-store, got {v_hdrs.get('cache-control')!r}")
    try:
        v_json = json.loads(v_body.decode("utf-8"))
    except Exception as exc:
        fail(f"Failed to parse /version.json as JSON: {exc}")
    entry_chunk = v_json.get("build") if isinstance(v_json, dict) else None
    if not isinstance(entry_chunk, str) or not entry_chunk:
        fail(f"Invalid version.json payload: {v_json!r}")

    # Verify hashed entry asset long-cache header
    a_status, a_hdrs, _ = request("GET", f"/assets/{entry_chunk}")
    if a_status != 200:
        fail(f"Expected GET /assets/{entry_chunk} -> 200, got {a_status}")
    a_cc = a_hdrs.get("cache-control", "")
    if "max-age=31536000" not in a_cc or "immutable" not in a_cc:
        fail(f"Expected /assets/{entry_chunk} Cache-Control immutable, got {a_cc!r}")
else:
    v_status, _, v_body = request("GET", "/version.json")
    if v_status != 404:
        fail(
            f"Expected GET /version.json -> 404 when absent (must not fall back to index.html), "
            f"got {v_status} (body prefix={v_body[:80]!r})"
        )

miss_asset_status, miss_asset_hdrs, _ = request("GET", "/assets/__nonexistent_edge_probe__.js")
if miss_asset_status != 404:
    fail(f"Expected missing /assets/* -> 404 (no SPA fallback), got {miss_asset_status}")
if "immutable" in miss_asset_hdrs.get("cache-control", ""):
    fail("404 on missing /assets/* must not carry Cache-Control: immutable")
print("[2/6] Read-only /version.json, /__edge_probe_missing__.json SPA fallback, and /assets/* cache & 404 OK")

# ---------------------------------------------------------------------------
# 3. /api/v1/users/me without token -> 401
# ---------------------------------------------------------------------------
u_status, _, u_body = request("GET", "/api/v1/users/me")
if u_status != 401:
    fail(f"Expected unauthenticated GET /api/v1/users/me -> 401, got {u_status} ({u_body.decode('utf-8', 'replace')})")
print("[3/6] GET /api/v1/users/me without token -> 401 OK")

# ---------------------------------------------------------------------------
# 4. /api/v1/users/me with forged X-Service-Token + X-User-Id -> stripped (401)
# ---------------------------------------------------------------------------
# Note: FastAPI has no header-echo endpoint. In src/server/utils/api.py:
#   - If X-Service-Token reaches the backend while INTERNAL_SERVICE_TOKEN is set,
#     _service_token_user_id() rejects a wrong token with 401 {"detail": "Invalid service token"},
#     and get_current_user_id() rejects a matching token without X-User-Id with 401 {"detail": "Missing X-User-Id"}.
#   - When Caddy strips X-Service-Token (and X-User-Id), _service_token_user_id() sees no header
#     and falls through to verify_dalton_bearer(), returning 401 {"detail": "Missing authentication"}.
s_status, _, s_body = request(
    "GET",
    "/api/v1/users/me",
    headers={
        "X-Service-Token": "forged-service-token-0123456789abcdef0123456789abcdef",
        "X-User-Id": "00000000-0000-0000-0000-000000000001",
    },
)
s_payload_text = s_body.decode("utf-8", errors="replace")
if s_status != 401:
    fail(f"Expected GET /api/v1/users/me with X-Service-Token -> 401, got {s_status} ({s_payload_text})")
try:
    s_json = json.loads(s_payload_text)
    detail = s_json.get("detail") if isinstance(s_json, dict) else None
except Exception as exc:
    fail(f"Expected JSON error body from /api/v1/users/me, got {s_payload_text!r} ({exc})")
if detail != "Missing authentication":
    fail(
        f"Expected detail == 'Missing authentication' (proving X-Service-Token was stripped), "
        f"got detail={detail!r}"
    )
print(
    f"[4/6] GET /api/v1/users/me with forged X-Service-Token -> 401 (detail={detail!r}; "
    "not treated as service request, header stripped)"
)

# ---------------------------------------------------------------------------
# 5. Non-proxied backend paths & non-GET static requests -> 404
# ---------------------------------------------------------------------------
for blocked_path in ("/health", "/openapi.json", "/docs"):
    b_status, _, _ = request("GET", blocked_path)
    if b_status != 404:
        fail(f"Expected GET {blocked_path} -> 404 at edge, got {b_status}")
post_root_status, _, _ = request("POST", "/")
if post_root_status != 404:
    fail(f"Expected POST / -> 404 at edge, got {post_root_status}")
print("[5/6] Non-proxied paths (/health, /openapi.json, /docs) & POST / -> 404 OK")

# ---------------------------------------------------------------------------
# 6. WebSocket preflight & handshake upgrade (/ws/v1/market-data/...)
# ---------------------------------------------------------------------------
ws_preflight_status, _, _ = request("GET", "/ws/v1/market-data/status")
if ws_preflight_status != 200:
    fail(f"Expected GET /ws/v1/market-data/status -> 200, got {ws_preflight_status}")

# Plain HTTP GET (without Upgrade: websocket) to a @router.websocket route returns 404 from FastAPI
plain_ws_status, _, _ = request("GET", "/ws/v1/market-data/aggregates/stock")
if plain_ws_status != 404:
    fail(f"Expected plain non-Upgrade GET /ws/v1/market-data/aggregates/stock -> 404, got {plain_ws_status}")

# Real WebSocket Upgrade handshake (RFC 6455 §4.1 requires a base64-encoded 16-byte nonce)
ws_token = os.environ.get("DALTON_WS_TEST_TOKEN", "").strip()
probe_token = ws_token or "dalton-edge-ws-probe-redacted-token"
ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
expected_accept = base64.b64encode(
    hashlib.sha1((ws_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
).decode("ascii")

raw_req = (
    f"GET /ws/v1/market-data/aggregates/stock?interval=second&token={probe_token} HTTP/1.1\r\n"
    f"Host: {host}:{port}\r\n"
    "Upgrade: websocket\r\n"
    "Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {ws_key}\r\n"
    "Sec-WebSocket-Version: 13\r\n"
    "\r\n"
).encode("ascii")

with socket.create_connection((host, port), timeout=10.0) as sock:
    sock.sendall(raw_req)
    raw_resp = b""
    while b"\r\n\r\n" not in raw_resp:
        chunk = sock.recv(4096)
        if not chunk:
            break
        raw_resp += chunk

header_block = raw_resp.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1", errors="replace")
status_line = header_block.splitlines()[0] if header_block else ""
parts = status_line.split(" ", 2)
ws_code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0

if ws_token:
    if ws_code != 101:
        fail(f"Expected WebSocket handshake with DALTON_WS_TEST_TOKEN -> 101 Switching Protocols, got {status_line!r}")
    if expected_accept not in header_block:
        fail(f"Missing expected Sec-WebSocket-Accept={expected_accept!r} in 101 response")
    print("[6/6] WebSocket handshake (/ws/v1/market-data/aggregates/stock) -> 101 Switching Protocols OK")
else:
    # With a valid token or in OSS mode -> 101 Switching Protocols;
    # With an unauthenticated probe token in DALTON_AUTH_MODE=supabase -> Uvicorn enters the ASGI
    # websocket handler (unlike plain HTTP GET which returned 404 above) and closes pre-accept with 403.
    if ws_code not in (101, 403):
        fail(f"Expected WebSocket upgrade to reach backend WS handler (101 or pre-accept 403), got {status_line!r}")
    mode_note = (
        "101 Switching Protocols"
        if ws_code == 101
        else "reached ASGI WebSocket handler (pre-accept auth rejection 403 vs plain GET 404; set DALTON_WS_TEST_TOKEN for authenticated 101)"
    )
    print(f"[6/6] WebSocket handshake upgrade forwarded to backend -> {mode_note}")

print("ALL PUBLIC EDGE CHECKS PASSED.")
PY
