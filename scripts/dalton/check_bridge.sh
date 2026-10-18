#!/usr/bin/env bash
# End-to-end verification of the host-side Dalton researchdata bridge (6 real MCP calls + auth + handoff filter).
# Never echoes DALTON_BRIDGE_TOKEN.
set -euo pipefail

ENV_FILE="${DALTON_BRIDGE_ENV_FILE:-${HOME}/.config/dalton/researchdata-bridge.env}"
if [[ ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: ${ENV_FILE} not found; run scripts/dalton/install_bridge.sh first." >&2
    exit 1
fi

PYTHON_BIN="${DALTON_OCW_PYTHON:-/opt/homebrew/bin/python3}"
if [[ ! -x "${PYTHON_BIN}" ]]; then
    PYTHON_BIN="$(command -v python3)"
fi

"${PYTHON_BIN}" - "${ENV_FILE}" <<'PY'
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

env_file = Path(sys.argv[1])
cfg = {}
for line in env_file.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip().strip("'").strip('"')

host = cfg.get("DALTON_BRIDGE_HOST", "127.0.0.1")
port = int(cfg.get("DALTON_BRIDGE_PORT", "8960"))
token = cfg.get("DALTON_BRIDGE_TOKEN", "")
if not token:
    raise SystemExit("ERROR: DALTON_BRIDGE_TOKEN missing in env file")

base_url = f"http://{host}:{port}"
mcp_url = f"{base_url}/mcp/{token}"

# 0a. Healthz check
with urllib.request.urlopen(f"{base_url}/healthz", timeout=10) as resp:
    health = json.loads(resp.read().decode("utf-8"))
assert health.get("ok") is True, f"healthz failed: {health}"
print(f"[0a] GET /healthz OK: server_version={health.get('server_version')} max_inflight={health.get('max_inflight')}")

# 0b. Bad token -> 404
bad_req = urllib.request.Request(
    f"{base_url}/mcp/invalid-token-probe",
    data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    urllib.request.urlopen(bad_req, timeout=5)
    raise AssertionError("Expected HTTP 404 for bad token, got 200")
except urllib.error.HTTPError as e:
    assert e.code == 404, f"Expected 404 for bad token, got {e.code}"
print("[0b] Bad token probe returned HTTP 404 as expected")

def rpc(method: str, params: dict, req_id: int = 1) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(
        mcp_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

# 1. tools/list: 6 tools, news & community have 0 handoff routes
tl = rpc("tools/list", {}, 1)
tools = {t["name"]: t for t in tl["result"]["tools"]}
assert len(tools) == 6, f"Expected 6 tools, got {list(tools.keys())}"
news_routes = tools["news"]["inputSchema"]["properties"]["route"]["enum"]
comm_routes = tools["community"]["inputSchema"]["properties"]["route"]["enum"]
assert news_routes == [], f"Expected empty news route enum, got {news_routes}"
assert comm_routes == [], f"Expected empty community route enum, got {comm_routes}"
print(f"[1/6] tools/list OK: 6 tools ({', '.join(tools.keys())}); news/community handoff routes stripped")

# 2. company-wiki search
r2 = rpc(
    "tools/call",
    {
        "name": "research",
        "arguments": {
            "route": "company-wiki",
            "operation": "search",
            "query": "Meta CAPEX",
            "params": {"company": "META"},
            "limit": 2,
        },
    },
    2,
)
p2 = json.loads(r2["result"]["content"][0]["text"])
s2 = p2.get("_declared_sources") or []
assert p2.get("ok") is True and len(s2) > 0, f"company-wiki search failed: {p2.get('error')}"
print(f"[2/6] research(company-wiki, search) OK: status={p2.get('status')} declared_sources={len(s2)} first={s2[0]['document_id']} ({s2[0]['identity']})")

# 3. alphaengine search_library
r3 = rpc(
    "tools/call",
    {
        "name": "research",
        "arguments": {
            "route": "alphaengine",
            "operation": "search_library",
            "params": {"query": "Meta AI", "companies": ["META"]},
        },
    },
    3,
)
p3 = json.loads(r3["result"]["content"][0]["text"])
s3 = p3.get("_declared_sources") or []
assert p3.get("ok") is True and len(s3) > 0, f"alphaengine search_library failed: {p3.get('error')}"
assert s3[0]["identity"] in ("broker_report", "company")
print(f"[3/6] research(alphaengine, search_library) OK: status={p3.get('status')} declared_sources={len(s3)} first={s3[0]['document_id']} ({s3[0]['publisher']})")

# 4. guidepoint search_library
r4 = rpc(
    "tools/call",
    {
        "name": "research",
        "arguments": {
            "route": "guidepoint",
            "operation": "search_library",
            "params": {"query": "Meta"},
        },
    },
    4,
)
p4 = json.loads(r4["result"]["content"][0]["text"])
s4 = p4.get("_declared_sources") or []
assert p4.get("ok") is True and len(s4) > 0, f"guidepoint search_library failed: {p4.get('error')}"
assert s4[0]["identity"] == "expert"
print(f"[4/6] research(guidepoint, search_library) OK: status={p4.get('status')} declared_sources={len(s4)} first={s4[0]['document_id']} ({s4[0]['publisher']})")

# 5. finance(sec, list-filings)
r5 = rpc(
    "tools/call",
    {
        "name": "finance",
        "arguments": {
            "route": "sec",
            "operation": "list-filings",
            "ticker": "META",
            "form": "10-Q",
            "limit": 3,
        },
    },
    5,
)
p5 = json.loads(r5["result"]["content"][0]["text"])
s5 = p5.get("_declared_sources") or []
assert p5.get("ok") is True and len(s5) > 0, f"sec list-filings failed: {p5.get('error')}"
assert s5[0]["identity"] == "filing"
print(f"[5/6] finance(sec, list-filings) OK: status={p5.get('status')} declared_sources={len(s5)} first={s5[0]['document_id']}")

# 6. finance(estimates, consensus)
r6 = rpc(
    "tools/call",
    {
        "name": "finance",
        "arguments": {
            "route": "estimates",
            "operation": "consensus",
            "ticker": "META",
        },
    },
    6,
)
p6 = json.loads(r6["result"]["content"][0]["text"])
s6 = p6.get("_declared_sources") or []
assert p6.get("ok") is True and len(s6) > 0, f"estimates consensus failed: {p6.get('error')}"
assert s6[0]["identity"] == "street_estimate"
print(f"[6/6] finance(estimates, consensus) OK: status={p6.get('status')} declared_sources={len(s6)} first={s6[0]['document_id']}")
print("ALL 6 BRIDGE CHECKS PASSED.")
PY
