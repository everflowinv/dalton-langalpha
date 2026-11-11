"""Authorize a running market-digest execution to read its prepared batch.

The caller is the server identity. A report profile on the task is necessary
and not sufficient: the locked execution must belong to that same owner,
workspace, thread, and run, and it must still be running. This read does not
check delivery preferences and does not write.
"""

from __future__ import annotations

import re
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

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PERIOD_PROFILE = {"AM": "market-digest-am", "PM": "market-digest-pm"}
_MESSAGES = {
    "not_automation_run": "This turn is not a server-started automation run.",
    "invalid_execution": "Server run identity does not match a running automation execution.",
    "owner_mismatch": "Server run identity does not match this execution.",
    "workspace_mismatch": "Server run identity does not match this execution.",
    "thread_mismatch": "Server run identity does not match this execution.",
    "run_mismatch": "Server run identity does not match this execution.",
    "execution_not_running": "This automation execution is not running.",
    "profile_invalid": "This automation has no market-digest report profile.",
    "profile_mismatch": "This automation profile does not match the requested period.",
    "invalid_period": "period must be AM or PM.",
}


def _deny(code: str) -> dict[str, Any]:
    return {
        "ok": False,
        "code": code,
        "error": _MESSAGES.get(code, "This turn cannot read market-digest inputs."),
    }


async def authorize_market_digest_execution(
    *,
    user_id: str,
    workspace_id: str,
    thread_id: str,
    run_id: str,
    automation_id: str,
    execution_id: str,
    period: str,
    conn: Any = None,
) -> dict[str, Any]:
    """Return ``ok`` only for this running market-digest execution.

    Malformed ids are refused before any UUID cast. The transaction ends
    before this function returns.
    """
    if period not in _PERIOD_PROFILE:
        return _deny("invalid_period")
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
                expected = _PERIOD_PROFILE[period]
                if parsed.get("profile") != expected:
                    return _deny("profile_mismatch")
                return {
                    "ok": True,
                    "code": "ok",
                    "profile": expected,
                    "period": period,
                }


__all__ = ["authorize_market_digest_execution"]
