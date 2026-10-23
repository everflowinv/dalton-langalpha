"""Raw psycopg3 SQL queries for native-model sync runs (`dalton_openclaw_sync_runs`, `source='native'`).

Check and sync each record one run row in the shared sync-run table (the `source`
column added by d012 keeps them apart from OpenClaw runs). A Postgres advisory
lock — separate from OpenClaw's — serializes native check/sync executions across
workers.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

NATIVE_RUN_SOURCE = "native"


async def try_acquire_native_sync_lock(conn: Any) -> bool:
    """Try to acquire session-level advisory lock `dalton:native_model_sync`."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT pg_try_advisory_lock(hashtext('dalton:native_model_sync')) AS locked"
        )
        row = await cur.fetchone()
        return bool(row and row.get("locked"))


async def release_native_sync_lock(conn: Any) -> None:
    """Release session-level advisory lock `dalton:native_model_sync`."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT pg_advisory_unlock(hashtext('dalton:native_model_sync'))")


async def create_native_sync_run(
    *,
    started_by: str,
    kind: str,
    conn: Any | None = None,
) -> str:
    """Insert a native run row (`kind` is `check` or `sync`; check rides `dry_run=TRUE`)."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_openclaw_sync_runs
                    (started_by, dry_run, forced, status, source)
                VALUES (%s, %s, FALSE, 'running', %s)
                RETURNING run_id::text AS run_id
                """,
                (started_by, kind == "check", NATIVE_RUN_SOURCE),
            )
            row = await cur.fetchone()
            assert row is not None
            return str(row["run_id"])


async def finish_native_sync_run(
    *,
    run_id: str,
    status: str,
    counts: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    error: str | None = None,
    overlay_stamp: str | None = None,
    conn: Any | None = None,
) -> None:
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_openclaw_sync_runs
                SET status = %s,
                    finished_at = NOW(),
                    counts = %s::jsonb,
                    diff = %s::jsonb,
                    warnings = %s::jsonb,
                    error = %s,
                    overlay_stamp = %s
                WHERE run_id = %s AND source = 'native'
                """,
                (
                    status,
                    json.dumps(counts) if counts is not None else None,
                    json.dumps(diff) if diff is not None else None,
                    json.dumps(warnings) if warnings is not None else None,
                    error,
                    overlay_stamp,
                    run_id,
                ),
            )


def _normalize_run_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    out = dict(row)
    out["run_id"] = str(out["run_id"])
    # Same 5-minute staleness rule as OpenClaw runs (openclaw_sync._normalize_sync_row).
    if out.get("status") == "running" and out.get("is_stale_running"):
        out["status"] = "interrupted"
    out.pop("is_stale_running", None)
    for ts_key in ("started_at", "finished_at"):
        val = out.get(ts_key)
        if hasattr(val, "isoformat"):
            out[ts_key] = val.isoformat()
    return out


async def fetch_recent_native_runs() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return `(last_check_run, last_sync_run)` from native runs (any status)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT *,
                    (status = 'running' AND started_at < NOW() - INTERVAL '5 minutes')
                        AS is_stale_running
                FROM dalton_openclaw_sync_runs
                WHERE source = 'native' AND dry_run = TRUE
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            last_check = _normalize_run_row(await cur.fetchone())

            await cur.execute(
                """
                SELECT *,
                    (status = 'running' AND started_at < NOW() - INTERVAL '5 minutes')
                        AS is_stale_running
                FROM dalton_openclaw_sync_runs
                WHERE source = 'native' AND dry_run = FALSE
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            last_sync = _normalize_run_row(await cur.fetchone())
            return last_check, last_sync


async def fetch_last_succeeded_native_check() -> dict[str, Any] | None:
    """Return the latest succeeded native check run (its diff feeds a confirm-sync)."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT *, FALSE AS is_stale_running
                FROM dalton_openclaw_sync_runs
                WHERE source = 'native' AND dry_run = TRUE AND status = 'succeeded'
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            return _normalize_run_row(await cur.fetchone())
