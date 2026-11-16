#!/usr/bin/env python3
"""
compare.py — BDC Holdings Comparator

Compares holdings between two periods using the workspace JSON database
(or the read-only built-in seed archive when --workspace-dir is omitted).
No SEC fetching — purely database-driven.

Usage:
    python3 compare.py --ticker ARCC --periodA 25Q4 --periodB 25Q3
    python3 compare.py --workspace-dir <workspace> --ticker all --periodA 25Q4 --periodB 25Q3
    python3 compare.py --workspace-dir <workspace> --ticker all --periodA 25Q4 --periodB 25Q3 --csv output.csv
    python3 compare.py --ticker ARCC --periodA 25Q4 --periodB 25Q3 --json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

from tabulate import tabulate

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from normalize_names import canonical_key as _canon_key
from state_store import ensure_workspace_state, list_seed_tickers, read_seed_json


def _load_period(
    ticker: str,
    period: str,
    *,
    state_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Load a period's holdings from workspace state (or read-only seed if state_dir is None)."""
    if state_dir is not None:
        path = state_dir / "data" / f"{ticker}.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = read_seed_json(f"data/{ticker}.json")
        if db is None:
            return None
    return db.get("periods", {}).get(period)


def compare_ticker(
    ticker: str,
    periodA: str,
    periodB: str,
    top_n: int = 20,
    min_face_pct: float = 0.001,
    *,
    workspace_dir: str | Path | None = None,
    state_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Compare holdings between two periods for a single ticker.

    Returns list of dicts with deteriorated/impaired holdings,
    sorted by ratio change (worst first).
    """
    if state_dir is None and workspace_dir is not None:
        state_dir = ensure_workspace_state(workspace_dir)

    entry_a = _load_period(ticker, periodA, state_dir=state_dir)
    entry_b = _load_period(ticker, periodB, state_dir=state_dir)

    if not entry_a or not entry_b:
        return []

    equity_a = entry_a.get("equity_usd", 0)

    def build_map(holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        m: dict[str, dict[str, Any]] = {}
        for h in holdings:
            key = _canon_key(h["company"])
            if key in m:
                m[key]["face_m"] += h["face_m"]
                m[key]["fair_m"] += h["fair_m"]
            else:
                m[key] = {**h, "canon_key": key}
        return m

    map_a = build_map(entry_a.get("holdings", []))
    map_b = build_map(entry_b.get("holdings", []))

    results = []
    for key in map_a:
        if key not in map_b:
            continue

        ha = map_a[key]
        hb = map_b[key]

        face_a, fair_a = ha["face_m"], ha["fair_m"]
        face_b, fair_b = hb["face_m"], hb["fair_m"]

        if equity_a > 0 and face_a < (equity_a / 1_000_000) * min_face_pct:
            continue

        if face_a <= 0 or face_b <= 0:
            continue

        ratio_a = fair_a / face_a
        ratio_b = fair_b / face_b

        ratio_change = ratio_a - ratio_b
        if ratio_a > 1.0:
            continue
        if ratio_change >= 0 and ratio_a >= 0.9:
            continue

        results.append(
            {
                "ticker": ticker,
                "company": ha["company"],
                "face_a": round(face_a, 2),
                "fair_a": round(fair_a, 2),
                "ratio_a": ratio_a,
                "face_b": round(face_b, 2),
                "fair_b": round(fair_b, 2),
                "ratio_b": ratio_b,
                "ratio_change": ratio_change,
                "industry": ha.get("industry", ""),
                "sub_industry": ha.get("sub_industry", ""),
                "description": ha.get("description", ""),
            }
        )

    results.sort(key=lambda x: x["ratio_change"])
    return results[:top_n]


def format_table(results: list[dict[str, Any]], periodA: str, periodB: str) -> str:
    """Format as markdown table."""
    if not results:
        return ""

    rows = []
    for r in results:
        rows.append(
            {
                "公司名": r["company"],
                f"{periodA} face value（金额百万美元，下同）": r["face_a"],
                f"{periodA} fair value": r["fair_a"],
                f"{periodA} face/fair（用百分比表示）": f'{r["ratio_a"]*100:.2f}%',
                f"{periodB} face": r["face_b"],
                f"{periodB} fair": r["fair_b"],
                f"{periodB} face/fair（用百分比表示）": f'{r["ratio_b"]*100:.2f}%',
                "期间face/fair变化": f'{r["ratio_change"]*100:.2f}%',
                "行业": r["industry"],
                "细分行业": r["sub_industry"],
                "公司主要业务简介": r["description"],
            }
        )

    return tabulate(rows, headers="keys", tablefmt="pipe", showindex=False)


def write_csv(all_results: list[dict[str, Any]], periodA: str, periodB: str, path: str) -> None:
    """Write all results to CSV."""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "BDC",
                "公司名",
                f"{periodA} Face(M)",
                f"{periodA} Fair(M)",
                f"{periodA} F/F",
                f"{periodB} Face(M)",
                f"{periodB} Fair(M)",
                f"{periodB} F/F",
                "QoQ变化",
                "行业",
                "细分行业",
                "业务简介",
            ]
        )
        for r in all_results:
            w.writerow(
                [
                    r["ticker"],
                    r["company"],
                    r["face_a"],
                    r["fair_a"],
                    f'{r["ratio_a"]*100:.2f}%',
                    r["face_b"],
                    r["fair_b"],
                    f'{r["ratio_b"]*100:.2f}%',
                    f'{r["ratio_change"]*100:.2f}%',
                    r["industry"],
                    r["sub_industry"],
                    r["description"],
                ]
            )
    print(f"Saved: {path} ({len(all_results)} rows)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="BDC Holdings Comparator")
    parser.add_argument(
        "--workspace-dir",
        type=str,
        default=None,
        help="Optional workspace root directory; if omitted, reads read-only built-in seed",
    )
    parser.add_argument("--ticker", required=True, help='Ticker or "all"')
    parser.add_argument("--periodA", required=True, help="Period A (e.g. 25Q4)")
    parser.add_argument("--periodB", required=True, help="Period B (e.g. 25Q3)")
    parser.add_argument("--top", type=int, default=20, help="Max results per ticker")
    parser.add_argument("--csv", type=str, help="Output CSV path")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()

    state_dir: Path | None = None
    if args.workspace_dir:
        try:
            state_dir = ensure_workspace_state(args.workspace_dir)
        except PermissionError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    periodA = args.periodA.upper()
    periodB = args.periodB.upper()

    if args.ticker.upper() == "ALL":
        if state_dir is not None:
            data_dir = state_dir / "data"
            tickers = sorted(
                [p.stem for p in data_dir.glob("*.json") if not p.name.startswith("_")]
            )
        else:
            tickers = list_seed_tickers()
    else:
        tickers = [t.strip().upper() for t in args.ticker.split(",")]

    all_results = []
    for ticker in tickers:
        results = compare_ticker(
            ticker, periodA, periodB, top_n=args.top, state_dir=state_dir
        )
        if results:
            all_results.extend(results)

    if args.json:
        print(json.dumps(all_results, indent=2, ensure_ascii=False))
    elif args.csv:
        write_csv(all_results, periodA, periodB, args.csv)
    else:
        for ticker in tickers:
            ticker_results = [r for r in all_results if r["ticker"] == ticker]
            if ticker_results:
                table = format_table(ticker_results, periodA, periodB)
                if table:
                    print(f"\n=== {ticker} ({len(ticker_results)} results) ===")
                    print(table)

    ticker_counts: dict[str, int] = {}
    for r in all_results:
        ticker_counts[r["ticker"]] = ticker_counts.get(r["ticker"], 0) + 1
    print(
        f"\nTotal: {len(all_results)} deteriorated holdings across {len(ticker_counts)} BDCs",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
