#!/usr/bin/env python3
"""Dalton SEC EDGAR XBRL period-normalized facts MCP server (`dalton_sec`).

Self-contained stdio MCP server implementing deterministic period normalization
(calendar/fiscal quarters, annual, H1/9M YTD, derived Q4 = FY - 9M, trailing N
periods) and restatement selection (`as_reported` vs `latest`) with point-in-time
`as_of` filtering and `_declared_sources` provenance.
"""

from __future__ import annotations

import difflib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from _bootstrap import MCPServer
except ModuleNotFoundError:
    from mcp_servers._bootstrap import MCPServer

from mcp_servers._envelope import make_error, make_response
from mcp_servers._schemas import OBJECT, RECORDS, envelope_schema, output_model

mcp = MCPServer("dalton_sec")


def _extended_sec_tools_enabled() -> bool:
    """Return True only when the backend explicitly enables Batch 3 extended SEC tools.

    Fail-closed by design: sandboxes do not ship ``src.dalton``, so the server
    never tries to guess the deployment profile. The backend decides via
    ``src.dalton.capabilities.is_batch3_connectors_enabled()`` and injects
    ``DALTON_SEC_EXTENDED_TOOLS=1`` into the MCP server environment only when
    enabled. When unset, only the 2 baseline tools (``get_company_facts`` and
    pre-Batch-3 ``list_filings``) are registered.
    """
    return os.environ.get("DALTON_SEC_EXTENDED_TOOLS", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _extended_sec_tool():
    """Register an extended Batch 3 SEC tool on ``mcp`` only when enabled for this profile/cutover."""
    if _extended_sec_tools_enabled():
        return mcp.tool()

    def _passthrough(fn):
        return fn

    return _passthrough

_FactsOut = output_model("DaltonSecFactsOutput", envelope_schema(RECORDS))
_FilingsOut = output_model("DaltonSecFilingsOutput", envelope_schema(RECORDS))
_ObjectOut = output_model("DaltonSecObjectOutput", envelope_schema(OBJECT))

_SEC_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_VALID_STATEMENTS: frozenset[str] = frozenset({"all", "income", "balance", "cash"})
_MAX_TEXT_CHARS = 500_000
_MAX_MATCHES_CAP = 100

# Built-in CIK map for common coverage tickers so offline/cached runs never block on ticker lookup
BUILTIN_TICKER_CIKS: dict[str, tuple[str, str]] = {
    "META": ("0001326801", "Meta Platforms, Inc."),
    "MSFT": ("0000789019", "MICROSOFT CORP"),
    "AMZN": ("0001018724", "AMAZON COM INC"),
    "GOOGL": ("0001652044", "Alphabet Inc."),
    "GOOG": ("0001652044", "Alphabet Inc."),
    "AAPL": ("0000320193", "Apple Inc."),
    "NVDA": ("0001045810", "NVIDIA CORP"),
    "TSLA": ("0001318605", "Tesla, Inc."),
    "AVGO": ("0001730168", "Broadcom Inc."),
    "ORCL": ("0001341439", "ORACLE CORP"),
    "CRM": ("0001108524", "Salesforce, Inc."),
    "NFLX": ("0001065280", "NETFLIX INC"),
    "AMD": ("0000002488", "ADVANCED MICRO DEVICES INC"),
    "INTC": ("0000050863", "INTEL CORP"),
    "TSM": ("0001046179", "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"),
    "BABA": ("0001577552", "Alibaba Group Holding Limited"),
    "PDD": ("0001737806", "PDD Holdings Inc."),
    "JD": ("0001549802", "JD.com, Inc."),
    "BIDU": ("0001329099", "Baidu, Inc."),
}

# Canonical concept alias groups (ordered by modern US-GAAP prevalence)
CONCEPT_ALIASES: dict[str, list[str]] = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "RevenuesNetOfInterestExpense",
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "operating_income": [
        "OperatingIncomeLoss",
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "eps_diluted": [
        "EarningsPerShareDiluted",
    ],
    "eps_basic": [
        "EarningsPerShareBasic",
    ],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "r_and_d": [
        "ResearchAndDevelopmentExpense",
    ],
    "sga": [
        "SellingGeneralAndAdministrativeExpense",
    ],
    "share_based_compensation": [
        "ShareBasedCompensation",
        "AllocatedShareBasedCompensationExpense",
    ],
    "total_assets": [
        "Assets",
    ],
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "long_term_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
    ],
}

# Reverse map so passing e.g. "Revenues" can fall back to sibling revenue tags if needed
_CONCEPT_SIBLINGS: dict[str, list[str]] = {}
for _alias, _cands in CONCEPT_ALIASES.items():
    for _c in _cands:
        _CONCEPT_SIBLINGS[_c] = _cands

VALID_SEC_FORMS: frozenset[str] = frozenset(
    {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "6-K", "6-K/A", "40-F", "40-F/A", "8-K", "8-K/A"}
)

_LAST_HTTP_TS: float = 0.0


def _cache_dir() -> Path:
    d = Path(os.environ.get("DALTON_SEC_CACHE_DIR", os.path.expanduser("~/.cache/dalton/sec")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sec_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    return ua if ua else "DaltonResearch research@dalton.local"


def _http_get_json(url: str, cache_key: str, ttl_seconds: int = 86400) -> dict[str, Any]:
    global _LAST_HTTP_TS
    cache_file = _cache_dir() / f"{cache_key}.json"
    now = time.time()
    if cache_file.is_file():
        try:
            if now - cache_file.stat().st_mtime < ttl_seconds:
                return json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Rate limit <= 5 req/s
    elapsed = now - _LAST_HTTP_TS
    if elapsed < 0.21:
        time.sleep(0.21 - elapsed)

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _sec_user_agent(),
            "Accept": "application/json",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw_bytes = resp.read()
    _LAST_HTTP_TS = time.time()
    parsed = json.loads(raw_bytes.decode("utf-8"))
    try:
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(parsed), encoding="utf-8")
        tmp.replace(cache_file)
    except Exception:
        pass
    return parsed


def resolve_cik(ticker_or_cik: str) -> tuple[str, str]:
    """Return `(cik10, company_name)` for a ticker or numeric CIK."""
    cleaned = ticker_or_cik.strip().upper()
    if cleaned.isdigit() or (cleaned.startswith("CIK") and cleaned[3:].isdigit()):
        digits = cleaned[3:] if cleaned.startswith("CIK") else cleaned
        return digits.zfill(10), cleaned

    if cleaned in BUILTIN_TICKER_CIKS:
        return BUILTIN_TICKER_CIKS[cleaned]

    data = _http_get_json(
        "https://www.sec.gov/files/company_tickers.json",
        "company_tickers",
        ttl_seconds=7 * 86400,
    )
    items = data.values() if isinstance(data, dict) else data
    for entry in items:
        if isinstance(entry, dict) and str(entry.get("ticker") or "").upper() == cleaned:
            cik10 = str(entry.get("cik_str") or "").zfill(10)
            title = str(entry.get("title") or cleaned)
            return cik10, title
    raise ValueError(f"Unknown ticker or CIK: {ticker_or_cik}")


def fetch_submissions(cik10: str) -> dict[str, Any]:
    return _http_get_json(
        f"https://data.sec.gov/submissions/CIK{cik10}.json",
        f"sub_CIK{cik10}",
        ttl_seconds=86400,
    )


def fetch_company_facts(cik10: str) -> dict[str, Any]:
    return _http_get_json(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json",
        f"facts_CIK{cik10}",
        ttl_seconds=86400,
    )


def _parse_iso_date(s: str | None) -> date | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


def _effective_year_month(end_d: date) -> tuple[int, int]:
    """Normalize 52/53-week period end dates that spill up to 6 days into the next month."""
    if end_d.day <= 6:
        shifted = end_d - timedelta(days=7)
        return shifted.year, shifted.month
    return end_d.year, end_d.month


def _infer_fye_month(
    facts_json: dict[str, Any], submissions_json: dict[str, Any] | None = None
) -> int:
    """Infer fiscal year-end month (1..12) from submissions or 10-K/20-F annual facts."""
    if isinstance(submissions_json, dict):
        fye_str = str(submissions_json.get("fiscalYearEnd") or "").strip()
        if len(fye_str) == 4 and fye_str[:2].isdigit():
            m = int(fye_str[:2])
            if 1 <= m <= 12:
                return m

    # Scan 10-K / 20-F full-year duration facts in us-gaap / ifrs-full
    facts_root = facts_json.get("facts") if isinstance(facts_json, dict) else {}
    if not isinstance(facts_root, dict):
        return 12
    month_counts: dict[int, int] = {}
    for tax in ("us-gaap", "ifrs-full", "dei"):
        tax_obj = facts_root.get(tax)
        if not isinstance(tax_obj, dict):
            continue
        for c_idx, c_data in enumerate(tax_obj.values()):
            if c_idx > 25 or not isinstance(c_data, dict):
                break
            units = c_data.get("units")
            if not isinstance(units, dict):
                continue
            for arr in units.values():
                if not isinstance(arr, list):
                    continue
                for row in arr:
                    if not isinstance(row, dict):
                        continue
                    form = str(row.get("form") or "")
                    if form not in ("10-K", "20-F"):
                        continue
                    end_d = _parse_iso_date(row.get("end"))
                    start_d = _parse_iso_date(row.get("start"))
                    if end_d and start_d and 350 <= (end_d - start_d).days <= 380:
                        _, em = _effective_year_month(end_d)
                        month_counts[em] = month_counts.get(em, 0) + 1
    if month_counts:
        return max(month_counts.items(), key=lambda kv: kv[1])[0]
    return 12


def _cal_and_fiscal_period(eff_year: int, eff_month: int, fye_month: int) -> tuple[int, int, int, int]:
    """Return `(cal_year, cal_q, fy_year, fy_q)` for an effective `(year, month)` and `fye_month`."""
    cal_year = eff_year
    cal_q = (eff_month - 1) // 3 + 1
    fy_year = eff_year + 1 if eff_month > fye_month else eff_year
    offset_m = (eff_month - fye_month - 1) % 12 + 1
    fy_q = (offset_m - 1) // 3 + 1
    return cal_year, cal_q, fy_year, fy_q


def _edgar_archive_url(cik10: str, accession_no: str) -> str | None:
    if not cik10 or not accession_no:
        return None
    cik_int = str(int(cik10)) if cik10.isdigit() else cik10.lstrip("0")
    acc_no_hyphens = accession_no.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_hyphens}/{accession_no}-index.htm"


def _make_sec_source(
    ticker: str, cik10: str, form: str, filed: str, accession_no: str, period_end: str = ""
) -> dict[str, Any]:
    doc_id = f"sec:{ticker}:{accession_no or filed}"
    title_parts = [ticker, form or "Filing"]
    if period_end:
        title_parts.append(f"(period {period_end})")
    title = " ".join(title_parts)
    url = _edgar_archive_url(cik10, accession_no) if accession_no else None
    return {
        "document_id": doc_id[:200],
        "identifier": doc_id[:200],
        "route": "dalton_sec",
        "source_type": "sec_filing",
        "identity": "filing",
        "detail": "filing",
        "tier": 1,
        "publisher": "SEC EDGAR",
        "provider": "dalton_sec:SEC EDGAR",
        "title": title[:300],
        "as_of": filed[:10],
        "as_of_basis": "filed",
        "url": url,
        "private": False,
        "independent": True,
    }


_PERIOD_PATTERNS = {
    "cal_q": re.compile(r"^(\d{4})\s*Q([1-4])$", re.IGNORECASE),
    "fy_q": re.compile(r"^FY\s*(\d{4})\s*Q([1-4])$", re.IGNORECASE),
    "fy": re.compile(r"^(?:FY\s*)?(\d{4})$", re.IGNORECASE),
    "h1": re.compile(r"^(?:(FY)\s*)?(\d{4})\s*H1$|^H1\s*(?:(FY)\s*)?(\d{4})$", re.IGNORECASE),
    "m9": re.compile(r"^(?:(FY)\s*)?(\d{4})\s*9M$|^9M\s*(?:(FY)\s*)?(\d{4})$", re.IGNORECASE),
    "trailing": re.compile(r"^trailing\s*:\s*(\d+)\s*([qy])$", re.IGNORECASE),
}


def parse_period_spec(period: str) -> dict[str, Any]:
    """Parse period string into a structured query spec."""
    p = (period or "latest_quarter").strip()
    low = p.lower()
    if low in ("latest_quarter", "latest_q", "latest"):
        return {"mode": "latest_q"}
    if low in ("latest_annual", "latest_fy", "latest_y"):
        return {"mode": "latest_fy"}

    m = _PERIOD_PATTERNS["trailing"].match(p)
    if m:
        count = max(1, min(40, int(m.group(1))))
        unit = m.group(2).lower()
        return {"mode": "trailing_q" if unit == "q" else "trailing_fy", "count": count}

    m = _PERIOD_PATTERNS["cal_q"].match(p)
    if m:
        return {"mode": "cal_q", "year": int(m.group(1)), "q": int(m.group(2))}

    m = _PERIOD_PATTERNS["fy_q"].match(p)
    if m:
        return {"mode": "fy_q", "year": int(m.group(1)), "q": int(m.group(2))}

    m = _PERIOD_PATTERNS["h1"].match(p)
    if m:
        is_fy = bool(m.group(1) or m.group(3))
        yr = int(m.group(2) or m.group(4))
        return {"mode": "h1", "year": yr, "is_fy": is_fy}

    m = _PERIOD_PATTERNS["m9"].match(p)
    if m:
        is_fy = bool(m.group(1) or m.group(3))
        yr = int(m.group(2) or m.group(4))
        return {"mode": "m9", "year": yr, "is_fy": is_fy}

    m = _PERIOD_PATTERNS["fy"].match(p)
    if m:
        return {"mode": "fy", "year": int(m.group(1))}

    raise ValueError(
        f"Invalid period '{period}'. Supported: YYYYQ[1-4], FY<YYYY>Q[1-4], FY<YYYY>, H1 <YYYY>, 9M <YYYY>, latest_quarter, latest_annual, trailing:<N>q, trailing:<N>y."
    )


def _select_unit(units_dict: dict[str, list[dict[str, Any]]]) -> tuple[str, list[dict[str, Any]]]:
    for pref in ("USD", "USD/shares", "shares", "pure"):
        if pref in units_dict and isinstance(units_dict[pref], list) and units_dict[pref]:
            return pref, units_dict[pref]
    for u, rows in units_dict.items():
        if isinstance(rows, list) and rows:
            return u, rows
    return "", []


def _classify_fact_rows(
    raw_rows: list[dict[str, Any]],
    unit: str,
    fye_month: int,
    as_of: str | None,
) -> dict[tuple[str, int, int], list[dict[str, Any]]]:
    """Group valid fact rows by `(kind, eff_year, eff_month)` after applying `as_of` cutoff."""
    grouped: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for r in raw_rows:
        if not isinstance(r, dict) or r.get("val") is None:
            continue
        form = str(r.get("form") or "").strip()
        if form and form not in VALID_SEC_FORMS:
            continue
        filed = str(r.get("filed") or "").strip()[:10]
        if not filed:
            continue
        if as_of and filed > as_of[:10]:
            continue
        end_d = _parse_iso_date(r.get("end"))
        if not end_d:
            continue
        eff_y, eff_m = _effective_year_month(end_d)
        cal_y, cal_q, fy_y, fy_q = _cal_and_fiscal_period(eff_y, eff_m, fye_month)

        start_str = r.get("start")
        if start_str is None:
            kind = "instant"
            days = 0
        else:
            start_d = _parse_iso_date(start_str)
            if not start_d:
                continue
            days = (end_d - start_d).days
            if 75 <= days <= 105:
                kind = "1q"
            elif 165 <= days <= 195:
                kind = "2q"
            elif 255 <= days <= 285:
                kind = "3q"
            elif 350 <= days <= 380:
                kind = "4q"
            else:
                continue

        norm_row = {
            "val": r["val"],
            "unit": unit,
            "start": str(start_str)[:10] if start_str else None,
            "end": str(r.get("end"))[:10],
            "duration_days": days if start_str else None,
            "kind": kind,
            "eff_year": eff_y,
            "eff_month": eff_m,
            "calendar_period": f"{cal_y}Q{cal_q}" if kind in ("1q", "instant") else (
                f"H1 {cal_y}" if kind == "2q" else (f"9M {cal_y}" if kind == "3q" else f"{cal_y}")
            ),
            "fiscal_period": f"FY{fy_y}Q{fy_q}" if kind in ("1q", "instant") else (
                f"FY{fy_y}H1" if kind == "2q" else (f"FY{fy_y}9M" if kind == "3q" else f"FY{fy_y}")
            ),
            "cal_year": cal_y,
            "cal_q": cal_q,
            "fy_year": fy_y,
            "fy_q": fy_q,
            "form": form,
            "filed": filed,
            "accession_no": str(r.get("accn") or "").strip(),
            "raw_fy": r.get("fy"),
            "raw_fp": r.get("fp"),
            "frame": r.get("frame"),
            "derived": False,
        }
        key = (kind, eff_y, eff_m)
        grouped.setdefault(key, []).append(norm_row)

    return grouped


def _resolve_restatement(
    candidates: list[dict[str, Any]], restatement: str
) -> dict[str, Any]:
    """Apply R7 (`as_reported` vs `latest`) to a period's candidate rows."""
    # Sort by (filed, accession_no); if same filing has duplicate rows, prefer the one with a frame tag
    ordered = sorted(
        candidates,
        key=lambda x: (x["filed"], x["accession_no"], 1 if x.get("frame") else 0),
    )
    # Deduplicate within the exact same (filed, accession_no) keeping the last (framed) entry
    by_filing: list[dict[str, Any]] = []
    for item in ordered:
        if (
            by_filing
            and by_filing[-1]["filed"] == item["filed"]
            and by_filing[-1]["accession_no"] == item["accession_no"]
        ):
            by_filing[-1] = item
        else:
            by_filing.append(item)

    earliest = dict(by_filing[0])
    latest = dict(by_filing[-1])
    is_restated = earliest["val"] != latest["val"]

    if restatement == "latest":
        chosen = latest
        chosen["restatement_mode"] = "latest"
        chosen["restated"] = is_restated
        chosen["as_reported_val"] = earliest["val"]
        chosen["as_reported_filed"] = earliest["filed"]
        chosen["as_reported_accession_no"] = earliest["accession_no"]
    else:
        chosen = earliest
        chosen["restatement_mode"] = "as_reported"
        chosen["restated_in_later_filing"] = is_restated
        if is_restated:
            chosen["latest_val"] = latest["val"]
            chosen["latest_filed"] = latest["filed"]
            chosen["latest_accession_no"] = latest["accession_no"]

    return chosen


_YTD_DERIVATION_SPECS: dict[int, tuple[str, int, str, str, int, str, str]] = {
    2: ("2q", 2, "H1", "1q", 1, "Q1", "H1_minus_Q1"),
    3: ("3q", 3, "9M", "2q", 2, "H1", "9M_minus_H1"),
    4: ("4q", 4, "", "3q", 3, "9M", "FY_minus_9M"),
}


def _try_derive_quarter(
    grouped: dict[tuple[str, int, int], list[dict[str, Any]]],
    target_fy_year: int,
    target_fy_q: int,
    fye_month: int,
    restatement: str,
) -> dict[str, Any] | None:
    """Derive standalone quarter (Q2=H1-Q1, Q3=9M-H1, Q4=FY-9M) when direct 1q fact is absent (Rule R4)."""
    spec_tuple = _YTD_DERIVATION_SPECS.get(target_fy_q)
    if not spec_tuple:
        return None
    up_kind, up_q, up_suffix, low_kind, low_q, low_suffix, method_name = spec_tuple

    up_candidates: list[dict[str, Any]] = []
    low_candidates: list[dict[str, Any]] = []
    for (kind, _ey, _em), rows in grouped.items():
        if not rows:
            continue
        sample = rows[0]
        if sample["fy_year"] != target_fy_year:
            continue
        if kind == up_kind and sample["fy_q"] == up_q:
            up_candidates.extend(rows)
        elif kind == low_kind and sample["fy_q"] == low_q:
            low_candidates.extend(rows)

    if not up_candidates or not low_candidates:
        return None

    up_fact = _resolve_restatement(up_candidates, restatement)
    low_fact = _resolve_restatement(low_candidates, restatement)

    # Do not derive per-share ratios (EPS) via simple subtraction if diluted share counts differ
    if "shares" in str(up_fact.get("unit") or "").lower() and "/" in str(up_fact.get("unit") or ""):
        return None

    up_end = _parse_iso_date(up_fact["end"])
    low_end = _parse_iso_date(low_fact["end"])
    if not up_end or not low_end:
        return None
    diff_days = (up_end - low_end).days
    if not (75 <= diff_days <= 105):
        return None

    up_val = up_fact["val"]
    low_val = low_fact["val"]
    derived_val = (
        round(up_val - low_val, 6)
        if isinstance(up_val, float) or isinstance(low_val, float)
        else (up_val - low_val)
    )
    cal_y, cal_q, fy_y, _ = _cal_and_fiscal_period(
        up_fact["eff_year"], up_fact["eff_month"], fye_month
    )
    return {
        "val": derived_val,
        "unit": up_fact["unit"],
        "start": low_fact["end"],
        "end": up_fact["end"],
        "duration_days": diff_days,
        "kind": "1q",
        "eff_year": up_fact["eff_year"],
        "eff_month": up_fact["eff_month"],
        "calendar_period": f"{cal_y}Q{cal_q}",
        "fiscal_period": f"FY{fy_y}Q{target_fy_q}",
        "cal_year": cal_y,
        "cal_q": cal_q,
        "fy_year": fy_y,
        "fy_q": target_fy_q,
        "form": up_fact["form"],
        "filed": max(up_fact["filed"], low_fact["filed"]),
        "accession_no": up_fact["accession_no"],
        "restatement_mode": restatement,
        "derived": True,
        "derivation_method": method_name,
        "components": [
            {
                "period": f"FY{target_fy_year}{up_suffix}",
                "val": up_val,
                "start": up_fact["start"],
                "end": up_fact["end"],
                "form": up_fact["form"],
                "filed": up_fact["filed"],
                "accession_no": up_fact["accession_no"],
            },
            {
                "period": f"FY{target_fy_year}{low_suffix}",
                "val": low_val,
                "start": low_fact["start"],
                "end": low_fact["end"],
                "form": low_fact["form"],
                "filed": low_fact["filed"],
                "accession_no": low_fact["accession_no"],
            },
        ],
    }


def _try_derive_q4(
    grouped: dict[tuple[str, int, int], list[dict[str, Any]]],
    target_fy_year: int,
    fye_month: int,
    restatement: str,
) -> dict[str, Any] | None:
    return _try_derive_quarter(grouped, target_fy_year, 4, fye_month, restatement)


def _match_period_for_concept(
    grouped: dict[tuple[str, int, int], list[dict[str, Any]]],
    spec: dict[str, Any],
    fye_month: int,
    restatement: str,
    allow_derived_q4: bool = True,
) -> list[dict[str, Any]]:
    """Match normalized fact groups against `spec`, applying R4 (derived quarter) when needed."""
    mode = spec["mode"]
    has_duration = any(k != "instant" for (k, _, _) in grouped)
    is_instant_only = not has_duration and any(k == "instant" for (k, _, _) in grouped)

    def _collect(predicate) -> list[dict[str, Any]]:
        matched_groups: list[dict[str, Any]] = []
        for (_k, _ey, _em), rows in sorted(grouped.items(), key=lambda kv: (kv[0][1], kv[0][2])):
            if rows and predicate(rows[0]):
                matched_groups.append(_resolve_restatement(rows, restatement))
        return matched_groups

    if mode == "cal_q":
        ty, tq = spec["year"], spec["q"]
        target_kind = "instant" if is_instant_only else "1q"
        hits = _collect(lambda s: s["kind"] == target_kind and s["cal_year"] == ty and s["cal_q"] == tq)
        if not hits and not is_instant_only and allow_derived_q4:
            target_eff_m = tq * 3
            _, _, target_fy_y, target_fy_q = _cal_and_fiscal_period(ty, target_eff_m, fye_month)
            if target_fy_q in (2, 3, 4):
                derived = _try_derive_quarter(grouped, target_fy_y, target_fy_q, fye_month, restatement)
                if derived:
                    hits = [derived]
        return hits[-1:] if hits else []

    if mode == "fy_q":
        ty, tq = spec["year"], spec["q"]
        target_kind = "instant" if is_instant_only else "1q"
        hits = _collect(lambda s: s["kind"] == target_kind and s["fy_year"] == ty and s["fy_q"] == tq)
        if not hits and not is_instant_only and tq in (2, 3, 4) and allow_derived_q4:
            derived = _try_derive_quarter(grouped, ty, tq, fye_month, restatement)
            if derived:
                hits = [derived]
        return hits[-1:] if hits else []

    if mode == "fy":
        ty = spec["year"]
        if is_instant_only:
            hits = _collect(lambda s: s["kind"] == "instant" and s["fy_year"] == ty and s["fy_q"] == 4)
        else:
            hits = _collect(lambda s: s["kind"] == "4q" and s["fy_year"] == ty and s["fy_q"] == 4)
        return hits[-1:] if hits else []

    if mode == "h1":
        ty, is_fy = spec["year"], spec["is_fy"]
        target_kind = "instant" if is_instant_only else "2q"
        hits = _collect(
            lambda s: s["kind"] == target_kind
            and ((s["fy_year"] == ty and s["fy_q"] == 2) if is_fy else (s["cal_year"] == ty and s["cal_q"] == 2))
        )
        return hits[-1:] if hits else []

    if mode == "m9":
        ty, is_fy = spec["year"], spec["is_fy"]
        target_kind = "instant" if is_instant_only else "3q"
        hits = _collect(
            lambda s: s["kind"] == target_kind
            and ((s["fy_year"] == ty and s["fy_q"] == 3) if is_fy else (s["cal_year"] == ty and s["cal_q"] == 3))
        )
        return hits[-1:] if hits else []

    if mode in ("latest_q", "trailing_q"):
        count = 1 if mode == "latest_q" else spec["count"]
        target_kind = "instant" if is_instant_only else "1q"
        hits = _collect(lambda s: s["kind"] == target_kind)
        if not is_instant_only and allow_derived_q4:
            existing_fy_q = {(h["fy_year"], h["fy_q"]) for h in hits}
            fy_years = {rows[0]["fy_year"] for (_k, _, _), rows in grouped.items() if rows}
            for fy_y in sorted(fy_years):
                for q in (2, 3, 4):
                    if (fy_y, q) not in existing_fy_q:
                        d_q = _try_derive_quarter(grouped, fy_y, q, fye_month, restatement)
                        if d_q:
                            hits.append(d_q)
                            existing_fy_q.add((fy_y, q))
            hits.sort(key=lambda h: (h["eff_year"], h["eff_month"]))
        return hits[-count:]

    if mode in ("latest_fy", "trailing_fy"):
        count = 1 if mode == "latest_fy" else spec["count"]
        if is_instant_only:
            hits = _collect(lambda s: s["kind"] == "instant" and s["fy_q"] == 4)
        else:
            hits = _collect(lambda s: s["kind"] == "4q" and s["fy_q"] == 4)
        return hits[-count:]

    return []


def _find_concept_candidates(
    facts_root: dict[str, Any], requested: str
) -> tuple[list[tuple[str, str, dict[str, Any]]], list[str]]:
    """Return `(matched_candidates, fuzzy_suggestions)` for a requested concept or alias."""
    req_clean = requested.strip()
    low = req_clean.lower()

    # Build lookup of all concepts across taxonomies
    all_concepts: dict[str, tuple[str, str, dict[str, Any]]] = {}
    bare_to_tax: dict[str, tuple[str, str, dict[str, Any]]] = {}
    for tax in ("us-gaap", "ifrs-full", "dei"):
        tax_dict = facts_root.get(tax)
        if not isinstance(tax_dict, dict):
            continue
        for cname, cobj in tax_dict.items():
            if not isinstance(cobj, dict):
                continue
            full_name = f"{tax}:{cname}"
            all_concepts[full_name.lower()] = (tax, cname, cobj)
            if cname.lower() not in bare_to_tax:
                bare_to_tax[cname.lower()] = (tax, cname, cobj)

    candidates: list[tuple[str, str, dict[str, Any]]] = []
    seen: set[str] = set()

    def _add(entry: tuple[str, str, dict[str, Any]] | None) -> None:
        if entry:
            key = f"{entry[0]}:{entry[1]}"
            if key not in seen:
                seen.add(key)
                candidates.append(entry)

    # 1. Check alias table
    if low in CONCEPT_ALIASES:
        for c in CONCEPT_ALIASES[low]:
            _add(bare_to_tax.get(c.lower()))

    # 2. Check exact taxonomy:Concept or bare Concept
    if ":" in req_clean:
        _add(all_concepts.get(low))
        bare_part = req_clean.split(":", 1)[1]
    else:
        _add(bare_to_tax.get(low))
        bare_part = req_clean

    # 3. If bare_part has known siblings (e.g. Revenues <-> RevenueFromContractWithCustomerExcludingAssessedTax), add as fallback
    if bare_part in _CONCEPT_SIBLINGS:
        for sib in _CONCEPT_SIBLINGS[bare_part]:
            _add(bare_to_tax.get(sib.lower()))

    if candidates:
        return candidates, []

    # Not found -> compute fuzzy suggestions across bare concept names
    all_bare_names = [v[1] for v in bare_to_tax.values()]
    suggestions = difflib.get_close_matches(bare_part, all_bare_names, n=5, cutoff=0.45)
    if not suggestions:
        tokens = [
            t.lower()
            for t in re.findall(r"[A-Z][a-z]+|[a-z]+", bare_part)
            if len(t) >= 4 and t.lower() not in ("non", "existent", "concept")
        ]
        sub_hits = [
            n
            for n in all_bare_names
            if bare_part.lower() in n.lower() or any(tok in n.lower() for tok in tokens)
        ][:5]
        suggestions = sub_hits or difflib.get_close_matches(bare_part, all_bare_names, n=5, cutoff=0.2)
    return [], suggestions


def normalize_company_facts(
    facts_json: dict[str, Any],
    ticker: str,
    concepts: list[str] | str,
    period: str = "latest_quarter",
    restatement: str = "as_reported",
    as_of: str | None = None,
    fiscal_year_end_month: int | None = None,
    submissions_json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure normalization function usable both by MCP tool and unit tests."""
    if restatement not in ("as_reported", "latest"):
        raise ValueError("restatement must be 'as_reported' or 'latest'")

    spec = parse_period_spec(period)
    if isinstance(concepts, str):
        concept_list = [c.strip() for c in concepts.split(",") if c.strip()]
    elif isinstance(concepts, list):
        concept_list = [str(c).strip() for c in concepts if str(c).strip()]
    else:
        concept_list = []
    if not concept_list:
        raise ValueError("At least one concept is required")

    cik_raw = str(facts_json.get("cik") or "")
    cik10 = cik_raw.zfill(10) if cik_raw else ""
    entity_name = str(facts_json.get("entityName") or ticker)
    fye_month = (
        int(fiscal_year_end_month)
        if fiscal_year_end_month and 1 <= int(fiscal_year_end_month) <= 12
        else _infer_fye_month(facts_json, submissions_json)
    )

    facts_root = facts_json.get("facts") if isinstance(facts_json, dict) else {}
    if not isinstance(facts_root, dict):
        facts_root = {}

    results_by_concept: dict[str, Any] = {}
    declared_sources: list[dict[str, Any]] = []
    seen_accessions: set[str] = set()

    def _register_source(form: str, filed: str, accn: str, end_date: str) -> None:
        key = accn or f"{form}:{filed}"
        if not key or key in seen_accessions:
            return
        seen_accessions.add(key)
        if len(declared_sources) < 50:
            declared_sources.append(
                _make_sec_source(ticker.upper(), cik10, form, filed, accn, end_date)
            )

    for req_concept in concept_list:
        candidates, suggestions = _find_concept_candidates(facts_root, req_concept)
        if not candidates:
            results_by_concept[req_concept] = {
                "status": "not_found",
                "requested_concept": req_concept,
                "error": f"Concept '{req_concept}' not found in SEC XBRL facts for {ticker.upper()}.",
                "suggestions": suggestions,
            }
            continue

        chosen_tax = ""
        chosen_name = ""
        chosen_label = ""
        matched_facts: list[dict[str, Any]] = []

        # Try candidates in priority order; for trailing queries, merge across concept transitions if needed
        if spec["mode"] in ("trailing_q", "trailing_fy"):
            merged_by_period: dict[tuple[int, int], dict[str, Any]] = {}
            for tax, cname, cobj in reversed(candidates):
                unit, raw_rows = _select_unit(cobj.get("units") or {})
                if not raw_rows:
                    continue
                grouped = _classify_fact_rows(raw_rows, unit, fye_month, as_of)
                hits = _match_period_for_concept(grouped, spec, fye_month, restatement)
                for h in hits:
                    h["concept"] = f"{tax}:{cname}"
                    merged_by_period[(h["eff_year"], h["eff_month"])] = h
                if hits:
                    chosen_tax, chosen_name = tax, cname
                    chosen_label = str(cobj.get("label") or cname)
            all_hits = [merged_by_period[k] for k in sorted(merged_by_period)]
            matched_facts = all_hits[-spec["count"] :]
        else:
            for tax, cname, cobj in candidates:
                unit, raw_rows = _select_unit(cobj.get("units") or {})
                if not raw_rows:
                    continue
                grouped = _classify_fact_rows(raw_rows, unit, fye_month, as_of)
                hits = _match_period_for_concept(grouped, spec, fye_month, restatement)
                if hits:
                    chosen_tax, chosen_name = tax, cname
                    chosen_label = str(cobj.get("label") or cname)
                    for h in hits:
                        h["concept"] = f"{tax}:{cname}"
                    matched_facts = hits
                    break

        if not matched_facts:
            tax0, cname0, cobj0 = candidates[0]
            results_by_concept[req_concept] = {
                "status": "empty_period",
                "requested_concept": req_concept,
                "resolved_concept": f"{tax0}:{cname0}",
                "label": str(cobj0.get("label") or cname0),
                "period_requested": period,
                "facts": [],
            }
            continue

        for f in matched_facts:
            _register_source(
                str(f.get("form") or ""),
                str(f.get("filed") or ""),
                str(f.get("accession_no") or ""),
                str(f.get("end") or ""),
            )
            for comp in f.get("components") or []:
                if isinstance(comp, dict):
                    _register_source(
                        str(comp.get("form") or ""),
                        str(comp.get("filed") or ""),
                        str(comp.get("accession_no") or ""),
                        str(comp.get("end") or ""),
                    )

        primary = matched_facts[-1]
        concept_entry: dict[str, Any] = {
            "status": "ok",
            "requested_concept": req_concept,
            "resolved_concept": f"{chosen_tax}:{chosen_name}",
            "label": chosen_label,
            "period_requested": period,
            "calendar_period": primary["calendar_period"],
            "fiscal_period": primary["fiscal_period"],
            "val": primary["val"],
            "unit": primary["unit"],
            "start": primary["start"],
            "end": primary["end"],
            "duration_days": primary["duration_days"],
            "form": primary["form"],
            "filed": primary["filed"],
            "accession_no": primary["accession_no"],
            "derived": primary.get("derived", False),
            "derivation_method": primary.get("derivation_method"),
            "components": primary.get("components"),
            "restatement_mode": restatement,
            "restated": primary.get("restated", primary.get("restated_in_later_filing", False)),
            "facts": matched_facts,
        }
        for extra_key in (
            "restated_in_later_filing",
            "latest_val",
            "latest_filed",
            "latest_accession_no",
            "as_reported_val",
            "as_reported_filed",
            "as_reported_accession_no",
        ):
            if extra_key in primary:
                concept_entry[extra_key] = primary[extra_key]
        results_by_concept[req_concept] = concept_entry

    return {
        "_declared_sources": declared_sources,
        "_sources": declared_sources,
        "ticker": ticker.upper(),
        "cik": cik10,
        "entity_name": entity_name,
        "fiscal_year_end_month": fye_month,
        "period_requested": period,
        "restatement_mode": restatement,
        "as_of": as_of,
        "concepts": results_by_concept,
    }


@mcp.tool()
def get_company_facts(
    ticker: str,
    concepts: list[str] | str,
    period: str = "latest_quarter",
    restatement: str = "as_reported",
    as_of: str | None = None,
    fiscal_year_end_month: int | None = None,
) -> _FactsOut:
    """Fetch period-normalized SEC XBRL facts with restatement and derived-Q4 support.

    Supports calendar (2025Q2), fiscal (FY2026Q1), annual (FY2025), YTD (H1 2025,
    9M 2025), latest_quarter, latest_annual, and trailing:8q/3y periods. Derives Q4
    flow facts as FY minus 9M when a standalone Q4 fact is absent.

    Args:
        ticker: Stock ticker symbol or CIK (e.g. META, MSFT, AMZN).
        concepts: XBRL concept name(s) or alias(es), comma-separated or list.
        period: Target period (default 'latest_quarter').
        restatement: 'as_reported' (earliest filing) or 'latest' (most recent).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.
        fiscal_year_end_month: Optional fiscal year-end month override (1-12).

    Returns:
        dict: Normalized facts per concept with period, value, unit, and _declared_sources.
    """
    try:
        cik10, _ = resolve_cik(ticker)
        facts_json = fetch_company_facts(cik10)
        sub_json: dict[str, Any] | None = None
        if fiscal_year_end_month is None:
            try:
                sub_json = fetch_submissions(cik10)
            except Exception:
                sub_json = None

        normalized = normalize_company_facts(
            facts_json=facts_json,
            ticker=ticker,
            concepts=concepts,
            period=period,
            restatement=restatement,
            as_of=as_of,
            fiscal_year_end_month=fiscal_year_end_month,
            submissions_json=sub_json,
        )
        env = make_response(
            data=[normalized],
            source="dalton_sec",
            symbol=ticker.upper(),
            period=period,
        )
        # Put _declared_sources and _sources at the top of the envelope for ProvenanceMiddleware
        return {
            "_declared_sources": normalized["_declared_sources"],
            "_sources": normalized["_sources"],
            **env,
        }
    except Exception as exc:
        return make_error("sec_error", str(exc), symbol=ticker)


def _validate_as_of(as_of: str | None) -> str | None:
    if as_of is None or not str(as_of).strip():
        return None
    cleaned = str(as_of).strip()
    try:
        parsed = date.fromisoformat(cleaned)
    except ValueError as exc:
        raise ValueError("as_of must be YYYY-MM-DD") from exc
    if parsed.isoformat() != cleaned or parsed > date.today():
        raise ValueError("as_of must be YYYY-MM-DD and no later than today")
    return cleaned


def _attach_sources(payload: dict[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any]:
    """Attach non-empty `_sources` and `_declared_sources` at top level (omit empty `_declared_sources`)."""
    out = dict(payload)
    if sources:
        out = {
            "_declared_sources": sources,
            "_sources": sources,
            **out,
        }
    return out


def _get_edgar_company(ticker: str) -> Any:
    """Lazy-import `edgartools` and return `Company(ticker)` with configured SEC identity."""
    from edgar import Company, configure_http, set_identity

    try:
        configure_http(use_system_certs=True)
    except Exception:
        pass
    set_identity(_sec_user_agent())
    return Company(ticker)


def _edgar_filings(company: Any, *, form: str | None = None, as_of: str | None = None) -> Any:
    filings = company.get_filings(form=form) if form else company.get_filings()
    if as_of:
        filings = filings.filter(filing_date=f":{as_of}")
    return filings


def _extract_company_facts_flat_rows(
    facts_json: dict[str, Any],
    *,
    as_of: str | None = None,
) -> list[dict[str, Any]]:
    """Flatten SEC EDGAR `companyfacts` JSON into point-in-time rows matching `point_in_time.select_fact_rows`."""
    facts_root = facts_json.get("facts") if isinstance(facts_json, dict) else {}
    if not isinstance(facts_root, dict):
        return []
    out: list[dict[str, Any]] = []
    for tax, tax_obj in facts_root.items():
        if not isinstance(tax_obj, dict):
            continue
        for cname, cdata in tax_obj.items():
            if not isinstance(cdata, dict):
                continue
            full_concept = f"{tax}:{cname}"
            label = str(cdata.get("label") or cname)
            units = cdata.get("units")
            if not isinstance(units, dict):
                continue
            for unit_name, rows in units.items():
                if not isinstance(rows, list):
                    continue
                for r in rows:
                    if not isinstance(r, dict) or r.get("val") is None:
                        continue
                    filed = str(r.get("filed") or "").strip()[:10]
                    end_s = str(r.get("end") or "").strip()[:10]
                    start_s = str(r.get("start") or "").strip()[:10] if r.get("start") else None
                    if not end_s or _parse_iso_date(end_s) is None:
                        continue
                    if as_of:
                        if not filed or filed > as_of or end_s > as_of:
                            continue
                    end_d = _parse_iso_date(end_s)
                    assert end_d is not None
                    fy_raw = r.get("fy")
                    try:
                        fy_num: int | None = int(fy_raw) if fy_raw is not None else None
                    except (TypeError, ValueError):
                        fy_num = None
                    out.append(
                        {
                            "concept": full_concept,
                            "bare_concept": cname,
                            "label": label,
                            "period_start": start_s,
                            "period_end": end_s,
                            "derived_year": end_d.year,
                            "fiscal_year_raw": fy_num,
                            "fiscal_period": str(r.get("fp") or "") or None,
                            "numeric_value": r.get("val"),
                            "unit": str(unit_name),
                            "year_mismatch": bool(fy_num is not None and fy_num != end_d.year),
                            "filing_date": filed or None,
                            "accession": str(r.get("accn") or "").strip() or None,
                            "form_type": str(r.get("form") or "").strip() or None,
                        }
                    )
    return out


def select_raw_concept_rows(
    facts_json: dict[str, Any],
    *,
    concept: str,
    form: str = "10-Q",
    limit: int = 10,
    as_of: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Select raw time-series rows for a single XBRL `concept` (legacy `facts` semantics)."""
    as_of_iso = _validate_as_of(as_of)
    flat = _extract_company_facts_flat_rows(facts_json, as_of=as_of_iso)
    req = str(concept or "").strip()
    if not req:
        raise ValueError("concept is required")

    if ":" in req:
        matched = [r for r in flat if r["concept"] == req]
        resolved_concept = req
    else:
        exact_full = [r for r in flat if r["concept"] == f"us-gaap:{req}"]
        if exact_full:
            matched = exact_full
            resolved_concept = f"us-gaap:{req}"
        else:
            matched = [r for r in flat if r["bare_concept"] == req]
            resolved_concept = matched[0]["concept"] if matched else req

    if not matched:
        raise LookupError(f"Concept '{req}' not found. Use search_concepts first.")

    form_clean = str(form or "").strip().upper()
    if as_of_iso:
        if form_clean:
            allowed_forms = {form_clean, f"{form_clean}/A"}
            matched = [r for r in matched if str(r.get("form_type") or "").upper() in allowed_forms]
        matched.sort(key=lambda r: (str(r.get("filing_date") or ""), str(r.get("accession") or "")))
        dedup: dict[tuple[Any, ...], dict[str, Any]] = {}
        for r in matched:
            key = (r["concept"], r["period_start"], r["period_end"], r["unit"])
            dedup[key] = r
        selected = list(dedup.values())
    else:
        # Match legacy point_in_time.select_fact_rows(as_of=None): only prefer FY for 10-K,
        # without adding an extra form_type filter for other forms when as_of is omitted.
        if form_clean == "10-K":
            fy_rows = [r for r in matched if r.get("fiscal_period") == "FY"]
            if fy_rows:
                matched = fy_rows
        matched.sort(key=lambda r: (str(r.get("period_end") or ""), str(r.get("filing_date") or "")))
        dedup_end: dict[str, dict[str, Any]] = {}
        for r in matched:
            dedup_end[str(r["period_end"])] = r
        selected = list(dedup_end.values())

    selected.sort(key=lambda r: str(r.get("period_end") or ""), reverse=True)
    capped = max(1, min(100, int(limit)))
    rows_out = [
        {
            "period_start": r["period_start"],
            "period_end": r["period_end"],
            "derived_year": r["derived_year"],
            "fiscal_year_raw": r["fiscal_year_raw"],
            "fiscal_period": r["fiscal_period"],
            "numeric_value": r["numeric_value"],
            "unit": r["unit"],
            "year_mismatch": r["year_mismatch"],
            "filing_date": r["filing_date"],
            "accession": r["accession"],
            "form_type": r["form_type"],
        }
        for r in selected[:capped]
    ]
    return resolved_concept, rows_out


def _compile_concept_matcher(query: str) -> Any:
    """Match legacy `findata_cli.py` pandas `.str.contains(keyword, case=False, na=False)` (regex with safe fallback)."""
    raw = str(query or "").strip()
    try:
        pattern = re.compile(raw, re.IGNORECASE)
        return lambda text: bool(pattern.search(text))
    except re.error:
        low = raw.lower()
        return lambda text: low in text.lower()


def search_concepts_in_facts(
    facts_json: dict[str, Any],
    *,
    query: str,
    as_of: str | None = None,
) -> list[dict[str, str]]:
    """Search XBRL concepts by keyword or regex across concept name and label (legacy `search-concepts`)."""
    kw = str(query or "").strip()
    if not kw:
        raise ValueError("query is required")
    as_of_iso = _validate_as_of(as_of)
    flat = _extract_company_facts_flat_rows(facts_json, as_of=as_of_iso)
    matcher = _compile_concept_matcher(kw)
    seen: set[str] = set()
    hits: list[dict[str, str]] = []
    for r in flat:
        cname = r["concept"]
        if cname in seen:
            continue
        lbl = str(r.get("label") or "")
        if matcher(cname) or matcher(lbl):
            seen.add(cname)
            hits.append({"concept": cname, "label": lbl or cname})
    return hits


def search_smart_facts_in_facts(
    facts_json: dict[str, Any],
    *,
    ticker: str,
    query: str,
    form: str = "10-Q",
    limit: int = 5,
    as_of: str | None = None,
) -> dict[str, Any]:
    """One-step keyword -> best XBRL concept -> raw time-series rows (legacy `smart-facts`)."""
    kw = str(query or "").strip()
    if not kw:
        raise ValueError("query is required")
    as_of_iso = _validate_as_of(as_of)
    flat = _extract_company_facts_flat_rows(facts_json, as_of=as_of_iso)
    form_clean = str(form or "10-Q").strip().upper()
    scoped = flat
    if as_of_iso and form_clean:
        allowed_forms = {form_clean, f"{form_clean}/A"}
        scoped = [r for r in flat if str(r.get("form_type") or "").upper() in allowed_forms]

    kw_low = kw.lower()
    matcher = _compile_concept_matcher(kw)
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    ordered_concepts: list[str] = []
    for r in scoped:
        cname = r["concept"]
        lbl = str(r.get("label") or cname)
        if matcher(cname) or matcher(lbl):
            if cname not in counts:
                ordered_concepts.append(cname)
                labels[cname] = lbl
            counts[cname] = counts.get(cname, 0) + 1

    if not ordered_concepts:
        return {
            "ok": False,
            "status": "error",
            "ticker": ticker.upper(),
            "keyword": kw,
            "error": f"No concepts matching '{kw}' for {ticker.upper()}.",
            "hint": "Try broader keywords (e.g., 'revenue' instead of 'total revenue').",
        }

    best_concept = ordered_concepts[0]
    best_score = -10**9
    for cname in ordered_concepts[:15]:
        lbl = labels[cname]
        score = counts[cname]
        if cname.startswith("us-gaap:"):
            score += 100
        if "deprecated" in lbl.lower():
            score -= 200
        if kw_low in lbl.lower():
            score += 50
        if score > best_score:
            best_score = score
            best_concept = cname

    resolved_concept, rows = select_raw_concept_rows(
        facts_json,
        concept=best_concept,
        form=form_clean,
        limit=limit,
        as_of=as_of_iso,
    )
    all_concepts = [{"concept": c, "label": labels[c]} for c in ordered_concepts[:5]]
    return {
        "ok": True,
        "status": "ok" if rows else "empty",
        "ticker": ticker.upper(),
        "keyword": kw,
        "chosen_concept": resolved_concept,
        "all_concepts": all_concepts,
        "form": form_clean,
        "rows": rows,
    }


def _sources_from_fact_rows(
    ticker: str, cik10: str, rows: list[dict[str, Any]], default_form: str = ""
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in rows:
        accn = str(r.get("accession") or r.get("accession_no") or "").strip()
        filed = str(r.get("filing_date") or "").strip()[:10]
        form = str(r.get("form_type") or r.get("form") or default_form).strip()
        period_end = str(r.get("period_end") or r.get("report_date") or "").strip()[:10]
        key = accn or f"{form}:{filed}"
        if not key or key in seen:
            continue
        seen.add(key)
        sources.append(_make_sec_source(ticker.upper(), cik10, form, filed, accn, period_end))
        if len(sources) >= 50:
            break
    return sources


def _list_filings_impl(
    ticker: str,
    forms: str = "10-K,10-Q,20-F,6-K",
    limit: int = 10,
    as_of: str | None = None,
    form: str | None = None,
    *,
    extended_shape: bool = False,
) -> _FilingsOut:
    try:
        as_of_iso = _validate_as_of(as_of) if extended_shape else (as_of[:10] if as_of else None)
        cik10, company_name = resolve_cik(ticker)
        sub = fetch_submissions(cik10)
        recent = ((sub.get("filings") or {}).get("recent")) or {}
        if extended_shape and form is not None and str(form).strip():
            form_set = {str(form).strip().upper()}
        else:
            form_set = {f.strip().upper() for f in forms.split(",") if f.strip()}
        capped_limit = max(1, min(50, int(limit)))

        acc_list = recent.get("accessionNumber") or []
        form_list = recent.get("form") or []
        fdate_list = recent.get("filingDate") or []
        rdate_list = recent.get("reportDate") or []
        doc_list = recent.get("primaryDocument") or []

        rows: list[dict[str, Any]] = []
        declared_sources: list[dict[str, Any]] = []
        for idx, form_val in enumerate(form_list):
            f_str = str(form_val or "").strip().upper()
            if form_set and f_str not in form_set:
                continue
            fdate = str(fdate_list[idx] if idx < len(fdate_list) else "").strip()[:10]
            if as_of_iso and fdate > as_of_iso:
                continue
            rdate = str(rdate_list[idx] if idx < len(rdate_list) else "").strip()[:10]
            accn = str(acc_list[idx] if idx < len(acc_list) else "").strip()
            pdoc = str(doc_list[idx] if idx < len(doc_list) else "").strip()
            url = _edgar_archive_url(cik10, accn)
            rows.append(
                {
                    "ticker": ticker.upper(),
                    "cik": cik10,
                    "company_name": str(sub.get("name") or company_name),
                    "form": f_str,
                    "filing_date": fdate,
                    "report_date": rdate,
                    "accession_no": accn,
                    "primary_document": pdoc,
                    "url": url,
                }
            )
            declared_sources.append(
                _make_sec_source(ticker.upper(), cik10, f_str, fdate, accn, rdate)
            )
            if len(rows) >= capped_limit:
                break

        env = make_response(
            data=rows,
            source="dalton_sec",
            symbol=ticker.upper(),
        )
        if not extended_shape:
            return {
                "_declared_sources": declared_sources,
                "_sources": declared_sources,
                **env,
            }
        payload = {
            "ok": True,
            "status": "ok" if rows else "empty",
            "ticker": ticker.upper(),
            "rows": rows,
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, declared_sources)
    except Exception as exc:
        if not extended_shape:
            return make_error("sec_error", str(exc), symbol=ticker)
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@mcp.tool()
def list_filings(
    ticker: str,
    forms: str = "10-K,10-Q,20-F,6-K",
    limit: int = 10,
    as_of: str | None = None,
) -> _FilingsOut:
    """List recent SEC EDGAR filings for a company with accession and report dates.

    Filters filings by form type and optional point-in-time filing date cutoff,
    returning direct SEC EDGAR archive URLs and _declared_sources metadata.

    Args:
        ticker: Stock ticker symbol or CIK (e.g. META, MSFT, AMZN).
        forms: Comma-separated SEC form types (default '10-K,10-Q,20-F,6-K').
        limit: Maximum number of filings to return (1-50, default 10).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: Filing list with form, filing_date, report_date, accession_no, and url.
    """
    return _list_filings_impl(
        ticker=ticker,
        forms=forms,
        limit=limit,
        as_of=as_of,
        form=None,
        extended_shape=False,
    )


if _extended_sec_tools_enabled():

    def _list_filings_extended(
        ticker: str,
        forms: str = "10-K,10-Q,20-F,6-K",
        limit: int = 10,
        as_of: str | None = None,
        form: str | None = None,
    ) -> _FilingsOut:
        """List recent SEC EDGAR filings for a company with accession and report dates.

        Filters filings by form type and optional point-in-time filing date cutoff,
        returning direct SEC EDGAR archive URLs and `_sources` provenance metadata.
        When single-value `form` is provided (e.g. '8-K', '4', '424B5', '10-Q'),
        strictly filters by that single form type, overriding `forms`.

        Args:
            ticker: Stock ticker symbol or CIK (e.g. META, MSFT, AMZN).
            forms: Comma-separated SEC form types (default '10-K,10-Q,20-F,6-K').
            limit: Maximum number of filings to return (1-50, default 10).
            as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.
            form: Optional single SEC form code (e.g. '10-Q', '8-K', '4', '424B5').

        Returns:
            dict: Filing list with form, filing_date, report_date, accession_no, and url.
        """
        return _list_filings_impl(
            ticker=ticker,
            forms=forms,
            limit=limit,
            as_of=as_of,
            form=form,
            extended_shape=True,
        )

    if hasattr(mcp, "remove_tool"):
        mcp.remove_tool("list_filings")
    elif hasattr(mcp, "_tool_manager") and hasattr(mcp._tool_manager, "_tools"):
        mcp._tool_manager._tools.pop("list_filings", None)
    mcp.add_tool(_list_filings_extended, name="list_filings")
    list_filings = _list_filings_extended  # type: ignore[assignment]


@_extended_sec_tool()
def get_concept_facts(
    ticker: str,
    concept: str,
    form: str = "10-Q",
    limit: int = 10,
    as_of: str | None = None,
) -> _FactsOut:
    """Fetch raw SEC XBRL time-series rows for a single concept (legacy `facts` compatibility).

    Args:
        ticker: Stock ticker symbol or CIK (e.g. META, MSFT, AMZN).
        concept: Single XBRL concept ID (e.g. 'us-gaap:Revenues' or 'Revenues').
        form: SEC form filter (default '10-Q').
        limit: Maximum number of periods to return (1-100, default 10).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: Raw concept rows with period_start, period_end, numeric_value, unit, filing_date, accession.
    """
    try:
        as_of_iso = _validate_as_of(as_of)
        cik10, _ = resolve_cik(ticker)
        facts_json = fetch_company_facts(cik10)
        resolved_concept, rows = select_raw_concept_rows(
            facts_json,
            concept=concept,
            form=form,
            limit=limit,
            as_of=as_of_iso,
        )
        sources = _sources_from_fact_rows(ticker, cik10, rows, default_form=form)
        env = make_response(
            data=rows,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(rows),
        )
        payload = {
            "ok": True,
            "status": "ok" if rows else "empty",
            "ticker": ticker.upper(),
            "concept": resolved_concept,
            "form": form,
            "rows": rows,
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except LookupError as exc:
        return {"ok": False, "status": "error", **make_error("not_found", str(exc), symbol=ticker)}
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@_extended_sec_tool()
def search_concepts(
    ticker: str,
    query: str,
    as_of: str | None = None,
) -> _FactsOut:
    """Search SEC EDGAR XBRL concept IDs and labels matching a keyword.

    Args:
        ticker: Stock ticker symbol or CIK.
        query: Keyword to match against XBRL concept IDs and labels.
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: Matching `{concept, label}` list and count.
    """
    try:
        as_of_iso = _validate_as_of(as_of)
        cik10, _ = resolve_cik(ticker)
        facts_json = fetch_company_facts(cik10)
        concepts = search_concepts_in_facts(facts_json, query=query, as_of=as_of_iso)
        if not concepts:
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "keyword": query,
                "count": 0,
                "concepts": [],
                **make_error(
                    "not_found",
                    f"No concepts matching '{query}' for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }
        sources = [
            _make_sec_source(
                ticker.upper(),
                cik10,
                "XBRL",
                as_of_iso or date.today().isoformat(),
                f"companyfacts-{cik10}",
            )
        ]
        env = make_response(
            data=concepts,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(concepts),
        )
        payload = {
            "ok": True,
            "status": "ok",
            "ticker": ticker.upper(),
            "keyword": query,
            "concepts": concepts,
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@_extended_sec_tool()
def search_smart_facts(
    ticker: str,
    query: str,
    form: str = "10-Q",
    limit: int = 5,
    as_of: str | None = None,
) -> _FactsOut:
    """Auto-discover the best matching SEC XBRL concept for a keyword and fetch its time series.

    Args:
        ticker: Stock ticker symbol or CIK.
        query: Metric keyword (e.g. 'revenue', 'gross profit', 'capex').
        form: SEC form filter (default '10-Q').
        limit: Maximum number of periods to return (default 5).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: `chosen_concept`, `all_concepts`, `rows`, and `_sources`.
    """
    try:
        as_of_iso = _validate_as_of(as_of)
        cik10, _ = resolve_cik(ticker)
        facts_json = fetch_company_facts(cik10)
        res = search_smart_facts_in_facts(
            facts_json,
            ticker=ticker,
            query=query,
            form=form,
            limit=limit,
            as_of=as_of_iso,
        )
        if not res.get("ok"):
            return {
                **res,
                **make_error("not_found", str(res.get("error") or "No concepts found"), symbol=ticker.upper()),
            }
        rows = res["rows"]
        sources = _sources_from_fact_rows(ticker, cik10, rows, default_form=form)
        env = make_response(
            data=rows,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(rows),
        )
        payload = {
            **res,
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


def _is_pandas_na_or_nat(val: Any) -> bool:
    """Detect ``pandas.NaT`` / ``pandas.NA`` BEFORE ``isinstance(val, datetime)`` (NaT subclasses datetime)."""
    type_name = type(val).__name__.lower()
    if type_name in ("natype", "nattype"):
        return True
    try:
        if isinstance(val, (datetime, date)) and str(val) == "NaT":
            return True
    except Exception:
        pass
    return False


def _json_safe_key(key: Any) -> str:
    """Convert DataFrame date/Timestamp keys into strict ISO-8601 strings."""
    if _is_pandas_na_or_nat(key):
        return "null"
    if isinstance(key, (datetime, date)):
        return key.isoformat()
    iso_fn = getattr(key, "isoformat", None)
    if callable(iso_fn):
        try:
            return str(iso_fn())
        except Exception:
            pass
    return str(key)


def _json_safe_scalar(val: Any) -> Any:
    """Normalize pandas/numpy scalars and non-finite/NA/NaT values into strict RFC 8259 JSON values."""
    if val is None or _is_pandas_na_or_nat(val):
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    # Unwrap numpy/pandas scalar objects (e.g., np.float64, np.int64)
    item_fn = getattr(val, "item", None)
    if callable(item_fn) and not isinstance(val, (dict, list, tuple, set)):
        try:
            val = item_fn()
        except Exception:
            pass
    if val is None or _is_pandas_na_or_nat(val):
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    if isinstance(val, (int, str)):
        return val
    if str(val) in ("NaT", "<NA>", "nan", "NaN"):
        return None
    iso_fn = getattr(val, "isoformat", None)
    if callable(iso_fn):
        try:
            return str(iso_fn())
        except Exception:
            pass
    if isinstance(val, dict):
        return {_json_safe_key(k): _json_safe_scalar(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_json_safe_scalar(v) for v in val]
    return val


def _sanitize_yfinance_frame(df: Any) -> dict[str, Any]:
    """Convert a yfinance statement DataFrame to strict JSON-serializable nested dicts."""
    raw = df.to_dict()
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for col_key, col_val in raw.items():
        safe_col = _json_safe_key(col_key)
        if isinstance(col_val, dict):
            out[safe_col] = {_json_safe_key(rk): _json_safe_scalar(rv) for rk, rv in col_val.items()}
        else:
            out[safe_col] = _json_safe_scalar(col_val)
    return out


def _yf_financials_fallback(ticker: str, form: str, statement: str = "all") -> dict[str, Any] | None:
    """Optional `yfinance` fallback for `get_financials` when `as_of is None` (matches `findata_cli.py:214-249`)."""
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return None
    try:
        t = yf.Ticker(ticker.upper())
        is_annual = "K" in str(form or "").upper() or "20-F" in str(form or "").upper()
        stmts: dict[str, Any] = {}
        mapping = {
            "income": "income_stmt" if is_annual else "quarterly_income_stmt",
            "balance": "balance_sheet" if is_annual else "quarterly_balance_sheet",
            "cash": "cashflow" if is_annual else "quarterly_cashflow",
        }
        for key, attr in mapping.items():
            if statement not in (key, "all"):
                continue
            df = getattr(t, attr, None)
            if df is not None and not getattr(df, "empty", True):
                stmts[key] = _sanitize_yfinance_frame(df)
        if not stmts:
            return None
        row = {
            "form": form,
            "filing_date": "yfinance",
            "statements": stmts,
        }
        env = make_response(data=[row], source="yfinance", symbol=ticker.upper(), count=1)
        return {
            "ok": True,
            "status": "ok",
            "ticker": ticker.upper(),
            "form": form,
            "statement": statement,
            "source": "yfinance",
            "fallback_used": True,
            "rows": [row],
            **env,
        }
    except Exception:
        return None


def _yf_ratios_fallback(ticker: str, form: str) -> dict[str, Any] | None:
    """Optional `yfinance` fallback for `get_ratios` when `as_of is None` (matches `findata_cli.py:252-290`)."""
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        return None
    try:
        info = getattr(yf.Ticker(ticker.upper()), "info", None) or {}
        if not isinstance(info, dict) or not info:
            return None
        ratios: dict[str, Any] = {"filing_date": "yfinance-latest", "source": "yfinance"}
        for key, yf_key in (
            ("gross_margin", "grossMargins"),
            ("operating_margin", "operatingMargins"),
            ("net_margin", "profitMargins"),
            ("roe", "returnOnEquity"),
            ("roa", "returnOnAssets"),
        ):
            val = _json_safe_scalar(info.get(yf_key))
            if val is not None and isinstance(val, (int, float)):
                ratios[key] = round(float(val) * 100, 2)
        ratios["raw"] = {
            "revenue": _json_safe_scalar(info.get("totalRevenue")),
            "gross_profit": _json_safe_scalar(info.get("grossProfits")),
            "net_income": _json_safe_scalar(info.get("netIncomeToCommon")),
            "total_assets": _json_safe_scalar(info.get("totalAssets")),
            "total_equity": _json_safe_scalar(info.get("bookValue")),
        }
        env = make_response(data=[ratios], source="yfinance", symbol=ticker.upper(), count=1)
        return {
            "ok": True,
            "status": "ok",
            "ticker": ticker.upper(),
            "form": form,
            "source": "yfinance",
            "fallback_used": True,
            "ratios": [ratios],
            "rows": [ratios],
            **env,
        }
    except Exception:
        return None


@_extended_sec_tool()
def get_financials(
    ticker: str,
    form: str = "10-Q",
    limit: int = 1,
    statement: str = "all",
    as_of: str | None = None,
) -> _FilingsOut:
    """Fetch SEC EDGAR financial statements (income, balance sheet, cash flow) from recent filings.

    Args:
        ticker: Stock ticker symbol or CIK.
        form: SEC form type (default '10-Q'; pass '10-K' or '20-F' for annual).
        limit: Number of filings to parse (1-20, default 1).
        statement: Statement filter: 'all', 'income', 'balance', or 'cash'.
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: Multi-period `rows` with `statements`, partial-failure `warnings`, and `_sources`.
    """
    try:
        stmt_key = str(statement or "all").strip().lower()
        if stmt_key not in _VALID_STATEMENTS:
            raise ValueError(f"statement must be one of {sorted(_VALID_STATEMENTS)}")
        as_of_iso = _validate_as_of(as_of)
        capped_limit = max(1, min(20, int(limit)))
        form_clean = str(form or "10-Q").strip() or "10-Q"

        cik10, _ = resolve_cik(ticker)
        company = _get_edgar_company(ticker.upper())
        filings = _edgar_filings(company, form=form_clean, as_of=as_of_iso)
        selected_filings = list(filings.head(capped_limit)) if len(filings) else []
        if not selected_filings:
            if as_of_iso is None:
                yf_res = _yf_financials_fallback(ticker, form_clean, stmt_key)
                if yf_res is not None:
                    return yf_res
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "form": form_clean,
                "rows": [],
                **make_error(
                    "not_found",
                    f"No {form_clean} filings found for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }

        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        sources: list[dict[str, Any]] = []
        stmt_attrs = {
            "income": "income_statement",
            "balance": "balance_sheet",
            "cash": "cash_flow_statement",
        }
        success_count = 0
        for f in selected_filings:
            f_form = str(getattr(f, "form", form_clean))
            f_date = str(getattr(f, "filing_date", ""))[:10]
            r_date = str(getattr(f, "report_date", "") or "")[:10]
            accn = str(getattr(f, "accession_no", "") or "").strip()
            try:
                obj = f.obj()
                row: dict[str, Any] = {
                    "form": f_form,
                    "filing_date": f_date,
                    "report_date": r_date or None,
                    "accession_no": accn or None,
                    "statements": {},
                }
                fin = getattr(obj, "financials", None)
                if fin:
                    for key, attr in stmt_attrs.items():
                        if stmt_key not in (key, "all"):
                            continue
                        stmt = getattr(fin, attr, None)
                        if callable(stmt):
                            stmt = stmt()
                        if stmt is not None and hasattr(stmt, "to_dataframe"):
                            row["statements"][key] = stmt.to_dataframe().to_dict(orient="records")
                        else:
                            row["statements"][key] = None
                rows.append(row)
                success_count += 1
                sources.append(_make_sec_source(ticker.upper(), cik10, f_form, f_date, accn, r_date))
            except Exception as exc:
                warnings.append(f"{f_form} ({f_date}): {exc}")
                rows.append(
                    {
                        "form": f_form,
                        "filing_date": f_date,
                        "accession_no": accn or None,
                        "error": str(exc),
                    }
                )

        if success_count == 0:
            if as_of_iso is None:
                yf_res = _yf_financials_fallback(ticker, form_clean, stmt_key)
                if yf_res is not None:
                    yf_res["edgar_errors"] = list(warnings)
                    return yf_res
            return {
                "ok": False,
                "status": "error",
                "ticker": ticker.upper(),
                "form": form_clean,
                "rows": rows,
                "warnings": warnings,
                **make_error(
                    "sec_error",
                    warnings[0] if warnings else "Failed to parse financial statements",
                    symbol=ticker.upper(),
                ),
            }

        status = "partial" if warnings else "ok"
        env = make_response(
            data=rows,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(rows),
            incomplete=bool(warnings),
        )
        payload: dict[str, Any] = {
            "ok": True,
            "status": status,
            "ticker": ticker.upper(),
            "form": form_clean,
            "statement": stmt_key,
            "rows": rows,
            **({"warnings": warnings} if warnings else {}),
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


def _extract_statement_value(stmt: Any, *label_patterns: str) -> float | None:
    if stmt is None:
        return None
    df_stmt = stmt.to_dataframe() if hasattr(stmt, "to_dataframe") else stmt
    if df_stmt is None or getattr(df_stmt, "empty", True):
        return None
    cols = list(getattr(df_stmt, "columns", []))
    date_cols = [c for c in cols if re.match(r"^\d{4}-\d{2}-\d{2}", str(c))]
    if not date_cols or "label" not in cols:
        return None
    latest_col = sorted(date_cols, reverse=True)[0]
    work = df_stmt
    if "dimension" in cols:
        consolidated = work[work["dimension"] == False]  # noqa: E712
        if not consolidated.empty:
            work = consolidated
    for name in label_patterns:
        mask = work["label"].astype(str).str.contains(name, case=False, na=False)
        matches = work[mask]
        if any(k in name.lower() for k in ("equity", "shareholders", "stockholders")):
            exclude = matches["label"].astype(str).str.contains("liabilities", case=False, na=False)
            matches = matches[~exclude]
        if not matches.empty:
            val = matches.iloc[0][latest_col]
            if val is not None and val == val:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return None
    return None


@_extended_sec_tool()
def get_ratios(
    ticker: str,
    form: str = "10-Q",
    limit: int = 1,
    as_of: str | None = None,
) -> _FilingsOut:
    """Compute profitability and return ratios (margins, ROE, ROA) from SEC filings.

    Args:
        ticker: Stock ticker symbol or CIK.
        form: SEC form type (default '10-Q').
        limit: Number of filings to compute ratios for (1-20, default 1).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: `ratios` list (gross_margin, operating_margin, net_margin, roe, roa, raw) and `_sources`.
    """
    try:
        as_of_iso = _validate_as_of(as_of)
        capped_limit = max(1, min(20, int(limit)))
        form_clean = str(form or "10-Q").strip() or "10-Q"

        cik10, _ = resolve_cik(ticker)
        try:
            company = _get_edgar_company(ticker.upper())
            filings = _edgar_filings(company, form=form_clean, as_of=as_of_iso)
        except Exception as exc:
            if as_of_iso is None:
                yf_res = _yf_ratios_fallback(ticker, form_clean)
                if yf_res is not None:
                    yf_res["edgar_error"] = str(exc)
                    return yf_res
            raise
        selected_filings = list(filings.head(capped_limit)) if len(filings) else []
        if not selected_filings:
            if as_of_iso is None:
                yf_res = _yf_ratios_fallback(ticker, form_clean)
                if yf_res is not None:
                    return yf_res
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "form": form_clean,
                "ratios": [],
                **make_error(
                    "not_found",
                    f"No {form_clean} filings found for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }

        ratios_list: list[dict[str, Any]] = []
        warnings: list[str] = []
        sources: list[dict[str, Any]] = []
        success_count = 0
        for f in selected_filings:
            f_form = str(getattr(f, "form", form_clean))
            f_date = str(getattr(f, "filing_date", ""))[:10]
            r_date = str(getattr(f, "report_date", "") or "")[:10]
            accn = str(getattr(f, "accession_no", "") or "").strip()
            try:
                obj = f.obj()
                fin = getattr(obj, "financials", None)
                if not fin:
                    warnings.append(f"{f_form} ({f_date}): financials unavailable")
                    continue
                inc = getattr(fin, "income_statement", None)
                if callable(inc):
                    inc = inc()
                bal = getattr(fin, "balance_sheet", None)
                if callable(bal):
                    bal = bal()
                revenue = _extract_statement_value(inc, "net sales", "revenue", "total revenue", "net revenue", "sales")
                gross_profit = _extract_statement_value(inc, "gross margin", "gross profit")
                operating_income = _extract_statement_value(inc, "operating income", "operating profit", "income from operations")
                net_income = _extract_statement_value(inc, "net income")
                total_assets = _extract_statement_value(bal, "total assets")
                total_equity = _extract_statement_value(bal, "total shareholders", "total stockholders", "total equity")

                entry: dict[str, Any] = {
                    "filing_date": f_date,
                    "report_date": r_date or None,
                    "accession_no": accn or None,
                }
                if revenue and revenue != 0:
                    if gross_profit is not None:
                        entry["gross_margin"] = round(gross_profit / revenue * 100, 2)
                    if operating_income is not None:
                        entry["operating_margin"] = round(operating_income / revenue * 100, 2)
                    if net_income is not None:
                        entry["net_margin"] = round(net_income / revenue * 100, 2)
                if net_income is not None:
                    if total_assets and total_assets != 0:
                        entry["roa"] = round(net_income / total_assets * 100, 2)
                    if total_equity and total_equity != 0:
                        entry["roe"] = round(net_income / total_equity * 100, 2)
                entry["raw"] = {
                    "revenue": revenue,
                    "gross_profit": gross_profit,
                    "operating_income": operating_income,
                    "net_income": net_income,
                    "total_assets": total_assets,
                    "total_equity": total_equity,
                }
                ratios_list.append(entry)
                success_count += 1
                sources.append(_make_sec_source(ticker.upper(), cik10, f_form, f_date, accn, r_date))
            except Exception as exc:
                warnings.append(f"{f_form} ({f_date}): {exc}")
                ratios_list.append({"filing_date": f_date, "error": str(exc)})

        if success_count == 0 and warnings:
            return {
                "ok": False,
                "status": "error",
                "ticker": ticker.upper(),
                "form": form_clean,
                "ratios": ratios_list,
                "warnings": warnings,
                **make_error("sec_error", warnings[0], symbol=ticker.upper()),
            }

        status = "partial" if warnings else ("ok" if ratios_list else "empty")
        env = make_response(
            data=ratios_list,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(ratios_list),
            incomplete=bool(warnings),
        )
        payload: dict[str, Any] = {
            "ok": True,
            "status": status,
            "ticker": ticker.upper(),
            "form": form_clean,
            "ratios": ratios_list,
            "rows": ratios_list,
            **({"warnings": warnings} if warnings else {}),
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@_extended_sec_tool()
def search_filing_text(
    ticker: str,
    query: str,
    form: str = "10-Q",
    limit: int = 1,
    window: int = 500,
    max_matches: int = 10,
    as_of: str | None = None,
) -> _FilingsOut:
    """Full-text keyword search inside recent SEC filings with context windows.

    Args:
        ticker: Stock ticker symbol or CIK.
        query: Case-insensitive text keyword to search for.
        form: SEC form type (default '10-Q').
        limit: Number of recent filings to search (1-20, default 1).
        window: Context window characters around each match (1-500000, default 500).
        max_matches: Maximum matches per filing (1-100, default 10).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: Per-filing `matches` snippets, `total_matches`, and `_sources`.
    """
    try:
        kw = str(query or "").strip()
        if not kw:
            raise ValueError("query is required")
        if type(window) is not int or not 1 <= window <= _MAX_TEXT_CHARS:
            raise ValueError(f"window must be an integer between 1 and {_MAX_TEXT_CHARS}")
        if type(max_matches) is not int or not 1 <= max_matches <= _MAX_MATCHES_CAP:
            raise ValueError(f"max_matches must be an integer between 1 and {_MAX_MATCHES_CAP}")
        as_of_iso = _validate_as_of(as_of)
        capped_limit = max(1, min(20, int(limit)))
        form_clean = str(form or "10-Q").strip() or "10-Q"

        cik10, _ = resolve_cik(ticker)
        company = _get_edgar_company(ticker.upper())
        filings = _edgar_filings(company, form=form_clean, as_of=as_of_iso)
        selected_filings = list(filings.head(capped_limit)) if len(filings) else []
        if not selected_filings:
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "keyword": kw,
                "filings": [],
                **make_error(
                    "not_found",
                    f"No {form_clean} filings found for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }

        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        sources: list[dict[str, Any]] = []
        kw_low = kw.lower()
        success_count = 0
        for f in selected_filings:
            f_form = str(getattr(f, "form", form_clean))
            f_date = str(getattr(f, "filing_date", ""))[:10]
            r_date = str(getattr(f, "report_date", "") or "")[:10]
            accn = str(getattr(f, "accession_no", "") or "").strip()
            filing_result: dict[str, Any] = {
                "filing_date": f_date,
                "form": f_form,
                "accession_no": accn or None,
                "matches": [],
            }
            try:
                text = ""
                if hasattr(f, "text") and callable(f.text):
                    text = str(f.text() or "")
                else:
                    obj = f.obj()
                    text = str(obj.text() if hasattr(obj, "text") and callable(obj.text) else obj)
                text_low = text.lower()
                pos = 0
                match_count = 0
                while True:
                    idx = text_low.find(kw_low, pos)
                    if idx == -1:
                        break
                    match_count += 1
                    if match_count > max_matches:
                        break
                    w_start = max(0, idx - window)
                    w_end = min(len(text), idx + len(kw_low) + window)
                    snippet = " ".join(text[w_start:w_end].split())
                    filing_result["matches"].append({"index": match_count, "snippet": snippet})
                    pos = idx + len(kw_low)
                filing_result["total_matches"] = match_count
                success_count += 1
                sources.append(_make_sec_source(ticker.upper(), cik10, f_form, f_date, accn, r_date))
            except Exception as exc:
                filing_result["error"] = str(exc)
                warnings.append(f"{f_form} ({f_date}): {exc}")
            results.append(filing_result)

        if success_count == 0:
            return {
                "ok": False,
                "status": "error",
                "ticker": ticker.upper(),
                "keyword": kw,
                "filings": results,
                "warnings": warnings,
                **make_error("sec_error", warnings[0] if warnings else "Text search failed", symbol=ticker.upper()),
            }

        status = "partial" if warnings else "ok"
        env = make_response(
            data=results,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(results),
            incomplete=bool(warnings),
        )
        payload: dict[str, Any] = {
            "ok": True,
            "status": status,
            "ticker": ticker.upper(),
            "keyword": kw,
            "form": form_clean,
            "filings": results,
            "rows": results,
            **({"warnings": warnings} if warnings else {}),
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@_extended_sec_tool()
def read_filing_item(
    ticker: str,
    item: str,
    form: str = "10-Q",
    max_chars: int = 15000,
    as_of: str | None = None,
) -> _ObjectOut:
    """Read a specific section/item (e.g. '1A', '7', 'Item 7', '2.02') from the latest SEC filing.

    Args:
        ticker: Stock ticker symbol or CIK.
        item: SEC item identifier (e.g. '1A', '7', 'Item 1A', '2.02').
        form: SEC form type (default '10-Q').
        max_chars: Maximum characters to return (1-500000, default 15000).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: Extracted section `text`, `total_chars`, `truncated`, filing metadata, and `_sources`.
    """
    try:
        item_clean = str(item or "").strip()
        if not item_clean:
            raise ValueError("item is required")
        if type(max_chars) is not int or not 1 <= max_chars <= _MAX_TEXT_CHARS:
            raise ValueError(f"max_chars must be an integer between 1 and {_MAX_TEXT_CHARS}")
        as_of_iso = _validate_as_of(as_of)
        form_clean = str(form or "10-Q").strip() or "10-Q"

        cik10, _ = resolve_cik(ticker)
        company = _get_edgar_company(ticker.upper())
        filings = _edgar_filings(company, form=form_clean, as_of=as_of_iso)
        selected = list(filings.head(1)) if len(filings) else []
        if not selected:
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "form": form_clean,
                "item": item_clean,
                **make_error(
                    "not_found",
                    f"No {form_clean} filings found for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }

        f = selected[0]
        f_form = str(getattr(f, "form", form_clean))
        f_date = str(getattr(f, "filing_date", ""))[:10]
        r_date = str(getattr(f, "report_date", "") or "")[:10]
        accn = str(getattr(f, "accession_no", "") or "").strip()
        obj = f.obj()
        if not hasattr(obj, "__getitem__"):
            return {
                "ok": False,
                "status": "error",
                "ticker": ticker.upper(),
                "filing_date": f_date,
                "form": f_form,
                "item": item_clean,
                **make_error("sec_error", "Filing object does not support item extraction.", symbol=ticker.upper()),
            }

        text: str | None = None
        candidates = [item_clean]
        if not item_clean.lower().startswith("item "):
            candidates.append(f"Item {item_clean}")
        for cand in candidates:
            try:
                val = obj[cand]
                if val:
                    text = str(val)
                    break
            except (KeyError, IndexError):
                continue

        if not text:
            available = str(getattr(obj, "items", "Unknown"))
            return {
                "ok": False,
                "status": "error",
                "ticker": ticker.upper(),
                "filing_date": f_date,
                "form": f_form,
                "accession_no": accn or None,
                "item": item_clean,
                **make_error(
                    "not_found",
                    f"Item '{item_clean}' not found or empty. Available: {available}",
                    symbol=ticker.upper(),
                ),
            }

        truncated = len(text) > max_chars
        record = {
            "ticker": ticker.upper(),
            "filing_date": f_date,
            "report_date": r_date or None,
            "form": f_form,
            "accession_no": accn or None,
            "item": item_clean,
            "text": text[:max_chars],
            "truncated": truncated,
            "total_chars": len(text),
        }
        sources = [_make_sec_source(ticker.upper(), cik10, f_form, f_date, accn, r_date)]
        env = make_response(
            data=record,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=1,
            truncated=truncated,
        )
        payload = {
            "ok": True,
            "status": "ok",
            **record,
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@_extended_sec_tool()
def get_filing_attachments(
    ticker: str,
    form: str = "10-Q",
    accession: str | None = None,
    document: str | None = None,
    document_type: str | None = None,
    max_chars: int = 120000,
    as_of: str | None = None,
) -> _ObjectOut:
    """List or read an attachment (e.g. EX-99.1 earnings release) from an SEC filing.

    `document` (exact filename) and `document_type` (e.g. 'EX-99.1') are mutually
    exclusive. Omit both to list all attachments on the filing.

    Args:
        ticker: Stock ticker symbol or CIK.
        form: SEC form type (default '10-Q'; pass '8-K' or '6-K' for press releases).
        accession: Optional exact SEC accession number (`XXXXXXXXXX-XX-XXXXXX`).
        document: Optional exact attachment filename to read (mutually exclusive with `document_type`).
        document_type: Optional SEC document type to read (e.g. 'EX-99.1', mutually exclusive with `document`).
        max_chars: Maximum characters to return when reading an attachment (1-500000, default 120000).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: Filing metadata, `attachments` list, optional `selected` + `text`, and `_sources`.
    """
    try:
        doc_clean = str(document).strip() if document is not None and str(document).strip() else None
        dtype_clean = str(document_type).strip() if document_type is not None and str(document_type).strip() else None
        if doc_clean and dtype_clean:
            raise ValueError("document and document_type are mutually exclusive")
        accn_clean = str(accession).strip() if accession is not None and str(accession).strip() else None
        if accn_clean and not _SEC_ACCESSION_RE.fullmatch(accn_clean):
            raise ValueError("accession must match XXXXXXXXXX-XX-XXXXXX")
        if type(max_chars) is not int or not 1 <= max_chars <= _MAX_TEXT_CHARS:
            raise ValueError(f"max_chars must be an integer between 1 and {_MAX_TEXT_CHARS}")
        as_of_iso = _validate_as_of(as_of)
        form_clean = str(form or "10-Q").strip() or "10-Q"

        cik10, company_name = resolve_cik(ticker)
        company = _get_edgar_company(ticker.upper())
        filings = _edgar_filings(company, form=form_clean, as_of=as_of_iso)
        if accn_clean:
            filings = filings.filter(accession_number=accn_clean)
        selected_list = list(filings.head(1)) if len(filings) else []
        if not selected_list:
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "form": form_clean,
                **make_error(
                    "not_found",
                    f"No matching {form_clean} filing found for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }

        filing = selected_list[0]
        f_form = str(getattr(filing, "form", form_clean))
        f_date = str(getattr(filing, "filing_date", ""))[:10]
        r_date = str(getattr(filing, "report_date", "") or "")[:10]
        f_accn = str(getattr(filing, "accession_no", "") or "").strip()

        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        chosen_att: Any = None
        for idx, att in enumerate(getattr(filing, "attachments", []) or []):
            try:
                row = {
                    "sequence": str(getattr(att, "sequence_number", "")),
                    "document": str(getattr(att, "document", "")),
                    "document_type": str(getattr(att, "document_type", "")),
                    "description": str(getattr(att, "display_description", "") or ""),
                    "size": getattr(att, "size", None),
                    "url": str(getattr(att, "url", "")),
                }
                rows.append(row)
                if doc_clean and row["document"] == doc_clean:
                    chosen_att = att
                elif (
                    not doc_clean
                    and dtype_clean
                    and row["document_type"].upper() == dtype_clean.upper()
                    and chosen_att is None
                ):
                    chosen_att = att
            except Exception as exc:
                warnings.append(f"attachment[{idx}]: {exc}")

        base: dict[str, Any] = {
            "ticker": ticker.upper(),
            "company": str(getattr(company, "name", company_name)),
            "cik": str(getattr(company, "cik", cik10)),
            "form": f_form,
            "filing_date": f_date,
            "accession_no": f_accn,
            "attachments": rows,
        }
        sources = [_make_sec_source(ticker.upper(), cik10, f_form, f_date, f_accn, r_date)]

        if not doc_clean and not dtype_clean:
            status = "partial" if warnings else "ok"
            env = make_response(
                data=base,
                source="sec_edgar",
                symbol=ticker.upper(),
                count=len(rows),
                incomplete=bool(warnings),
            )
            payload = {
                "ok": True,
                "status": status,
                **base,
                **({"warnings": warnings} if warnings else {}),
                **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
                **env,
            }
            return _attach_sources(payload, sources)

        if chosen_att is None:
            return {
                "ok": False,
                "status": "error",
                **base,
                **make_error(
                    "not_found",
                    "Requested attachment was not found.",
                    symbol=ticker.upper(),
                ),
            }

        raw_text = chosen_att.text()
        if raw_text is None:
            return {
                "ok": False,
                "status": "error",
                **base,
                **make_error(
                    "sec_error",
                    f"Attachment {chosen_att.document} is binary or not text-extractable.",
                    symbol=ticker.upper(),
                ),
            }
        if isinstance(raw_text, bytes):
            raw_text = raw_text.decode("utf-8", errors="replace")
        text_str = str(raw_text)
        truncated = len(text_str) > max_chars
        selected_meta = {
            "document": str(getattr(chosen_att, "document", "")),
            "document_type": str(getattr(chosen_att, "document_type", "")),
            "description": str(getattr(chosen_att, "display_description", "") or ""),
            "url": str(getattr(chosen_att, "url", "")),
        }
        full_record = {
            **base,
            "selected": selected_meta,
            "text": text_str[:max_chars],
            "total_chars": len(text_str),
            "truncated": truncated,
        }
        status = "partial" if warnings else "ok"
        env = make_response(
            data=full_record,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(rows),
            truncated=truncated,
            incomplete=bool(warnings),
        )
        payload = {
            "ok": True,
            "status": status,
            **full_record,
            **({"warnings": warnings} if warnings else {}),
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@_extended_sec_tool()
def list_eight_k_events(
    ticker: str,
    limit: int = 3,
    as_of: str | None = None,
) -> _FilingsOut:
    """List recent Form 8-K current reports with reported items and text snippets.

    Args:
        ticker: Stock ticker symbol or CIK.
        limit: Maximum number of 8-K filings to return (1-20, default 3).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: `rows` with `filing_date`, `accession_no`, `items`, `content_snippet`, and `_sources`.
    """
    try:
        as_of_iso = _validate_as_of(as_of)
        capped_limit = max(1, min(20, int(limit)))
        cik10, _ = resolve_cik(ticker)
        company = _get_edgar_company(ticker.upper())
        filings = _edgar_filings(company, form="8-K", as_of=as_of_iso)
        selected = list(filings.head(capped_limit)) if len(filings) else []
        if not selected:
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "count": 0,
                "rows": [],
                **make_error(
                    "not_found",
                    f"No 8-K filings found for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }

        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        sources: list[dict[str, Any]] = []
        success_count = 0
        for f in selected:
            f_date = str(getattr(f, "filing_date", ""))[:10]
            accn = str(getattr(f, "accession_no", "") or "").strip()
            row: dict[str, Any] = {"filing_date": f_date, "accession_no": accn}
            try:
                obj = f.obj()
                row["items"] = str(getattr(obj, "items", "N/A"))
                if hasattr(obj, "text") and callable(obj.text):
                    text = str(obj.text() or "")
                elif hasattr(f, "text") and callable(f.text):
                    text = str(f.text() or "")
                else:
                    text = str(obj)
                row["content_snippet"] = text[:3000]
                success_count += 1
                sources.append(_make_sec_source(ticker.upper(), cik10, "8-K", f_date, accn))
            except Exception as exc:
                row["error"] = str(exc)
                warnings.append(f"8-K ({f_date}): {exc}")
            rows.append(row)

        if success_count == 0:
            return {
                "ok": False,
                "status": "error",
                "ticker": ticker.upper(),
                "count": len(rows),
                "rows": rows,
                "warnings": warnings,
                **make_error("sec_error", warnings[0] if warnings else "Failed reading 8-K filings", symbol=ticker.upper()),
            }

        status = "partial" if warnings else "ok"
        env = make_response(
            data=rows,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(rows),
            incomplete=bool(warnings),
        )
        payload: dict[str, Any] = {
            "ok": True,
            "status": status,
            "ticker": ticker.upper(),
            "rows": rows,
            **({"warnings": warnings} if warnings else {}),
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


@_extended_sec_tool()
def list_insider_transactions(
    ticker: str,
    limit: int = 5,
    as_of: str | None = None,
) -> _FilingsOut:
    """List recent Form 4 insider trading filings for a company.

    Args:
        ticker: Stock ticker symbol or CIK.
        limit: Maximum number of Form 4 filings to return (1-20, default 5).
        as_of: Optional YYYY-MM-DD point-in-time filing date cutoff.

    Returns:
        dict: `rows` with `filing_date`, `accession_no`, `content`, and `_sources`.
    """
    try:
        as_of_iso = _validate_as_of(as_of)
        capped_limit = max(1, min(20, int(limit)))
        cik10, _ = resolve_cik(ticker)
        company = _get_edgar_company(ticker.upper())
        filings = _edgar_filings(company, form="4", as_of=as_of_iso)
        selected = list(filings.head(capped_limit)) if len(filings) else []
        if not selected:
            return {
                "ok": False,
                "status": "empty",
                "ticker": ticker.upper(),
                "count": 0,
                "rows": [],
                **make_error(
                    "not_found",
                    f"No Form 4 filings found for {ticker.upper()}.",
                    symbol=ticker.upper(),
                ),
            }

        rows: list[dict[str, Any]] = []
        warnings: list[str] = []
        sources: list[dict[str, Any]] = []
        success_count = 0
        for f in selected:
            f_date = str(getattr(f, "filing_date", ""))[:10]
            accn = str(getattr(f, "accession_no", "") or "").strip()
            row: dict[str, Any] = {"filing_date": f_date, "accession_no": accn}
            try:
                row["content"] = str(f.obj())[:3000]
                success_count += 1
                sources.append(_make_sec_source(ticker.upper(), cik10, "4", f_date, accn))
            except Exception as exc:
                row["error"] = str(exc)
                warnings.append(f"Form 4 ({f_date}): {exc}")
            rows.append(row)

        if success_count == 0:
            return {
                "ok": False,
                "status": "error",
                "ticker": ticker.upper(),
                "count": len(rows),
                "rows": rows,
                "warnings": warnings,
                **make_error("sec_error", warnings[0] if warnings else "Failed reading Form 4 filings", symbol=ticker.upper()),
            }

        status = "partial" if warnings else "ok"
        env = make_response(
            data=rows,
            source="sec_edgar",
            symbol=ticker.upper(),
            count=len(rows),
            incomplete=bool(warnings),
        )
        payload: dict[str, Any] = {
            "ok": True,
            "status": status,
            "ticker": ticker.upper(),
            "rows": rows,
            **({"warnings": warnings} if warnings else {}),
            **({"as_of": as_of_iso, "point_in_time": "filing_date_cutoff_daily"} if as_of_iso else {}),
            **env,
        }
        return _attach_sources(payload, sources)
    except Exception as exc:
        return {"ok": False, "status": "error", **make_error("sec_error", str(exc), symbol=ticker)}


if __name__ == "__main__":
    mcp.run()
