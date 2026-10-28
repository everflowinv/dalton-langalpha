"""Database queries for the controlled bug-report inbox (`dalton_report_issues`, E3)."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import strip_pg_nul_str

logger = logging.getLogger(__name__)

_ISSUE_COLS = "issue_id, ref, user_id, workspace_id, thread_id, title, severity, status, created_at"


async def create_report_issue(
    *,
    user_id: str,
    title: str,
    body: str,
    severity: str,
    workspace_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Insert one report row; returns the row with its human-trackable `ref`.

    The ref (``DALTON-<date>-<hex>``) is what the agent quotes back to the
    user, so it is generated here, in the insert's own return, rather than
    rebuilt from the serial id anywhere else.
    """
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    ref = f"DALTON-{day}-{secrets.token_hex(3)}"
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_report_issues (
                    ref, user_id, workspace_id, thread_id, title, body, severity
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING {_ISSUE_COLS}
                """,
                (
                    ref,
                    strip_pg_nul_str(user_id),
                    strip_pg_nul_str(workspace_id) if workspace_id else "",
                    strip_pg_nul_str(thread_id) if thread_id else "",
                    strip_pg_nul_str(title),
                    strip_pg_nul_str(body),
                    severity,
                ),
            )
            row = await cur.fetchone()
    return dict(row) if row else {}


async def get_report_issue(ref: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_ISSUE_COLS} FROM dalton_report_issues WHERE ref = %s",
                (ref,),
            )
            return await cur.fetchone()
    return None
