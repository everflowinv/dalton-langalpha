#!/usr/bin/env python3
"""Japan leverage/crowding monitor: official history + daily incremental dashboard."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import pandas as pd
import requests

WORKSPACE = Path.home() / ".openclaw" / "workspace"
OUT_ROOT = WORKSPACE / "research-outputs" / "jp-leverage"
DATA_DIR = OUT_ROOT / "data"
HTML_PATH = OUT_ROOT / "index.html"
SKILL_DIR = Path(__file__).resolve().parent.parent
TODAY = dt.date.today()
BASE = "https://www.jpx.co.jp"
MARGIN_HISTORY_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/05.html"
MARGIN_CURRENT_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/04.html"
STOCK_MARGIN_URL = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/140 Safari/537.36"}
TIMEOUT = 90


def log(msg: str):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def _is_missing_value(v) -> bool:
    """Narrow missing/null test that preserves explicit finite zero (`0`, `0.0`, `'0'`)."""
    if v is None:
        return True
    if isinstance(v, float):
        return not math.isfinite(v)
    if isinstance(v, str):
        return v.strip().lower() in ("", "nan", "none", "null")
    return False


def num(v):
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = str(v).replace(",", "").replace("%", "").strip()
    if not s or s.lower() in ("nan", "none", "null") or s in ("-", "－", "―", "*"):
        return None
    s = re.sub(r"^[▲△]\s*", "-", s)
    try:
        val = float(s)
    except ValueError:
        return None
    return val if math.isfinite(val) else None


def _sum_pair(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a + b


def get(url: str, **kwargs) -> requests.Response:
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


def hrefs(page_url: str, pattern: str, html: str | None = None) -> list[str]:
    if html is None:
        html = get(page_url).text
    links = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)
    return list(dict.fromkeys(urljoin(page_url, x) for x in links if re.search(pattern, x, re.I)))


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def _url_filename(url: str) -> str:
    return Path(urlsplit(url).path).name.lower()


def discover_margin_history_links(
    page_url: str = MARGIN_HISTORY_URL,
    html: str | None = None,
) -> dict[str, list[str]]:
    """Select combined and legacy margin history workbooks by semantic filename and label."""
    if html is None:
        html = get(page_url).text
    combined: list[str] = []
    detailed: list[str] = []
    jsf: list[str] = []
    seen: set[str] = set()

    def _add(bucket: list[str], url: str):
        if url not in bucket:
            bucket.append(url)
        seen.add(url)

    for tr_html in re.findall(r"<tr\b[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        label = _strip_tags(tr_html)
        row_links = re.findall(
            r'href=["\']([^"\']+\.xlsx?(?:\?[^"\']*)?)["\']',
            tr_html,
            flags=re.I,
        )
        for raw_href in row_links:
            full_url = urljoin(page_url, raw_href)
            fname = _url_filename(full_url)
            if "mtgk" in fname or "一般信用取引・制度信用取引別" in label:
                _add(detailed, full_url)
            elif "mtsk" in fname:
                _add(jsf, full_url)
            elif "信用取引現在高" in label:
                _add(combined, full_url)

    for match in re.finditer(
        r'<a\b[^>]*href=["\']([^"\']+\.xlsx?(?:\?[^"\']*)?)["\'][^>]*>(.*?)</a>',
        html,
        flags=re.I | re.S,
    ):
        raw_href, anchor_inner = match.group(1), match.group(2)
        full_url = urljoin(page_url, raw_href)
        if full_url in seen:
            continue
        fname = _url_filename(full_url)
        start = max(0, match.start() - 240)
        end = min(len(html), match.end() + 240)
        context_text = _strip_tags(anchor_inner) + " " + _strip_tags(html[start:end])
        if "mtgk" in fname or "一般信用取引・制度信用取引別" in context_text:
            _add(detailed, full_url)
        elif "mtsk" in fname:
            _add(jsf, full_url)
        elif "信用取引現在高" in context_text:
            _add(combined, full_url)

    return {"combined": combined, "detailed": detailed, "jsf": jsf}


def discover_margin_current_links(
    page_url: str = MARGIN_CURRENT_URL,
    html: str | None = None,
) -> list[str]:
    """Select current weekly margin workbooks (`YYYYMMDD_mtcurrent.xls[x]`) on `/04.html`."""
    return hrefs(page_url, r"(?:^|/)\d{8}_mtcurrent\.xlsx?(?:$|\?)", html=html)


def discover_kioxia_pdf_links(
    page_url: str = STOCK_MARGIN_URL,
    html: str | None = None,
) -> list[str]:
    """Select individual-stock margin PDFs (`YYYYMMDD_mtall.pdf` or compatible `syumatsu*.pdf`)."""
    return hrefs(page_url, r"(?:^|/)(?:\d{8}_mtall|syumatsu\d+)\.pdf(?:$|\?)", html=html)


def pdf_text(content: bytes) -> str:
    p = subprocess.run(["pdftotext", "-layout", "-", "-"], input=content,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return p.stdout.decode("utf-8", errors="replace")


def read_rows(path: Path, keycols=("date",)) -> dict:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {"|".join(r[k] for k in keycols): r for r in csv.DictReader(f)}


def merge_record_fields(
    existing: dict | None,
    incoming: dict,
    header: Sequence[str],
) -> dict:
    """Merge `incoming` into `existing` without erasing complementary non-missing fields."""
    base = dict(existing) if existing else {}
    merged: dict = {}
    for h in header:
        inc_val = incoming.get(h)
        if not _is_missing_value(inc_val):
            merged[h] = inc_val
        else:
            prev_val = base.get(h)
            merged[h] = prev_val if not _is_missing_value(prev_val) else ""
    return merged


def merge_margin_records(
    bydate: dict[str, dict],
    rows: Iterable[dict],
    header: Sequence[str] | None = None,
) -> dict[str, dict]:
    """Merge date-keyed partial or complete margin records preserving complementary fields."""
    cols = list(header or MARGIN_HEADER)
    for row in rows:
        day = str(row["date"])
        bydate[day] = merge_record_fields(bydate.get(day), row, cols)
    return bydate


def merge_rows(
    path: Path,
    header: list[str],
    rows: list[dict],
    keycols=("date",),
    preserve_complementary: bool = False,
):
    """Merge keyed rows into CSV atomically; preserve prior non-null fields only when requested."""
    old = read_rows(path, keycols)
    before = len(old)
    for row in rows:
        key = "|".join(str(row[k]) for k in keycols)
        if preserve_complementary:
            old[key] = merge_record_fields(old.get(key), row, header)
        else:
            old[key] = {h: ("" if row.get(h) is None else row.get(h, "")) for h in header}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for k in sorted(old):
                w.writerow(old[k])
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return len(old) - before, len(old)


def merge_margin_rows(
    path: Path,
    rows: list[dict],
    header: list[str] | None = None,
    keycols=("date",),
):
    """Persist JPX market-wide margin rows while preserving complementary fields across partial sources."""
    return merge_rows(
        path,
        list(header or MARGIN_HEADER),
        rows,
        keycols=keycols,
        preserve_complementary=True,
    )


# -------------------------- JPX market-wide weekly margin (official, 2002+)
MARGIN_HEADER = ["date", "short_shares_k", "short_value_mjpy", "long_shares_k",
                 "long_value_mjpy", "neg_short_value_mjpy", "std_short_value_mjpy",
                 "neg_long_value_mjpy", "std_long_value_mjpy",
                 "jsf_stock_loan_value_mjpy", "jsf_funding_value_mjpy"]


def _as_worksheet_date(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    return None


def _norm_cell_text(v) -> str:
    if v is None:
        return ""
    try:
        if pd.isna(v):
            return ""
    except (TypeError, ValueError):
        pass
    return re.sub(r"\s+", " ", str(v).replace("\u3000", " ")).strip()


def _col_header_text(d: pd.DataFrame, header_rows: int, col_idx: int) -> str:
    if col_idx >= d.shape[1]:
        return ""
    parts = [_norm_cell_text(d.iloc[r, col_idx]) for r in range(min(header_rows, len(d)))]
    return " ".join(p for p in parts if p)


def _is_combined_history_schema(d: pd.DataFrame, first_date_row: int) -> bool:
    if first_date_row <= 0 or d.shape[1] < 22:
        return False
    header_blob = " ".join(
        _col_header_text(d, first_date_row, c) for c in range(min(d.shape[1], 26))
    )
    if not (("千株" in header_blob or "thous.shs" in header_blob.lower())
            and ("百万円" in header_blob or "mil." in header_blob.lower())):
        return False
    c0 = _col_header_text(d, first_date_row, 0)
    if not (("月日" in c0 or "Month" in c0) and ("東京・名古屋" in header_blob or "Tokyo & Nagoya" in header_blob)):
        return False
    c1 = _col_header_text(d, first_date_row, 1)
    c3 = _col_header_text(d, first_date_row, 3)
    c5 = _col_header_text(d, first_date_row, 5)
    c7 = _col_header_text(d, first_date_row, 7)
    c9 = _col_header_text(d, first_date_row, 9)
    c11 = _col_header_text(d, first_date_row, 11)
    c13 = _col_header_text(d, first_date_row, 13)
    c15 = _col_header_text(d, first_date_row, 15)
    c18 = _col_header_text(d, first_date_row, 18)
    c20 = _col_header_text(d, first_date_row, 20)
    if not ("制度信用" in c1 and "自己" in c1 and "売残高" in c1):
        return False
    if "買残高" not in c3:
        return False
    if not ("委託" in c5 and "売残高" in c5):
        return False
    if "買残高" not in c7:
        return False
    if not ("一般信用" in c9 and "売残高" in c9):
        return False
    if "買残高" not in c11:
        return False
    if not ("合計" in c13 and "売残高" in c13 and "株数" in c13):
        return False
    if not ("買残高" in c15 and "株数" in c15):
        return False
    if not ("貸借取引" in c18 and "貸株" in c18):
        return False
    if "融資" not in c20:
        return False
    for val_col in (2, 4, 6, 8, 10, 12, 14, 16, 19, 21):
        col_txt = _col_header_text(d, first_date_row, val_col)
        if not ("金額" in col_txt or "Value" in col_txt):
            return False
    return True


def _parse_combined_history_df(d: pd.DataFrame, first_date_row: int) -> list[dict]:
    out: list[dict] = []
    for idx in range(first_date_row, len(d)):
        r = d.iloc[idx]
        day = _as_worksheet_date(r.iloc[0])
        if day is None:
            continue
        out.append({
            "date": day,
            "short_shares_k": num(r.iloc[13]),
            "short_value_mjpy": num(r.iloc[14]),
            "long_shares_k": num(r.iloc[15]),
            "long_value_mjpy": num(r.iloc[16]),
            "neg_short_value_mjpy": num(r.iloc[10]),
            "std_short_value_mjpy": _sum_pair(num(r.iloc[2]), num(r.iloc[6])),
            "neg_long_value_mjpy": num(r.iloc[12]),
            "std_long_value_mjpy": _sum_pair(num(r.iloc[4]), num(r.iloc[8])),
            "jsf_stock_loan_value_mjpy": num(r.iloc[19]),
            "jsf_funding_value_mjpy": num(r.iloc[21]),
        })
    if not out:
        raise ValueError("Combined JPX margin history workbook contained no worksheet date rows")
    return out


def _is_legacy_mtgk_schema(d: pd.DataFrame, first_date_row: int) -> bool:
    if first_date_row <= 0 or d.shape[1] < 13:
        return False
    if _is_combined_history_schema(d, first_date_row):
        return False
    header_blob = " ".join(
        _col_header_text(d, first_date_row, c) for c in range(d.shape[1])
    )
    if "貸借取引" in header_blob:
        return False
    if not ("売残" in header_blob and "買残" in header_blob):
        return False
    if not ("一般信用" in header_blob and "制度信用" in header_blob):
        return False
    if not ("株数" in header_blob and "金額" in header_blob):
        return False
    return True


def _is_legacy_mtsk_schema(d: pd.DataFrame, first_date_row: int) -> bool:
    if first_date_row <= 0 or not (18 <= d.shape[1] < 26):
        return False
    if _is_combined_history_schema(d, first_date_row):
        return False
    header_blob = " ".join(
        _col_header_text(d, first_date_row, c) for c in range(d.shape[1])
    )
    if "一般信用" in header_blob:
        return False
    if not ("信用取引現在高" in header_blob or ("売残" in header_blob and "買残" in header_blob)):
        return False
    jsf_blob = " ".join(_col_header_text(d, first_date_row, c) for c in range(13, min(d.shape[1], 20)))
    if not ("貸借" in jsf_blob or "証金" in jsf_blob or "貸株" in jsf_blob or "融資" in jsf_blob):
        return False
    return True


def _extract_current_application_date(cell_val) -> str | None:
    ws_date = _as_worksheet_date(cell_val)
    if ws_date is not None:
        return ws_date
    txt = _norm_cell_text(cell_val).replace(" ", "")
    m = re.search(r"(20\d{2})[/年-](\d{1,2})[/月-](\d{1,2})", txt)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _is_current_weekly_layout(d: pd.DataFrame) -> bool:
    if d.shape[0] < 18 or d.shape[1] < 8:
        return False
    if _extract_current_application_date(d.iloc[0, 1]) is None:
        return False
    scope_e6 = _norm_cell_text(d.iloc[5, 4])
    if not ("二市場" in scope_e6 or "Tokyo&Nagoya" in scope_e6 or "Tokyo & Nagoya" in scope_e6):
        return False
    col_e7 = _norm_cell_text(d.iloc[6, 4])
    col_f7 = _norm_cell_text(d.iloc[6, 5])
    col_g7 = _norm_cell_text(d.iloc[6, 6])
    col_h7 = _norm_cell_text(d.iloc[6, 7])
    if not (("売残高" in col_e7 or "Sales" in col_e7) and "前週比" not in col_e7 and "Weekly change" not in col_e7):
        return False
    if not ("前週比" in col_f7 or "Weekly change" in col_f7):
        return False
    if not (("買残高" in col_g7 or "Purchases" in col_g7) and "前週比" not in col_g7 and "Weekly change" not in col_g7):
        return False
    if not ("前週比" in col_h7 or "Weekly change" in col_h7):
        return False
    return True


def parse_margin_current_book(content: bytes) -> list[dict]:
    """Parse current weekly `YYYYMMDD_mtcurrent.xlsx` report layout on sheet 0."""
    d = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None)
    return _parse_current_weekly_df(d)


def _parse_current_weekly_df(d: pd.DataFrame) -> list[dict]:
    if not _is_current_weekly_layout(d):
        raise ValueError("Unrecognized JPX current weekly margin workbook schema")

    app_date = _extract_current_application_date(d.iloc[0, 1])
    if app_date is None:
        raise ValueError("Application date missing at B1 in current weekly margin workbook")

    top_units = " ".join(
        _norm_cell_text(x) for x in d.iloc[:7].to_numpy().ravel() if _norm_cell_text(x)
    )
    if not (("千株" in top_units or "thou. shares" in top_units.lower())
            and ("百万円" in top_units or "jpy mil." in top_units.lower())):
        raise ValueError("Expected thousand-shares / million-yen units not found in current weekly workbook")

    # Validate 1-based rows 8..15 (0-based 7..14) labels in columns B, C, D.
    if "制度信用" not in _norm_cell_text(d.iloc[7, 1]):
        raise ValueError("Standardized margin header missing at B8")
    if "自己" not in _norm_cell_text(d.iloc[7, 2]):
        raise ValueError("Proprietary account header missing at C8")
    if "株数" not in _norm_cell_text(d.iloc[7, 3]) or "金額" not in _norm_cell_text(d.iloc[8, 3]):
        raise ValueError("Proprietary shares/value row labels missing at D8/D9")
    if "委託" not in _norm_cell_text(d.iloc[9, 2]):
        raise ValueError("Standardized customer account header missing at C10")
    if "株数" not in _norm_cell_text(d.iloc[9, 3]) or "金額" not in _norm_cell_text(d.iloc[10, 3]):
        raise ValueError("Standardized customer shares/value row labels missing at D10/D11")
    if "一般信用" not in _norm_cell_text(d.iloc[11, 1]) or "委託" not in _norm_cell_text(d.iloc[11, 2]):
        raise ValueError("Negotiable margin header missing at B12/C12")
    if "株数" not in _norm_cell_text(d.iloc[11, 3]) or "金額" not in _norm_cell_text(d.iloc[12, 3]):
        raise ValueError("Negotiable shares/value row labels missing at D12/D13")
    if "信用取引残高合計" not in _norm_cell_text(d.iloc[13, 1]):
        raise ValueError("Total margin header missing at B14")
    if "株数" not in _norm_cell_text(d.iloc[13, 3]) or "金額" not in _norm_cell_text(d.iloc[14, 3]):
        raise ValueError("Total shares/value row labels missing at D14/D15")

    # Locate the JSF section (`貸借取引残高 Outstanding loans by JSF`) further down.
    jsf_shares_row: int | None = None
    for r_idx in range(15, len(d) - 1):
        b_label = _norm_cell_text(d.iloc[r_idx, 1])
        if "貸借取引残高" in b_label or "Outstanding loans by JSF" in b_label:
            if "株数" in _norm_cell_text(d.iloc[r_idx, 3]) and "金額" in _norm_cell_text(d.iloc[r_idx + 1, 3]):
                jsf_shares_row = r_idx
                break
    if jsf_shares_row is None:
        raise ValueError("JSF section (`貸借取引残高`) shares/value rows not found in current weekly workbook")
    jsf_value_row = jsf_shares_row + 1

    # Tokyo & Nagoya columns are E (0-based 4) for short/stock-loan and G (0-based 6) for long/funding.
    std_prop_short = num(d.iloc[8, 4])
    std_prop_long = num(d.iloc[8, 6])
    std_cust_short = num(d.iloc[10, 4])
    std_cust_long = num(d.iloc[10, 6])
    neg_short_val = num(d.iloc[12, 4])
    neg_long_val = num(d.iloc[12, 6])
    total_short_shs = num(d.iloc[13, 4])
    total_long_shs = num(d.iloc[13, 6])
    total_short_val = num(d.iloc[14, 4])
    total_long_val = num(d.iloc[14, 6])
    jsf_loan_val = num(d.iloc[jsf_value_row, 4])
    jsf_fund_val = num(d.iloc[jsf_value_row, 6])

    rec = {
        "date": app_date,
        "short_shares_k": total_short_shs,
        "short_value_mjpy": total_short_val,
        "long_shares_k": total_long_shs,
        "long_value_mjpy": total_long_val,
        "neg_short_value_mjpy": neg_short_val,
        "std_short_value_mjpy": _sum_pair(std_prop_short, std_cust_short),
        "neg_long_value_mjpy": neg_long_val,
        "std_long_value_mjpy": _sum_pair(std_prop_long, std_cust_long),
        "jsf_stock_loan_value_mjpy": jsf_loan_val,
        "jsf_funding_value_mjpy": jsf_fund_val,
    }
    if any(rec[k] is None for k in MARGIN_HEADER[1:]):
        raise ValueError("Current weekly margin workbook contains unparseable numeric cells")
    return [rec]


def parse_margin_book(content: bytes, detailed: bool | None = None) -> list[dict]:
    d = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None)
    if d.empty:
        raise ValueError("Empty JPX margin workbook")

    if _is_current_weekly_layout(d):
        return _parse_current_weekly_df(d)

    first_date_row: int | None = None
    for idx in range(len(d)):
        if _as_worksheet_date(d.iloc[idx, 0]) is not None:
            first_date_row = idx
            break

    if first_date_row is not None:
        if _is_combined_history_schema(d, first_date_row):
            return _parse_combined_history_df(d, first_date_row)

        if detailed is not False and _is_legacy_mtgk_schema(d, first_date_row):
            out = []
            indices = [1, 2, 3, 4, 6, 8, 10, 12]
            for idx in range(first_date_row, len(d)):
                r = d.iloc[idx]
                day = _as_worksheet_date(r.iloc[0])
                if day is None:
                    continue
                vals = [num(r.iloc[i]) if i < len(r) else None for i in indices]
                rec = {"date": day}
                rec.update(dict(zip(MARGIN_HEADER[1:9], vals)))
                out.append(rec)
            if not out:
                raise ValueError("Legacy mtgk margin workbook contained no rows")
            return out

        if detailed is not True and _is_legacy_mtsk_schema(d, first_date_row):
            out = []
            for idx in range(first_date_row, len(d)):
                r = d.iloc[idx]
                day = _as_worksheet_date(r.iloc[0])
                if day is None:
                    continue
                out.append({
                    "date": day,
                    "jsf_stock_loan_value_mjpy": num(r.iloc[15]) if len(r) > 15 else None,
                    "jsf_funding_value_mjpy": num(r.iloc[17]) if len(r) > 17 else None,
                })
            if not out:
                raise ValueError("Legacy mtsk margin workbook contained no rows")
            return out

        raise ValueError("Unrecognized JPX margin history workbook schema")

    # Legacy compact cross-sectional rolling workbooks (`mtgaisan` / `mtseisan`).
    top = " ".join(_norm_cell_text(x) for x in d.iloc[:3].to_numpy().ravel() if _norm_cell_text(x))
    md = re.search(r"(20\d{2})/(\d{1,2})/(\d{1,2})申込み?現在", top.replace(" ", ""))
    if not md:
        raise ValueError("Unrecognized JPX margin workbook schema")
    rec = {"date": f"{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"}
    body_text = " ".join(_norm_cell_text(x) for x in d.to_numpy().ravel() if _norm_cell_text(x))
    if detailed is not False and len(d) > 7 and d.shape[1] > 14 and "売残" in body_text and "買残" in body_text:
        shares, values = d.iloc[6], d.iloc[7]
        rec.update({
            "short_shares_k": num(shares.iloc[4]),
            "short_value_mjpy": num(values.iloc[4]),
            "long_shares_k": num(shares.iloc[6]),
            "long_value_mjpy": num(values.iloc[6]),
            "neg_short_value_mjpy": num(values.iloc[8]),
            "std_short_value_mjpy": num(values.iloc[10]),
            "neg_long_value_mjpy": num(values.iloc[12]),
            "std_long_value_mjpy": num(values.iloc[14]),
        })
        return [rec]
    if detailed is False and len(d) > 17 and d.shape[1] > 7 and "貸借" in body_text:
        values = d.iloc[17]
        rec["jsf_stock_loan_value_mjpy"] = num(values.iloc[5])
        rec["jsf_funding_value_mjpy"] = num(values.iloc[7])
        return [rec]
    raise ValueError("Unrecognized JPX cross-sectional margin workbook schema")


def sync_margin_weekly():
    hist_html = get(MARGIN_HISTORY_URL).text
    hist_links = discover_margin_history_links(MARGIN_HISTORY_URL, html=hist_html)
    if not hist_links["combined"] and not (hist_links["detailed"] and hist_links["jsf"]):
        raise RuntimeError("JPX margin history workbooks not found")

    bydate: dict[str, dict] = {}
    hist_errors: list[str] = []
    hist_specs: list[tuple[str, bool | None]] = (
        [(u, True) for u in hist_links["detailed"]]
        + [(u, False) for u in hist_links["jsf"]]
        + [(u, None) for u in hist_links["combined"]]
    )
    for url, detailed_flag in hist_specs:
        try:
            parsed = parse_margin_book(get(url).content, detailed_flag)
            if not parsed:
                raise ValueError("workbook yielded no rows")
            merge_margin_records(bydate, parsed)
        except Exception as e:
            hist_errors.append(f"{url}: {e}")
            log(f"JPX margin history skip {url}: {e}")
    if not bydate:
        raise RuntimeError(
            "JPX margin history workbooks yielded no rows"
            + (f" ({'; '.join(hist_errors)})" if hist_errors else "")
        )

    current_urls = discover_margin_current_links(MARGIN_CURRENT_URL)
    if not current_urls:
        merge_margin_rows(DATA_DIR / "margin_weekly.csv", list(bydate.values()), MARGIN_HEADER)
        raise RuntimeError("JPX current weekly margin workbooks not found")
    current_rows: list[dict] = []
    current_errors: list[str] = []
    for url in sorted(current_urls):
        try:
            parsed = parse_margin_book(get(url).content)
            if not parsed:
                raise ValueError("workbook yielded no rows")
            current_rows.extend(parsed)
            merge_margin_records(bydate, parsed)
        except Exception as e:
            current_errors.append(f"{url}: {e}")
            log(f"JPX current margin skip {url}: {e}")

    if bydate:
        added, total = merge_margin_rows(
            DATA_DIR / "margin_weekly.csv",
            list(bydate.values()),
            MARGIN_HEADER,
        )
        log(f"JPX margin weekly: fetched {len(bydate)}, +{added}, total {total}")

    if not current_rows:
        raise RuntimeError(
            "JPX current weekly margin workbooks yielded no rows"
            + (f" ({'; '.join(current_errors)})" if current_errors else "")
        )
    all_errors = hist_errors + current_errors
    if all_errors:
        raise RuntimeError(
            f"JPX weekly margin refresh incomplete ({len(all_errors)} workbook(s) failed): "
            + "; ".join(all_errors)
        )


# -------------------------- stock-level daily margin PDF (Kioxia focus)
KIOXIA_HEADER = ["date", "code", "name", "short_shares", "short_change",
                 "long_shares", "long_change", "src"]

_INT_TOKEN_RE = re.compile(r"^-?\d[\d,]*$")
_RATIO_TOKEN_RE = re.compile(r"^(?:-?\d+(?:\.\d+)?%|\*|-|－|―)$")
_KIOXIA_ISIN = "JP3236330001"


def _parse_kioxia_tokens(tokens: list[str], require_ratios: bool) -> tuple[int, int, int, int] | None:
    if len(tokens) >= 6 and _RATIO_TOKEN_RE.fullmatch(tokens[2]) and _RATIO_TOKEN_RE.fullmatch(tokens[5]):
        if not (
            _INT_TOKEN_RE.fullmatch(tokens[0])
            and _INT_TOKEN_RE.fullmatch(tokens[1])
            and _INT_TOKEN_RE.fullmatch(tokens[3])
            and _INT_TOKEN_RE.fullmatch(tokens[4])
        ):
            return None
        short_shares = int(tokens[0].replace(",", ""))
        short_change = int(tokens[1].replace(",", ""))
        long_shares = int(tokens[3].replace(",", ""))
        long_change = int(tokens[4].replace(",", ""))
        if short_shares < 0 or long_shares < 0:
            return None
        return short_shares, short_change, long_shares, long_change

    if not require_ratios and len(tokens) >= 4:
        if all(_INT_TOKEN_RE.fullmatch(tokens[i]) for i in range(4)):
            short_shares = int(tokens[0].replace(",", ""))
            short_change = int(tokens[1].replace(",", ""))
            long_shares = int(tokens[2].replace(",", ""))
            long_change = int(tokens[3].replace(",", ""))
            if short_shares < 0 or long_shares < 0:
                return None
            return short_shares, short_change, long_shares, long_change
    return None


def parse_kioxia_pdf(content: bytes, src: str) -> dict | None:
    txt = pdf_text(content)
    md = (
        re.search(r"(20\d{2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s*申込み?現在", txt)
        or re.search(r"As of\s+(20\d{2})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})\s+application based", txt, flags=re.I)
    )
    if not md:
        return None
    date_str = f"{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"

    lines = txt.splitlines()
    for idx, raw_line in enumerate(lines):
        # Reject monetary (`金額 Val.`) rows unconditionally.
        if "金額" in raw_line or "Val." in raw_line:
            continue
        has_code = bool(re.search(r"\b285A0?\b", raw_line))
        has_isin = _KIOXIA_ISIN in raw_line or (
            "キオクシアホールディングス" in raw_line and bool(re.search(r"\bJP\d{10}\b", raw_line))
        )
        if not (has_code and has_isin):
            continue

        candidate = raw_line
        if ("株数" not in candidate or "Shs." not in candidate) and idx + 1 < len(lines):
            nxt = lines[idx + 1]
            if "金額" not in nxt and "Val." not in nxt and ("株数" in nxt or "Shs." in nxt):
                candidate = f"{raw_line} {nxt}"

        shs_match = re.search(r"株数(?:\s|\S)*?Shs\.\s*(.+)$", candidate)
        if shs_match:
            tail = shs_match.group(1)
            clean = re.sub(r"[▲△]\s*", "-", tail)
            parsed = _parse_kioxia_tokens(clean.split(), require_ratios=True)
            if parsed is not None:
                short_shares, short_change, long_shares, long_change = parsed
                return {
                    "date": date_str,
                    "code": "285A",
                    "name": "Kioxia Holdings",
                    "short_shares": short_shares,
                    "short_change": short_change,
                    "long_shares": long_shares,
                    "long_change": long_change,
                    "src": src,
                }
            continue

        # Older compatible weekly layout without `株数 Shs.` marker.
        if "キオクシアホールディングス" in raw_line and "株数" not in txt and "Shs." not in txt:
            isin_split = re.split(r"\bJP\d{10}\b", raw_line, maxsplit=1)
            if len(isin_split) == 2:
                clean = re.sub(r"[▲△]\s*", "-", isin_split[1])
                parsed = _parse_kioxia_tokens(clean.split(), require_ratios=False)
                if parsed is not None:
                    short_shares, short_change, long_shares, long_change = parsed
                    return {
                        "date": date_str,
                        "code": "285A",
                        "name": "Kioxia Holdings",
                        "short_shares": short_shares,
                        "short_change": short_change,
                        "long_shares": long_shares,
                        "long_change": long_change,
                        "src": src,
                    }
    return None


def sync_kioxia_weekly():
    urls = discover_kioxia_pdf_links(STOCK_MARGIN_URL)
    if not urls:
        raise RuntimeError("JPX individual-stock margin PDFs not found")
    rows: list[dict] = []
    errors: list[str] = []
    for u in urls:
        try:
            r = parse_kioxia_pdf(get(u).content, u)
            if r:
                rows.append(r)
            else:
                reason = "Kioxia share row not found"
                errors.append(f"{u}: {reason}")
                log(f"Kioxia PDF skip {u}: {reason}")
        except Exception as e:
            errors.append(f"{u}: {e}")
            log(f"Kioxia PDF skip {u}: {e}")
    if rows:
        added, total = merge_rows(DATA_DIR / "kioxia_margin_weekly.csv", KIOXIA_HEADER, rows)
        log(f"Kioxia daily margin: fetched {len(rows)}, +{added}, total {total}")
    if not rows:
        raise RuntimeError(
            f"Kioxia daily margin refresh yielded 0 rows from {len(urls)} PDF(s)"
            + (f" ({'; '.join(errors)})" if errors else "")
        )
    if errors:
        raise RuntimeError(
            f"Kioxia daily margin refresh incomplete ({len(errors)}/{len(urls)} PDF(s) failed): "
            + "; ".join(errors)
        )


# -------------------------- JSF daily snapshot (official daily leading proxy)
JSF_AGG_HEADER = ["date", "funding_shares", "funding_value_mjpy", "stockloan_shares",
                  "stockloan_value_mjpy", "net_value_mjpy", "issue_count"]
JSF_ISSUE_HEADER = ["date", "code", "name", "funding_shares", "funding_value_jpy",
                    "stockloan_shares", "stockloan_value_jpy", "net_value_jpy"]


def sync_jsf_daily():
    raw = get("https://www.taisyaku.jp/data/zandaka.csv").content
    d = pd.read_csv(io.BytesIO(raw), encoding="cp932")
    date = pd.to_datetime(d["申込日"].iloc[0]).date().isoformat()
    cols = ["融資残高株数", "融資残高金額", "貸株残高株数", "貸株残高金額"]
    for c in cols:
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)
    agg = {"date": date, "funding_shares": int(d[cols[0]].sum()),
           "funding_value_mjpy": round(d[cols[1]].sum() / 1e6, 2),
           "stockloan_shares": int(d[cols[2]].sum()),
           "stockloan_value_mjpy": round(d[cols[3]].sum() / 1e6, 2),
           "net_value_mjpy": round((d[cols[1]].sum() - d[cols[3]].sum()) / 1e6, 2),
           "issue_count": int(len(d))}
    merge_rows(DATA_DIR / "jsf_daily_aggregate.csv", JSF_AGG_HEADER, [agg])

    d["銘柄コード"] = d["銘柄コード"].astype(str).str.replace(r"\.0$", "", regex=True)
    watch = set(d.nlargest(25, "融資残高金額")["銘柄コード"]) | {"285A", "1570", "1357"}
    rows = []
    for _, r in d[d["銘柄コード"].isin(watch)].iterrows():
        rows.append({"date": date, "code": r["銘柄コード"], "name": r["銘柄名"],
                     "funding_shares": int(r[cols[0]]), "funding_value_jpy": int(r[cols[1]]),
                     "stockloan_shares": int(r[cols[2]]), "stockloan_value_jpy": int(r[cols[3]]),
                     "net_value_jpy": int(r[cols[1]] - r[cols[3]])})
    merge_rows(DATA_DIR / "jsf_daily_issues.csv", JSF_ISSUE_HEADER, rows, ("date", "code"))
    log(f"JSF daily: {date}, {len(d)} issues, financing {agg['funding_value_mjpy']/1e6:.2f}tn JPY")


# taisyaku.jp per-issue search: daily loan balances, site keeps up to 3 years.
# This is the JSF (standardized margin routed through JSF) slice only -- a
# fraction of total JPX margin, but daily and back to each issue's start.
JSF_HIST_HEADER = ["date", "code", "name", "funding_shares", "stockloan_shares",
                   "net_shares", "loan_price_jpy"]
JSF_HIST_CODES = ("285A", "1570", "1357", "9984", "8035")


def sync_jsf_issue_history(codes=JSF_HIST_CODES):
    import requests as rq
    ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    today = dt.date.today()
    rows = []
    for code in codes:
        try:
            s = rq.Session()
            detail = f"https://www.taisyaku.jp/app/stock/detail/{code}-01"
            r1 = s.get(detail, headers=ua, timeout=60)
            r1.raise_for_status()
            tok = re.search(r'name="csrf_test_name" value="([^"]+)"', r1.text)
            mei = re.search(r'name="orgMgrMei" value="([^"]+)"', r1.text)
            if not tok:
                raise RuntimeError("csrf token not found")
            name = mei.group(1) if mei else code
            s.post(f"https://www.taisyaku.jp/app/stock/detail/{code}/search",
                   headers={**ua, "Referer": detail},
                   data={"csrf_test_name": tok.group(1), "orgMgrCd": code,
                         "orgMgrMei": name, "sort": "", "page": "", "fsort": "",
                         "fpage": "", "kjnYmdDays": "", "trjoKbn": "01",
                         "mkYmdFrom": "2013 / 01 / 01",
                         "mkYmdTo": today.strftime("%Y / %m / %d")},
                   timeout=60, allow_redirects=True)
            rc = s.get(f"https://www.taisyaku.jp/app/stock/detail/{code}/csv",
                       headers={**ua, "Referer": detail}, timeout=120)
            rc.raise_for_status()
            got = 0
            for rec in csv.DictReader(io.StringIO(rc.content.decode("cp932", "replace"))):
                fund, loan = rec.get("融資残高（株）", ""), rec.get("貸株残高（株）", "")
                if not fund and not loan:
                    continue
                d8 = rec.get("申込日", "").strip()
                if len(d8) != 8:
                    continue
                price = rec.get("貸借値段（円）", "").strip()
                rows.append({"date": f"{d8[:4]}-{d8[4:6]}-{d8[6:]}", "code": code,
                             "name": name,
                             "funding_shares": int(fund) if fund else 0,
                             "stockloan_shares": int(loan) if loan else 0,
                             "net_shares": (int(fund) if fund else 0) - (int(loan) if loan else 0),
                             "loan_price_jpy": float(price) if price and price not in ("-",) else None})
                got += 1
            log(f"JSF issue history {code}: {got} rows")
        except Exception as e:
            log(f"JSF issue history skip {code}: {e}")
    added, total = merge_rows(DATA_DIR / "jsf_issue_history.csv", JSF_HIST_HEADER,
                              rows, ("date", "code"))
    log(f"JSF issue history: +{added}, total {total}")


# -------------------------- investor flows (official weekly, 2021+)
FLOW_HEADER = ["date", "individual_net_bnjpy", "individual_margin_net_bnjpy",
               "individual_cash_net_bnjpy", "individual_margin_buy_share_pct",
               "foreign_net_bnjpy", "institutions_net_bnjpy", "investment_trust_net_bnjpy",
               "business_corp_net_bnjpy", "trust_bank_net_bnjpy", "life_insurance_net_bnjpy",
               "proprietary_net_bnjpy", "src"]


def _row_index(d: pd.DataFrame, needle: str):
    for i, v in d.iloc[:, 0].items():
        if needle in str(v).replace("\u3000", "").replace("\n", ""):
            return i
    return None


def parse_flow_xls(url: str) -> dict:
    content = get(url).content
    x = pd.ExcelFile(io.BytesIO(content))
    sheet = "TSE Prime" if "TSE Prime" in x.sheet_names else x.sheet_names[0]
    d = pd.read_excel(io.BytesIO(content), sheet_name=sheet, header=None)
    title = str(d.iloc[3, 0])
    my = re.search(r"(20\d{2})年", title)
    rg = re.search(r"\(\s*\d{1,2}/\d{1,2}\s*[-～]\s*(\d{1,2})/(\d{1,2})\s*\)", title)
    if not my or not rg:
        raise ValueError(f"week date not found: {title}")
    date = f"{my.group(1)}-{int(rg.group(1)):02d}-{int(rg.group(2)):02d}"

    def pair(label):
        i = _row_index(d, label)
        if i is None:
            return (None, None, None)
        # Current-period amounts are columns 8/9 for standard weekly layout.
        sell = num(d.iloc[i, 8]) if d.shape[1] > 8 else None
        buy = num(d.iloc[i + 1, 8]) if i + 1 < len(d) and d.shape[1] > 8 else None
        if sell is None or buy is None:  # older layout fallback
            numeric = [(j, num(d.iloc[i, j])) for j in range(d.shape[1])]
            numeric = [(j, v) for j, v in numeric if v is not None and j > 2]
            sell = numeric[-2][1] if len(numeric) >= 2 else None
            numeric2 = [(j, num(d.iloc[i + 1, j])) for j in range(d.shape[1])]
            numeric2 = [(j, v) for j, v in numeric2 if v is not None and j > 2]
            buy = numeric2[-2][1] if len(numeric2) >= 2 else None
        net = (buy - sell) / 1e6 if buy is not None and sell is not None else None
        return sell, buy, net

    _, indiv_buy, indiv = pair("個人")
    _, _, foreign = pair("海外投資家")
    _, _, institutions = pair("法人")
    _, _, invtrust = pair("投資信託")
    _, _, biz = pair("事業法人")
    _, _, trust = pair("信託銀行")
    _, _, life = pair("生保・損保")
    _, _, prop = pair("自己計")
    im = _row_index(d, "個人信用")
    ic = _row_index(d, "個人現金")
    im_sell = num(d.iloc[im, 2]) if im is not None and d.shape[1] > 4 else None
    im_buy = num(d.iloc[im, 4]) if im is not None and d.shape[1] > 4 else None
    ic_sell = num(d.iloc[ic, 2]) if ic is not None and d.shape[1] > 4 else None
    ic_buy = num(d.iloc[ic, 4]) if ic is not None and d.shape[1] > 4 else None
    imnet = (im_buy - im_sell) / 1e6 if im_buy is not None and im_sell is not None else None
    icnet = (ic_buy - ic_sell) / 1e6 if ic_buy is not None and ic_sell is not None else None
    share = im_buy / indiv_buy * 100 if im_buy is not None and indiv_buy else None
    return {"date": date, "individual_net_bnjpy": indiv,
            "individual_margin_net_bnjpy": imnet, "individual_cash_net_bnjpy": icnet,
            "individual_margin_buy_share_pct": share, "foreign_net_bnjpy": foreign,
            "institutions_net_bnjpy": institutions, "investment_trust_net_bnjpy": invtrust,
            "business_corp_net_bnjpy": biz, "trust_bank_net_bnjpy": trust,
            "life_insurance_net_bnjpy": life, "proprietary_net_bnjpy": prop, "src": url}


def sync_investor_flows(full=False):
    pages = ["https://www.jpx.co.jp/markets/statistics-equities/investor-type/index.html"]
    if full:
        # index.html only lists the most recent ~5 weeks. archives-00 holds the
        # rest of the current year; 01..05 hold 2025..2021.
        pages += [f"https://www.jpx.co.jp/markets/statistics-equities/investor-type/00-00-archives-{i:02d}.html"
                  for i in range(0, 6)]
    urls = []
    for p in pages:
        try:
            urls += hrefs(p, r"stock_val_1_\w+\.xlsx?(?:$|\?)")
        except Exception as e:
            log(f"flow page skip {p}: {e}")
    urls = list(dict.fromkeys(urls))
    old = read_rows(DATA_DIR / "investor_flows_weekly.csv")
    if not full:
        known_src = {r.get("src") for r in old.values()}
        urls = [u for u in urls if u not in known_src]
    rows, fails = [], []
    with ThreadPoolExecutor(max_workers=6) as ex:
        jobs = {ex.submit(parse_flow_xls, u): u for u in urls}
        for fut in as_completed(jobs):
            try:
                rows.append(fut.result())
            except Exception as e:
                fails.append((jobs[fut], str(e)))
    added, total = merge_rows(DATA_DIR / "investor_flows_weekly.csv", FLOW_HEADER, rows)
    log(f"JPX investor flows: parsed {len(rows)}/{len(urls)}, +{added}, total {total}, fail {len(fails)}")
    if fails and len(fails) > max(3, len(urls) // 5):
        raise RuntimeError(f"too many investor-flow parse failures: {fails[:3]}")


# -------------------------- short-selling ratio, official daily/monthly PDFs
SHORT_HEADER = ["date", "cash_value_mjpy", "cash_pct", "regulated_short_value_mjpy",
                "regulated_short_pct", "unregulated_short_value_mjpy",
                "unregulated_short_pct", "total_value_mjpy", "short_pct", "src"]


def parse_short_pdf(url: str, monthly=False) -> dict:
    txt = pdf_text(get(url).content)
    if monthly:
        pat = (r"(20\d{2})年(\d{1,2})月\s+([\d,]+)\s+([\d.]+)%\s+([\d,]+)\s+"
               r"([\d.]+)%\s+([\d,]+)\s+([\d.]+)%\s+([\d,]+)")
    else:
        pat = (r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s+([\d,]+)\s+([\d.]+)%\s+"
               r"([\d,]+)\s+([\d.]+)%\s+([\d,]+)\s+([\d.]+)%\s+([\d,]+)")
    m = re.search(pat, txt)
    if not m:
        raise ValueError("short-selling table row not found")
    g = m.groups()
    if monthly:
        date, vals = f"{g[0]}-{int(g[1]):02d}-01", g[2:]
    else:
        date, vals = f"{g[0]}-{int(g[1]):02d}-{int(g[2]):02d}", g[3:]
    cash, cp, reg, rp, unreg, up, total = vals
    return {"date": date, "cash_value_mjpy": num(cash), "cash_pct": num(cp),
            "regulated_short_value_mjpy": num(reg), "regulated_short_pct": num(rp),
            "unregulated_short_value_mjpy": num(unreg), "unregulated_short_pct": num(up),
            "total_value_mjpy": num(total), "short_pct": num(rp) + num(up), "src": url}


def sync_short_selling(full=False):
    daily_urls = hrefs("https://www.jpx.co.jp/markets/statistics-equities/short-selling/index.html",
                       r"/\d{6}-m\.pdf$")
    daily = []
    for u in daily_urls:
        try:
            daily.append(parse_short_pdf(u))
        except Exception as e:
            log(f"daily short skip {u}: {e}")
    merge_rows(DATA_DIR / "short_selling_daily.csv", SHORT_HEADER, daily)

    monthly = []
    pages = ["https://www.jpx.co.jp/markets/statistics-equities/short-selling/01.html"]
    if full:
        pages += [f"https://www.jpx.co.jp/markets/statistics-equities/short-selling/01-archives-{i:02d}.html"
                  for i in range(1, 6)]
    for p in pages:
        try:
            for u in hrefs(p, r"/\d{4}\.pdf$"):
                try:
                    monthly.append(parse_short_pdf(u, True))
                except Exception as e:
                    log(f"monthly short skip {u}: {e}")
        except Exception as e:
            log(f"short page skip {p}: {e}")
    merge_rows(DATA_DIR / "short_selling_monthly.csv", SHORT_HEADER, monthly)
    log(f"JPX short-selling: daily {len(daily)}, monthly {len(monthly)}")


# -------------------------- official NEXT FUNDS 1570/1357 NAV, units, C/R
ETF_SPECS = {"1570": ("NF Nikkei 225 Leveraged 2x", 2),
             "1357": ("NF Nikkei 225 Double Inverse -2x", -2)}
ETF_HEADER = ["date", "code", "name", "leverage", "aum_bnjpy", "shares_os",
              "nav", "return_pct", "creation_redemption_shares", "aum_after_bnjpy"]
ETF_PX_HEADER = ["date", "code", "name", "open", "high", "low", "close", "volume", "turnover_bnjpy"]


def sync_etf_units():
    rows = []
    for code, (name, lev) in ETF_SPECS.items():
        raw = get(f"https://www.nomura-am.co.jp/fund/etf/history/ETF_{code}.csv").content
        d = pd.read_csv(io.BytesIO(raw), encoding="cp932", skiprows=4)
        for _, r in d.iterrows():
            ds = str(r.iloc[0]).split(".")[0]
            if not re.fullmatch(r"\d{8}", ds):
                continue
            rows.append({"date": f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", "code": code,
                         "name": name, "leverage": lev, "aum_bnjpy": num(r.iloc[1]) / 1e9,
                         "shares_os": num(r.iloc[2]), "nav": num(r.iloc[3]),
                         "return_pct": num(r.iloc[5]) * 100 if num(r.iloc[5]) is not None else None,
                         "creation_redemption_shares": num(r.iloc[11]) if len(r) > 11 else None,
                         "aum_after_bnjpy": num(r.iloc[12]) / 1e9 if len(r) > 12 and num(r.iloc[12]) is not None else None})
    added, total = merge_rows(DATA_DIR / "etf_nav_units_daily.csv", ETF_HEADER, rows,
                              ("date", "code"))
    log(f"NEXT FUNDS units: fetched {len(rows)}, +{added}, total {total}")


def sync_etf_prices():
    rows = []
    for code, (name, _) in ETF_SPECS.items():
        end = int((dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).timestamp())
        data = None
        for attempt in range(6):
            host = "query1.finance.yahoo.com" if attempt % 2 else "query2.finance.yahoo.com"
            url = (f"https://{host}/v8/finance/chart/{code}.T"
                   f"?period1=1320000000&period2={end}&interval=1d&events=history&x={attempt}")
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()["chart"]["result"][0]
                break
            if r.status_code != 429:
                r.raise_for_status()
            time.sleep(2 * (attempt + 1))
        if data is None:
            raise RuntimeError(f"Yahoo chart rate-limited for {code} after retries")
        q = data["indicators"]["quote"][0]
        for i, ts in enumerate(data.get("timestamp") or []):
            close = q["close"][i]
            if close is None:
                continue
            day = (dt.datetime.fromtimestamp(ts, dt.timezone.utc) + dt.timedelta(hours=9)).date().isoformat()
            vol = q["volume"][i] or 0
            rows.append({"date": day, "code": code, "name": name,
                         "open": q["open"][i], "high": q["high"][i], "low": q["low"][i],
                         "close": close, "volume": vol, "turnover_bnjpy": close * vol / 1e9})
    added, total = merge_rows(DATA_DIR / "etf_prices_daily.csv", ETF_PX_HEADER, rows, ("date", "code"))
    log(f"Yahoo ETF prices: fetched {len(rows)}, +{added}, total {total}")


# -------------------------- official Nikkei 225 + Nikkei VI daily (3 years)
REF_HEADER = ["date", "nikkei_close", "nikkei_open", "nikkei_high", "nikkei_low",
              "nikkei_vi_close", "nikkei_vi_open", "nikkei_vi_high", "nikkei_vi_low"]


def nikkei_csv(name: str) -> dict[str, dict]:
    url = f"https://indexes.nikkei.co.jp/nkave/historical/{name}_daily_jp.csv"
    d = pd.read_csv(io.BytesIO(get(url).content), encoding="cp932")
    out = {}
    for _, r in d.iterrows():
        raw_day = str(r.iloc[0]).strip()
        if not re.fullmatch(r"20\d{2}/\d{1,2}/\d{1,2}", raw_day):
            continue
        day = pd.to_datetime(raw_day, format="%Y/%m/%d").date().isoformat()
        out[day] = {"close": num(r.iloc[1]), "open": num(r.iloc[2]),
                    "high": num(r.iloc[3]), "low": num(r.iloc[4])}
    return out


def sync_reference():
    n = nikkei_csv("nikkei_stock_average")
    v = nikkei_csv("nikkei_stock_average_vi")
    rows = []
    for day in sorted(set(n) | set(v)):
        a, b = n.get(day, {}), v.get(day, {})
        rows.append({"date": day, "nikkei_close": a.get("close"), "nikkei_open": a.get("open"),
                     "nikkei_high": a.get("high"), "nikkei_low": a.get("low"),
                     "nikkei_vi_close": b.get("close"), "nikkei_vi_open": b.get("open"),
                     "nikkei_vi_high": b.get("high"), "nikkei_vi_low": b.get("low")})
    added, total = merge_rows(DATA_DIR / "market_reference.csv", REF_HEADER, rows)
    log(f"Nikkei reference: fetched {len(rows)}, +{added}, total {total}")


def update(full=False):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    steps = [
        ("sync_margin_weekly", sync_margin_weekly),
        ("sync_kioxia_weekly", sync_kioxia_weekly),
        ("sync_jsf_daily", sync_jsf_daily),
        ("sync_jsf_issue_history", sync_jsf_issue_history),
        ("sync_investor_flows", lambda: sync_investor_flows(full)),
        ("sync_short_selling", lambda: sync_short_selling(full)),
        ("sync_etf_units", sync_etf_units),
        ("sync_etf_prices", sync_etf_prices),
        ("sync_reference", sync_reference),
    ]
    done, failed = [], []
    for name, fn in steps:
        try:
            fn()
            done.append(name)
        except Exception as e:
            failed.append({"step": name, "error": str(e)})
            log(f"WARN {name}: {e}")
    return {"ok": not failed, "done": done, "warnings": failed}


# -------------------------- dashboard
def build_dashboard():
    def rd(name):
        p = DATA_DIR / f"{name}.csv"
        if not p.exists():
            return pd.DataFrame()
        d = pd.read_csv(p)
        if "date" in d:
            d["date"] = d["date"].astype(str)
        return d

    margin, flows, jsf = rd("margin_weekly"), rd("investor_flows_weekly"), rd("jsf_daily_aggregate")
    kioxia, shortd, shortm = rd("kioxia_margin_weekly"), rd("short_selling_daily"), rd("short_selling_monthly")
    jsfh = rd("jsf_issue_history")
    etf, etfpx, ref = rd("etf_nav_units_daily"), rd("etf_prices_daily"), rd("market_reference")
    if margin.empty or etf.empty or ref.empty:
        raise RuntimeError("core data missing; run backfill first")

    for d in (margin, flows, jsf, jsfh, kioxia, shortd, shortm, etf, etfpx, ref):
        if not d.empty:
            for c in d.columns:
                if c not in ("date", "code", "name", "src"):
                    d[c] = pd.to_numeric(d[c], errors="coerce")
            d.sort_values("date", inplace=True)

    margin["long_tn"] = margin["long_value_mjpy"] / 1e6
    margin["short_tn"] = margin["short_value_mjpy"] / 1e6
    margin["ls_ratio"] = margin["long_value_mjpy"] / margin["short_value_mjpy"]
    margin["neg_long_share"] = margin["neg_long_value_mjpy"] / margin["long_value_mjpy"] * 100
    if not jsf.empty:
        jsf["funding_tn"] = jsf["funding_value_mjpy"] / 1e6
        jsf["stockloan_tn"] = jsf["stockloan_value_mjpy"] / 1e6
    if not etf.empty:
        etf["shares_mn"] = etf["shares_os"] / 1e6
        etf["cr_flow_bnjpy"] = etf["creation_redemption_shares"] * etf["nav"] / 1e9
        etf["rebalance_est_bnjpy"] = (etf["leverage"] - 1) * etf["return_pct"] / 100 * etf["aum_bnjpy"]

    def lastval(d, c):
        s = d[c].dropna() if not d.empty and c in d else pd.Series(dtype=float)
        return None if s.empty else float(s.iloc[-1])

    def percentile(d, c, n=None):
        s = d[c].dropna()
        if n:
            s = s.iloc[-n:]
        if len(s) < 5:
            return None
        return round(float((s <= s.iloc[-1]).mean() * 100), 1)

    def series(d, cols):
        if d.empty:
            return {"dates": [], **{k: [] for k in cols}}
        use = d[["date"] + list(cols.values())].copy()
        use = use[use[list(cols.values())].notna().any(axis=1)]
        out = {"dates": use["date"].tolist()}
        for k, c in cols.items():
            out[k] = [None if pd.isna(v) else round(float(v), 4) for v in use[c]]
        return out

    # Kioxia: union daily JPX all-market margin with daily JSF loan balances
    # (long-history proxy back to IPO; JSF slice only).
    kioxia_combined = kioxia
    if not jsfh.empty:
        k285 = jsfh[jsfh["code"].astype(str) == "285A"][["date", "funding_shares"]]
        k285 = k285.rename(columns={"funding_shares": "jsf_funding_shares"})
        if not k285.empty:
            kioxia_combined = pd.merge(kioxia, k285, on="date", how="outer").sort_values("date")
    if "jsf_funding_shares" not in kioxia_combined:
        kioxia_combined = kioxia_combined.assign(jsf_funding_shares=pd.NA)
        kioxia_combined["jsf_funding_shares"] = pd.to_numeric(kioxia_combined["jsf_funding_shares"], errors="coerce")

    e1570 = etf[etf["code"].astype(str) == "1570"]
    e1357 = etf[etf["code"].astype(str) == "1357"]
    latest_margin = margin.dropna(subset=["long_tn"]).iloc[-1]
    latest_flow = flows.iloc[-1] if not flows.empty else None
    latest_jsf = jsf.iloc[-1] if not jsf.empty else None
    latest_short = shortd.iloc[-1] if not shortd.empty else None
    l1570 = e1570.iloc[-1] if not e1570.empty else None
    l1357 = e1357.iloc[-1] if not e1357.empty else None
    aum_ratio = (l1570["aum_bnjpy"] / l1357["aum_bnjpy"] if l1570 is not None and l1357 is not None and l1357["aum_bnjpy"] else None)

    parts = {
        "信用买残": percentile(margin, "long_tn", 156),
        "买/卖残比": percentile(margin, "ls_ratio", 156),
        "个人信用买入占比": percentile(flows, "individual_margin_buy_share_pct", 156) if not flows.empty else None,
        "1570发行份额": percentile(e1570, "shares_mn", 756),
        "空卖占比": percentile(shortd, "short_pct", 252) if not shortd.empty else None,
    }
    composite = round(sum(v for v in parts.values() if v is not None) / max(1, sum(v is not None for v in parts.values())), 1)

    def kpi(name, value, unit, pct=None, asof=""):
        return {"name": name, "value": None if value is None else round(value, 2),
                "unit": unit, "pct": pct, "asof": asof}

    kpis = [
        kpi("信用买残", latest_margin["long_tn"], "兆円", percentile(margin, "long_tn", 156), latest_margin["date"]),
        kpi("信用买残/卖残", latest_margin["ls_ratio"], "倍", percentile(margin, "ls_ratio", 156), latest_margin["date"]),
        kpi("一般信用占买残", latest_margin["neg_long_share"], "%", percentile(margin, "neg_long_share", 156), latest_margin["date"]),
        kpi("日证金融资余额", None if latest_jsf is None else latest_jsf["funding_tn"], "兆円", percentile(jsf, "funding_tn", 252) if not jsf.empty else None, "" if latest_jsf is None else latest_jsf["date"]),
        kpi("个人信用净买入", None if latest_flow is None else latest_flow["individual_margin_net_bnjpy"], "十亿円", percentile(flows, "individual_margin_net_bnjpy", 156) if not flows.empty else None, "" if latest_flow is None else latest_flow["date"]),
        kpi("个人买入中信用占比", None if latest_flow is None else latest_flow["individual_margin_buy_share_pct"], "%", percentile(flows, "individual_margin_buy_share_pct", 156) if not flows.empty else None, "" if latest_flow is None else latest_flow["date"]),
        kpi("空卖成交占比", None if latest_short is None else latest_short["short_pct"], "%", percentile(shortd, "short_pct", 252) if not shortd.empty else None, "" if latest_short is None else latest_short["date"]),
        kpi("1570/1357 AUM", aum_ratio, "倍", None, "" if l1570 is None else l1570["date"]),
    ]
    etf_table = []
    for code in ("1570", "1357"):
        navrow = etf[etf["code"].astype(str) == code].iloc[-1]
        pxset = etfpx[etfpx["code"].astype(str) == code]
        pxrow = None if pxset.empty else pxset.iloc[-1]
        etf_table.append({"code": code, "name": ETF_SPECS[code][0],
                          "price": None if pxrow is None else float(pxrow["close"]),
                          "price_date": "" if pxrow is None else pxrow["date"],
                          "turnover": None if pxrow is None else float(pxrow["turnover_bnjpy"]),
                          "aum": float(navrow["aum_bnjpy"]), "shares": float(navrow["shares_mn"]),
                          "nav_date": navrow["date"]})

    payload = {
        "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M ET"), "composite": composite,
        "parts": parts, "kpis": kpis,
        "asof": {"margin": latest_margin["date"], "flow": "" if latest_flow is None else latest_flow["date"],
                 "daily": "" if latest_jsf is None else latest_jsf["date"], "etf": "" if l1570 is None else l1570["date"]},
        "ref": series(ref, {"nikkei": "nikkei_close", "vi": "nikkei_vi_close"}),
        "margin": series(margin, {"long": "long_tn", "short": "short_tn", "ratio": "ls_ratio", "negshare": "neg_long_share"}),
        "flows": series(flows, {"individual": "individual_net_bnjpy", "indmargin": "individual_margin_net_bnjpy",
                                 "indcash": "individual_cash_net_bnjpy", "foreign": "foreign_net_bnjpy",
                                 "trust": "trust_bank_net_bnjpy", "invtrust": "investment_trust_net_bnjpy",
                                 "business": "business_corp_net_bnjpy", "marginshare": "individual_margin_buy_share_pct"}),
        "jsf": series(jsf, {"funding": "funding_tn", "stockloan": "stockloan_tn", "net": "net_value_mjpy"}),
        "kioxia": series(kioxia_combined, {"long": "long_shares", "longchg": "long_change",
                                           "short": "short_shares", "jsffund": "jsf_funding_shares"}),
        "shortd": series(shortd, {"shortpct": "short_pct", "regulated": "regulated_short_pct", "unregulated": "unregulated_short_pct"}),
        "shortm": series(shortm, {"shortpct": "short_pct"}),
        "e1570": series(e1570, {"shares": "shares_mn", "aum": "aum_bnjpy", "flow": "cr_flow_bnjpy", "rebalance": "rebalance_est_bnjpy"}),
        "e1357": series(e1357, {"shares": "shares_mn", "aum": "aum_bnjpy", "flow": "cr_flow_bnjpy", "rebalance": "rebalance_est_bnjpy"}),
        "etf_table": etf_table,
    }

    def finite(o):
        if isinstance(o, float):
            return o if math.isfinite(o) else None
        if isinstance(o, dict):
            return {k: finite(v) for k, v in o.items()}
        if isinstance(o, list):
            return [finite(v) for v in o]
        return o

    html = HTML_TEMPLATE.replace("__PAYLOAD__", json.dumps(finite(payload), ensure_ascii=False, allow_nan=False))
    asset = SKILL_DIR / "assets" / "echarts.min.js"
    if asset.exists():
        html = html.replace('<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>',
                            "<script>" + asset.read_text(encoding="utf-8") + "</script>")
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    HTML_PATH.write_text(html, encoding="utf-8")
    log(f"dashboard: {HTML_PATH} ({HTML_PATH.stat().st_size // 1024} KB)")
    return {"ok": True, "html": str(HTML_PATH), "rows": {p.stem: sum(1 for _ in p.open()) - 1 for p in DATA_DIR.glob("*.csv")}}


HTML_TEMPLATE = r'''<!doctype html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>日本杠杆/拥挤度监控</title><script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>:root{--bg:#f4f6f9;--card:#fff;--tx:#1c2733;--mut:#5a6b7b;--blue:#1565c0;--red:#d32f2f;--green:#2e7d32;--orange:#ef6c00;--purple:#7b1fa2}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif}.wrap{max-width:1280px;margin:auto;padding:20px}h1{font-size:22px;margin:0 0 4px}.sub{font-size:12px;color:var(--mut);margin-bottom:15px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}.card{background:var(--card);border:1px solid #e3e9ef;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(30,45,60,.06)}.kname{font-size:12px;color:var(--mut)}.kval{font-size:24px;font-weight:650;margin:3px 0}.kmeta{font-size:11px;color:var(--mut)}.section{margin:22px 0 8px;border-left:3px solid var(--blue);padding-left:8px;font-size:15px;font-weight:650}.chart{height:330px}.interp{margin-top:10px;padding:10px 12px;background:#f7f9fc;border-left:3px solid #90a4ae;color:#415466;font-size:12px;line-height:1.7}.interp b{color:#1f3345}.gauge{display:flex;gap:22px;align-items:center;margin-top:14px}.gnum{font-size:44px;font-weight:750}.note{font-size:11px;color:var(--mut);line-height:1.7;margin:18px 2px}.pill{display:inline-block;padding:1px 6px;border-radius:4px;margin-left:5px}table{width:100%;border-collapse:collapse;font-size:12px;margin-top:4px}th,td{padding:7px 9px;text-align:right;border-bottom:1px solid #d6dee6}th:first-child,td:first-child{text-align:left}th{color:var(--mut);font-weight:500}</style></head><body><div class="wrap">
<h1>🇯🇵 日本市场杠杆 / 拥挤度监控</h1><div class="sub" id="sub"></div><div class="grid" id="kpis"></div>
<div class="card gauge"><div><div class="kname">综合拥挤度（各指标近3年分位均值）</div><div class="gnum" id="gnum">–</div></div><div id="parts" class="kmeta"></div></div>

<div class="section">全市场信用取引余额（JPX，周频，2002至今）</div><div class="card"><div id="margin" class="chart"></div><div class="interp"><b>怎么读：</b>信用买残是投资者借钱买股后尚未平仓的存量，是日本散户杠杆的主指标。买残创新高且买/卖残比上升，说明多头杠杆与未来潜在卖压都在堆积；余额回落表示去杠杆。一般信用占比高，代表非制度信用、期限更灵活的融资更活跃。原始数据周频，日更管线不会把它插值成日频。</div></div>
<div class="section">投资者类型净买卖（JPX Prime，周频，十亿日元）</div><div class="card"><div id="flows" class="chart"></div><div class="interp"><b>怎么读：</b>个人在下跌周逆势净买、海外投资者净卖，是典型“散户接机构/外资筹码”；若持续多周且信用买残同步增加，拥挤风险更高。信托银行常被当作年金/长期资金再平衡代理；事业法人净买往往包含回购。</div></div>
<div class="section">个人现金 vs 信用交易</div><div class="card"><div id="indflow" class="chart"></div><div class="interp"><b>怎么读：</b>个人信用净买入比个人总净买入更直接地反映“借钱抄底”。“个人买入中信用占比”回答的是散户当周买盘有多少通过信用账户完成；占比抬升且信用净买入为正，代表加杠杆，反之代表主动降杠杆或被动平仓。</div></div>
<div class="section">日证金貸借取引余额（日频先行代理）</div><div class="card"><div id="jsf" class="chart"></div><div class="interp"><b>怎么读：</b>“融资”是证券公司向日证金融入资金支持制度信用买入，“贷株”是为信用卖空借入股票。融资上升=制度信用多头加杠杆，贷株上升=做空需求增加。它只覆盖经日证金的制度信用，不等于全市场信用余额，但日频、发布快，适合做周频 JPX 数据的先行信号。注意：全市场合计的官方源每天只发布最新一日快照，本库自 2026-07-15 起逐日累积（起步阶段点少属正常，会越攒越长）；个股层面（Kioxia、1570 等重点票）已用日证金个股检索回补了最长 3 年的日频历史。</div></div>
<div class="section">Kioxia 个股信用余额（JPX 全市场日频余额）</div><div class="card"><div id="kioxia" class="chart"></div><div class="interp"><b>怎么读：</b>Kioxia 是本轮日本 AI/存储交易的拥挤核心。看绝对买残与日变化：指数下跌时买残继续增加，说明散户逆势加杠杆；买残快速下降则可能是止损、追证或主动降仓。蓝/橙线是 JPX 全市场日频余额（免费官方 PDF 只保留最近约 5 个交易日，无历史归档，数据库逐日向前累积）；绿线是日证金贷借融资残的日频序列，已回补到 2024-12 IPO 次日——它只覆盖经日证金的制度信用（当前约占全市场买残一成量级），绝对值不可比，但提供更长历史参照。</div></div>
<div class="section">1570 / 1357 绝对发行份额与 AUM（野村官方，日频）</div><div class="card"><div id="units" class="chart"></div><table id="etftable"></table><div class="interp"><b>怎么读：</b>1570 是日经 2x 多头，1357 是日经 -2x。成交额只说明换手，发行份额净增才代表一级市场有新杠杆资金进入；份额下降代表赎回/去杠杆。1570份额上升、1357份额下降是多头拥挤；反过来是防御/做空需求上升。表内收盘价/成交额来自交易所行情转载，AUM/份额来自管理人官方；拆分与并份已按官方历史口径调整。</div></div>
<div class="section">ETF 每日设置(+)/解约(−)金额</div><div class="card"><div id="etfflow" class="chart"></div><div class="interp"><b>怎么读：</b>这是“份额变化 × NAV”的资金金额，比成交额更接近真实净申购/赎回。正值是新份额创设、风险敞口扩张；负值是份额赎回、风险敞口收缩。注意一级市场申赎反映时点可能与盘中交易日存在一个业务日差。</div></div>
<div class="section">ETF 理论再平衡需求（模型估算）</div><div class="card"><div id="rebalance" class="chart"></div><div class="interp"><b>怎么读：</b>日内涨跌后，2x/-2x 产品需要在收盘附近调整期货敞口以恢复目标倍数。估算公式为 (L−1)×ETF当日收益×AUM；正值表示理论买入、负值表示理论卖出。它不是管理人实际成交披露，只用于判断机械流量可能占市场交易的方向与量级。</div></div>
<div class="section">空卖成交占比（JPX）</div><div class="card"><div id="short" class="chart"></div><div class="interp"><b>怎么读：</b>空卖占比上升表示做空/对冲成交活跃，但不等同于净空头仓位，因为信用卖空和做市对冲也在其中。价格下跌+空卖占比急升更像风险释放；价格上涨+空卖占比高可能意味着对冲或潜在回补。日频官方页面只保留滚动窗口，月频历史用于长比较。</div></div>
<div class="section">日经平均 VI（波动率/去杠杆压力）</div><div class="card"><div id="vi" class="chart"></div><div class="interp"><b>怎么读：</b>VI 是未来约一个月预期波动的代理。信用余额高位时 VI 急升，通常会放大保证金压力、CTA/风险平价减仓与散户追证；VI 回落但信用余额仍高，说明风险情绪修复却未必完成去杠杆。</div></div>
<div class="note"><b>数据边界：</b>松井证券店内信用评价损益率现为会员限定，公开抓取未接入；个股全市场信用余额免费版为日频 PDF；JPX 投资者类型为周频；日证金为日频但仅覆盖制度信用。官方原始值与模型估算分栏。来源：JPX、日证金、NEXT FUNDS、Nikkei Indexes。全部数据保存于 research-outputs/jp-leverage/data/，只增不删。</div>
</div><script type="application/json" id="payload">__PAYLOAD__</script><script>
const P=JSON.parse(document.getElementById('payload').textContent);document.getElementById('sub').textContent=`信用余额 ${P.asof.margin}｜投资者资金流 ${P.asof.flow||'—'}｜日频代理 ${P.asof.daily||'—'}｜ETF ${P.asof.etf||'—'}｜生成 ${P.generated}`;
document.getElementById('etftable').innerHTML='<tr><th>代码</th><th>收盘价(日元)</th><th>成交额(十亿日元)</th><th>AUM(十亿日元)</th><th>份额(百万)</th><th>行情日 / NAV日</th></tr>'+P.etf_table.map(x=>`<tr><td>${x.code} ${x.name}</td><td>${x.price?.toLocaleString?.()??'—'}</td><td>${x.turnover?.toFixed?.(1)??'—'}</td><td>${x.aum?.toFixed?.(1)??'—'}</td><td>${x.shares?.toFixed?.(2)??'—'}</td><td>${x.price_date} / ${x.nav_date}</td></tr>`).join('');
function pc(p){return p==null?'#64748b':p>=90?'#d32f2f':p>=70?'#ef6c00':p<=20?'#2e7d32':'#5a6b7b'};const K=document.getElementById('kpis');P.kpis.forEach(k=>K.insertAdjacentHTML('beforeend',`<div class="card"><div class="kname">${k.name}</div><div class="kval">${k.value==null?'—':k.value.toLocaleString()} <span class="kname">${k.unit}</span></div><div class="kmeta">截至 ${k.asof||'—'} ${k.pct==null?'':`<span class="pill" style="color:${pc(k.pct)};background:${pc(k.pct)}18">3年分位 ${k.pct}%</span>`}</div></div>`));const G=document.getElementById('gnum');G.textContent=P.composite+'%';G.style.color=pc(P.composite);document.getElementById('parts').innerHTML=Object.entries(P.parts).map(([k,v])=>`${k}: <b style="color:${pc(v)}">${v==null?'—':v+'%'}</b>`).join('<br>');
const base={backgroundColor:'transparent',textStyle:{color:'#5a6b7b'},tooltip:{trigger:'axis',backgroundColor:'#fff',borderColor:'#d6dee6',textStyle:{color:'#1c2733'}},legend:{textStyle:{color:'#3f5163'}},grid:{left:65,right:65,top:38,bottom:58}};function ax(){return{axisLine:{lineStyle:{color:'#cfd8e3'}},splitLine:{lineStyle:{color:'#edf1f5'}}}}const RM=new Map((P.ref.dates||[]).map((d,i)=>[d,P.ref.nikkei[i]]));function zoom(ds){let s=ds.length>504?(1-504/ds.length)*100:0;return[{type:'inside',start:s,end:100},{type:'slider',start:s,end:100,height:18,bottom:8}]};function chart(id,opt,dates){if(dates?.length&&RM.size){if(!Array.isArray(opt.yAxis))opt.yAxis=[opt.yAxis];let yi=opt.yAxis.length;opt.yAxis.push({type:'value',scale:true,position:'right',axisLabel:{show:false},axisLine:{show:false},splitLine:{show:false}});opt.series.push({name:'日经225(虚线)',type:'line',yAxisIndex:yi,data:dates.map(d=>RM.get(d)??null),showSymbol:false,connectNulls:true,lineStyle:{type:'dashed',width:1},color:'#2e7d32'});if(opt.legend?.data)opt.legend.data.push('日经225(虚线)')}let c=echarts.init(document.getElementById(id));c.setOption(Object.assign({},base,opt));addEventListener('resize',()=>c.resize())}
function line(name,data,color,y=0){const n=(data||[]).filter(v=>v!==null&&v!==undefined).length;return{name,type:'line',data,showSymbol:n<15,symbolSize:5,connectNulls:true,yAxisIndex:y,lineStyle:{width:1.6},color}}function bar(name,data,color,y=0){return{name,type:'bar',data,yAxisIndex:y,itemStyle:{color}}}
chart('margin',{legend:{data:['信用买残','信用卖残','买/卖残比(右)']},xAxis:{type:'category',data:P.margin.dates,...ax()},yAxis:[{type:'value',name:'兆円',scale:true,...ax()},{type:'value',name:'倍',scale:true,...ax()}],dataZoom:zoom(P.margin.dates),series:[line('信用买残',P.margin.long,'#1565c0'),line('信用卖残',P.margin.short,'#ef6c00'),line('买/卖残比(右)',P.margin.ratio,'#d32f2f',1)]},P.margin.dates);
chart('flows',{legend:{data:['个人','海外','信托银行','投信','事业法人']},xAxis:{type:'category',data:P.flows.dates,...ax()},yAxis:{type:'value',name:'十亿円',scale:true,...ax()},dataZoom:zoom(P.flows.dates),series:[bar('个人',P.flows.individual,'#1565c088'),bar('海外',P.flows.foreign,'#d32f2f88'),line('信托银行',P.flows.trust,'#7b1fa2'),line('投信',P.flows.invtrust,'#ef6c00'),line('事业法人',P.flows.business,'#2e7d32')]},P.flows.dates);
chart('indflow',{legend:{data:['信用净买入','现金净买入','信用买入占比(右)']},xAxis:{type:'category',data:P.flows.dates,...ax()},yAxis:[{type:'value',name:'十亿円',scale:true,...ax()},{type:'value',name:'%',scale:true,...ax()}],dataZoom:zoom(P.flows.dates),series:[bar('信用净买入',P.flows.indmargin,'#1565c088'),bar('现金净买入',P.flows.indcash,'#ef6c0088'),line('信用买入占比(右)',P.flows.marginshare,'#d32f2f',1)]},P.flows.dates);
chart('jsf',{legend:{data:['融资余额','贷株余额']},xAxis:{type:'category',data:P.jsf.dates,...ax()},yAxis:{type:'value',name:'兆円',scale:true,...ax()},dataZoom:zoom(P.jsf.dates),series:[line('融资余额',P.jsf.funding,'#1565c0'),line('贷株余额',P.jsf.stockloan,'#d32f2f')]},P.jsf.dates);
chart('kioxia',{legend:{data:['信用买残(JPX日频)','信用卖残(JPX日频)','日证金融资残(日频代理)','买残日变化(右)']},xAxis:{type:'category',data:P.kioxia.dates,...ax()},yAxis:[{type:'value',name:'股',scale:true,...ax()},{type:'value',name:'股',scale:true,...ax()}],dataZoom:zoom(P.kioxia.dates),series:[line('信用买残(JPX日频)',P.kioxia.long,'#1565c0'),line('信用卖残(JPX日频)',P.kioxia.short,'#ef6c00'),line('日证金融资残(日频代理)',P.kioxia.jsffund,'#2e7d32'),bar('买残日变化(右)',P.kioxia.longchg,'#d32f2f55',1)]},P.kioxia.dates);
const ud=P.e1570.dates.length>=P.e1357.dates.length?P.e1570:P.e1357;function amap(obj,key,dates){let m=new Map(obj.dates.map((d,i)=>[d,obj[key][i]]));return dates.map(d=>m.get(d)??null)}chart('units',{legend:{data:['1570份额','1357份额','1570 AUM(右)','1357 AUM(右)']},xAxis:{type:'category',data:ud.dates,...ax()},yAxis:[{type:'value',name:'百万份',scale:true,...ax()},{type:'value',name:'十亿円',scale:true,...ax()}],dataZoom:zoom(ud.dates),series:[line('1570份额',amap(P.e1570,'shares',ud.dates),'#1565c0'),line('1357份额',amap(P.e1357,'shares',ud.dates),'#d32f2f'),line('1570 AUM(右)',amap(P.e1570,'aum',ud.dates),'#2e7d32',1),line('1357 AUM(右)',amap(P.e1357,'aum',ud.dates),'#7b1fa2',1)]},ud.dates);
chart('etfflow',{legend:{data:['1570设置/解约','1357设置/解约']},xAxis:{type:'category',data:ud.dates,...ax()},yAxis:{type:'value',name:'十亿円',scale:true,...ax()},dataZoom:zoom(ud.dates),series:[bar('1570设置/解约',amap(P.e1570,'flow',ud.dates),'#1565c088'),bar('1357设置/解约',amap(P.e1357,'flow',ud.dates),'#d32f2f88')]},ud.dates);
chart('rebalance',{legend:{data:['1570理论流量','1357理论流量']},xAxis:{type:'category',data:ud.dates,...ax()},yAxis:{type:'value',name:'十亿円',scale:true,...ax()},dataZoom:zoom(ud.dates),series:[bar('1570理论流量',amap(P.e1570,'rebalance',ud.dates),'#1565c088'),bar('1357理论流量',amap(P.e1357,'rebalance',ud.dates),'#d32f2f88')]},ud.dates);
chart('short',{legend:{data:['空卖占比','有价格规制','无价格规制','月频空卖占比']},xAxis:{type:'category',data:P.shortd.dates,...ax()},yAxis:{type:'value',name:'%',scale:true,...ax()},dataZoom:zoom(P.shortd.dates),series:[line('空卖占比',P.shortd.shortpct,'#d32f2f'),line('有价格规制',P.shortd.regulated,'#ef6c00'),line('无价格规制',P.shortd.unregulated,'#7b1fa2')]},P.shortd.dates);
chart('vi',{legend:{data:['日经VI']},xAxis:{type:'category',data:P.ref.dates,...ax()},yAxis:{type:'value',name:'VI',scale:true,...ax()},dataZoom:zoom(P.ref.dates),series:[line('日经VI',P.ref.vi,'#d32f2f')]},P.ref.dates);
</script></body></html>'''


def self_test(args):
    checks = []
    tests = {
        "JPX margin": MARGIN_HISTORY_URL,
        "JSF daily CSV": "https://www.taisyaku.jp/data/zandaka.csv",
        "NEXT FUNDS 1570": "https://www.nomura-am.co.jp/fund/etf/history/ETF_1570.csv",
        "Nikkei VI": "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_vi_daily_jp.csv",
    }
    for name, url in tests.items():
        try:
            r = get(url)
            checks.append({"name": name, "ok": len(r.content) > 100})
        except Exception as e:
            checks.append({"name": name, "ok": False, "error": str(e), "hint": "Check network/source layout"})
    checks.append({"name": "pdftotext", "ok": subprocess.run(["which", "pdftotext"], stdout=subprocess.DEVNULL).returncode == 0})
    ok = all(x["ok"] for x in checks)
    payload = {"ok": ok, "checks": checks, "output": str(OUT_ROOT)}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"Self-test: {'PASS' if ok else 'FAIL'}")
        for x in checks:
            print(f"  [{'PASS' if x['ok'] else 'FAIL'}] {x['name']}")
    if not ok:
        raise SystemExit(1)


def emit(payload, as_json=False):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))


def main():
    p = argparse.ArgumentParser(description="Japan leverage/crowding monitor")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--version", action="version", version="jp-leverage-monitor 1.0")
    sp = p.add_subparsers(dest="command", required=True)
    sp.add_parser("self-test")
    sp.add_parser("backfill")
    sp.add_parser("update")
    sp.add_parser("dashboard")
    sp.add_parser("run")
    args = p.parse_args()
    try:
        if args.command == "self-test":
            return self_test(args)
        if args.command == "backfill":
            u = update(True)
            d = build_dashboard()
            ok = bool(u.get("ok")) and bool(d.get("ok", True))
            emit({"ok": ok, "update": u, "dashboard": d}, args.json)
            if not ok:
                raise SystemExit(1)
        elif args.command == "update":
            u = update(False)
            emit(u, args.json)
            if not u.get("ok"):
                raise SystemExit(1)
        elif args.command == "dashboard":
            d = build_dashboard()
            emit(d, args.json)
            if not d.get("ok", True):
                raise SystemExit(1)
        elif args.command == "run":
            u = update(False)
            d = build_dashboard()
            ok = bool(u.get("ok")) and bool(d.get("ok", True))
            emit({"ok": ok, "update": u, "dashboard": d}, args.json)
            if not ok:
                raise SystemExit(1)
    except Exception as e:
        payload = {"ok": False, "error": str(e), "hint": "Run self-test; inspect source layout/network; retry.",
                   "traceback": traceback.format_exc() if args.verbose else None}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(f"ERROR: {e}\nHint: {payload['hint']}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
