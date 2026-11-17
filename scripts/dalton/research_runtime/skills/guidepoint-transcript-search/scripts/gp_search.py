#!/usr/bin/env python3
"""CLI for Guidepoint MCP search_library via the local auto-refresh proxy (127.0.0.1:8943/mcp).

Used by skills/subagents that need Guidepoint expert-transcript search from bash.
The proxy (LaunchAgent ai.openclaw.guidepoint-mcp-proxy) handles OAuth token refresh.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8943/mcp"


def rpc(payload, session_id=None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(BASE, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
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


def mcp_session():
    init, sid = rpc({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "eve-gp-cli", "version": "1.0"},
        },
    })
    if not init or "result" not in init:
        raise RuntimeError(f"initialize failed: {init}")
    try:
        rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    except Exception:
        pass
    return init["result"], sid


def cmd_self_test(_args):
    try:
        result, sid = mcp_session()
    except (urllib.error.URLError, ConnectionError) as e:
        print(f"FAIL: proxy unreachable at {BASE} ({e})")
        print("Hint: launchctl kickstart -k gui/$UID/ai.openclaw.guidepoint-mcp-proxy")
        return 1
    except Exception as e:
        print(f"FAIL: initialize error: {e}")
        print("Hint: token may be invalid — re-run scripts/guidepoint_mcp_oauth.py to reseed tokens")
        return 1
    server = result.get("serverInfo", {})
    print(f"OK: connected to {server.get('name')} v{server.get('version')}")
    tools, _ = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    print(f"tools: {names}")
    if "search_library" not in names:
        print("FAIL: search_library tool missing")
        return 1
    print("PASS")
    return 0


def cmd_search(args):
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
        _, sid = mcp_session()
        res, _ = rpc({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "search_library", "arguments": arguments},
        }, sid)
    except (urllib.error.URLError, ConnectionError) as e:
        print(f"ERROR: proxy unreachable at {BASE} ({e})", file=sys.stderr)
        print("Hint: launchctl kickstart -k gui/$UID/ai.openclaw.guidepoint-mcp-proxy", file=sys.stderr)
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

    payload = result.get("structuredContent")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        payload = None
        for t in texts:
            try:
                candidate = json.loads(t)
                if isinstance(candidate, dict) and isinstance(candidate.get("data"), list):
                    payload = candidate
                    break
            except (ValueError, TypeError):
                continue
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

    sub.add_parser("self-test", help="check proxy + upstream connectivity")

    s = sub.add_parser("search", help="run search_library")
    s.add_argument("--query", required=True, help="thematic research question")
    s.add_argument("--keywords", nargs="*", help="keywords to narrow transcripts")
    s.add_argument("--size", type=int, choices=range(1, 31), metavar="1..30", default=10, help="number of ranked excerpts (1–30; default 10)")
    s.add_argument("--recency-bias", type=int, choices=range(1, 101), metavar="1..100", help="newer-result ranking bias; dates are the hard UTC calendar filter")
    s.add_argument("--start-date", help="YYYY-MM-DD")
    s.add_argument("--end-date", help="YYYY-MM-DD")
    s.add_argument("--json", action="store_true", help="raw JSON output")

    args = p.parse_args()
    if args.cmd == "self-test":
        sys.exit(cmd_self_test(args))
    sys.exit(cmd_search(args))


if __name__ == "__main__":
    main()
