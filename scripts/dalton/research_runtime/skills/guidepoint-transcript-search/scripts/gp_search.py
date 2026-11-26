#!/usr/bin/env python3
"""CLI for Guidepoint MCP search_library via the local auto-refresh proxy (127.0.0.1:8943/mcp).

Used by skills/subagents that need Guidepoint expert-transcript search from bash.
The proxy (LaunchAgent ai.openclaw.guidepoint-mcp-proxy) handles OAuth token refresh.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("DALTON_GUIDEPOINT_MCP_URL", "").strip() or "http://127.0.0.1:8943/mcp"


def _read_bridge_env_fallback(key: str) -> str:
    env_file = Path(
        os.environ.get("DALTON_BRIDGE_ENV_FILE", "").strip()
        or (Path.home() / ".config" / "dalton" / "researchdata-bridge.env")
    ).expanduser()
    if not env_file.is_file():
        return ""
    try:
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            k, v = line.split("=", 1)
            if k.strip() == key:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                return v
    except OSError:
        return ""
    return ""


def _resolve_base_url():
    return (
        os.environ.get("DALTON_GUIDEPOINT_MCP_URL", "").strip()
        or _read_bridge_env_fallback("DALTON_GUIDEPOINT_MCP_URL")
        or BASE
    )


def _resolve_host_token():
    return (
        os.environ.get("DALTON_RESEARCH_HOST_TOKEN", "").strip()
        or _read_bridge_env_fallback("DALTON_RESEARCH_HOST_TOKEN")
    )


def _is_host_direct_paused() -> bool:
    """Return True when external host-direct Guidepoint calls are paused for maintenance (§6.4b (1) #2)."""
    if os.environ.get("DALTON_BRIDGE_INTERNAL_CALL", "").strip() == "1":
        return False
    if os.environ.get("DALTON_OPS_CONTROLLED_PROBE", "").strip() == "1":
        return False
    env_flag = (
        os.environ.get("DALTON_HOST_DIRECT_PAUSED", "").strip()
        or _read_bridge_env_fallback("DALTON_HOST_DIRECT_PAUSED").strip()
    )
    if env_flag.lower() in ("1", "true", "yes", "on"):
        return True
    gate_override = os.environ.get("DALTON_MAINTENANCE_ADMISSION_FILE", "").strip()
    if gate_override:
        gate_path = Path(gate_override).expanduser()
    else:
        gate_path = Path(__file__).resolve().parents[6] / "var" / "dalton" / "maintenance-admission.json"
    if gate_path.is_file():
        try:
            data = json.loads(gate_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and bool(data.get("pause_host_direct", False)):
                return True
        except (OSError, ValueError):
            pass
    return False


def _resolve_deadline_timeout_s(requested_timeout_s: float) -> float:
    eff = max(0.1, float(requested_timeout_s))
    raw_ms = os.environ.get("DALTON_REQUEST_DEADLINE_EPOCH_MS", "").strip()
    if raw_ms:
        try:
            rem = (int(raw_ms) / 1000.0) - time.time()
            eff = min(eff, max(0.1, rem))
        except ValueError:
            pass
    return max(0.1, eff)


def _extract_transcripts(res: dict) -> list:
    """Extract transcript rows from both legacy `:8943` and Batch 3 `:8951/mcp/guidepoint` responses."""
    candidates = []
    if isinstance(res.get("structuredContent"), dict):
        candidates.append(res["structuredContent"])
    for item in res.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item["text"])
                if isinstance(parsed, dict):
                    candidates.append(parsed)
                elif isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
    for cand in candidates:
        data_val = cand.get("data")
        if isinstance(data_val, list):
            return data_val
        if isinstance(data_val, dict):
            nested_sc = data_val.get("structuredContent")
            if isinstance(nested_sc, dict) and isinstance(nested_sc.get("data"), list):
                return nested_sc["data"]
            if isinstance(data_val.get("data"), list):
                return data_val["data"]
        if isinstance(cand.get("results"), list):
            return cand["results"]
    return []


def rpc(payload, session_id=None, *, timeout_s: float = 120.0):
    eff_timeout_s = max(0.1, float(timeout_s))
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Dalton-Request-Timeout-Ms": str(max(100, int(eff_timeout_s * 1000))),
    }
    host_token = _resolve_host_token()
    if host_token:
        headers["Authorization"] = f"Bearer {host_token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(_resolve_base_url(), data=json.dumps(payload).encode(), headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=eff_timeout_s) as resp:
        sid = resp.headers.get("Mcp-Session-Id")
        raw = resp.read().decode()
        ctype = resp.headers.get("Content-Type", "")
    if "text/event-stream" in ctype:
        data = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
        body = json.loads(data) if data else None
    else:
        body = json.loads(raw) if raw.strip() else None
    return body, sid


def mcp_session(*, deadline_mono: float | None = None):
    def _rem() -> float:
        return 120.0 if deadline_mono is None else max(0.1, deadline_mono - time.monotonic())

    init, sid = rpc({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "eve-gp-cli", "version": "1.0"},
        },
    }, timeout_s=_rem())
    if not init or "result" not in init:
        raise RuntimeError(f"initialize failed: {init}")
    try:
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, timeout_s=_rem())
    except Exception:
        pass
    return init["result"], sid


def cmd_self_test(args):
    if _is_host_direct_paused():
        print("FAIL: maintenance_host_direct_paused (external host-direct Guidepoint calls paused)", file=sys.stderr)
        return 1
    eff_timeout_s = _resolve_deadline_timeout_s(getattr(args, "timeout", 120) or 120)
    deadline_mono = time.monotonic() + eff_timeout_s
    try:
        result, sid = mcp_session(deadline_mono=deadline_mono)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"FAIL: proxy unreachable at {_resolve_base_url()} ({e})")
        print("Hint: launchctl kickstart -k gui/$UID/com.dalton.guidepoint-mcp-proxy")
        return 1
    except Exception as e:
        print(f"FAIL: initialize error: {e}")
        print("Hint: token may be invalid — re-run scripts/guidepoint_mcp_oauth.py to reseed tokens")
        return 1
    server = result.get("serverInfo", {})
    print(f"OK: connected to {server.get('name')} v{server.get('version')}")
    tools, _ = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid, timeout_s=max(0.1, deadline_mono - time.monotonic()))
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    print(f"tools: {names}")
    if "search_library" not in names:
        print("FAIL: search_library tool missing")
        return 1
    print("PASS")
    return 0


def cmd_search(args):
    if _is_host_direct_paused():
        err_obj = {
            "ok": False,
            "status": "maintenance_host_direct_paused",
            "error_code": "maintenance_host_direct_paused",
            "error": "External host-direct Guidepoint calls are paused during maintenance.",
        }
        if args.json:
            json.dump(err_obj, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            print(f"ERROR: {err_obj['error_code']}: {err_obj['error']}", file=sys.stderr)
        return 1

    eff_timeout_s = _resolve_deadline_timeout_s(getattr(args, "timeout", 120) or 120)
    deadline_mono = time.monotonic() + eff_timeout_s
    arguments = {"query": args.query}
    if args.keywords:
        arguments["keywords"] = args.keywords
    if args.size:
        arguments["size"] = args.size
    if args.recency_bias:
        arguments["recency_bias"] = args.recency_bias
    if args.start_date:
        arguments["start_date"] = args.start_date
    if args.end_date:
        arguments["end_date"] = args.end_date

    try:
        _, sid = mcp_session(deadline_mono=deadline_mono)
        rem_s = max(0.1, deadline_mono - time.monotonic())
        call_arguments = dict(arguments)
        call_arguments["timeout"] = max(1, int(rem_s)) if rem_s >= 1.0 else rem_s
        res, _ = rpc({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_library", "arguments": call_arguments},
        }, sid, timeout_s=rem_s)
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        print(f"ERROR: proxy unreachable at {_resolve_base_url()} ({e})", file=sys.stderr)
        print("Hint: launchctl kickstart -k gui/$UID/com.dalton.guidepoint-mcp-proxy", file=sys.stderr)
        return 1

    if res.get("error"):
        print(f"ERROR: {json.dumps(res['error'])}", file=sys.stderr)
        return 1

    result = res.get("result") or {}
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    if result.get("isError"):
        # MCP tool failures commonly arrive inside HTTP 200 / JSON-RPC result.
        # Preserve the server's structured error and never report an empty success.
        error = result.get("structuredContent") or {"message": "\n".join(texts)}
        if args.json:
            json.dump({"ok": False, "isError": True, "error": error}, sys.stdout, ensure_ascii=False, indent=2)
            print()
        else:
            print("ERROR: " + ("\n".join(texts) or json.dumps(error, ensure_ascii=False)), file=sys.stderr)
        return 1

    rows = _extract_transcripts(result)
    payload = None
    if rows is not None and (
        rows
        or isinstance(result.get("structuredContent"), dict)
        or any(t.strip().startswith("{") for t in texts)
    ):
        # Verify at least one candidate carried an excerpt list (even empty [])
        found_list = False
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            if isinstance(sc.get("data"), list):
                payload = sc
                found_list = True
            elif isinstance(sc.get("data"), dict) and isinstance((sc["data"].get("structuredContent") or {}).get("data"), list):
                payload = sc["data"]["structuredContent"]
                found_list = True
            elif isinstance(sc.get("results"), list):
                payload = {"data": sc["results"]}
                found_list = True
        if not found_list:
            for t in texts:
                try:
                    candidate = json.loads(t)
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict):
                    if isinstance(candidate.get("data"), list):
                        payload = candidate
                        found_list = True
                        break
                    if isinstance(candidate.get("data"), dict) and isinstance((candidate["data"].get("structuredContent") or {}).get("data"), list):
                        payload = candidate["data"]["structuredContent"]
                        found_list = True
                        break
                    if isinstance(candidate.get("results"), list):
                        payload = {"data": candidate["results"]}
                        found_list = True
                        break
    if payload is None:
        if args.json:
            json.dump({"ok": False, "error": "E_UPSTREAM_SHAPE", "raw": texts}, sys.stdout, ensure_ascii=False)
            print()
        else:
            print("ERROR: unrecognized Guidepoint response; no valid excerpt list", file=sys.stderr)
        return 1

    if args.json:
        if payload is not None:
            json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        else:
            json.dump({"raw": texts}, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0

    rows = (payload or {}).get("data", []) if isinstance(payload, dict) else []
    if not rows:
        print("No results." if payload is not None else "\n\n".join(texts))
        return 0
    for i, r in enumerate(rows, 1):
        resp = r.get("respondent") or {}
        who = ", ".join(x for x in [resp.get("full_name"), resp.get("title"), resp.get("company")] if x)
        print(f"[{i}] {r.get('transcript_name')} ({r.get('date')})")
        if who:
            print(f"    Expert: {who}")
        if r.get("question"):
            print(f"    Q: {r['question']}")
        if r.get("answer"):
            print(f"    A: {r['answer']}")
        cite = (r.get("source_attribution") or {}).get("markdown")
        if cite:
            print(f"    {cite}")
        print()
    return 0


def main():
    p = argparse.ArgumentParser(description="Guidepoint expert transcript search (via local MCP proxy)")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("self-test", help="check proxy + upstream connectivity")
    st.add_argument("--timeout", type=int, default=120, help="request timeout in seconds (1–600; default 120)")

    s = sub.add_parser("search", help="run search_library")
    s.add_argument("--query", required=True, help="thematic research question")
    s.add_argument("--keywords", nargs="*", help="keywords to narrow transcripts")
    s.add_argument("--size", type=int, choices=range(1, 31), metavar="1..30", default=10, help="number of ranked excerpts (1–30; default 10)")
    s.add_argument("--recency-bias", type=int, choices=range(1, 101), metavar="1..100", help="newer-result ranking bias; dates are the hard UTC calendar filter")
    s.add_argument("--start-date", help="YYYY-MM-DD")
    s.add_argument("--end-date", help="YYYY-MM-DD")
    s.add_argument("--timeout", type=int, default=120, help="request timeout in seconds (1–600; default 120)")
    s.add_argument("--json", action="store_true", help="raw JSON output")

    args = p.parse_args()
    if not 1 <= int(args.timeout) <= 600:
        print("ERROR: --timeout must be between 1 and 600 seconds", file=sys.stderr)
        sys.exit(2)
    if args.cmd == "self-test":
        sys.exit(cmd_self_test(args))
    sys.exit(cmd_search(args))


if __name__ == "__main__":
    main()
