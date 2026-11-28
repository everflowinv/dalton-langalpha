#!/usr/bin/env python3
"""
tmtb_alphaengine_sync — pull TMT Breakout (TMTB) notes from the local AlphaEngine MCP
and index them into the market-digest vector DB (email_chunks layer).

Commands:
  sync      --date YYYY-MM-DD --period AM|PM [--lookback-days 7]
            Index TMTB docs not yet in the DB and write this run's new docs to
            state/tmtb-alphaengine/tmtb_<date>_<period>.json (merged if the file exists).
  backfill  --date-from YYYY-MM-DD --date-to YYYY-MM-DD
            Index every TMTB doc in the window that is not yet in the DB.
  self-test Check AlphaEngine MCP, auth, vector DB and current TMTB row count.

Dedup key: email_chunks.message_id = "alphaengine:<doc_id>". Safe to re-run.
Run through scripts/tmtb_alphaengine_sync.sh (uses explicit GEMINI_API_KEY from caller/env and market-digest venv).
"""

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

_DEFAULT_CODE_ROOT = Path(__file__).resolve().parent.parent


def _resolve_research_roots(default_code_root: Path = _DEFAULT_CODE_ROOT) -> tuple[Path, Path]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root


CODE_ROOT, DATA_ROOT = _resolve_research_roots()
WORKSPACE = DATA_ROOT
SKILL_SCRIPT = CODE_ROOT / "skills" / "market-digest" / "scripts" / "market_digest_cli.py"
STATE_DIR = DATA_ROOT / "state" / "tmtb-alphaengine"
if str(CODE_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(CODE_ROOT / "scripts"))
MCP_URL = "http://127.0.0.1:8950/mcp"
SOURCE_NAME = "TMT Breakout"
SENDER = "TMT Breakout (AlphaEngine TMTB)"
ID_PREFIX = "alphaengine:"
MAX_CHUNK_CHARS = 1500

JSON_MODE = False


def emit(data, code=0):
    if JSON_MODE:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        for k, v in data.items():
            if k in ("docs",):
                continue
            print(f"{k}: {v}")
    sys.exit(code)


def load_md():
    """Reuse market-digest's config / embedding / DB helpers without modifying the skill."""
    spec = importlib.util.spec_from_file_location("market_digest_cli", SKILL_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# AlphaEngine MCP (local HTTP JSON-RPC)
# ---------------------------------------------------------------------------
class AlphaEngineError(Exception):
    pass


def mcp_call(name, arguments, timeout=180):
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()
    req = urllib.request.Request(MCP_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError) as e:
        raise AlphaEngineError(f"E_MCP_UNREACHABLE: {e}") from e
    if "error" in payload:
        raise AlphaEngineError(f"E_MCP_RPC: {payload['error']}")
    result = payload.get("result", {})
    if result.get("isError"):
        raise AlphaEngineError(result["content"][0]["text"])
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def auth_ready():
    try:
        status = mcp_call("auth_status", {}, timeout=60)
    except AlphaEngineError as e:
        return False, str(e)
    return status.get("status") == "ready", status.get("status") or "unknown"


def list_tmtb_docs(date_from, date_to):
    """Union of source-filtered searches (an empty query and 'TMTB' return slightly different sets)."""
    docs = {}
    for query in ("", "TMTB"):
        args = {
            "source_names": [SOURCE_NAME],
            "date_from": date_from,
            "date_to": date_to,
            "limit": 50,
            "include_snippets": True,
        }
        if query:
            args["query"] = query
        cursor = None
        for _ in range(20):
            call_args = dict(args)
            if cursor:
                call_args["cursor"] = cursor
            res = mcp_call("search_library", call_args)
            for item in res.get("results", []):
                if SOURCE_NAME in item.get("sources", []):
                    docs.setdefault(str(item["doc_id"]), item)
            cursor = res.get("cursor")
            if not res.get("has_more") or not cursor:
                break
    return docs


def fetch_full_text(doc_id):
    parts, offset, sha = [], 0, None
    for _ in range(20):
        res = mcp_call("get_document", {"doc_id": doc_id, "offset": offset, "max_chars": 30000})
        if sha and res.get("content_sha256") and res["content_sha256"] != sha:
            raise AlphaEngineError("E_VERSION_CHANGED: document changed between pages")
        sha = res.get("content_sha256")
        parts.append(res.get("text", ""))
        if res.get("next_offset") is None:
            return "".join(parts), res.get("metadata", {})
        offset = res["next_offset"]
    raise AlphaEngineError("E_TOO_LONG: more than 20 pages")


def is_quota_error(msg):
    return "额度" in msg or "无查看权限" in msg


# ---------------------------------------------------------------------------
# Dates / chunking
# ---------------------------------------------------------------------------
def us_date(item):
    """AlphaEngine rank_date is Beijing time; TMTB writes on US Eastern time (EDT = Beijing - 12h).
    Bulk-ingested items carry a placeholder publish_time of 08:00:00, for those keep its date."""
    publish = str(item.get("publish_time") or "")
    rank = str(item.get("rank_date") or "")
    if rank and not publish.endswith("08:00:00"):
        try:
            return (datetime.strptime(rank[:16], "%Y-%m-%d %H:%M") - timedelta(hours=12)).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return publish[:10] or rank[:10]


SENTENCE_END = re.compile(r"(?<=[.!?])(?=\s*[A-Z\"“(])")


def split_text(text, limit=MAX_CHUNK_CHARS):
    """TMTB text arrives with paragraph breaks stripped, so split on sentence ends, then hard-wrap."""
    pieces = []
    for para in re.split(r"[\r\n]{2,}", text):
        para = para.strip()
        if not para:
            continue
        for sent in SENTENCE_END.split(para):
            sent = sent.strip()
            while len(sent) > limit:
                pieces.append(sent[:limit])
                sent = sent[limit:]
            if sent:
                pieces.append(sent)
    chunks, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 1 > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current} {piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def build_chunks(doc_id, title, date_str, text):
    prefix = f"From: {SENDER}\nSubject: {title}\nDate: {date_str}\nAlphaEngine doc_id: {doc_id}\n\n"
    msg_id = ID_PREFIX + doc_id
    return [
        {
            "date": date_str,
            "email_id": msg_id,
            "message_id": msg_id,
            "sender": SENDER,
            "subject": title,
            "chunk_index": i,
            "content": prefix + body,
        }
        for i, body in enumerate(split_text(text))
    ]


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def indexed_doc_ids(md):
    conn = md.init_db()
    rows = conn.execute(
        "SELECT DISTINCT message_id FROM email_chunks WHERE message_id LIKE ?", (ID_PREFIX + "%",)
    ).fetchall()
    conn.close()
    return {r[0][len(ID_PREFIX):] for r in rows}


def index_window(md, config, date_from, date_to):
    """Index all TMTB docs in [date_from, date_to] that are not in the DB yet."""
    found = list_tmtb_docs(date_from, date_to)
    from research_images import enqueue
    for doc_id,item in found.items():
        enqueue("tmtb", ID_PREFIX+doc_id, "tmtb", {"doc_id":doc_id}, {
            "title":item.get("title") or "TMTB", "date":us_date(item),
            "original_source":SENDER,"parent_document_id":"tmtb:"+doc_id,
            "date_basis":"原同步脚本日期；保留旧时差口径"})
    done = indexed_doc_ids(md)
    todo = sorted((d for d in found if d not in done), key=lambda d: str(found[d].get("rank_date") or ""))
    new_docs, pending, failures, quota_hit = [], [], [], False
    conn = md.init_db()
    try:
        for doc_id in todo:
            item = found[doc_id]
            title = item.get("title") or "TMTB"
            date_str = us_date(item)
            if quota_hit:
                pending.append({"doc_id": doc_id, "title": title, "date": date_str,
                                "snippet": item.get("snippet", "")})
                continue
            try:
                text, _meta = fetch_full_text(doc_id)
            except AlphaEngineError as e:
                msg = str(e)
                if is_quota_error(msg):
                    quota_hit = True
                    pending.append({"doc_id": doc_id, "title": title, "date": date_str,
                                    "snippet": item.get("snippet", "")})
                else:
                    failures.append({"doc_id": doc_id, "title": title, "error": msg[:300]})
                continue
            if not text.strip():
                failures.append({"doc_id": doc_id, "title": title, "error": "empty text"})
                continue
            chunks = build_chunks(doc_id, title, date_str, text)
            embeddings = md.embed_texts([c["content"] for c in chunks], config)
            if len(embeddings) != len(chunks):
                failures.append({"doc_id": doc_id, "title": title, "error": "embedding count mismatch"})
                continue
            md.store_email_chunks(conn, chunks, embeddings)  # commits per doc
            new_docs.append({
                "doc_id": doc_id,
                "title": title,
                "date": date_str,
                "rank_date": item.get("rank_date"),
                "chunks": len(chunks),
                "source_attribution": (item.get("source_attribution") or {}).get("markdown"),
                "text": text,
            })
    finally:
        conn.close()
    from research_images import drain
    images = ({"ok": True, "status": "pending", "reason": "upstream_quota_hit"}
              if quota_hit else drain("tmtb"))
    return {
        "found": len(found),
        "already_indexed": len(found) - len(todo),
        "new_docs": new_docs,
        "pending": pending,
        "failures": failures,
        "quota_hit": quota_hit,
        "image_ingestion": images,
    }


def cmd_sync(args, md, config):
    ready, status = auth_ready()
    out_file = STATE_DIR / f"tmtb_{args.date}_{args.period}.json"
    if not ready:
        emit({"ok": False, "status": "alphaengine_unavailable", "auth_status": status,
              "output_file": str(out_file) if out_file.exists() else None,
              "hint": "AlphaEngine Desktop 未就绪：app_not_running 先启动 App，no_module_page 打开任一模块页，"
                      "token_expired 才重登。下次运行会按 lookback 窗口自动补齐。"}, 1)
    date_to = args.date
    date_from = (datetime.strptime(args.date, "%Y-%m-%d") - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d")
    try:
        res = index_window(md, config, date_from, date_to)
    except AlphaEngineError as e:
        emit({"ok": False, "status": "alphaengine_unavailable", "auth_status": "search_failed",
              "reason": str(e)[:300],
              "output_file": str(out_file) if out_file.exists() else None,
              "hint": "检索失败（额度用完时报「无查看权限或额度已达上限」）。不要重试；下次运行按 lookback 窗口自动补齐。"}, 1)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    previous = []
    if out_file.exists():
        previous = json.loads(out_file.read_text()).get("docs", [])
    seen = {d["doc_id"] for d in previous}
    docs = previous + [d for d in res["new_docs"] if d["doc_id"] not in seen]
    if docs or res["pending"]:
        out_file.write_text(json.dumps({
            "date": args.date, "period": args.period, "window": [date_from, date_to],
            "docs": docs, "pending_snippets": res["pending"],
            "image_ingestion": res["image_ingestion"],
        }, ensure_ascii=False, indent=2))

    if res["new_docs"]:
        status = "new_docs"
    elif docs:
        status = "reused_existing"
    else:
        status = "no_new_docs"
    emit({
        "ok": not res["failures"] and not res["quota_hit"],
        "status": status,
        "date": args.date,
        "period": args.period,
        "window": [date_from, date_to],
        "output_file": str(out_file) if (docs or res["pending"]) else None,
        "new_count": len(res["new_docs"]),
        "count": len(docs),
        "titles": [f'{d["date"]} {d["title"]}' for d in docs],
        "pending_count": len(res["pending"]),
        "quota_hit": res["quota_hit"],
        "failures": res["failures"],
        "image_ingestion": res["image_ingestion"],
    }, 0)


def cmd_backfill(args, md, config):
    ready, status = auth_ready()
    if not ready:
        emit({"ok": False, "status": "alphaengine_unavailable", "auth_status": status}, 1)
    try:
        res = index_window(md, config, args.date_from, args.date_to)
    except AlphaEngineError as e:
        emit({"ok": False, "status": "alphaengine_unavailable", "reason": str(e)[:300]}, 1)
    emit({
        "ok": not res["failures"] and not res["quota_hit"],
        "window": [args.date_from, args.date_to],
        "found": res["found"],
        "already_indexed": res["already_indexed"],
        "indexed_now": len(res["new_docs"]),
        "chunks_now": sum(d["chunks"] for d in res["new_docs"]),
        "pending_count": len(res["pending"]),
        "pending": [f'{p["date"]} {p["title"]} ({p["doc_id"]})' for p in res["pending"]],
        "quota_hit": res["quota_hit"],
        "failures": res["failures"],
        "image_ingestion": res["image_ingestion"],
        "hint": "quota_hit=true 时 AlphaEngine 全文额度用完，次日重跑同一命令即可补齐（按 doc_id 去重）。"
        if res["quota_hit"] else None,
    }, 0)


def cmd_self_test(args, md, config):
    checks = {}
    ready, status = auth_ready()
    checks["alphaengine"] = {"ok": ready, "status": status}
    try:
        conn = md.init_db()
        n_docs, n_chunks, max_date = conn.execute(
            "SELECT COUNT(DISTINCT message_id), COUNT(*), MAX(date) FROM email_chunks WHERE message_id LIKE ?",
            (ID_PREFIX + "%",),
        ).fetchone()
        conn.close()
        checks["vector_db"] = {"ok": True, "tmtb_docs": n_docs, "tmtb_chunks": n_chunks, "latest_date": max_date}
    except Exception as e:  # noqa: BLE001
        checks["vector_db"] = {"ok": False, "error": str(e)}
    sample = "Good morning.QQQs +55bps.Oil +1.4%. In Tech, Semis +1%. " * 80
    checks["chunker"] = {"ok": all(len(c) <= MAX_CHUNK_CHARS for c in split_text(sample))}
    checks["us_date"] = {"ok": us_date({"publish_time": "2026-10-06 04:13:39",
                                         "rank_date": "2026-10-05 20:13:39"}) == "2026-10-05"}
    emit({"ok": all(c["ok"] for c in checks.values()), "checks": checks}, 0 if all(c["ok"] for c in checks.values()) else 1)


def main():
    global JSON_MODE
    parser = argparse.ArgumentParser(description="Sync AlphaEngine TMTB notes into the market-digest vector DB")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command")
    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--date", required=True, help="DIGEST_DATE (YYYY-MM-DD) from market-digest")
    p_sync.add_argument("--period", required=True, choices=["AM", "PM"])
    p_sync.add_argument("--lookback-days", type=int, default=7)
    p_back = sub.add_parser("backfill")
    p_back.add_argument("--date-from", required=True)
    p_back.add_argument("--date-to", required=True)
    sub.add_parser("self-test")
    args = parser.parse_args()
    JSON_MODE = args.json
    if not args.command:
        parser.print_help()
        sys.exit(0)
    md = load_md()
    config = md.load_config()
    {"sync": cmd_sync, "backfill": cmd_backfill, "self-test": cmd_self_test}[args.command](args, md, config)


if __name__ == "__main__":
    main()
