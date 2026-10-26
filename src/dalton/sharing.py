"""Dalton team read-only sharing permissions and team workspace listing.

Active only when ``is_dalton_supabase_auth_enabled()`` is True and the caller is
an allowlisted team member (``DALTON_ALLOWED_EMAILS``). Write paths continue to
use ``require_workspace_owner`` and ``require_thread_owner`` unchanged.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.auth import _verified_token_identity
from src.dalton.invites import get_team_member_emails, is_team_member
from src.dalton.settings import (
    is_dalton_supabase_auth_enabled,
)
from src.server.database import conversation as conv_db
from src.server.database import user as user_db
from src.server.database import workspace as ws_db
from src.server.database.home_workspace import is_home_row
from src.server.database.pool import get_db_connection
from src.server.utils import api as auth_api

logger = logging.getLogger(__name__)


def _display_name(name: str | None, email: str | None) -> str:
    """Resolve display name from user row name or email local-part."""
    cleaned_name = (name or "").strip()
    if cleaned_name:
        return cleaned_name
    cleaned_email = (email or "").strip()
    if "@" in cleaned_email:
        return cleaned_email.split("@", 1)[0]
    return cleaned_email or "Teammate"


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


async def require_workspace_reader(
    workspace_id: str | dict[str, Any] | None,
    user_id: str | None,
    *,
    workspace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Verify caller can read the workspace (owner, or allowlisted team member when Dalton auth is on).

    When ``is_dalton_supabase_auth_enabled()`` is False, enforces strict owner check
    identical to ``require_workspace_owner``.
    When True, allows any allowlisted team member to read non-deleted, non-Home
    research workspaces while still allowing the owner to read their own Home.
    """
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
        return ws

    owner_id = str(ws.get("user_id") or "").strip()
    if not owner_id:
        raise HTTPException(status_code=403, detail="Forbidden")

    await require_team_reader(uid)

    if owner_id != uid:
        # Personal Home workspaces and deleted/unbound workspaces are never shared with colleagues.
        if (
            is_home_row(ws)
            or bool(ws.get("is_home"))
            or ws.get("status") in ("unbound", "deleted")
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        # Resource owner must also be an allowlisted team member.
        await require_team_reader(owner_id)

    return ws


async def require_thread_reader(
    thread_id: str,
    user_id: str | None,
    *,
    conn: Any = None,
    owner_id: str | None = None,
    workspace_id: str | dict[str, Any] | None = None,
) -> str:
    """Verify caller can read the thread (owner, or allowlisted team member when Dalton auth is on).

    When ``is_dalton_supabase_auth_enabled()`` is False, delegates to ``require_thread_owner``.
    When True, resolves the thread's ``workspace_id`` and validates via ``require_workspace_reader``
    (checking non-Home, non-deleted, and resource owner in ``DALTON_ALLOWED_EMAILS``).
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

    ws = await require_workspace_reader(resolved_workspace_id, uid)
    cleaned_owner = str(ws.get("user_id") or owner_id or "").strip()
    if not cleaned_owner:
        raise HTTPException(status_code=403, detail="Forbidden")

    return cleaned_owner


async def list_team_workspaces(current_user_id: str) -> list[dict[str, Any]]:
    """Return colleagues' research workspaces grouped by user, excluding caller's own and Home workspaces."""
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
                    u.avatar_url AS owner_avatar_url
                FROM workspaces w
                JOIN users u ON w.user_id = u.user_id
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
