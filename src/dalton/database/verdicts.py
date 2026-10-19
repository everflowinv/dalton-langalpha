"""Database queries for append-only event verdicts (PK.3)."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import SafeJson, strip_pg_nul_str

_VERDICT_COLS = """
    verdict_id, user_id, workspace_id, verdict_at, word,
    trigger_kind, trigger_ref, progress, next_due, payload,
    conversation_thread_id, run_id, automation_execution_id,
    origin_type, created_at
"""


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        k: (str(v) if isinstance(v, UUID) else v)
        for k, v in row.items()
    }


def _day_str(day: date | datetime | str | None = None) -> str:
    if isinstance(day, str) and len(day) >= 8:
        clean = day[:10].replace("-", "")
        if len(clean) == 8 and clean.isdigit():
            return clean
    if isinstance(day, datetime):
        return day.astimezone(timezone.utc).strftime("%Y%m%d")
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def next_verdict_id(
    workspace_id: str,
    day: date | datetime | str | None = None,
    *,
    cur: Any = None,
) -> str:
    """Compute the next ``VD-YYYYMMDD-NN`` ID for ``workspace_id`` on ``day``."""
    day_stamp = _day_str(day)
    prefix = f"VD-{day_stamp}-"

    async def _query(c: Any) -> str:
        await c.execute(
            """
            SELECT verdict_id
            FROM dalton_verdicts
            WHERE verdict_id LIKE %s
            ORDER BY verdict_id DESC
            """,
            (f"{prefix}%",),
        )
        rows = await c.fetchall()
        max_n = 0
        pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        for r in rows:
            vid = str(r["verdict_id"] if isinstance(r, dict) else r[0])
            m = pat.match(vid)
            if m:
                max_n = max(max_n, int(m.group(1)))
        return f"{prefix}{max_n + 1:02d}"

    if cur is not None:
        return await _query(cur)

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as c:
            return await _query(c)


async def insert_verdict(
    *,
    user_id: str,
    workspace_id: str,
    verdict: dict[str, Any],
    thread_id: str | None = None,
    run_id: str | None = None,
    automation_execution_id: str | None = None,
    origin_type: str | None = None,
) -> dict[str, Any]:
    """Assign a ``VD-YYYYMMDD-NN`` ID under an advisory lock and insert into ``dalton_verdicts``."""
    at_raw = verdict.get("at")
    if isinstance(at_raw, str) and at_raw:
        try:
            verdict_at = datetime.fromisoformat(at_raw.replace("Z", "+00:00"))
        except ValueError:
            verdict_at = datetime.now(timezone.utc)
    elif isinstance(at_raw, datetime):
        verdict_at = at_raw
    else:
        verdict_at = datetime.now(timezone.utc)

    day_stamp = _day_str(verdict_at)
    lock_key = f"dalton:verdict:{day_stamp}"

    trigger = verdict.get("trigger") or {}
    trigger_kind = str(trigger.get("kind") or "manual")
    trigger_ref = trigger.get("ref")
    word = str(verdict.get("verdict") or "NO_CHANGE")
    progress = str(verdict.get("progress") or "none")
    next_val = verdict.get("next_validation") or {}
    next_due = next_val.get("due")

    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (lock_key,),
            )
            verdict_id = verdict.get("id")
            if not verdict_id:
                verdict_id = await next_verdict_id(
                    workspace_id, day_stamp, cur=cur
                )
            payload = {**verdict, "id": verdict_id}

            await cur.execute(
                f"""
                INSERT INTO dalton_verdicts (
                    verdict_id, user_id, workspace_id, verdict_at, word,
                    trigger_kind, trigger_ref, progress, next_due, payload,
                    conversation_thread_id, run_id, automation_execution_id,
                    origin_type
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s
                )
                RETURNING {_VERDICT_COLS}
                """,
                (
                    strip_pg_nul_str(verdict_id),
                    strip_pg_nul_str(user_id),
                    workspace_id,
                    verdict_at,
                    strip_pg_nul_str(word),
                    strip_pg_nul_str(trigger_kind),
                    strip_pg_nul_str(str(trigger_ref)) if trigger_ref else None,
                    strip_pg_nul_str(progress),
                    next_due,
                    SafeJson(payload),
                    thread_id,
                    run_id,
                    automation_execution_id
                    or trigger.get("automation_execution_id"),
                    strip_pg_nul_str(origin_type) if origin_type else None,
                ),
            )
            row = await cur.fetchone()
            return _serialize_row(row)  # type: ignore[return-value]


async def list_verdicts(
    workspace_id: str,
    limit: int = 60,
    since: datetime | str | None = None,
) -> list[dict[str, Any]]:
    """Return recent verdicts for ``workspace_id`` ordered by ``verdict_at DESC``."""
    capped = max(1, min(limit, 200))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if since is not None:
                await cur.execute(
                    f"""
                    SELECT {_VERDICT_COLS}
                    FROM dalton_verdicts
                    WHERE workspace_id = %s AND verdict_at >= %s
                    ORDER BY verdict_at DESC
                    LIMIT %s
                    """,
                    (workspace_id, since, capped),
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_VERDICT_COLS}
                    FROM dalton_verdicts
                    WHERE workspace_id = %s
                    ORDER BY verdict_at DESC
                    LIMIT %s
                    """,
                    (workspace_id, capped),
                )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def list_user_verdicts(
    user_id: str,
    *,
    workspace_id: str | None = None,
    since: datetime | str | None = None,
    until: datetime | str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List verdicts for ``user_id`` with optional workspace and time filters."""
    capped = max(1, min(limit, 500))
    clauses = ["user_id = %s"]
    params: list[Any] = [user_id]
    if workspace_id:
        clauses.append("workspace_id = %s")
        params.append(workspace_id)
    if since:
        clauses.append("verdict_at >= %s")
        params.append(since)
    if until:
        clauses.append("verdict_at <= %s")
        params.append(until)
    params.append(capped)
    where_sql = " AND ".join(clauses)

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_VERDICT_COLS}
                FROM dalton_verdicts
                WHERE {where_sql}
                ORDER BY verdict_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]
