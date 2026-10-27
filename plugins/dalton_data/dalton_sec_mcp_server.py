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
import os
import re
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    from _bootstrap import MCPServer
except ModuleNotFoundError:
    from mcp_servers._bootstrap import MCPServer

from mcp_servers._envelope import make_error, make_response
from mcp_servers._schemas import RECORDS, envelope_schema, output_model

mcp = MCPServer("dalton_sec")

_FactsOut = output_model("DaltonSecFactsOutput", envelope_schema(RECORDS))
_FilingsOut = output_model("DaltonSecFilingsOutput", envelope_schema(RECORDS))

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
    try:
        cik10, company_name = resolve_cik(ticker)
        sub = fetch_submissions(cik10)
        recent = ((sub.get("filings") or {}).get("recent")) or {}
        form_Set = {f.strip().upper() for f in forms.split(",") if f.strip()}
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
            if form_Set and f_str not in form_Set:
                continue
            fdate = str(fdate_list[idx] if idx < len(fdate_list) else "").strip()[:10]
            if as_of and fdate > as_of[:10]:
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
        return {
            "_declared_sources": declared_sources,
            "_sources": declared_sources,
            **env,
        }
    except Exception as exc:
        return make_error("sec_error", str(exc), symbol=ticker)


if __name__ == "__main__":
    mcp.run()
