#!/usr/bin/env python3
"""Bootstrap Dalton autonomous watch automations (`dalton-*`) for covered companies and Home (PK.15).

Usage:
    uv run python scripts/dalton/bootstrap_watch.py --tickers META,AMZN --dry-run
    uv run python scripts/dalton/bootstrap_watch.py --tickers META,AMZN --include-global
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.autonomy.schedule import (  # noqa: E402
    build_company_watch_specs,
    build_global_director_specs,
    schedule_dalton_automation_helper,
)


async def _resolve_workspace_id(ticker: str, user_id: str) -> str | None:
    from psycopg.rows import dict_row
    from src.server.database.pool import get_db_connection

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id
                FROM workspaces
                WHERE UPPER(name) = %s AND user_id = %s AND status <> 'deleted'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (ticker.upper(), user_id),
            )
            row = await cur.fetchone()
            return str(row["workspace_id"]) if row else None


async def _seed_workspace_cik(workspace_id: str, ticker: str) -> None:
    """Seed resolved SEC CIK into ``dalton_watch_state`` on bootstrap (G4.4)."""
    try:
        from src.dalton.autonomy.probes.sec import resolve_company_cik
        from src.dalton.database import watch as watch_db

        state = await watch_db.get_watch_state(workspace_id)
        if not state.get("cik"):
            cik = await resolve_company_cik(
                ticker,
                watch_state=state,
                workspace_id=workspace_id,
                allow_network=False,
            )
            if cik:
                state["cik"] = cik
                await watch_db.upsert_watch_state(workspace_id, ticker, state)
    except Exception:
        pass


async def _upsert_automation_spec(user_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    return await schedule_dalton_automation_helper(
        user_id,
        spec["name"],
        spec,
        workspace_id=spec.get("workspace_id"),
    )


async def main_async(args: argparse.Namespace) -> int:
    tickers = [t.strip().upper() for t in (args.tickers or "").split(",") if t.strip()]
    specs: list[dict[str, Any]] = []

    for ticker in tickers:
        ws_id = args.workspace_id
        if not ws_id and not args.dry_run:
            ws_id = await _resolve_workspace_id(ticker, args.user_id)
        if not ws_id:
            ws_id = f"dry-run-{ticker.lower()}"
        elif not args.dry_run and not ws_id.startswith("dry-run-"):
            await _seed_workspace_cik(ws_id, ticker)
        specs.extend(
            build_company_watch_specs(
                ticker=ticker,
                workspace_id=ws_id,
                tier=args.tier,
                timezone_name=args.timezone,
            )
        )

    if args.include_global:
        specs.extend(build_global_director_specs(timezone_name=args.timezone))

    if args.dry_run:
        print(json.dumps({"dry_run": True, "count": len(specs), "specs": specs}, ensure_ascii=False, indent=2, default=str))
        return 0

    results = []
    for spec in specs:
        res = await _upsert_automation_spec(args.user_id, spec)
        results.append(res)

    print(json.dumps({"dry_run": False, "count": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap Dalton autonomous watch automations.")
    parser.add_argument("--tickers", default="", help="Comma-separated tickers (e.g. META,AMZN,GOOGL,MSFT)")
    parser.add_argument("--tier", default="Core", help="Coverage tier (Core or Watch)")
    parser.add_argument("--workspace-id", default=None, help="Explicit workspace UUID (single ticker)")
    parser.add_argument("--user-id", default="local_dev_user", help="Owner user_id")
    parser.add_argument("--timezone", default=None, help="Override IANA timezone")
    parser.add_argument("--include-global", action="store_true", help="Also bootstrap Home director & calendar automations")
    parser.add_argument("--dry-run", action="store_true", help="Print specs without writing to Postgres")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
