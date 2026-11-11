#!/usr/bin/env python3
"""Keyword-discover AlphaEngine earnings transcripts, then scan selected full text for AI signals."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import re
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    from .ai_finance_signal_memory import now_iso, resolve_explicit_path
except ImportError:
    from ai_finance_signal_memory import now_iso, resolve_explicit_path


class TransportUnconfigured(RuntimeError):
    """scan was asked to call AlphaEngine without an explicit transport."""


EARNINGS_TYPE_ID = "IND_SECOND_5FA442AE"
HKT = ZoneInfo("Asia/Hong_Kong")
DEFAULT_MAX_DOCUMENTS = 80
DEFAULT_SEARCH_QUERIES = [
    "artificial intelligence",
    "AI",
    "generative AI",
    "GenAI",
    "machine learning",
    "large language model",
    "LLM",
    "agentic AI",
    "AI infrastructure",
    "人工智能",
    "大模型",
    "智能体",
    "AI 算力",
]
_rpc_counter = 0
_rpc_lock = threading.Lock()


TERM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("artificial-intelligence", re.compile(r"artificial intelligence", re.I)),
    ("generative-ai", re.compile(r"generative\s+AI|GenAI|Gen[- ]AI", re.I)),
    ("ai", re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")),
    ("machine-learning", re.compile(r"machine learning", re.I)),
    ("large-language-model", re.compile(r"large language model|(?<![A-Za-z])LLMs?(?![A-Za-z])", re.I)),
    ("agentic-ai", re.compile(r"agentic|AI agents?|intelligent agents?", re.I)),
    ("copilot", re.compile(r"copilot", re.I)),
    ("inference", re.compile(r"AI inference|inference workloads?|inference chips?", re.I)),
    ("gpu-accelerator", re.compile(r"(?<![A-Za-z])GPUs?(?![A-Za-z])|AI accelerators?|accelerated computing", re.I)),
    ("ai-data-center", re.compile(r"AI data cent(?:er|re)s?|AI infrastructure|hyperscalers?", re.I)),
    ("人工智能", re.compile(r"人工智能|生成式\s*AI|大语言模型|大模型|机器学习|智能体")),
    ("算力基础设施", re.compile(r"AI算力|人工智能算力|算力集群|推理算力|训练算力|智算中心")),
]

QUANT_PATTERN = re.compile(
    r"(?:[$€£¥₹]\s?\d[\d,.]*|\d[\d,.]*\s?(?:%|bp|bps|basis points?|million|billion|"
    r"thousand|万|亿|万元|亿元|美元|欧元|英镑|人|employees?|headcount|hours?))",
    re.I,
)
IMPACT_PATTERN = re.compile(
    r"revenue|ARR|bookings?|orders?|customers?|pricing|conversion|costs?|savings?|"
    r"productivity|efficien|headcount|employees?|margins?|opex|capex|capital expenditure|"
    r"收入|营收|订单|客户|定价|转化|成本|节省|降本|效率|人效|员工|裁员|毛利|费用|资本开支",
    re.I,
)
NEGATIVE_IMPACT_PATTERN = re.compile(
    r"declin|decreas|lower|pressure|headwinds?|disrupt|cannibali[sz]|replac|"
    r"lost|loss|miss(?:ed|ing)?|delay|overrun|over budget|exceed(?:ed|s|ing)? budget|"
    r"higher costs?|cost inflation|compress(?:ed|ion)?|dilut|adverse|negative|slower|weakness|"
    r"冲击|下降|下滑|承压|挤压|超支|超预算|侵蚀|替代|流失|放缓|不及预期|"
    r"推迟|延期|亏损|拖累|负面|费用增加|成本增加|高于预期|价格压力|降价",
    re.I,
)
QA_CUE_PATTERN = re.compile(
    r"question-and-answer|questions?\s+(?:and|&)\s+answers?|Q&A|"
    r"(?:sell[- ]side|research) analyst|analyst\s*:|operator\s*:|"
    r"could you|can you|would you|how should we|what (?:is|are|was|were)|"
    r"问答|分析师|请问|能否|为什么",
    re.I,
)


def rpc_call(tool: str, arguments: dict[str, Any], timeout: int = 150) -> dict[str, Any]:
    """Placeholder. Callers pass an rpc callable; this default fails closed."""
    raise TransportUnconfigured(
        "AlphaEngine transport is not configured; pass an rpc callable or an explicit transport URL"
    )


def _http_rpc(url: str, default_timeout: int = 150) -> Callable[..., dict[str, Any]]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise TransportUnconfigured("AlphaEngine transport URL must be an explicit http(s) endpoint without credentials")

    def rpc(tool: str, arguments: dict[str, Any], timeout: int = default_timeout) -> dict[str, Any]:
        global _rpc_counter
        with _rpc_lock:
            _rpc_counter += 1
            rpc_id = _rpc_counter
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        req = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "Eve-AI-Finance-Scanner/1.0"},
        )
        with urlopen(req, timeout=timeout) as response:
            data = json.load(response)
        if data.get("error"):
            raise RuntimeError(data["error"].get("message") or str(data["error"]))
        result = data.get("result") or {}
        if result.get("isError"):
            detail = (result.get("content") or [{}])[0].get("text") or "AlphaEngine MCP tool error"
            raise RuntimeError(detail)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        return json.loads((result.get("content") or [{}])[0]["text"])

    return rpc


def rpc_from_transport(transport: Any, *, timeout: int = 150) -> Callable[..., dict[str, Any]]:
    """Adapt an explicit callable or URL into search_library/auth_status/get_document calls."""
    if isinstance(transport, str):
        return _http_rpc(transport, timeout)
    if isinstance(transport, Mapping):
        callback = transport.get("call", transport.get("rpc"))
        if callback is not None:
            return rpc_from_transport(callback, timeout=timeout)
        url = transport.get("url")
        if url:
            return _http_rpc(str(url), timeout)
        raise TransportUnconfigured(
            "AlphaEngine transport is not configured; pass an rpc callable or an explicit transport URL"
        )
    if not callable(transport):
        raise TransportUnconfigured(
            "AlphaEngine transport is not configured; pass an rpc callable or an explicit transport URL"
        )
    try:
        parameters = inspect.signature(transport).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_timeout = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or name == "timeout"
        for name, parameter in parameters.items()
    )

    def rpc(tool: str, arguments: dict[str, Any], timeout: int = timeout) -> dict[str, Any]:
        if accepts_timeout:
            return transport(tool, arguments, timeout=timeout)
        return transport(tool, arguments)

    return rpc


def parse_hkt(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HKT)
    return parsed.astimezone(HKT)


def item_time(item: dict[str, Any]) -> tuple[datetime | None, str]:
    rank = parse_hkt(item.get("rank_date"))
    if rank:
        return rank, "rank_date"
    return parse_hkt(item.get("publish_time")), "publish_time_fallback"


def merge_search_item(
    collected: dict[str, dict[str, Any]], item: dict[str, Any], query: str
) -> None:
    doc_id = str(item.get("doc_id") or "")
    if not doc_id:
        return
    if doc_id not in collected:
        collected[doc_id] = dict(item)
        collected[doc_id]["matched_queries"] = [query]
        return
    existing = collected[doc_id]
    existing["matched_queries"] = sorted(set(existing.get("matched_queries") or []) | {query})
    for key, value in item.items():
        if value and not existing.get(key):
            existing[key] = value


def search_transcripts(
    queries: list[str],
    date_from: str,
    date_to: str,
    rpc: Callable[..., dict[str, Any]] = rpc_call,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, int]]]:
    collected: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    query_stats: dict[str, dict[str, int]] = {}
    for query in queries:
        cursor: Any = None
        returned = 0
        pages = 0
        for _ in range(30):
            call_args: dict[str, Any] = {
                "query": query,
                "date_from": date_from,
                "date_to": date_to,
                "type_ids": [EARNINGS_TYPE_ID],
                "include_snippets": True,
                "sort": "relevance",
                "limit": 50,
            }
            if cursor:
                call_args["cursor"] = cursor
            try:
                page = rpc("search_library", call_args)
            except Exception as exc:
                errors.append({
                    "query": query,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                break
            pages += 1
            page_results = page.get("results") or []
            returned += len(page_results)
            for item in page_results:
                merge_search_item(collected, item, query)
            if not page.get("has_more") or not page.get("cursor"):
                break
            cursor = page["cursor"]
        else:
            errors.append({"query": query, "error": "pagination exceeded 30 pages"})
        query_stats[query] = {"pages": pages, "returned": returned}
    return list(collected.values()), errors, query_stats


def discovery_score(item: dict[str, Any]) -> int:
    text = "\n".join(str(item.get(key) or "") for key in ("title", "snippet"))
    matches, counts = match_spans(text)
    query_count = len(item.get("matched_queries") or [])
    impact_hits = len(IMPACT_PATTERN.findall(text))
    quant_hits = len(QUANT_PATTERN.findall(text))
    negative_hits = len(NEGATIVE_IMPACT_PATTERN.findall(text))
    qa_hits = len(QA_CUE_PATTERN.findall(text))
    return (
        query_count * 100
        + len(counts) * 20
        + min(len(matches), 20) * 3
        + min(impact_hits, 20) * 6
        + min(quant_hits, 10) * 7
        + min(negative_hits, 10) * 8
        + min(qa_hits, 10) * 5
    )


def fetch_full_document(doc_id: str, rpc: Callable[..., dict[str, Any]] = rpc_call) -> dict[str, Any]:
    offset = 0
    chunks: list[str] = []
    metadata: dict[str, Any] = {}
    expected_sha: str | None = None
    total_chars: int | None = None
    for _ in range(100):
        part = rpc(
            "get_document",
            {"doc_id": doc_id, "offset": offset, "max_chars": 30000},
        )
        metadata = metadata or (part.get("metadata") or {})
        total_chars = int(part.get("content_chars") or 0)
        sha = part.get("content_sha256")
        if expected_sha and sha and expected_sha != sha:
            raise RuntimeError(f"content changed while reading {doc_id}")
        expected_sha = expected_sha or sha
        text = str(part.get("text") or "")
        chunks.append(text)
        next_offset = part.get("next_offset")
        if next_offset is None:
            break
        if int(next_offset) <= offset:
            raise RuntimeError(f"non-advancing offset for {doc_id}")
        offset = int(next_offset)
    else:
        raise RuntimeError(f"document {doc_id} exceeded 100 chunks")
    body = "".join(chunks)
    actual_sha = hashlib.sha256(body.encode("utf-8")).hexdigest() if body else None
    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError(f"sha256 mismatch for {doc_id}")
    if total_chars is not None and len(body) != total_chars:
        raise RuntimeError(f"character count mismatch for {doc_id}: {len(body)} != {total_chars}")
    return {"metadata": metadata, "text": body, "sha256": actual_sha, "content_chars": len(body)}


def match_spans(text: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    matches: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for label, pattern in TERM_PATTERNS:
        found = list(pattern.finditer(text))
        if found:
            counts[label] = len(found)
            matches.extend({"start": match.start(), "end": match.end(), "label": label} for match in found)
    matches.sort(key=lambda match: match["start"])
    return matches, counts


def context_windows(text: str, matches: list[dict[str, Any]], max_chars: int = 7000) -> str:
    if not matches:
        return ""
    spans: list[list[int]] = []
    for match in matches:
        start = max(0, int(match["start"]) - 900)
        end = min(len(text), int(match["end"]) + 1500)
        if spans and start <= spans[-1][1] + 250:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])
    ranked: list[tuple[int, int, int, str]] = []
    for start, end in spans:
        chunk = text[start:end].strip()
        score = (
            len(QUANT_PATTERN.findall(chunk)) * 3
            + len(IMPACT_PATTERN.findall(chunk)) * 2
            + len(NEGATIVE_IMPACT_PATTERN.findall(chunk)) * 6
            + len(QA_CUE_PATTERN.findall(chunk)) * 3
        )
        ranked.append((score, start, end, chunk))
    selected: list[tuple[int, int, str]] = []
    used = 0
    for _, start, end, chunk in sorted(ranked, key=lambda item: item[0], reverse=True):
        if not chunk:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunk = chunk[:remaining]
        selected.append((start, end, chunk))
        used += len(chunk)
    selected.sort(key=lambda item: item[0])
    return "\n\n--- AI MATCH CONTEXT ---\n\n".join(chunk for _, _, chunk in selected)


def scan(args: argparse.Namespace) -> dict[str, Any]:
    rpc = rpc_from_transport(getattr(args, "transport", None))
    auth = rpc("auth_status", {})
    if not auth.get("authenticated"):
        raise RuntimeError("AlphaEngine Desktop is not authenticated")
    now_hkt = getattr(args, "now", None) or datetime.now(HKT)
    if now_hkt.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now_hkt = now_hkt.astimezone(HKT)
    cutoff = now_hkt - timedelta(hours=args.hours)
    queries = args.query or DEFAULT_SEARCH_QUERIES
    raw, search_errors, query_stats = search_transcripts(
        queries, cutoff.date().isoformat(), now_hkt.date().isoformat(), rpc
    )
    in_window = []
    fallback_count = 0
    for item in raw:
        timestamp, time_source = item_time(item)
        if time_source == "publish_time_fallback":
            fallback_count += 1
        if timestamp and cutoff <= timestamp <= now_hkt:
            item = dict(item)
            item["window_time_source"] = time_source
            item["discovery_score"] = discovery_score(item)
            in_window.append(item)
    in_window.sort(
        key=lambda item: (
            item.get("discovery_score") or 0,
            item_time(item)[0] or datetime.min.replace(tzinfo=HKT),
        ),
        reverse=True,
    )
    matched_in_window = len(in_window)
    selected = in_window[: args.max_documents]

    candidates: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    fetched_map: dict[str, dict[str, Any] | Exception] = {}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(fetch_full_document, str(item.get("doc_id")), rpc): item for item in selected
        }
        for future in as_completed(futures):
            item = futures[future]
            doc_id = str(item.get("doc_id"))
            try:
                fetched_map[doc_id] = future.result()
            except Exception as exc:
                fetched_map[doc_id] = exc

    candidate_dir = resolve_explicit_path(args.candidate_dir)
    relative_root = getattr(args, "relative_root", None)
    if relative_root is not None:
        relative_root = resolve_explicit_path(relative_root)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    for item in selected:
        doc_id = str(item.get("doc_id"))
        fetched = fetched_map[doc_id]
        if isinstance(fetched, Exception):
            error = f"{type(fetched).__name__}: {fetched}"
            documents.append({"doc_id": doc_id, "status": "fetch_error", "error": error})
            continue
        body = str(fetched.get("text") or "")
        matches, counts = match_spans(body)
        context = context_windows(body, matches)
        quant_hits = len(QUANT_PATTERN.findall(context))
        negative_impact_hits = len(NEGATIVE_IMPACT_PATTERN.findall(context))
        qa_cue_hits = len(QA_CUE_PATTERN.findall(context))
        status = "temporary_ai_candidate" if matches else "temporary_no_ai_match"
        documents.append({"doc_id": doc_id, "status": status})
        if matches:
            safe_doc_id = re.sub(r"[^A-Za-z0-9_-]", "_", doc_id)
            full_text_path = candidate_dir / f"{safe_doc_id}.txt"
            full_text_path.write_text(body, encoding="utf-8")
            if relative_root is not None:
                full_text_value = full_text_path.resolve().relative_to(relative_root).as_posix()
            else:
                full_text_value = str(full_text_path.resolve())
            candidates.append({
                "doc_id": doc_id,
                "title": item.get("title"),
                "companies": item.get("companies") or fetched.get("metadata", {}).get("companies") or [],
                "industries": item.get("industries") or fetched.get("metadata", {}).get("industries") or [],
                "publish_time": item.get("publish_time"),
                "rank_date": item.get("rank_date"),
                "matched_queries": item.get("matched_queries") or [],
                "discovery_score": item.get("discovery_score"),
                "content_chars": fetched.get("content_chars"),
                "ai_match_count": len(matches),
                "quant_hits": quant_hits,
                "negative_impact_hits": negative_impact_hits,
                "qa_cue_hits": qa_cue_hits,
                "negative_qa_priority": bool(negative_impact_hits and qa_cue_hits),
                "match_terms": sorted(counts),
                "context": context,
                "full_text_path": full_text_value,
            })
    candidates.sort(
        key=lambda item: (
            item["negative_qa_priority"],
            item["negative_impact_hits"],
            item["quant_hits"],
            item["ai_match_count"],
        ),
        reverse=True,
    )
    errors = [document for document in documents if document["status"] == "fetch_error"]
    return {
        "generated_at": now_iso(),
        "window": {
            "timezone": "Asia/Hong_Kong",
            "start": cutoff.isoformat(),
            "end": now_hkt.isoformat(),
            "hours": args.hours,
        },
        "coverage": {
            "search_mode": "keyword_first_then_full_text",
            "selection_policy": "relevance_ranked_top_n",
            "selection_score": (
                "matched_queries*100 + distinct_ai_terms*20 + ai_matches*3 + "
                "business_impact_hits*6 + quantitative_hits*7 + "
                "negative_impact_hits*8 + qa_cue_hits*5; rank_date tie-break"
            ),
            "search_queries": queries,
            "search_query_count": len(queries),
            "search_query_errors": len(search_errors),
            "search_query_stats": query_stats,
            "keyword_matches_dedup_date_range": len(raw),
            "keyword_matches_in_exact_window": matched_in_window,
            "full_text_budget": args.max_documents,
            "selected_for_full_text": len(selected),
            "skipped_due_to_budget": max(0, matched_in_window - len(selected)),
            "fetched_this_run": len(selected) - len(errors),
            "full_text_scanned": len(selected) - len(errors),
            "ai_candidates": len(candidates),
            "negative_qa_priority_candidates": sum(
                1 for candidate in candidates if candidate["negative_qa_priority"]
            ),
            "no_ai_match": sum(1 for document in documents if document["status"] == "temporary_no_ai_match"),
            "fetch_errors": len(errors),
            "persisted_to_signal_memory": 0,
            "retention_policy": "Only model-confirmed signals are ingested later; scan results are temporary.",
            "publish_time_fallback_seen_before_window_filter": fallback_count,
            "market_filters": [],
            "industry_filters": [],
            "market_cap_filters": [],
        },
        "selected_documents": [
            {
                "selection_rank": rank,
                "doc_id": str(item.get("doc_id") or ""),
                "title": item.get("title"),
                "rank_date": item.get("rank_date"),
                "matched_queries": item.get("matched_queries") or [],
                "discovery_score": item.get("discovery_score"),
                "status": next(
                    (
                        document["status"]
                        for document in documents
                        if document.get("doc_id") == str(item.get("doc_id") or "")
                    ),
                    "unknown",
                ),
            }
            for rank, item in enumerate(selected, start=1)
        ],
        "search_errors": search_errors,
        "errors": errors,
        "candidates": candidates,
    }


def self_test() -> None:
    sample = (
        "Revenue grew 8%. Our generative AI assistant reached $12 million ARR. "
        "AI automation reduced support costs by 15% and 200 basis points."
    )
    matches, counts = match_spans(sample)
    context = context_windows(sample, matches)
    assert counts["generative-ai"] == 1
    assert len(QUANT_PATTERN.findall(context)) >= 2
    assert parse_hkt("2026-07-22 10:00:00").tzinfo is not None
    merged: dict[str, dict[str, Any]] = {}
    merge_search_item(merged, {"doc_id": "demo", "title": "AI earnings"}, "AI")
    merge_search_item(merged, {"doc_id": "demo", "snippet": "GenAI ARR $12m"}, "generative AI")
    assert merged["demo"]["matched_queries"] == ["AI", "generative AI"]
    assert discovery_score(merged["demo"]) > 0
    generic = {
        "doc_id": "generic",
        "title": "Earnings call",
        "snippet": "We continue to invest in AI.",
        "matched_queries": ["AI"],
    }
    assert discovery_score(merged["demo"]) > discovery_score(generic)
    negative_qa = (
        "Analyst: Could you explain why AI spending exceeded budget and compressed "
        "gross margin by 300 basis points?"
    )
    negative_matches, _ = match_spans(negative_qa)
    negative_context = context_windows(negative_qa, negative_matches)
    assert NEGATIVE_IMPACT_PATTERN.search(negative_context)
    assert QA_CUE_PATTERN.search(negative_context)
    print(json.dumps({"ok": True, "tests": 8}))


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    scan_cmd = sub.add_parser("scan")
    scan_cmd.add_argument("--hours", type=float, default=24)
    scan_cmd.add_argument("--workers", type=int, default=4)
    scan_cmd.add_argument("--max-documents", type=int, default=DEFAULT_MAX_DOCUMENTS)
    scan_cmd.add_argument(
        "--query",
        action="append",
        help="AlphaEngine keyword; repeat to override the default AI query set",
    )
    scan_cmd.add_argument("--candidate-dir", type=Path, default=None)
    scan_cmd.add_argument("--output", type=Path)
    scan_cmd.add_argument("--transport-url", help="explicit AlphaEngine MCP endpoint; no token is read")
    scan_cmd.add_argument("--relative-root", type=Path)
    scan_cmd.add_argument("--now", help="fixed timezone-aware ISO timestamp")

    sub.add_parser("self-test")
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
        return 0
    if args.max_documents < 1:
        parser.error("--max-documents must be at least 1")
    if not args.transport_url:
        sys.stderr.write(json.dumps({
            "ok": False,
            "error": "TransportUnconfigured",
            "message": "AlphaEngine transport is not configured; pass --transport-url or inject an rpc callable",
        }, ensure_ascii=False) + "\n")
        return 1
    args.transport = {"url": args.transport_url}
    if args.now:
        args.now = datetime.fromisoformat(args.now)
        if args.now.tzinfo is None:
            parser.error("--now must include a timezone")
    if args.candidate_dir is None:
        args.candidate_dir = Path(__file__).resolve().parents[3] / "temp" / "ai_finance_earnings_candidates"
    result = scan(args)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = resolve_explicit_path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
