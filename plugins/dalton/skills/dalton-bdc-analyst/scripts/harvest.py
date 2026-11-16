#!/usr/bin/env python3
"""
harvest.py — BDC Holdings Harvester

Harvests Schedule of Investments data from SEC filings and stores
in per-ticker JSON files under <workspace>/state/dalton-bdc-analyst/data/.

Usage:
    python3 harvest.py --workspace-dir <workspace> --ticker ARCC --period 25Q4
    python3 harvest.py --workspace-dir <workspace> --ticker all --period 25Q4
    python3 harvest.py --workspace-dir <workspace> --ticker ARCC --period 25Q4 --force
    python3 harvest.py --workspace-dir <workspace> --list
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import statistics
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure scripts/ is in path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bdc_analyzer import (
    FilingNotFoundForPeriod,
    fetch_filing_url_for_period,
    get_cik,
    get_shareholder_equity,
)
from config import PROFILE_MAP, SCALE_OVERRIDES, SEC_TICKER_UNAVAILABLE, UNSUPPORTED
from normalize_names import canonical_key, normalize_company_name
from state_store import (
    atomic_write_json,
    ensure_workspace_state,
    resolve_safe_state_dir,
    state_lock,
    verify_safe_target,
)


def _parse_period(period_str: str) -> tuple[int, int]:
    """Parse '25Q4' -> (2025, 4)."""
    m = re.match(r"^(\d{2})Q([1-4])$", period_str)
    if not m:
        raise ValueError(f"Invalid period: {period_str}. Use format like 25Q4")
    return 2000 + int(m.group(1)), int(m.group(2))


def _load_db(ticker: str, state_dir: Path) -> dict[str, Any]:
    """Load existing DB file for a ticker from workspace state, or return empty structure."""
    path = state_dir / "data" / f"{ticker}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"ticker": ticker, "periods": {}}


def _save_period_entry(
    ticker: str,
    period: str,
    entry: dict[str, Any],
    workspace_dir: str | Path,
) -> dict[str, Any]:
    """Atomically merge and save a period entry under an exclusive per-ticker lock."""
    resolved_ws, state_dir = resolve_safe_state_dir(workspace_dir)
    target = verify_safe_target(state_dir / "data" / f"{ticker}.json", resolved_ws)
    with state_lock(state_dir, resolved_ws, lock_name=f".{ticker}.lock"):
        verify_safe_target(target, resolved_ws)
        db = _load_db(ticker, state_dir)
        if "periods" not in db or not isinstance(db["periods"], dict):
            db["periods"] = {}
        db["ticker"] = ticker
        db["periods"][period] = entry
        atomic_write_json(target, db, resolved_ws)
    return db


def _detect_scale(values: list[float]) -> float:
    """Auto-detect unit scale from raw values."""
    if not values:
        return 1
    med = statistics.median(values)
    if med > 100_000:
        return 1_000_000  # full dollars -> millions
    if med > 2000:
        return 1000  # thousands -> millions
    return 1  # already in millions


_COMPANY_DB_CACHE: dict[str, dict[str, Any]] = {}


def _company_db_lookup(company_key: str, state_dir: Path) -> dict[str, str]:
    """Look up company info from <state_dir>/company_db.json."""
    cache_key = str(state_dir)
    if cache_key not in _COMPANY_DB_CACHE:
        db_path = state_dir / "company_db.json"
        if db_path.is_file():
            with open(db_path, "r", encoding="utf-8") as f:
                _COMPANY_DB_CACHE[cache_key] = json.load(f)
        else:
            _COMPANY_DB_CACHE[cache_key] = {}

    db = _COMPANY_DB_CACHE[cache_key]
    key = company_key.upper().strip()

    if key in db:
        return db[key]

    words = key.split()[:2]
    prefix = " ".join(words)
    for k, v in db.items():
        if k.startswith(prefix):
            return v

    return {"industry": "", "sub_industry": "", "description": ""}


class PeriodNotFiledError(Exception):
    """Raised when the SEC 10-Q/10-K for a requested period has not been filed yet."""

    def __init__(self, ticker: str, period: str) -> None:
        self.ticker = ticker
        self.period = period
        super().__init__(f"{ticker}/{period}: 该期 10-Q/10-K 尚未申报")


def harvest_one(
    ticker: str,
    period: str,
    force: bool = False,
    *,
    workspace_dir: str | Path | None = None,
) -> dict[str, Any] | None:
    """Harvest a single ticker for a single period into workspace state.

    Returns the period entry dict, or None on failure; raises PeriodNotFiledError when the period has no 10-Q/10-K yet.
    """
    if workspace_dir is None:
        raise RuntimeError("--workspace-dir is required for harvest")

    state_dir = ensure_workspace_state(workspace_dir)
    resolved_ws, state_dir = resolve_safe_state_dir(workspace_dir)

    ticker = ticker.upper()
    if ticker in UNSUPPORTED:
        return None

    target_file = verify_safe_target(state_dir / "data" / f"{ticker}.json", resolved_ws)
    with state_lock(state_dir, resolved_ws, lock_name=f".{ticker}.lock"):
        verify_safe_target(target_file, resolved_ws)
        db = _load_db(ticker, state_dir)
        if period in db.get("periods", {}) and not force:
            return db["periods"][period]

    cik = get_cik(ticker)
    if not cik:
        print(f"  ✗ {ticker}: CIK not found", file=sys.stderr)
        return None

    # No fallback: a BDC whose fiscal year ends in September files its September
    # quarter as a 10-K weeks later than the others, and parsing the nearest older
    # filing under the requested period stores wrong holdings for that period.
    try:
        url, resolved, _ = fetch_filing_url_for_period(
            cik, period, allow_fallback=False, return_meta=True
        )
    except FilingNotFoundForPeriod as e:
        print(f"  ⏳ {ticker}/{period}: 该期 10-Q/10-K 尚未申报，跳过", file=sys.stderr)
        raise PeriodNotFiledError(ticker, period) from e
    except Exception as e:
        print(f"  ✗ {ticker}/{period}: Filing not found — {e}", file=sys.stderr)
        return None

    year, quarter = _parse_period(period)
    module_name = PROFILE_MAP.get(ticker)
    if not module_name:
        print(f"  ✗ {ticker}/{period}: No parser profile configured", file=sys.stderr)
        return None

    try:
        mod = importlib.import_module(module_name)
        df = mod.parse_period(url, year, quarter)
    except Exception as e:
        print(f"  ✗ {ticker}/{period}: Parse error — {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return None

    if df is None or df.empty:
        print(f"  ⚠ {ticker}/{period}: 0 holdings parsed", file=sys.stderr)
        return None

    equity_usd = get_shareholder_equity(cik) or 0

    face_values = [v for v in df["Face"].tolist() if v > 0]
    scale = _detect_scale(face_values)
    if ticker in SCALE_OVERRIDES:
        scale = SCALE_OVERRIDES[ticker]

    merged: dict[str, dict[str, Any]] = {}
    for _, row in df.iterrows():
        raw_name = str(row.get("CompanyKey", "")).strip()
        if not raw_name:
            continue

        face_m = row["Face"] / scale
        fair_m = row["Fair"] / scale

        display_name = normalize_company_name(raw_name)
        canon = canonical_key(raw_name)
        if not canon:
            continue

        if canon in merged:
            merged[canon]["face_m"] += face_m
            merged[canon]["fair_m"] += fair_m
            if len(raw_name) > len(merged[canon].get("_raw", "")):
                merged[canon]["company"] = display_name
                merged[canon]["_raw"] = raw_name
        else:
            info = _company_db_lookup(raw_name, state_dir)
            merged[canon] = {
                "company": display_name,
                "face_m": face_m,
                "fair_m": fair_m,
                "industry": info.get("industry", ""),
                "sub_industry": info.get("sub_industry", ""),
                "description": info.get("description", ""),
                "_raw": raw_name,
            }

    holdings = []
    for h_entry in merged.values():
        h_entry.pop("_raw", None)
        h_entry["face_m"] = round(h_entry["face_m"], 4)
        h_entry["fair_m"] = round(h_entry["fair_m"], 4)

        name = h_entry["company"].upper()
        name_norm = re.sub(r"\s+", " ", name).strip()

        _date_vals = [312.025, 312.02, 312.0, 302.025, 302.02, 302.0, 930.25, 630.25, 331.25]
        is_date_artifact = False
        for dv in _date_vals:
            if abs(h_entry["face_m"] - dv) < 0.1 and abs(h_entry["fair_m"] - dv) < 0.1:
                is_date_artifact = True
                break
            if abs(h_entry["face_m"] - dv) < 0.1 and h_entry["fair_m"] == 0:
                is_date_artifact = True
                break
        if is_date_artifact:
            continue

        _header_keywords = [
            "NAME OF INVESTMENT", "FAIR VALUE, END OF PERIOD", "FAIR VALUE, BEGINNING",
            "BALANCE,",
            "NET CHANGE IN UNREALIZED", "CHANGE IN UNREALIZED", "CHANGES IN UNREALIZED",
            "PURCHASES OF INVESTMENTS", "SALES OF INVESTMENTS", "REALIZED GAIN", "REALIZED LOSS",
            "TRANSFERS IN", "TRANSFERS OUT", "ENDING BALANCE", "BEGINNING BALANCE",
            "PORTFOLIO COMPANY", "PORTFOLIO INVESTMENTS",
        ]
        if any(k in name_norm for k in _header_keywords):
            continue

        _section_keywords = [
            "TOTAL NON-CONTROL", "TOTAL CONTROL", "TOTAL AFFILIATE", "TOTAL NON-AFFILIATE",
            "TOTAL PORTFOLIO", "TOTAL INVESTMENTS", "TOTAL FUNDED", "TOTAL UNFUNDED",
            "NON-CONTROLLED/", "CONTROLLED INVESTMENTS",
            "COMMON STOCK", "PREFERRED STOCK", "MEMBERSHIP INTEREST",
            "SUBORDINATED DEBT", "SUBORDINATED NOTES", "SUBORDINATED STRUCTURED",
            "SENIOR SECURED NOTES", "CLO SUBORDINATED", "CLO EQUITY",
            "EQUITY INTERESTS IN ", "LP UNITS",
        ]
        if any(name_norm == k or name_norm.startswith(k) for k in _section_keywords):
            continue

        _exact_skip = {
            "CASH", "EQUITY", "WARRANT", "WARRANTS", "UNSECURED",
            "BUILDING PRODUCTS", "ONE STOP", "ARTIFICIAL INTELLIGENCE", "IT CONSULTING",
            "CORPORATE",
            "GAINS / INCOME",
        }
        if name_norm in _exact_skip:
            continue

        if re.match(r"^FOR THE (THREE|SIX|NINE|TWELVE) MONTHS ENDED", name_norm):
            continue

        if re.match(r"^BALANCE[S]?\s+(AT|AS OF)\s", name_norm):
            continue

        if re.match(r"^INVESTMENTS IN (EQUITY|DEBT) SECURITIES$", name_norm):
            continue

        _industry_headers = [
            "HEALTHCARE, EDUCATION",
            "AEROSPACE AND DEFENSE",
            "BANKING, FINANCE, INSURANCE & REAL ESTATE",
        ]
        if any(name_norm.startswith(k) for k in _industry_headers):
            continue

        if re.match(r"^\d{4}\s+NOTES$", name_norm):
            continue
        if re.match(r"^\d{4}-\d+", name_norm) and "NOTES" in name_norm:
            continue

        if re.match(r"^(SOFR|L)\+", name_norm) or "% CASH, DUE " in name_norm:
            continue

        _fee_expense_patterns = [
            "INCENTIVE FEE", "EXPENSES BEFORE CREDITS",
            "AMORTIZATION OF DEFERRED FINANCING", "OTHER GENERAL AND ADMINISTRATIVE",
            "MULTIPLIED BY PRORATED", "BASE MANAGEMENT FEE",
            "NET INVESTMENT INCOME", "TOTAL INVESTMENT INCOME",
            "INTEREST AND FINANCING EXPENSES", "MANAGEMENT FEES",
        ]
        if any(k in name_norm for k in _fee_expense_patterns):
            continue

        if name_norm.startswith("GLADSTONE ALTERNATIVE") or name_norm.startswith("GLADSTONE INVESTMENT"):
            continue

        _cash_keywords = [
            "TREASURY OBLIGATIONS", "TREASURY BILL", "MONEY MARKET",
            "CASH EQUIVALENTS", "CASH AND CASH EQUIVALENTS",
            "GOVERNMENT OBLIGATIONS FUND", "INSTITUTIONAL LIQUIDITY",
            "PREMIUM MONEY MARKET", "TREASURY FUND", "TREASURY LIQUID",
            "US TREASURY", "U.S. TREASURY", "UNITED STATES TREASURY",
            "MFB NORTHERN INST FUNDS", "TREAS PORTFOLIO",
        ]
        if any(k in name_norm for k in _cash_keywords):
            continue

        try:
            float(name_norm.replace(",", ""))
            continue
        except ValueError:
            pass

        if h_entry["face_m"] == 0 and h_entry["fair_m"] == 0:
            continue

        if h_entry["fair_m"] > 0 and h_entry["face_m"] / h_entry["fair_m"] > 20:
            continue
        if h_entry["face_m"] > 500 and h_entry["fair_m"] < h_entry["face_m"] * 0.1:
            continue

        holdings.append(h_entry)

    entry = {
        "period": period,
        "resolved_period": resolved,
        "source_url": url,
        "equity_usd": equity_usd,
        "scale_detected": scale,
        "holdings_count": len(holdings),
        "total_face_m": round(sum(h["face_m"] for h in holdings), 2),
        "total_fair_m": round(sum(h["fair_m"] for h in holdings), 2),
        "harvested_at": datetime.now(timezone.utc).isoformat(),
        "holdings": holdings,
    }

    sanity_warning = None
    existing_periods = {
        k: v
        for k, v in db.get("periods", {}).items()
        if k != period and v.get("holdings_count", 0) > 0
    }
    if existing_periods and len(holdings) > 0:
        def _qnum(p: str) -> int:
            return int(p[:2]) * 4 + int(p[3])

        target_qn = _qnum(period)
        nearest_p = min(existing_periods.keys(), key=lambda p: abs(_qnum(p) - target_qn))
        ref = existing_periods[nearest_p]
        ref_face = ref.get("total_face_m", 0)
        ref_count = ref.get("holdings_count", 0)
        cur_face = entry["total_face_m"]
        cur_count = entry["holdings_count"]

        if ref_face > 0 and ref_count > 0:
            face_ratio = cur_face / ref_face
            count_ratio = cur_count / ref_count
            if (face_ratio < 0.5 or face_ratio > 2.0) and (count_ratio < 0.5 or count_ratio > 2.0):
                sanity_warning = (
                    f"⚠️  SANITY CHECK: {ticker}/{period} face=${cur_face:,.0f}M ({cur_count} holdings) "
                    f"vs {nearest_p} face=${ref_face:,.0f}M ({ref_count} holdings) — "
                    f"face {face_ratio:.1f}x, count {count_ratio:.1f}x. "
                    f"Possible rate-limited/truncated fetch."
                )
            elif face_ratio < 0.7 or face_ratio > 1.5:
                sanity_warning = (
                    f"⚠️  SANITY NOTE: {ticker}/{period} face=${cur_face:,.0f}M "
                    f"vs {nearest_p} face=${ref_face:,.0f}M ({face_ratio:.1f}x). "
                    f"Holdings: {cur_count} vs {ref_count}."
                )

    if sanity_warning:
        print(f"\n{sanity_warning}", file=sys.stderr)

    _save_period_entry(ticker, period, entry, workspace_dir)
    return entry


def list_db(workspace_dir: str | Path) -> None:
    """Show current DB status in workspace state."""
    state_dir = ensure_workspace_state(workspace_dir)
    data_dir = state_dir / "data"
    print(f'{"Ticker":8s} {"Periods":40s} {"Holdings":>10s}')
    print("-" * 62)
    total_periods = 0
    ticker_files = [p for p in sorted(data_dir.glob("*.json")) if not p.name.startswith("_")]
    for path in ticker_files:
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
        ticker = db.get("ticker", path.stem)
        periods = db.get("periods", {})
        period_strs = []
        total_h = 0
        for p, entry in sorted(periods.items()):
            count = entry.get("holdings_count", 0)
            period_strs.append(f"{p}({count})")
            total_h += count
        total_periods += len(periods)
        print(f'{ticker:8s} {", ".join(period_strs):40s} {total_h:>10d}')
    print(f"\nTotal: {len(ticker_files)} tickers, {total_periods} periods")


def main() -> None:
    parser = argparse.ArgumentParser(description="BDC Holdings Harvester")
    parser.add_argument(
        "--workspace-dir",
        required=True,
        help="Workspace root directory (writes to <workspace>/state/dalton-bdc-analyst/)",
    )
    parser.add_argument("--ticker", type=str, help='Ticker or "all"')
    parser.add_argument("--period", type=str, help="Period like 25Q4")
    parser.add_argument("--force", action="store_true", help="Re-harvest even if exists")
    parser.add_argument("--list", action="store_true", help="Show DB status")
    args = parser.parse_args()

    try:
        state_dir = ensure_workspace_state(args.workspace_dir)
    except PermissionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.list:
        list_db(args.workspace_dir)
        return

    if not args.ticker or not args.period:
        parser.error("--ticker and --period are required (or use --list)")

    period = args.period.upper()
    _parse_period(period)

    if args.ticker.upper() == "ALL":
        tickers = sorted(PROFILE_MAP.keys())
    else:
        tickers = [t.strip().upper() for t in args.ticker.split(",")]
    is_all_mode = args.ticker.upper() == "ALL"

    success = 0
    fail = 0
    skip = 0
    skipped_unavailable: list[tuple[str, str]] = []
    skipped_not_filed: list[str] = []
    failures: list[tuple[str, str]] = []

    for ticker in tickers:
        if ticker in UNSUPPORTED:
            print(f"  ⏭ {ticker}: unsupported", file=sys.stderr)
            skip += 1
            continue

        if is_all_mode and ticker in SEC_TICKER_UNAVAILABLE:
            reason = SEC_TICKER_UNAVAILABLE[ticker]
            print(f"  ⏭ {ticker}: {reason}（默认跳过，历史数据仍可供 compare 读取）", file=sys.stderr)
            skipped_unavailable.append((ticker, reason))
            skip += 1
            continue

        t0 = time.time()
        try:
            db = _load_db(ticker, state_dir)
            if period in db.get("periods", {}) and not args.force:
                count = db["periods"][period].get("holdings_count", 0)
                print(f"  ⏭ {ticker}/{period}: already harvested ({count} holdings)")
                skip += 1
                continue

            print(f"  → {ticker}/{period}...", end="", flush=True, file=sys.stderr)
            t0 = time.time()
            entry = harvest_one(ticker, period, force=args.force, workspace_dir=args.workspace_dir)
            elapsed = time.time() - t0
            if entry:
                count = entry.get("holdings_count", 0)

                db_check = _load_db(ticker, state_dir)
                other_periods = {
                    k: v
                    for k, v in db_check.get("periods", {}).items()
                    if k != period and v.get("holdings_count", 0) > 0
                }
                needs_retry = False
                if other_periods and count > 0:
                    ref_counts = [v["holdings_count"] for v in other_periods.values()]
                    ref_faces = [v["total_face_m"] for v in other_periods.values()]
                    median_count = sorted(ref_counts)[len(ref_counts) // 2]
                    median_face = sorted(ref_faces)[len(ref_faces) // 2]
                    if median_count > 0 and median_face > 0:
                        face_ratio = entry["total_face_m"] / median_face
                        count_ratio = count / median_count
                        if (face_ratio < 0.5 or face_ratio > 2.0) and (
                            count_ratio < 0.5 or count_ratio > 2.0
                        ):
                            needs_retry = True

                if needs_retry:
                    print(
                        f" ⚠️ {count} holdings ({elapsed:.1f}s) — suspicious, retrying after 15s...",
                        file=sys.stderr,
                    )
                    time.sleep(15)
                    print(f"  → {ticker}/{period} (retry)...", end="", flush=True, file=sys.stderr)
                    t0 = time.time()
                    entry = harvest_one(
                        ticker, period, force=True, workspace_dir=args.workspace_dir
                    )
                    elapsed = time.time() - t0
                    if entry:
                        count = entry.get("holdings_count", 0)
                        print(f" ✓ {count} holdings ({elapsed:.1f}s)", file=sys.stderr)
                        success += 1
                    else:
                        print(f" ✗ retry failed ({elapsed:.1f}s)", file=sys.stderr)
                        failures.append((ticker, "retry returned no data"))
                        fail += 1
                else:
                    print(f" ✓ {count} holdings ({elapsed:.1f}s)", file=sys.stderr)
                    success += 1
            else:
                print(f" ✗ failed ({elapsed:.1f}s)", file=sys.stderr)
                failures.append((ticker, "harvest returned no data"))
                fail += 1
        except PeriodNotFiledError:
            skipped_not_filed.append(ticker)
            skip += 1
        except Exception as e:
            elapsed = time.time() - t0
            err_msg = f"{type(e).__name__}: {e}"
            print(f" ✗ {err_msg} ({elapsed:.1f}s)", file=sys.stderr)
            failures.append((ticker, err_msg))
            fail += 1

        time.sleep(2)

    print(f"\nDone: {success} harvested, {skip} skipped, {fail} failed", file=sys.stderr)
    if skipped_unavailable:
        skipped_str = ", ".join(f"{t} ({r})" for t, r in skipped_unavailable)
        print(f"Skipped (SEC ticker unavailable): {skipped_str}", file=sys.stderr)
    if skipped_not_filed:
        print(
            f"尚未申报（跳过）({len(skipped_not_filed)}): {', '.join(skipped_not_filed)}",
            file=sys.stderr,
        )
    if failures:
        print("失败 / Failed tickers:", file=sys.stderr)
        for t, reason in failures:
            print(f"  - {t}: {reason}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
