"""Database access layer for Dalton autonomy watch state, signals, and preflight logs (d006)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.pool import get_db_connection


HOME_WORKSPACE_SENTINEL = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _resolve_ws_uuid(workspace_id: Optional[str]) -> uuid.UUID:
    """Resolve workspace_id to UUID, using HOME_WORKSPACE_SENTINEL when empty/None/home (A10)."""
    if not workspace_id or str(workspace_id).strip().lower() == "home":
        return HOME_WORKSPACE_SENTINEL
    try:
        return uuid.UUID(str(workspace_id))
    except (ValueError, TypeError):
        return uuid.uuid5(uuid.NAMESPACE_DNS, str(workspace_id))


async def get_watch_state(workspace_id: Optional[str]) -> dict[str, Any]:
    """Return the watch state JSON for a workspace (or Home sentinel), or an empty dict."""
    ws_uuid = _resolve_ws_uuid(workspace_id)
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
    workspace_id: Optional[str],
    ticker: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Create or update watch state for a workspace (or Home sentinel)."""
    ws_uuid = _resolve_ws_uuid(workspace_id)
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
                (ws_uuid, (ticker or "HOME").upper(), Jsonb(state)),
            )
            row = await cur.fetchone()
            return dict(row) if row else {}


def _with_signal_status(row: dict[str, Any]) -> dict[str, Any]:
    """Attach canonical lifecycle ``status`` ('pending' | 'woken' | 'resolved' | 'expired') to a signal row."""
    out = dict(row)
    payload = out.get("payload") if isinstance(out.get("payload"), dict) else {}
    if out.get("resolved_at") is not None or payload.get("status") == "resolved":
        out["status"] = "resolved"
    elif out.get("expired_at") is not None or payload.get("status") == "expired":
        out["status"] = "expired"
    elif out.get("woken_execution_id") is not None or out.get("woken_at") is not None:
        out["status"] = "woken"
    else:
        out["status"] = "pending"
    return out


def _try_uuid(val: Any) -> Optional[uuid.UUID]:
    if not val:
        return None
    try:
        return uuid.UUID(str(val))
    except (ValueError, TypeError):
        return None


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
    """Idempotently record a signal via atomic ``INSERT ... ON CONFLICT``; returns (row, should_wake).

    Empty/None ``workspace_id`` uses ``HOME_WORKSPACE_SENTINEL`` so Home-level signals are also
    deduplicated by Postgres ``UNIQUE (workspace_id, probe, signal_key)`` (P0H §3.2 A10).
    """
    ws_uuid = _resolve_ws_uuid(workspace_id)
    sig_id = uuid.uuid4()
    init_payload = dict(payload or {})
    init_payload.setdefault("status", "pending")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
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
                          summary, payload, detected_at, woken_execution_id, woken_at,
                          resolved_at, expired_at
                """,
                (
                    sig_id,
                    ws_uuid,
                    ticker.upper() if ticker else None,
                    probe,
                    signal_key,
                    priority,
                    summary,
                    Jsonb(init_payload),
                ),
            )
            created = await cur.fetchone()
            row_dict = _with_signal_status(dict(created)) if created else {}
            should_wake = (
                row_dict.get("woken_execution_id") is None
                and row_dict.get("resolved_at") is None
                and row_dict.get("expired_at") is None
            )
            return row_dict, should_wake


async def list_pending_signals(
    workspace_id: Optional[str] = None,
    ticker: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List unresolved, non-expired signals (woken or unwoken) for a workspace/ticker."""
    clauses = ["resolved_at IS NULL", "expired_at IS NULL"]
    params: list[Any] = []
    if workspace_id:
        ws_uuid = _try_uuid(workspace_id)
        if ws_uuid:
            clauses.append("workspace_id = %s")
            params.append(ws_uuid)
    if ticker:
        clauses.append("ticker = %s")
        params.append(ticker.upper())
    where_sql = " AND ".join(clauses)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT signal_id, workspace_id, ticker, probe, signal_key, priority,
                       summary, payload, detected_at, woken_execution_id, woken_at,
                       resolved_at, expired_at
                FROM dalton_signals
                WHERE {where_sql}
                ORDER BY detected_at DESC
                """,
                tuple(params),
            )
            rows = await cur.fetchall()
            return [_with_signal_status(dict(r)) for r in rows]


async def claim_signals_woken(
    signal_ids: list[str],
    execution_id: str,
) -> list[dict[str, Any]]:
    """Atomically claim unwoken signals for ``execution_id`` using ``UPDATE ... WHERE woken_execution_id IS NULL RETURNING *`` (R5-12)."""
    if not signal_ids:
        return []
    exec_uuid = _try_uuid(execution_id) or uuid.uuid5(uuid.NAMESPACE_DNS, str(execution_id))
    sig_uuids = [u for s in signal_ids if (u := _try_uuid(s)) is not None]
    if not sig_uuids:
        return []
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE dalton_signals
                SET woken_execution_id = %s,
                    woken_at = NOW(),
                    payload = COALESCE(payload, '{}'::jsonb) || '{"status": "woken"}'::jsonb
                WHERE signal_id = ANY(%s)
                  AND woken_execution_id IS NULL
                  AND resolved_at IS NULL
                  AND expired_at IS NULL
                RETURNING signal_id, workspace_id, ticker, probe, signal_key, priority,
                          summary, payload, detected_at, woken_execution_id, woken_at,
                          resolved_at, expired_at
                """,
                (exec_uuid, sig_uuids),
            )
            rows = await cur.fetchall()
            return [_with_signal_status(dict(r)) for r in (rows or [])]


async def mark_signals_woken(
    signal_ids: list[str],
    execution_id: str,
) -> int:
    """Stamp woken_execution_id and woken_at on the given signals (atomic claim)."""
    claimed = await claim_signals_woken(signal_ids, execution_id)
    return len(claimed)


async def resolve_signals(
    signal_ids: Optional[list[str]] = None,
    *,
    signal_refs: Optional[list[str]] = None,
    workspace_id: Optional[str] = None,
    ticker: Optional[str] = None,
    resolved_by: Optional[str] = None,
    execution_id: Optional[str] = None,
) -> int:
    """Mark signals as resolved by UUID, signal_key / ref suffix, or woken_execution_id (G4.2)."""
    refs = list(signal_ids or []) + list(signal_refs or [])
    refs = [str(r).strip() for r in refs if r and str(r).strip()]
    exec_uuid = _try_uuid(execution_id)
    if not refs and exec_uuid is None:
        return 0

    sig_uuids: list[uuid.UUID] = []
    key_refs: list[str] = []
    for ref in refs:
        u = _try_uuid(ref)
        if u is not None:
            sig_uuids.append(u)
        else:
            key_refs.append(ref)

    ws_uuid = _try_uuid(workspace_id)
    patch_obj: dict[str, Any] = {"status": "resolved"}
    if resolved_by:
        patch_obj["resolved_by"] = str(resolved_by)

    total_updated = 0
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if sig_uuids:
                await cur.execute(
                    """
                    UPDATE dalton_signals
                    SET resolved_at = NOW(),
                        payload = COALESCE(payload, '{}'::jsonb) || %s::jsonb
                    WHERE signal_id = ANY(%s)
                      AND resolved_at IS NULL
                    """,
                    (Jsonb(patch_obj), sig_uuids),
                )
                total_updated += cur.rowcount or 0

            for kref in key_refs:
                scope_clauses = ["resolved_at IS NULL"]
                params: list[Any] = [Jsonb(patch_obj)]
                if ws_uuid is not None:
                    scope_clauses.append("workspace_id = %s")
                    params.append(ws_uuid)
                if ticker:
                    scope_clauses.append("ticker = %s")
                    params.append(ticker.upper())
                scope_clauses.append(
                    "(signal_key = %s OR signal_key LIKE %s OR signal_key LIKE %s "
                    "OR payload->>'id' = %s OR payload->>'accession_number' = %s OR payload->>'doc_id' = %s)"
                )
                params.extend([kref, f"%:{kref}", f"%:{kref}:%", kref, kref, kref])
                where_sql = " AND ".join(scope_clauses)
                await cur.execute(
                    f"""
                    UPDATE dalton_signals
                    SET resolved_at = NOW(),
                        payload = COALESCE(payload, '{{}}'::jsonb) || %s::jsonb
                    WHERE {where_sql}
                    """,
                    tuple(params),
                )
                total_updated += cur.rowcount or 0

            if exec_uuid is not None:
                await cur.execute(
                    """
                    UPDATE dalton_signals
                    SET resolved_at = NOW(),
                        payload = COALESCE(payload, '{}'::jsonb) || %s::jsonb
                    WHERE woken_execution_id = %s
                      AND resolved_at IS NULL
                    """,
                    (Jsonb(patch_obj), exec_uuid),
                )
                total_updated += cur.rowcount or 0

    return total_updated


async def expire_old_signals(
    cur: Any = None,
    *,
    max_age_days: int = 7,
    workspace_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Mark unresolved signals older than ``max_age_days`` (default 7d) as expired (G4.2)."""
    import inspect

    days = max(1, int(max_age_days))
    ws_uuid = _try_uuid(workspace_id)

    async def _run_with_cursor(c: Any) -> list[dict[str, Any]]:
        if ws_uuid is not None:
            sql = """
                UPDATE dalton_signals
                SET expired_at = NOW(),
                    payload = COALESCE(payload, '{}'::jsonb) || '{"status": "expired"}'::jsonb
                WHERE resolved_at IS NULL
                  AND expired_at IS NULL
                  AND workspace_id = %s
                  AND detected_at < NOW() - (%s * INTERVAL '1 day')
                RETURNING signal_id, workspace_id, ticker, probe, signal_key, priority,
                          summary, payload, detected_at, woken_execution_id, woken_at,
                          resolved_at, expired_at
            """
            params: tuple[Any, ...] = (ws_uuid, days)
        else:
            sql = """
                UPDATE dalton_signals
                SET expired_at = NOW(),
                    payload = COALESCE(payload, '{}'::jsonb) || '{"status": "expired"}'::jsonb
                WHERE resolved_at IS NULL
                  AND expired_at IS NULL
                  AND detected_at < NOW() - (%s * INTERVAL '1 day')
                RETURNING signal_id, workspace_id, ticker, probe, signal_key, priority,
                          summary, payload, detected_at, woken_execution_id, woken_at,
                          resolved_at, expired_at
            """
            params = (days,)

        exec_res = c.execute(sql, params)
        if inspect.isawaitable(exec_res):
            await exec_res
        fetch_res = c.fetchall()
        rows = await fetch_res if inspect.isawaitable(fetch_res) else fetch_res
        out: list[dict[str, Any]] = []
        for r in rows or []:
            d = dict(r) if isinstance(r, dict) else {"signal_id": str(r[0])}
            d["status"] = "expired"
            out.append(d)
        return out

    if cur is not None:
        return await _run_with_cursor(cur)

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as db_cur:
            return await _run_with_cursor(db_cur)


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
