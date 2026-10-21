#!/usr/bin/env python3
"""Generate the Dalton weekly autonomy health and verdict distribution report (PK.20).

Usage:
    uv run python scripts/dalton/autonomy_week_report.py --days 7 --out var/dalton/reports/autonomy_week.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.autonomy.report import generate_weekly_autonomy_report  # noqa: E402


async def _run(days: int, user_id: str, out_path: str | None, as_json: bool) -> int:
    report = await generate_weekly_autonomy_report(days=days, user_id=user_id)
    if out_path:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(report["markdown"], encoding="utf-8")
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    else:
        print(report["markdown"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Dalton weekly autonomy report.")
    parser.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    parser.add_argument("--user-id", required=True, help="Owner user_id (required)")
    parser.add_argument("--out", default=None, help="Optional output markdown file path")
    parser.add_argument("--json", action="store_true", help="Print full JSON report to stdout")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.days, args.user_id, args.out, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
