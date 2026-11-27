#!/usr/bin/env python3
import argparse
import datetime as dt
import io
import json
import math
import re
import tempfile
import time
from pathlib import Path

import fitz
import pytesseract
import requests
from PIL import Image
from pypdf import PdfReader

BASE = "https://www.cninfo.com.cn"
STATIC_BASE = "https://static.cninfo.com.cn"
SEARCH_URL = f"{BASE}/new/information/topSearch/query"
ANN_URL = f"{BASE}/new/hisAnnouncement/query"

REPORT_KEYWORDS = ["年度报告", "年报", "中期报告", "半年报", "第一季度报告", "第三季度报告", "季报", "业绩公告", "业绩快报"]

METRIC_PATTERNS = {
    "revenue": r"(营业收入|收入总额|Revenue)",
    "net_profit": r"(归属于.*净利润|净利润|Net\s*Profit)",
    "gross_profit": r"(毛利|毛利润|Gross\s*Profit)",
    "operating_profit": r"(营业利润|Operating\s*Profit)",
    "assets": r"(总资产|Assets\s*Total)",
    "liabilities": r"(总负债|Liabilities\s*Total)",
    "operating_cashflow": r"(经营活动产生的现金流量净额|经营现金流|Operating\s*Cash\s*Flow)",
}


def emit_json(payload):
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def error_hint(msg: str) -> str:
    m = (msg or "").lower()
    if "timeout" in m:
        return "请求超时。检查网络/代理后重试。"
    if "403" in m or "forbidden" in m:
        return "请求被拒绝。检查网络环境或稍后重试。"
    if "no announcements found" in m:
        return "该股票近期无公告，或股票代码不正确。"
    if "orgid" in m:
        return "未能解析 orgId。请确认 ticker 格式（A股6位/HK 5位）。"
    if "tesseract" in m:
        return "OCR 依赖缺失。请安装 tesseract（macOS: brew install tesseract tesseract-lang）。"
    if "index out of range" in m:
        return "索引超出范围，请先 list-filings 确认可用 index。"
    return "检查 ticker、网络与参数后重试。"


def normalize_ticker(ticker: str) -> str:
    t = str(ticker).strip()
    if t.isdigit() and len(t) <= 5:
        return t.zfill(5)
    return t


def resolve_market(stock_code: str):
    if len(stock_code) == 5 and stock_code.isdigit():
        return "hke"
    if stock_code.startswith(("6", "9")):
        return "sse"
    return "szse"


def classify_title(title: str) -> str:
    t = title or ""
    if any(k in t for k in ["年度报告", "年报"]):
        return "annual_report"
    if any(k in t for k in ["中期报告", "半年报"]):
        return "interim_report"
    if any(k in t for k in ["季度报告", "季报", "第一季度报告", "第三季度报告"]):
        return "quarter_report"
    if "业绩" in t:
        return "earnings"
    if "分红" in t or "利润分配" in t:
        return "dividend"
    return "other"


CNINFO_MAX_PAGE_SIZE = 30
DEFAULT_SINGLE_DAY_MAX_PAGES = 5
CNINFO_PAGE_TIMEOUT_S = 20.0
CNINFO_ORG_ID_TIMEOUT_S = 15.0
CNINFO_RETURN_MARGIN_S = 1.5
CNINFO_DEADLINE_MARGIN_S = CNINFO_RETURN_MARGIN_S


def _summarize_page_error(exc: Exception) -> str:
    name = type(exc).__name__
    msg = " ".join(str(exc).split())
    summary = f"{name}: {msg}" if msg else name
    return summary[:160]


def get_org_id(stock_code: str, headers: dict, timeout: float = CNINFO_ORG_ID_TIMEOUT_S) -> str:
    r = requests.post(SEARCH_URL, data={"keyWord": stock_code, "maxNum": 10}, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        return ""
    return str(data[0].get("orgId", "") or "")


def fetch_announcements(
    ticker: str,
    limit: int = 10,
    max_pages: int | None = None,
    date_from: str = "",
    date_to: str = "",
    start_page: int = 1,
    deadline_at: float | None = None,
    deadline_s: float | None = None,
    clock=None,
):
    stock_code = normalize_ticker(ticker)
    market = resolve_market(stock_code)
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    }

    now_fn = clock if clock is not None else (time.time if deadline_at is not None else time.monotonic)
    eff_deadline_at = None
    stop_margin_s = 0.0
    if deadline_at is not None and not isinstance(deadline_at, bool):
        cand_at = float(deadline_at)
        if math.isfinite(cand_at) and cand_at > 0:
            eff_deadline_at = cand_at
            stop_margin_s = CNINFO_RETURN_MARGIN_S
    elif deadline_s is not None and not isinstance(deadline_s, bool):
        eff_deadline_s = float(deadline_s)
        if math.isfinite(eff_deadline_s) and eff_deadline_s > 0:
            eff_deadline_at = now_fn() + eff_deadline_s
            stop_margin_s = min(CNINFO_RETURN_MARGIN_S, eff_deadline_s * 0.3)

    stop_at = (eff_deadline_at - stop_margin_s) if eff_deadline_at is not None else None

    org_id = ""
    try:
        if stop_at is None:
            org_id = get_org_id(stock_code, headers, timeout=CNINFO_ORG_ID_TIMEOUT_S)
        else:
            rem_for_org = stop_at - now_fn()
            if rem_for_org > 0:
                org_timeout = min(CNINFO_ORG_ID_TIMEOUT_S, min(5.0, rem_for_org * 0.4))
                org_id = get_org_id(stock_code, headers, timeout=org_timeout)
    except Exception:
        org_id = ""

    is_single_day = bool(date_from and date_to and date_from.strip() == date_to.strip())
    start_page = max(1, int(start_page or 1))
    if max_pages is None:
        page_budget = DEFAULT_SINGLE_DAY_MAX_PAGES if is_single_day else 1
    else:
        page_budget = max(1, int(max_pages))
    page_size = CNINFO_MAX_PAGE_SIZE if is_single_day else min(max(10, int(limit)), CNINFO_MAX_PAGE_SIZE)
    wanted = max(1, int(limit))
    end_page = start_page + page_budget - 1

    rows = []
    seen = set()
    pages_fetched = 0
    last_page = start_page - 1
    total_count = None
    has_more = False
    next_page = None
    page_error = None
    deadline_reached = False

    for page in range(start_page, end_page + 1):
        if stop_at is not None:
            remaining = stop_at - now_fn()
            if remaining <= 0:
                has_more = True
                next_page = page
                deadline_reached = True
                if pages_fetched == 0:
                    page_error = "deadline_exceeded"
                break
            req_timeout = min(CNINFO_PAGE_TIMEOUT_S, remaining)
        else:
            req_timeout = CNINFO_PAGE_TIMEOUT_S

        payload = {
            "pageNum": page,
            "pageSize": page_size,
            "column": market,
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{stock_code},{org_id}" if org_id else stock_code,
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{date_from}~{date_to}" if date_from or date_to else "",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }

        try:
            r = requests.post(ANN_URL, data=payload, headers=headers, timeout=req_timeout)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError(f"invalid_json_type:{type(data).__name__}")
        except (requests.RequestException, ValueError) as exc:
            has_more = True
            next_page = page
            page_error = _summarize_page_error(exc)
            break

        pages_fetched += 1
        last_page = page
        raw_has_more = data.get("hasMore")
        if isinstance(raw_has_more, bool):
            api_has_more = raw_has_more
        elif isinstance(raw_has_more, str) and raw_has_more.strip().lower() in ("true", "false"):
            api_has_more = raw_has_more.strip().lower() == "true"
        else:
            api_has_more = None

        raw_total = data.get("totalAnnouncement")
        if raw_total is None:
            raw_total = data.get("totalRecordNum")
        if isinstance(raw_total, int) and not isinstance(raw_total, bool) and raw_total >= 0:
            total_count = raw_total
        elif isinstance(raw_total, str) and raw_total.strip().isdigit():
            total_count = int(raw_total.strip())

        items = data.get("announcements", []) or []
        if not items:
            has_more = False
            next_page = None
            break

        stopped_for_limit = False
        for idx, item in enumerate(items):
            title = str(item.get("announcementTitle", "")).replace("<em>", "").replace("</em>", "")
            ts = float(item.get("announcementTime", 0) or 0) / 1000
            date_str = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            adjunct_url = str(item.get("adjunctUrl", ""))
            key = (date_str, title, adjunct_url)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "ticker": stock_code,
                "date": date_str,
                "title": title,
                "category": classify_title(title),
                "pdf_link": f"{STATIC_BASE}/{adjunct_url}" if adjunct_url else "",
                "source": "cninfo",
            })
            # Single-day queries crawl complete pages up to page_budget so all filings on
            # that day are collected (or cleanly resumable by page number when budget runs out).
            if not is_single_day and len(rows) >= wanted:
                stopped_for_limit = True
                consumed_pos = (page - 1) * page_size + idx + 1
                if (
                    idx < len(items) - 1
                    or api_has_more is True
                    or (total_count is not None and total_count > consumed_pos)
                    or (api_has_more is None and len(items) >= page_size)
                ):
                    has_more = True
                    next_page = page if idx < len(items) - 1 else page + 1
                break
        if stopped_for_limit:
            break

        # Detect whether upstream pagination actually completed or was cut off by page_budget.
        reached_end = (
            api_has_more is False
            or (total_count is not None and page * page_size >= total_count)
            or (len(items) < page_size and api_has_more is not True)
        )
        if reached_end:
            has_more = False
            next_page = None
            break
        if page == end_page:
            has_more = (
                api_has_more is True
                or (total_count is not None and total_count > page * page_size)
                or len(items) >= page_size
            )
            next_page = (page + 1) if has_more else None

    for i, r in enumerate(rows, start=1):
        r["index"] = i

    result = {
        "ok": True,
        "ticker": stock_code,
        "market": market,
        "count": len(rows),
        "rows": rows,
        "has_more": bool(has_more),
        "truncated": bool(has_more),
        "pagination_complete": not bool(has_more),
        "page_size": page_size,
        "pages_fetched": pages_fetched,
        "start_page": start_page,
        "last_page": last_page,
        "max_pages": page_budget,
        "next_page": next_page if has_more else None,
    }
    if total_count is not None:
        result["total_count"] = total_count
    if page_error is not None:
        result["page_error"] = page_error
    if deadline_reached:
        result["deadline_reached"] = True
    return result


def pick_announcement(rows, index):
    if not rows:
        raise ValueError("No announcements found")
    if index < 1 or index > len(rows):
        raise ValueError(f"Index out of range: {index}, valid range is 1..{len(rows)}")
    return rows[index - 1]


def pick_announcement_by_keyword(rows, keyword: str):
    kw = (keyword or "").strip().lower()
    if not kw:
        raise ValueError("Keyword is empty")
    for r in rows:
        if kw in str(r.get("title", "")).lower():
            return r
    raise ValueError(f"No announcement title matched keyword: {keyword}")


def pick_latest_financial_report(rows):
    for r in rows:
        if any(k in str(r.get("title", "")) for k in REPORT_KEYWORDS):
            return r
    raise ValueError("No financial report announcement found in current list")


def download_pdf(pdf_url: str) -> Path:
    if not pdf_url:
        raise ValueError("Missing PDF link")
    r = requests.get(pdf_url, timeout=30)
    r.raise_for_status()
    fd, path = tempfile.mkstemp(suffix=".pdf", prefix="cnhk_")
    with open(fd, "wb", closefd=False) as f:
        f.write(r.content)
    return Path(path)


def extract_pdf_text(pdf_path: Path, max_chars: int = 12000) -> str:
    reader = PdfReader(str(pdf_path))
    parts = []
    for page in reader.pages:
        txt = page.extract_text() or ""
        if txt:
            parts.append(txt)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


def ocr_pdf_text(pdf_path: Path, max_chars: int = 12000, max_pages: int = 10, lang: str = "chi_sim+eng") -> str:
    doc = fitz.open(str(pdf_path))
    parts = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        txt = pytesseract.image_to_string(img, lang=lang) or ""
        if txt:
            parts.append(txt)
        if sum(len(p) for p in parts) >= max_chars:
            break
    return "\n".join(parts)[:max_chars]


def is_text_garbled(text: str) -> bool:
    if not text:
        return True
    printable = sum(1 for c in text if c.isprintable())
    return printable / max(1, len(text)) < 0.65


def load_report_text(chosen, max_chars=12000, ocr_fallback=True):
    pdf_path = download_pdf(chosen["pdf_link"])
    text = extract_pdf_text(pdf_path, max_chars=max_chars)
    garbled = is_text_garbled(text)
    ocr_used = False
    ocr_error = None

    if garbled and ocr_fallback:
        try:
            ocr_text = ocr_pdf_text(pdf_path, max_chars=max_chars)
            if ocr_text:
                text = ocr_text
            ocr_used = True
            garbled = is_text_garbled(text)
        except Exception as e:
            ocr_error = str(e)

    return text, garbled, ocr_used, ocr_error


def extract_value_near(text: str, keyword_pattern: str):
    pattern = re.compile(rf"(?:{keyword_pattern})[\s\S]{{0,60}}?([\-]?[0-9]{{1,3}}(?:,[0-9]{{3}})*(?:\.[0-9]+)?(?:亿|万)?|[\-]?[0-9]+(?:\.[0-9]+)?(?:亿|万)?)", re.IGNORECASE)
    m = pattern.search(text)
    return m.group(m.lastindex) if m else None


def extract_financial_metrics(text: str):
    out = {}
    for k, pat in METRIC_PATTERNS.items():
        out[k] = extract_value_near(text, pat)
    return out


def search_text(text: str, keyword: str, window: int = 200, max_matches: int = 5):
    low = text.lower()
    kw = keyword.lower()
    start = 0
    matches = []
    while len(matches) < max_matches:
        idx = low.find(kw, start)
        if idx == -1:
            break
        s = max(0, idx - window)
        e = min(len(text), idx + len(keyword) + window)
        snippet = " ".join(text[s:e].split())
        matches.append(snippet)
        start = idx + len(keyword)
    return matches


def build_facts_series(ticker: str, metric: str, limit: int, ocr_fallback: bool, max_pages: int, date_from: str, date_to: str):
    ann = fetch_announcements(ticker, limit=max(60, limit * 6), max_pages=max_pages, date_from=date_from, date_to=date_to)
    rows = ann["rows"]
    report_rows = [r for r in rows if r["category"] in {"annual_report", "interim_report", "quarter_report", "earnings"}]

    series = []
    for r in report_rows[:limit]:
        try:
            text, garbled, ocr_used, ocr_error = load_report_text(r, max_chars=10000, ocr_fallback=ocr_fallback)
            metrics = extract_financial_metrics(text)
            series.append({
                "period_end": r.get("date"),
                "metric": metric,
                "value": metrics.get(metric),
                "ticker": ticker,
                "title": r.get("title"),
                "source_link": r.get("pdf_link"),
                "text_garbled": garbled,
                "ocr_used": ocr_used,
                "ocr_error": ocr_error,
            })
        except Exception as e:
            series.append({
                "period_end": r.get("date"),
                "metric": metric,
                "value": None,
                "ticker": ticker,
                "title": r.get("title"),
                "source_link": r.get("pdf_link"),
                "error": str(e),
                "hint": error_hint(str(e)),
            })

    return {"ok": True, "ticker": ticker, "metric": metric, "rows": series}


def self_test(ticker: str = "00700"):
    checks = []
    try:
        out = fetch_announcements(ticker=ticker, limit=1)
        conn_ok = bool(out.get("ok")) and not out.get("page_error")
        check = {"name": "api.connectivity", "ok": conn_ok, "count": out.get("count", 0)}
        if out.get("page_error"):
            check["error"] = out["page_error"]
            check["hint"] = error_hint(str(out["page_error"]))
        checks.append(check)
    except Exception as e:
        checks.append({"name": "api.connectivity", "ok": False, "error": str(e), "hint": error_hint(str(e))})

    try:
        _ = str(pytesseract.get_tesseract_version())
        checks.append({"name": "ocr.tesseract", "ok": True})
    except Exception as e:
        checks.append({"name": "ocr.tesseract", "ok": False, "error": str(e), "hint": error_hint(str(e))})

    return {"ok": all(c.get("ok") for c in checks), "checks": checks}


def print_human_ann(data):
    rows = data.get("rows", [])
    if not rows:
        print(f"No announcements found for {data.get('ticker')}.")
        return
    print("=" * 80)
    print(f"Latest {len(rows)} Filings for {data.get('ticker')}")
    print("=" * 80)
    for r in rows:
        print(f"[{r['index']}] [{r['date']}] [{r['category']}] {r['title']}")
        print(f"Link: {r['pdf_link']}")
        print("-" * 80)


def choose_report(args, ann_rows):
    if args.report_index:
        return pick_announcement(ann_rows, args.report_index)
    if args.report_keyword:
        return pick_announcement_by_keyword(ann_rows, args.report_keyword)
    return pick_latest_financial_report(ann_rows)


def main():
    parser = argparse.ArgumentParser(description="CN/HK filing fetcher + report extractor")
    parser.add_argument("--ticker", type=str, help="Stock ticker")
    parser.add_argument("--limit", type=int, default=10, help="Number of filings to return")
    parser.add_argument("--max-pages", type=int, default=None, help="How many pages to crawl from Cninfo (default: 5 for single-day, 1 otherwise)")
    parser.add_argument("--start-page", type=int, default=1, help="1-based start page for Cninfo pagination")
    parser.add_argument("--deadline-at", type=float, default=None, help="Absolute Unix timestamp (seconds) cutoff for paginated announcement fetch")
    parser.add_argument("--deadline-s", type=float, default=None, help="Total time budget in seconds for paginated announcement fetch (legacy compat)")
    parser.add_argument("--date-from", type=str, default="", help="Start date YYYY-MM-DD (optional)")
    parser.add_argument("--date-to", type=str, default="", help="End date YYYY-MM-DD (optional)")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--self-test", action="store_true", help="Run health checks")

    # findata-analyst-like commands
    parser.add_argument("--list-filings", action="store_true", help="List recent filings")
    parser.add_argument("--financials", action="store_true", help="Extract key financial metrics from selected report")
    parser.add_argument("--search-text", type=str, help="Search keyword within selected report text")
    parser.add_argument("--facts-metric", choices=list(METRIC_PATTERNS.keys()), help="Build time series for selected metric")
    parser.add_argument("--facts-limit", type=int, default=5, help="Number of report points for facts series")

    # report selection
    parser.add_argument("--report-index", type=int, help="Use Nth filing as selected report")
    parser.add_argument("--report-keyword", type=str, help="Select first filing title matching keyword")

    # legacy compat
    parser.add_argument("--read-report-index", type=int)
    parser.add_argument("--extract-financials-index", type=int)
    parser.add_argument("--read-report-keyword", type=str)
    parser.add_argument("--extract-financials-keyword", type=str)
    parser.add_argument("--extract-financials-latest", action="store_true")

    parser.add_argument("--max-chars", type=int, default=12000)
    parser.add_argument("--ocr-fallback", choices=["true", "false"], default="true")
    parser.add_argument("--window", type=int, default=200)
    parser.add_argument("--max-matches", type=int, default=5)

    args = parser.parse_args()
    try:
        if args.self_test:
            out = self_test(ticker=args.ticker or "00700")
            emit_json(out) if args.json else print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        if not args.ticker:
            parser.error("--ticker is required unless --self-test is used")

        # map legacy switches into unified selection/commands
        if args.read_report_index and not args.report_index:
            args.report_index = args.read_report_index
        if args.read_report_keyword and not args.report_keyword:
            args.report_keyword = args.read_report_keyword
        if args.extract_financials_index and not args.report_index:
            args.report_index = args.extract_financials_index
        if args.extract_financials_keyword and not args.report_keyword:
            args.report_keyword = args.extract_financials_keyword
        if args.extract_financials_latest:
            args.financials = True

        ann = fetch_announcements(
            args.ticker,
            args.limit,
            max_pages=args.max_pages,
            date_from=args.date_from,
            date_to=args.date_to,
            start_page=args.start_page,
            deadline_at=args.deadline_at,
            deadline_s=args.deadline_s,
        )

        if args.facts_metric:
            out = build_facts_series(args.ticker, args.facts_metric, args.facts_limit, args.ocr_fallback == "true", args.max_pages, args.date_from, args.date_to)
            emit_json(out) if args.json else print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        if args.financials or args.search_text:
            chosen = choose_report(args, ann["rows"])
            text, garbled, ocr_used, ocr_error = load_report_text(chosen, max_chars=args.max_chars, ocr_fallback=args.ocr_fallback == "true")

            if args.search_text:
                matches = search_text(text, args.search_text, window=args.window, max_matches=args.max_matches)
                out = {
                    "ok": True,
                    "mode": "search-text",
                    "announcement": chosen,
                    "keyword": args.search_text,
                    "matches": matches,
                    "text_garbled": garbled,
                    "ocr_used": ocr_used,
                    "ocr_error": ocr_error,
                }
                emit_json(out) if args.json else print(json.dumps(out, ensure_ascii=False, indent=2))
                return

            metrics = extract_financial_metrics(text)
            out = {
                "ok": True,
                "mode": "financials",
                "announcement": chosen,
                "metrics": metrics,
                "text_garbled": garbled,
                "ocr_used": ocr_used,
                "ocr_error": ocr_error,
                "note": "Values are regex-extracted candidates; verify against source report context.",
            }
            emit_json(out) if args.json else print(json.dumps(out, ensure_ascii=False, indent=2))
            return

        # default/list-filings
        if args.list_filings or True:
            emit_json(ann) if args.json else print_human_ann(ann)
            return

    except Exception as e:
        payload = {"ok": False, "error": str(e), "hint": error_hint(str(e))}
        emit_json(payload) if args.json else print(f"Error: {payload['error']}\nHint: {payload['hint']}")


if __name__ == "__main__":
    main()
