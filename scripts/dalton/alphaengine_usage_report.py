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


def summarize(entries: list[dict]) -> dict:
    by_day: dict[str, Counter] = defaultdict(Counter)
    by_user: dict[str, Counter] = defaultdict(Counter)
    by_thread: dict[str, Counter] = defaultdict(Counter)
    doc_reads: Counter = Counter()
    query_searches: Counter = Counter()
    totals: Counter = Counter()

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

        user = _clean_identity(entry.get("user"))
        thread = _clean_identity(entry.get("thread"))
        outcome = "ok" if ok else "fail"
        by_day[day][f"{op}.{outcome}"] += 1
        by_day[day]["total"] += 1
        if is_upstream:
            by_day[day]["upstream"] += 1
            totals["upstream_calls"] += 1
        if cache_state in ("hit", "coalesced", "stale", "fresh", "miss", "bypass"):
            by_day[day][cache_state] += 1
            totals[cache_state] += 1
        if not ok:
            by_day[day]["fail"] += 1
            totals["failures"] += 1

        by_user[user][op] += 1
        by_user[user]["total"] += 1
        by_thread[thread][op] += 1
        by_thread[thread]["total"] += 1
        if op == "read" and raw_operation not in _META_OPERATIONS and ok:
            doc_id = str(entry.get("doc_id") or "").strip()
            if doc_id:
                doc_reads[doc_id] += 1
        if op == "search":
            query_hash = str(entry.get("query_sha256_12") or "").strip()
            if query_hash:
                query_searches[query_hash] += 1

    return {
        "total_calls": len(entries),
        "upstream_calls": totals["upstream_calls"],
        "cache_hits": totals["hit"],
        "coalesced": totals["coalesced"],
        "stale": totals["stale"],
        "fresh": totals["fresh"],
        "misses": totals["miss"],
        "bypasses": totals["bypass"],
        "failures": totals["failures"],
        "by_day": {day: dict(counts) for day, counts in sorted(by_day.items())},
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
    print(
        f"AlphaEngine 用量汇总：共 {summary['total_calls']} 次请求"
        f"（调上游 {summary.get('upstream_calls', 0)}，"
        f"命中 {summary.get('cache_hits', 0)}，"
        f"合并 {summary.get('coalesced', 0)}，"
        f"兜底 {summary.get('stale', 0)}，"
        f"刷新 {summary.get('fresh', 0)}，"
        f"失败 {summary.get('failures', 0)}）"
    )
    print("\n按天（北京时间）：")
    if not summary["by_day"]:
        print("  （无记录）")
    for day, counts in summary["by_day"].items():
        search = counts.get("search.ok", 0) + counts.get("search.fail", 0)
        read = counts.get("read.ok", 0) + counts.get("read.fail", 0)
        fails = counts.get("fail", sum(v for k, v in counts.items() if k.endswith(".fail")))
        print(
            f"  {day}  请求={counts.get('total', 0)} 上游={counts.get('upstream', 0)}"
            f" 命中={counts.get('hit', 0)} 合并={counts.get('coalesced', 0)}"
            f" 兜底={counts.get('stale', 0)} 刷新={counts.get('fresh', 0)}"
            f" 失败={fails} | search={search}（失败 {counts.get('search.fail', 0)}）"
            f" read={read}（失败 {counts.get('read.fail', 0)}）"
        )
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
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args()

    base = Path(args.ledger).expanduser()
    paths = [base, base.with_name(base.name + ".1")]
    entries = load_entries(paths)
    if not entries:
        paths_str = ", ".join(str(p) for p in paths if p.is_file()) or str(base)
        print(f"no ledger entries found ({paths_str})", file=sys.stderr)
        return 1

    summary = summarize(entries)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        _print_report(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
