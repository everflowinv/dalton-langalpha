#!/usr/bin/env python3
"""Thin CLI wrapper for Dalton data source health checks (`src/dalton/data_map/health.py`)."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.dalton.data_map.health import (  # noqa: E402
    DEFAULT_ITEM_TIMEOUT_S,
    DEFAULT_MAX_CONCURRENCY,
    HEALTH_ADVISORY_LOCK_KEY,
    MAX_ITEM_TIMEOUT_S,
    build_route_probe_spec,
    clamp_timeout_s,
    default_bridge_rpc_call,
    default_conversation_probe,
    default_market_quote_probe,
    default_sec_filings_probe,
    default_web_search_probe,
    default_x_search_probe,
    extract_latest_date,
    resolve_researchdata_endpoint_and_token,
    run_all_health_checks,
    run_startup_health_check_once,
    sanitize_health_text,
)
from src.server.database.pool import get_or_create_pool  # noqa: E402

__all__ = [
    "DEFAULT_ITEM_TIMEOUT_S",
    "DEFAULT_MAX_CONCURRENCY",
    "HEALTH_ADVISORY_LOCK_KEY",
    "MAX_ITEM_TIMEOUT_S",
    "build_arg_parser",
    "build_route_probe_spec",
    "clamp_timeout_s",
    "default_bridge_rpc_call",
    "default_conversation_probe",
    "default_market_quote_probe",
    "default_sec_filings_probe",
    "default_web_search_probe",
    "default_x_search_probe",
    "extract_latest_date",
    "main",
    "resolve_researchdata_endpoint_and_token",
    "run_all_health_checks",
    "run_startup_health_check_once",
    "sanitize_health_text",
]


async def _cli_async(args: argparse.Namespace) -> int:
    pool = None
    if not args.no_db:
        pool = get_or_create_pool()
        await pool.open()
    try:
        summary = await run_all_health_checks(
            item_timeout_s=clamp_timeout_s(float(args.timeout)),
            max_concurrency=args.concurrency,
            persist=not args.no_db,
            use_advisory_lock=not args.no_db,
        )
        if args.print_map and summary.get("data_map"):
            print(summary["data_map"]["rendered_markdown"])
        else:
            safe_out = {
                "ok": summary["ok"],
                "skipped": summary.get("skipped"),
                "checked_count": summary["checked_count"],
                "available_count": summary["available_count"],
                "unavailable_count": summary["unavailable_count"],
                "results": summary["results"],
                "token_estimate": (summary.get("data_map") or {}).get("token_estimate"),
            }
            print(json.dumps(safe_out, ensure_ascii=False, indent=2))
        return 0
    finally:
        if pool is not None:
            await pool.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dalton data source health check CLI (per-item timeout <= 60s)."
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_ITEM_TIMEOUT_S,
        help="Per-item timeout in seconds (clamped to <= 60s).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_MAX_CONCURRENCY,
        help="Max concurrent probes (1..8, default 4).",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Run probes without writing to PostgreSQL or acquiring the advisory lock.",
    )
    parser.add_argument(
        "--print-map",
        action="store_true",
        help="Print the rendered Data Map Markdown after updating.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return asyncio.run(_cli_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
