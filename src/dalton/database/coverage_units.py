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
    Archiving a sector unit also deletes its member projection in the same
    transaction (codex r3) — an archived unit must not keep appearing as its
    member companies' "related sector". Lock order matches bind: unit row
    lock (the pre-read below) → workspace advisory lock.

    Returns ``None`` (unchanged contract) when the unit does not belong to the
    owner; the caller turns that into the not-found error.
    """

    async def _run_in_tx(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT kind, workspace_id::text AS workspace_id
                FROM dalton_coverage_units
                WHERE unit_id = %s::uuid AND owner_user_id = %s
                FOR UPDATE
                """,
                (unit_id, owner_user_id),
            )
            pre = await cur.fetchone()
            if pre is None:
                return None
            if (
                status == "archived"
                and str(pre.get("kind") or "") == "sector"
            ):
                await _clear_sector_projection(
                    cur, str(pre.get("workspace_id") or ""), unit_id
                )
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


async def _clear_sector_projection(
    cur: Any, workspace_id: str | None, unit_id: str
) -> None:
    """Delete one sector unit's member rows under the workspace's kv advisory lock.

    The unbind/archive twin of the bind-time sync (codex r3): the projection
    means "what the unit's currently bound workspace's sector document says",
    so it must not survive the binding it described. `workspace_id` is the
    binding being removed — empty means the unit was never bound, so there is
    no doc-write lock to take. Refuses silently when the coverage-unit flag is
    off (same gate as the knowledge pipeline's projection).
    """
    from src.dalton.settings import is_coverage_units_enabled

    if not is_coverage_units_enabled():
        return
    old_ws = str(workspace_id or "").strip()
    if old_ws:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"dalton:kv:{old_ws}:sector",),
        )
    await cur.execute(
        "DELETE FROM dalton_sector_members WHERE sector_unit_id = %s::uuid",
        (unit_id,),
    )


async def bind_coverage_unit_workspace(
    unit_id: str,
    workspace_id: str,
    owner_user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Bind an active unit to a workspace inside one transaction.

    Confirms first, under row lock, that the unit is the caller's own active
    unit (codex r2 #2): someone else's or a missing `unit_id` paired with an
    occupied workspace or a Home must report the unit as not found, not leak
    the workspace-side verdict first. Then verifies in the same transaction
    that the workspace exists, is not deleted, belongs to the owner, and is
    not the owner's Home (a Home never binds a unit — design §③.2), and lets
    the partial unique index arbitrate the one-active-binding rule against
    concurrent writers.

    A sector unit's member projection is resynced from the workspace's latest
    valid sector document in this same transaction (codex r3), so re-binding a
    workspace to a new sector unit never leaves members on the old one. Lock
    order is unit row lock → workspace advisory locks
    (`dalton:kv:{workspace_id}:sector`, the insert_version lock; on a re-bind
    both the previous and the target workspace, in sorted order); insert_version
    takes only the advisory lock and reads units without row locks, and
    dalton_sector_members carries no foreign keys, so the order cannot cycle.
    """

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id FROM dalton_coverage_units
                WHERE unit_id = %s::uuid AND owner_user_id = %s AND status = 'active'
                FOR UPDATE
                """,
                (unit_id, owner_user_id),
            )
            pre = await cur.fetchone()
            if pre is None:
                raise ValueError(f"active coverage unit {unit_id} not found for this owner")
            previous_ws = str(pre.get("workspace_id") or "")
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
            # 行业单元：绑定关系变了，成员投影跟着变（同一事务；codex r3）。
            # 锁顺序见 docstring——单元行锁已在手，这里补工作区 advisory 锁。
            if str(row.get("kind") or "") == "sector":
                from src.dalton.settings import is_coverage_units_enabled

                if is_coverage_units_enabled():
                    from src.dalton.database.sector_members import (
                        sync_projection_to_latest_doc,
                    )

                    # 锁 key 用库里返回的规范化 id（大小写不同的同一 UUID 必须是同一把锁），
                    # 改绑时连原工作区一起锁、按固定顺序取：原工作区上正在进行的写文档
                    # 事务若已读到本单元，会先于本事务提交（随后被这里覆盖），否则等本事务
                    # 提交后再读，已看不到本单元（codex r3 #1/#2）。
                    target_ws = str(ws_row["workspace_id"])
                    for lock_ws in sorted({target_ws, previous_ws} - {""}):
                        await cur.execute(
                            "SELECT pg_advisory_xact_lock(hashtext(%s))",
                            (f"dalton:kv:{lock_ws}:sector",),
                        )
                    await sync_projection_to_latest_doc(
                        cur, target_ws, unit_id, clear_when_no_doc=True
                    )
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
    """Clear a unit's workspace binding (any status), in one transaction.

    Reads the unit under row lock first (codex r3) so a sector unit's member
    projection is deleted for its ORIGINAL workspace inside the same
    transaction as the unbind. Lock order matches bind: unit row lock →
    workspace advisory lock. Returns ``None`` (unchanged contract) when the
    unit does not belong to the owner.
    """

    async def _run_in_tx(c: Any) -> dict[str, Any] | None:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT kind, workspace_id::text AS workspace_id
                FROM dalton_coverage_units
                WHERE unit_id = %s::uuid AND owner_user_id = %s
                FOR UPDATE
                """,
                (unit_id, owner_user_id),
            )
            pre = await cur.fetchone()
            if pre is None:
                return None
            if str(pre.get("kind") or "") == "sector":
                await _clear_sector_projection(
                    cur, str(pre.get("workspace_id") or ""), unit_id
                )
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
