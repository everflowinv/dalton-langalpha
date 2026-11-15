#!/usr/bin/env python3
"""
sales_notes_search.py — per-company sweep of Dalton internal libraries for richard-coverage-weekly.

Calls Dalton's ``tools.researchdata`` / ``mcp_client`` ``research`` tool:
  - sales-notes, tmtb, funda-ai: keyword search per company inside [date-from, date-to];
  - company-wiki: documents *indexed* since --date-from, kept when filed under the company
    or mentioning it at least twice.
Every hit is read in full once (shared across companies), re-checked with a case-sensitive
word-boundary keyword pattern (CJK substrings), and excerpted around matching passages.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _load_richard_cov_io() -> Any:
    cached = sys.modules.get("richard_cov_io")
    if cached is not None:
        return cached
    target = Path(__file__).resolve().with_name("richard_cov_io.py")
    spec = importlib.util.spec_from_file_location("richard_cov_io", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["richard_cov_io"] = mod
    spec.loader.exec_module(mod)
    return mod


_io = _load_richard_cov_io()
workspace_file = _io.workspace_file
resolve_workspace_target = _io.resolve_workspace_target
write_target_text = _io.write_target_text
_transport = _io.load_sibling("richard_cov_transport", "richard_cov_transport.py")
call_bridge_jsonrpc = _transport.call_bridge_jsonrpc
extract_nested_value = _transport.extract_nested_value
extract_route_items = _transport.extract_route_items
unwrap_bridge_response = _transport.unwrap_bridge_response

MAIL_SOURCES = ("sales-notes", "tmtb", "funda-ai")
# Recall stage is case-insensitive "any term"; these words only add noise to ranking.
STOP = {
    "of",
    "the",
    "and",
    "inc",
    "inc.",
    "co",
    "co.",
    "corp",
    "corp.",
    "ltd",
    "ltd.",
    "limited",
    "group",
    "holdings",
    "company",
    "international",
    "technologies",
    "technology",
    "plc",
}
HEADER = re.compile(r"^(From|Subject|Date|AlphaEngine doc_id):.*$", re.M)
SEARCH_LIMIT = 100
EXCERPT_PER_DOC = 1500
# picks 名额按来源分层排：销售邮件和 TMTB 先排满，其次 FUNDA AI，wiki 最后；同层内按命中次数、日期。
SOURCE_TIER = {"sales-notes": 0, "tmtb": 0, "funda-ai": 1, "company-wiki": 2}

ResearchCaller = Callable[[dict[str, Any]], dict[str, Any]]


def keyword_pattern(kws: list[str]) -> re.Pattern[str]:
    """Compile case-sensitive word-boundary regex for ASCII terms and substring match for CJK."""
    parts: list[str] = []
    for kw in (item.strip() for item in kws if item and item.strip()):
        if re.match(r"^[A-Za-z0-9 .,&']+$", kw):
            parts.append(r"\b" + re.escape(kw) + r"\b")
        else:
            parts.append(re.escape(kw))
    if not parts:
        return re.compile(r"(?!)")
    return re.compile("|".join(parts))


def target_kws(target: dict[str, Any]) -> list[str]:
    """Return stripped search keywords for a target company record."""
    raw = target.get("kw") or [target.get("name", ""), target.get("ticker", "")]
    return [k.strip() for k in raw if isinstance(k, str) and k.strip()]


def excerpt(text: str, pat: re.Pattern[str], limit: int = EXCERPT_PER_DOC) -> str:
    """Build merged windows around each keyword mention without bleeding into adjacent tickers."""
    spans: list[list[int]] = []
    for match in pat.finditer(text):
        a, b = max(0, match.start() - 300), min(len(text), match.start() + 900)
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    out: list[str] = []
    total = 0
    for a, b in spans:
        piece = re.sub(r"\s+", " ", text[a:b]).strip()
        if total + len(piece) > limit:
            piece = piece[: max(0, limit - total)]
            if piece:
                out.append(piece + " …")
            break
        out.append(piece)
        total += len(piece)
    return " … ".join(out)


def _default_research_caller(arguments: dict[str, Any]) -> dict[str, Any]:
    return call_bridge_jsonrpc("research", arguments)


_TICKER_SPLIT = re.compile(r"[\s,/]+")
_CANONICAL_WIKI_ALIASES: dict[str, tuple[str, ...]] = {
    "0700.HK": ("TENCENT",),
    "3690.HK": ("MEITUAN",),
    "9988.HK": ("ALIBABA", "BABA"),
    "9618.HK": ("JD", "JD.COM", "JD_COM"),
    "1024.HK": ("KUAISHOU",),
    "1810.HK": ("XIAOMI",),
    "9992.HK": ("POP_MART", "POP MART", "POPMART"),
    "6181.HK": ("LAOPU", "LAOPU_GOLD"),
    "2513.HK": ("ZHIPU",),
    "0100.HK": ("MINIMAX",),
    "0625.HK": ("SHEIN",),
}


def _add_canonical_token(raw: str, own: set[str], aliases: dict[str, str]) -> None:
    cleaned = raw.strip()
    if not cleaned:
        return
    upper = cleaned.upper()
    own.add(upper)
    own.add(re.sub(r"[\s_-]+", "_", upper))
    own.add(re.sub(r"[\s_-]+", "", upper))
    mapped = aliases.get(cleaned.lower())
    if mapped:
        own.add(mapped.upper())
    for canonical in _CANONICAL_WIKI_ALIASES.get(upper, ()):
        own.add(canonical)


def _own_company_tokens(target: dict[str, Any], kws: list[str], aliases: dict[str, str]) -> set[str]:
    """Match mark_weekly._own_tokens: ticker pieces/stems, company name, aliases, and canonical wiki folders."""
    own: set[str] = set()
    ticker = str(target.get("ticker") or "").strip().upper()
    if ticker:
        _add_canonical_token(ticker, own, aliases)
        for piece in _TICKER_SPLIT.split(ticker):
            code = piece.strip()
            if not code:
                continue
            _add_canonical_token(code, own, aliases)
            if "." in code:
                _add_canonical_token(code.split(".", 1)[0], own, aliases)
    for kw in [str(target.get("name") or ""), *kws, *(target.get("aliases") or [])]:
        if isinstance(kw, str) and kw.strip():
            _add_canonical_token(kw, own, aliases)
    return own - {""}


def unified_scan(
    targets: list[dict[str, Any]],
    date_from: str,
    date_to: str,
    *,
    per_company: int = 12,
    caller: ResearchCaller | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
    """Sweep sales-notes/tmtb/funda-ai/company-wiki for all targets via Dalton research bridge."""
    invoke = caller or _default_research_caller
    alias_map = {k.lower(): v.upper() for k, v in (aliases or {}).items()}
    warnings: list[str] = []
    cache: dict[str, tuple[str, dict[str, Any]]] = {}

    def read_full(did: str) -> tuple[str, dict[str, Any]]:
        if did in cache:
            return cache[did]
        parts: list[str] = []
        offset = 0
        meta: dict[str, Any] = {}
        for _ in range(30):
            resp = unwrap_bridge_response(
                invoke(
                    {
                        "route": "all",
                        "operation": "read",
                        "document": did,
                        "params": {"offset": offset, "max_chars": 30000},
                    }
                )
            )
            doc = extract_nested_value(resp, "document") or {}
            if not resp.get("ok") or not isinstance(doc, dict) or not doc:
                break
            if not meta:
                meta = {k: doc.get(k) for k in ("title", "date", "original_source", "location", "source")}
            parts.append(str(doc.get("content") or ""))
            nxt = doc.get("next_offset")
            if not nxt or nxt == offset:
                break
            offset = int(nxt)
        cache[did] = (HEADER.sub("", "".join(parts)), meta)
        return cache[did]

    wiki = unwrap_bridge_response(
        invoke(
            {
                "route": "company-wiki",
                "operation": "list",
                "limit": SEARCH_LIMIT,
                "params": {"indexed_since": date_from, "media": "text"},
            }
        )
    )
    wiki_docs = extract_route_items(wiki)
    if not wiki.get("ok"):
        warnings.append(f"company-wiki list 失败：{wiki.get('error') or wiki.get('status')}")
    elif len(wiki_docs) >= SEARCH_LIMIT:
        warnings.append("company-wiki 窗口内新入库超过 100 篇，只看了最新 100 篇")

    out: list[dict[str, Any]] = []
    source_errors: dict[str, Any] = {}
    for target in targets:
        kws = target_kws(target)
        pat = keyword_pattern(kws)
        terms = [w for w in re.findall(r"[\w.-]+", " ".join(kws).casefold()) if w not in STOP]
        query = " ".join(dict.fromkeys(terms)) or " ".join(kws)
        found: dict[str, dict[str, Any]] = {}
        limit_hit: list[str] = []

        for src in MAIL_SOURCES:
            resp = unwrap_bridge_response(
                invoke(
                    {
                        "route": src,
                        "operation": "search",
                        "query": query,
                        "start": date_from,
                        "end": date_to,
                        "limit": SEARCH_LIMIT,
                        "params": {"search_mode": "keyword", "media": "text"},
                    }
                )
            )
            if not resp.get("ok"):
                source_errors[src] = resp.get("error") or resp.get("status")
                continue
            rows = extract_route_items(resp)
            if len(rows) >= SEARCH_LIMIT:
                # 短词（如 H World 的 H、UP Fintech 的 UP）会把窗口全召回，按别名逐个补查并合并；
                # 仅当单个别名仍触及 SEARCH_LIMIT 才记为 limit_hit。
                full = False
                for kw in kws:
                    kq = " ".join(w for w in re.findall(r"[\w.-]+", kw.casefold()) if w not in STOP) or kw
                    rk = unwrap_bridge_response(
                        invoke(
                            {
                                "route": src,
                                "operation": "search",
                                "query": kq,
                                "start": date_from,
                                "end": date_to,
                                "limit": SEARCH_LIMIT,
                                "params": {"search_mode": "keyword", "media": "text"},
                            }
                        )
                    )
                    more = extract_route_items(rk)
                    full = full or len(more) >= SEARCH_LIMIT
                    rows.extend(more)
                if full:
                    limit_hit.append(src)
            for row in rows:
                did = row.get("document_id")
                if isinstance(did, str) and did and not did.startswith("image:"):
                    found.setdefault(
                        did,
                        {
                            "source": src,
                            "date": row.get("date"),
                            "sender": str(row.get("original_source") or "")[:80],
                            "subject": str(row.get("title") or "")[:160],
                        },
                    )

        own = _own_company_tokens(target, kws, alias_map)
        for row in wiki_docs:
            did = row.get("document_id")
            if isinstance(did, str) and did:
                comp = str(row.get("company") or "").strip().upper()
                comp_slug = re.sub(r"[\s_-]+", "_", comp)
                comp_compact = re.sub(r"[\s_-]+", "", comp)
                is_own = bool(comp and (comp in own or comp_slug in own or comp_compact in own))
                found.setdefault(
                    did,
                    {
                        "source": "company-wiki",
                        "date": row.get("date"),
                        "sender": str(row.get("original_source") or "")[:80],
                        "subject": str(row.get("title") or "")[:160],
                        "own": is_own,
                    },
                )

        picks: list[dict[str, Any]] = []
        for did, info in found.items():
            text, _ = read_full(did)
            hits = len(pat.findall(text)) if text else 0
            # 行业稿顺带提一句的 wiki 不要；归在本公司目录下的 wiki 一律要
            if info["source"] == "company-wiki" and not info.get("own") and hits < 2:
                continue
            if not hits and not info.get("own"):
                continue
            picks.append(
                {
                    "source": info["source"],
                    "document_id": did,
                    "date": info["date"],
                    "sender": info["sender"],
                    "subject": info["subject"],
                    "hits": hits,
                    "excerpt": excerpt(text, pat) if hits else text[:EXCERPT_PER_DOC],
                }
            )

        picks.sort(
            key=lambda p: (-SOURCE_TIER.get(p["source"], 9), p["hits"], p["date"] or ""),
            reverse=True,
        )
        by_source: dict[str, int] = {}
        for pick in picks:
            by_source[pick["source"]] = by_source.get(pick["source"], 0) + 1
        rest = picks[per_company:]
        out.append(
            {
                "name": target["name"],
                "ticker": target.get("ticker"),
                "matched_docs": len(picks),
                "by_source": by_source,
                "omitted": len(rest),
                "limit_hit": limit_hit,
                "picks": picks[:per_company],
                "omitted_list": [
                    {k: p[k] for k in ("source", "document_id", "date", "subject", "hits")} for p in rest
                ],
            }
        )

    if source_errors:
        if len(source_errors) == len(MAIL_SOURCES):
            raise RuntimeError("all mail sources failed: " + json.dumps(source_errors, ensure_ascii=False))
        warnings.append("部分来源失败：" + json.dumps(source_errors, ensure_ascii=False))
    return out, {"docs_read": len(cache), "wiki_indexed_in_window": len(wiki_docs)}, warnings


def _read_path(path_arg: str, workspace_dir: str | None) -> Path:
    return resolve_workspace_target(Path(workspace_dir) if workspace_dir else None, path_arg)


def main() -> None:
    ap = argparse.ArgumentParser(description="Sales notes / TMTB / FUNDA AI / wiki search per company")
    ap.add_argument("--targets", required=True)
    ap.add_argument("--date-from", required=True)
    ap.add_argument("--date-to", required=True)
    ap.add_argument("--per-company", type=int, default=12)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workspace-dir", default=None)
    args = ap.parse_args()

    targets_path = _read_path(args.targets, args.workspace_dir)
    targets = json.loads(targets_path.read_text(encoding="utf-8"))
    if not isinstance(targets, list):
        print(json.dumps({"ok": False, "error": "targets must be a JSON array"}, ensure_ascii=False))
        sys.exit(1)

    try:
        out, stats, warnings = unified_scan(
            targets,
            args.date_from,
            args.date_to,
            per_company=args.per_company,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "ok": False,
                    "engine": "dalton_researchdata",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "hint": "检查沙箱内 tools.researchdata / researchdata 桥接与内部库服务可用性",
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)

    payload_text = json.dumps(out, ensure_ascii=False, indent=1) + "\n"
    write_target_text(Path(args.workspace_dir) if args.workspace_dir else None, args.out, payload_text)

    print(
        json.dumps(
            {
                "ok": True,
                "engine": "dalton_researchdata",
                "fallback_reason": None,
                **stats,
                "companies": len(out),
                "with_hits": sum(1 for item in out if item["picks"]),
                "warnings": warnings,
                "out": args.out,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
