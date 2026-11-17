"""Coverage-pool filing scan for the daily filing alert.

SEC listings come from the EDGAR submissions feed. A-share and Hong Kong
filings are normalized from the finance cn-hk JSON the agent already wrote.
A-share notice text in that JSON is parsed into body_excerpt, ndr, and digest.
last_check is written after filings.json and the scan output both succeed.
scan-cn-hk does not write last_check.

Threat model: defends against model payload errors/hallucinations/amount mixing
(payload JSON is untrusted), NOT against an attacker forging the script-written
scan output file itself.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

HK = ZoneInfo("Asia/Hong_Kong")
STATE_PREFIX = "state/daily-filing-alert"
WEEKDAY = "一二三四五六日"
_STATE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$")
_MODS: dict[str, Any] = {}

FILING_CATEGORIES = {
    "earnings": ["8-K", "6-K"],
    "insider": ["4"],
    "annual_quarterly": ["10-K", "10-Q", "20-F"],
    "proxy": ["DEF 14A", "DEFA14A"],
    "offering": ["S-1", "F-1", "424B"],
    "ownership": ["SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"],
    "other": ["144"],
}
ALL_FORM_TYPES = [form for forms in FILING_CATEGORIES.values() for form in forms]
CN_HK_LIMIT = 50
_CALL_META = {
    "start",
    "end",
    "limit",
    "ok",
    "status",
    "calls",
    "rows",
    "data",
    "error",
    "message",
    "filings",
    "items",
    "results",
    "_sources",
    "normalized_params",
}
ENRICH_KEYS = (
    "url",
    "doc_url",
    "headline",
    "item_labels",
    "item_titles",
    "body_excerpt",
    "cover_summary",
    "report_date",
    "reporting_persons",
    "percent_of_class",
    "shares",
    "amendment_no",
    "event_date",
    "seller",
    "position",
    "market_value",
    "sale_date",
    "nature",
    "enrich_error",
)


def _locals() -> dict[str, Any]:
    global _MODS
    if _MODS:
        return _MODS
    here = Path(__file__).resolve().parent
    modules: dict[str, Any] = {}
    for name in ("netio", "card_io", "filing_card", "edgar_client", "cn_ndr", "buyback_parse"):
        qualified = f"dalton_daily_filing_{name}"
        existing = sys.modules.get(qualified)
        expected = str(here / f"{name}.py")
        if existing is not None and getattr(existing, "__file__", None) == expected:
            modules[name] = existing
            continue
        spec = importlib.util.spec_from_file_location(qualified, expected)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"missing {name}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[qualified] = module
        spec.loader.exec_module(module)
        modules[name] = module
    _MODS = modules
    return modules


def _skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def require_workspace(raw: str | None) -> Path:
    if not raw:
        raise RuntimeError("missing --workspace-dir")
    path = Path(raw)
    if path.is_symlink():
        raise RuntimeError("workspace is a symlink")
    if not path.is_dir():
        raise RuntimeError("workspace not found")
    return path.resolve()


def parse_as_of(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(HK)
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=HK)
    return parsed.astimezone(HK)


def describe_window(as_of: datetime) -> dict[str, Any]:
    local = as_of.astimezone(HK)
    monday = local.weekday() == 0
    report_date = local.strftime("%Y-%m-%d")
    return {
        "ok": True,
        "report_date": report_date,
        "weekday": "周" + WEEKDAY[local.weekday()],
        "lookback_days": 3 if monday else 1,
        "window_note": "覆盖上周五至今" if monday else "覆盖过去 24 小时",
        "report_run_id": f"daily-filing-alert-{report_date}",
        "tz": "Asia/Hong_Kong",
    }


def _bundled_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("bundled watchlist missing")
    return path.read_text(encoding="utf-8")


def _symbol_lines(text: str) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            sym = stripped.upper()
            if sym not in seen:
                seen.add(sym)
                symbols.append(sym)
    return symbols


def parse_tickers(workspace: Path, raw: str) -> list[str]:
    if not raw:
        return _symbol_lines(_bundled_text(_skill_dir() / "watchlist.txt"))
    if "/" in raw or "\\" in raw or raw.endswith(".txt"):
        path = _locals()["card_io"].workspace_file(workspace, raw)
        if not path.is_file():
            raise RuntimeError("watchlist file not found")
        return _symbol_lines(path.read_text(encoding="utf-8"))
    symbols: list[str] = []
    seen: set[str] = set()
    for item in raw.split(","):
        sym = item.strip().upper()
        if sym and sym not in seen:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def parse_cn_hk_text(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    cn_stocks: list[tuple[str, str]] = []
    hk_stocks: list[tuple[str, str]] = []
    seen_cn: set[str] = set()
    seen_hk: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2:
            continue
        code, name = parts[0].strip(), parts[1].strip()
        if ".HK" in code.upper() or (code.startswith("0") and len(code) == 5):
            clean = re.sub(r"\.hk$", "", code, flags=re.IGNORECASE)
            if clean not in seen_hk:
                seen_hk.add(clean)
                hk_stocks.append((clean, name))
        else:
            if code not in seen_cn:
                seen_cn.add(code)
                cn_stocks.append((code, name))
    return cn_stocks, hk_stocks


def load_cn_hk_watchlist(workspace: Path, raw: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    if raw:
        path = _locals()["card_io"].workspace_file(workspace, raw)
        if not path.is_file():
            raise RuntimeError("watchlist file not found")
        text = path.read_text(encoding="utf-8")
    else:
        text = _bundled_text(_skill_dir() / "watchlist_cn_hk.txt")
    return parse_cn_hk_text(text)


def state_io(workspace: Path):
    card_io = _locals()["card_io"]
    card_io.workspace_file(workspace, f"{STATE_PREFIX}/last_check.json")

    def read_name(name: str) -> str | None:
        if _STATE_NAME.fullmatch(name) is None:
            raise ValueError("path must stay inside the workspace")
        path = card_io.workspace_file(workspace, f"{STATE_PREFIX}/{name}")
        if not path.exists():
            return None
        if path.is_symlink():
            raise ValueError("refusing symlink")
        return path.read_text(encoding="utf-8")

    def write_name(name: str, text: str) -> None:
        if _STATE_NAME.fullmatch(name) is None:
            raise ValueError("path must stay inside the workspace")
        path = card_io.workspace_file(workspace, f"{STATE_PREFIX}/{name}")
        payload = text if text.endswith("\n") else text + "\n"
        card_io.write_bytes_exclusive(path, payload.encode("utf-8"))

    return read_name, write_name


def write_json(workspace: Path, relative: str, payload: dict[str, Any]) -> None:
    card_io = _locals()["card_io"]
    path = card_io.workspace_file(workspace, relative)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    card_io.write_bytes_exclusive(path, text.encode("utf-8"))


def read_json(workspace: Path, relative: str) -> Any:
    card_io = _locals()["card_io"]
    path = card_io.workspace_file(workspace, relative)
    if not path.is_file():
        raise RuntimeError("json file not found")
    return json.loads(path.read_text(encoding="utf-8"))


def categorize_filing(form: str) -> str:
    edgar = _locals()["edgar_client"]
    form_u = (form or "").upper().strip()
    if form_u in {"FORM 4", "4", "4/A"}:
        return "insider"
    for category, forms in FILING_CATEGORIES.items():
        if any(edgar.form_matches(form_u, candidate) for candidate in forms):
            return category
    return "other"


def _apply_enrichment(filing: dict[str, Any], extra: dict[str, Any]) -> None:
    if not extra:
        return
    if filing.get("type") == "Form 4":
        if extra.get("url"):
            filing.setdefault("url", extra["url"])
        if extra.get("parsed"):
            filing["parsed"] = extra["parsed"]
        if extra.get("headline"):
            filing["headline"] = extra["headline"]
        if extra.get("enrich_error"):
            filing["enrich_error"] = extra["enrich_error"]
        return
    for key in ENRICH_KEYS:
        value = extra.get(key)
        if value not in (None, "", [], {}):
            filing[key] = value


def check_filings(ticker: str, days: int, as_of: datetime, enrich: bool = True) -> dict[str, Any]:
    cutoff = (as_of - timedelta(days=days)).strftime("%Y-%m-%d")
    edgar = _locals()["edgar_client"]
    listed = edgar.list_recent_filings(ticker, ALL_FORM_TYPES, limit_per_form=5)
    filings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in listed.get("filings") or []:
        filing_date = row.get("date") or ""
        if filing_date < cutoff:
            continue
        accession = row.get("accession") or ""
        if accession and accession in seen:
            continue
        if accession:
            seen.add(accession)
        form = row.get("type") or row.get("form") or ""
        filings.append({
            "type": form,
            "date": filing_date,
            "accession": accession,
            "category": "insider" if form == "Form 4" else categorize_filing(row.get("form") or form),
        })
    filings.sort(key=lambda item: item["date"], reverse=True)
    if enrich:
        for filing in filings:
            extra = edgar.enrich_filing(
                ticker,
                filing["type"],
                filing["accession"],
                cik=listed.get("cik") or None,
            )
            _apply_enrichment(filing, extra)
    out: dict[str, Any] = {
        "ticker": ticker,
        "new_filings": filings,
        "errors": list(listed.get("errors") or []),
        "count": len(filings),
        "cutoff": cutoff,
    }
    if listed.get("ambiguous_ciks"):
        out["ambiguous_ciks"] = list(listed["ambiguous_ciks"])
    return out


def _dump_filings(results: list[dict[str, Any]], scan_time: str) -> dict[str, Any]:
    flat = []
    for result in results:
        for filing in result.get("new_filings") or []:
            parsed = filing.get("parsed") or {}
            flat.append({
                "ticker": result.get("ticker"),
                "form": filing.get("type"),
                "date": filing.get("date"),
                "accession": filing.get("accession"),
                "category": filing.get("category"),
                "url": filing.get("url"),
                "headline": filing.get("headline") or parsed.get("summary", ""),
                "item_labels": filing.get("item_labels", []),
                "body_excerpt": filing.get("body_excerpt", ""),
                "cover_summary": filing.get("cover_summary", ""),
                "report_date": filing.get("report_date", ""),
                "enrich_error": filing.get("enrich_error", ""),
            })
    return {"scan_time": scan_time, "filings": flat}


def _save_last_check(read_name, write_name, tickers: list[str], scan_time: str) -> None:
    raw = read_name("last_check.json")
    state: dict[str, Any] = {}
    if raw:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                state = loaded
        except json.JSONDecodeError:
            state = {}
    for ticker in tickers:
        state[ticker] = scan_time
    write_name("last_check.json", json.dumps(state, indent=2, ensure_ascii=False))


def _window_bounds(as_of: datetime, lookback_days: int) -> tuple[str, str]:
    """SEC window: filing date >= report date minus lookback_days, through the report date."""
    local = as_of.astimezone(HK)
    end = local.strftime("%Y-%m-%d")
    start = (local - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    return start, end


def _market_bounds(report_end: str, days: int, market: str) -> tuple[str, str]:
    """Original A-share and Hong Kong date bounds, anchored on the report date.

    A-share scans ``range(days)`` calendar dates ending on the report date, so
    ``days == 1`` is that date only. Hong Kong keeps the old search window:
    ``date_after`` is the report date minus ``days``, and ``date_before`` is
    the next calendar day.
    """
    end = datetime.strptime(report_end, "%Y-%m-%d").date()
    span = max(0, days)
    if market == "A":
        start = end - timedelta(days=max(span - 1, 0))
        return start.isoformat(), end.isoformat()
    start = end - timedelta(days=span)
    stop = end + timedelta(days=1)
    return start.isoformat(), stop.isoformat()


def _finance_ticker(code: str) -> str:
    raw = code.strip().upper().replace(".HK", "")
    if raw.isdigit() and len(raw) <= 5:
        return raw.zfill(5)
    return raw


def _query_rows(
    stocks: list[tuple[str, str]],
    market: str,
    start: str,
    end: str,
) -> list[dict[str, str]]:
    return [
        {
            "ticker": _finance_ticker(code),
            "code": code,
            "name": name,
            "market": market,
            "start": start,
            "end": end,
        }
        for code, name in stocks
    ]


def cmd_window(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    as_of = parse_as_of(getattr(args, "as_of", None))
    window = describe_window(as_of)
    start, end = _window_bounds(as_of, window["lookback_days"])
    cn_stocks, hk_stocks = load_cn_hk_watchlist(workspace, "")
    window["start"] = start
    window["end"] = end
    window["limit"] = CN_HK_LIMIT
    window["cn_hk"] = []
    for market, stocks in (("A", cn_stocks), ("HK", hk_stocks)):
        market_start, market_end = _market_bounds(end, window["lookback_days"], market)
        window["cn_hk"].extend(_query_rows(stocks, market, market_start, market_end))
    return window


def cmd_scan(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    as_of = parse_as_of(getattr(args, "as_of", None))
    window = describe_window(as_of)
    tickers = parse_tickers(workspace, args.tickers or "")
    if not tickers:
        return {"ok": False, "error": "No tickers provided"}
    _locals()["card_io"].workspace_file(workspace, args.output)
    read_name, write_name = state_io(workspace)
    edgar = _locals()["edgar_client"]
    edgar.bind_state(read_name, write_name)
    results = [
        check_filings(ticker, args.days, as_of, enrich=not args.no_enrich)
        for ticker in tickers
    ]
    if not args.no_enrich:
        edgar.save_enrich_cache()
    scan_time = as_of.isoformat()
    alert_tickers = [result["ticker"] for result in results if result["count"] > 0]
    document = {
        "ok": True,
        "scan_time": scan_time,
        "lookback_days": args.days,
        "report_date": window["report_date"],
        "report_run_id": window["report_run_id"],
        "tickers_scanned": len(tickers),
        "tickers_with_alerts": len(alert_tickers),
        "results": results,
    }
    write_name("filings.json", json.dumps(_dump_filings(results, scan_time), indent=2, ensure_ascii=False))
    write_json(workspace, args.output, document)
    _save_last_check(read_name, write_name, tickers, scan_time)
    return {
        "ok": True,
        "path": args.output,
        "report_date": window["report_date"],
        "report_run_id": window["report_run_id"],
        "lookback_days": args.days,
        "tickers_scanned": len(tickers),
        "tickers_with_alerts": len(alert_tickers),
    }


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _public_text(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())[:limit]
    if not text or "/Users/" in text or text.startswith("/") or "\\" in text:
        return ""
    return text


def _safe_url(value: Any) -> str:
    text = str(value or "").strip()
    if not (text.startswith(("https://", "http://"))):
        return ""
    if "/Users/" in text or "\\" in text:
        return ""
    return text[:500]


def _row_date(row: dict[str, Any]) -> str:
    raw = str(row.get("date") or row.get("filing_date") or "").strip()
    return raw[:10]


def _unwrap_rows(blob: Any) -> list[dict[str, Any]]:
    if isinstance(blob, list):
        return [row for row in blob if isinstance(row, dict)]
    if not isinstance(blob, dict):
        return []
    sources = [blob]
    inner = blob.get("data")
    if isinstance(inner, dict):
        sources.append(inner)
    for source in sources:
        for key in ("rows", "filings", "items", "results", "data"):
            found = source.get(key)
            if isinstance(found, list) and found:
                return [row for row in found if isinstance(row, dict)]
    return []


def _iter_calls(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("calls"), list):
        return [item for item in raw["calls"] if isinstance(item, dict)]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    if raw.get("ticker") and (_unwrap_rows(raw) or raw.get("ok") is False or raw.get("error")):
        return [raw]
    calls = []
    for key, value in raw.items():
        if key in _CALL_META:
            continue
        if isinstance(value, dict):
            call = dict(value)
            call.setdefault("ticker", key)
            calls.append(call)
        elif isinstance(value, list):
            calls.append({"ticker": key, "ok": True, "rows": value})
    return calls


def _group_calls(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Keep every call for a ticker so date segments can be merged."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for call in _iter_calls(raw):
        ticker = _finance_ticker(str(call.get("ticker") or ""))
        if ticker:
            grouped.setdefault(ticker, []).append(call)
    return grouped


def _call_ok(call: dict[str, Any]) -> bool:
    if call.get("ok") is False or call.get("isError") is True:
        return False
    return str(call.get("status") or "").lower() != "error"


def _call_error(call: dict[str, Any]) -> str:
    err = call.get("error") or call.get("message") or "finance failed"
    if isinstance(err, dict):
        err = err.get("error") or err.get("message") or "finance failed"
    return _public_text(err) or "finance failed"


def _report_end(raw: Any, as_of: datetime) -> str:
    """Anchor CN/HK filtering strictly on the HK report date from ``as_of``."""
    return as_of.astimezone(HK).strftime("%Y-%m-%d")


def _report_end_gap_note(raw: Any, report_end: str) -> str:
    if not isinstance(raw, dict):
        return ""
    raw_end = str(raw.get("end") or "").strip()
    if not raw_end:
        return ""
    if raw_end[:10] == report_end:
        try:
            datetime.strptime(raw_end[:10], "%Y-%m-%d")
            return ""
        except ValueError:
            pass
    return f"raw JSON 顶层 end（{raw_end[:40]}）与香港报告日（{report_end}）不一致，已按 {report_end} 筛选"


def _file_limit(raw: Any) -> int:
    if isinstance(raw, dict) and raw.get("limit") is not None:
        return _as_int(raw.get("limit"), CN_HK_LIMIT)
    return CN_HK_LIMIT


def _notice_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    if "/Users/" in value or "\\" in value:
        return ""
    return value


_BUYBACK_ANN_RE = re.compile(
    r"(?:翌日披露|next\s+day\s+disclosure|股份[購购]回|股份回[購购]|[購购]回|回[購购]|[買买]回|"
    r"(?:购入|購入)[^\n。；;]*?(?:股份|股票|H股|A股|普通股)|"
    r"\b(?:share[- ]?)?repurchas\w*|\bbuy[- ]?backs?\b)",
    re.IGNORECASE,
)
_AMENDMENT_TITLE_RE = re.compile(r"(?:更正|修訂|修订|補充|补充)")
_STATUS_LINE_RE = re.compile(r"[狀状]\s*[態态][：:]\s*([^\n]+)")


def _is_buyback_announcement(
    item: dict[str, Any],
    row: dict[str, Any] | None = None,
    *,
    allow_notice_text: bool = True,
) -> bool:
    title = str(item.get("title") or "")
    ann_type = str(item.get("type") or "")
    if _BUYBACK_ANN_RE.search(title) or _BUYBACK_ANN_RE.search(ann_type):
        return True
    if row is not None and allow_notice_text:
        text = _notice_text(row.get("notice_text"))
        if "翌日披露報表" in text[:400]:
            return True
    return False


def _is_amendment_row(
    item: dict[str, Any],
    row: dict[str, Any],
    *,
    allow_notice_text: bool = True,
) -> bool:
    if _AMENDMENT_TITLE_RE.search(str(item.get("title") or "")):
        return True
    if not allow_notice_text:
        return False
    text = _notice_text(row.get("notice_text"))
    if not text:
        return False
    status_m = _STATUS_LINE_RE.search(text[:800])
    if status_m and _AMENDMENT_TITLE_RE.search(status_m.group(1)):
        return True
    first_lines = "\n".join(line.strip() for line in text.splitlines()[:3] if line.strip())
    return bool(_AMENDMENT_TITLE_RE.search(first_lines))


def _filter_buyback_amendments(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    *,
    allow_notice_text: bool = True,
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], list[tuple[dict[str, Any], dict[str, Any]]]]:
    has_amendment = any(
        _is_buyback_announcement(item, row, allow_notice_text=allow_notice_text)
        and _is_amendment_row(item, row, allow_notice_text=allow_notice_text)
        for item, row in pairs
    )
    if not has_amendment:
        return pairs, []
    kept: list[tuple[dict[str, Any], dict[str, Any]]] = []
    dropped: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for item, row in pairs:
        if (
            not _is_buyback_announcement(item, row, allow_notice_text=allow_notice_text)
            or _is_amendment_row(item, row, allow_notice_text=allow_notice_text)
        ):
            kept.append((item, row))
        else:
            dropped.append((item, row))
    return kept, dropped


def _apply_a_share_body(item: dict[str, Any], row: dict[str, Any]) -> None:
    """Fill the old enrich fields from agent-supplied notice text. A shares only."""
    url = item.get("url") or ""
    if url:
        item["pdf_url"] = url
    text = _notice_text(row.get("notice_text"))
    agent_error = _public_text(row.get("enrich_error"), limit=300)
    if not text.strip():
        item["enrich_error"] = agent_error or "正文为空（可能只有 PDF）"
        return
    parser = _locals()["cn_ndr"]
    title = item.get("title") or ""
    is_ndr = "翌日披露" in title or "翌日披露報表" in text[:400]
    if is_ndr:
        try:
            parsed = parser.parse_ndr(text)
        except Exception as exc:  # noqa: BLE001 - one next-day form failure is recorded on that notice
            item["enrich_error"] = f"翌日披露报表解析失败: {exc}"
            return
        _raw_sc, conflict_err = _locals()["buyback_parse"].stock_code_conflict(str(item.get("code") or ""), parsed)
        if conflict_err:
            item["enrich_error"] = f"翌日披露报表身份冲突: {conflict_err}"
            return
        item["body_excerpt"] = parser.body_excerpt(text)
        item["ndr"] = parsed
        item["digest"] = parsed["digest"]
        return
    if ("第二章節" in text or "第二章节" in text) and ("購回報告" in text or "购回报告" in text):
        validated = _locals()["buyback_parse"].parse(text)
        if "error" in validated:
            item["enrich_error"] = f"回购报表解析失败: {validated['error']}"
            return
        _raw_sc, conflict_err = _locals()["buyback_parse"].stock_code_conflict(str(item.get("code") or ""), validated)
        if conflict_err:
            item["enrich_error"] = f"回购报表身份冲突: {conflict_err}"
            return
        item["body_excerpt"] = parser.body_excerpt(text)
        item["buyback"] = validated
        item["digest"] = validated["line"]
        return
    item["body_excerpt"] = parser.body_excerpt(text)


def _announcement(row: dict[str, Any], code: str, name: str) -> dict[str, Any] | None:
    title = _public_text(row.get("title"), limit=500)
    category = _public_text(row.get("category") or row.get("type"), limit=120)
    url = _safe_url(row.get("pdf_link") or row.get("url"))
    if not title and not url:
        return None
    day = _row_date(row)
    explicit_id = _public_text(row.get("id") or row.get("announcement_id") or row.get("notice_id"), limit=200)
    if explicit_id:
        ann_id = explicit_id
    elif url:
        ann_id = url
    else:
        digest = hashlib.sha1(f"{code}|{day}|{title}".encode("utf-8")).hexdigest()[:12]
        ann_id = f"{code}:{day}:{digest}"
    return {
        "id": ann_id,
        "code": code,
        "name": name,
        "title": title,
        "type": category,
        "date": day,
        "url": url,
    }


def _validate_buyback_scan_entry(
    item: dict[str, Any],
    row: dict[str, Any],
    market: str,
) -> dict[str, Any] | None:
    # M2: HK rows must never trust model-supplied notice_text; only A-share rows use notice_text.
    text = _notice_text(row.get("notice_text")) if market == "A" else ""
    is_bb = _is_buyback_announcement(item) or (market == "A" and "翌日披露報表" in text[:400])
    if market == "A":
        is_bb = is_bb or "ndr" in item or "buyback" in item
    if not is_bb:
        return None
    bp = _locals()["buyback_parse"]
    common = {
        "ann_id": str(item.get("id") or ""),
        "url": str(item.get("url") or ""),
        "code": str(item.get("code") or ""),
        "date": str(item.get("date") or ""),
        "title": str(item.get("title") or ""),
    }
    if market == "A":
        if item.get("enrich_error"):
            err_text = str(item["enrich_error"])
            err_payload: dict[str, Any] = {"error": err_text}
            sc_match = re.search(r"证券代号（([^）]+)）", err_text)
            if sc_match:
                err_payload["stock_code"] = sc_match.group(1).strip()
            return bp.scan_record_from_parsed(err_payload, **common)
        if isinstance(item.get("ndr"), dict):
            _parsed, rec = bp.validate_for_scan(text, ndr_data=item["ndr"], **common)
            return rec
        if isinstance(item.get("buyback"), dict):
            return bp.scan_record_from_parsed(item["buyback"], **common)
        return bp.scan_record_from_parsed({"error": "未解析到第二章節购回明细"}, **common)

    agent_error = _public_text(row.get("enrich_error"), limit=300)
    return bp.scan_record_from_parsed({"error": agent_error or "未解析回购PDF"}, **common)


def _merge_preserved_buybacks(
    buyback_results: dict[str, dict[str, Any]],
    existing_buyback_results: dict[str, dict[str, Any]],
    dropped_keys: set[str],
) -> None:
    """Preserve prior validated buyback entries across scan-cn-hk reruns."""
    if not existing_buyback_results:
        return
    token_to_current_key: dict[str, str] = {}
    for cur_key, cur_rec in buyback_results.items():
        if cur_key:
            token_to_current_key[cur_key] = cur_key
        if isinstance(cur_rec, dict):
            for field in ("id", "url", "pdf_url"):
                tok = str(cur_rec.get(field) or "").strip()
                if tok:
                    token_to_current_key.setdefault(tok, cur_key)

    for prev_key, prev_rec in existing_buyback_results.items():
        if not isinstance(prev_rec, dict) or prev_rec.get("validated") is not True:
            continue
        prev_tokens = [
            tok
            for tok in (
                prev_key,
                str(prev_rec.get("id") or "").strip(),
                str(prev_rec.get("url") or "").strip(),
                str(prev_rec.get("pdf_url") or "").strip(),
            )
            if tok
        ]
        if any(tok in dropped_keys for tok in prev_tokens):
            continue
        matched_cur_key = next(
            (token_to_current_key[tok] for tok in prev_tokens if tok in token_to_current_key),
            None,
        )
        if matched_cur_key is None:
            buyback_results[prev_key] = prev_rec
        elif buyback_results[matched_cur_key].get("validated") is not True:
            buyback_results[matched_cur_key] = prev_rec


def _cn_hk_result(
    code: str,
    name: str,
    market: str,
    calls: list[dict[str, Any]] | None,
    start: str,
    end: str,
    limit: int,
    *,
    buyback_results: dict[str, dict[str, Any]] | None = None,
    dropped_keys: set[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ticker": code,
        "name": name,
        "market": market,
        "announcements": [],
        "errors": [],
        "count": 0,
        "cutoff": start,
        "truncated": False,
    }
    if not calls:
        result["errors"] = ["finance result missing"]
        return result
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    any_ok = False
    for call in calls:
        if not _call_ok(call):
            errors.append(_call_error(call))
            continue
        any_ok = True
        got = _unwrap_rows(call)
        call_limit = _as_int(call.get("limit"), limit)
        if call_limit > 0 and len(got) >= call_limit:
            result["truncated"] = True
        rows.extend(got)
    if not any_ok:
        result["errors"] = errors or ["finance failed"]
        return result
    result["errors"] = errors
    seen: set[str] = set()
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        day = _row_date(row)
        if len(day) != 10 or day < start or day > end:
            continue
        item = _announcement(row, code, name)
        if item is None:
            continue
        key = f"{item['title']}|{item['date']}|{item['url']}"
        if key in seen:
            continue
        seen.add(key)
        pairs.append((item, row))
    pairs, dropped = _filter_buyback_amendments(pairs, allow_notice_text=(market == "A"))
    for item, _row in dropped:
        title = str(item.get("title") or "").strip() or "未命名公告"
        day = str(item.get("date") or "").strip()
        desc = f"{day} {title}".strip()
        result["errors"].append(f"存在更正回购公告，已过滤: {desc}")
        if dropped_keys is not None:
            for token_field in ("id", "url", "pdf_url"):
                tok = str(item.get(token_field) or "").strip()
                if tok:
                    dropped_keys.add(tok)
    announcements = []
    for item, row in pairs:
        if market == "A":
            _apply_a_share_body(item, row)
        rec = _validate_buyback_scan_entry(item, row, market)
        if rec is not None:
            if rec.get("validated") is False and "证券代号" in str(rec.get("error") or ""):
                title = str(item.get("title") or "").strip() or "未命名公告"
                day = str(item.get("date") or "").strip()
                desc = f"{day} {title}".strip()
                result["errors"].append(f"回购公告证券代号与公司不一致: {desc}（{rec['error']}）")
            if buyback_results is not None:
                key = str(rec.get("id") or rec.get("url") or "").strip()
                if key:
                    buyback_results[key] = rec
        announcements.append(item)
    result["announcements"] = announcements
    result["count"] = len(announcements)
    return result


def cmd_scan_cn_hk(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    as_of = parse_as_of(getattr(args, "as_of", None))
    window = describe_window(as_of)
    cn_stocks, hk_stocks = load_cn_hk_watchlist(workspace, getattr(args, "watchlist", "") or "")
    if not cn_stocks and not hk_stocks:
        return {"ok": False, "error": "No CN/HK stocks in watchlist"}
    raw = read_json(workspace, args.input)
    out_path = _locals()["card_io"].workspace_file(workspace, args.output)
    existing_buyback_results: dict[str, dict[str, Any]] = {}
    if out_path.exists():
        if out_path.is_symlink():
            raise RuntimeError("refusing symlink")
        if out_path.is_file():
            try:
                existing_doc = json.loads(out_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing_doc = None
            if isinstance(existing_doc, dict) and isinstance(existing_doc.get("buyback_results"), dict):
                existing_buyback_results = {
                    str(k): v
                    for k, v in existing_doc["buyback_results"].items()
                    if isinstance(v, dict)
                }
    report_end = _report_end(raw, as_of)
    limit = _file_limit(raw)
    grouped = _group_calls(raw)
    results = []
    buyback_results: dict[str, dict[str, Any]] = {}
    dropped_keys: set[str] = set()
    day_errors: list[str] = []
    end_gap = _report_end_gap_note(raw, report_end)
    if end_gap:
        day_errors.append(end_gap)
    truncated: list[str] = []
    alert_count = 0
    for market, stocks in (("A", cn_stocks), ("HK", hk_stocks)):
        start, end = _market_bounds(report_end, args.days, market)
        for code, name in stocks:
            result = _cn_hk_result(
                code,
                name,
                market,
                grouped.get(_finance_ticker(code)),
                start,
                end,
                limit,
                buyback_results=buyback_results,
                dropped_keys=dropped_keys,
            )
            if result["count"]:
                alert_count += 1
            if result["truncated"]:
                truncated.append(code)
            for message in result["errors"]:
                day_errors.append(f"{code}: {message}")
            results.append(result)
    _merge_preserved_buybacks(buyback_results, existing_buyback_results, dropped_keys)
    document = {
        "ok": True,
        "scan_time": as_of.isoformat(),
        "lookback_days": args.days,
        "report_date": window["report_date"],
        "report_run_id": window["report_run_id"],
        "cn_stocks_scanned": len(cn_stocks),
        "hk_stocks_scanned": len(hk_stocks),
        "stocks_with_alerts": alert_count,
        "day_errors": day_errors,
        "truncated": truncated,
        "buyback_results": buyback_results,
        "results": results,
    }
    write_json(workspace, args.output, document)
    return {
        "ok": True,
        "path": args.output,
        "report_date": window["report_date"],
        "report_run_id": window["report_run_id"],
        "cn_stocks_scanned": len(cn_stocks),
        "hk_stocks_scanned": len(hk_stocks),
        "stocks_with_alerts": alert_count,
        "day_errors": day_errors,
        "truncated": truncated,
    }


def cmd_check(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    as_of = parse_as_of(getattr(args, "as_of", None))
    read_name, write_name = state_io(workspace)
    edgar = _locals()["edgar_client"]
    edgar.bind_state(read_name, write_name)
    result = check_filings(args.ticker.upper().strip(), args.days, as_of, enrich=not args.no_enrich)
    if not args.no_enrich:
        edgar.save_enrich_cache()
    return {"ok": True, **result}


def _load_render_scan_data(workspace: Path, payload: dict[str, Any], cn_hk_arg: str) -> dict[str, Any] | None:
    card_io = _locals()["card_io"]
    if cn_hk_arg:
        loaded = read_json(workspace, cn_hk_arg)
        if not isinstance(loaded, dict):
            raise RuntimeError("cn-hk scan file must be a JSON object")
        return loaded
    payload_date = str(payload.get("date") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", payload_date):
        default_rel = f"temp/daily-filing-alert/{payload_date}-cn-hk.json"
        candidate = card_io.workspace_file(workspace, default_rel)
        if candidate.is_file():
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                return loaded
    return None


def cmd_render(args: argparse.Namespace) -> dict[str, Any]:
    workspace = require_workspace(args.workspace_dir)
    if args.payload == args.output:
        return {"ok": False, "error": "card output must differ from the payload"}
    payload = read_json(workspace, args.payload)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be an object"}
    scan_data = _load_render_scan_data(workspace, payload, getattr(args, "cn_hk", "") or "")
    card_io = _locals()["card_io"]
    card, day_errors = _locals()["filing_card"].build_card_with_notes(payload, scan_data=scan_data)
    card_io._scan(card)
    renderer = card_io.load_renderer_module()
    if hasattr(renderer, "dump_card"):
        text = renderer.dump_card(card)
    else:
        text = json.dumps(card, ensure_ascii=False, indent=2) + "\n"
    encoded = text.encode("utf-8")
    output = card_io.workspace_file(workspace, args.output)
    if output.exists():
        if output.is_symlink():
            raise ValueError("refusing symlink")
        if output.read_bytes() != encoded:
            return {
                "ok": False,
                "error": "card_conflict",
                "hint": "existing card differs; left the file unchanged",
                "path": args.output,
            }
        return {"ok": True, "reused": True, "path": args.output, "card": card, "day_errors": day_errors}
    card_io.write_bytes_exclusive(output, encoded)
    return {"ok": True, "reused": False, "path": args.output, "card": card, "day_errors": day_errors}


def cmd_self_test(args: argparse.Namespace) -> dict[str, Any]:
    require_workspace(args.workspace_dir)
    modules = _locals()
    modules["edgar_client"].headline_self_check()
    modules["filing_card"].self_check()
    modules["cn_ndr"].self_check()
    modules["buyback_parse"].self_check()
    _bundled_text(_skill_dir() / "watchlist.txt")
    _bundled_text(_skill_dir() / "watchlist_cn_hk.txt")
    return {"ok": True, "message": "All checks passed"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="daily filing alert scan and card")
    parser.add_argument("--workspace-dir", default=None)
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--workspace-dir", default=argparse.SUPPRESS)
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    window = sub.add_parser("window", parents=[shared])
    window.add_argument("--as-of", default=None)
    window.set_defaults(func=cmd_window)

    scan = sub.add_parser("scan", parents=[shared])
    scan.add_argument("--tickers", default="")
    scan.add_argument("--days", type=int, default=3)
    scan.add_argument("--output", required=True)
    scan.add_argument("--no-enrich", action="store_true")
    scan.add_argument("--as-of", default=None)
    scan.set_defaults(func=cmd_scan)

    check = sub.add_parser("check", parents=[shared])
    check.add_argument("ticker")
    check.add_argument("--days", type=int, default=3)
    check.add_argument("--no-enrich", action="store_true")
    check.add_argument("--as-of", default=None)
    check.set_defaults(func=cmd_check)

    cn_hk = sub.add_parser("scan-cn-hk", parents=[shared])
    cn_hk.add_argument("--days", type=int, default=1)
    cn_hk.add_argument("--input", required=True)
    cn_hk.add_argument("--output", required=True)
    cn_hk.add_argument("--watchlist", default="")
    cn_hk.add_argument("--as-of", default=None)
    cn_hk.set_defaults(func=cmd_scan_cn_hk)

    render = sub.add_parser("render-card", parents=[shared])
    render.add_argument("--payload", required=True)
    render.add_argument("--cn-hk", default="")
    render.add_argument("--output", required=True)
    render.set_defaults(func=cmd_render)

    probe = sub.add_parser("self-test", parents=[shared])
    probe.set_defaults(func=cmd_self_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    _locals()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the CLI reports any failure as one JSON object
        payload = {"ok": False, "error": type(exc).__name__, "message": str(exc)[:400]}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(payload.get("message") or json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
