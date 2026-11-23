"""DB access for registered coverage units (`dalton_coverage_units`, d023 / d041).

SQL and transactional lock arbitration: input normalization lives in
`src/dalton/coverage/units.py`, while row/workspace locking, duplicate
conflict inspection under SAVEPOINT, and workspace-write re-checks live here.

Global deadlock-free lock order across coverage-unit writes, workspace soft delete,
and automation scheduling/execution:
1. `dalton_coverage_units` (`WHERE unit_id = %s::uuid FOR UPDATE` or `WHERE ... FOR SHARE`).
   Global invariant: **持有 `dalton_coverage_units FOR UPDATE` 的事务绝不对 `automations` 或
   `automation_executions` 取行锁（包括 `FOR SHARE`）或取 `user_automations_lock`**。
   原因是通用自动化写路径（`lifecycle._reconcile_locked_update` 与 `automation_recycle`）
   在已持有 `automations FOR UPDATE` 的同一事务里通过 `lock_and_assert_managed_unit_write_allowed`
   对 `dalton_coverage_units` 取 `FOR SHARE`；若解绑/归档持有 `dalton_coverage_units FOR UPDATE`
   后再取 `automations FOR SHARE`，将直接形成 AB-BA 等待环而死锁。
   因此 `unbind_coverage_unit_workspace` 与 `set_coverage_unit_status(..., "archived")` 采用：
   (a) 在获取 `dalton_coverage_units FOR UPDATE` 之前先做一次只读预检（验证调用者写权限/可见性后
   只读检查当前绑定工作区下 `status IN ('active', 'executing')` 的受管任务与 `pending`/`waiting` 执行）；
   (b) 获取 `dalton_coverage_units FOR UPDATE` -> `workspaces FOR UPDATE` 后，在更新单元行之前再做一次
   **完全只读、不加任何任务/执行行锁**的 `SELECT` 复查（所有并发激活/立即触发写路径在提交前均会取
   `dalton_coverage_units FOR SHARE`，因此在 `FOR UPDATE` 授予之后并发写路径要么已提交并在此次只读
   复查中可见，要么阻塞等待本次提交/回滚）。
   首期不支持改绑，且解绑与归档本身绝不修改任何 `automations`、`automation_executions` 或通知行。
2. `workspaces` (`WHERE workspace_id = ANY(%s::uuid[]) ORDER BY w.created_at, w.workspace_id FOR UPDATE`)
3. `pg_advisory_xact_lock(hashtext('dalton:kv:' || ws_id || ':sector'))` in sorted order
4. `pg_advisory_xact_lock(hashtext('dalton_coverage'), hashtext(user_id))` in sorted `user_id` order
   (acquired together before any personal coverage clear/sync inside the same transaction).
5. `automation_executions` (`FOR UPDATE`) -> `user_automations_lock(user_id)` -> `automations` (`FOR UPDATE`).
   Callers entering from automation mutations/execution (`lifecycle`, `AutomationFile.prelock_units`,
   `schedule_dalton_automation_helper`, `tracking.enable_tracking`/`disable_tracking`,
   `automation_executions.begin_execution_if_not_deleted` with an independent top-level transaction
   per attempt on `ws_changed` retry, and `create_execution_for_live_parent`) take
   `dalton_coverage_units FOR SHARE` BEFORE acquiring any `automation_executions` /
   `user_automations_lock` / `automations` locks when entering from outside `auto_db`, or under
   `automations FOR UPDATE` (serialized by the no-lock-after-FOR-UPDATE invariant above).
   One exception: `schedule_dalton_automation_helper` pausing / resuming by `automation_id`
   learns the task's workspace only after reading the task, so it takes that workspace's
   `dalton_coverage_units FOR SHARE` after `user_automations_lock`. It cannot form a wait
   cycle: the only `FOR UPDATE` holders (bind / unbind / archive) never take
   `user_automations_lock` or touch `automations` rows (invariant in item 1).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from src.dalton.coverage.identity import (
    is_historical_duplicate_workspace,
    normalize_ticker_key,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_UNIT_COLS = """
    unit_id::text AS unit_id,
    kind,
    owner_user_id,
    COALESCE(created_by_user_id, owner_user_id) AS created_by_user_id,
    lead_analyst_user_id,
    slug,
    display_name,
    workspace_id::text AS workspace_id,
    ticker,
    ticker_key,
    status,
    research_tz,
    created_at,
    updated_at
"""

_SLUG_CONFLICT = "uq_dalton_coverage_units_active_kind_slug"
_LEGACY_SLUG_CONFLICT = "uq_dalton_coverage_units_owner_kind_slug"
_TICKER_CONFLICT = "uq_dalton_coverage_units_active_ticker_key"
_WORKSPACE_CONFLICT = "uq_dalton_coverage_units_active_workspace"


class CoverageUnitConflictError(ValueError):
    """409 conflict carrying the winning unit id, lead analyst, and readable workspace_id."""

    def __init__(
        self,
        message: str,
        *,
        conflict: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.conflict = conflict or {}


def _friendly_unique_error(
    exc: pg_errors.UniqueViolation,
    *,
    conflict: dict[str, Any] | None = None,
) -> ValueError:
    constraint = str(getattr(getattr(exc, "diag", None), "constraint_name", "") or "")
    if constraint in (_SLUG_CONFLICT, _LEGACY_SLUG_CONFLICT):
        msg = "coverage unit slug already registered for this owner and kind"
        return CoverageUnitConflictError(msg, conflict=conflict) if conflict else ValueError(msg)
    if constraint == _TICKER_CONFLICT:
        msg = "coverage unit ticker already registered for an active company"
        return CoverageUnitConflictError(msg, conflict=conflict) if conflict else ValueError(msg)
    if constraint == _WORKSPACE_CONFLICT:
        msg = "workspace already bound to another active coverage unit"
        return CoverageUnitConflictError(msg, conflict=conflict) if conflict else ValueError(msg)
    msg = f"coverage unit constraint violated: {constraint or 'unique'}"
    return CoverageUnitConflictError(msg, conflict=conflict) if conflict else ValueError(msg)


def _norm_uuid(raw: Any) -> str:
    val = str(raw or "").strip()
    if not val:
        return ""
    try:
        return str(UUID(val))
    except (ValueError, AttributeError):
        return val.lower()


async def _can_read_workspace(
    ws_id: str | None,
    actor_user_id: str,
    *,
    workspace: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> bool:
    cleaned_ws = str(ws_id or "").strip()
    if not cleaned_ws:
        return False
    from src.dalton.sharing import require_workspace_access

    try:
        await require_workspace_access(
            cleaned_ws,
            actor_user_id,
            channel="web",
            need="read",
            workspace=dict(workspace) if workspace is not None else None,
            conn=conn,
        )
        return True
    except (HTTPException, PermissionError):
        return False


class _WorkspaceAccessError(ValueError):
    """Structured workspace access refusal carrying a stable machine code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.unit_error_code = code


async def _require_ws_write(
    ws_row: dict[str, Any],
    user_id: str,
    *,
    is_pm: bool = False,
    conn: Any = None,
) -> None:
    """Enforce ``channel='web', need='write'`` inside a locked transaction."""
    from src.dalton.access_tiers import classify_owner_tier
    from src.dalton.settings import is_dalton_supabase_auth_enabled
    from src.dalton.sharing import require_workspace_access
    from src.server.database.home_workspace import is_reserved_row

    if str(ws_row.get("status") or "") == "deleted":
        raise _WorkspaceAccessError(
            "workspace does not exist or is deleted", code="workspace_not_found"
        )
    if is_reserved_row(ws_row) or bool(ws_row.get("is_home")) or str(ws_row.get("status") or "") == "unbound":
        raise _WorkspaceAccessError(
            "the Home workspace never binds a coverage unit",
            code="home_workspace_refused",
        )

    ws_owner = str(ws_row.get("user_id") or "").strip()
    uid = str(user_id or "").strip()
    supabase_auth = is_dalton_supabase_auth_enabled()

    if supabase_auth and ws_owner and ws_owner != uid:
        owner_tier = await classify_owner_tier(ws_owner)
        if owner_tier == "unknown":
            raise _WorkspaceAccessError(
                "workspace does not exist or is hidden", code="workspace_hidden"
            )
        if owner_tier in ("external", "company"):
            raise _WorkspaceAccessError(
                "workspace belongs to another user", code="workspace_not_owned"
            )

    # Closed-auth deployments only allow the workspace owner (even explicit PM).
    if is_pm and supabase_auth:
        return

    try:
        await require_workspace_access(
            ws_row,
            uid,
            channel="web",
            need="write",
            workspace=ws_row,
            conn=conn,
        )
    except HTTPException as exc:
        if exc.status_code == 404:
            raise _WorkspaceAccessError(
                "workspace does not exist or is hidden", code="workspace_hidden"
            ) from exc
        if not supabase_auth and ws_owner and ws_owner != uid:
            raise _WorkspaceAccessError(
                "workspace belongs to another user", code="workspace_not_owned"
            ) from exc
        raise _WorkspaceAccessError(
            "workspace write permission required", code="workspace_write_forbidden"
        ) from exc


def compute_unit_action_flags(
    *,
    actor_user_id: str | None,
    lead_analyst_user_id: str,
    created_by_user_id: str,
    workspace_id: str | None,
    workspace_active: bool,
    workspace_owner_id: str,
    actor_has_ws_write: bool,
    is_pm: bool,
    supabase_auth_enabled: bool,
) -> dict[str, bool]:
    """Single-source action authorization flags matching write-path enforcement."""
    del workspace_owner_id
    if not actor_user_id:
        return {
            "can_bind": False,
            "can_unbind": False,
            "can_archive": False,
        }
    actor_uid = str(actor_user_id).strip()
    lead_uid = str(lead_analyst_user_id or "").strip()
    creator_uid = str(created_by_user_id or "").strip()
    eff_pm = bool(is_pm and supabase_auth_enabled)

    if not workspace_id:
        is_lead = actor_uid == lead_uid
        return {
            "can_bind": is_lead or eff_pm,
            "can_unbind": False,
            "can_archive": is_lead or eff_pm,
        }

    if not workspace_active:
        can_recover = (actor_uid in {lead_uid, creator_uid}) or eff_pm
        return {
            "can_bind": False,
            "can_unbind": can_recover,
            "can_archive": can_recover,
        }

    can_write_old = bool(actor_has_ws_write or eff_pm)
    return {
        "can_bind": False,
        "can_unbind": can_write_old,
        "can_archive": can_write_old,
    }


async def _fetch_conflict_metadata(
    c: Any,
    constraint: str,
    *,
    kind: str,
    slug: str,
    ticker_key: str | None,
    workspace_id: str | None,
    actor_user_id: str,
) -> dict[str, Any] | None:
    """Query the winning active unit after SAVEPOINT rollback and redact unreadable workspace_id."""
    winner: dict[str, Any] | None = None
    async with c.cursor(row_factory=dict_row) as cur:
        if constraint in (_SLUG_CONFLICT, _LEGACY_SLUG_CONFLICT):
            await cur.execute(
                """
                SELECT unit_id::text AS unit_id,
                       COALESCE(lead_analyst_user_id, owner_user_id) AS lead_analyst_user_id,
                       workspace_id::text AS workspace_id
                FROM dalton_coverage_units
                WHERE status = 'active' AND kind = %s AND slug = %s
                LIMIT 1
                """,
                (kind, slug),
            )
            winner = await cur.fetchone()
        elif constraint == _TICKER_CONFLICT and ticker_key:
            await cur.execute(
                """
                SELECT unit_id::text AS unit_id,
                       COALESCE(lead_analyst_user_id, owner_user_id) AS lead_analyst_user_id,
                       workspace_id::text AS workspace_id
                FROM dalton_coverage_units
                WHERE status = 'active' AND kind = 'company' AND ticker_key = %s
                LIMIT 1
                """,
                (ticker_key,),
            )
            winner = await cur.fetchone()
        elif constraint == _WORKSPACE_CONFLICT and workspace_id:
            await cur.execute(
                """
                SELECT unit_id::text AS unit_id,
                       COALESCE(lead_analyst_user_id, owner_user_id) AS lead_analyst_user_id,
                       workspace_id::text AS workspace_id
                FROM dalton_coverage_units
                WHERE status = 'active' AND workspace_id = %s::uuid
                LIMIT 1
                """,
                (workspace_id,),
            )
            winner = await cur.fetchone()

    if not winner:
        return None
    out: dict[str, Any] = {
        "unit_id": str(winner["unit_id"]),
        "lead_analyst_user_id": str(winner["lead_analyst_user_id"]),
    }
    win_ws = str(winner.get("workspace_id") or "").strip()
    if win_ws and await _can_read_workspace(win_ws, actor_user_id, conn=c):
        out["workspace_id"] = win_ws
    return out


async def has_active_coverage_unit(
    workspace_id: str | UUID,
    *,
    cur: Any = None,
) -> bool:
    """Return True if ``workspace_id`` currently holds an active coverage unit."""
    wid = str(workspace_id or "").strip()
    if not wid:
        return False
    sql = """
        SELECT EXISTS(
            SELECT 1
            FROM dalton_coverage_units
            WHERE workspace_id = %s::uuid AND status = 'active'
        ) AS has_unit
    """
    if cur is not None:
        await cur.execute(sql, (wid,))
        row = await cur.fetchone()
        return bool(row and row.get("has_unit"))
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as db_cur:
                await db_cur.execute(sql, (wid,))
                row = await db_cur.fetchone()
                return bool(row and row.get("has_unit"))
    except pg_errors.UndefinedTable:
        return False


async def get_coverage_unit(
    unit_id: str,
    *,
    conn: Any = None,
    for_share: bool = False,
) -> dict[str, Any] | None:
    """Fetch one `dalton_coverage_units` row by id, any status."""
    uid = str(unit_id or "").strip()
    if not uid:
        return None
    lock_clause = " FOR SHARE" if for_share else ""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_UNIT_COLS} FROM dalton_coverage_units WHERE unit_id = %s::uuid{lock_clause}",
                (uid,),
            )
            return dict(row) if (row := await cur.fetchone()) else None


async def _find_blocking_unit_automations(
    cur: Any,
    unit: Mapping[str, Any],
    bound_ws: str,
    *,
    limit: int = 5,
) -> tuple[list[str], bool, int]:
    """Read-only check (no row locks) for lead analyst's active/executing managed tasks or unstarted executions.

    SQL only fetches workspace/owner candidates with active/executing status or pending/waiting
    executions; authoritative unit matching runs in Python via ``is_managed_unit_automation_for_unit``
    so ticker aliases (e.g. ``600519`` vs ``600519.SS``) stay 100% consistent with tracking.
    Colleague-owned tasks on the same workspace belong to their personal scope and do not block.
    """
    from src.dalton.autonomy.classification import is_managed_unit_automation_for_unit

    lead_uid = str(
        unit.get("lead_analyst_user_id") or unit.get("owner_user_id") or ""
    ).strip()
    if not lead_uid:
        return [], False, 0

    await cur.execute(
        """
        SELECT a.automation_id::text AS automation_id,
               a.user_id,
               a.name,
               a.metadata,
               a.workspace_id::text AS workspace_id,
               a.status
        FROM automations a
        WHERE a.workspace_id = %s::uuid
          AND a.user_id = %s
          AND a.deleted_at IS NULL
          AND (
              a.status IN ('active', 'executing')
              OR EXISTS (
                  SELECT 1
                  FROM automation_executions e
                  WHERE e.automation_id = a.automation_id
                    AND e.status IN ('pending', 'waiting')
              )
          )
        ORDER BY a.name, a.automation_id
        """,
        (bound_ws, lead_uid),
    )
    rows = await cur.fetchall()
    matched_names: list[str] = []
    for r in rows or []:
        if not isinstance(r, Mapping):
            continue
        row_uid = str(r.get("user_id") or "").strip()
        if row_uid != lead_uid:
            continue
        if not is_managed_unit_automation_for_unit(r, unit=unit):
            continue
        matched_names.append(str(r.get("name") or "").strip() or "(unnamed)")
    total_count = len(matched_names)
    has_more = total_count > limit
    return matched_names[:limit], has_more, total_count


def _active_tracking_conflict_error(
    op_label: str,
    names: list[str],
    has_more: bool,
    *,
    total_count: int | None = None,
    include_names: bool = True,
) -> CoverageUnitConflictError:
    count = total_count if total_count is not None else len(names)
    if include_names:
        shown = "、".join(names[:5]) + (" 等" if has_more else "")
        return CoverageUnitConflictError(
            "coverage unit still has active automations or unstarted executions: "
            f"请先关闭该单元的跟踪（会暂停相关任务），再{op_label}（相关任务：{shown}）",
            conflict={
                "blocking_automations": list(names[:5]),
                "blocking_automations_has_more": bool(has_more),
                "blocking_automations_count": int(count),
            },
        )
    return CoverageUnitConflictError(
        "coverage unit still has active automations or unstarted executions: "
        f"请先关闭该单元的跟踪（会暂停相关任务），再{op_label}（该单元下仍有 {count} 个运行中的受管任务）",
        conflict={
            "blocking_automations_count": int(count),
        },
    )


async def _check_and_raise_blocking_automations(
    cur: Any,
    c: Any,
    unit: Mapping[str, Any],
    bound_ws: str,
    actor_uid: str,
    ws_row: Mapping[str, Any] | None,
    *,
    op_label: str,
) -> None:
    names, has_more, total_count = await _find_blocking_unit_automations(
        cur, unit, bound_ws
    )
    if total_count <= 0:
        return
    if ws_row is None or str(ws_row.get("status") or "") == "deleted":
        can_read = False
    else:
        can_read = await _can_read_workspace(
            bound_ws, actor_uid, workspace=ws_row, conn=c
        )
    raise _active_tracking_conflict_error(
        op_label,
        names,
        has_more,
        total_count=total_count,
        include_names=can_read,
    )


async def _precheck_blocking_before_unit_lock(
    c: Any,
    unit_id: str,
    actor_uid: str,
    *,
    is_pm: bool,
    eff_pm: bool,
    op_label: str,
    target_status: str | None = None,
) -> None:
    """Unlocked pre-check BEFORE `dalton_coverage_units FOR UPDATE` (only after verifying caller auth)."""
    async with c.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT unit_id::text AS unit_id, kind, slug, ticker, ticker_key, owner_user_id,
                   COALESCE(created_by_user_id, owner_user_id) AS created_by_user_id,
                   COALESCE(lead_analyst_user_id, owner_user_id) AS lead_analyst_user_id,
                   workspace_id::text AS workspace_id, status
            FROM dalton_coverage_units
            WHERE unit_id = %s::uuid
            """,
            (unit_id,),
        )
        pre = await cur.fetchone()
        if pre is None:
            return
        if target_status is not None and str(pre.get("status") or "").strip() == target_status:
            return
        bound_ws = str(pre.get("workspace_id") or "").strip()
        if not bound_ws:
            return
        lead_uid = str(pre.get("lead_analyst_user_id") or pre.get("owner_user_id") or "").strip()
        creator_uid = str(pre.get("created_by_user_id") or "").strip()
        await cur.execute(
            """
            SELECT workspace_id::text AS workspace_id, user_id, status, config
            FROM workspaces
            WHERE workspace_id = %s::uuid
            """,
            (bound_ws,),
        )
        ws_row = await cur.fetchone()
        if ws_row is not None and str(ws_row.get("status") or "") != "deleted":
            await _require_ws_write(ws_row, actor_uid, is_pm=is_pm, conn=c)
        elif actor_uid not in {lead_uid, creator_uid} and not eff_pm:
            return
        await _check_and_raise_blocking_automations(
            cur,
            c,
            pre,
            bound_ws,
            actor_uid,
            ws_row,
            op_label=op_label,
        )


async def get_active_unit_by_workspace(
    workspace_id: str,
    *,
    conn: Any = None,
    for_share: bool = False,
) -> dict[str, Any] | None:
    """Fetch the active unit bound to `workspace_id`, if any (identity resolution's lookup)."""
    wid = str(workspace_id or "").strip()
    if not wid:
        return None
    lock_clause = " FOR SHARE" if for_share else ""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_UNIT_COLS}
                FROM dalton_coverage_units
                WHERE workspace_id = %s::uuid AND status = 'active'{lock_clause}
                """,
                (wid,),
            )
            return dict(row) if (row := await cur.fetchone()) else None


async def load_active_units_by_workspace(
    workspace_ids: list[str] | tuple[str, ...],
    *,
    conn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Batch form of `get_active_unit_by_workspace` for board-style enumeration."""
    ids = [str(w).strip() for w in workspace_ids if str(w or "").strip()]
    if not ids:
        return {}
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
    return {str(r["workspace_id"]): dict(r) for r in rows if r.get("workspace_id")}


async def list_active_company_units_with_ticker(
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Return active company units carrying a ticker (for Home coverage.yaml sync)."""
    async with get_db_connection(conn) as c:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_UNIT_COLS}
                FROM dalton_coverage_units
                WHERE status = 'active' AND kind = 'company' AND ticker IS NOT NULL
                ORDER BY slug
                """
            )
            return [dict(r) for r in await cur.fetchall()]


async def list_coverage_units(
    owner_user_id: str | None = None,
    *,
    include_archived: bool = False,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """List units, optionally scoped to units led/owned/bound-to `owner_user_id`."""
    clauses: list[str] = []
    params: list[Any] = []
    if owner_user_id:
        uid = str(owner_user_id).strip()
        clauses.append(
            """
            (
                COALESCE(lead_analyst_user_id, owner_user_id) = %s
                OR owner_user_id = %s
                OR EXISTS (
                    SELECT 1 FROM workspaces w
                    WHERE w.workspace_id = dalton_coverage_units.workspace_id
                      AND w.user_id = %s
                      AND w.status <> 'deleted'
                )
            )
            """
        )
        params.extend((uid, uid, uid))
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
                ORDER BY lead_analyst_user_id, kind, slug
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
    ticker_key: str | None = None,
    lead_analyst_user_id: str | None = None,
    workspace_id: str | None = None,
    research_tz: str | None = None,
    unit_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert one unit row in a single transaction with SAVEPOINT 409 arbitration.

    When ``workspace_id`` is provided, locks the target workspace ``FOR UPDATE``
    first, verifies write permission / non-Home / non-historical-duplicate, and
    sets ``lead_analyst_user_id = owner_user_id = target_ws.user_id``.
    """
    from src.dalton.access_tiers import is_admin_user
    from src.dalton.settings import is_coverage_units_enabled

    eff_ticker_key = (
        ticker_key
        if ticker_key is not None
        else (normalize_ticker_key(ticker) if ticker and kind == "company" else None)
    )
    actor_uid = str(owner_user_id).strip()
    eff_lead = str(lead_analyst_user_id or actor_uid).strip()
    eff_owner = eff_lead
    target_ws = str(workspace_id).strip() if workspace_id else None
    is_pm = await is_admin_user(actor_uid, conn=conn)

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        nonlocal eff_lead, eff_owner
        ws_row: dict[str, Any] | None = None
        if target_ws is not None:
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT w.workspace_id::text AS workspace_id, w.user_id, w.status, w.config
                    FROM workspaces w
                    WHERE w.workspace_id = %s::uuid
                    FOR UPDATE
                    """,
                    (target_ws,),
                )
                ws_row = await cur.fetchone()
                if ws_row is None or str(ws_row.get("status") or "") == "deleted":
                    raise ValueError("workspace does not exist or is deleted")
                await _require_ws_write(ws_row, actor_uid, is_pm=is_pm, conn=c)
                if is_historical_duplicate_workspace(ws_row):
                    raise ValueError("historical duplicate workspace cannot bind a coverage unit")
                await cur.execute(
                    """
                    SELECT slug
                    FROM dalton_coverage_units
                    WHERE workspace_id = %s::uuid AND status = 'active'
                    """,
                    (target_ws,),
                )
                if await cur.fetchone():
                    raise ValueError("workspace already bound to another active coverage unit")
                eff_lead = str(ws_row["user_id"])
                eff_owner = eff_lead

        id_col = "unit_id, " if unit_id else ""
        id_param = "%s::uuid, " if unit_id else ""
        params: tuple[Any, ...] = (
            kind,
            eff_owner,
            actor_uid,
            eff_lead,
            slug,
            display_name,
            target_ws,
            ticker,
            eff_ticker_key,
            research_tz,
        )
        if unit_id:
            params = (unit_id, *params)

        try:
            async with c.transaction():
                async with c.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        f"""
                        INSERT INTO dalton_coverage_units (
                            {id_col}kind, owner_user_id, created_by_user_id, lead_analyst_user_id, slug,
                            display_name, workspace_id, ticker, ticker_key, status,
                            research_tz, created_at, updated_at
                        )
                        VALUES (
                            {id_param}%s, %s, %s, %s, %s,
                            %s, %s::uuid, %s, %s, 'active',
                            %s, NOW(), NOW()
                        )
                        RETURNING {_UNIT_COLS}
                        """,
                        params,
                    )
                    row = await cur.fetchone()
                    assert row is not None
                    inserted = dict(row)
        except pg_errors.UniqueViolation as exc:
            constraint = str(getattr(getattr(exc, "diag", None), "constraint_name", "") or "")
            conflict = await _fetch_conflict_metadata(
                c,
                constraint,
                kind=kind,
                slug=slug,
                ticker_key=eff_ticker_key,
                workspace_id=target_ws,
                actor_user_id=actor_uid,
            )
            raise _friendly_unique_error(exc, conflict=conflict) from exc

        if (
            target_ws is not None
            and str(inserted.get("kind") or "") == "sector"
            and is_coverage_units_enabled()
        ):
            from src.dalton.database.sector_members import sync_projection_to_latest_doc

            norm_ws = str((ws_row or {}).get("workspace_id") or target_ws)
            async with c.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton:kv:{norm_ws}:sector",),
                )
                await sync_projection_to_latest_doc(
                    cur, norm_ws, str(inserted["unit_id"]), clear_when_no_doc=True
                )
        return inserted

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
    previous_out: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Set `status` on a unit (archive/reactivate) after checking workspace write or lead/PM."""
    from src.dalton.access_tiers import is_admin_user
    from src.dalton.settings import is_dalton_auth_enabled

    actor_uid = str(owner_user_id or "").strip()
    is_pm = await is_admin_user(actor_uid, conn=conn)
    eff_pm = is_pm and is_dalton_auth_enabled()

    async def _run_in_tx(c: Any) -> dict[str, Any] | None:
        if status == "archived":
            await _precheck_blocking_before_unit_lock(
                c,
                unit_id,
                actor_uid,
                is_pm=is_pm,
                eff_pm=eff_pm,
                op_label="归档",
                target_status="archived",
            )
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT unit_id::text AS unit_id, kind, slug, ticker, ticker_key, owner_user_id,
                       COALESCE(created_by_user_id, owner_user_id) AS created_by_user_id,
                       COALESCE(lead_analyst_user_id, owner_user_id) AS lead_analyst_user_id,
                       workspace_id::text AS workspace_id, status
                FROM dalton_coverage_units
                WHERE unit_id = %s::uuid
                FOR UPDATE
                """,
                (unit_id,),
            )
            pre = await cur.fetchone()
            if pre is None:
                return None
            if previous_out is not None:
                previous_out.clear()
                previous_out.update(dict(pre))

            bound_ws = str(pre.get("workspace_id") or "").strip()
            lead_uid = str(pre.get("lead_analyst_user_id") or pre.get("owner_user_id") or "").strip()
            creator_uid = str(pre.get("created_by_user_id") or "").strip()
            ws_row: dict[str, Any] | None = None
            if bound_ws:
                await cur.execute(
                    """
                    SELECT w.workspace_id::text AS workspace_id, w.user_id, w.status, w.config
                    FROM workspaces w
                    WHERE w.workspace_id = %s::uuid
                    FOR UPDATE
                    """,
                    (bound_ws,),
                )
                ws_row = await cur.fetchone()
                if ws_row is not None and str(ws_row.get("status") or "") != "deleted":
                    await _require_ws_write(ws_row, actor_uid, is_pm=is_pm, conn=c)
                elif actor_uid not in {lead_uid, creator_uid} and not eff_pm:
                    return None
            elif actor_uid != lead_uid and not eff_pm:
                return None

            if str(pre.get("status") or "").strip() == status:
                await cur.execute(
                    f"SELECT {_UNIT_COLS} FROM dalton_coverage_units WHERE unit_id = %s::uuid",
                    (unit_id,),
                )
                same_row = await cur.fetchone()
                return dict(same_row) if same_row else None

            if status == "archived" and bound_ws:
                await _check_and_raise_blocking_automations(
                    cur,
                    c,
                    pre,
                    bound_ws,
                    actor_uid,
                    ws_row,
                    op_label="归档",
                )

            if status == "archived" and str(pre.get("kind") or "") == "sector":
                await _clear_sector_projection(cur, bound_ws, unit_id)

            try:
                async with c.transaction():
                    await cur.execute(
                        f"""
                        UPDATE dalton_coverage_units
                        SET status = %s,
                            updated_at = NOW()
                        WHERE unit_id = %s::uuid
                        RETURNING {_UNIT_COLS}
                        """,
                        (status, unit_id),
                    )
                    row = await cur.fetchone()
                    return dict(row) if row else None
            except pg_errors.UniqueViolation as exc:
                constraint = str(getattr(getattr(exc, "diag", None), "constraint_name", "") or "")
                conflict = await _fetch_conflict_metadata(
                    c,
                    constraint,
                    kind=str(pre.get("kind") or ""),
                    slug=str(pre.get("slug") or ""),
                    ticker_key=pre.get("ticker_key"),
                    workspace_id=bound_ws or None,
                    actor_user_id=actor_uid,
                )
                raise _friendly_unique_error(exc, conflict=conflict) from exc

    if conn is not None:
        return await _run_in_tx(conn)
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)


async def _clear_sector_projection(
    cur: Any, workspace_id: str | None, unit_id: str
) -> None:
    """Delete one sector unit's member rows under the workspace's kv advisory lock."""
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
    previous_out: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind an unbound active unit to a writable non-Home, non-duplicate workspace (or idempotent same-ws).

    Lock order: unit row ``FOR UPDATE`` -> sorted ``{old_ws, target_ws} FOR UPDATE``
    -> sorted sector advisory locks. Rebinding to a different workspace is refused (409).
    """
    from src.dalton.access_tiers import is_admin_user
    from src.dalton.settings import is_coverage_units_enabled, is_dalton_auth_enabled

    actor_uid = str(owner_user_id or "").strip()
    target_req = _norm_uuid(workspace_id)
    is_pm = await is_admin_user(actor_uid, conn=conn)
    eff_pm = is_pm and is_dalton_auth_enabled()

    async def _run_in_tx(c: Any) -> dict[str, Any]:
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT unit_id::text AS unit_id, kind, slug, ticker, ticker_key, owner_user_id,
                       COALESCE(created_by_user_id, owner_user_id) AS created_by_user_id,
                       COALESCE(lead_analyst_user_id, owner_user_id) AS lead_analyst_user_id,
                       workspace_id::text AS workspace_id, status
                FROM dalton_coverage_units
                WHERE unit_id = %s::uuid AND status = 'active'
                FOR UPDATE
                """,
                (unit_id,),
            )
            pre = await cur.fetchone()
            if pre is None:
                raise ValueError(f"active coverage unit {unit_id} not found for this owner")
            if previous_out is not None:
                previous_out.clear()
                previous_out.update(dict(pre))

            previous_ws = _norm_uuid(pre.get("workspace_id"))
            current_lead = str(
                pre.get("lead_analyst_user_id") or pre.get("owner_user_id") or ""
            ).strip()
            creator_uid = str(pre.get("created_by_user_id") or "").strip()
            # First bind (not previous_ws): strictly current lead_analyst or effective PM (§2.2).
            if not previous_ws and actor_uid != current_lead and not eff_pm:
                raise ValueError(f"active coverage unit {unit_id} not found for this owner")

            ws_lock_ids = sorted({target_req, previous_ws} - {""})
            await cur.execute(
                """
                SELECT w.workspace_id::text AS workspace_id, w.user_id, w.status, w.config
                FROM workspaces w
                WHERE w.workspace_id = ANY(%s::uuid[])
                ORDER BY w.created_at, w.workspace_id
                FOR UPDATE
                """,
                (ws_lock_ids,),
            )
            ws_by_id = {
                _norm_uuid(r["workspace_id"]): dict(r)
                for r in await cur.fetchall()
            }

            # Validate caller access to the original unit / previous binding BEFORE inspecting target workspace
            # so hidden units cannot be distinguished from nonexistent units via target errors (codex r7 L2).
            if previous_ws:
                old_ws = ws_by_id.get(previous_ws)
                if old_ws is not None and str(old_ws.get("status") or "") != "deleted":
                    try:
                        await _require_ws_write(old_ws, actor_uid, is_pm=is_pm, conn=c)
                    except _WorkspaceAccessError as exc:
                        if exc.unit_error_code in {"workspace_hidden", "workspace_not_owned"}:
                            raise ValueError(
                                f"active coverage unit {unit_id} not found for this owner"
                            ) from exc
                        raise
                elif actor_uid not in {current_lead, creator_uid} and not eff_pm:
                    raise ValueError(
                        f"active coverage unit {unit_id} not found for this owner"
                    )
                if previous_ws != target_req:
                    raise ValueError(
                        "coverage unit already bound to another workspace; 请先解绑再绑定到新的工作区"
                    )

            ws_row = ws_by_id.get(target_req)
            if ws_row is None or str(ws_row.get("status") or "") == "deleted":
                raise ValueError("workspace does not exist or is deleted")
            await _require_ws_write(ws_row, actor_uid, is_pm=is_pm, conn=c)
            if is_historical_duplicate_workspace(ws_row):
                raise ValueError("historical duplicate workspace cannot bind a coverage unit")

            # Idempotent same-workspace bind: keep lead analyst and all fields untouched.
            if previous_ws and previous_ws == target_req:
                await cur.execute(
                    f"SELECT {_UNIT_COLS} FROM dalton_coverage_units WHERE unit_id = %s::uuid",
                    (unit_id,),
                )
                same_row = await cur.fetchone()
                if same_row is None:
                    raise ValueError(f"active coverage unit {unit_id} not found for this owner")
                return dict(same_row)

            await cur.execute(
                """
                SELECT slug FROM dalton_coverage_units
                WHERE workspace_id = %s::uuid AND status = 'active' AND unit_id <> %s::uuid
                """,
                (workspace_id, unit_id),
            )
            if await cur.fetchone():
                raise ValueError("workspace already bound to another active coverage unit")

            new_lead = str(ws_row["user_id"])
            try:
                await cur.execute(
                    f"""
                    UPDATE dalton_coverage_units
                    SET workspace_id = %s::uuid,
                        lead_analyst_user_id = %s,
                        owner_user_id = %s,
                        updated_at = NOW()
                    WHERE unit_id = %s::uuid AND status = 'active'
                    RETURNING {_UNIT_COLS}
                    """,
                    (
                        workspace_id,
                        new_lead,
                        new_lead,
                        unit_id,
                    ),
                )
            except pg_errors.UniqueViolation as exc:
                raise _friendly_unique_error(exc) from exc
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"active coverage unit {unit_id} not found for this owner")

            if str(row.get("kind") or "") == "sector" and is_coverage_units_enabled():
                from src.dalton.database.sector_members import sync_projection_to_latest_doc

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
    previous_out: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Clear a unit's workspace binding after checking workspace write or lead/PM and zero active tracking."""
    from src.dalton.access_tiers import is_admin_user
    from src.dalton.settings import is_dalton_auth_enabled

    actor_uid = str(owner_user_id or "").strip()
    is_pm = await is_admin_user(actor_uid, conn=conn)
    eff_pm = is_pm and is_dalton_auth_enabled()

    async def _run_in_tx(c: Any) -> dict[str, Any] | None:
        await _precheck_blocking_before_unit_lock(
            c,
            unit_id,
            actor_uid,
            is_pm=is_pm,
            eff_pm=eff_pm,
            op_label="解绑",
        )
        async with c.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT unit_id::text AS unit_id, kind, slug, ticker, ticker_key, owner_user_id,
                       COALESCE(created_by_user_id, owner_user_id) AS created_by_user_id,
                       COALESCE(lead_analyst_user_id, owner_user_id) AS lead_analyst_user_id,
                       workspace_id::text AS workspace_id, status
                FROM dalton_coverage_units
                WHERE unit_id = %s::uuid
                FOR UPDATE
                """,
                (unit_id,),
            )
            pre = await cur.fetchone()
            if pre is None:
                return None
            if previous_out is not None:
                previous_out.clear()
                previous_out.update(dict(pre))

            bound_ws = str(pre.get("workspace_id") or "").strip()
            lead_uid = str(pre.get("lead_analyst_user_id") or pre.get("owner_user_id") or "").strip()
            creator_uid = str(pre.get("created_by_user_id") or "").strip()
            ws_row: dict[str, Any] | None = None
            if bound_ws:
                await cur.execute(
                    """
                    SELECT w.workspace_id::text AS workspace_id, w.user_id, w.status, w.config
                    FROM workspaces w
                    WHERE w.workspace_id = %s::uuid
                    FOR UPDATE
                    """,
                    (bound_ws,),
                )
                ws_row = await cur.fetchone()
                if ws_row is not None and str(ws_row.get("status") or "") != "deleted":
                    await _require_ws_write(ws_row, actor_uid, is_pm=is_pm, conn=c)
                elif actor_uid not in {lead_uid, creator_uid} and not eff_pm:
                    return None
            elif actor_uid != lead_uid and not eff_pm:
                return None

            if not bound_ws:
                await cur.execute(
                    f"SELECT {_UNIT_COLS} FROM dalton_coverage_units WHERE unit_id = %s::uuid",
                    (unit_id,),
                )
                same_row = await cur.fetchone()
                return dict(same_row) if same_row else None

            await _check_and_raise_blocking_automations(
                cur,
                c,
                pre,
                bound_ws,
                actor_uid,
                ws_row,
                op_label="解绑",
            )

            if str(pre.get("kind") or "") == "sector":
                await _clear_sector_projection(cur, bound_ws, unit_id)
            await cur.execute(
                f"""
                UPDATE dalton_coverage_units
                SET workspace_id = NULL,
                    updated_at = NOW()
                WHERE unit_id = %s::uuid
                RETURNING {_UNIT_COLS}
                """,
                (unit_id,),
            )
            return dict(row) if (row := await cur.fetchone()) else None

    if conn is not None:
        return await _run_in_tx(conn)
    async with get_db_connection() as c:
        async with c.transaction():
            return await _run_in_tx(c)
