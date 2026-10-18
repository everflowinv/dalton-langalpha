"""Database access layer for Dalton autonomy watch state, signals, and preflight logs (d006)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.pool import get_db_connection


async def get_watch_state(workspace_id: str) -> dict[str, Any]:
    """Return the watch state JSON for a workspace, or an empty dict."""
    ws_uuid = uuid.UUID(str(workspace_id))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id, ticker, state, updated_at
                FROM dalton_watch_state
                WHERE workspace_id = %s
                """,
                (ws_uuid,),
            )
            row = await cur.fetchone()
            if not row:
                return {}
            return dict(row.get("state") or {})


async def upsert_watch_state(
    workspace_id: str,
    ticker: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create or update watch state for a workspace."""
    ws_uuid = uuid.UUID(str(workspace_id))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_watch_state (workspace_id, ticker, state, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (workspace_id) DO UPDATE
                SET ticker = EXCLUDED.ticker,
                    state = EXCLUDED.state,
                    updated_at = NOW()
                RETURNING workspace_id, ticker, state, updated_at
                """,
                (ws_uuid, ticker.upper(), Jsonb(state)),
            )
            row = await cur.fetchone()
            return dict(row) if row else {}


async def upsert_signal(
    *,
    workspace_id: Optional[str],
    ticker: Optional[str],
    probe: str,
    signal_key: str,
    priority: str,
    summary: str,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], bool]:
    """Idempotently record a signal; returns (row, should_wake).

    If (workspace_id, probe, signal_key) already exists and woken_execution_id is set,
    should_wake is False so we never double-wake on the same signal.
    """
    ws_uuid = uuid.UUID(str(workspace_id)) if workspace_id else None
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if ws_uuid is not None:
                await cur.execute(
                    """
                    SELECT signal_id, workspace_id, ticker, probe, signal_key, priority,
                           summary, payload, detected_at, woken_execution_id, woken_at, resolved_at
                    FROM dalton_signals
                    WHERE workspace_id = %s AND probe = %s AND signal_key = %s
                    """,
                    (ws_uuid, probe, signal_key),
                )
            else:
                await cur.execute(
                    """
                    SELECT signal_id, workspace_id, ticker, probe, signal_key, priority,
                           summary, payload, detected_at, woken_execution_id, woken_at, resolved_at
                    FROM dalton_signals
                    WHERE workspace_id IS NULL AND probe = %s AND signal_key = %s
                    """,
                    (probe, signal_key),
                )
            existing = await cur.fetchone()
            if existing:
                row_dict = dict(existing)
                should_wake = (
                    row_dict.get("woken_execution_id") is None
                    and row_dict.get("resolved_at") is None
                )
                return row_dict, should_wake

            sig_id = uuid.uuid4()
            await cur.execute(
                """
                INSERT INTO dalton_signals (
                    signal_id, workspace_id, ticker, probe, signal_key,
                    priority, summary, payload, detected_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (workspace_id, probe, signal_key) DO UPDATE
                SET summary = EXCLUDED.summary
                RETURNING signal_id, workspace_id, ticker, probe, signal_key, priority,
                          summary, payload, detected_at, woken_execution_id, woken_at, resolved_at
                """,
                (
                    sig_id,
                    ws_uuid,
                    ticker.upper() if ticker else None,
                    probe,
                    signal_key,
                    priority,
                    summary,
                    Jsonb(payload or {}),
                ),
            )
            created = await cur.fetchone()
            row_dict = dict(created) if created else {}
            should_wake = (
                row_dict.get("woken_execution_id") is None
                and row_dict.get("resolved_at") is None
            )
            return row_dict, should_wake


async def list_pending_signals(
    workspace_id: Optional[str] = None,
    ticker: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List unresolved signals (woken or unwoken) for a workspace/ticker."""
    clauses = ["resolved_at IS NULL"]
    params: list[Any] = []
    if workspace_id:
        clauses.append("workspace_id = %s")
        params.append(uuid.UUID(str(workspace_id)))
    if ticker:
        clauses.append("ticker = %s")
        params.append(ticker.upper())
    where_sql = " AND ".join(clauses)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT signal_id, workspace_id, ticker, probe, signal_key, priority,
                       summary, payload, detected_at, woken_execution_id, woken_at, resolved_at
                FROM dalton_signals
                WHERE {where_sql}
                ORDER BY detected_at DESC
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def mark_signals_woken(
    signal_ids: list[str],
    execution_id: str,
) -> int:
    """Stamp woken_execution_id and woken_at on the given signals."""
    if not signal_ids:
        return 0
    exec_uuid = uuid.UUID(str(execution_id))
    sig_uuids = [uuid.UUID(str(s)) for s in signal_ids]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_signals
                SET woken_execution_id = %s,
                    woken_at = NOW()
                WHERE signal_id = ANY(%s)
                  AND woken_execution_id IS NULL
                """,
                (exec_uuid, sig_uuids),
            )
            return cur.rowcount or 0


async def resolve_signals(signal_ids: list[str]) -> int:
    """Mark signals as resolved."""
    if not signal_ids:
        return 0
    sig_uuids = [uuid.UUID(str(s)) for s in signal_ids]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_signals
                SET resolved_at = NOW()
                WHERE signal_id = ANY(%s)
                  AND resolved_at IS NULL
                """,
                (sig_uuids,),
            )
            return cur.rowcount or 0


async def insert_preflight_log(
    *,
    automation_id: Optional[str],
    automation_execution_id: Optional[str],
    workspace_id: Optional[str],
    decision: str,
    reason: Optional[str],
    signals: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Record a preflight check outcome in dalton_preflight_log."""
    auto_uuid = uuid.UUID(str(automation_id)) if automation_id else None
    exec_uuid = uuid.UUID(str(automation_execution_id)) if automation_execution_id else None
    ws_uuid = uuid.UUID(str(workspace_id)) if workspace_id else None
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_preflight_log (
                    automation_id, automation_execution_id, workspace_id,
                    decision, reason, signals, checked_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                RETURNING log_id, automation_id, automation_execution_id, workspace_id,
                          decision, reason, signals, checked_at
                """,
                (
                    auto_uuid,
                    exec_uuid,
                    ws_uuid,
                    decision,
                    reason,
                    Jsonb(signals or []),
                ),
            )
            row = await cur.fetchone()
            return dict(row) if row else {}


async def list_preflight_logs(
    *,
    workspace_id: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query preflight logs since a given timestamp."""
    clauses: list[str] = []
    params: list[Any] = []
    if workspace_id:
        clauses.append("workspace_id = %s")
        params.append(uuid.UUID(str(workspace_id)))
    if since:
        clauses.append("checked_at >= %s")
        params.append(since)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT log_id, automation_id, automation_execution_id, workspace_id,
                       decision, reason, signals, checked_at
                FROM dalton_preflight_log
                {where_sql}
                ORDER BY checked_at DESC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def list_signals_since(
    since: datetime,
    workspace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List signals detected since ``since``."""
    clauses = ["detected_at >= %s"]
    params: list[Any] = [since]
    if workspace_id:
        clauses.append("workspace_id = %s")
        params.append(uuid.UUID(str(workspace_id)))
    where_sql = " AND ".join(clauses)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT signal_id, workspace_id, ticker, probe, signal_key, priority,
                       summary, payload, detected_at, woken_execution_id, woken_at, resolved_at
                FROM dalton_signals
                WHERE {where_sql}
                ORDER BY detected_at DESC
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
