#!/usr/bin/env python3
"""company-filings-alert CLI — Monitor SEC filings for a watchlist of companies.

Checks for new filings (8-K, 6-K, Form 4, 144, 10-Q, 10-K, 20-F, etc.)
since a given lookback window. Designed to run daily via cron.
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

__version__ = "1.4.0"

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import edgar_enrich
except ImportError:  # enrichment is optional; the scan still reports form/date
    edgar_enrich = None

SKILL_DIR = Path(__file__).resolve().parent.parent


def _resolve_research_roots(default_code_root: Path) -> tuple[Path, Path, bool]:
    raw_code = os.environ.get("DALTON_RESEARCH_CODE", "").strip()
    raw_home = os.environ.get("DALTON_RESEARCH_HOME", "").strip()
    raw_ocw = os.environ.get("DALTON_OCW_ROOT", "").strip()
    # Legacy compat: when new vars are unset and DALTON_OCW_ROOT is set, keep old single-root layout.
    if raw_ocw and not raw_code and not raw_home:
        ocw = Path(raw_ocw).expanduser().resolve()
        return ocw, ocw, True
    code_root = Path(raw_code).expanduser().resolve() if raw_code else default_code_root.resolve()
    home_root = (
        Path(raw_home).expanduser().resolve()
        if raw_home
        else (Path.home() / "Library" / "Application Support" / "Everflow" / "research")
    )
    return code_root, home_root, False


CODE_ROOT, DATA_ROOT, LEGACY_MODE = _resolve_research_roots(SKILL_DIR.parent.parent)
FINDATA_SKILL = CODE_ROOT / "skills" / "findata-analyst"
CN_HK_SKILL = CODE_ROOT / "skills" / "cn-hk-findata"
STATE_DIR = (
    (DATA_ROOT / "skills" / "company-filings-alert" / "state")
    if LEGACY_MODE
    else (DATA_ROOT / "state" / "company-filings-alert")
)
STATE_FILE = STATE_DIR / "last_check.json"
# Structured dump of detected filings for downstream consumers (ai-chain event
# ingestion). Distinct from last_check.json, which only holds per-ticker bookmarks.
FILINGS_DUMP = STATE_DIR / "filings.json"

# Filing types to monitor, grouped by category
FILING_CATEGORIES = {
    "earnings": ["8-K", "6-K"],          # Earnings releases, material events
    "insider": ["4"],                      # Form 4 insider trading
    "annual_quarterly": ["10-K", "10-Q", "20-F"],  # Annual/quarterly reports
    "proxy": ["DEF 14A", "DEFA14A"],      # Proxy statements
    "offering": ["S-1", "F-1", "424B"],   # Offerings/IPO
    "ownership": ["SC 13D", "SC 13G", "SCHEDULE 13D", "SCHEDULE 13G"],  # Large ownership
    "other": ["144"],                      # Form 144 (intent to sell restricted)
}

ALL_FORM_TYPES = []
for forms in FILING_CATEGORIES.values():
    ALL_FORM_TYPES.extend(forms)


def load_state() -> dict:
    """Load last check timestamps per ticker."""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_state(state: dict):
    """Save last check timestamps."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def save_filings_dump(all_results: list, scan_time: str) -> int:
    """Persist a flat, structured list of detected filings for downstream
    consumers (ai-chain ingest_filings). Keep the enrichment fields too so a
    cron can recover the announcement substance from this file if its captured
    command output is unavailable. Written every scan (empty list when nothing
    new) so the consumer always sees a valid file."""
    flat = []
    for result in all_results:
        ticker = result.get("ticker")
        for f in result.get("new_filings", []):
            flat.append({
                "ticker": ticker,
                "form": f.get("type"),
                "date": f.get("date"),
                "accession": f.get("accession"),
                "category": f.get("category"),
                "url": f.get("url"),
                "headline": f.get("headline") or (f.get("parsed") or {}).get("summary", ""),
                "item_labels": f.get("item_labels", []),
                "body_excerpt": f.get("body_excerpt", ""),
                "cover_summary": f.get("cover_summary", ""),
                "report_date": f.get("report_date", ""),
                "enrich_error": f.get("enrich_error", ""),
            })
    FILINGS_DUMP.parent.mkdir(parents=True, exist_ok=True)
    FILINGS_DUMP.write_text(json.dumps(
        {"scan_time": scan_time, "filings": flat},
        indent=2, ensure_ascii=False))
    return len(flat)


def run_findata(command: str, args: list) -> dict:
    """Run findata-analyst CLI and return parsed JSON."""
    run_sh = FINDATA_SKILL / "run.sh"
    if not run_sh.exists():
        return {"ok": False, "error": f"findata-analyst not found at {FINDATA_SKILL}"}

    cmd = ["bash", str(run_sh), "--json", command] + args
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            env={**os.environ, "SKILL_WARNINGS": "ignore"}
        )
        # Find JSON in output (skip stderr bootstrap messages)
        stdout = result.stdout.strip()
        # Try to find JSON object in output
        for line in stdout.split("\n"):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        # Try full output
        if stdout.startswith("{"):
            return json.loads(stdout)
        return {"ok": False, "error": f"No JSON in output", "raw": stdout[:500]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timeout calling findata-analyst"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


import re


def _parse_json_output(stdout: str) -> dict:
    """Extract first JSON object from potentially multi-line CLI output."""
    if not stdout:
        return {}
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return {}


def parse_insider_detail(content: str) -> dict:
    """Parse Form 4 content to extract insider name, position, transaction summary with amounts."""
    result = {"insider": "", "position": "", "transactions": [], "net_change": "", "avg_sell_price": ""}

    if not content:
        return result

    # Extract insider name
    m = re.search(r"Insider:\s+(.+?)(?:\s{2,}|\n|│)", content)
    if m:
        result["insider"] = m.group(1).strip()

    # Extract position
    m = re.search(r"Position:\s+(.+?)(?:\s{2,}|\n|│)", content)
    if m:
        result["position"] = m.group(1).strip()

    # Extract transaction type from header
    header_match = re.search(r"Ownership Transactions \((.+?)\)", content)
    result["tx_type"] = header_match.group(1) if header_match else ""

    # Extract individual transactions: SALE, PURCHASE, EXERCISE, AWARD, GIFT etc.
    # Pattern: TYPE  CODE  Description  Shares  Price/Share  Value
    tx_pattern = re.compile(
        r"(SALE|PURCHASE|EXERCISE|AWARD|GIFT|DISPOSE)\s+"
        r"([A-Z])\s+"
        r"(.+?)\s+"
        r"([\d,]+)\s+"
        r"\$?([\d,.]+)\s+"
        r"\$?([\d,.]+(?:\.\d+)?(?:…)?)"
    )
    for m in tx_pattern.finditer(content):
        tx = {
            "type": m.group(1),
            "code": m.group(2),
            "desc": m.group(3).strip(),
            "shares": m.group(4).replace(",", ""),
            "price": m.group(5).replace(",", ""),
            "value": m.group(6).replace(",", "").rstrip("…"),
        }
        result["transactions"].append(tx)

    # Extract net change
    m = re.search(r"NET CHANGE\s+(-?[\d,]+)\s+\$?(-?[\d,.]+(?:…)?)", content)
    if m:
        result["net_shares"] = m.group(1).replace(",", "")
        result["net_value"] = m.group(2).replace(",", "").rstrip("…")

    # Extract avg sell price
    m = re.search(r"AVG SELL\s+\$?([\d,.]+)", content)
    if m:
        result["avg_sell_price"] = m.group(1).replace(",", "")

    # Build summary
    sales = [t for t in result["transactions"] if t["type"] == "SALE"]
    purchases = [t for t in result["transactions"] if t["type"] == "PURCHASE"]
    exercises = [t for t in result["transactions"] if t["type"] == "EXERCISE"]
    awards = [t for t in result["transactions"] if t["type"] == "AWARD"]

    parts = []
    if sales:
        total_shares = sum(int(t["shares"]) for t in sales)
        total_value = sum(float(t["value"]) for t in sales if t["value"])
        parts.append(f"卖出 {total_shares:,} 股，约 ${total_value:,.0f}")
    if purchases:
        total_shares = sum(int(t["shares"]) for t in purchases)
        total_value = sum(float(t["value"]) for t in purchases if t["value"])
        parts.append(f"买入 {total_shares:,} 股，约 ${total_value:,.0f}")
    if exercises:
        total_shares = sum(int(t["shares"]) for t in exercises)
        total_value = sum(float(t["value"]) for t in exercises if t["value"])
        parts.append(f"行权 {total_shares:,} 股（价值 ${total_value:,.0f}）")
    if awards:
        total_shares = sum(int(t["shares"]) for t in awards)
        total_value = sum(float(t["value"]) for t in awards if t["value"])
        if total_value > 0:
            parts.append(f"获授 {total_shares:,} 股（价值 ${total_value:,.0f}）")
        else:
            parts.append(f"获授 {total_shares:,} 股")

    result["summary"] = "；".join(parts) if parts else result["tx_type"]

    return result


def enrich_filings(ticker: str, filings: list) -> None:
    """Attach headline / item labels / body excerpt to each filing in place.

    Without this the push has nothing but a form type and an accession number,
    which is why entries used to read "未取得正文". Failures are recorded per
    filing and never abort a scan.
    """
    if not edgar_enrich:
        return
    for f in filings:
        try:
            extra = edgar_enrich.enrich_filing(ticker, f.get("type", ""), f.get("accession", ""))
        except Exception as e:
            extra = {"enrich_error": f"{type(e).__name__}: {e}"}
        if not extra:
            continue
        # Form 4 already carries a parsed summary from findata-analyst.
        if f.get("type") == "Form 4":
            f.setdefault("url", extra.get("url", ""))
            continue
        for key in ("url", "doc_url", "headline", "item_labels", "item_titles",
                    "body_excerpt", "cover_summary", "report_date",
                    "reporting_persons", "percent_of_class", "shares",
                    "amendment_no", "event_date", "seller", "position",
                    "market_value", "sale_date", "nature", "enrich_error"):
            if extra.get(key) not in (None, "", [], {}):
                f[key] = extra[key]


def check_filings(ticker: str, days: int = 3, form_types: list = None,
                  enrich: bool = True) -> dict:
    """Check for new filings for a ticker within the lookback window."""
    if form_types is None:
        form_types = ALL_FORM_TYPES

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    results = {"ticker": ticker, "new_filings": [], "errors": []}

    # Check general filings (8-K, 6-K, 10-Q, 10-K, 20-F, etc.)
    general_forms = [f for f in form_types if f != "4"]
    if general_forms:
        for form in general_forms:
            data = run_findata("list-filings", [
                "--ticker", ticker, "--form", form, "--limit", "5"
            ])
            if data.get("ok") and data.get("rows"):
                for row in data["rows"]:
                    filing_date = row.get("filing_date", "")
                    if filing_date >= cutoff:
                        filing_entry = {
                            "type": row.get("form", form),
                            "date": filing_date,
                            "accession": row.get("accession_no", ""),
                            "category": categorize_filing(row.get("form", form)),
                        }
                        results["new_filings"].append(filing_entry)

    # Check insider trading (Form 4) separately — uses dedicated command
    if "4" in form_types:
        data = run_findata("insider", ["--ticker", ticker, "--limit", "5"])
        if data.get("ok") and data.get("rows"):
            for row in data["rows"]:
                filing_date = row.get("filing_date", "")
                if filing_date >= cutoff:
                    content = row.get("content", "")
                    parsed = parse_insider_detail(content)
                    results["new_filings"].append({
                        "type": "Form 4",
                        "date": filing_date,
                        "accession": row.get("accession_no", ""),
                        "category": "insider",
                        "detail": content[:300],
                        "parsed": parsed,
                    })

    # Deduplicate by accession number
    seen = set()
    unique = []
    for f in results["new_filings"]:
        acc = f.get("accession", "")
        if acc and acc not in seen:
            seen.add(acc)
            unique.append(f)
        elif not acc:
            unique.append(f)
    results["new_filings"] = sorted(unique, key=lambda x: x["date"], reverse=True)

    if enrich:
        enrich_filings(ticker, results["new_filings"])

    results["count"] = len(results["new_filings"])
    results["cutoff"] = cutoff

    return results


def categorize_filing(form: str) -> str:
    """Map a form type to its category."""
    form_upper = form.upper().strip()
    for cat, forms in FILING_CATEGORIES.items():
        if any(form_upper.startswith(f.upper()) for f in forms):
            return cat
    return "other"


def format_filing_alert(ticker: str, filing: dict) -> str:
    """Format a single filing into readable alert text."""
    category_emoji = {
        "earnings": "📊",
        "insider": "👤",
        "annual_quarterly": "📋",
        "proxy": "🗳️",
        "offering": "💰",
        "ownership": "🏛️",
        "other": "📄",
    }
    cat = filing.get("category", "other")
    emoji = category_emoji.get(cat, "📄")
    line = f"{emoji} **{ticker}** — {filing['type']} ({filing['date']})"

    if filing.get("headline"):
        line += f"\n  → {filing['headline'][:200]}"
    if filing.get("detail"):
        # Clean up insider detail for display
        detail = filing["detail"].replace("\n", " ").strip()
        # Extract key info from insider content
        if "Insider:" in detail:
            parts = detail.split("│")
            for p in parts:
                p = p.strip()
                if p.startswith("Insider:"):
                    line += f"\n  → {p.strip()}"
                elif "SALE" in p.upper() or "PURCHASE" in p.upper() or "AWARD" in p.upper():
                    line += f"\n  → {p.strip()}"
    return line


def check_cn_announcements(stock_code: str, stock_name: str, days: int = 1) -> dict:
    """Check A-share announcements via AkShare (eastmoney backend)."""
    result = {"ticker": stock_code, "name": stock_name, "market": "A", "announcements": [], "errors": []}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        # Use cn-hk-findata venv's AkShare
        cn_hk_venv = (
            (DATA_ROOT / "skills" / "cn-hk-findata" / "venv")
            if LEGACY_MODE
            else (DATA_ROOT / "venvs" / "cn-hk-findata")
        )
        venv_python = cn_hk_venv / "bin" / "python3"
        if not venv_python.exists():
            venv_python = cn_hk_venv / "bin" / "python"

        # Scan each day in the window
        announcements = []
        for d in range(days):
            date = (datetime.now() - timedelta(days=d)).strftime("%Y%m%d")
            cmd = [
                str(venv_python), "-c",
                f"""
import akshare as ak
import json
try:
    df = ak.stock_notice_report(symbol='全部', date='{date}')
    filtered = df[df['代码'] == '{stock_code}']
    rows = []
    for _, row in filtered.iterrows():
        rows.append({{
            "code": row.get("代码",""),
            "name": row.get("名称",""),
            "title": row.get("公告标题",""),
            "type": row.get("公告类型",""),
            "date": str(row.get("公告日期","")),
            "url": row.get("网址",""),
        }})
    print(json.dumps({{"ok":True,"rows":rows}}, ensure_ascii=False))
except Exception as e:
    print(json.dumps({{"ok":False,"error":str(e)}}, ensure_ascii=False))
"""
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            stdout = proc.stdout.strip()
            for line in stdout.split("\n"):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        if data.get("ok") and data.get("rows"):
                            announcements.extend(data["rows"])
                        break
                    except json.JSONDecodeError:
                        continue

        # Deduplicate by title+date
        seen = set()
        for a in announcements:
            key = f"{a['title']}|{a['date']}"
            if key not in seen:
                seen.add(key)
                result["announcements"].append(a)

        result["count"] = len(result["announcements"])
        result["cutoff"] = cutoff

    except subprocess.TimeoutExpired:
        result["errors"].append("Timeout fetching A-share announcements")
        result["count"] = 0
    except Exception as e:
        result["errors"].append(str(e))
        result["count"] = 0

    return result


def check_hk_announcements(stock_code: str, stock_name: str, days: int = 1) -> dict:
    """Return a Gemini grounded-search request for official HKEX announcements.

    HKEX pages are sometimes unavailable to this host. The cron agent executes
    the returned request through OpenClaw web_search, which can ground against
    Google Search while enforcing an explicit date range.
    """
    padded = stock_code.replace(".HK", "").replace("HK", "").zfill(5)
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    date_before = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    query = (
        f'site:hkexnews.hk "{stock_name}" "{padded}" '
        '(announcement OR results OR dividend OR buyback OR disclosure OR notice OR 公告 OR 业绩 OR 回购 OR 股息)'
    )
    return {
        "ticker": stock_code,
        "name": stock_name,
        "market": "HK",
        "announcements": [],
        "errors": [],
        "count": 0,
        "cutoff": cutoff,
        "web_search_required": True,
        "web_search_query": query,
        "date_after": cutoff,
        "date_before": date_before,
        "accepted_sources": ["hkexnews.hk", "company official IR"],
    }


def parse_cn_hk_watchlist(watchlist_path: Path = None) -> tuple:
    """Parse CN/HK watchlist. Returns (cn_stocks, hk_stocks) as lists of (code, name) tuples."""
    if watchlist_path is None:
        watchlist_path = SKILL_DIR / "watchlist_cn_hk.txt"
    if not watchlist_path.exists():
        return [], []

    cn_stocks = []
    hk_stocks = []
    for line in watchlist_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: CODE NAME (e.g., "300308 中际旭创" or "9992.HK 泡泡玛特")
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        code, name = parts[0], parts[1]
        if ".HK" in code.upper() or code.startswith("0") and len(code) == 5:
            hk_stocks.append((code.replace(".HK", "").replace(".hk", ""), name))
        else:
            cn_stocks.append((code, name))

    return cn_stocks, hk_stocks


def cmd_scan_cn_hk(args):
    """Scan CN/HK watchlist for new announcements."""
    cn_stocks, hk_stocks = parse_cn_hk_watchlist()

    if not cn_stocks and not hk_stocks:
        error = {"ok": False, "error": "No CN/HK stocks in watchlist",
                 "hint": "Create skills/company-filings-alert/watchlist_cn_hk.txt"}
        if args.json:
            print(json.dumps(error, ensure_ascii=False))
        else:
            print(f"ERROR: {error['error']}")
        sys.exit(1)

    days = args.days
    all_results = []
    alert_count = 0

    # Scan A-share stocks
    for code, name in cn_stocks:
        result = check_cn_announcements(code, name, days)
        all_results.append(result)
        if result.get("count", 0) > 0:
            alert_count += 1

    # Scan HK stocks
    for code, name in hk_stocks:
        result = check_hk_announcements(code, name, days)
        all_results.append(result)
        if result.get("count", 0) > 0:
            alert_count += 1

    now = datetime.now().isoformat()

    if args.json:
        output = {
            "ok": True,
            "scan_time": now,
            "lookback_days": days,
            "cn_stocks_scanned": len(cn_stocks),
            "hk_stocks_scanned": len(hk_stocks),
            "stocks_with_alerts": alert_count,
            "results": all_results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        total = len(cn_stocks) + len(hk_stocks)
        if alert_count == 0:
            print(f"✅ No new announcements in the past {days} day(s) for {total} CN/HK stocks.")
        else:
            print(f"🔔 New announcements for {alert_count}/{total} stocks:\n")
            for result in all_results:
                if result.get("count", 0) > 0:
                    print(f"**{result['ticker']}** {result['name']}")
                    for ann in result["announcements"]:
                        print(f"  {ann['type']} | {ann['title']} | {ann['date']}")
                    print()


def cmd_scan(args):
    """Scan all watchlist tickers for new filings."""
    tickers = parse_tickers(args.tickers)
    if not tickers:
        error = {"ok": False, "error": "No tickers provided",
                 "hint": "Use --tickers AAPL,NVDA,TCOM or pass a file path"}
        if args.json:
            print(json.dumps(error, ensure_ascii=False))
        else:
            print(f"ERROR: {error['error']}\n{error['hint']}")
        sys.exit(1)

    days = args.days
    all_results = []
    alert_tickers = []

    # Parallel scan with ThreadPoolExecutor (5 workers to avoid SEC rate limit)
    max_workers = min(5, len(tickers))
    results_map = {}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_ticker = {
            executor.submit(check_filings, ticker, days, None, not args.no_enrich): ticker
            for ticker in tickers
        }
        for future in as_completed(future_to_ticker):
            ticker = future_to_ticker[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"ticker": ticker, "new_filings": [], "errors": [str(e)], "count": 0, "cutoff": ""}
            results_map[ticker] = result

    # Maintain original order
    for ticker in tickers:
        result = results_map[ticker]
        all_results.append(result)
        if result["count"] > 0:
            alert_tickers.append(ticker)

    if edgar_enrich and not args.no_enrich:
        edgar_enrich.save_enrich_cache()

    # Update state
    state = load_state()
    now = datetime.now().isoformat()
    for t in tickers:
        state[t] = now
    save_state(state)
    save_filings_dump(all_results, now)

    if args.json:
        output = {
            "ok": True,
            "scan_time": now,
            "lookback_days": days,
            "tickers_scanned": len(tickers),
            "tickers_with_alerts": len(alert_tickers),
            "results": all_results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        if not alert_tickers:
            print(f"✅ No new filings in the past {days} day(s) for {len(tickers)} tickers.")
        else:
            print(f"🔔 New filings detected for {len(alert_tickers)}/{len(tickers)} tickers:\n")
            for result in all_results:
                if result["count"] > 0:
                    for filing in result["new_filings"]:
                        print(format_filing_alert(result["ticker"], filing))
                    print()


def cmd_check(args):
    """Check a single ticker."""
    ticker = args.ticker.upper().strip()
    result = check_filings(ticker, days=args.days, enrich=not args.no_enrich)
    if edgar_enrich and not args.no_enrich:
        edgar_enrich.save_enrich_cache()

    if args.json:
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2))
    else:
        if result["count"] == 0:
            print(f"✅ {ticker}: No new filings in the past {args.days} day(s).")
        else:
            print(f"🔔 {ticker}: {result['count']} new filing(s):\n")
            for filing in result["new_filings"]:
                print(format_filing_alert(ticker, filing))


def cmd_self_test(args):
    """Verify findata-analyst is available and EDGAR works."""
    errors = []

    # Check findata-analyst exists
    run_sh = FINDATA_SKILL / "run.sh"
    if not run_sh.exists():
        errors.append(f"findata-analyst not found at {FINDATA_SKILL}")

    # Check EDGAR_IDENTITY
    if not os.environ.get("EDGAR_IDENTITY"):
        errors.append("EDGAR_IDENTITY env var not set (required by SEC)")

    if edgar_enrich is None:
        errors.append("edgar_enrich.py is missing or cannot be imported")
    else:
        sample = (
            '<font style="font-size:14pt;font-weight:700">'
            'Example Company Announces Quarterly Results</font>'
        )
        if edgar_enrich._styled_headline(sample) != "Example Company Announces Quarterly Results":
            errors.append("EDGAR headline parser self-test failed")

    # Try a simple query
    if not errors:
        data = run_findata("list-filings", ["--ticker", "AAPL", "--form", "10-K", "--limit", "1"])
        if not data.get("ok"):
            errors.append(f"findata-analyst test failed: {data.get('error', 'unknown')}")

    if args.json:
        if errors:
            print(json.dumps({
                "ok": False,
                "errors": errors,
                "hint": "Set EDGAR_IDENTITY and verify scripts/edgar_enrich.py plus findata-analyst.",
            }, ensure_ascii=False))
        else:
            print(json.dumps({"ok": True, "message": "All checks passed"}, ensure_ascii=False))
    else:
        if errors:
            for e in errors:
                print(f"❌ {e}")
            print("Hint: set EDGAR_IDENTITY and verify scripts/edgar_enrich.py plus findata-analyst.")
            sys.exit(1)
        else:
            print("✅ All checks passed")


def parse_tickers(tickers_arg: str) -> list:
    """Parse tickers from comma-separated string or file path."""
    if not tickers_arg:
        # Try default watchlist file
        watchlist = SKILL_DIR / "watchlist.txt"
        if watchlist.exists():
            return [t.strip().upper() for t in watchlist.read_text().splitlines()
                    if t.strip() and not t.strip().startswith("#")]
        return []

    # Check if it's a file path
    p = Path(tickers_arg)
    if p.exists() and p.is_file():
        return [t.strip().upper() for t in p.read_text().splitlines()
                if t.strip() and not t.strip().startswith("#")]

    # Comma-separated
    return [t.strip().upper() for t in tickers_arg.split(",") if t.strip()]


def main():
    parser = argparse.ArgumentParser(
        prog="company-filings-alert",
        description="Monitor SEC filings for a watchlist of companies"
    )
    parser.add_argument("--json", action="store_true", help="JSON output mode")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command")

    # scan — batch check all watchlist tickers
    p_scan = sub.add_parser("scan", help="Scan all watchlist tickers for new filings")
    p_scan.add_argument("--tickers", default="", help="Comma-separated tickers or path to watchlist file")
    p_scan.add_argument("--days", type=int, default=3, help="Lookback window in days (default: 3)")
    p_scan.add_argument("--no-enrich", action="store_true",
                        help="Skip SEC headline/body enrichment (faster, form type only)")

    # check — single ticker
    p_check = sub.add_parser("check", help="Check a single ticker")
    p_check.add_argument("ticker", help="Ticker symbol")
    p_check.add_argument("--days", type=int, default=3, help="Lookback window in days (default: 3)")
    p_check.add_argument("--no-enrich", action="store_true",
                        help="Skip SEC headline/body enrichment (faster, form type only)")

    # scan-cn-hk — check CN/HK announcements
    p_scan_cn = sub.add_parser("scan-cn-hk", help="Scan CN/HK watchlist for announcements")
    p_scan_cn.add_argument("--days", type=int, default=1, help="Lookback window in days (default: 1)")

    # self-test
    sub.add_parser("self-test", help="Verify setup")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "scan":
        cmd_scan(args)
    elif args.command == "scan-cn-hk":
        cmd_scan_cn_hk(args)
    elif args.command == "check":
        cmd_check(args)
    elif args.command == "self-test":
        cmd_self_test(args)


if __name__ == "__main__":
    main()
