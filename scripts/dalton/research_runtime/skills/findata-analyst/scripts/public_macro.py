"""Official, keyless FRED/ALFRED and Treasury data; no provider substitution."""
import csv
import io
import json
import re
import subprocess
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

CATALOG = {
    "DGS10": "美国十年期国债收益率",
    "DGS2": "美国两年期国债收益率",
    "DFII10": "美国十年期实际收益率",
    "T10Y2Y": "美国十年期减两年期国债利差",
    "BAMLH0A0HYM2": "美国高收益债期权调整利差",
    "CPIAUCSL": "美国 CPI（季调）",
    "PCEPI": "美国 PCE 价格指数",
    "UNRATE": "美国失业率",
    "PAYEMS": "美国非农就业人数",
    "GDPC1": "美国实际 GDP",
    "WALCL": "美联储总资产",
    "RRPONTSYD": "隔夜逆回购余额",
    "WTREGEN": "财政部一般账户周度余额",
}
TREASURY_BASE = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/"
TREASURY = {
    "tga": ("v1/accounting/dts/operating_cash_balance", "https://fiscaldata.treasury.gov/datasets/daily-treasury-statement/operating-cash-balance"),
    "debt": ("v2/accounting/od/debt_to_penny", "https://fiscaldata.treasury.gov/datasets/debt-to-the-penny/debt-to-the-penny"),
}


def iso_date(value):
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed > date.today():
        raise ValueError("日期须为 YYYY-MM-DD，且不能晚于今天")
    return value


def request_text(url):
    # The same official URL was verified through the host's curl transport.
    # No credentials, browser state, shell evaluation or alternate source.
    result = subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location",
                             "--proto", "=https", "--proto-redir", "=https",
                             "--max-time", "20", "--retry", "2", "--retry-delay", "1",
                             "--max-filesize", "32000000", url],
                            capture_output=True, timeout=65)
    if result.returncode:
        raise RuntimeError(f"Official endpoint request failed (curl exit {result.returncode}); no data substituted")
    return result.stdout.decode("utf-8-sig")


def decimal_value(value):
    if value is None or str(value).strip() in ("", ".", "null"):
        return None
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Non-finite numeric value in provider response")
    return str(number)


def parse_fred_csv(content, series, start, end, vintage=None):
    reader = csv.DictReader(io.StringIO(content))
    expected = series + ("_" + vintage.replace("-", "") if vintage else "")
    if reader.fieldnames != ["observation_date", expected]:
        raise ValueError(f"Unexpected CSV columns; expected observation_date,{expected}. Historical requests never fall back to current data.")
    rows = []
    for row in reader:
        observed = iso_date(row["observation_date"])
        if start <= observed <= end and (not vintage or observed <= vintage):
            rows.append({"observation_date": observed, "value": decimal_value(row[expected])})
    dates = [r["observation_date"] for r in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("Duplicate observation dates in provider response")
    return sorted(rows, key=lambda r: r["observation_date"])


def base_result(source, url):
    return {"source": source, "source_url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "cache": {"hit": False}, "published_at": None}


def fetch_macro(series, start, end, vintage=None, fetch=request_text):
    if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", series):
        raise ValueError("Use a FRED series ID, for example DGS10; not a URL")
    start, end = iso_date(start), iso_date(end)
    if start > end:
        raise ValueError("--start must not be later than --end")
    if vintage:
        vintage = iso_date(vintage)
        if start > vintage:
            raise ValueError("--start must not be later than --vintage")
        end = min(end, vintage)
    params = {"id": series, "cosd": start, "coed": end}
    if vintage:
        params["vintage_date"] = vintage
    url = ("https://alfred.stlouisfed.org/graph/alfredgraph.csv" if vintage else "https://fred.stlouisfed.org/graph/fredgraph.csv") + "?" + urlencode(params)
    rows = parse_fred_csv(fetch(url), series, start, end, vintage)
    valid = [r for r in rows if r["value"] is not None]
    return {**base_result("alfred" if vintage else "fred", url), "ok": True,
            "status": "ok" if valid else "empty", "series": series, "start": start, "end": end,
            "vintage": vintage, "point_in_time": "alfred_vintage_daily" if vintage else "current_revised_series",
            "unit": None, "metadata_status": "CSV does not supply units/frequency; verify series notes for the requested vintage, especially rebased GDP",
            "series_url": f"https://alfred.stlouisfed.org/series?seid={series}",
            "missing_count": len(rows) - len(valid), "count": len(rows), "rows": rows}


def parse_treasury(payload, dataset, start, end):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Treasury response has no data array")
    records = payload["data"]
    rows = []
    for row in records:
        observed = iso_date(row["record_date"])
        if not start <= observed <= end:
            raise ValueError("Treasury returned an observation outside the requested window")
        if dataset == "tga":
            # In the current layout every balance/flow is in open_today_bal.
            # close_today_bal is null; select the explicit Closing Balance row.
            if row.get("account_type") == "Treasury General Account (TGA) Closing Balance":
                value, field = row.get("open_today_bal"), "open_today_bal"
            elif row.get("account_type") == "Treasury General Account (TGA)" and decimal_value(row.get("close_today_bal")) is not None:
                value, field = row["close_today_bal"], "close_today_bal"
            else:
                continue
            rows.append({"observation_date": observed, "value": decimal_value(value),
                         "unit": "USD million", "account_type": row["account_type"], "value_field": field})
        else:
            fields = ("debt_held_public_amt", "intragov_hold_amt", "tot_pub_debt_out_amt")
            values = {key: decimal_value(row.get(key)) for key in fields}
            if all(values[key] is not None for key in fields):
                if abs(Decimal(values[fields[0]]) + Decimal(values[fields[1]]) - Decimal(values[fields[2]])) > Decimal("0.01"):
                    raise ValueError("Treasury debt components do not reconcile")
            rows.append({"observation_date": observed, "unit": "USD", **values})
    if records and not rows:
        raise ValueError("Treasury returned records but no recognized closing balance; inspect raw schema instead of reporting no data")
    dates = [row["observation_date"] for row in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("Multiple selected Treasury observations for a date; inspect dimensions")
    return sorted(rows, key=lambda r: r["observation_date"])


def fetch_treasury(dataset, start, end, fetch=request_text):
    start, end = iso_date(start), iso_date(end)
    if start > end:
        raise ValueError("--start must not be later than --end")
    path, source_url = TREASURY[dataset]
    merged, request_urls, meta = [], [], {}
    complete = True
    for page in range(1, 21):
        query = {"filter": f"record_date:gte:{start},record_date:lte:{end}", "sort": "record_date",
                 "page[size]": 1000, "page[number]": page}
        url = TREASURY_BASE + path + "?" + urlencode(query)
        request_urls.append(url)
        payload = json.loads(fetch(url))
        if not isinstance(payload.get("data"), list):
            raise ValueError("Invalid Treasury data response")
        merged.extend(payload["data"])
        meta = payload.get("meta", {})
        pages = meta.get("total-pages")
        if pages is None:
            raise ValueError("Treasury response missing pagination metadata")
        if page >= int(pages):
            break
    else:
        complete = False
    rows = parse_treasury({"data": merged}, dataset, start, end)
    return {**base_result("us_treasury_fiscaldata", source_url), "ok": True,
            "status": ("ok" if rows else "empty") if complete else "partial", "complete": complete,
            "dataset": dataset, "start": start, "end": end, "point_in_time": "not_available",
            "date_semantics": "record_date bounds observations only; this endpoint does not supply historical release vintages",
            "count": len(rows), "rows": rows, "request_urls": request_urls,
            "provider_metadata": meta, "raw_records": merged,
            "warnings": [] if complete else ["Pagination capped at 20 pages; narrow the date range"]}


def cmd_macro(args):
    return fetch_macro(args.series, args.start, args.end, args.vintage)


def cmd_treasury(args):
    return fetch_treasury(args.dataset, args.start, args.end)


def register_commands(sub):
    p = sub.add_parser("macro", help="Official FRED/ALFRED series; no new API key")
    p.add_argument("--series", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--vintage", help="ALFRED historical vintage, YYYY-MM-DD; no live-data fallback")
    p.set_defaults(func=cmd_macro)
    p = sub.add_parser("treasury", help="Official daily TGA closing balance or federal debt")
    p.add_argument("--dataset", choices=list(TREASURY), required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.set_defaults(func=cmd_treasury)
    p = sub.add_parser("macro-catalog", help="Starter FRED series catalog; any valid series ID is supported")
    p.set_defaults(func=lambda args: {"ok": True, "source": "local_catalog", "series": CATALOG})
