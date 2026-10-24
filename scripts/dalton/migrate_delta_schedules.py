#!/usr/bin/env python3
"""One-time migration for existing ``dalton-*-delta`` automation schedules.

Switches all ``dalton-*-delta`` tasks to ``Asia/Shanghai`` and tier-specific
frequencies (Core: ``0 11 * * 2-6``, Watch: ``0 11 * * 6``) while recalculating
``next_run_at`` via ``AutomationScheduler.calculate_first_run``.

Usage:
  .venv/bin/python scripts/dalton/migrate_delta_schedules.py           # dry-run (default)
  .venv/bin/python scripts/dalton/migrate_delta_schedules.py --apply   # apply in one transaction
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
from pathlib import Path
import sys
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton.migrate_owner_identity import (  # noqa: E402
    build_conninfo,
    resolve_db_params,
)
from src.dalton.autonomy.schedule import resolve_delta_schedule  # noqa: E402
from src.dalton.autonomy.tracking import resolve_default_tier  # noqa: E402

PG_LOCK_OPTIONS = "-c lock_timeout=5000 -c statement_timeout=60000"
_DELTA_NAME_RE = re.compile(r"^dalton-([a-z0-9.]+)-delta$")


def calculate_first_run(cron_expression: str, tz_name: str) -> datetime:
    """Delegate next-run calculation to ``AutomationScheduler.calculate_first_run``."""
    from src.server.services.automation_scheduler import AutomationScheduler

    return AutomationScheduler.calculate_first_run(cron_expression, tz_name)


def extract_ticker_from_delta_row(row: dict[str, Any]) -> str:
    """Resolve uppercase ticker from ``metadata.ticker`` or ``dalton-<ticker>-delta`` name."""
    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    meta_ticker = str(meta.get("ticker") or "").strip().upper()
    if meta_ticker:
        return meta_ticker
    name = str(row.get("name") or "").strip().lower()
    match = _DELTA_NAME_RE.match(name)
    if match:
        return match.group(1).upper()
    raise ValueError(f"Cannot extract ticker from automation row: {row.get('name')!r}")


def _iso_or_none(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat()
    return str(val)


def build_migration_plan(
    rows: list[dict[str, Any]],
    *,
    tier_resolver: Callable[[str], str] = resolve_default_tier,
    first_run_calculator: Callable[[str, str], datetime] = calculate_first_run,
) -> list[dict[str, Any]]:
    """Build per-row migration plan with resolved tier, new cron/timezone, and new ``next_run_at``."""
    plan: list[dict[str, Any]] = []
    for row in rows:
        ticker = extract_ticker_from_delta_row(row)
        tier = str(tier_resolver(ticker) or "watch").strip().lower()
        new_cron, new_tz = resolve_delta_schedule(tier)
        new_next_run_at = first_run_calculator(new_cron, new_tz)

        old_meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        new_meta = dict(old_meta)
        new_meta["ticker"] = ticker
        new_meta["tier"] = tier

        plan.append(
            {
                "automation_id": str(row["automation_id"]),
                "name": str(row["name"]),
                "ticker": ticker,
                "tier": tier,
                "status": str(row.get("status") or ""),
                "old_cron_expression": row.get("cron_expression"),
                "old_timezone": row.get("timezone"),
                "old_next_run_at": _iso_or_none(row.get("next_run_at")),
                "new_cron_expression": new_cron,
                "new_timezone": new_tz,
                "new_next_run_at": new_next_run_at,
                "new_next_run_at_iso": _iso_or_none(new_next_run_at),
                "new_metadata": new_meta,
            }
        )
    return plan


def run_migration(
    conninfo: str | None = None,
    *,
    apply: bool = False,
    expect_count: int | None = None,
    connect_fn: Callable[..., Any] = psycopg.connect,
    tier_resolver: Callable[[str], str] = resolve_default_tier,
    first_run_calculator: Callable[[str, str], datetime] = calculate_first_run,
) -> dict[str, Any]:
    """Inspect (dry-run) or update (``--apply``) all ``dalton-*-delta`` rows in a single transaction."""
    resolved_conninfo = conninfo or build_conninfo(resolve_db_params())
    select_sql = """
        SELECT
            automation_id::text AS automation_id,
            user_id,
            workspace_id::text AS workspace_id,
            name,
            status,
            cron_expression,
            timezone,
            next_run_at,
            metadata
        FROM automations
        WHERE name ~ '^dalton-[a-z0-9.]+-delta$'
        ORDER BY name ASC, automation_id ASC
    """
    if apply:
        select_sql = select_sql.rstrip() + "\n        FOR UPDATE"

    with connect_fn(resolved_conninfo, options=PG_LOCK_OPTIONS) as conn:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            cur.execute(select_sql)
            rows = list(cur.fetchall() or [])

            plan = build_migration_plan(
                rows,
                tier_resolver=tier_resolver,
                first_run_calculator=first_run_calculator,
            )

            if expect_count is not None and len(plan) != expect_count:
                raise AssertionError(
                    f"Expected {expect_count} dalton-*-delta rows, found {len(plan)}"
                )

            updated_count = 0
            if apply:
                for item in plan:
                    cur.execute(
                        """
                        UPDATE automations
                        SET cron_expression = %s,
                            timezone = %s,
                            next_run_at = %s,
                            metadata = %s,
                            updated_at = NOW()
                        WHERE automation_id = %s::uuid
                        """,
                        (
                            item["new_cron_expression"],
                            item["new_timezone"],
                            item["new_next_run_at"],
                            Jsonb(item["new_metadata"]),
                            item["automation_id"],
                        ),
                    )
                    if cur.rowcount != 1:
                        raise AssertionError(
                            f"Expected rowcount == 1 for {item['name']} ({item['automation_id']}), "
                            f"got {cur.rowcount}"
                        )
                    updated_count += int(cur.rowcount)

                if updated_count != len(plan):
                    raise AssertionError(
                        f"Expected total updated_count == {len(plan)}, got {updated_count}"
                    )

    core_count = sum(1 for item in plan if item["tier"] == "core")
    watch_count = sum(1 for item in plan if item["tier"] != "core")
    serializable_items = [
        {k: v for k, v in item.items() if k not in {"new_next_run_at", "new_metadata"}}
        for item in plan
    ]
    return {
        "mode": "apply" if apply else "dry-run",
        "matched_count": len(plan),
        "updated_count": updated_count,
        "core_count": core_count,
        "watch_count": watch_count,
        "items": serializable_items,
    }


def _format_report(report: dict[str, Any]) -> str:
    lines = [
        f"[{report['mode'].upper()}] matched={report['matched_count']} "
        f"(core={report['core_count']}, watch={report['watch_count']}) "
        f"updated={report['updated_count']}"
    ]
    for item in report.get("items", []):
        lines.append(
            f"  - {item['name']} (ticker={item['ticker']}, tier={item['tier']}): "
            f"{item['old_cron_expression']} [{item['old_timezone']}] -> "
            f"{item['new_cron_expression']} [{item['new_timezone']}], "
            f"next_run_at={item['new_next_run_at_iso']}"
        )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate dalton-*-delta automations to Asia/Shanghai and tier-specific "
            "cron schedules (Core: '0 11 * * 2-6', Watch: '0 11 * * 6')."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes in a single DB transaction (default is dry-run).",
    )
    parser.add_argument(
        "--expect-count",
        type=int,
        default=None,
        help="Assert exact number of matched dalton-*-delta rows before applying.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output migration plan and summary as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    report = run_migration(apply=bool(args.apply), expect_count=args.expect_count)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(_format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
