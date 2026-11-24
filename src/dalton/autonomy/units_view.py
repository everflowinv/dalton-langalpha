"""Coverage-unit view assembly shared by the director board and the HTTP API.

The ``units`` view (sector layer P2) used to live inline in
``build_coverage_board``; the P4 coverage API needs the same enumeration, so
the block moved here verbatim and the board delegates. Colleague visibility,
member rendering, and the automations preload have exactly one implementation
— board, list endpoint and detail endpoint all read through these functions,
so the three surfaces cannot drift on who sees what.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import inspect
import logging
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row

from src.dalton.coverage.identity import is_historical_duplicate_workspace
from src.dalton.database.coverage_units import (
    can_write_bound_unit_workspace,
    can_write_bound_unit_workspaces_batch,
    compute_unit_action_flags,
    get_coverage_unit,
    is_coverage_multiuser_auth_enabled,
    load_active_units_by_workspace,
)
from src.dalton.invites import is_invite_pm
from src.dalton.settings import (
    is_dalton_local_auth_enabled,
    is_dalton_supabase_auth_enabled as _default_is_dalton_supabase_auth_enabled,
)
from src.server.database.home_workspace import is_reserved_row
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)
# Preserved as a module attribute so Everflow unit tests that monkeypatch
# `units_view.is_dalton_supabase_auth_enabled` continue to work unchanged.
is_dalton_supabase_auth_enabled = _default_is_dalton_supabase_auth_enabled


def _is_multiuser_auth_enabled() -> bool:
    """Delegate to the single-source predicate shared with ``coverage_units._require_ws_write``."""
    if is_dalton_supabase_auth_enabled is not _default_is_dalton_supabase_auth_enabled:
        return bool(is_dalton_supabase_auth_enabled()) or is_dalton_local_auth_enabled()
    return is_coverage_multiuser_auth_enabled()


async def _is_coverage_pm(user_id: str | None, *, conn: Any = None) -> bool:
    """Return True when ``user_id`` is a coverage admin for the active auth mode."""
    if user_id is None:
        return False
    if is_dalton_local_auth_enabled():
        from src.dalton.access_tiers import is_admin_user

        return (
            await is_admin_user(user_id, conn=conn)
            if conn is not None
            else await is_admin_user(user_id)
        )
    return is_invite_pm(user_id)


@asynccontextmanager
async def _repeatable_read_conn(conn: Any = None, *, get_conn: Any = None):
    """Open or reuse a connection running in REPEATABLE READ when backed by a real tx."""
    if conn is not None:
        yield conn
        return
    conn_factory = get_conn or get_db_connection
    async with conn_factory() as owned:
        tx_fn = getattr(owned, "transaction", None)
        if callable(tx_fn):
            async with tx_fn():
                async with owned.cursor() as iso_cur:
                    await iso_cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                yield owned
        else:
            yield owned


@asynccontextmanager
async def _savepoint(conn: Any):
    """Wrap degradable reads inside REPEATABLE READ so SQL errors roll back locally."""
    tx_fn = getattr(conn, "transaction", None)
    if callable(tx_fn):
        async with tx_fn():
            yield
    else:
        yield


async def load_dalton_automations_by_user(
    cur: Any, user_id: str | None
) -> dict[str, list[dict[str, Any]]]:
    """Prefetch the user's ``dalton-*`` automation rows (identity fallback input).

    Same filter and order as tracking's own rule: SQL only prefilters on the
    ``dalton-`` prefix, Python's ``strip().lower().startswith`` decides —
    paused rows count too, because a paused tracking task is still verified
    tracking information.
    """
    autos_by_user: dict[str, list[dict[str, Any]]] = {}
    # 与 tracking 的筛选和顺序一致：它按 strip().lower().startswith("dalton-")
    # 过滤、按 file_name 排序取第一条，这里用 strpos(lower(name), 'dalton-')
    # 等价筛选，并保持 ORDER BY file_name 的确定顺序。
    if user_id is not None:
        await cur.execute(
            """
            SELECT user_id, workspace_id::text AS workspace_id, name, metadata
            FROM automations
            WHERE strpos(lower(name), 'dalton-') > 0 AND user_id = %s
            ORDER BY file_name
            """,
            (user_id,),
        )
    else:
        await cur.execute(
            """
            SELECT user_id, workspace_id::text AS workspace_id, name, metadata
            FROM automations
            WHERE strpos(lower(name), 'dalton-') > 0
            ORDER BY file_name
            """
        )
    for ar in await cur.fetchall() or []:
        # The SQL only prefilters; tracking's own rule (Python strip(), which
        # also drops tabs and other whitespace btrim keeps) decides.
        if (
            isinstance(ar, dict)
            and ar.get("user_id")
            and str(ar.get("name") or "").strip().lower().startswith("dalton-")
        ):
            autos_by_user.setdefault(str(ar["user_id"]), []).append(dict(ar))
    return autos_by_user


async def _load_colleague_workspace_state(
    user_id: str | None,
    *,
    conn: Any = None,
) -> tuple[
    dict[str, tuple[bool, bool]],
    dict[str, str],
    list[dict[str, Any]],
]:
    """Load colleague-visible workspaces: ``(access_map, web_level_map, flat_rows)``."""
    colleague_access: dict[str, tuple[bool, bool]] = {}
    web_levels: dict[str, str] = {}
    flat_rows: list[dict[str, Any]] = []
    if user_id is None:
        return colleague_access, web_levels, flat_rows
    try:
        from src.dalton.sharing import list_team_workspaces

        team_kw: dict[str, Any] = {}
        if conn is not None and "conn" in inspect.signature(list_team_workspaces).parameters:
            team_kw["conn"] = conn
        for member in await list_team_workspaces(user_id, **team_kw):
            member_uid = str(member.get("user_id") or "").strip()
            member_name = member.get("display_name") or member.get("name")
            member_email = member.get("email")
            for cws in member.get("workspaces") or []:
                ws_id_c = str(cws.get("workspace_id") or "")
                if not ws_id_c:
                    continue
                web_acc = str(cws.get("web_access") or "hidden").strip().lower()
                cos_acc = str(cws.get("cos_access") or "hidden").strip().lower()
                colleague_access[ws_id_c] = (
                    web_acc != "hidden",
                    cos_acc == "rw",
                )
                web_levels[ws_id_c] = web_acc
                flat_rows.append(
                    {
                        **dict(cws),
                        "workspace_id": ws_id_c,
                        "owner_user_id": str(cws.get("user_id") or member_uid),
                        "owner_name": cws.get("owner_name") or member_name,
                        "owner_email": cws.get("owner_email") or member_email,
                        "web_access": web_acc,
                    }
                )
    except Exception as exc:
        logger.warning("[DALTON_UNITS_VIEW] team workspace lookup failed: %s", exc)
    return colleague_access, web_levels, flat_rows


async def load_colleague_workspace_access(
    user_id: str | None,
    *,
    conn: Any = None,
) -> dict[str, tuple[bool, bool]]:
    """Map each colleague-visible workspace to ``(web_readable, cos_rw)``."""
    colleague_access, _, _ = await _load_colleague_workspace_state(user_id, conn=conn)
    return colleague_access


def member_workspace_visible(
    company_row: dict[str, Any] | None,
    user_id: str | None,
    colleague_access: dict[str, tuple[bool, bool]],
) -> bool:
    """A member's company workspace id is only output when the viewer may read it.

    Readability follows the BOUND WORKSPACE's actual permissions (``workspaces.user_id == uid``
    or visible in ``list_team_workspaces``), never the unit's ``owner_user_id``.
    """
    if company_row is None:
        return False
    if user_id is None:
        return True
    ws_id = str(company_row.get("workspace_id") or "").strip()
    if not ws_id:
        return False
    if str(company_row.get("workspace_owner_id") or "") == str(user_id):
        return True
    return colleague_access.get(ws_id, (False, False))[0]


def render_sector_member(
    m: dict[str, Any],
    company_row: dict[str, Any] | None,
    visible: bool,
) -> dict[str, Any]:
    """One sector member row as the board and the detail endpoint output it."""
    return {
        "member_id": m.get("member_id"),
        "name": m.get("member_name"),
        "ticker": m.get("ticker"),
        "company_unit_id": m.get("company_unit_id"),
        "workspace_created": bool(m.get("workspace_created")),
        "workspace_id": (
            company_row.get("workspace_id") if visible else None
        ),
        "scope": m.get("scope") or "",
        "reason": m.get("reason") or "",
        "valid_until": (
            m["valid_until"].isoformat() if m.get("valid_until") else None
        ),
    }


async def _load_company_bindings(
    ucur: Any, member_rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Active company-unit bindings for the members of the given sectors.

    Only active bindings count: an archived company unit no longer points at
    its workspace, which would otherwise read as a false "workspace created".
    """
    company_unit_ids = sorted(
        {
            str(m.get("company_unit_id"))
            for m in member_rows
            if m.get("company_unit_id")
        }
    )
    company_ws_by_unit: dict[str, dict[str, Any]] = {}
    if company_unit_ids:
        await ucur.execute(
            """
            SELECT u.unit_id::text AS unit_id, u.status, u.owner_user_id,
                   COALESCE(u.lead_analyst_user_id, u.owner_user_id) AS lead_analyst_user_id,
                   u.workspace_id::text AS workspace_id,
                   w.user_id AS workspace_owner_id
            FROM dalton_coverage_units u
            LEFT JOIN workspaces w ON w.workspace_id = u.workspace_id AND w.status <> 'deleted'
            WHERE u.unit_id = ANY(%s::uuid[])
            """,
            (company_unit_ids,),
        )
        for cr in await ucur.fetchall():
            company_ws_by_unit[str(cr["unit_id"])] = dict(cr)
        company_ws_by_unit = {
            uid: row
            for uid, row in company_ws_by_unit.items()
            if str(row.get("status") or "") == "active"
        }
    return company_ws_by_unit


def _compute_unit_action_flags(
    *,
    user_id: str | None,
    lead_uid: str,
    created_by_uid: str = "",
    ws_id: str | None,
    ws_active: bool = False,
    ws_owner: str,
    readable: bool,
    web_level: str,
    is_pm: bool,
    supabase_auth_enabled: bool | None = None,
    actor_has_ws_write: bool | None = None,
    workspace_owner_tier: str | None = None,
) -> dict[str, bool]:
    supabase_auth = (
        _is_multiuser_auth_enabled()
        if supabase_auth_enabled is None
        else bool(supabase_auth_enabled)
    )
    if actor_has_ws_write is None:
        owner_allows_colleague_write = (
            ws_owner == user_id
            or str(workspace_owner_tier or "").strip().lower() in ("admin", "internal")
        )
        has_ws_write = bool(
            ws_id
            and ws_active
            and user_id
            and (
                ws_owner == user_id
                or (
                    supabase_auth
                    and readable
                    and web_level == "rw"
                    and owner_allows_colleague_write
                )
            )
        )
    else:
        has_ws_write = bool(actor_has_ws_write)
    return compute_unit_action_flags(
        actor_user_id=user_id,
        lead_analyst_user_id=lead_uid,
        created_by_user_id=created_by_uid,
        workspace_id=ws_id,
        workspace_active=ws_active,
        workspace_owner_id=ws_owner,
        actor_has_ws_write=has_ws_write,
        is_pm=is_pm,
        supabase_auth_enabled=supabase_auth,
        workspace_owner_tier=workspace_owner_tier,
    )


async def _batch_readable_workspaces(
    cur: Any,
    conn: Any,
    uid: str,
    candidate_ws_ids: set[str],
    colleague_access: dict[str, tuple[bool, bool]],
    *,
    own_readable_ws: str | None = None,
) -> set[str]:
    """Return subset of ``candidate_ws_ids`` the caller currently holds Web read access to."""
    readable: set[str] = {
        wid for wid, acc in colleague_access.items() if acc[0]
    }
    if own_readable_ws:
        readable.add(own_readable_ws)
    to_check = sorted(wid for wid in candidate_ws_ids if wid and wid not in readable)
    if not to_check:
        return readable
    try:
        from fastapi import HTTPException
        from src.dalton.sharing import require_workspace_access

        await cur.execute(
            """
            SELECT workspace_id::text AS workspace_id, user_id, status
            FROM workspaces
            WHERE workspace_id = ANY(%s::uuid[]) AND status <> 'deleted'
            """,
            (to_check,),
        )
        rows = [dict(r) for r in await cur.fetchall()]
        for ws in rows:
            wid = str(ws.get("workspace_id") or "")
            if not wid:
                continue
            try:
                await require_workspace_access(
                    ws, uid, channel="web", need="read", workspace=ws, conn=conn
                )
                readable.add(wid)
            except HTTPException:
                pass
    except Exception as exc:
        logger.warning("[DALTON_UNITS_VIEW] workspace read check failed: %s", exc)
    return readable


async def build_units_view(
    user_id: str | None,
    autos_by_user: dict[str, list[dict[str, Any]]],
    *,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """The cross-owner coverage-unit list (P2 board ``units``).

    Reads colleague permissions, unit bindings, sector members, and company
    bindings under a single REPEATABLE READ snapshot so concurrent rebinds cannot
    expose new workspace members under stale workspace permissions.
    """
    from src.dalton.database.sector_members import fetch_members_for_sectors

    supabase_auth = _is_multiuser_auth_enabled()
    own_auto_ws = {
        str(a.get("workspace_id") or "")
        for a in autos_by_user.get(str(user_id or ""), [])
        if a.get("workspace_id")
    }
    out: list[dict[str, Any]] = []
    async with _repeatable_read_conn(conn) as uconn:
        is_pm = await _is_coverage_pm(user_id, conn=uconn)
        eff_pm = is_pm and supabase_auth
        colleague_access, web_levels, _ = await _load_colleague_workspace_state(
            user_id, conn=uconn
        )
        async with uconn.cursor(row_factory=dict_row) as ucur:
            await ucur.execute(
                """
                SELECT u.unit_id::text AS unit_id, u.kind, u.owner_user_id,
                       COALESCE(u.created_by_user_id, u.owner_user_id) AS created_by_user_id,
                       COALESCE(u.lead_analyst_user_id, u.owner_user_id) AS lead_analyst_user_id,
                       u.slug, u.display_name, u.workspace_id::text AS workspace_id,
                       u.ticker, w.user_id AS workspace_owner_id, w.status AS workspace_status,
                       w.config AS workspace_config
                FROM dalton_coverage_units u
                LEFT JOIN workspaces w ON w.workspace_id = u.workspace_id
                WHERE u.status = 'active'
                  AND (
                      %s::text IS NULL
                      OR COALESCE(u.lead_analyst_user_id, u.owner_user_id) = %s
                      OR u.created_by_user_id = %s
                      OR u.owner_user_id = %s
                  )
                ORDER BY u.kind, u.slug
                """,
                (user_id, user_id, user_id, user_id),
            )
            unit_rows = [dict(r) for r in await ucur.fetchall()]

            if user_id is not None:
                await ucur.execute(
                    """
                    SELECT u.unit_id::text AS unit_id, u.kind, u.owner_user_id,
                           COALESCE(u.created_by_user_id, u.owner_user_id) AS created_by_user_id,
                           COALESCE(u.lead_analyst_user_id, u.owner_user_id) AS lead_analyst_user_id,
                           u.slug, u.display_name, u.workspace_id::text AS workspace_id,
                           u.ticker, w.user_id AS workspace_owner_id, w.status AS workspace_status,
                           w.config AS workspace_config
                    FROM dalton_coverage_units u
                    JOIN workspaces w ON w.workspace_id = u.workspace_id AND w.status <> 'deleted'
                    WHERE u.status = 'active'
                      AND (u.workspace_id = ANY(%s::uuid[]) OR w.user_id = %s)
                    ORDER BY u.kind, u.slug
                    """,
                    (list(colleague_access), user_id),
                )
                existing_ids = {u["unit_id"] for u in unit_rows}
                unit_rows.extend(
                    dict(r)
                    for r in await ucur.fetchall()
                    if str(r.get("unit_id")) not in existing_ids
                )

            sector_ids = [
                str(u["unit_id"]) for u in unit_rows if u.get("kind") == "sector"
            ]
            member_rows = await fetch_members_for_sectors(ucur, sector_ids)
            members_by_sector: dict[str, list[dict[str, Any]]] = {}
            for mr in member_rows:
                members_by_sector.setdefault(
                    str(mr.get("sector_unit_id")), []
                ).append(dict(mr))
            company_ws_by_unit = await _load_company_bindings(ucur, member_rows)

        ws_owner_tiers: dict[str, str] = {}
        ws_write_map: dict[str, bool] | None = None
        if user_id is not None and supabase_auth:
            non_self_ws_owners = sorted(
                {
                    str(u.get("workspace_owner_id") or "").strip()
                    for u in unit_rows
                    if u.get("workspace_id")
                    and str(u.get("workspace_status") or "") != "deleted"
                    and str(u.get("workspace_owner_id") or "").strip()
                    and str(u.get("workspace_owner_id") or "").strip() != str(user_id)
                }
            )
            if non_self_ws_owners:
                from src.dalton.access_tiers import classify_owner_tiers_batch

                ws_owner_tiers = dict(
                    await classify_owner_tiers_batch(non_self_ws_owners, conn=uconn)
                )

            if is_dalton_supabase_auth_enabled is _default_is_dalton_supabase_auth_enabled:
                non_self_ws_rows = [
                    {
                        "workspace_id": str(u.get("workspace_id") or ""),
                        "user_id": str(u.get("workspace_owner_id") or ""),
                        "status": str(u.get("workspace_status") or ""),
                        "config": u.get("workspace_config"),
                    }
                    for u in unit_rows
                    if u.get("workspace_id")
                    and str(u.get("workspace_status") or "") != "deleted"
                    and str(u.get("workspace_owner_id") or "").strip()
                    and str(u.get("workspace_owner_id") or "").strip() != str(user_id)
                ]
                ws_write_map = await can_write_bound_unit_workspaces_batch(
                    non_self_ws_rows,
                    str(user_id),
                    is_pm=is_pm,
                    conn=uconn,
                    owner_tiers=ws_owner_tiers,
                )

        for u in unit_rows:
            u_id = str(u.get("unit_id"))
            owner = str(u.get("owner_user_id") or "")
            created_by_uid = str(u.get("created_by_user_id") or owner)
            lead_uid = str(u.get("lead_analyst_user_id") or owner)
            ws_id = str(u.get("workspace_id") or "") or None
            ws_status = str(u.get("workspace_status") or "")
            raw_ws_owner = str(u.get("workspace_owner_id") or "")
            ws_active = bool(ws_id and raw_ws_owner and ws_status != "deleted")
            ws_owner = raw_ws_owner if ws_active else ""
            web_level = "hidden"

            if user_id is None:
                readable = True
                delegatable = False
                auto_tracked = False
                redacted_ws_id = ws_id
            elif ws_id is None:
                is_lead = lead_uid == str(user_id)
                if not is_lead and not eff_pm:
                    continue
                readable = is_lead
                delegatable = False
                auto_tracked = False
                redacted_ws_id = None
            else:
                if ws_active and ws_owner == str(user_id):
                    readable = True
                    delegatable = True
                    web_level = "rw"
                    redacted_ws_id = ws_id
                elif ws_active and ws_id in colleague_access and colleague_access[ws_id][0]:
                    readable = True
                    delegatable = colleague_access[ws_id][1]
                    web_level = web_levels.get(ws_id, "ro")
                    redacted_ws_id = ws_id
                else:
                    can_see_unreadable = (
                        lead_uid == str(user_id)
                        or eff_pm
                        or (not ws_active and created_by_uid == str(user_id))
                    )
                    if not can_see_unreadable:
                        continue
                    readable = False
                    delegatable = False
                    redacted_ws_id = None
                auto_tracked = bool(
                    readable and redacted_ws_id and redacted_ws_id in own_auto_ws
                )

            ws_owner_tier = ws_owner_tiers.get(ws_owner) if ws_owner else None
            actor_has_ws_write: bool | None = None
            if ws_id is not None and ws_active and user_id is not None:
                if ws_owner == str(user_id):
                    actor_has_ws_write = True
                elif supabase_auth and ws_write_map is not None:
                    actor_has_ws_write = ws_write_map.get(ws_id, False)

            flags = _compute_unit_action_flags(
                user_id=user_id,
                lead_uid=lead_uid,
                created_by_uid=created_by_uid,
                ws_id=ws_id,
                ws_active=ws_active,
                ws_owner=ws_owner,
                readable=readable,
                web_level=web_level,
                is_pm=is_pm,
                supabase_auth_enabled=supabase_auth,
                actor_has_ws_write=actor_has_ws_write,
                workspace_owner_tier=ws_owner_tier,
            )

            entry: dict[str, Any] = {
                "unit_id": u_id,
                "kind": u.get("kind"),
                "slug": u.get("slug"),
                "display_name": u.get("display_name"),
                "owner_user_id": owner,
                "created_by_user_id": created_by_uid,
                "lead_analyst_user_id": lead_uid,
                "bound": bool(ws_id),
                "workspace_id": redacted_ws_id,
                "readable": readable,
                "delegatable": delegatable,
                "auto_tracked": auto_tracked,
                "workspace_share_rw": (ws_id is None or web_level == "rw"),
                **flags,
            }
            if ws_id is not None and not readable:
                from src.dalton.coverage.units import clear_unreadable_unit_identity

                clear_unreadable_unit_identity(entry)
            if readable and redacted_ws_id and ws_owner:
                entry["workspace_owner_id"] = ws_owner
            if u.get("kind") == "company":
                entry["ticker"] = u.get("ticker")
            elif not readable:
                # Never leak doc-derived members/scope/reason/valid_until without Web read access.
                entry["members"] = []
            else:
                members_out: list[dict[str, Any]] = []
                for m in members_by_sector.get(u_id, []):
                    company_row = company_ws_by_unit.get(
                        str(m.get("company_unit_id") or "")
                    )
                    members_out.append(
                        render_sector_member(
                            m,
                            company_row,
                            member_workspace_visible(company_row, user_id, colleague_access),
                        )
                    )
                entry["members"] = members_out
            out.append(entry)
    return out


async def list_unclassified_workspaces(user_id: str) -> list[dict[str, Any]]:
    """The caller's non-deleted workspaces bound to no active unit, not reserved (Home/stand-in), and not duplicate."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT workspace_id::text AS workspace_id, user_id, name,
                       description, updated_at, status, config
                FROM workspaces
                WHERE status <> 'deleted' AND user_id = %s
                ORDER BY name ASC
                """,
                (user_id,),
            )
            ws_rows = [dict(r) for r in await cur.fetchall()]
            units_by_ws = await load_active_units_by_workspace(
                [str(r["workspace_id"]) for r in ws_rows], conn=conn
            )
    out: list[dict[str, Any]] = []
    for ws in ws_rows:
        ws_id = str(ws["workspace_id"])
        if (
            is_reserved_row(ws)
            or bool(ws.get("is_home"))
            or str(ws.get("status") or "") == "unbound"
        ):
            continue
        if is_historical_duplicate_workspace(ws):
            continue
        if ws_id in units_by_ws:
            continue
        updated = ws.get("updated_at")
        out.append(
            {
                "workspace_id": ws_id,
                "name": ws.get("name"),
                "description": ws.get("description"),
                "updated_at": (
                    updated.isoformat()
                    if isinstance(updated, datetime)
                    else (str(updated) if updated else None)
                ),
            }
        )
    return out


async def list_bindable_workspaces(user_id: str) -> list[dict[str, Any]]:
    """Workspaces the caller can write on web (own U colleague ``web_access='rw'``), unbound and non-duplicate."""
    own_unbound = await list_unclassified_workspaces(user_id)
    out: list[dict[str, Any]] = [
        {
            **ws,
            "owner_user_id": user_id,
            "owner_name": None,
            "owner_email": None,
            "is_own": True,
        }
        for ws in own_unbound
    ]
    _, _, colleague_rows = await _load_colleague_workspace_state(user_id)
    # _load_colleague_workspace_state / sharing.py (load_accessible_workspaces_for_user)
    # already filters out reserved Home/stand-in workspaces from colleague rows.
    rw_colleague = [
        cws
        for cws in colleague_rows
        if cws.get("web_access") == "rw"
        and not bool(cws.get("is_historical_duplicate"))
        and str(cws.get("status") or "") not in ("deleted", "unbound")
        and not bool(cws.get("is_home"))
    ]
    if not rw_colleague:
        return out

    bound_map = await load_active_units_by_workspace(
        [str(cws["workspace_id"]) for cws in rw_colleague]
    )
    for cws in rw_colleague:
        ws_id = str(cws["workspace_id"])
        if ws_id in bound_map:
            continue
        out.append(
            {
                "workspace_id": ws_id,
                "name": cws.get("name"),
                "description": cws.get("description"),
                "updated_at": cws.get("updated_at"),
                "owner_user_id": cws.get("owner_user_id"),
                "owner_name": cws.get("owner_name"),
                "owner_email": cws.get("owner_email"),
                "is_own": False,
            }
        )
    return out


def _summarize_thesis(thesis: dict[str, Any] | None) -> dict[str, Any] | None:
    """Thesis summary shared by the sector and company detail views."""
    if not thesis:
        return None
    content = thesis.get("content") or {}
    house_view = content.get("house_view") or {}
    pillars = [
        {
            "id": p.get("id"),
            "claim": p.get("claim"),
            "priority": p.get("priority"),
            "status": p.get("status"),
        }
        for p in (content.get("pillars") or [])
        if isinstance(p, dict)
    ]
    return {
        "version_no": thesis.get("version_no"),
        "one_liner": content.get("one_liner"),
        "aggregate_status": content.get("aggregate_status"),
        "stance": house_view.get("stance") or content.get("stance"),
        "conviction": house_view.get("conviction") or content.get("conviction"),
        "pillars": pillars,
    }


def _summarize_sector_docs(
    docs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Light, display-only summaries of a sector unit's knowledge documents."""
    sector = docs.get("sector")
    drivers = docs.get("drivers")
    agenda = docs.get("agenda")

    sector_out: dict[str, Any] | None = None
    if sector:
        content = sector.get("content") or {}
        scope = content.get("scope") or {}
        sector_out = {
            "version_no": sector.get("version_no"),
            "name": content.get("name"),
            "name_zh": content.get("name_zh"),
            "definition": content.get("definition"),
            "boundaries": content.get("boundaries"),
            "one_liner": content.get("one_liner"),
            "coverage_stage": content.get("coverage_stage"),
            "coverage_kind": content.get("kind"),
            "wiki_slug": content.get("wiki_slug"),
            "research_tz": content.get("research_tz"),
            "regions": scope.get("regions") or [],
            "markets": scope.get("markets") or [],
        }

    thesis_out = _summarize_thesis(docs.get("thesis"))

    drivers_out: dict[str, Any] | None = None
    if drivers:
        content = drivers.get("content") or {}

        def _indicator_out(ind: dict[str, Any]) -> dict[str, Any]:
            # drivers schema 的指标真实字段（codex p4 r1 #4）：指标名之外，
            # 最新读数（value/unit/period/as_of）才是页面要展示的内容；
            # readings 历史与 probe 是预检/滚动归档的运行时数据，不进摘要。
            latest = ind.get("latest")
            return {
                "id": ind.get("id"),
                "name": ind.get("name"),
                "frequency": ind.get("frequency"),
                "good_direction": ind.get("good_direction"),
                "latest": (
                    {
                        "value": latest.get("value"),
                        "unit": latest.get("unit"),
                        "period": latest.get("period"),
                        "as_of": latest.get("as_of"),
                    }
                    if isinstance(latest, dict)
                    else None
                ),
            }

        drivers_out = {
            "version_no": drivers.get("version_no"),
            "as_of": content.get("as_of"),
            "drivers": [
                {
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "kind": d.get("kind"),
                    "updated_at": d.get("updated_at"),
                    "leading_indicators": [
                        _indicator_out(ind)
                        for ind in (d.get("leading_indicators") or [])
                        if isinstance(ind, dict)
                    ],
                }
                for d in (content.get("drivers") or [])
                if isinstance(d, dict)
            ],
        }

    return {
        "sector": sector_out,
        "thesis": thesis_out,
        "drivers": drivers_out,
        "agenda_version_no": (agenda or {}).get("version_no"),
    }


def _extract_agenda_todos(agenda: dict[str, Any] | None) -> dict[str, Any]:
    """Open agenda items of one unit: open questions, due validations, events.

    Tolerant like the board's extraction — malformed entries are skipped, no
    scoring; this view answers "what is pending", the board ranks it.
    """
    if not agenda:
        return {"questions": [], "validations": [], "events": []}
    content = agenda.get("content") or {}

    questions: list[dict[str, Any]] = []
    for q in content.get("open_questions") or []:
        # 只排除真正已关闭的状态（codex p4 r1 #3）：waiting_pm 的问题还没
        # 解决，转交 PM 后仍要留在待办里并带等待标识。schema 状态枚举是
        # open/waiting_pm/resolved/dropped；investigating 是旧数据容忍。
        if not isinstance(q, dict) or q.get("status") in ("resolved", "dropped"):
            continue
        questions.append(
            {
                "id": str(q.get("id") or "Q-001"),
                "question": q.get("question") or q.get("title") or "",
                "why": q.get("why") or "",
                "value": str(q.get("value") or "medium").lower(),
                "urgency": str(q.get("urgency") or "this_week").lower(),
                "origin": q.get("origin"),
                "waiting_pm": bool(q.get("waiting_pm")) or q.get("status") == "waiting_pm",
            }
        )

    validations: list[dict[str, Any]] = []
    for v in (content.get("next_validations") or []) + (
        content.get("validation_queue") or []
    ):
        if not isinstance(v, dict) or v.get("status") not in (None, "open", "pending"):
            continue
        validations.append(
            {
                "id": str(v.get("id") or "V1"),
                "what": str(v.get("what") or v.get("metric") or v.get("title") or ""),
                "due": v.get("due") or v.get("target_date") or v.get("due_date"),
            }
        )

    events: list[dict[str, Any]] = []
    catalysts = ((content.get("watch") or {}).get("events") or []) + (
        content.get("catalysts") or []
    )
    for cat in catalysts:
        if isinstance(cat, dict) and cat.get("status") in (
            None, "upcoming", "watching", "active", "confirmed",
        ):
            events.append(
                {
                    "type": cat.get("type") or cat.get("kind") or "catalyst",
                    "title": cat.get("plan") or cat.get("title") or cat.get("event") or "",
                    "date": cat.get("date") or cat.get("expected_date"),
                    "date_status": cat.get("date_status") or "confirmed",
                }
            )

    return {"questions": questions, "validations": validations, "events": events}


def _extract_re_review_questions(agenda: dict[str, Any] | None) -> list[dict[str, Any]]:
    """A company unit's open re-review items (P2 linkage, origin ``sector``)."""
    if not agenda:
        return []
    out: list[dict[str, Any]] = []
    for q in (agenda.get("content") or {}).get("open_questions") or []:
        if not isinstance(q, dict) or q.get("origin") != "sector":
            continue
        if q.get("status") not in (None, "open", "waiting_pm"):
            continue
        out.append(
            {
                "id": str(q.get("id") or "Q-001"),
                "question": q.get("question") or "",
                "value": str(q.get("value") or "medium").lower(),
                "urgency": str(q.get("urgency") or "this_week").lower(),
                "status": q.get("status") or "open",
                "source_ref": q.get("source_ref"),
            }
        )
    return out


async def _latest_valid_docs(
    cur: Any, workspace_id: str, doc_keys: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Latest schema-valid knowledge docs of one workspace, by doc key."""
    await cur.execute(
        """
        SELECT DISTINCT ON (doc_key) doc_key, version_no, content
        FROM dalton_knowledge_versions
        WHERE workspace_id = %s::uuid
          AND schema_valid = TRUE
          AND doc_key = ANY(%s::text[])
        ORDER BY doc_key, version_no DESC
        """,
        (workspace_id, list(doc_keys)),
    )
    return {
        str(r["doc_key"]): dict(r)
        for r in await cur.fetchall()
    }


async def build_units_listing(user_id: str) -> dict[str, Any]:
    """One-call coverage listing for the HTTP API: units + unclassified + bindable workspaces."""
    uid = str(user_id or "").strip()
    autos_by_user: dict[str, list[dict[str, Any]]] = {}
    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                autos_by_user = await load_dalton_automations_by_user(cur, uid)
    except Exception as exc:
        logger.warning("[DALTON_UNITS_VIEW] automations preload failed: %s", exc)
    units = await build_units_view(uid, autos_by_user)
    unclassified = await list_unclassified_workspaces(uid)
    bindable_workspaces = await list_bindable_workspaces(uid)
    return {
        "enabled": True,
        "units": units,
        "unclassified": unclassified,
        "bindable_workspaces": bindable_workspaces,
    }


class UnitNotFound(LookupError):
    """The unit does not exist, is archived, or is invisible to the caller."""


async def build_unit_detail(user_id: str, unit_id: str) -> dict[str, Any]:
    """One coverage unit's read-only detail (P4 coverage view).

    Bound workspace readability strictly follows ``workspaces.user_id == uid`` or
    ``colleague_access[ws_id][0]`` (never ``unit.owner_user_id``). Unreadable bound
    units raise :class:`UnitNotFound` (404) before reading any workspace documents.
    """
    from src.dalton.database.sector_members import (
        fetch_members_for_sectors,
        list_members_for_company,
    )

    uid = str(user_id or "").strip()
    supabase_auth = _is_multiuser_auth_enabled()

    async with _repeatable_read_conn() as snap_conn:
        is_pm = await _is_coverage_pm(uid, conn=snap_conn)
        eff_pm = is_pm and supabase_auth
        unit = await get_coverage_unit(unit_id, conn=snap_conn)
        if unit is None or str(unit.get("status") or "") != "active":
            raise UnitNotFound(f"coverage unit {unit_id} not found")
        ws_id = str(unit.get("workspace_id") or "") or None
        owner = str(unit.get("owner_user_id") or "")
        created_by_uid = str(unit.get("created_by_user_id") or owner)
        lead_uid = str(unit.get("lead_analyst_user_id") or owner)
        is_lead = lead_uid == uid

        colleague_access, web_levels, _ = await _load_colleague_workspace_state(
            uid, conn=snap_conn
        )
        ws_owner = ""
        ws_active = False
        ws_owner_tier: str | None = None
        web_level = "hidden"
        actor_has_ws_write = False
        ws_row: dict[str, Any] | None = None
        async with snap_conn.cursor(row_factory=dict_row) as cur:
            if ws_id is None:
                if not is_lead and not eff_pm:
                    raise UnitNotFound(f"coverage unit {unit_id} not found")
                readable = True
                delegatable = False
            else:
                await cur.execute(
                    """
                    SELECT workspace_id::text AS workspace_id, user_id, status, config
                    FROM workspaces
                    WHERE workspace_id = %s::uuid
                    """,
                    (ws_id,),
                )
                fetched = await cur.fetchone()
                ws_row = dict(fetched) if fetched else None
                if ws_row is not None and str(ws_row.get("status") or "") != "deleted":
                    ws_owner = str(ws_row.get("user_id") or "")
                    ws_active = bool(ws_owner)
                if ws_active and ws_owner == uid:
                    readable = True
                    delegatable = True
                    web_level = "rw"
                    actor_has_ws_write = True
                elif ws_active and ws_id in colleague_access and colleague_access[ws_id][0]:
                    readable = True
                    delegatable = colleague_access[ws_id][1]
                    web_level = web_levels.get(ws_id, "ro")
                else:
                    raise UnitNotFound(f"coverage unit {unit_id} not found")
                if supabase_auth and ws_active and ws_owner and ws_owner != uid:
                    from src.dalton.access_tiers import classify_owner_tier

                    ws_owner_tier = await classify_owner_tier(ws_owner, conn=snap_conn)
                if not actor_has_ws_write and ws_row is not None and ws_active:
                    actor_has_ws_write = await can_write_bound_unit_workspace(
                        ws_row,
                        uid,
                        is_pm=is_pm,
                        conn=snap_conn,
                        owner_tiers=(
                            {ws_owner: ws_owner_tier}
                            if ws_owner_tier is not None
                            else None
                        ),
                    )

            auto_tracked = False
            if ws_id and readable:
                try:
                    autos = await load_dalton_automations_by_user(cur, uid)
                    auto_tracked = ws_id in {
                        str(a.get("workspace_id") or "")
                        for a in autos.get(uid, [])
                        if a.get("workspace_id")
                    }
                except Exception as exc:
                    logger.warning("[DALTON_UNITS_VIEW] automations preload failed: %s", exc)

            flags = _compute_unit_action_flags(
                user_id=uid,
                lead_uid=lead_uid,
                created_by_uid=created_by_uid,
                ws_id=ws_id,
                ws_active=ws_active,
                ws_owner=ws_owner,
                readable=readable,
                web_level=web_level,
                is_pm=is_pm,
                supabase_auth_enabled=supabase_auth,
                actor_has_ws_write=actor_has_ws_write,
                workspace_owner_tier=ws_owner_tier,
            )
            entry: dict[str, Any] = {
                "unit_id": str(unit.get("unit_id")),
                "kind": unit.get("kind"),
                "slug": unit.get("slug"),
                "display_name": unit.get("display_name"),
                "owner_user_id": owner,
                "created_by_user_id": created_by_uid,
                "lead_analyst_user_id": lead_uid,
                "bound": bool(ws_id),
                "workspace_id": ws_id,
                "readable": readable,
                "delegatable": delegatable,
                "auto_tracked": auto_tracked,
                "workspace_share_rw": (ws_id is None or web_level == "rw"),
                **flags,
            }
            if ws_id is not None and not readable:
                from src.dalton.coverage.units import clear_unreadable_unit_identity

                clear_unreadable_unit_identity(entry)
            if ws_id and ws_owner:
                entry["workspace_owner_id"] = ws_owner
            if str(unit.get("kind")) == "company":
                entry["ticker"] = unit.get("ticker")

            docs: dict[str, dict[str, Any]] = {}
            if ws_id:
                docs = await _latest_valid_docs(
                    cur, ws_id, ("sector", "thesis", "drivers", "agenda", "company")
                )

            if str(unit.get("kind")) == "sector":
                members: list[dict[str, Any]] = []
                member_rows = await fetch_members_for_sectors(cur, [entry["unit_id"]])
                company_ws_by_unit = await _load_company_bindings(cur, member_rows)
                for m in member_rows:
                    company_row = company_ws_by_unit.get(str(m.get("company_unit_id") or ""))
                    members.append(
                        render_sector_member(
                            m,
                            company_row,
                            member_workspace_visible(company_row, uid, colleague_access),
                        )
                    )

                evidence_gaps: list[dict[str, Any]] = []
                ledger_gaps: list[dict[str, Any]] = []
                pending_total = 0
                if is_lead:
                    from src.dalton.database.cross_unit_requests import list_pending_for_sector

                    try:
                        pending_rows, pending_total = await list_pending_for_sector(
                            uid, entry["unit_id"], ws_id, limit=50, conn=snap_conn
                        )
                    except Exception as exc:
                        logger.warning("[DALTON_UNITS_VIEW] ledger read failed: %s", exc)
                        pending_rows = []
                    candidate_targets = {
                        str(r.get("target_workspace_id") or "").strip()
                        for r in pending_rows
                        if str(r.get("target_workspace_id") or "").strip()
                    }
                    readable_targets = await _batch_readable_workspaces(
                        cur,
                        snap_conn,
                        uid,
                        candidate_targets,
                        colleague_access,
                        own_readable_ws=ws_id,
                    )
                    for r in pending_rows:
                        raw_target_ws = str(r.get("target_workspace_id") or "").strip() or None
                        safe_target_ws = (
                            raw_target_ws
                            if raw_target_ws and raw_target_ws in readable_targets
                            else None
                        )
                        if (
                            str(r.get("kind") or "") == "research_request"
                            and str(r.get("status") or "") in ("open", "dispatched")
                        ):
                            evidence_gaps.append(
                                {
                                    "request_id": r.get("request_id"),
                                    "question": r.get("question"),
                                    "evidence_needed": r.get("evidence_needed") or [],
                                    "completion_condition": r.get("completion_condition"),
                                    "target_workspace_id": safe_target_ws,
                                    "status": r.get("status"),
                                    "created_at": _iso_or_none(r.get("created_at")),
                                }
                            )
                        else:
                            ledger_gaps.append(
                                {
                                    "request_id": r.get("request_id"),
                                    "kind": r.get("kind"),
                                    "question": r.get("question"),
                                    "target_workspace_id": safe_target_ws,
                                    "status": r.get("status"),
                                    "created_at": _iso_or_none(r.get("created_at")),
                                    "updated_at": _iso_or_none(r.get("updated_at")),
                                }
                            )

                entry["sector"] = {
                    "docs": _summarize_sector_docs(docs) if ws_id else None,
                    "agenda_todos": (
                        _extract_agenda_todos(docs.get("agenda")) if ws_id else None
                    ),
                    "members": members,
                    "evidence_gaps": evidence_gaps,
                    "ledger_gaps": ledger_gaps,
                    "pending_total": pending_total,
                }
                return entry

            sectors_out: list[dict[str, Any]] = []
            member_rows = await list_members_for_company(entry["unit_id"], conn=snap_conn)
            sector_unit_ids = sorted(
                {
                    str(m.get("sector_unit_id"))
                    for m in member_rows
                    if m.get("sector_unit_id")
                }
            )
            sector_units: dict[str, dict[str, Any]] = {}
            if sector_unit_ids:
                await cur.execute(
                    """
                    SELECT u.unit_id::text AS unit_id, u.kind, u.owner_user_id,
                           COALESCE(u.lead_analyst_user_id, u.owner_user_id) AS lead_analyst_user_id,
                           u.slug, u.display_name, u.workspace_id::text AS workspace_id,
                           u.status, w.user_id AS workspace_owner_id
                    FROM dalton_coverage_units u
                    LEFT JOIN workspaces w ON w.workspace_id = u.workspace_id AND w.status <> 'deleted'
                    WHERE u.unit_id = ANY(%s::uuid[])
                    """,
                    (sector_unit_ids,),
                )
                sector_units = {
                    str(r["unit_id"]): dict(r) for r in await cur.fetchall()
                }

            for m in member_rows:
                s_unit = sector_units.get(str(m.get("sector_unit_id") or ""))
                if s_unit is None or str(s_unit.get("status") or "") != "active":
                    continue
                s_ws_owner = str(s_unit.get("workspace_owner_id") or "")
                s_ws = str(s_unit.get("workspace_id") or "") or None
                if s_ws is not None and s_ws_owner == uid:
                    s_readable, s_delegatable = True, True
                else:
                    s_access = colleague_access.get(s_ws or "")
                    if s_access is None or not s_access[0]:
                        continue
                    s_readable, s_delegatable = s_access
                sectors_out.append(
                    {
                        "unit_id": str(s_unit.get("unit_id")),
                        "slug": s_unit.get("slug"),
                        "display_name": s_unit.get("display_name"),
                        "member_id": m.get("member_id"),
                        "scope": m.get("scope") or "",
                        "valid_until": (
                            m["valid_until"].isoformat() if m.get("valid_until") else None
                        ),
                        "readable": s_readable,
                        "delegatable": s_delegatable,
                        "workspace_id": s_ws if s_readable else None,
                    }
                )

            re_review_items = _extract_re_review_questions(docs.get("agenda")) if ws_id else []
            ref_ws_candidates: set[str] = set()
            for item in re_review_items:
                ref = item.get("source_ref")
                if isinstance(ref, dict):
                    src_ws = str(ref.get("workspace_id") or "").strip()
                    if src_ws:
                        ref_ws_candidates.add(src_ws)
            readable_ref_ws = (
                await _batch_readable_workspaces(
                    cur,
                    snap_conn,
                    uid,
                    ref_ws_candidates,
                    colleague_access,
                    own_readable_ws=ws_id,
                )
                if ref_ws_candidates
                else set()
            )
            for item in re_review_items:
                ref = item.get("source_ref")
                if isinstance(ref, dict):
                    src_ws = str(ref.get("workspace_id") or "").strip()
                    if not src_ws or src_ws not in readable_ref_ws:
                        item.pop("source_ref", None)
                else:
                    item.pop("source_ref", None)

            entry["company"] = {
                "docs": ({"thesis": _summarize_thesis(docs.get("thesis"))} if ws_id else None),
                "sectors": sectors_out,
                "re_review": re_review_items,
            }
            return entry


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None
