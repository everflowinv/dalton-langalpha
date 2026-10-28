#!/usr/bin/env python3
"""Read-only backup and verification script for migration 096 (`user_preferences.agent_preference`).

Migration `096_rank_output_formats` has a lossy downgrade (`deliverable` is merged into
`output_format` on upgrade and deleted on downgrade). Run this script before applying 096
to snapshot all rows matched by 096's WHERE clause, and run with `--verify` afterwards to
inspect how many backed-up rows changed in the database.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

# First statement of each transaction: a session-level SET issued after
# psycopg has already opened the transaction would only govern later ones.
READ_ONLY_SQL = "SET TRANSACTION READ ONLY"

BACKUP_SELECT_SQL = """
SELECT user_id, agent_preference
FROM user_preferences
WHERE agent_preference ?| ARRAY['output_format', 'deliverable']
ORDER BY user_id
"""

VERIFY_SELECT_SQL = """
SELECT user_id, agent_preference
FROM user_preferences
WHERE user_id = ANY(%s)
ORDER BY user_id
"""


def _normalize_pref(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _row_user_id_and_pref(row: Any) -> tuple[str, Any]:
    if isinstance(row, dict):
        return str(row["user_id"]), _normalize_pref(row["agent_preference"])
    user_id, pref = row
    return str(user_id), _normalize_pref(pref)


def open_read_only_connection(database_url: str | None = None) -> psycopg.Connection[Any]:
    url = (database_url if database_url is not None else os.environ.get("DATABASE_URL", "")).strip()
    if not url:
        raise RuntimeError("DATABASE_URL environment variable is required")
    conn = psycopg.connect(url, row_factory=dict_row)
    conn.read_only = True
    return conn


def backup_agent_preferences(
    backup_path: str | Path,
    *,
    conn: psycopg.Connection[Any] | None = None,
    database_url: str | None = None,
) -> list[dict[str, Any]]:
    path = Path(backup_path)
    owns_conn = conn is None
    active_conn = conn if conn is not None else open_read_only_connection(database_url)
    try:
        with active_conn.cursor() as cur:
            cur.execute(READ_ONLY_SQL)
            cur.execute(BACKUP_SELECT_SQL)
            raw_rows = cur.fetchall()
    finally:
        if owns_conn:
            active_conn.close()

    records: list[dict[str, Any]] = []
    for raw in raw_rows:
        user_id, pref = _row_user_id_and_pref(raw)
        records.append({"user_id": user_id, "agent_preference": pref})

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(records, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Backed up {len(records)} rows to {path}")
    return records


def verify_agent_preferences(
    backup_path: str | Path,
    *,
    conn: psycopg.Connection[Any] | None = None,
    database_url: str | None = None,
) -> dict[str, int]:
    path = Path(backup_path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, list):
        raise ValueError(f"Expected JSON list in {path}")

    expected_by_user: dict[str, Any] = {}
    for item in loaded:
        user_id, pref = _row_user_id_and_pref(item)
        expected_by_user[user_id] = pref

    user_ids = list(expected_by_user.keys())
    owns_conn = conn is None
    active_conn = conn if conn is not None else open_read_only_connection(database_url)
    try:
        with active_conn.cursor() as cur:
            cur.execute(READ_ONLY_SQL)
            if user_ids:
                cur.execute(VERIFY_SELECT_SQL, (user_ids,))
                raw_rows = cur.fetchall()
            else:
                raw_rows = []
    finally:
        if owns_conn:
            active_conn.close()

    current_by_user: dict[str, Any] = {}
    for raw in raw_rows:
        user_id, pref = _row_user_id_and_pref(raw)
        current_by_user[user_id] = pref

    changed = sum(
        1
        for uid, old_pref in expected_by_user.items()
        if uid not in current_by_user or current_by_user[uid] != old_pref
    )
    total = len(expected_by_user)
    unchanged = total - changed
    print(f"Verified {total} rows against {path}: {changed} changed, {unchanged} unchanged")
    return {"total": total, "changed": changed, "unchanged": unchanged}


def main(
    argv: list[str] | None = None,
    *,
    conn: psycopg.Connection[Any] | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only backup and verification for migration 096 agent_preference rows.",
    )
    parser.add_argument(
        "backup_path",
        help="Path to the JSON backup file to write (default) or read (--verify).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Compare the JSON backup file against current database rows in read-only mode.",
    )
    args = parser.parse_args(argv)

    if args.verify:
        verify_agent_preferences(args.backup_path, conn=conn)
    else:
        backup_agent_preferences(args.backup_path, conn=conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
