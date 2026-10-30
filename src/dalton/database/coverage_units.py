"""DB access for registered coverage units (`dalton_coverage_units`, d023, sector layer P0).

SQL only, no policy: input normalization and the friendly pre-checks live in
`src/dalton/coverage/units.py`. The table's constraints (sector has no ticker,
one active binding per workspace, owner+kind+slug uniqueness) are the real
guards — a pre-check races only with itself, never with the index.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_UNIT_COLS = """
    unit_id::text AS unit_id,
    kind,
    owner_user_id,
    slug,
    display_name,
    workspace_id::text AS workspace_id,
    ticker,
    status,
    research_tz,
    created_at,
    updated_at
"""

_SLUG_CONFLICT = "uq_dalton_coverage_units_owner_kind_slug"
_WORKSPACE_CONFLICT = "uq_dalton_coverage_units_active_workspace"


def _friendly_unique_error(exc: pg_errors.UniqueViolation) -> ValueError:
    constraint = str(getattr(getattr(exc, "diag", None), "constraint_name", "") or "")
    if constraint == _SLUG_CONFLICT:
        return ValueError("coverage unit slug already registered for this owner and kind")
    if constraint == _WORKSPACE_CONFLICT:
        return ValueError("workspace already bound to another active coverage unit")
    return ValueError(f"coverage unit constraint violated: {constraint or 'unique'}")


async def get_coverage_unit(
    unit_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch one `dalton_coverage_units` row by id, any status."""
    uid = str(unit_id or "").strip()
    if not uid:
        return None
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_UNIT_COLS} FROM dalton_coverage_units WHERE unit_id = %s::uuid",
                (uid,),
            )
            return dict(row) if (row := await cur.fetchone()) else None


async def get_active_unit_by_workspace(
    workspace_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Fetch the active unit bound to `workspace_id`, if any (identity resolution's lookup).

    An unmigrated database (flag on without d023) degrades to "no unit" with one
    error log, so tracking keeps working on compatible rules instead of raising
    through every call site.
    """
    wid = str(workspace_id or "").strip()
    if not wid:
        return None
    try:
        async with get_db_connection(conn) as c:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT {_UNIT_COLS}
                    FROM dalton_coverage_units
                    WHERE workspace_id = %s::uuid AND status = 'active'
                    """,
                    (wid,),
                )
                return dict(row) if (row := await cur.fetchone()) else None
    except pg_errors.UndefinedTable:
        logger.error(
            "[DALTON_COVERAGE_UNITS] dalton_coverage_units is missing — run the d023 "
            "migration or turn coverage_units.enabled off; falling back to compatible rules"
        )
        return None


async def load_active_units_by_workspace(
    workspace_ids: list[str] | tuple[str, ...],
    *,
    conn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Batch form of `get_active_unit_by_workspace` for board-style enumeration.

    Returns `{workspace_id: unit_row}`. An unmigrated database (flag on without
    d023) logs once and yields an empty map instead of failing every board build.
    """
    ids = [str(w).strip() for w in workspace_ids if str(w or "").strip()]
    if not ids:
        return {}
    try:
        async with get_db_connection(conn) as c:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT {_UNIT_COLS}
                    FROM dalton_coverage_units
                    WHERE status = 'active' AND workspace_id = ANY(%s::uuid[])
                    """,
                    (ids,),
                )
                rows = await cur.fetchall()
    except pg_errors.UndefinedTable:
        logger.error(
            "[DALTON_COVERAGE_UNITS] dalton_coverage_units is missing — run the d023 "
            "migration or turn coverage_units.enabled off; falling back to compatible rules"
        )
        return {}
    return {str(r["workspace_id"]): dict(r) for r in rows if r.get("workspace_id")}


async def list_coverage_units(
    owner_user_id: str | None = None,
    *,
    include_archived: bool = False,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """List units, optionally filtered to one owner and/or active-only."""
    clauses: list[str] = []
    params: list[Any] = []
    if owner_user_id:
        clauses.append("owner_user_id = %s")
        params.append(str(owner_user_id).strip())
    if not include_archived:
        clauses.append("status = 'active'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_UNIT_COLS}
                FROM dalton_coverage_units
                {where}
                ORDER BY owner_user_id, kind, slug
                """,
                tuple(params),
            )
            return [dict(r) for r in await cur.fetchall()]


async def insert_coverage_unit(
    *,
    kind: str,
    owner_user_id: str,
    slug: str,
    display_name: str = "",
    ticker: str | None = None,
    workspace_id: str | None = None,
    research_tz: str | None = None,
    unit_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert one unit row in a single transaction; unique violations become ValueError.

    Without `unit_id` the column is left out of the INSERT entirely: an
    explicit NULL into the PK would NOT trigger `DEFAULT gen_random_uuid()`
    and every registration would die on NotNullViolation.
    """
    id_col = "unit_id, " if unit_id else ""
    id_param = "%s::uuid, " if unit_id else ""
    params: tuple[Any, ...] = (
        kind,
        owner_user_id,
        slug,
        display_name,
        workspace_id,
        ticker,
        research_tz,
    )
    if unit_id:
        params = (unit_id, *params)

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        async with c.cursor(row_factory=dict_row) as cur:
            try:
                await cur.execute(
                    f"""
                    INSERT INTO dalton_coverage_units (
                        {id_col}kind, owner_user_id, slug, display_name,
                        workspace_id, ticker, status, research_tz, created_at, updated_at
                    )
                    VALUES (
                        {id_param}%s, %s, %s, %s,
                        %s::uuid, %s, 'active', %s, NOW(), NOW()
                    )
                    RETURNING {_UNIT_COLS}
                    """,
                    params,
                )
            except pg_errors.UniqueViolation as exc:
                raise _friendly_unique_error(exc) from exc
            row = await cur.fetchone()
            assert row is not None
            return dict(row)

    if conn is not None:
        return await _run_in_tx(conn)
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def set_coverage_unit_status(
    unit_id: str,
    owner_user_id: str,
    status: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Set `status` on one of `owner_user_id`'s units (archive/reactivate).

    Archiving frees the workspace-binding slot: the partial unique index only
    covers active rows, so the workspace can take another unit afterwards.
    """

    async def _run_in_tx(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_coverage_units
                SET status = %s, updated_at = NOW()
                WHERE unit_id = %s::uuid AND owner_user_id = %s
                RETURNING {_UNIT_COLS}
                """,
                (status, unit_id, owner_user_id),
            )
            return dict(row) if (row := await cur.fetchone()) else None

    if conn is not None:
        return await _run_in_tx(conn)
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def bind_coverage_unit_workspace(
    unit_id: str,
    workspace_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Bind an active unit to a workspace inside one transaction.

    Verifies inside the same transaction that the workspace exists, is not
    deleted, belongs to the owner, and is not the owner's Home (a Home never
    binds a unit — design §③.2), then lets the partial unique index arbitrate
    the one-active-binding rule against concurrent writers.
    """

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT w.workspace_id::text AS workspace_id, w.user_id, w.status
                FROM workspaces w
                WHERE w.workspace_id = %s::uuid AND w.status <> 'deleted'
                """,
                (workspace_id,),
            )
            ws_row = await cur.fetchone()
            if ws_row is None:
                raise ValueError(f"workspace {workspace_id} does not exist or is deleted")
            if str(ws_row.get("user_id") or "") != str(owner_user_id):
                raise ValueError("workspace belongs to another user")
            from src.server.database.home_workspace import is_home_row

            if is_home_row(ws_row):
                raise ValueError("the Home workspace never binds a coverage unit")

            await cur.execute(
                """
                SELECT slug FROM dalton_coverage_units
                WHERE workspace_id = %s::uuid AND status = 'active' AND unit_id <> %s::uuid
                """,
                (workspace_id, unit_id),
            )
            if await cur.fetchone():
                raise ValueError("workspace already bound to another active coverage unit")

            try:
                await cur.execute(
                    f"""
                    UPDATE dalton_coverage_units
                    SET workspace_id = %s::uuid, updated_at = NOW()
                    WHERE unit_id = %s::uuid AND owner_user_id = %s AND status = 'active'
                    RETURNING {_UNIT_COLS}
                    """,
                    (workspace_id, unit_id, owner_user_id),
                )
            except pg_errors.UniqueViolation as exc:
                raise _friendly_unique_error(exc) from exc
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"active coverage unit {unit_id} not found for this owner")
            return dict(row)

    if conn is not None:
        return await _run_in_tx(conn)
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def unbind_coverage_unit_workspace(
    unit_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Clear a unit's workspace binding (any status)."""

    async def _run_in_tx(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_coverage_units
                SET workspace_id = NULL, updated_at = NOW()
                WHERE unit_id = %s::uuid AND owner_user_id = %s
                RETURNING {_UNIT_COLS}
                """,
                (unit_id, owner_user_id),
            )
            return dict(row) if (row := await cur.fetchone()) else None

    if conn is not None:
        return await _run_in_tx(conn)
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)
