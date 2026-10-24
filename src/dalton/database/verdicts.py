"""Database queries for append-only event verdicts (PK.3)."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from src.dalton.autonomy.tracking import (
    _extract_ticker_from_text,
    _resolve_workspace_ticker,
)
from src.server.database.pool import get_db_connection
from src.server.utils.pg_sanitize import SafeJson, strip_pg_nul_str

_VERDICT_COLS = """
    verdict_id, user_id, workspace_id, ticker, verdict_at, word,
    trigger_kind, trigger_ref, progress, next_due, payload,
    conversation_thread_id, run_id, automation_execution_id,
    origin_type, created_at
"""


async def _resolve_verdict_ticker(
    workspace_id: str | None,
    verdict: dict[str, Any] | None = None,
    ticker: str | None = None,
) -> str | None:
    """Resolve ticker from explicit arg, verdict.ticker, or workspace metadata."""
    for candidate in (
        ticker,
        verdict.get("ticker") if isinstance(verdict, dict) else None,
    ):
        if isinstance(candidate, str) and candidate.strip():
            extracted = _extract_ticker_from_text(candidate.strip())
            return extracted or candidate.strip().upper()[:24]
    try:
        return await _resolve_workspace_ticker(workspace_id)
    except Exception:
        return None


def _serialize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        k: (str(v) if isinstance(v, UUID) else v)
        for k, v in row.items()
    }


def _now_utc() -> datetime:
    """Return current UTC timestamp with full clock precision."""
    return datetime.now(timezone.utc)


def _day_str(day: date | datetime | str | None = None) -> str:
    if isinstance(day, str) and len(day) >= 8:
        clean = day[:10].replace("-", "")
        if len(clean) == 8 and clean.isdigit():
            return clean
    if isinstance(day, datetime):
        return day.astimezone(timezone.utc).strftime("%Y%m%d")
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    return _now_utc().strftime("%Y%m%d")


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
    ticker: str | None = None,
) -> dict[str, Any]:
    """Assign a ``VD-YYYYMMDD-NN`` ID under an advisory lock and insert into ``dalton_verdicts``."""
    verdict_at = _now_utc().astimezone(timezone.utc)
    now_iso = verdict_at.isoformat().replace("+00:00", "Z")
    day_stamp = _day_str(verdict_at)
    lock_key = f"dalton:verdict:{day_stamp}"
    resolved_ticker = await _resolve_verdict_ticker(
        workspace_id, verdict=verdict, ticker=ticker
    )

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
            payload = {**verdict, "id": verdict_id, "at": now_iso}

            await cur.execute(
                f"""
                INSERT INTO dalton_verdicts (
                    verdict_id, user_id, workspace_id, verdict_at, word,
                    trigger_kind, trigger_ref, progress, next_due, payload,
                    conversation_thread_id, run_id, automation_execution_id,
                    origin_type, ticker
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
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
                    strip_pg_nul_str(resolved_ticker) if resolved_ticker else None,
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
                    ORDER BY verdict_at DESC, length(verdict_id) DESC, verdict_id DESC
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
                    ORDER BY verdict_at DESC, length(verdict_id) DESC, verdict_id DESC
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
                ORDER BY verdict_at DESC, length(verdict_id) DESC, verdict_id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return [_serialize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_verdict_for_execution(
    *,
    automation_execution_id: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the verdict row matching this run's ``automation_execution_id`` or ``run_id``.

    Never falls back to ``conversation_thread_id`` because recurring automations
    on a fixed thread (e.g. ``dalton-director-weekly``) would return a stale
    verdict from an earlier run.
    """
    _ = thread_id
    exec_id = (automation_execution_id or "").strip()
    rid = (run_id or "").strip()
    if not (exec_id or rid):
        return None

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            for col, val in (
                ("automation_execution_id", exec_id),
                ("run_id", rid),
            ):
                if not val:
                    continue
                await cur.execute(
                    f"""
                    SELECT {_VERDICT_COLS}
                    FROM dalton_verdicts
                    WHERE {col} = %s
                    ORDER BY verdict_at DESC, created_at DESC, length(verdict_id) DESC, verdict_id DESC
                    LIMIT 1
                    """,
                    (val,),
                )
                row = await cur.fetchone()
                if row is not None:
                    return _serialize_row(row)
    return None
