#!/usr/bin/env python3
"""Summarize the researchdata bridge's AlphaEngine usage ledger (B5).

Reads ``alphaengine_usage.jsonl`` — one line per served AlphaEngine call, as
written by ``researchdata_bridge.py`` — and reports:

- per day (Beijing time, Asia/Shanghai): requests, upstream calls, cache
  hits, coalesced, stale fallbacks, fresh refreshes, and failures;
- per user and per thread: search + read counts;
- documents read more than once, and queries (sha256 prefix) searched more
  than once;
- the ten threads with the most calls.

Usage:
    python3 scripts/dalton/alphaengine_usage_report.py [ledger.jsonl] [--json]

The ledger path defaults to ``~/Library/Logs/alphaengine_usage.jsonl``
(override with ``DALTON_ALPHAENGINE_USAGE_FILE``), the bridge's own default;
the rotated ``.jsonl.1`` beside it is folded in when present.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_BEIJING_TZ = ZoneInfo("Asia/Shanghai")

DEFAULT_LEDGER = os.path.expanduser(
    os.environ.get("DALTON_ALPHAENGINE_USAGE_FILE", "~/Library/Logs/alphaengine_usage.jsonl")
)

_OPS = ("search", "read", "meta", "status")

_OP_CATEGORY_MAP = {
    "search": "search",
    "search_library": "search",
    "transcript": "search",
    "list_filters": "search",
    "list_recording_tasks": "search",
    "meta": "meta",
    "get_document_info": "meta",
    "list_attachments": "meta",
    "get_recording_status": "meta",
    "get_recording_task": "meta",
    "read": "read",
    "get_document": "read",
    "status": "status",
    "auth_status": "status",
}
_META_OPERATIONS = frozenset(
    {
        "get_document_info",
        "list_attachments",
        "get_recording_status",
        "get_recording_task",
    }
)


def _clean_identity(value: object) -> str:
    text = str(value or "").strip()
    return text if text else "(空)"


def _beijing_day(ts_raw: object) -> str:
    text = str(ts_raw or "").strip()
    if not text:
        return "(无时间)"
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_BEIJING_TZ).strftime("%Y-%m-%d")
    except ValueError:
        return text[:10] if len(text) >= 10 else "(无时间)"


def load_entries(paths: list[Path]) -> list[dict]:
    entries: list[dict] = []
    for path in paths:
        if not path.is_file():
            continue
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    print(f"warning: skipping malformed line {path}:{line_no}", file=sys.stderr)
                    continue
                if isinstance(parsed, dict):
                    entries.append(parsed)
    return entries


def load_query_entries(query_log_dir: Path) -> list[dict]:
    """Load team-scope query log entries from daily rotated `YYYY-MM-DD.jsonl` files."""
    if not query_log_dir.is_dir():
        return []
    paths = sorted(p for p in query_log_dir.iterdir() if p.is_file() and p.suffix == ".jsonl")
    return load_entries(paths)


def summarize(entries: list[dict], query_entries: list[dict] | None = None) -> dict:
    by_day: dict[str, Counter] = defaultdict(Counter)
    failures_by_day: dict[str, Counter] = defaultdict(Counter)
    by_user: dict[str, Counter] = defaultdict(Counter)
    by_thread: dict[str, Counter] = defaultdict(Counter)
    by_caller_search: dict[str, Counter] = defaultdict(Counter)
    caller_types: dict[str, str] = {}
    doc_reads: Counter = Counter()
    seen_read_docs: set[str] = set()
    query_searches: Counter = Counter()
    totals: Counter = Counter()
    cacheable_stats: Counter = Counter()

    for entry in entries:
        day = _beijing_day(entry.get("ts"))
        raw_op_field = str(entry.get("op") or "").strip()
        raw_operation = str(entry.get("operation") or "").strip()
        if raw_operation in _META_OPERATIONS or raw_op_field in _META_OPERATIONS:
            op = "meta"
        else:
            op = _OP_CATEGORY_MAP.get(
                raw_op_field,
                _OP_CATEGORY_MAP.get(raw_operation, raw_op_field or "call"),
            )
        ok = bool(entry.get("ok"))
        cache_state = str(entry.get("cache") or "").strip().lower()
        upstream_flag = entry.get("upstream")
        if isinstance(upstream_flag, bool):
            is_upstream = upstream_flag
        else:
            is_upstream = cache_state not in ("hit", "coalesced")

        ws_raw = str(entry.get("workspace") or "").strip()
        user_raw = str(entry.get("user") or "").strip()
        user = _clean_identity(user_raw)
        thread = _clean_identity(entry.get("thread"))
        outcome = "ok" if ok else "fail"
        by_day[day][f"{op}.{outcome}"] += 1
        by_day[day]["total"] += 1
        if is_upstream:
            by_day[day]["upstream"] += 1
            totals["upstream_calls"] += 1
        if cache_state in ("hit", "coalesced", "stale", "fresh", "miss", "bypass", "circuit_open"):
            by_day[day][cache_state] += 1
            totals[cache_state] += 1
        if bool(entry.get("breaker_tripped")):
            by_day[day]["breaker_trips"] += 1
            totals["breaker_trips"] += 1
        if bool(entry.get("breaker_open")):
            by_day[day]["breaker_open"] += 1
            totals["breaker_open_calls"] += 1
        if not ok:
            by_day[day]["fail"] += 1
            totals["failures"] += 1
            err_cat = str(entry.get("error_category") or "").strip() or "(未分类)"
            failures_by_day[day][err_cat] += 1

        by_user[user][op] += 1
        by_user[user]["total"] += 1
        by_thread[thread][op] += 1
        by_thread[thread]["total"] += 1
        if op == "read" and raw_operation not in _META_OPERATIONS and ok:
            doc_id = str(entry.get("doc_id") or "").strip()
            if doc_id:
                doc_reads[doc_id] += 1
            if isinstance(entry.get("first_open"), bool):
                is_first = bool(entry["first_open"])
            elif doc_id:
                is_first = doc_id not in seen_read_docs
            else:
                is_first = True
            if doc_id:
                seen_read_docs.add(doc_id)
            if is_first:
                by_day[day]["read.first_open"] += 1
                totals["read_first_open"] += 1
            else:
                by_day[day]["read.repeat"] += 1
                totals["read_repeat"] += 1
        if op == "search":
            totals["search_calls"] += 1
            if not ok:
                totals["search_failures"] += 1
            raw_pages = entry.get("pages_scanned")
            pages = (
                int(raw_pages)
                if isinstance(raw_pages, int) and not isinstance(raw_pages, bool) and raw_pages > 0
                else 1
            )
            if is_upstream:
                by_day[day]["search.upstream"] += pages
                totals["search_upstream"] += pages
            elif ok:
                by_day[day]["search.cache_saved"] += 1
                totals["search_cache_saved"] += 1

            query_hash = str(entry.get("query_sha256_12") or "").strip()
            if query_hash:
                query_searches[query_hash] += 1

            caller_id = ws_raw or user_raw or "(空)"
            if caller_id not in caller_types:
                caller_types[caller_id] = (
                    "workspace" if ws_raw else ("user" if user_raw else "anonymous")
                )
            by_caller_search[caller_id]["searches"] += 1
            by_caller_search[caller_id]["ok" if ok else "failures"] += 1

            is_personal = bool(entry.get("personal_scope")) or (
                str(entry.get("bypass_reason") or "").strip().lower() == "personal_scope"
            )
            if not is_personal and cache_state in ("hit", "coalesced", "miss", "fresh", "stale"):
                cacheable_stats["total"] += 1
                if cache_state in ("hit", "coalesced"):
                    cacheable_stats["hits"] += 1
                else:
                    cacheable_stats[cache_state] += 1

    cacheable_total = cacheable_stats["total"]
    cacheable_hits = cacheable_stats["hits"]
    cacheable_stale = cacheable_stats["stale"]
    hit_rate = round(cacheable_hits / cacheable_total, 4) if cacheable_total > 0 else 0.0
    hit_rate_with_stale = (
        round((cacheable_hits + cacheable_stale) / cacheable_total, 4)
        if cacheable_total > 0
        else 0.0
    )

    caller_search_ranking = [
        {
            "caller": caller,
            "caller_type": caller_types.get(caller, "anonymous"),
            "searches": counts["searches"],
            "ok": counts["ok"],
            "failures": counts["failures"],
            "fail_rate": (
                round(counts["failures"] / counts["searches"], 4)
                if counts["searches"] > 0
                else 0.0
            ),
        }
        for caller, counts in sorted(
            by_caller_search.items(),
            key=lambda kv: (-kv[1]["searches"], -kv[1]["failures"], kv[0]),
        )
    ]

    session_company_queries: dict[tuple[str, str], list[str]] = defaultdict(list)
    query_text_counts: Counter = Counter()
    for qe in query_entries or []:
        if not isinstance(qe, dict):
            continue
        q_text = str(qe.get("query") or "").strip()
        kw_list = qe.get("keywords")
        if isinstance(kw_list, list) and kw_list:
            kw_str = ", ".join(str(k).strip() for k in kw_list if str(k).strip())
        else:
            kw_str = ""
        display_q = f"{q_text} [keywords: {kw_str}]".strip() if kw_str else q_text
        if display_q:
            query_text_counts[display_q] += 1
        sess = (
            str(qe.get("session") or qe.get("thread") or qe.get("workspace") or "").strip()
            or "(空)"
        )
        comps = qe.get("companies")
        if isinstance(comps, list):
            for comp in comps:
                c_str = str(comp or "").strip()
                if c_str and display_q:
                    session_company_queries[(sess, c_str)].append(display_q)

    multi_search_by_session_company = [
        {
            "session": sess,
            "company": comp,
            "count": len(q_list),
            "distinct_count": len(set(q_list)),
            "queries": q_list,
        }
        for (sess, comp), q_list in sorted(
            session_company_queries.items(),
            key=lambda kv: (-len(kv[1]), -len(set(kv[1])), kv[0][0], kv[0][1]),
        )
        if len(q_list) > 1
    ]

    top_repeated_queries = [
        {"query": q_text, "count": cnt}
        for q_text, cnt in query_text_counts.most_common()
        if cnt > 1
    ]

    return {
        "total_calls": len(entries),
        "search_calls": totals["search_calls"],
        "search_failures": totals["search_failures"],
        "upstream_calls": totals["upstream_calls"],
        "search_upstream_calls": totals["search_upstream"],
        "search_cache_saved": totals["search_cache_saved"],
        "read_first_open": totals["read_first_open"],
        "read_repeat": totals["read_repeat"],
        "cache_hits": totals["hit"],
        "coalesced": totals["coalesced"],
        "stale": totals["stale"],
        "fresh": totals["fresh"],
        "misses": totals["miss"],
        "bypasses": totals["bypass"],
        "circuit_open_fails": totals["circuit_open"],
        "failures": totals["failures"],
        "breaker_trips": totals["breaker_trips"],
        "breaker_open_calls": totals["breaker_open_calls"],
        "cacheable_search_hit_rate": hit_rate,
        "cacheable_search_stats": {
            "total": cacheable_total,
            "hits": cacheable_hits,
            "stale": cacheable_stale,
            "misses": cacheable_stats["miss"],
            "fresh": cacheable_stats["fresh"],
            "hit_rate": hit_rate,
            "hit_rate_with_stale": hit_rate_with_stale,
        },
        "by_day": {day: dict(counts) for day, counts in sorted(by_day.items())},
        "failures_by_day": {
            day: dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
            for day, counts in sorted(failures_by_day.items())
        },
        "caller_search_ranking": caller_search_ranking,
        "multi_search_by_session_company": multi_search_by_session_company,
        "top_repeated_queries": top_repeated_queries,
        "by_user": {
            user: {op: counts.get(op, 0) for op in (*_OPS, "total")}
            for user, counts in sorted(by_user.items())
        },
        "by_thread": {
            thread: {op: counts.get(op, 0) for op in (*_OPS, "total")}
            for thread, counts in sorted(by_thread.items())
        },
        "repeated_doc_reads": {
            doc_id: count for doc_id, count in doc_reads.most_common() if count > 1
        },
        "repeated_query_searches": {
            query_hash: count
            for query_hash, count in query_searches.most_common()
            if count > 1
        },
        "top_threads": {
            thread: counts["total"]
            for thread, counts in sorted(by_thread.items(), key=lambda kv: -kv[1]["total"])[:10]
        },
    }


def _print_report(summary: dict) -> None:
    cs = summary.get("cacheable_search_stats") or {}
    cs_total = cs.get("total", 0)
    cs_hits = cs.get("hits", 0)
    hit_rate_pct = float(summary.get("cacheable_search_hit_rate", 0.0)) * 100.0
    print(
        f"AlphaEngine 用量汇总：共 {summary['total_calls']} 次请求"
        f"（检索次数={summary.get('search_calls', 0)}，"
        f"检索消耗额度={summary.get('search_upstream_calls', 0)}，"
        f"检索缓存节省={summary.get('search_cache_saved', 0)}，"
        f"首次打开文档(计额度)={summary.get('read_first_open', 0)}，"
        f"重复打开文档(不计额度)={summary.get('read_repeat', 0)}，"
        f"命中 {summary.get('cache_hits', 0)}，"
        f"合并 {summary.get('coalesced', 0)}，"
        f"兜底 {summary.get('stale', 0)}，"
        f"刷新 {summary.get('fresh', 0)}，"
        f"失败 {summary.get('failures', 0)}，"
        f"可缓存检索命中率 {hit_rate_pct:.1f}% [{cs_hits}/{cs_total}]，"
        f"熔断触发 {summary.get('breaker_trips', 0)} 次）"
    )
    print(
        "  口径：检索次数=search 类调用数；检索消耗额度=实际请求上游的检索，"
        "含多页扫描时按 pages_scanned 计（缺省按 1）；失败次数=ok 为 false 的调用数。"
    )
    print("\n按天（北京时间）：")
    if not summary["by_day"]:
        print("  （无记录）")
    failures_by_day = summary.get("failures_by_day") or {}
    for day, counts in summary["by_day"].items():
        search = counts.get("search.ok", 0) + counts.get("search.fail", 0)
        read = counts.get("read.ok", 0) + counts.get("read.fail", 0)
        fails = counts.get("fail", sum(v for k, v in counts.items() if k.endswith(".fail")))
        breaker_info = (
            f" 熔断触发={counts['breaker_trips']}" if counts.get("breaker_trips") else ""
        )
        print(
            f"  {day}  请求={counts.get('total', 0)} 上游={counts.get('upstream', 0)}"
            f" 命中={counts.get('hit', 0)} 合并={counts.get('coalesced', 0)}"
            f" 兜底={counts.get('stale', 0)} 刷新={counts.get('fresh', 0)}"
            f" 失败={fails}{breaker_info}"
            f" | search={search}（消耗额度 {counts.get('search.upstream', 0)}，"
            f"失败 {counts.get('search.fail', 0)}，"
            f"缓存节省 {counts.get('search.cache_saved', 0)}）"
            f" read={read}（首次 {counts.get('read.first_open', 0)}，"
            f"重复 {counts.get('read.repeat', 0)}，"
            f"失败 {counts.get('read.fail', 0)}）"
        )
        day_fail_cats = failures_by_day.get(day)
        if day_fail_cats:
            cats_text = "，".join(f"{cat}={cnt}" for cat, cnt in day_fail_cats.items())
            print(f"    失败分布：{cats_text}")
    print("\n按调用方（工作区或用户）检索与失败率排行（前 15）：")
    ranking = summary.get("caller_search_ranking") or []
    if not ranking:
        print("  （无记录）")
    for item in ranking[:15]:
        rate_pct = float(item.get("fail_rate", 0.0)) * 100.0
        print(
            f"  {item['caller']} [{item['caller_type']}]: "
            f"检索={item['searches']} 失败={item['failures']} 失败率={rate_pct:.1f}%"
        )
    print("\n同一公司的多次检索排行（按会话和公司，前 15）：")
    multi_co = summary.get("multi_search_by_session_company") or []
    if not multi_co:
        print("  （无）")
    for item in multi_co[:15]:
        uniq_q = " | ".join(dict.fromkeys(item["queries"]))
        print(
            f"  {item['session']} / {item['company']}: "
            f"{item['count']} 次（{item['distinct_count']} 种查询） -> {uniq_q}"
        )
    print("\n重复最多的查询文本（>1 次，前 15）：")
    rep_q = summary.get("top_repeated_queries") or []
    if not rep_q:
        print("  （无）")
    for item in rep_q[:15]:
        print(f"  {item['count']} 次: {item['query']}")
    print("\n按用户：")
    for user, counts in summary["by_user"].items():
        print(f"  {user}: search={counts['search']} read={counts['read']} 共 {counts['total']}")
    print("\n按线程（前 10，按总次数）：")
    for thread, total in summary["top_threads"].items():
        counts = summary["by_thread"][thread]
        print(f"  {thread}: search={counts['search']} read={counts['read']} 共 {total}")
    print("\n同一文档重复读取（>1 次）：")
    if summary["repeated_doc_reads"]:
        for doc_id, count in summary["repeated_doc_reads"].items():
            print(f"  {doc_id}: {count} 次")
    else:
        print("  （无）")
    print("\n同一查询重复搜索（>1 次，按 sha256 前 12 位）：")
    if summary["repeated_query_searches"]:
        for query_hash, count in summary["repeated_query_searches"].items():
            print(f"  {query_hash}: {count} 次")
    else:
        print("  （无）")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "ledger",
        nargs="?",
        default=DEFAULT_LEDGER,
        help="ledger JSONL path (default: %(default)s)",
    )
    parser.add_argument(
        "--query-log-dir",
        default=os.environ.get("DALTON_ALPHAENGINE_QUERY_LOG_DIR", ""),
        help="team-scope query log directory (default: <ledger_dir>/alphaengine_search_queries)",
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args()

    base = Path(args.ledger).expanduser()
    paths = [base, base.with_name(base.name + ".1")]
    entries = load_entries(paths)
    if not entries:
        paths_str = ", ".join(str(p) for p in paths if p.is_file()) or str(base)
        print(f"no ledger entries found ({paths_str})", file=sys.stderr)
        return 1

    q_dir = (
        Path(args.query_log_dir).expanduser()
        if args.query_log_dir
        else (base.parent / "alphaengine_search_queries")
    )
    query_entries = load_query_entries(q_dir)
    summary = summarize(entries, query_entries=query_entries)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_report(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
