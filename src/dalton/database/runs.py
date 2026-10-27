"""Read-only run-chain helpers for Dalton hand-off report-back."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from src.server.database import pool
from src.server.utils.pg_sanitize import normalize_uuid


async def get_previous_run_on_thread(
    thread_id: str, run_id: str
) -> dict[str, Any] | None:
    """Return the run immediately preceding ``run_id`` on ``thread_id`` by ``run_seq``."""
    thread, run = normalize_uuid(thread_id), normalize_uuid(run_id)
    if thread is None or run is None:
        return None
    async with (
        pool.get_db_connection() as conn,
        conn.cursor(row_factory=dict_row) as cur,
    ):
        await cur.execute(
            """
            SELECT conversation_response_id, status
            FROM conversation_responses
            WHERE conversation_thread_id = %s
              AND run_seq < (
                  SELECT run_seq FROM conversation_responses
                  WHERE conversation_response_id = %s
                    AND conversation_thread_id = %s
              )
            ORDER BY run_seq DESC
            LIMIT 1
            """,
            (thread, run, thread),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "conversation_response_id": str(row["conversation_response_id"]),
        "status": str(row["status"]),
    }
