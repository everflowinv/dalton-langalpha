#!/usr/bin/env python3
"""Enrich company_db.json with industry/sub_industry/description.

Usage:
    python3 enrich_company_db.py --workspace-dir <workspace> [--batch N] [--dry-run]
    python3 enrich_company_db.py --workspace-dir <workspace> --stats
    python3 enrich_company_db.py --workspace-dir <workspace> --list-missing
    python3 enrich_company_db.py --workspace-dir <workspace> --apply <updates.json>

Reads <workspace>/state/dalton-bdc-analyst/company_db.json (or read-only seed if
--workspace-dir is omitted for read-only stats/listing), finds entries with empty
fields, and outputs a batch for the agent to enrich via web_search. Writes always
target the workspace state directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from state_store import (
    atomic_write_json,
    ensure_workspace_state,
    read_seed_json,
    resolve_safe_state_dir,
    state_lock,
    verify_safe_target,
)


def load_db(workspace_dir: str | Path | None = None) -> dict[str, Any]:
    if workspace_dir is not None:
        state_dir = ensure_workspace_state(workspace_dir)
        db_path = state_dir / "company_db.json"
        if db_path.is_file():
            with open(db_path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    return read_seed_json("company_db.json") or {}


def save_db(db: dict[str, Any], workspace_dir: str | Path | None = None) -> Path:
    if workspace_dir is None:
        raise RuntimeError("--workspace-dir is required to write company_db.json")
    state_dir = ensure_workspace_state(workspace_dir)
    resolved_ws, state_dir = resolve_safe_state_dir(workspace_dir)
    target = verify_safe_target(state_dir / "company_db.json", resolved_ws)
    with state_lock(state_dir, resolved_ws):
        verify_safe_target(target, resolved_ws)
        atomic_write_json(target, db, resolved_ws)
    return target


def apply_updates(updates: dict[str, Any], workspace_dir: str | Path) -> tuple[int, Path]:
    """Merge updates into company_db.json, reading and writing under one lock so two
    concurrent --apply runs each keep the other's entries."""
    state_dir = ensure_workspace_state(workspace_dir)
    resolved_ws, state_dir = resolve_safe_state_dir(workspace_dir)
    target = verify_safe_target(state_dir / "company_db.json", resolved_ws)
    with state_lock(state_dir, resolved_ws):
        verify_safe_target(target, resolved_ws)
        db: dict[str, Any] = {}
        if target.is_file():
            with open(target, "r", encoding="utf-8") as f:
                db = json.load(f)
        updated_count = 0
        for company, info in updates.items():
            if not isinstance(info, dict):
                continue
            existing = db.get(company)
            merged = dict(existing) if isinstance(existing, dict) else {}
            for field in ("industry", "sub_industry", "description"):
                if field in info:
                    merged[field] = info[field]
            db[company] = merged
            updated_count += 1
        atomic_write_json(target, db, resolved_ws)
    return updated_count, target


def get_missing(db: dict[str, Any]) -> list[str]:
    """Return list of company names with empty fields."""
    missing = []
    for name, info in db.items():
        if not info.get("industry") or not info.get("description"):
            missing.append(name)
    return missing


def print_stats(db: dict[str, Any]) -> None:
    total = len(db)
    filled = sum(1 for v in db.values() if v.get("industry") and v.get("description"))
    print(f"DB stats: {filled}/{total} filled ({total - filled} remaining)")


def main() -> None:
    parser = argparse.ArgumentParser(description="BDC Company DB Enrichment Helper")
    parser.add_argument(
        "--workspace-dir",
        type=str,
        default=None,
        help="Workspace root directory (<workspace>/state/dalton-bdc-analyst/company_db.json)",
    )
    parser.add_argument("--batch", type=int, default=50, help="Number of companies per batch")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stats", action="store_true", help="Just print stats")
    parser.add_argument("--list-missing", action="store_true", help="List missing entries")
    parser.add_argument(
        "--apply",
        type=str,
        default=None,
        help="Apply JSON mapping {company: {industry, sub_industry, description}} via save_db",
    )
    args = parser.parse_args()

    if args.apply:
        if not args.workspace_dir:
            print("ERROR: --workspace-dir is required with --apply", file=sys.stderr)
            sys.exit(1)
        try:
            with open(args.apply, "r", encoding="utf-8") as f:
                updates = json.load(f)
            if not isinstance(updates, dict):
                raise ValueError("--apply JSON root must be an object mapping company -> info")
            updated_count, target = apply_updates(updates, args.workspace_dir)
        except (PermissionError, RuntimeError, ValueError, OSError, json.JSONDecodeError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Applied {updated_count} updates to {target}")
        return

    try:
        db = load_db(args.workspace_dir)
    except PermissionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if args.stats:
        print_stats(db)
        return

    missing = get_missing(db)

    if args.list_missing:
        for name in missing[:50]:
            print(f"  {name}")
        if len(missing) > 50:
            print(f"  ... and {len(missing) - 50} more")
        print(f"\nTotal missing: {len(missing)}")
        return

    if not missing:
        print("All entries are filled!")
        return

    batch = missing[: args.batch]
    print(f"Processing {len(batch)} companies (of {len(missing)} missing)...")

    if args.dry_run:
        for name in batch:
            print(f"  Would search: {name}")
        return

    print(json.dumps(batch, ensure_ascii=False))


if __name__ == "__main__":
    main()
