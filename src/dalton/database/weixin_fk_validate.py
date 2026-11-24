"""Post-deploy foreign key validator for migration ``d049_weixin_multi_account``.

Validates the 5 ``NOT VALID`` foreign key constraints created by ``d049`` each
in its own independent PostgreSQL transaction with ``SET LOCAL lock_timeout = '5s'``
so online reads and writes are never blocked (only ``SHARE UPDATE EXCLUSIVE`` is
acquired per constraint).

Shared logic invoked by both Everflow (``scripts/dalton/ops/prod/validate_d049_fks.sh``)
and Standalone Core (``scripts/standalone/validate_d049_fks.sh``).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg
from psycopg import sql

WEIXIN_D049_FK_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("dalton_channel_inbound_events", "fk_dalton_inbound_wx_account"),
    ("dalton_channel_inbound_events", "fk_dalton_inbound_wx_collect_buf"),
    ("dalton_channel_thread_watches", "fk_dalton_watches_wx_account"),
    ("dalton_channel_background_runs", "fk_dalton_bg_runs_wx_account"),
    ("dalton_channel_deliveries", "fk_dalton_deliveries_wx_account"),
)


def validate_d049_foreign_keys(
    dsn: str | None = None,
    *,
    lock_timeout: str = "5s",
) -> dict[str, Any]:
    """Validate all 5 ``d049`` foreign keys in separate transactions and verify ``convalidated``.

    Returns a summary dict ``{"ok": True, "validated": [...]}``; raises
    ``RuntimeError`` if any constraint is missing or fails validation.
    """
    if not dsn:
        from src.server.database.pool import get_db_connection_string

        dsn = get_db_connection_string()

    results: list[dict[str, Any]] = []
    with psycopg.connect(dsn, autocommit=False) as conn:
        for table_name, constraint_name in WEIXIN_D049_FK_CONSTRAINTS:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("SET LOCAL lock_timeout = {}").format(
                            sql.Literal(lock_timeout)
                        )
                    )
                    cur.execute(
                        sql.SQL("ALTER TABLE {} VALIDATE CONSTRAINT {}").format(
                            sql.Identifier(table_name),
                            sql.Identifier(constraint_name),
                        )
                    )
                    cur.execute(
                        """
                        SELECT convalidated
                        FROM pg_constraint
                        WHERE conname = %s
                          AND conrelid = %s::regclass
                        """,
                        (constraint_name, table_name),
                    )
                    row = cur.fetchone()
                    if row is None or not bool(row[0]):
                        raise RuntimeError(
                            f"Constraint {constraint_name} on {table_name} is not validated."
                        )
                    results.append(
                        {
                            "table": table_name,
                            "constraint": constraint_name,
                            "convalidated": True,
                        }
                    )

    return {"ok": True, "validated": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate d049 Weixin multi-account NOT VALID foreign keys post-deploy."
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help="Optional PostgreSQL DSN (defaults to backend pool DB connection string).",
    )
    parser.add_argument(
        "--lock-timeout",
        default="5s",
        help="PostgreSQL lock_timeout per constraint transaction (default: 5s).",
    )
    args = parser.parse_args(argv)
    summary = validate_d049_foreign_keys(dsn=args.dsn, lock_timeout=args.lock_timeout)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
