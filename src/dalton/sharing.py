"""Dalton workspace sharing permissions (Web UI & Chief of Staff channels) and team workspace listing.

Active only when ``is_dalton_supabase_auth_enabled()`` is True and the caller is
an allowlisted team member (``DALTON_ALLOWED_EMAILS``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.auth import _verified_token_identity
from src.dalton.invites import get_team_member_emails, is_team_member
from src.dalton.settings import (
    get_dalton_settings,
    is_dalton_supabase_auth_enabled,
)
from src.server.database import conversation as conv_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database.home_workspace import is_home_row
from src.server.database.pool import get_db_connection
from src.server.utils import api as auth_api

logger = logging.getLogger(__name__)

VALID_ACCESS_LEVELS: frozenset[str] = frozenset({"rw", "ro", "hidden"})
VALID_CHANNELS: frozenset[str] = frozenset({"web", "cos"})
VALID_NEEDS: frozenset[str] = frozenset({"read", "write"})

DEFAULT_COS_ACCESS: str = "ro"
DEFAULT_WEB_ACCESS: str = "hidden"


def _display_name(name: str | None, email: str | None) -> str:
    """Resolve display name from user row name or email local-part."""
    cleaned_name = (name or "").strip()
    if cleaned_name:
        return cleaned_name
    cleaned_email = (email or "").strip()
    if "@" in cleaned_email:
        return cleaned_email.split("@", 1)[0]
    return cleaned_email or "Teammate"


def _normalize_access_level(value: Any, default: str) -> str:
    cleaned = str(value or "").strip().lower()
    if cleaned in VALID_ACCESS_LEVELS:
        return cleaned
    return default


def can_edit_workspace_sharing(
    workspace: Mapping[str, Any] | None,
    user_id: str | None,
) -> bool:
    """Return True when ``user_id`` is the workspace owner or a configured PM (and workspace is not Home)."""
    if not workspace:
        return False
    uid = (user_id or "").strip()
    if not uid:
        return False
    if (
        is_home_row(workspace)
        or bool(workspace.get("is_home"))
        or workspace.get("status") in ("unbound", "deleted")
    ):
        return False
    owner_id = str(workspace.get("user_id") or "").strip()
    if owner_id and uid == owner_id:
        return True
    pm_ids = get_dalton_settings().pm_user_ids
    return uid in pm_ids


async def get_workspace_sharing(
    workspace_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Fetch ``(cos_access, web_access, updated_by, updated_at)`` from ``dalton_workspace_sharing``.

    Missing row returns the default policy: ``cos_access='ro'``, ``web_access='hidden'``.
    """
    cleaned_ws = str(workspace_id or "").strip()
    default_row: dict[str, Any] = {
        "workspace_id": cleaned_ws,
        "cos_access": DEFAULT_COS_ACCESS,
        "web_access": DEFAULT_WEB_ACCESS,
        "updated_by": None,
        "updated_at": None,
    }
    if not cleaned_ws:
        return default_row

    try:
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT workspace_id, cos_access, web_access, updated_by, updated_at
                    FROM dalton_workspace_sharing
                    WHERE workspace_id = %s
                    """,
                    (cleaned_ws,),
                )
                row = await cur.fetchone()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to read dalton_workspace_sharing for workspace_id=%s: %s",
            cleaned_ws,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="Workspace sharing settings unavailable",
        ) from exc

    if not row:
        return default_row

    updated_at = row.get("updated_at")
    return {
        "workspace_id": str(row.get("workspace_id") or cleaned_ws),
        "cos_access": _normalize_access_level(
            row.get("cos_access"), DEFAULT_COS_ACCESS
        ),
        "web_access": _normalize_access_level(
            row.get("web_access"), DEFAULT_WEB_ACCESS
        ),
        "updated_by": row.get("updated_by"),
        "updated_at": (
            updated_at.isoformat()
            if hasattr(updated_at, "isoformat")
            else updated_at
        ),
    }


async def upsert_workspace_sharing(
    workspace_id: str,
    *,
    cos_access: str | None = None,
    web_access: str | None = None,
    updated_by: str,
) -> dict[str, Any]:
    """Insert or partially update ``dalton_workspace_sharing`` for ``workspace_id``."""
    cleaned_ws = str(workspace_id or "").strip()
    if not cleaned_ws:
        raise HTTPException(status_code=400, detail="workspace_id is required")

    if cos_access is None and web_access is None:
        raise HTTPException(
            status_code=400,
            detail="At least one of cos_access or web_access must be provided",
        )

    cleaned_cos: str | None = None
    if cos_access is not None:
        cleaned_cos = str(cos_access).strip().lower()
        if cleaned_cos not in VALID_ACCESS_LEVELS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid cos_access '{cos_access}'; must be one of rw, ro, hidden",
            )

    cleaned_web: str | None = None
    if web_access is not None:
        cleaned_web = str(web_access).strip().lower()
        if cleaned_web not in VALID_ACCESS_LEVELS:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid web_access '{web_access}'; must be one of rw, ro, hidden",
            )

    insert_cos = cleaned_cos if cleaned_cos is not None else DEFAULT_COS_ACCESS
    insert_web = cleaned_web if cleaned_web is not None else DEFAULT_WEB_ACCESS
    cleaned_updated_by = (updated_by or "").strip() or None

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO dalton_workspace_sharing (
                    workspace_id, cos_access, web_access, updated_by, updated_at
                )
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (workspace_id) DO UPDATE SET
                    cos_access = COALESCE(%s, dalton_workspace_sharing.cos_access),
                    web_access = COALESCE(%s, dalton_workspace_sharing.web_access),
                    updated_by = EXCLUDED.updated_by,
                    updated_at = NOW()
                RETURNING workspace_id, cos_access, web_access, updated_by, updated_at
                """,
                (
                    cleaned_ws,
                    insert_cos,
                    insert_web,
                    cleaned_updated_by,
                    cleaned_cos,
                    cleaned_web,
                ),
            )
            row = await cur.fetchone()
            await conn.commit()

    if not row:
        return {
            "workspace_id": cleaned_ws,
            "cos_access": insert_cos,
            "web_access": insert_web,
            "updated_by": cleaned_updated_by,
            "updated_at": None,
        }

    updated_at = row.get("updated_at")
    return {
        "workspace_id": str(row.get("workspace_id") or cleaned_ws),
        "cos_access": _normalize_access_level(
            row.get("cos_access"), insert_cos
        ),
        "web_access": _normalize_access_level(
            row.get("web_access"), insert_web
        ),
        "updated_by": row.get("updated_by"),
        "updated_at": (
            updated_at.isoformat()
            if hasattr(updated_at, "isoformat")
            else updated_at
        ),
    }


async def require_team_reader(user_id: str | None) -> str:
    """Verify caller is authenticated and in DALTON_ALLOWED_EMAILS when Dalton auth is on.

    Raises:
        HTTPException(401): Missing or empty user_id.
        HTTPException(403): Caller is not in DALTON_ALLOWED_EMAILS.
    """
    uid = (user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not is_dalton_supabase_auth_enabled():
        return uid

    verified = _verified_token_identity.get()
    if verified is not None and verified[0] == uid:
        verified_email = (verified[1] or "").strip().lower()
        if await is_team_member(verified_email):
            return uid
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        user_row = await user_db.get_user(uid)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to verify team reader user %s: %s", uid, exc)
        raise HTTPException(
            status_code=503, detail="Team reader verification unavailable"
        ) from exc

    if not user_row:
        raise HTTPException(status_code=403, detail="Forbidden")

    email = str(user_row.get("email") or "").strip().lower()
    if not email or not await is_team_member(email):
        raise HTTPException(status_code=403, detail="Forbidden")

    return uid


async def require_workspace_access(
    workspace_id: str | dict[str, Any] | None,
    user_id: str | None,
    channel: str = "web",
    need: str = "read",
    *,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify caller has ``need`` ('read' | 'write') access to workspace on ``channel`` ('web' | 'cos').

    - Owner always has full read/write access (including their own Home workspace).
    - When ``is_dalton_supabase_auth_enabled()`` is False, enforces strict owner check.
    - Non-owner team members:
      - Home or deleted/unbound workspaces -> HTTP 403.
      - ``hidden`` on ``channel`` -> HTTP 404 (does not leak existence).
      - ``ro`` on ``channel`` -> allows ``need='read'``, rejects ``need='write'`` with HTTP 403.
      - ``rw`` on ``channel`` -> allows both ``need='read'`` and ``need='write'``.
    """
    cleaned_channel = str(channel or "web").strip().lower()
    if cleaned_channel not in VALID_CHANNELS:
        raise ValueError(f"Unsupported channel: {channel!r}")
    cleaned_need = str(need or "read").strip().lower()
    if cleaned_need not in VALID_NEEDS:
        raise ValueError(f"Unsupported need: {need!r}")

    uid = (user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    ws: dict[str, Any] | None
    if isinstance(workspace_id, dict):
        ws = workspace_id
    elif workspace is not None:
        ws = workspace
    elif workspace_id:
        ws_id_str = str(workspace_id).strip()
        if not ws_id_str or ws_id_str.lower() == "home":
            raise HTTPException(status_code=403, detail="Forbidden")
        try:
            ws = await ws_db.get_workspace(ws_id_str)
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Failed to fetch workspace %s: %s", ws_id_str, exc)
            raise HTTPException(
                status_code=503, detail="Workspace access verification unavailable"
            ) from exc
    else:
        ws = None

    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if not is_dalton_supabase_auth_enabled():
        auth_api.require_workspace_owner(ws, user_id=uid)
        ws["_caller_web_access"] = "owner"
        ws["_caller_cos_access"] = "owner"
        return ws

    owner_id = str(ws.get("user_id") or "").strip()
    if not owner_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    await require_team_reader(uid)

    if owner_id == uid:
        ws["_caller_web_access"] = "owner"
        ws["_caller_cos_access"] = "owner"
        return ws

    # Personal Home workspaces and deleted/unbound workspaces are never shared with colleagues.
    if (
        is_home_row(ws)
        or bool(ws.get("is_home"))
        or ws.get("status") in ("unbound", "deleted")
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Resource owner must also be an allowlisted team member.
    await require_team_reader(owner_id)

    ws_id = str(ws.get("workspace_id") or "").strip()
    sharing = await get_workspace_sharing(ws_id)
    access_field = "cos_access" if cleaned_channel == "cos" else "web_access"
    default_level = (
        DEFAULT_COS_ACCESS if cleaned_channel == "cos" else DEFAULT_WEB_ACCESS
    )
    level = _normalize_access_level(sharing.get(access_field), default_level)

    if level == "hidden":
        raise HTTPException(status_code=404, detail="Workspace not found")
    if level == "ro" and cleaned_need == "write":
        raise HTTPException(status_code=403, detail="Forbidden")

    ws[f"_caller_{cleaned_channel}_access"] = level
    return ws


async def require_thread_access(
    thread_id: str,
    user_id: str | None,
    channel: str = "web",
    need: str = "read",
    *,
    conn: Any = None,
    owner_id: str | None = None,
    workspace_id: str | dict[str, Any] | None = None,
) -> str:
    """Verify caller has ``need`` ('read' | 'write') access to ``thread_id`` on ``channel`` ('web' | 'cos').

    Returns the resolved owner ``user_id`` of the thread.
    """
    uid = (user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    if not is_dalton_supabase_auth_enabled():
        if owner_id is not None:
            if owner_id != uid:
                raise HTTPException(status_code=403, detail="Forbidden")
            return owner_id
        await auth_api.require_thread_owner(thread_id, uid, conn=conn)
        return uid

    await require_team_reader(uid)

    resolved_workspace_id = workspace_id
    if resolved_workspace_id is None:
        meta = await conv_db.get_thread_auth_meta(thread_id, conn=conn)
        if not meta or not meta.get("workspace_id"):
            raise HTTPException(status_code=404, detail="Thread not found")
        resolved_workspace_id = meta["workspace_id"]

    ws = await require_workspace_access(
        resolved_workspace_id,
        uid,
        channel=channel,
        need=need,
    )
    cleaned_owner = str(ws.get("user_id") or owner_id or "").strip()
    if not cleaned_owner:
        raise HTTPException(status_code=403, detail="Forbidden")

    return cleaned_owner


async def require_workspace_reader(
    workspace_id: str | dict[str, Any] | None,
    user_id: str | None,
    *,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify caller can read the workspace on the web channel."""
    return await require_workspace_access(
        workspace_id,
        user_id,
        channel="web",
        need="read",
        workspace=workspace,
    )


async def require_thread_reader(
    thread_id: str,
    user_id: str | None,
    *,
    conn: Any = None,
    owner_id: str | None = None,
    workspace_id: str | dict[str, Any] | None = None,
) -> str:
    """Verify caller can read the thread on the web channel."""
    return await require_thread_access(
        thread_id,
        user_id,
        channel="web",
        need="read",
        conn=conn,
        owner_id=owner_id,
        workspace_id=workspace_id,
    )


async def list_team_workspaces(current_user_id: str) -> list[dict[str, Any]]:
    """Return colleagues' visible web workspaces grouped by user, excluding caller's own, Home, and hidden workspaces."""
    if not is_dalton_supabase_auth_enabled():
        return []

    uid = await require_team_reader(current_user_id)
    allowed_emails = sorted(await get_team_member_emails())

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    w.workspace_id,
                    w.user_id,
                    w.name,
                    w.description,
                    w.status,
                    w.created_at,
                    w.updated_at,
                    w.last_activity_at,
                    u.email AS owner_email,
                    u.name AS owner_name,
                    u.avatar_url AS owner_avatar_url,
                    COALESCE(s.web_access, 'hidden') AS web_access,
                    COALESCE(s.cos_access, 'ro') AS cos_access
                FROM workspaces w
                JOIN users u ON w.user_id = u.user_id
                LEFT JOIN dalton_workspace_sharing s ON s.workspace_id = w.workspace_id
                WHERE w.user_id <> %s
                  AND w.status <> 'deleted'
                  AND w.status <> 'unbound'
                  AND (
                      LOWER(BTRIM(u.email)) = ANY(%s)
                      OR EXISTS (
                          SELECT 1 FROM dalton_members dm
                          WHERE dm.email = LOWER(BTRIM(u.email))
                      )
                  )
                  AND COALESCE(s.web_access, 'hidden') IN ('ro', 'rw')
                ORDER BY
                    COALESCE(NULLIF(BTRIM(u.name), ''), LOWER(BTRIM(u.email))) ASC,
                    COALESCE(w.last_activity_at, w.updated_at) DESC,
                    w.updated_at DESC
                """,
                (uid, allowed_emails),
            )
            rows = [dict(r) for r in await cur.fetchall()]

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        if is_home_row(row) or bool(row.get("is_home")):
            continue
        raw_web_access = row.get("web_access")
        if raw_web_access == "hidden":
            continue
        web_access = _normalize_access_level(raw_web_access, "ro")
        if web_access == "hidden":
            continue
        cos_access = _normalize_access_level(
            row.get("cos_access"), DEFAULT_COS_ACCESS
        )

        owner_uid = str(row["user_id"])
        owner_email = str(row.get("owner_email") or "").strip()
        if not await is_team_member(owner_email):
            continue
        owner_display = _display_name(row.get("owner_name"), owner_email)
        member = grouped.get(owner_uid)
        if member is None:
            member = {
                "user_id": owner_uid,
                "email": owner_email,
                "display_name": owner_display,
                "name": owner_display,
                "avatar_url": row.get("owner_avatar_url"),
                "workspaces": [],
            }
            grouped[owner_uid] = member

        updated_at = row.get("updated_at")
        last_activity_at = row.get("last_activity_at")
        created_at = row.get("created_at")
        member["workspaces"].append(
            {
                "workspace_id": str(row["workspace_id"]),
                "user_id": owner_uid,
                "owner_name": owner_display,
                "owner_email": owner_email,
                "name": row.get("name") or "Workspace",
                "description": row.get("description"),
                "status": row.get("status") or "stopped",
                "is_home": False,
                "web_access": web_access,
                "cos_access": cos_access,
                "access": web_access,
                "updated_at": (
                    updated_at.isoformat()
                    if hasattr(updated_at, "isoformat")
                    else updated_at
                ),
                "last_activity_at": (
                    last_activity_at.isoformat()
                    if hasattr(last_activity_at, "isoformat")
                    else last_activity_at
                ),
                "created_at": (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else created_at
                ),
            }
        )

    return list(grouped.values())


async def list_colleague_cos_workspaces(
    current_user_id: str,
) -> list[dict[str, Any]]:
    """Return colleagues' Chief of Staff visible workspaces (``cos_access`` in ``('ro', 'rw')``)."""
    if not is_dalton_supabase_auth_enabled():
        return []

    try:
        uid = await require_team_reader(current_user_id)
    except HTTPException:
        return []

    allowed_emails = sorted(await get_team_member_emails())

    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    w.workspace_id,
                    w.user_id,
                    w.name,
                    w.description,
                    w.status,
                    w.created_at,
                    w.updated_at,
                    w.last_activity_at,
                    u.email AS owner_email,
                    u.name AS owner_name,
                    COALESCE(s.cos_access, 'ro') AS cos_access,
                    COALESCE(s.web_access, 'hidden') AS web_access
                FROM workspaces w
                JOIN users u ON w.user_id = u.user_id
                LEFT JOIN dalton_workspace_sharing s ON s.workspace_id = w.workspace_id
                WHERE w.user_id <> %s
                  AND w.status <> 'deleted'
                  AND w.status <> 'unbound'
                  AND (
                      LOWER(BTRIM(u.email)) = ANY(%s)
                      OR EXISTS (
                          SELECT 1 FROM dalton_members dm
                          WHERE dm.email = LOWER(BTRIM(u.email))
                      )
                  )
                  AND COALESCE(s.cos_access, 'ro') IN ('ro', 'rw')
                ORDER BY
                    COALESCE(NULLIF(BTRIM(u.name), ''), LOWER(BTRIM(u.email))) ASC,
                    COALESCE(w.last_activity_at, w.updated_at) DESC,
                    w.updated_at DESC
                """,
                (uid, allowed_emails),
            )
            rows = [dict(r) for r in await cur.fetchall()]

    items: list[dict[str, Any]] = []
    for row in rows:
        if is_home_row(row) or bool(row.get("is_home")):
            continue
        cos_access = _normalize_access_level(
            row.get("cos_access"), DEFAULT_COS_ACCESS
        )
        if cos_access == "hidden":
            continue
        web_access = _normalize_access_level(
            row.get("web_access"), DEFAULT_WEB_ACCESS
        )
        owner_uid = str(row["user_id"])
        owner_email = str(row.get("owner_email") or "").strip()
        owner_display = _display_name(row.get("owner_name"), owner_email)
        updated_at = row.get("updated_at")
        last_activity_at = row.get("last_activity_at")
        created_at = row.get("created_at")
        items.append(
            {
                "workspace_id": str(row["workspace_id"]),
                "user_id": owner_uid,
                "owner_name": owner_display,
                "owner_email": owner_email,
                "name": row.get("name") or "Workspace",
                "description": row.get("description"),
                "status": row.get("status") or "stopped",
                "is_home": False,
                "cos_access": cos_access,
                "web_access": web_access,
                "updated_at": (
                    updated_at.isoformat()
                    if hasattr(updated_at, "isoformat")
                    else updated_at
                ),
                "last_activity_at": (
                    last_activity_at.isoformat()
                    if hasattr(last_activity_at, "isoformat")
                    else last_activity_at
                ),
                "created_at": (
                    created_at.isoformat()
                    if hasattr(created_at, "isoformat")
                    else created_at
                ),
            }
        )

    return items
