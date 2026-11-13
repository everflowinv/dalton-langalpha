#!/usr/bin/env python3
"""Deterministic helper for the Mark new-energy and China-ADR weekly.

collect calls research for AlphaEngine and the internal libraries. The model
uses web_search, feishu_create_doc, and automation_report directly. This script
also computes the window, downloads prices and the cninfo earnings calendar,
assembles evidence, checks the draft, validates a restyle, stores Feishu
receipts, and renders the text summary.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
TARGETS_PATH = SKILL_ROOT / "references" / "targets.json"
STYLE_PATH = SKILL_ROOT / "references" / "restyle_style.md"

STATE_ROOT = "state/mark-weekly-news"
TZ_NAME = "America/New_York"
PROFILE = "mark-weekly-newenergy"
MIN_DRAFT_CHARS = 6000
MAX_CHUNK = 6500
SUMMARY_CHAR_LIMIT = 1500
DOC_NAMES = {"original": "doc-original.json", "restyled": "doc-restyled.json"}

SELL = ["sell_side_report", "sell_side_comment", "meeting_minutes"]
NEWS = ["news", "announcement"]
REQUIRED_H2 = ["本周结论", "行业动态", "新能源公司", "中概股公司", "业绩日历", "涨跌幅", "未核实"]
STYLE_PHRASES = ["不代表", "并非", "而不是", "不能", "不构成", "不应"]
VERBATIM_H2 = ("六、近一周涨跌幅",)
INDUSTRY_SECTIONS = [
    "中国锂电与储能",
    "海外锂电与储能",
    "中国新能源车",
    "海外新能源车",
    "燃油车与汽车出口",
    "AI 电力链",
]
MAIL_SOURCES = ("sales-notes", "tmtb", "funda-ai")
# Recall is case-insensitive. These words only add noise to the query.
STOP = {
    "of", "the", "and", "inc", "inc.", "co", "co.", "corp", "corp.", "ltd", "ltd.", "limited",
    "group", "holdings", "company", "international", "technologies", "technology", "plc",
}
HEADER = re.compile(r"^(From|Subject|Date|AlphaEngine doc_id):.*$", re.MULTILINE)
SEARCH_LIMIT = 100
EXCERPT_PER_DOC = 1500
WIKI_LIST_LIMIT = 100
PER_COMPANY = 5
READ_PAGES = 30
READ_CHARS = 30000
AE_WORKERS = 4
ATTEMPT_LIMIT = 2
# Sales notes and TMTB fill the quota first, then FUNDA AI, then wiki.
SOURCE_TIER = {"sales-notes": 0, "tmtb": 0, "funda-ai": 1, "company-wiki": 2}
WIKI_LIST_KEY = "company-wiki|list"
WIKI_FULL_WARNING = "company-wiki 窗口内新入库超过 100 篇，只看了最新 100 篇"
INTERNAL_NOT_DONE = "内部库尚未完成检索（预算用尽或重试已满）"
RESTYLE_BLOCK_REASON = "整体校验未过，不要发布"
SUMMARY_TOO_LONG = "摘要超过 1500 字，先压缩再提交"
NO_QUARTERLY_NOTE = "只发中期和年度业绩"
FEISHU_DOC_BASE = "https://feishu.cn"
_FEISHU_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
SIGNED_NUM = re.compile(r"[+\-\u2212\uff0d]?\d+(?:\.\d+)?%?")
DATE_MARK = re.compile(r"\[\d{1,2}/\d{1,2}\]")
_TICKER_SPLIT = re.compile(r"[/\s()（）]+")
LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
DOC_ID = re.compile(r"\d{10,}(?:_inst_\d+)?|mail:\d+")
BULLET = re.compile(r"\s*(- |\d+\. |\*\*)")
BEGIN_END = re.compile(r"===BEGIN===\s*\n(.*?)\n?===END===", re.DOTALL)
UNCERTAIN_SRC = re.compile(r"待|未回读|未核|尚未|单一来源|线索|仅检索")
UNCERTAIN_OUT = re.compile(
    r"待|还没|还要|还需|尚|未|没有|没拿到|没找到|不能|不算|线索|暂|单一来源|仅检索|等|要看|要等|核对|核实"
)
STRONGER = ["主因", "主要原因", "明显", "大幅", "显著", "已确认", "确认了", "必然", "一定", "肯定", "全面", "一致预期", "此前预期"]
SAME_AS = {"明显": ["显著"], "显著": ["明显"], "已确认": ["确认"], "确认了": ["确认"], "大幅": ["显著", "明显"]}
CNINFO_URL = "http://www.cninfo.com.cn/new/information/getPrbookInfo"


def _load_script(filename: str, module_name: str):
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    path = HERE / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{filename} is missing")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _card_io():
    return _load_script("card_io.py", "card_io_dalton_mark_weekly_newenergy")


def _rd():
    return _load_script("rd_transport.py", "rd_transport_dalton_mark_weekly_newenergy")


def workspace_file(workspace: Path, relative: str) -> Path:
    return _card_io().workspace_file(workspace, relative)


def write_bytes_exclusive(path: Path, payload: bytes) -> None:
    _card_io().write_bytes_exclusive(path, payload)


def require_workspace(value: str | None) -> Path:
    if not value:
        raise ValueError("missing --workspace-dir")
    path = Path(value)
    if path.is_symlink():
        raise ValueError("path must stay inside the workspace")
    if not path.is_dir():
        raise ValueError("workspace not found")
    return path


def _public_error(exc: BaseException) -> str:
    text = str(exc)
    if "/Users/" in text or "/private/" in text or "/home/" in text:
        return exc.__class__.__name__
    return text


def run_rel(end: dt.date, *parts: str) -> str:
    head = f"{STATE_ROOT}/runs/{end.isoformat()}"
    if not parts:
        return head
    return "/".join((head, *parts))


def write_text(workspace: Path, relative: str, text: str) -> None:
    path = workspace_file(workspace, relative)
    write_bytes_exclusive(path, text.encode("utf-8"))


def write_json(workspace: Path, relative: str, payload: Any) -> None:
    write_text(workspace, relative, json.dumps(payload, ensure_ascii=False, indent=1) + "\n")


def read_text(workspace: Path, relative: str) -> str:
    path = workspace_file(workspace, relative)
    if not path.is_file():
        raise ValueError("file not found")
    return path.read_text(encoding="utf-8")


def read_json(workspace: Path, relative: str) -> Any:
    return json.loads(read_text(workspace, relative))


def load_targets() -> dict[str, Any]:
    return json.loads(TARGETS_PATH.read_text(encoding="utf-8"))


def today_et() -> dt.date:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(TZ_NAME)).date()
    except Exception:  # noqa: BLE001 - fall back to the local calendar date
        return dt.date.today()


def now_et_iso() -> str:
    try:
        from zoneinfo import ZoneInfo

        return dt.datetime.now(ZoneInfo(TZ_NAME)).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001 - clock fallback stays on the local clock
        return dt.datetime.now().isoformat(timespec="seconds")


def parse_end(value: str | None) -> dt.date:
    if not value:
        return today_et()
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("end must be YYYY-MM-DD") from exc


def make_window(end: dt.date) -> dict[str, str]:
    start = end - dt.timedelta(days=7)
    return {
        "end": end.isoformat(),
        "start": start.isoformat(),
        "date_from": start.isoformat(),
        "date_to": (end + dt.timedelta(days=1)).isoformat(),
        "web_date_after": start.isoformat(),
        "web_date_before": (end + dt.timedelta(days=1)).isoformat(),
        "calendar_until": (end + dt.timedelta(days=15)).isoformat(),
    }


def report_run_id(end: dt.date) -> str:
    return f"{PROFILE}-{end.isoformat()}"


def title_for(end: dt.date, *, restyled: bool = False) -> str:
    window = make_window(end)
    start = dt.date.fromisoformat(window["start"])
    if restyled:
        return f"新能源与中概股周度动态（{start.month}/{start.day}至{end.month}/{end.day}，改写版）"
    return f"新能源与中概股周度动态（{start.month}/{start.day}至{end.month}/{end.day}）"


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def monotonic_now() -> float:
    """Clock for collect deadlines. Tests replace this with a stand-in."""
    return time.monotonic()


def _web_query(company: dict[str, Any]) -> str:
    """English ae_q when that query has no Han characters, otherwise the Chinese name."""
    query = str(company.get("ae_q") or "")
    if query and not _has_cjk(query):
        return query
    return str(company.get("name") or query)


def feishu_doc_url(record: dict[str, Any] | None) -> str | None:
    """Build a tenant link from document_id or node_token. A valid token is enough."""
    if not isinstance(record, dict):
        return None
    node = record.get("node_token")
    if isinstance(node, str) and _FEISHU_TOKEN_RE.fullmatch(node):
        return f"{FEISHU_DOC_BASE}/wiki/{node}"
    document_id = record.get("document_id")
    if isinstance(document_id, str) and _FEISHU_TOKEN_RE.fullmatch(document_id):
        return f"{FEISHU_DOC_BASE}/docx/{document_id}"
    return None


def _read_history(workspace: Path) -> tuple[dict[str, Any] | None, bool]:
    """Return (data, writable). A corrupt file is not writable and is not replaced."""
    relative = f"{STATE_ROOT}/history.json"
    path = workspace_file(workspace, relative)
    if not path.exists():
        return {"runs": []}, True
    if path.is_symlink():
        raise ValueError("refusing symlink")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, False
    if not isinstance(data, dict) or not isinstance(data.get("runs", []), list):
        return None, False
    return data, True


def _write_history(workspace: Path, data: dict[str, Any]) -> bool:
    existing, writable = _read_history(workspace)
    if not writable:
        return False
    if existing is None:
        return False
    write_json(workspace, f"{STATE_ROOT}/history.json", data)
    return True


def _draft_stems(workspace: Path, end: dt.date) -> list[str]:
    relative = f"{STATE_ROOT}/drafts/keep.md"
    try:
        marker = workspace_file(workspace, relative)
    except ValueError:
        return []
    directory = marker.parent
    if directory.is_symlink() or not directory.is_dir():
        return []
    found: list[str] = []
    for child in directory.iterdir():
        name = child.name
        if name.endswith(".restyled.md") or not name.endswith(".md"):
            continue
        if child.is_symlink():
            continue
        stem = name[: -len(".md")]
        try:
            dt.date.fromisoformat(stem)
        except ValueError:
            continue
        if stem < end.isoformat():
            found.append(stem)
    return sorted(found)


def previous_run(workspace: Path, end: dt.date) -> dict[str, Any] | None:
    """Latest history run before this Saturday, else the latest archived draft."""
    data, writable = _read_history(workspace)
    runs: list[dict[str, Any]] = []
    if writable and isinstance(data, dict):
        for item in data.get("runs", []):
            if isinstance(item, dict) and isinstance(item.get("end"), str) and item["end"] < end.isoformat():
                runs.append(item)
    if runs:
        runs.sort(key=lambda item: str(item.get("end")))
        return runs[-1]
    stems = _draft_stems(workspace, end)
    if not stems:
        return None
    stem = stems[-1]
    return {"end": stem, "draft": f"{STATE_ROOT}/drafts/{stem}.md"}


def _load_doc(workspace: Path, end: dt.date, kind: str) -> dict[str, Any] | None:
    relative = run_rel(end, DOC_NAMES[kind])
    path = workspace_file(workspace, relative)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict):
        return None
    return record


def split_sections(md: str) -> list[tuple[str, str]]:
    """Return [(h3 title, body)]. An h2 ends the current h3 section."""
    secs: list[tuple[str, str]] = []
    cur: str | None = None
    buf: list[str] = []
    for line in md.splitlines():
        if line.startswith(("## ", "### ")):
            if cur is not None:
                secs.append((cur, "\n".join(buf)))
            cur = line[4:].strip() if line.startswith("### ") else None
            buf = []
            continue
        if cur is not None:
            buf.append(line)
    if cur is not None:
        secs.append((cur, "\n".join(buf)))
    return secs


def draft_rel(end: dt.date, which: str) -> str:
    if which == "restyled":
        return run_rel(end, "restyle", "draft.md")
    return run_rel(end, "draft.md")


def _draft_file(workspace: Path, end: dt.date, which: str) -> tuple[str, Path] | None:
    candidates = [draft_rel(end, which)]
    if which == "restyled":
        candidates.append(f"{STATE_ROOT}/drafts/{end.isoformat()}.restyled.md")
    else:
        candidates.append(f"{STATE_ROOT}/drafts/{end.isoformat()}.md")
    for relative in candidates:
        path = workspace_file(workspace, relative)
        if path.is_file():
            return relative, path
    return None


def _true_h2(line: str) -> bool:
    return line.startswith("## ") and not line.startswith("### ")


def section_body(md: str, marker: str) -> str:
    """Body of the true H2 that contains marker, through the next true H2.

    Outer whitespace is removed. Line breaks inside the section stay as they are.
    """
    lines = md.splitlines()
    start = None
    for index, line in enumerate(lines):
        if _true_h2(line) and marker in line:
            start = index
            break
    if start is None:
        return ""
    end_index = len(lines)
    for index in range(start + 1, len(lines)):
        if _true_h2(lines[index]):
            end_index = index
            break
    return "\n".join(lines[start:end_index]).strip()


def restyle_whole_doc(source: str, final: str) -> dict[str, Any]:
    """Whole-document restyle gate, including the verbatim price section."""
    leftover = re.findall(r"LINK_\d+", final)
    headings_equal = [line for line in source.split("\n") if line.startswith("#")] == [
        line for line in final.split("\n") if line.startswith("#")
    ]
    urls_equal = sorted(re.findall(r"https?://[^)\s]+", source)) == sorted(re.findall(r"https?://[^)\s]+", final))
    prices_equal = section_body(source, VERBATIM_H2[0]) == section_body(final, VERBATIM_H2[0])
    ok = not leftover and headings_equal and urls_equal and prices_equal
    return {
        "ok": ok,
        "leftover_placeholders": leftover,
        "headings_equal": headings_equal,
        "urls_equal": urls_equal,
        "prices_equal": prices_equal,
        "reason": None if ok else RESTYLE_BLOCK_REASON,
    }


def restyle_segment_gate(source: str, final: str) -> dict[str, Any]:
    """Re-check each heading block after a hand edit. A different block count fails the draft."""
    src_units = units(source)
    out_units = units(final)
    if len(src_units) != len(out_units):
        return {"ok": False, "reason": RESTYLE_BLOCK_REASON}
    for src, out in zip(src_units, out_units):
        if src.get("verbatim") or out.get("verbatim"):
            if str(src.get("text") or "").strip() != str(out.get("text") or "").strip():
                return {"ok": False, "reason": RESTYLE_BLOCK_REASON}
            continue
        if validate_chunk(str(src.get("text") or ""), str(out.get("text") or "")):
            return {"ok": False, "reason": RESTYLE_BLOCK_REASON}
    return {"ok": True, "reason": None}


def run_check(workspace: Path, end: dt.date, *, which: str = "original") -> dict[str, Any]:
    cfg = load_targets()
    located = _draft_file(workspace, end, which)
    relative = located[0] if located else draft_rel(end, which)
    result: dict[str, Any] = {"ok": False, "draft": relative, "chars": 0, "which": which}
    if located is None:
        result["error"] = "draft.md 不存在"
        return result
    md = located[1].read_text(encoding="utf-8")
    result["chars"] = len(md)
    h2 = [line for line in md.splitlines() if _true_h2(line)]
    result["missing_h2"] = [key for key in REQUIRED_H2 if not any(key in line for line in h2)]
    found: list[str] = []
    missing: list[str] = []
    weak: list[str] = []
    sections = split_sections(md)
    for company in cfg["companies"]:
        aliases = [str(company["name"])] + [str(item) for item in list(company.get("kw") or [])[:2]]
        needles = [item.replace(" ", "") for item in aliases if item.replace(" ", "")]
        hit = [
            (title, body)
            for title, body in sections
            if any(needle and needle in title.replace(" ", "") for needle in needles)
        ]
        if not hit:
            missing.append(company["name"])
            continue
        found.append(company["name"])
        body = hit[0][1]
        dated = re.search(r"\[\d{1,2}/\d{1,2}", body) is not None or "NA" in body
        if "判断" not in body or not dated:
            weak.append(company["name"])
    total = len(cfg["companies"])
    result["companies_found"] = f"{len(found)}/{total}"
    result["missing_companies"] = missing
    result["sections_without_judgement_or_events"] = weak
    result["dollar_signs"] = md.count("$")
    result["tildes"] = md.count("~")
    result["style_warnings"] = {phrase: md.count(phrase) for phrase in STYLE_PHRASES if md.count(phrase)}
    result["ok"] = (
        not result["missing_h2"]
        and not missing
        and not weak
        and result["dollar_signs"] == 0
        and result["tildes"] == 0
        and len(md) >= MIN_DRAFT_CHARS
    )
    if which == "restyled":
        try:
            source = _source_draft(workspace, end)
        except ValueError:
            source = ""
        gate = restyle_whole_doc(source, md)
        result["leftover_placeholders"] = gate["leftover_placeholders"]
        result["headings_equal"] = gate["headings_equal"]
        result["urls_equal"] = gate["urls_equal"]
        result["prices_equal"] = gate["prices_equal"]
        if not gate["ok"]:
            result["ok"] = False
            result["reason"] = gate["reason"]
        segment_gate = restyle_segment_gate(source, md)
        if not segment_gate["ok"]:
            result["ok"] = False
            result["reason"] = segment_gate["reason"]
    if not result["ok"] and "hint" not in result:
        result["hint"] = "补齐缺失小节、删掉 $ 与 ~（金额写美元，区间写“至”）后重跑 check"
    return result


def _ae_call(query: str, start: str, end: str, limit: int, categories: list[str] | None) -> dict[str, Any]:
    params: dict[str, Any] = {"sort": "relevance"}
    if categories is not None:
        params["document_categories"] = list(categories)
    return {
        "route": "alphaengine",
        "operation": "search_library",
        "query": query,
        "start": start,
        "end": end,
        "limit": limit,
        "params": params,
    }


def build_plan(end: dt.date) -> dict[str, Any]:
    cfg = load_targets()
    window = make_window(end)
    alphaengine: list[dict[str, Any]] = []
    for company in cfg["companies"]:
        for kind, categories in (("sell", SELL), ("news", NEWS)):
            alphaengine.append(
                {
                    "key": f"{company['name']}|{kind}",
                    "company": company["name"],
                    "kind": kind,
                    "call": _ae_call(company["ae_q"], window["date_from"], window["date_to"], 8, categories),
                }
            )
    for item in cfg["industry_queries"]:
        alphaengine.append(
            {
                "key": f"industry|{item['topic']}|{item['q']}",
                "topic": item["topic"],
                "kind": "industry",
                "call": _ae_call(item["q"], window["date_from"], window["date_to"], 10, None),
            }
        )
    web_search: list[dict[str, Any]] = []
    for company in cfg["companies"]:
        query = _web_query(company)
        web_search.append(
            {
                "key": f"web|{company['name']}",
                "company": company["name"],
                "tool": "web_search",
                "params": {
                    "query": query,
                    "start_date": window["web_date_after"],
                    "end_date": window["web_date_before"],
                },
            }
        )
    for item in cfg["industry_queries"]:
        web_search.append(
            {
                "key": f"web|industry|{item['topic']}|{item['q']}",
                "topic": item["topic"],
                "tool": "web_search",
                "params": {
                    "query": item["q"],
                    "start_date": window["web_date_after"],
                    "end_date": window["web_date_before"],
                },
            }
        )
    internal_companies = [
        {"company": company["name"], "key": f"{company['name']}|internal"} for company in cfg["companies"]
    ]
    return {
        "ok": True,
        "saturday": end.isoformat(),
        "is_saturday": end.weekday() == 5,
        "timezone": TZ_NAME,
        "report_run_id": report_run_id(end),
        "title": title_for(end),
        "window": window,
        "companies": len(cfg["companies"]),
        "ae_queries": len(alphaengine),
        "industry_sections": list(INDUSTRY_SECTIONS),
        "alphaengine": alphaengine,
        "internal": {
            "handled_by": "collect",
            "wiki_list": {
                "route": "company-wiki",
                "operation": "list",
                "limit": WIKI_LIST_LIMIT,
                "params": {"indexed_since": window["date_from"], "media": "text"},
            },
            "mail_sources": list(MAIL_SOURCES),
            "search_limit": SEARCH_LIMIT,
            "per_company": PER_COMPANY,
            "read": {
                "route": "all",
                "operation": "read",
                "params": {"offset": 0, "max_chars": READ_CHARS},
            },
            "companies": internal_companies,
        },
        "web_search": web_search,
        "get_document": {
            "max": 6,
            "note": "全文在 ExecuteCode 里调用 research。doc_id 用检索命中的编号。下面的编号是桥的样例。",
            "call": {
                "route": "alphaengine",
                "operation": "get_document",
                "params": {
                    "doc_id": "290001790954279_inst_134249",
                    "content_field": "original_text",
                    "offset": 0,
                    "max_chars": 30000,
                },
            },
        },
        "raw_path": run_rel(end, "raw.json"),
        "after_prices": (
            "prices 和 earnings 之后先 assemble。"
            "然后在对话里按 web_search 清单调用 web_search。"
            "big_moves_ge_8pct 里的公司，以及未来 7 天有业绩日期的公司，再各做一次 web_search。"
            "参数仍只有 query、start_date、end_date。网页结果留在对话里使用，不要写入 raw.json。"
        ),
    }


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    import pandas as pd

    return pd.Timestamp(value).date()


def _slice_until(series: Any, day: dt.date) -> Any:
    import pandas as pd

    index = series.index
    cutoff = pd.Timestamp(day)
    tz = getattr(index, "tz", None)
    if tz is not None:
        cutoff = cutoff.tz_localize(tz)
    return series[index <= cutoff]


def _close_series(frame: Any, symbol: str) -> Any:
    import pandas as pd

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("download did not return a table")
    columns = frame.columns
    if isinstance(columns, pd.MultiIndex):
        level0 = columns.get_level_values(0)
        if symbol in level0:
            picked = frame[symbol]
            if isinstance(picked, pd.DataFrame) and "Close" in picked.columns:
                return picked["Close"].dropna()
            if isinstance(picked, pd.Series):
                return picked.dropna()
        if "Close" in level0:
            picked = frame["Close"]
            if isinstance(picked, pd.DataFrame) and symbol in picked.columns:
                return picked[symbol].dropna()
            if isinstance(picked, pd.Series):
                return picked.dropna()
    if symbol in columns:
        return frame[symbol].dropna()
    raise KeyError(symbol)


def _price_rows_from_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("rows"), dict):
        return payload["rows"]
    if any(isinstance(value, dict) and ("chg_1w" in value or "error" in value) for value in payload.values()):
        return payload
    return None


def big_moves_from_rows(rows: dict[str, Any]) -> list[list[Any]]:
    cfg = load_targets()
    moves: list[list[Any]] = []
    for company in cfg["companies"]:
        record = rows.get(company["yf"]) or {}
        change = record.get("chg_1w") if isinstance(record, dict) else None
        if isinstance(change, (int, float)) and not isinstance(change, bool) and abs(float(change)) >= 8:
            moves.append([company["name"], float(change)])
    moves.sort(key=lambda item: -abs(float(item[1])))
    return moves


def fetch_prices(workspace: Path, end: dt.date) -> dict[str, Any]:
    import yfinance as yf

    cfg = load_targets()
    symbols = [company["yf"] for company in cfg["companies"]] + list(cfg["indices"].keys())
    try:
        frame = yf.download(
            symbols,
            start=(end - dt.timedelta(days=20)).isoformat(),
            end=(end + dt.timedelta(days=1)).isoformat(),
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
    except Exception as exc:  # noqa: BLE001 - a download failure still writes an all-missing table
        name = exc.__class__.__name__
        rows = {symbol: {"error": name} for symbol in symbols}
        write_json(workspace, run_rel(end, "prices.json"), {"error": name, "rows": rows})
        return rows
    base_day = end - dt.timedelta(days=7)
    rows: dict[str, Any] = {}
    for symbol in symbols:
        try:
            series = _close_series(frame, symbol)
            base = _slice_until(series, base_day)
            last = _slice_until(series, end)
            if len(base) and len(last):
                base_px = float(base.iloc[-1])
                last_px = float(last.iloc[-1])
                if base_px == 0:
                    rows[symbol] = {"error": "no data"}
                else:
                    rows[symbol] = {
                        "base_date": _as_date(base.index[-1]).isoformat(),
                        "base": base_px,
                        "end_date": _as_date(last.index[-1]).isoformat(),
                        "close": last_px,
                        "chg_1w": round((last_px / base_px - 1) * 100, 1),
                    }
            else:
                rows[symbol] = {"error": "no data"}
        except Exception as exc:  # noqa: BLE001 - one symbol failure stays in the price table
            rows[symbol] = {"error": exc.__class__.__name__}
    write_json(workspace, run_rel(end, "prices.json"), rows)
    return rows


def load_prices(workspace: Path, end: dt.date) -> tuple[dict[str, Any], bool]:
    relative = run_rel(end, "prices.json")
    path = workspace_file(workspace, relative)
    if path.is_file():
        rows = _price_rows_from_payload(json.loads(path.read_text(encoding="utf-8")))
        if rows is not None:
            return rows, True
    return fetch_prices(workspace, end), False


def _price_file_error(workspace: Path, end: dt.date) -> str | None:
    path = workspace_file(workspace, run_rel(end, "prices.json"))
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("rows"), dict) and isinstance(payload.get("error"), str):
        return payload["error"]
    return None


def quarter_ends(end: dt.date, n: int = 2) -> list[dt.date]:
    candidates = [
        dt.date(year, month, day)
        for year in (end.year - 1, end.year)
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31))
        if dt.date(year, month, day) < end
    ]
    return sorted(candidates)[-n:]


def cninfo_prbook(code: str, section: str) -> dict[str, Any] | None:
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode(
        {
            "sectionTime": section,
            "firstTime": "",
            "lastTime": "",
            "market": "szsh",
            "stockCode": code,
            "orderClos": "",
            "isDesc": "",
            "pagesize": "10",
            "pageNum": "1",
        }
    ).encode()
    request = urllib.request.Request(CNINFO_URL, data=data, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read())
    rows = payload.get("prbookinfos") or []
    if not rows:
        return None
    row = rows[0]
    changes = [row.get(key) for key in ("f003d_0102", "f004d_0102", "f005d_0102") if row.get(key)]
    return {"period": section, "first": row.get("f002d_0102"), "changes": changes, "actual": row.get("f006d_0102") or None}


def _calendar_fields(calendar: Any) -> dict[str, Any]:
    if calendar is None:
        return {}
    if isinstance(calendar, dict):
        return calendar
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001 - a non-dict calendar without pandas is ignored
        return {}
    if not isinstance(calendar, pd.DataFrame):
        return {}
    out: dict[str, Any] = {}
    if "Earnings Date" in calendar.columns:
        out["Earnings Date"] = list(calendar["Earnings Date"].dropna())
    elif "Earnings Date" in calendar.index:
        value = calendar.loc["Earnings Date"]
        out["Earnings Date"] = list(value.dropna()) if hasattr(value, "dropna") else [value]
    if "Earnings Average" in calendar.columns:
        values = calendar["Earnings Average"].dropna()
        if len(values):
            out["Earnings Average"] = values.iloc[0]
    elif "Earnings Average" in calendar.index:
        out["Earnings Average"] = calendar.loc["Earnings Average"]
        if hasattr(out["Earnings Average"], "iloc"):
            out["Earnings Average"] = out["Earnings Average"].iloc[0]
    return out


def _iso_dates(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (dt.date, dt.datetime)):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        items = [value]
    dates: list[str] = []
    for item in items:
        if isinstance(item, dt.datetime):
            dates.append(item.date().isoformat())
        elif isinstance(item, dt.date):
            dates.append(item.isoformat())
        elif item is not None and str(item).strip():
            dates.append(str(item)[:10])
    return dates


def fetch_earnings(workspace: Path, end: dt.date) -> dict[str, Any]:
    import yfinance as yf

    cfg = load_targets()
    periods = [day.isoformat() for day in quarter_ends(end)]
    result: dict[str, Any] = {}
    for company in cfg["companies"]:
        hint: dict[str, Any] = {"name": company["name"], "no_quarterly": bool(company.get("no_quarterly"))}
        if company.get("a_code"):
            records = []
            for period in periods:
                try:
                    row = cninfo_prbook(str(company["a_code"]), period)
                    if row:
                        records.append(row)
                except Exception as exc:  # noqa: BLE001 - one filing-date lookup failure is recorded and skipped
                    records.append({"period": period, "error": exc.__class__.__name__})
            hint["cninfo"] = records
        try:
            calendar = _calendar_fields(yf.Ticker(company["yf"]).calendar or {})
            dates = _iso_dates(calendar.get("Earnings Date"))
            if dates:
                hint["yf_date"] = dates
            average = calendar.get("Earnings Average")
            if average is not None and not (isinstance(average, float) and math.isnan(average)):
                try:
                    hint["yf_eps_avg"] = float(average)
                except (TypeError, ValueError):
                    hint["yf_eps_avg"] = str(average)
        except Exception as exc:  # noqa: BLE001 - one calendar failure is recorded and skipped
            hint["yf_error"] = exc.__class__.__name__
        result[company["name"]] = hint
    write_json(workspace, run_rel(end, "earnings.json"), result)
    return result


def load_earnings(workspace: Path, end: dt.date) -> tuple[dict[str, Any], bool]:
    relative = run_rel(end, "earnings.json")
    path = workspace_file(workspace, relative)
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return payload, True
    return fetch_earnings(workspace, end), False


def fmt_earn(hint: dict[str, Any]) -> str:
    parts: list[str] = []
    for record in hint.get("cninfo") or []:
        if record.get("error"):
            parts.append(f"巨潮{record.get('period')}查询失败")
            continue
        text = f"巨潮预约（报告期{record.get('period')}）首次{record.get('first')}"
        changes = record.get("changes") or []
        if changes:
            text += "，变更为" + "→".join(str(item) for item in changes)
        if record.get("actual"):
            text += f"，实际披露{record.get('actual')}"
        parts.append(text)
    if hint.get("yf_date"):
        text = "yfinance 日历 " + "/".join(str(item) for item in hint["yf_date"])
        if hint.get("yf_eps_avg") is not None:
            text += f"（EPS 一致预期均值 {hint['yf_eps_avg']}）"
        parts.append(text)
    if hint.get("yf_error"):
        parts.append(f"yfinance 日历查询失败：{hint['yf_error']}")
    if hint.get("no_quarterly"):
        parts.append(NO_QUARTERLY_NOTE)
    return "；".join(parts) or "无自动线索"


def kw_match(keywords: list[str], text: str) -> bool:
    lowered = text.lower()
    for keyword in keywords:
        if not keyword or len(keyword) < 2:
            continue
        if keyword.isascii() and keyword.isupper() and len(keyword) <= 5:
            if re.search(r"(?<![A-Za-z])" + re.escape(keyword) + r"(?![A-Za-z])", text):
                return True
        elif keyword.lower() in lowered:
            return True
    return False


def _coerce_hit(item: dict[str, Any]) -> dict[str, Any]:
    sources = item.get("sources")
    if isinstance(sources, list):
        src = ",".join(str(part) for part in sources)
    else:
        src = str(item.get("src") or item.get("type_show_name") or item.get("source") or "")
    snippet = item.get("summary") or item.get("snippet") or item.get("digest") or ""
    return {
        "doc_id": item.get("doc_id") or item.get("document_id"),
        "date": str(item.get("publish_time") or item.get("date") or "")[:10],
        "src": src,
        "title": str(item.get("title") or item.get("title_cn") or "")[:140],
        "snippet": re.sub(r"\s+", " ", str(snippet))[:240],
    }


def _note_pick(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": item.get("document_id") or item.get("doc_id"),
        "date": str(item.get("date") or item.get("publish_time") or "")[:10],
        "source": item.get("source") or item.get("route") or "",
        "subject": item.get("subject") or item.get("title") or "",
        "excerpt": item.get("excerpt") or item.get("snippet") or item.get("summary") or "",
    }


def normalize_ae(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"hits": [], "error": "未执行"}
    if raw.get("ok") is False:
        return {"hits": [], "error": str(raw.get("error") or raw.get("message") or "检索失败")[:200]}
    if isinstance(raw.get("hits"), list):
        hits = [_coerce_hit(item) for item in raw["hits"] if isinstance(item, dict)]
        return {"hits": hits, "error": None}
    items = _rd().find_result_items(raw)
    if items is None:
        if isinstance(raw.get("error"), str) and raw["error"]:
            return {"hits": [], "error": raw["error"][:200]}
        if raw.get("ok") is True:
            return {"hits": [], "error": None}
        return {"hits": [], "error": "未执行"}
    return {"hits": [_coerce_hit(item) for item in items], "error": None}


def normalize_notes(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"matched_docs": 0, "picks": [], "error": "未执行"}
    if raw.get("ok") is False:
        return {"matched_docs": 0, "picks": [], "error": str(raw.get("error") or "检索失败")[:200]}
    if isinstance(raw.get("picks"), list):
        matched = raw.get("matched_docs")
        if matched is None:
            matched = raw.get("result_count", len(raw["picks"]))
        result: dict[str, Any] = {
            "matched_docs": matched,
            "picks": [item for item in raw["picks"] if isinstance(item, dict)][:3],
            "error": None,
        }
        if raw.get("limit_hit"):
            result["limit_hit"] = raw["limit_hit"]
        if raw.get("warnings"):
            result["warnings"] = raw["warnings"]
        if raw.get("errors"):
            result["errors"] = raw["errors"]
        if raw.get("read_errors"):
            result["read_errors"] = raw["read_errors"]
        if raw.get("partial") is True:
            result["partial"] = True
        return result
    items = _rd().find_result_items(raw)
    if items is None:
        return {"matched_docs": 0, "picks": [], "error": None}
    picks = [_note_pick(item) for item in items[:3] if isinstance(item, dict)]
    matched = raw.get("matched_docs")
    if matched is None:
        matched = raw.get("result_count", len(items))
    return {"matched_docs": matched, "picks": picks, "error": None}


def _internal_gap_line(notes: dict[str, Any]) -> str | None:
    """One evidence line when an internal-library company result is incomplete."""
    if notes.get("partial") is not True and not notes.get("errors") and not notes.get("warnings"):
        return None
    parts: list[str] = []
    for item in list(notes.get("errors") or []) + list(notes.get("warnings") or []):
        text = str(item).strip()
        if text and text not in parts:
            parts.append(text)
    detail = "；".join(parts) if parts else "有来源未完成"
    return "内部库检索不完整：" + detail


def _wiki_list_failure_line(raw: dict[str, Any]) -> str | None:
    """Sentence for a missing or failed company-wiki list. A successful list stays quiet."""
    record = raw.get(WIKI_LIST_KEY)
    if isinstance(record, dict) and record.get("ok") is not False:
        return None
    if isinstance(record, dict):
        code = _short_error(record.get("error") or record.get("status"))
    else:
        code = "未执行"
    return f"company-wiki 新入库列表获取失败：{code}，各公司内部库结果缺少维基来源"


def fmt_hits(result: dict[str, Any], keywords: list[str] | None = None, seen: set[str] | None = None, cap: int = 8) -> list[str]:
    if result.get("error"):
        return [f"  - 检索失败：{result['error']}"]
    hits = list(result.get("hits") or [])
    if keywords:
        hits = [hit for hit in hits if kw_match(keywords, f"{hit.get('title', '')} {hit.get('snippet', '')}")]
    if seen is not None:
        kept = []
        for hit in hits:
            key = str(hit.get("title") or "")[:40]
            if key in seen:
                continue
            seen.add(key)
            kept.append(hit)
        hits = kept
    if not hits:
        return ["  - （无直接命中）" if keywords else "  - （无结果）"]
    lines = []
    for hit in hits[:cap]:
        lines.append(
            f"  - {hit.get('doc_id')} | {hit.get('date')} | {hit.get('src') or '-'} | {hit.get('title')} | {str(hit.get('snippet') or '')[:180]}"
        )
    return lines


def _price_line(record: dict[str, Any]) -> str:
    if "chg_1w" in record:
        return (
            f"{record['base_date']} {float(record['base']):.2f} → {record['end_date']} "
            f"{float(record['close']):.2f}，近一周 {float(record['chg_1w']):+.1f}%"
        )
    return f"行情缺失（{record.get('error', 'n/a')}）"


def assemble_evidence(workspace: Path, end: dt.date, raw: dict[str, Any]) -> dict[str, Any]:
    cfg = load_targets()
    window = make_window(end)
    prices, _cached_prices = load_prices(workspace, end)
    earnings, _cached_earnings = load_earnings(workspace, end)
    errors: list[dict[str, Any]] = []
    jobs = build_plan(end)["alphaengine"]
    ae_results: dict[str, dict[str, Any]] = {}
    for job in jobs:
        key = job["key"]
        if key not in raw:
            ae_results[key] = {"hits": [], "error": "未执行"}
            errors.append({"stage": "alphaengine", "key": key, "error": "未执行"})
            continue
        normalized = normalize_ae(raw.get(key))
        ae_results[key] = normalized
        if normalized.get("error"):
            errors.append({"stage": "alphaengine", "key": key, "error": str(normalized["error"])[:200]})
    files: list[str] = []
    wiki_note = _wiki_list_failure_line(raw)
    if wiki_note:
        errors.append({"stage": "internal", "key": WIKI_LIST_KEY, "error": wiki_note})
    for group in cfg["groups"]:
        gid = group["id"]
        lines = [f"# {group['sector']}｜{group['label']}（窗口 {window['start']} 至 {window['end']}）", ""]
        if wiki_note:
            lines.append(wiki_note)
            lines.append("")
        for company in [item for item in cfg["companies"] if item["grp"] == gid]:
            record = prices.get(company["yf"]) or {}
            lines.append(f"## {company['name']}（{company['ticker']}）")
            lines.append(f"- 行情（{company['yf']}）：{_price_line(record)}")
            lines.append(f"- 业绩日历线索：{fmt_earn(earnings.get(company['name']) or {})}")
            seen: set[str] = set()
            keywords = [company["name"]] + list(company.get("kw") or [])
            lines.append(f"- AlphaEngine 卖方/纪要（query={company['ae_q']}）：")
            lines.extend(fmt_hits(ae_results.get(f"{company['name']}|sell", {"hits": [], "error": "未执行"}), keywords, seen))
            lines.append(f"- AlphaEngine 新闻/公告（query={company['ae_q']}）：")
            lines.extend(fmt_hits(ae_results.get(f"{company['name']}|news", {"hits": [], "error": "未执行"}), keywords, seen))
            internal_key = f"{company['name']}|internal"
            stored_internal = raw.get(internal_key)
            if not isinstance(stored_internal, dict):
                lines.append(f"- {INTERNAL_NOT_DONE}")
                errors.append({"stage": "internal", "key": company["name"], "error": INTERNAL_NOT_DONE})
                lines.append("")
                continue
            notes = normalize_notes(stored_internal)
            if notes.get("error"):
                lines.append(f"- 内部库检索失败：{notes['error']}")
                errors.append({"stage": "internal", "key": company["name"], "error": str(notes["error"])[:200]})
                lines.append("")
                continue
            picks = notes.get("picks") or []
            incomplete = notes.get("partial") is True or bool(notes.get("errors") or notes.get("warnings"))
            if incomplete and not picks:
                lines.append("- 内部库（sales-notes/TMTB/FUNDA/wiki）：")
            else:
                lines.append(
                    f"- 内部库（sales-notes/TMTB/FUNDA/wiki）命中 {notes.get('matched_docs', 0)} 篇，摘录："
                )
            if picks:
                for pick in picks[:3]:
                    excerpt = re.sub(r"\s+", " ", str(pick.get("excerpt") or ""))[:220]
                    subject = str(pick.get("subject") or pick.get("title") or "")[:100]
                    lines.append(
                        f"  - {pick.get('document_id')} | {pick.get('date')} | {pick.get('source')} | {subject} | {excerpt}"
                    )
            elif not incomplete:
                lines.append("  - （无）")
            if notes.get("limit_hit"):
                lines.append("  - 检索满额：" + "、".join(str(item) for item in notes["limit_hit"]))
            gap = _internal_gap_line(notes)
            if gap:
                lines.append(f"  - {gap}")
                errors.append({"stage": "internal", "key": company["name"], "error": gap[:200]})
            lines.append("")
        relative = run_rel(end, "evidence", f"{gid}.md")
        write_text(workspace, relative, "\n".join(lines))
        files.append(relative)
    industry_lines = [f"# 行业检索（AlphaEngine，{window['date_from']} 至 {window['date_to']}）", ""]
    for item in cfg["industry_queries"]:
        industry_lines.append(f"## {item['topic']}｜query={item['q']}")
        industry_lines.extend(fmt_hits(ae_results.get(f"industry|{item['topic']}|{item['q']}", {"hits": [], "error": "未执行"})))
        industry_lines.append("")
    industry_rel = run_rel(end, "evidence", "industry.md")
    write_text(workspace, industry_rel, "\n".join(industry_lines))
    files.append(industry_rel)
    prev_rel = run_rel(end, "evidence", "prev-earnings.md")
    write_text(workspace, prev_rel, previous_earnings_section(workspace, end) + "\n")
    files.append(prev_rel)
    wiki_raw = raw.get(WIKI_LIST_KEY)
    wiki_warnings = list(wiki_raw.get("warnings") or []) if isinstance(wiki_raw, dict) else []
    table = ["| 公司 | 代码 | 收盘日 | 收盘价 | 近一周涨跌 |", "|---|---|---|---|---|"]
    for company in cfg["companies"]:
        record = prices.get(company["yf"]) or {}
        if "chg_1w" in record:
            table.append(
                f"| {company['name']} | {company['yf']} | {record['end_date']} | {float(record['close']):.2f} | {float(record['chg_1w']):+.1f}% |"
            )
        else:
            table.append(f"| {company['name']} | {company['yf']} | 缺失 | 缺失 | 缺失 |")
    for symbol, name in cfg["indices"].items():
        record = prices.get(symbol) or {}
        if "chg_1w" in record:
            table.append(
                f"| {name} | {symbol} | {record['end_date']} | {float(record['close']):.2f} | {float(record['chg_1w']):+.1f}% |"
            )
    prices_rel = run_rel(end, "evidence", "prices.md")
    write_text(workspace, prices_rel, "\n".join(table))
    files.append(prices_rel)
    ae_failed = sum(1 for item in errors if item.get("stage") == "alphaengine")
    hint = None
    if jobs and ae_failed > len(jobs) // 3:
        codes = []
        for item in errors:
            if item.get("stage") == "alphaengine" and item.get("error") not in codes:
                codes.append(str(item["error"])[:80])
        shown = "、".join(codes[:3]) if codes else "未执行"
        hint = f"AlphaEngine 失败过多。改用 web_search，并在未核实写明 AlphaEngine 不可用：{shown}"
    ordered_errors = [item for item in errors if item.get("stage") == "internal"] + [
        item for item in errors if item.get("stage") != "internal"
    ]
    manifest = {
        "ok": True,
        "work": run_rel(end),
        "window": window,
        "web_date_after": window["web_date_after"],
        "web_date_before": window["web_date_before"],
        "calendar_until": window["calendar_until"],
        "draft": run_rel(end, "draft.md"),
        "evidence_files": files,
        "companies": len(cfg["companies"]),
        "ae_queries": len(jobs),
        "ae_failed": ae_failed,
        "big_moves_ge_8pct": big_moves_from_rows(prices),
        "prev_run": previous_run(workspace, end),
        "errors": ordered_errors,
        "errors_summary": ordered_errors[:30],
        "warnings": wiki_warnings,
        "hint": hint,
        "report_run_id": report_run_id(end),
        "saturday": end.isoformat(),
    }
    write_json(workspace, run_rel(end, "manifest.json"), manifest)
    return manifest


def previous_earnings_section(workspace: Path, end: dt.date) -> str:
    """上一期稿的「五、业绩日历」，作为本周业绩日历的底稿。"""
    prev = previous_run(workspace, end)
    if not isinstance(prev, dict):
        return "上周稿没有「五、业绩日历」。"
    candidates: list[str] = []
    draft = prev.get("draft")
    if isinstance(draft, str) and draft:
        candidates.append(draft)
    prev_end = prev.get("end")
    if isinstance(prev_end, str) and prev_end:
        candidates.append(f"{STATE_ROOT}/drafts/{prev_end}.md")
        try:
            candidates.append(run_rel(dt.date.fromisoformat(prev_end), "draft.md"))
        except ValueError:
            pass
    seen: set[str] = set()
    for relative in candidates:
        if relative in seen:
            continue
        seen.add(relative)
        try:
            body = section_body(read_text(workspace, relative), "五、业绩日历")
        except ValueError:
            continue
        if body:
            return body
    return "上周稿没有「五、业绩日历」。"


def _failure_message(workspace: Path, end: dt.date) -> str | None:
    relative = run_rel(end, "failure.json")
    path = workspace_file(workspace, relative)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])
    return None


def original_block_reason(workspace: Path, end: dt.date) -> str | None:
    """Why restyle must not start. A recorded success overrides an older failure note."""
    check = run_check(workspace, end, which="original")
    if not check.get("ok"):
        if check.get("error") == "draft.md 不存在":
            return "原版草稿还没有写好"
        return "原版校验还没有通过"
    state, record = _doc_state(workspace, end, "original")
    if state == "unreadable":
        return "doc_record_unreadable"
    if record is None:
        return _failure_message(workspace, end) or "原版飞书文档还没有记录"
    if record.get("ok") is True and feishu_doc_url(record):
        return None
    return _failure_message(workspace, end) or str(record.get("error") or "原版飞书文档还没有记录")


def units(md: str) -> list[dict[str, Any]]:
    lines = md.split("\n")
    result: list[dict[str, Any]] = []
    cur: list[str] = []
    cur_h2 = ""

    def flush() -> None:
        if cur:
            text = "\n".join(cur).strip("\n")
            result.append({"text": text, "verbatim": any(key in cur_h2 for key in VERBATIM_H2)})

    for line in lines:
        if line.startswith(("## ", "### ")):
            flush()
            cur = []
            if line.startswith("## "):
                cur_h2 = line
        cur.append(line)
    flush()
    return [item for item in result if str(item["text"]).strip()]


def chunks_of(md: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    buf: list[str] = []

    def push() -> None:
        if buf:
            chunks.append({"text": "\n\n".join(buf), "verbatim": False})

    for unit in units(md):
        if unit["verbatim"]:
            push()
            buf = []
            chunks.append(unit)
            continue
        if buf and sum(len(part) for part in buf) + len(unit["text"]) > MAX_CHUNK:
            push()
            buf = []
        buf.append(unit["text"])
    push()
    return chunks


def placeholder_links(md: str) -> tuple[str, dict[str, str]]:
    links: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        key = f"LINK_{len(links) + 1}"
        links[key] = match.group(1)
        return f"]({key})"

    return LINK.sub(replace, md), links


def bullets(text: str) -> list[str]:
    return [line for line in text.split("\n") if BULLET.match(line)]


def _signed_numbers(text: str) -> list[str]:
    """Digit tokens with their sign. Fullwidth minus counts as ASCII hyphen."""
    found = SIGNED_NUM.findall(text)
    return [item.replace("\u2212", "-").replace("\uff0d", "-") for item in found]


def _appearance_tokens() -> list[str]:
    seen: set[str] = set()
    tokens: list[str] = []
    for company in load_targets()["companies"]:
        for raw in [company.get("name"), *(company.get("kw") or [])]:
            text = str(raw or "").strip()
            if len(text) < 2 or text in seen:
                continue
            seen.add(text)
            tokens.append(text)
    return tokens


def _token_present(text: str, token: str) -> bool:
    if _has_cjk(token):
        return token in text
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", text) is not None


def _mentioned_names(text: str) -> set[str]:
    return {token for token in _appearance_tokens() if _token_present(text, token)}


def _date_marks(text: str) -> set[str]:
    return set(DATE_MARK.findall(text))


def validate_chunk(src: str, outp: str) -> list[str]:
    errs: list[str] = []
    src_headings = [line.strip() for line in src.split("\n") if line.startswith("#")]
    out_headings = [line.strip() for line in outp.split("\n") if line.startswith("#")]
    if src_headings != out_headings:
        errs.append("标题行被改动或缺失")
    if len(bullets(src)) != len(bullets(outp)):
        errs.append(f"条目数不一致：原文 {len(bullets(src))}，改写 {len(bullets(outp))}")
    src_nums = Counter(_signed_numbers(src))
    out_nums = Counter(_signed_numbers(outp))
    missing = dict(src_nums - out_nums)
    added = dict(out_nums - src_nums)
    if missing:
        errs.append(f"缺少数字：{list(missing)[:12]}")
    new_nums = [number for number in added if number not in src_nums]
    if new_nums:
        errs.append(f"出现原文没有的数字：{new_nums[:12]}")
    if _mentioned_names(src) != _mentioned_names(outp):
        errs.append("公司名出现集合变化")
    if _date_marks(src) != _date_marks(outp):
        errs.append("日期标记集合变化")
    if Counter(DOC_ID.findall(src)) != Counter(DOC_ID.findall(outp)):
        errs.append("doc 编号不一致")
    if sorted(re.findall(r"LINK_\d+", src)) != sorted(re.findall(r"LINK_\d+", outp)):
        errs.append("链接占位符不一致")
    if Counter(re.findall(r"【[^】]+】", src)) != Counter(re.findall(r"【[^】]+】", outp)):
        errs.append("【】来源标签不一致")
    if "$" in outp or "~" in outp:
        errs.append("出现了 $ 或 ~")
    if len(outp) < len(src) * 0.75:
        errs.append("改写稿明显变短，可能漏了内容")
    return errs


def flags_for(src: str, outp: str) -> list[dict[str, Any]]:
    result = []
    for src_line, out_line in zip(bullets(src), bullets(outp)):
        why = []
        if len(UNCERTAIN_SRC.findall(src_line)) and len(UNCERTAIN_OUT.findall(out_line)) < len(UNCERTAIN_SRC.findall(src_line)):
            why.append("可信度标记变少")
        added = [
            word
            for word in STRONGER
            if word in out_line and word not in src_line and not any(alias in src_line for alias in SAME_AS.get(word, []))
        ]
        if added:
            why.append("新增了加强语气的词：" + "、".join(added))
        repeated = [number for number in (Counter(_signed_numbers(out_line)) - Counter(_signed_numbers(src_line)))]
        if repeated:
            why.append("重复写了原文已有的数字：" + "、".join(repeated[:6]))
        if len(src_line) > 40 and not (0.6 <= len(out_line) / len(src_line) <= 1.7):
            why.append(f"长度变化大（{len(src_line)}→{len(out_line)}）")
        if why:
            result.append({"why": why, "src": src_line.strip(), "out": out_line.strip()})
    return result


def extract_rewrite(text: str) -> str:
    match = BEGIN_END.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


def apply_rewritten_chunks(chunks: list[dict[str, Any]], rewritten: dict[int, str], links: dict[str, str]) -> dict[str, Any]:
    parts: list[str] = []
    kept: list[int] = []
    flags: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for chunk in chunks:
        src = str(chunk["text"])
        index = int(chunk["index"])
        if chunk.get("verbatim"):
            parts.append(src)
            report.append({"chunk": index, "verbatim": True, "chars": len(src)})
            continue
        raw = rewritten.get(index)
        if raw is None:
            parts.append(src)
            kept.append(index)
            report.append({"chunk": index, "ok": False, "errors": ["缺少改写"]})
            continue
        outp = extract_rewrite(raw)
        errs = validate_chunk(src, outp)
        if errs:
            parts.append(src)
            kept.append(index)
            report.append({"chunk": index, "ok": False, "errors": errs})
            continue
        parts.append(outp)
        flags.extend(dict(item, chunk=index) for item in flags_for(src, outp))
        report.append({"chunk": index, "ok": True, "chars": [len(src), len(outp)]})
    final = "\n\n".join(part.strip() for part in parts)
    if final and not final.endswith("\n"):
        final += "\n"
    for key in sorted(links, key=lambda item: -int(item.split("_")[1])):
        final = final.replace(f"]({key})", f"]({links[key]})")
    leftover = re.findall(r"LINK_\d+", final)
    return {
        "text": final,
        "chunks_kept_original": kept,
        "flags": flags,
        "leftover_placeholders": leftover,
        "report": report,
    }


def _rewritten_map(payload: Any) -> dict[int, str]:
    if isinstance(payload, list):
        return {index: str(item) for index, item in enumerate(payload)}
    if not isinstance(payload, dict):
        raise TypeError("rewritten json is invalid")
    chunks = payload.get("chunks")
    if isinstance(chunks, list):
        mapped: dict[int, str] = {}
        for index, item in enumerate(chunks):
            if isinstance(item, dict):
                mapped[int(item.get("index", index))] = str(item.get("text") or item.get("out") or "")
            else:
                mapped[index] = str(item)
        return mapped
    mapped = {}
    for key, value in payload.items():
        if str(key).isdigit():
            mapped[int(key)] = str(value)
    if mapped:
        return mapped
    raise TypeError("rewritten json is invalid")


def _source_draft(workspace: Path, end: dt.date) -> str:
    relative = run_rel(end, "draft.md")
    path = workspace_file(workspace, relative)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    archived = workspace_file(workspace, f"{STATE_ROOT}/drafts/{end.isoformat()}.md")
    if archived.is_file():
        return archived.read_text(encoding="utf-8")
    raise ValueError("原版草稿还没有写好")


def _upsert_history(workspace: Path, end: dt.date, updates: dict[str, Any], *, restyled: bool) -> bool:
    data, writable = _read_history(workspace)
    if not writable or data is None:
        return False
    runs = [item for item in data.get("runs", []) if isinstance(item, dict)]
    current = next((item for item in runs if item.get("end") == end.isoformat()), None)
    if current is None:
        current = {"end": end.isoformat()}
        runs.append(current)
    original_draft = current.get("draft")
    current.update(updates)
    if restyled and original_draft:
        current["draft"] = original_draft
    data["runs"] = runs
    return _write_history(workspace, data)


def _archive_draft(workspace: Path, end: dt.date, kind: str) -> str | None:
    source_rel = run_rel(end, "restyle", "draft.md") if kind == "restyled" else run_rel(end, "draft.md")
    source = workspace_file(workspace, source_rel)
    if not source.is_file():
        return None
    name = f"{end.isoformat()}.restyled.md" if kind == "restyled" else f"{end.isoformat()}.md"
    relative = f"{STATE_ROOT}/drafts/{name}"
    write_text(workspace, relative, source.read_text(encoding="utf-8"))
    return relative


def _doc_state(workspace: Path, end: dt.date, kind: str) -> tuple[str, dict[str, Any] | None]:
    """Return missing, unreadable, or present plus the parsed record."""
    relative = run_rel(end, DOC_NAMES[kind])
    path = workspace_file(workspace, relative)
    if not path.is_file() or path.is_symlink():
        return "missing", None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "unreadable", None
    if not isinstance(record, dict):
        return "unreadable", None
    return "present", record


def _failed_rel(kind: str) -> str:
    return f"doc-{kind}.failed.json"


def _failed_attempts(workspace: Path, end: dt.date, kind: str) -> int:
    path = workspace_file(workspace, run_rel(end, _failed_rel(kind)))
    if not path.is_file() or path.is_symlink():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    try:
        return int(data.get("attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _record_failed_attempt(workspace: Path, end: dt.date, kind: str, raw: bytes) -> dict[str, Any]:
    relative = run_rel(end, _failed_rel(kind))
    path = workspace_file(workspace, relative)
    attempts = 0
    returns: list[Any] = []
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {
                "ok": False,
                "stored": False,
                "create": False,
                "reason": "doc_record_unreadable",
                "path": relative,
            }
        if isinstance(current, dict):
            try:
                attempts = int(current.get("attempts") or 0)
            except (TypeError, ValueError):
                attempts = 0
            if isinstance(current.get("returns"), list):
                returns = list(current["returns"])
    text = raw.decode("utf-8", "replace")
    attempts += 1
    returns.append(text)
    write_json(workspace, relative, {"attempts": attempts, "returns": returns})
    create = attempts < 2
    payload: dict[str, Any] = {
        "ok": True,
        "stored": True,
        "skipped": False,
        "create": create,
        "path": relative,
        "url": None,
        "attempts": attempts,
        "draft_copied": False,
        "history_updated": False,
    }
    if not create:
        payload["reason"] = "create_failed_twice"
    return payload


def _history_updates(end: dt.date, kind: str, record: dict[str, Any], url: str | None, archived: str | None) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "end": end.isoformat(),
        "start": make_window(end)["start"],
        "published_at": now_et_iso(),
    }
    if kind == "restyled":
        updates["restyled_title"] = title_for(end, restyled=True)
        updates["restyled_url"] = url
        if isinstance(record.get("document_id"), str):
            updates["restyled_document_id"] = record["document_id"]
        if isinstance(record.get("node_token"), str):
            updates["restyled_node_token"] = record["node_token"]
        if archived:
            updates["restyled_draft"] = archived
    else:
        updates["title"] = title_for(end)
        updates["url"] = url
        if isinstance(record.get("document_id"), str):
            updates["document_id"] = record["document_id"]
        if isinstance(record.get("node_token"), str):
            updates["node_token"] = record["node_token"]
        if archived:
            updates["draft"] = archived
    return updates


def save_doc_record(workspace: Path, end: dt.date, kind: str, raw: bytes) -> dict[str, Any]:
    if kind not in DOC_NAMES:
        return {"ok": False, "error": "kind must be original or restyled"}
    relative = run_rel(end, DOC_NAMES[kind])
    dest = workspace_file(workspace, relative)
    if dest.is_file():
        try:
            existing = json.loads(dest.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = None
        return {
            "ok": True,
            "stored": False,
            "skipped": True,
            "create": False,
            "path": relative,
            "url": feishu_doc_url(existing if isinstance(existing, dict) else None),
        }
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"ok": False, "stored": False, "error": "invalid json"}
    if not isinstance(record, dict):
        return {"ok": False, "stored": False, "error": "invalid json"}
    url = feishu_doc_url(record)
    if url is None:
        return _record_failed_attempt(workspace, end, kind, raw)
    # The receipt is stored before archive and history. A later failure does not remove it.
    write_bytes_exclusive(dest, raw)
    draft_copied = False
    history_updated = False
    archived: str | None = None
    archive_error = None
    history_error = None
    if record.get("ok") is True:
        try:
            archived = _archive_draft(workspace, end, kind)
            draft_copied = archived is not None
        except Exception as exc:  # noqa: BLE001 - archive failure must not undo the saved receipt
            archive_error = exc.__class__.__name__
        try:
            updates = _history_updates(end, kind, record, url, archived)
            history_updated = _upsert_history(workspace, end, updates, restyled=kind == "restyled")
            if not history_updated:
                history_error = "history_unreadable"
        except Exception as exc:  # noqa: BLE001 - history failure must not undo the saved receipt
            history_error = exc.__class__.__name__
            history_updated = False
    payload = {
        "ok": True,
        "stored": True,
        "skipped": False,
        "create": False,
        "path": relative,
        "url": url,
        "draft_copied": draft_copied,
        "history_updated": history_updated,
    }
    if archive_error:
        payload["archive_error"] = archive_error
    if history_error:
        payload["history_error"] = history_error
    return payload


def _bullet(line: str) -> str:
    text = str(line).strip()
    if text.startswith("- "):
        return text
    return f"- {text}"


def _earn_sort_key(item: Any) -> tuple[Any, ...]:
    if isinstance(item, dict):
        date = str(item.get("date") or "")
        text = str(item.get("line") or item.get("text") or date)
    else:
        text = str(item)
        date = ""
    iso = re.search(r"(\d{4})-(\d{2})-(\d{2})", date or text)
    if iso:
        return (int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), text)
    month_day = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if month_day:
        return (0, int(month_day.group(1)), int(month_day.group(2)), text)
    return (9, 99, 99, text)


def _earn_line(item: Any) -> str:
    if isinstance(item, dict):
        if item.get("line") or item.get("text"):
            return str(item.get("line") or item.get("text"))
        return " ".join(part for part in (str(item.get("date") or ""), str(item.get("company") or "")) if part).strip()
    return str(item).strip()


def _kept_line(indexes: list[int]) -> str:
    shown = "、".join(str(index + 1) for index in indexes)
    return f"第 {shown} 段改写没通过校验，保留原文"


def _restyle_gate_reason(workspace: Path, end: dt.date) -> str | None:
    located = _draft_file(workspace, end, "restyled")
    if located is None:
        return None
    try:
        source = _source_draft(workspace, end)
    except ValueError:
        source = ""
    final = located[1].read_text(encoding="utf-8")
    gate = restyle_whole_doc(source, final)
    if not gate["ok"]:
        return str(gate["reason"] or RESTYLE_BLOCK_REASON)
    segment = restyle_segment_gate(source, final)
    if not segment["ok"]:
        return str(segment["reason"] or RESTYLE_BLOCK_REASON)
    return None


def render_summary(workspace: Path, end: dt.date, model_input: dict[str, Any]) -> dict[str, Any]:
    check = run_check(workspace, end, which="original")
    manifest_path = workspace_file(workspace, run_rel(end, "manifest.json"))
    manifest: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            manifest = loaded
    original = _load_doc(workspace, end, "original")
    restyled = _load_doc(workspace, end, "restyled")
    original_url = feishu_doc_url(original)
    restyled_url = feishu_doc_url(restyled)
    report_path = workspace_file(workspace, run_rel(end, "restyle", "report.json"))
    kept: list[int] = []
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
        if isinstance(report, dict) and isinstance(report.get("chunks_kept_original"), list):
            kept = [int(index) for index in report["chunks_kept_original"]]
    lines: list[str] = []
    publish_failure = model_input.get("publish_failure")
    if not publish_failure and isinstance(restyled, dict) and restyled.get("ok") is False:
        publish_failure = restyled.get("error") or "改写版发布失败"
    if not publish_failure and isinstance(original, dict) and original.get("ok") is False:
        publish_failure = original.get("error") or "原版发布失败"
    if publish_failure:
        lines.append(f"飞书发布失败：{publish_failure}")
        lines.append(f"草稿：{run_rel(end, 'draft.md')}")
    unfinished = model_input.get("unfinished") or []
    if isinstance(unfinished, list) and unfinished:
        lines.append("本周未查完：" + "、".join(str(name) for name in unfinished))
    if not check.get("ok"):
        missing = check.get("missing_companies") or []
        if missing:
            lines.append("原版未完成：" + "、".join(str(name) for name in missing))
        elif check.get("error"):
            lines.append(f"原版未完成：{check['error']}")
        else:
            lines.append("原版未完成：校验没有通过")
    lines.append(f"**{title_for(end)}**")
    restyled_ok = isinstance(restyled, dict) and restyled.get("ok") is True and bool(restyled_url)
    original_ok = isinstance(original, dict) and original.get("ok") is True and bool(original_url)
    restyled_incomplete = isinstance(restyled, dict) and restyled.get("ok") is False and bool(restyled_url)
    original_incomplete = isinstance(original, dict) and original.get("ok") is False and bool(original_url)
    model_failure = str(model_input.get("restyle_failure") or "").strip()
    gate_reason = _restyle_gate_reason(workspace, end)
    if restyled_ok:
        lines.append(f"改写版（推荐读这份）：<{restyled_url}>")
    else:
        lines.append("改写版（推荐读这份）：未发布")
    if original_ok:
        lines.append(f"原版：<{original_url}>")
    else:
        lines.append("原版：未发布")
    if restyled_incomplete or original_incomplete:
        lines.append("正文未完成")
    if not restyled_ok:
        if restyled_incomplete:
            unpublished = model_failure or gate_reason or "正文未完成"
        elif not check.get("ok"):
            unpublished = "原版未完成"
        else:
            unpublished = model_failure or gate_reason or "还没有改写版文档"
        lines.append(f"改写版未发布：{unpublished}")
    elif gate_reason and not model_failure:
        lines.append(gate_reason)
    if kept:
        lines.append(_kept_line(kept))
    coverage = check.get("companies_found") or "0/35"
    queries = int(manifest.get("ae_queries") or 0)
    failed = int(manifest.get("ae_failed") or 0)
    lines.append(f"已覆盖 {coverage} 家；AlphaEngine 检索 {queries} 次，失败 {failed} 次。")
    lines.append("**本周最重要的几条**")
    highlights = model_input.get("highlights") or []
    if isinstance(highlights, list):
        for item in highlights[:5]:
            text = str(item).strip()
            if text:
                lines.append(_bullet(text))
    lines.append("**下周业绩**")
    upcoming = model_input.get("next_earnings") or []
    if isinstance(upcoming, list):
        ordered = sorted(upcoming, key=_earn_sort_key)
        for item in ordered:
            text = _earn_line(item)
            if text:
                lines.append(_bullet(text))
    lines.append("**未核实**")
    unverified = model_input.get("unverified") or []
    if isinstance(unverified, list):
        for item in unverified[:5]:
            text = str(item).strip()
            if text:
                lines.append(_bullet(text))
    text = "\n".join(lines).strip() + "\n"
    relative = run_rel(end, "summary.md")
    write_text(workspace, relative, text)
    over_limit = len(text) > SUMMARY_CHAR_LIMIT
    payload: dict[str, Any] = {
        "ok": not over_limit,
        "text": text,
        "path": relative,
        "chars": len(text),
        "over_limit": over_limit,
        "report_run_id": report_run_id(end),
        "companies_found": coverage,
        "ae_queries": queries,
        "ae_failed": failed,
    }
    if over_limit:
        payload["reason"] = SUMMARY_TOO_LONG
    return payload


class _BudgetExpired(Exception):
    """The collect deadline passed before this request was sent."""


class _ReadFailed(Exception):
    """A document read returned no text. The empty body is not cached."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _key_done(value: Any) -> bool:
    return isinstance(value, dict) and value.get("ok") is not False and value.get("partial") is not True


def _short_error(value: Any, fallback: str = "检索失败") -> str:
    if value is None or value is False or value == "":
        text = fallback
    elif isinstance(value, str):
        text = value.strip() or fallback
    else:
        text = json.dumps(value, ensure_ascii=False)
    if any(marker in text for marker in ("/Users/", "/private/", "/home/")):
        return fallback
    return text[:200]


def _call_research(arguments: dict[str, Any]) -> dict[str, Any]:
    result = _rd().call_bridge_jsonrpc("research", arguments, timeout=120)
    if not isinstance(result, dict):
        raise TypeError("research transport returned a non-dict")
    return result


def _research_record(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("ok") is False:
        return {"ok": False, "error": _short_error(result.get("error") or result.get("status"))}
    stored = dict(result)
    stored["ok"] = True
    return stored


def keyword_pattern(keywords: list[str]) -> re.Pattern[str]:
    parts: list[str] = []
    for raw in keywords:
        if not raw or not str(raw).strip():
            continue
        word = str(raw).strip()
        if re.match(r"^[A-Za-z0-9 .,&']+$", word):
            parts.append(r"\b" + re.escape(word) + r"\b")
        else:
            parts.append(re.escape(word))
    if not parts:
        return re.compile(r"a^")
    return re.compile("|".join(parts))


def target_keywords(company: dict[str, Any]) -> list[str]:
    raw = company.get("kw") or [company.get("name"), company.get("ticker", "")]
    return [str(item) for item in raw if item and str(item).strip()]


def excerpt_text(text: str, pattern: re.Pattern[str], limit: int = EXCERPT_PER_DOC) -> str:
    """Windows around each mention, merged, capped at EXCERPT_PER_DOC."""
    spans: list[list[int]] = []
    for match in pattern.finditer(text):
        start, end = max(0, match.start() - 300), min(len(text), match.start() + 900)
        if spans and start <= spans[-1][1]:
            spans[-1][1] = end
        else:
            spans.append([start, end])
    pieces: list[str] = []
    used = 0
    for start, end in spans:
        piece = re.sub(r"\s+", " ", text[start:end]).strip()
        if used + len(piece) > limit:
            piece = piece[: max(0, limit - used)]
            if piece:
                pieces.append(piece + " …")
            break
        pieces.append(piece)
        used += len(piece)
    return " … ".join(pieces)


def _doc_id_of(row: dict[str, Any]) -> str:
    value = row.get("document_id") or row.get("doc_id") or ""
    return str(value) if value else ""


def _own_tokens(company: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    ticker = str(company.get("ticker") or "").upper()
    if ticker:
        tokens.add(ticker)
        for piece in _TICKER_SPLIT.split(ticker):
            code = piece.strip()
            if not code:
                continue
            tokens.add(code)
            if "." in code:
                tokens.add(code.split(".", 1)[0])
    for key in [company.get("name"), *(company.get("kw") or [])]:
        if key:
            tokens.add(str(key).upper())
    tokens.discard("")
    return tokens


def _payload_dicts(payload: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return found
    found.append(payload)
    structured = payload.get("structuredContent")
    if isinstance(structured, dict):
        found.append(structured)
    inner = payload.get("data")
    if isinstance(inner, dict):
        found.append(inner)
        nested = inner.get("structuredContent")
        if isinstance(nested, dict):
            found.append(nested)
    content = payload.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("text"), str):
                continue
            try:
                parsed = json.loads(block["text"])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                found.extend(_payload_dicts(parsed))
    return found


def _extract_page(data: Any) -> tuple[str, Any]:
    for candidate in _payload_dicts(data):
        document = candidate.get("document") if isinstance(candidate.get("document"), dict) else candidate
        if not isinstance(document, dict):
            continue
        content = document.get("content") if isinstance(document.get("content"), str) else None
        text = content if content is not None else document.get("text")
        has_text = isinstance(text, str)
        if not has_text and "next_offset" not in document:
            continue
        if not isinstance(text, str):
            text = ""
        nxt = document.get("next_offset")
        if nxt is None:
            nxt = candidate.get("next_offset")
        return text, nxt
    return "", None


def read_full(document_id: str, caller: Any, cache: dict[str, str], expired: Any = None) -> str:
    if document_id in cache:
        return cache[document_id]
    parts: list[str] = []
    offset: Any = 0
    for _page in range(READ_PAGES):
        if expired is not None and expired():
            raise _BudgetExpired()
        result = caller(
            {
                "route": "all",
                "operation": "read",
                "document": document_id,
                "params": {"offset": offset, "max_chars": READ_CHARS},
            }
        )
        if not isinstance(result, dict):
            raise TypeError("research transport returned a non-dict")
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        text, nxt = _extract_page(data)
        if not text:
            text, nxt = _extract_page(result)
        if result.get("ok") is False and not text:
            raise _ReadFailed(_short_error(result.get("error") or result.get("status")))
        if not text and nxt is None:
            # A continuation page that comes back empty means the document was
            # cut short; keeping the earlier pages as a full read would hide it.
            raise _ReadFailed("truncated_document" if parts else "empty_document")
        parts.append(text)
        if not nxt or nxt == offset:
            break
        offset = nxt
    cache[document_id] = HEADER.sub("", "".join(parts))
    return cache[document_id]


def wiki_record_from_call(result: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise TypeError("research transport returned a non-dict")
    if result.get("ok") is False:
        return {"ok": False, "error": _short_error(result.get("error") or result.get("status"))}
    items = _rd().find_result_items(result) or []
    warnings = [WIKI_FULL_WARNING] if len(items) >= WIKI_LIST_LIMIT else []
    return {"ok": True, "results": items, "result_count": len(items), "warnings": warnings}


def _search_args(route: str, query: str, window: dict[str, str]) -> dict[str, Any]:
    return {
        "route": route,
        "operation": "search",
        "query": query,
        "start": window["date_from"],
        "end": window["date_to"],
        "limit": SEARCH_LIMIT,
        "params": {"search_mode": "keyword", "media": "text"},
    }


def _remember_row(found: dict[str, dict[str, Any]], row: dict[str, Any], source: str, *, own: bool | None = None) -> None:
    document_id = _doc_id_of(row)
    if not document_id or document_id.startswith("image:"):
        return
    info: dict[str, Any] = {
        "source": source,
        "date": row.get("date"),
        "sender": str(row.get("original_source") or "")[:80],
        "subject": str(row.get("title") or row.get("subject") or "")[:160],
    }
    if own is not None:
        info["own"] = own
    found.setdefault(document_id, info)


def scan_company(
    company: dict[str, Any],
    wiki_docs: list[dict[str, Any]],
    window: dict[str, str],
    caller: Any,
    cache: dict[str, str],
    expired: Any = None,
) -> dict[str, Any]:
    """One company of the old unified_scan: mail search, wiki assignment, excerpts."""

    def due() -> None:
        if expired is not None and expired():
            raise _BudgetExpired()

    keywords = target_keywords(company)
    pattern = keyword_pattern(keywords)
    terms = [word for word in re.findall(r"[\w.-]+", " ".join(keywords).casefold()) if word not in STOP]
    query = " ".join(dict.fromkeys(terms)) or " ".join(keywords)
    found: dict[str, dict[str, Any]] = {}
    limit_hit: list[str] = []
    source_errors: dict[str, str] = {}
    warnings: list[str] = []
    errors: list[str] = []
    read_errors: list[dict[str, str]] = []
    for source in MAIL_SOURCES:
        due()
        result = caller(_search_args(source, query, window))
        if not isinstance(result, dict):
            raise TypeError("research transport returned a non-dict")
        if result.get("ok") is False:
            source_errors[source] = _short_error(result.get("error") or result.get("status"))
            continue
        rows = _rd().find_result_items(result) or []
        if len(rows) >= SEARCH_LIMIT:
            full = False
            for keyword in keywords:
                due()
                tokens = [word for word in re.findall(r"[\w.-]+", keyword.casefold()) if word not in STOP]
                alias_query = " ".join(tokens) or keyword
                alias_result = caller(_search_args(source, alias_query, window))
                if not isinstance(alias_result, dict):
                    raise TypeError("research transport returned a non-dict")
                more: list[dict[str, Any]] = []
                if alias_result.get("ok") is False:
                    errors.append(f"{source} 别名：{_short_error(alias_result.get('error') or alias_result.get('status'))}")
                else:
                    more = _rd().find_result_items(alias_result) or []
                full = full or len(more) >= SEARCH_LIMIT
                rows.extend(more)
            if full:
                limit_hit.append(source)
        for row in rows:
            if isinstance(row, dict):
                _remember_row(found, row, source)
    own = _own_tokens(company)
    for row in wiki_docs:
        if isinstance(row, dict):
            _remember_row(found, row, "company-wiki", own=str(row.get("company") or "").upper() in own)
    if len(source_errors) == len(MAIL_SOURCES):
        errors.append(_short_error("all mail sources failed: " + json.dumps(source_errors, ensure_ascii=False)))
    elif source_errors:
        errors.append("部分来源失败：" + json.dumps(source_errors, ensure_ascii=False))
    picks: list[dict[str, Any]] = []
    for document_id, info in found.items():
        try:
            text = read_full(document_id, caller, cache, expired)
        except _BudgetExpired:
            raise
        except _ReadFailed as exc:
            read_errors.append({"document_id": document_id, "error": exc.code})
            errors.append(f"{document_id}: {exc.code}")
            continue
        except Exception as exc:  # noqa: BLE001 - one document read failure stays partial and is not cached as empty text
            read_errors.append({"document_id": document_id, "error": exc.__class__.__name__})
            errors.append(f"{document_id}: {exc.__class__.__name__}")
            continue
        hits = len(pattern.findall(text)) if text else 0
        if info["source"] == "company-wiki" and not info.get("own") and hits < 2:
            continue
        if not hits and not info.get("own"):
            continue
        picks.append(
            {
                "source": info["source"],
                "document_id": document_id,
                "date": info.get("date"),
                "sender": info.get("sender"),
                "subject": info.get("subject"),
                "hits": hits,
                "excerpt": excerpt_text(text, pattern) if hits else text[:EXCERPT_PER_DOC],
            }
        )
    picks.sort(key=lambda item: (-SOURCE_TIER.get(str(item["source"]), 9), item["hits"], item["date"] or ""), reverse=True)
    by_source: dict[str, int] = {}
    for item in picks:
        by_source[str(item["source"])] = by_source.get(str(item["source"]), 0) + 1
    rest = picks[PER_COMPANY:]
    payload: dict[str, Any] = {
        "ok": True,
        "name": company.get("name"),
        "matched_docs": len(picks),
        "by_source": by_source,
        "omitted": len(rest),
        "limit_hit": limit_hit,
        "warnings": warnings,
        "picks": picks[:PER_COMPANY],
        "omitted_list": [
            {key: item[key] for key in ("source", "document_id", "date", "subject", "hits")} for item in rest
        ],
    }
    if errors or read_errors:
        payload["partial"] = True
        payload["errors"] = errors
        payload["read_errors"] = read_errors
    return payload


def _load_raw_dict(workspace: Path, end: dt.date) -> dict[str, Any] | None:
    path = workspace_file(workspace, run_rel(end, "raw.json"))
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _load_collect_state(workspace: Path, end: dt.date) -> dict[str, Any] | None:
    path = workspace_file(workspace, run_rel(end, "collect-state.json"))
    if not path.exists():
        return {"attempts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    attempts = data.get("attempts", {})
    if not isinstance(attempts, dict):
        return None
    cleaned: dict[str, int] = {}
    for key, value in attempts.items():
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        cleaned[str(key)] = value
    return {"attempts": cleaned}


def _attempt_count(attempts: dict[str, int], key: str) -> int:
    value = attempts.get(key, 0)
    return value if isinstance(value, int) else 0


def _retryable(raw: dict[str, Any], attempts: dict[str, int], key: str) -> bool:
    return not _key_done(raw.get(key)) and _attempt_count(attempts, key) < ATTEMPT_LIMIT


def _collect_counts(keys: list[str], raw: dict[str, Any], attempts: dict[str, int]) -> dict[str, int]:
    completed = 0
    failed = 0
    exhausted = 0
    remaining = 0
    for key in keys:
        value = raw.get(key)
        if _key_done(value):
            completed += 1
            continue
        if isinstance(value, dict) and value.get("ok") is False:
            failed += 1
        if _attempt_count(attempts, key) >= ATTEMPT_LIMIT:
            exhausted += 1
        else:
            remaining += 1
    return {"completed": completed, "failed": failed, "exhausted": exhausted, "remaining": remaining}


def _collect_stopped(end: dt.date, relative: str, keys: list[str], error: str, detail: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": error,
        "remaining_keys": len(keys),
        "exhausted_keys": 0,
        "failed": 0,
        "completed": 0,
        "raw": relative,
        "saturday": end.isoformat(),
    }
    if detail:
        payload["detail"] = detail[:300]
    return payload


def collect_raw(workspace: Path, end: dt.date, budget_seconds: float) -> dict[str, Any]:
    """Fetch AlphaEngine, the wiki list, and per-company internal notes into raw.json."""
    plan = build_plan(end)
    keys = [job["key"] for job in plan["alphaengine"]]
    keys.append(WIKI_LIST_KEY)
    keys.extend(item["key"] for item in plan["internal"]["companies"])
    relative = run_rel(end, "raw.json")
    state_rel = run_rel(end, "collect-state.json")
    transport = _rd().resolve_researchdata_transport()
    if transport.get("mode") is None:
        detail = str(transport.get("error") or "")
        if any(marker in detail for marker in ("/Users/", "/private/", "/home/")):
            detail = ""
        return _collect_stopped(end, relative, keys, _rd().RESEARCHDATA_TRANSPORT_UNCONFIGURED, detail)
    raw = _load_raw_dict(workspace, end)
    if raw is None:
        return _collect_stopped(end, relative, keys, "raw_unreadable")
    state = _load_collect_state(workspace, end)
    if state is None:
        return _collect_stopped(end, relative, keys, "collect_state_unreadable")
    attempts: dict[str, int] = state["attempts"]
    deadline = monotonic_now() + budget_seconds
    if budget_seconds <= 0:
        deadline = monotonic_now() - 1

    def expired() -> bool:
        return monotonic_now() >= deadline

    lock = threading.Lock()

    def store(key: str, value: dict[str, Any] | None) -> None:
        if not isinstance(value, dict):
            return
        with lock:
            raw[key] = value
            write_json(workspace, relative, raw)
            if not _key_done(value):
                attempts[key] = _attempt_count(attempts, key) + 1
                write_json(workspace, state_rel, {"attempts": attempts})

    def ae_worker(job: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        key = str(job["key"])
        if expired():
            return key, None
        try:
            return key, _research_record(_call_research(job["call"]))
        except Exception as exc:  # noqa: BLE001 - one query failure is stored and the other queries continue
            return key, {"ok": False, "error": exc.__class__.__name__}

    pending = [job for job in plan["alphaengine"] if _retryable(raw, attempts, str(job["key"]))]
    if pending:
        job_iter = iter(pending)
        in_flight: dict[Any, str] = {}
        with ThreadPoolExecutor(max_workers=AE_WORKERS) as pool:

            def submit_more() -> None:
                while len(in_flight) < AE_WORKERS:
                    if expired():
                        return
                    try:
                        job = next(job_iter)
                    except StopIteration:
                        return
                    if expired():
                        return
                    in_flight[pool.submit(ae_worker, job)] = str(job["key"])

            submit_more()
            while in_flight:
                done = wait(set(in_flight), return_when=FIRST_COMPLETED)[0]
                for future in done:
                    in_flight.pop(future, None)
                    key, value = future.result()
                    store(key, value)
                if not expired():
                    submit_more()

    companies = {company["name"]: company for company in load_targets()["companies"]}
    window = plan["window"]
    cache: dict[str, str] = {}
    wiki_docs: list[dict[str, Any]] = []
    skip_companies = False
    if _key_done(raw.get(WIKI_LIST_KEY)):
        stored_wiki = raw.get(WIKI_LIST_KEY)
        if isinstance(stored_wiki, dict):
            wiki_docs = [item for item in (stored_wiki.get("results") or []) if isinstance(item, dict)]
    elif not _retryable(raw, attempts, WIKI_LIST_KEY):
        wiki_docs = []
    elif expired():
        skip_companies = True
    else:
        if expired():
            skip_companies = True
        else:
            try:
                listed = _call_research(
                    {
                        "route": "company-wiki",
                        "operation": "list",
                        "limit": WIKI_LIST_LIMIT,
                        "params": {"indexed_since": window["date_from"], "media": "text"},
                    }
                )
                record = wiki_record_from_call(listed)
            except Exception as exc:  # noqa: BLE001 - the list failure is one key; company scans wait for the next collect
                record = {"ok": False, "error": exc.__class__.__name__}
            store(WIKI_LIST_KEY, record)
            if _key_done(record):
                wiki_docs = [item for item in record.get("results") or [] if isinstance(item, dict)]
            elif _attempt_count(attempts, WIKI_LIST_KEY) < ATTEMPT_LIMIT:
                skip_companies = True

    if not skip_companies:
        for item in plan["internal"]["companies"]:
            key = item["key"]
            if not _retryable(raw, attempts, key):
                continue
            if expired():
                break
            company = companies.get(item["company"])
            if company is None:
                store(key, {"ok": False, "error": "unknown_company"})
                continue
            try:
                store(key, scan_company(company, wiki_docs, window, _call_research, cache, expired))
            except _BudgetExpired:
                break
            except Exception as exc:  # noqa: BLE001 - one company failure is stored and the next company continues
                store(key, {"ok": False, "error": exc.__class__.__name__})

    counts = _collect_counts(keys, raw, attempts)
    return {
        "ok": counts["remaining"] == 0 and counts["exhausted"] == 0,
        "remaining_keys": counts["remaining"],
        "exhausted_keys": counts["exhausted"],
        "failed": counts["failed"],
        "completed": counts["completed"],
        "raw": relative,
        "saturday": end.isoformat(),
    }


def cmd_self_test(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    checks: dict[str, bool] = {}
    try:
        cfg = load_targets()
        group_ids = {group["id"] for group in cfg["groups"]}
        checks["targets"] = TARGETS_PATH.is_file() and len(cfg["companies"]) == 35
        checks["groups"] = all(company.get("grp") in group_ids for company in cfg["companies"])
    except (OSError, json.JSONDecodeError, TypeError, KeyError):
        checks["targets"] = False
        checks["groups"] = False
    try:
        style = STYLE_PATH.read_text(encoding="utf-8")
        checks["style"] = "===BEGIN===" in style and "===END===" in style
    except OSError:
        checks["style"] = False
    try:
        import pandas
        import yfinance

        checks["pandas"] = pandas.__name__ == "pandas"
        checks["yfinance"] = yfinance.__name__ == "yfinance"
    except Exception:  # noqa: BLE001 - self-test reports a missing price library as a failed check
        checks["pandas"] = False
        checks["yfinance"] = False
    return {"ok": all(checks.values()), "checks": checks}


def cmd_window(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    return {
        "ok": True,
        "saturday": end.isoformat(),
        "is_saturday": end.weekday() == 5,
        "timezone": TZ_NAME,
        "report_run_id": report_run_id(end),
        "title": title_for(end),
        "window": make_window(end),
    }


def cmd_plan(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    return build_plan(parse_end(args.end))


def cmd_collect(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    try:
        budget = float(args.budget_seconds)
    except (TypeError, ValueError):
        return {"ok": False, "error": "budget-seconds must be a number"}
    return collect_raw(workspace, end, budget)


def cmd_prices(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    rows, cached = load_prices(workspace, end)
    payload: dict[str, Any] = {
        "ok": True,
        "cached": cached,
        "path": run_rel(end, "prices.json"),
        "big_moves_ge_8pct": big_moves_from_rows(rows),
        "saturday": end.isoformat(),
    }
    error = _price_file_error(workspace, end)
    if error:
        payload["error"] = error
    return payload


def cmd_earnings(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    _rows, cached = load_earnings(workspace, end)
    return {"ok": True, "cached": cached, "path": run_rel(end, "earnings.json"), "saturday": end.isoformat()}


def cmd_assemble(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    if not args.raw:
        return {"ok": False, "error": "missing --raw"}
    payload = read_json(workspace, args.raw)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "raw json must be an object"}
    manifest = assemble_evidence(workspace, end, payload)
    return manifest


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    which = args.which or "original"
    if which not in {"original", "restyled"}:
        return {"ok": False, "error": "which must be original or restyled"}
    return run_check(workspace, parse_end(args.end), which=which)


def cmd_doc_status(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    kind = args.kind
    if kind not in DOC_NAMES:
        return {"ok": False, "error": "kind must be original or restyled"}
    which = "restyled" if kind == "restyled" else "original"
    check = run_check(workspace, end, which=which)
    state, record = _doc_state(workspace, end, kind)
    attempts = _failed_attempts(workspace, end, kind)
    reason = None
    if state == "unreadable":
        create = False
        reason = "doc_record_unreadable"
    elif state == "present":
        create = False
    elif attempts >= 2:
        create = False
        reason = "create_failed_twice"
    elif not check.get("ok"):
        create = False
        reason = check.get("reason") or "check_failed"
    else:
        create = True
    payload: dict[str, Any] = {
        "ok": True,
        "kind": kind,
        "exists": state == "present",
        "create": create,
        "check_ok": bool(check.get("ok")),
        "url": feishu_doc_url(record),
        "path": run_rel(end, DOC_NAMES[kind]),
        "failed_attempts": attempts,
    }
    if reason:
        payload["reason"] = reason
    return payload


def cmd_save_doc(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    if not args.file:
        return {"ok": False, "error": "missing --file"}
    raw = workspace_file(workspace, args.file).read_bytes()
    return save_doc_record(workspace, end, args.kind, raw)


def cmd_restyle_segment(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    reason = original_block_reason(workspace, end)
    if reason:
        return {"ok": False, "restyle": False, "reason": reason, "create": False}
    md, links = placeholder_links(_source_draft(workspace, end))
    chunks = []
    for index, chunk in enumerate(chunks_of(md)):
        chunks.append({"index": index, "verbatim": bool(chunk.get("verbatim")), "text": chunk["text"]})
    payload = {"links": links, "chunks": chunks}
    relative = run_rel(end, "restyle", "chunks.json")
    write_json(workspace, relative, payload)
    return {
        "ok": True,
        "restyle": True,
        "create": False,
        "chunk_count": len(chunks),
        "verbatim": [chunk["index"] for chunk in chunks if chunk["verbatim"]],
        "chunks_path": relative,
        "saturday": end.isoformat(),
    }


def cmd_restyle_apply(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    if not args.rewritten:
        return {"ok": False, "error": "missing --rewritten"}
    stored = read_json(workspace, run_rel(end, "restyle", "chunks.json"))
    if not isinstance(stored, dict) or not isinstance(stored.get("chunks"), list):
        return {"ok": False, "error": "chunks.json 不存在"}
    links = stored.get("links") if isinstance(stored.get("links"), dict) else {}
    applied = apply_rewritten_chunks(stored["chunks"], _rewritten_map(read_json(workspace, args.rewritten)), links)
    draft_rel_path = run_rel(end, "restyle", "draft.md")
    write_text(workspace, draft_rel_path, applied["text"])
    write_json(workspace, run_rel(end, "restyle", "flags.json"), applied["flags"])
    source = _source_draft(workspace, end)
    gate = restyle_whole_doc(source, applied["text"])
    overall = bool(gate["ok"])
    report = {
        "ok": overall,
        "chunks_kept_original": applied["chunks_kept_original"],
        "leftover_placeholders": gate["leftover_placeholders"],
        "headings_equal": gate["headings_equal"],
        "urls_equal": gate["urls_equal"],
        "prices_equal": gate["prices_equal"],
        "reason": gate["reason"],
        "report": applied["report"],
    }
    write_json(workspace, run_rel(end, "restyle", "report.json"), report)
    return {
        "ok": overall,
        "draft": draft_rel_path,
        "chunks_kept_original": applied["chunks_kept_original"],
        "leftover_placeholders": gate["leftover_placeholders"],
        "headings_equal": gate["headings_equal"],
        "urls_equal": gate["urls_equal"],
        "prices_equal": gate["prices_equal"],
        "reason": gate["reason"],
        "flags_path": run_rel(end, "restyle", "flags.json"),
    }


def cmd_summary(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    if not args.input:
        return {"ok": False, "error": "missing --input"}
    payload = read_json(workspace, args.input)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "summary input must be an object"}
    return render_summary(workspace, end, payload)


def cmd_note_failure(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    end = parse_end(args.end)
    message = (args.message or "").strip()
    if not message:
        return {"ok": False, "error": "missing message"}
    relative = run_rel(end, "failure.json")
    write_json(workspace, relative, {"ok": False, "end": end.isoformat(), "message": message})
    return {"ok": True, "stored": True, "path": relative}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mark weekly new-energy helper")
    parser.add_argument("--workspace-dir", default=None)
    parser.add_argument("--json", action="store_true")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("self-test", parents=[shared])
    probe.set_defaults(func=cmd_self_test)

    window = sub.add_parser("window", parents=[shared])
    window.add_argument("--end")
    window.set_defaults(func=cmd_window)

    plan = sub.add_parser("plan", parents=[shared])
    plan.add_argument("--end")
    plan.set_defaults(func=cmd_plan)

    collect = sub.add_parser("collect", parents=[shared])
    collect.add_argument("--end")
    collect.add_argument("--budget-seconds", type=float, default=2400)
    collect.set_defaults(func=cmd_collect)

    prices = sub.add_parser("prices", parents=[shared])
    prices.add_argument("--end")
    prices.set_defaults(func=cmd_prices)

    earnings = sub.add_parser("earnings", parents=[shared])
    earnings.add_argument("--end")
    earnings.set_defaults(func=cmd_earnings)

    assembled = sub.add_parser("assemble", parents=[shared])
    assembled.add_argument("--end")
    assembled.add_argument("--raw")
    assembled.set_defaults(func=cmd_assemble)

    check = sub.add_parser("check", parents=[shared])
    check.add_argument("--end")
    check.add_argument("--which", default="original")
    check.set_defaults(func=cmd_check)

    status = sub.add_parser("doc-status", parents=[shared])
    status.add_argument("--end")
    status.add_argument("--kind", required=True)
    status.set_defaults(func=cmd_doc_status)

    save = sub.add_parser("save-doc", parents=[shared])
    save.add_argument("--end")
    save.add_argument("--kind", required=True)
    save.add_argument("--file", required=True)
    save.set_defaults(func=cmd_save_doc)

    segment = sub.add_parser("restyle-segment", parents=[shared])
    segment.add_argument("--end")
    segment.set_defaults(func=cmd_restyle_segment)

    apply_cmd = sub.add_parser("restyle-apply", parents=[shared])
    apply_cmd.add_argument("--end")
    apply_cmd.add_argument("--rewritten", required=True)
    apply_cmd.set_defaults(func=cmd_restyle_apply)

    summary = sub.add_parser("summary", parents=[shared])
    summary.add_argument("--end")
    summary.add_argument("--input", required=True)
    summary.set_defaults(func=cmd_summary)

    failure = sub.add_parser("note-failure", parents=[shared])
    failure.add_argument("--end")
    failure.add_argument("--message", required=True)
    failure.set_defaults(func=cmd_note_failure)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _build_parser().parse_args(argv)
    except SystemExit:
        return 2
    try:
        result = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        result = {"ok": False, "error": _public_error(exc)}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
