"""DB access for cross-unit research requests (`dalton_cross_unit_requests`, d026, sector layer P2).

SQL only. Ownership is enforced by ``owner_user_id`` on every read and update:
these are the director's own coordination records, and a colleague's request
ledger is not visible or writable from another Home.
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

VALID_STATUSES = ("open", "dispatched", "fulfilled", "gap", "cancelled")
VALID_KINDS = ("research_request", "linkage_retry")

_REQ_COLS = """
    request_id::text AS request_id,
    owner_user_id,
    kind,
    sector_unit_id::text AS sector_unit_id,
    sector_workspace_id::text AS sector_workspace_id,
    target_workspace_id::text AS target_workspace_id,
    question,
    evidence_needed,
    completion_condition,
    status,
    dispatch_thread_id::text AS dispatch_thread_id,
    result,
    payload,
    created_at,
    updated_at
"""


def _uuid_or_none(raw: Any, field: str) -> str | None:
    val = str(raw or "").strip()
    if not val:
        return None
    try:
        return str(uuid.UUID(val))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID, got {raw!r}") from exc


async def insert_request(
    *,
    owner_user_id: str,
    question: str,
    sector_unit_id: str | None = None,
    sector_workspace_id: str | None = None,
    target_workspace_id: str | None = None,
    evidence_needed: list[dict[str, Any]] | None = None,
    completion_condition: str = "",
    kind: str = "research_request",
    payload: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Create one request (``kind='research_request'``) or linkage gap row.

    ``kind='linkage_retry'`` rows are created in status ``gap`` directly: they
    record a propagation failure whose retry payload lives in ``payload``.
    """
    uid = str(owner_user_id or "").strip()
    if not uid:
        raise ValueError("owner_user_id must not be empty")
    q = str(question or "").strip()
    if not q:
        raise ValueError("question must not be empty")
    norm_kind = str(kind or "research_request").strip() or "research_request"
    if norm_kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    initial_status = "gap" if norm_kind == "linkage_retry" else "open"

    async def _run(c: Any) -> dict[str, Any]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_cross_unit_requests (
                    owner_user_id, kind, sector_unit_id, sector_workspace_id,
                    target_workspace_id, question, evidence_needed,
                    completion_condition, status, payload, created_at, updated_at
                ) VALUES (
                    %s, %s, %s::uuid, %s::uuid,
                    %s::uuid, %s, %s,
                    %s, %s, %s, NOW(), NOW()
                )
                RETURNING {_REQ_COLS}
                """,
                (
                    uid,
                    norm_kind,
                    _uuid_or_none(sector_unit_id, "sector_unit_id"),
                    _uuid_or_none(sector_workspace_id, "sector_workspace_id"),
                    _uuid_or_none(target_workspace_id, "target_workspace_id"),
                    q,
                    _jsonb(evidence_needed or []),
                    str(completion_condition or "").strip(),
                    initial_status,
                    _jsonb(payload or {}),
                ),
            )
            return dict(await cur.fetchone())

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)


def _jsonb(value: Any) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


async def get_request(
    request_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_REQ_COLS}
                FROM dalton_cross_unit_requests
                WHERE request_id = %s::uuid AND owner_user_id = %s
                """,
                (_uuid_or_none(request_id, "request_id"), str(owner_user_id).strip()),
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_requests(
    owner_user_id: str,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    conn: Any = None,
) -> list[dict[str, Any]]:
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    if kind is not None and kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")
    clauses = ["owner_user_id = %s"]
    params: list[Any] = [str(owner_user_id).strip()]
    if status:
        clauses.append("status = %s")
        params.append(status)
    if kind:
        clauses.append("kind = %s")
        params.append(kind)
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_REQ_COLS}
                FROM dalton_cross_unit_requests
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (*params, max(1, min(int(limit), 200))),
            )
            return [dict(r) for r in await cur.fetchall()]


_UNSET = object()


async def list_linkage_retry_batch(
    owner_user_id: str,
    *,
    target_workspace_id: str | None = None,
    limit: int = 20,
    cursor: tuple[Any, str] | None = None,
    conn: Any = None,
) -> tuple[list[dict[str, Any]], int]:
    """Oldest-first batch of the owner's ``gap`` linkage rows, plus how many remain.

    The target filter is pushed into SQL and paging is a keyset cursor on
    ``(created_at, request_id)`` ascending — a plain newest-first LIMIT lets a
    pile of newer unrecoverable gaps permanently starve an older recoverable
    one (codex r2 #3). ``cursor`` is the previous batch's last row
    ``(created_at, request_id)``; the returned count covers the same filter
    from the cursor onward.
    """
    clauses = ["owner_user_id = %s", "status = 'gap'", "kind = 'linkage_retry'"]
    params: list[Any] = [str(owner_user_id).strip()]
    if target_workspace_id and str(target_workspace_id).strip():
        clauses.append("target_workspace_id = %s::uuid")
        params.append(_uuid_or_none(target_workspace_id, "target_workspace_id"))
    if cursor is not None:
        clauses.append("(created_at, request_id) > (%s, %s::uuid)")
        params.extend([cursor[0], _uuid_or_none(cursor[1], "cursor request_id")])
    where = " AND ".join(clauses)
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_REQ_COLS}
                FROM dalton_cross_unit_requests
                WHERE {where}
                ORDER BY created_at ASC, request_id ASC
                LIMIT %s
                """,
                (*params, max(1, min(int(limit), 100))),
            )
            rows = [dict(r) for r in await cur.fetchall()]
            await cur.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM dalton_cross_unit_requests
                WHERE {where}
                """,
                tuple(params),
            )
            cnt_row = await cur.fetchone()
    return rows, int((cnt_row or {}).get("cnt") or 0)


async def update_request(
    request_id: str,
    owner_user_id: str,
    *,
    status: str | None = None,
    target_workspace_id: Any = _UNSET,
    dispatch_thread_id: Any = _UNSET,
    result: list[dict[str, Any]] | None = None,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Owner-scoped field update; ``None`` clears a field, omitted leaves it.

    Status transitions are not constrained here beyond the table's CHECK: the
    director tool layer decides which transitions make sense (a failed or
    stopped dispatch becomes ``gap`` and STAYS until explicitly re-dispatched
    or cancelled).
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(f"status must be one of {VALID_STATUSES}, got {status!r}")
    sets: list[str] = ["updated_at = NOW()"]
    params: list[Any] = []
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if target_workspace_id is not _UNSET:
        sets.append("target_workspace_id = %s::uuid")
        params.append(_uuid_or_none(target_workspace_id, "target_workspace_id"))
    if dispatch_thread_id is not _UNSET:
        sets.append("dispatch_thread_id = %s::uuid")
        params.append(_uuid_or_none(dispatch_thread_id, "dispatch_thread_id"))
    if result is not None:
        sets.append("result = %s")
        params.append(_jsonb(result))

    async def _run(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_cross_unit_requests
                SET {', '.join(sets)}
                WHERE request_id = %s::uuid AND owner_user_id = %s
                RETURNING {_REQ_COLS}
                """,
                (*params, _uuid_or_none(request_id, "request_id"), str(owner_user_id).strip()),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as c, c.transaction():
        return await _run(c)
