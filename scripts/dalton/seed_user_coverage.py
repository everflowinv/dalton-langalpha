#!/usr/bin/env python3
"""Seed per-user coverage roster in `dalton_coverage` from a `coverage.yaml` file.

Usage:
  uv run python scripts/dalton/seed_user_coverage.py --user <id> --from config/dalton/coverage.yaml [--apply]
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

DEFAULT_COVERAGE_YAML = REPO_ROOT / "config" / "dalton" / "coverage.yaml"


async def run_seed(
    *,
    user_id: str,
    yaml_path: Path,
    apply: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """Run dry-run or transactional seed of `dalton_coverage` for `user_id`."""
    from src.dalton.coverage.service import seed_user_coverage_from_yaml
    from src.server.database import pool as pool_mod

    if not apply or conn is not None:
        return await seed_user_coverage_from_yaml(
            user_id=user_id,
            yaml_path=yaml_path,
            apply=apply,
            conn=conn,
        )

    pool = pool_mod.get_or_create_pool()
    await pool.open()
    try:
        return await seed_user_coverage_from_yaml(
            user_id=user_id,
            yaml_path=yaml_path,
            apply=True,
            conn=conn,
        )
    finally:
        await pool.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seed per-user Dalton coverage from coverage.yaml into dalton_coverage"
    )
    parser.add_argument(
        "--user",
        required=True,
        help="Target user_id to seed coverage for",
    )
    parser.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        default=DEFAULT_COVERAGE_YAML,
        help="Source coverage YAML path (default: config/dalton/coverage.yaml)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write rows in a transaction (default: dry-run)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    uid = str(args.user or "").strip()
    if not uid:
        print("ERROR: --user must not be empty", file=sys.stderr)
        return 1

    res = asyncio.run(
        run_seed(
            user_id=uid,
            yaml_path=args.from_path,
            apply=bool(args.apply),
        )
    )
    if args.apply:
        print(
            f"[seed] Applied {res['written_count']} companies (Core={res['core_count']}, "
            f"Watch={res['watch_count']}) for user={uid} from {args.from_path}"
        )
    else:
        print(
            f"[seed dry-run] Would write {res['planned_count']} companies (Core={res['core_count']}, "
            f"Watch={res['watch_count']}) for user={uid} from {args.from_path}"
        )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
