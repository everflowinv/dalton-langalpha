"""Authorize a running ai-finance-daily execution to prepare and read its batch.

The caller is the server identity. A report profile on the task is necessary
and not sufficient: the locked execution must belong to that same owner,
workspace, thread, and run, and it must still be running. The transaction
ends before this function returns, so the connection is not held across HTTP.
This read does not write and does not invent a market-digest period.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.dalton.database.automation_reports import (
    _clean_identity,
    _early_identity_code,
    _identity_code,
    lock_report_execution,
    parse_report_profile,
)
from src.server.database.pool import get_db_connection

PROFILE = "ai-finance-daily"

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_EXECUTION_CLOCK_SQL = """
SELECT scheduled_at, started_at
FROM automation_executions
WHERE automation_execution_id = %s::uuid
  AND automation_id = %s::uuid
"""
_MESSAGES = {
    "not_automation_run": "This turn is not a server-started automation run.",
    "invalid_execution": "Server run identity does not match a running automation execution.",
    "owner_mismatch": "Server run identity does not match this execution.",
    "workspace_mismatch": "Server run identity does not match this execution.",
    "thread_mismatch": "Server run identity does not match this execution.",
    "run_mismatch": "Server run identity does not match this execution.",
    "execution_not_running": "This automation execution is not running.",
    "profile_invalid": "This automation has no ai-finance-daily report profile.",
    "profile_mismatch": "This automation profile is not ai-finance-daily.",
    "execution_not_started": "This automation execution has no started time.",
    "execution_clock_invalid": "This automation execution has no usable schedule clock.",
}


def _deny(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "error": _MESSAGES.get(code, "This turn cannot prepare AI-finance inputs."),
    }


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    if value.tzinfo.utcoffset(value) is None:
        return None
    return value


async def authorize_ai_finance_execution(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    run_id: str,
    automation_id: str,
    execution_id: str,
    conn: Any = None,
) -> dict[str, Any]:
    """Return ``ok`` only for this running ai-finance-daily execution.

    Malformed ids are refused before any UUID cast. ``scheduled_at`` and
    ``started_at`` come from the locked execution row. The transaction ends
    before this function returns.
    """
    identity = _clean_identity(
        user_id=user_id,
        workspace_id=workspace_id,
        thread_id=thread_id,
        run_id=run_id,
        automation_id=automation_id,
        execution_id=execution_id,
    )
    early = _early_identity_code(identity)
    if early:
        return _deny(early)
    if not _UUID_RE.fullmatch(identity["automation_id"]):
        return _deny("not_automation_run")
    async with get_db_connection(conn) as owned:
        async with owned.transaction():
            async with owned.cursor(row_factory=dict_row) as cur:
                row = await lock_report_execution(
                    cur,
                    identity["execution_id"],
                    for_update=False,
                )
                code = _identity_code(row, identity)
                if code:
                    return _deny(code)
                assert row is not None
                parsed, profile_err = parse_report_profile(row.get("metadata"))
                if profile_err or parsed is None:
                    return _deny("profile_invalid")
                if parsed.get("profile") != PROFILE:
                    return _deny("profile_mismatch")
                automation_id_text = str(row.get("automation_id") or "").strip()
                if not _UUID_RE.fullmatch(automation_id_text):
                    return _deny("invalid_execution")
                await cur.execute(
                    _EXECUTION_CLOCK_SQL,
                    (identity["execution_id"], automation_id_text),
                )
                clock = await cur.fetchone()
                if not isinstance(clock, dict):
                    return _deny("execution_clock_invalid")
                scheduled_at = _aware(clock.get("scheduled_at"))
                started_at = _aware(clock.get("started_at"))
                if scheduled_at is None:
                    return _deny("execution_clock_invalid")
                if started_at is None:
                    return _deny("execution_not_started")
                return {
                    "ok": True,
                    "code": "ok",
                    "profile": PROFILE,
                    "scheduled_at": scheduled_at.isoformat(),
                    "started_at": started_at.isoformat(),
                }


__all__ = ["PROFILE", "authorize_ai_finance_execution"]
