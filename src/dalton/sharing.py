"""Dalton workspace sharing permissions (Web UI & Chief of Staff channels) and team workspace listing.

Active only when ``is_dalton_supabase_auth_enabled()`` is True and the caller is
an allowlisted team member (``DALTON_ALLOWED_EMAILS``).

External members (``get_user_tier == 'external'``, roster-fixed) are fully
isolated: they see no colleague workspace on either channel, whatever the owner
shared, and their own workspaces are visible to admins (read-only) and nobody
else — the owner's sharing settings never apply to an external-owned workspace.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.access_tiers import classify_owner_tier, get_user_tier
from src.dalton.auth import _verified_token_identity
from src.dalton.invites import (
    get_team_member_emails,
    is_invite_pm,
    is_team_member,
)
from src.dalton.settings import (
    get_dalton_allowed_emails,
    get_dalton_settings,
    is_colleague_dispatch_enabled,
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

# Admins may look into an external member's workspace but never write it;
# the sharing settings of an external-owned workspace never take effect.
EXTERNAL_OWNED_ADMIN_WRITE_FORBIDDEN_DETAIL = "外部成员的工作区仅管理员可查看（只读）"

COLLEAGUE_ACCESS_REVOKED_CODE = "colleague_access_revoked"


class ColleagueAccessRevokedError(PermissionError):
    """Raised when a colleague's write access to a workspace has been revoked."""

    def __init__(
        self,
        message: str = "Workspace write access has been revoked by the owner.",
    ) -> None:
        super().__init__(message)
        self.code = COLLEAGUE_ACCESS_REVOKED_CODE
        self.message = message


def _classify_owner_row_tier(owner_id: str | None, owner_email: Any) -> str:
    """Classify a workspace owner from the row's joined email, with exactly the
    ``classify_owner_tier`` rules (PM -> admin, allowlisted email -> internal,
    everything else -> external); a missing email or any failing roster read
    is ``unknown`` so list loops skip the row instead of applying an isolation
    rule to a guess."""
    uid = str(owner_id or "").strip()
    if not uid:
        return "unknown"
    try:
        if is_invite_pm(uid):
            return "admin"
        email = str(owner_email or "").strip().lower()
        if not email:
            return "unknown"
        if email in get_dalton_allowed_emails():
            return "internal"
        return "external"
    except Exception:
        # A single unclassifiable row must not fail the whole listing.
        return "unknown"


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
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    WITH ins AS (
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
                    ),
                    locked_ws AS (
                        SELECT w.workspace_id
                        FROM workspaces w
                        JOIN ins ON w.workspace_id = ins.workspace_id
                        FOR UPDATE
                    )
                    SELECT ins.workspace_id, ins.cos_access, ins.web_access, ins.updated_by, ins.updated_at
                    FROM ins
                    LEFT JOIN locked_ws ON TRUE
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


async def get_user_cos_sharing(
    user_id: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Fetch user-level Chief of Staff sharing setting from ``dalton_user_sharing``.

    Defaults to ``cos_rw_enabled=True`` (``cos_access='rw'``) when no row exists.
    """
    cleaned_uid = str(user_id or "").strip()
    default_row: dict[str, Any] = {
        "user_id": cleaned_uid,
        "cos_rw_enabled": True,
        "cos_access": "rw",
        "updated_at": None,
    }
    if not cleaned_uid:
        return default_row

    try:
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT user_id, cos_rw_enabled, updated_at
                    FROM dalton_user_sharing
                    WHERE user_id = %s
                    """,
                    (cleaned_uid,),
                )
                row = await cur.fetchone()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Failed to read dalton_user_sharing for user_id=%s: %s",
            cleaned_uid,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="User sharing settings unavailable",
        ) from exc

    if not row:
        return default_row

    cos_rw = bool(row.get("cos_rw_enabled", True))
    updated_at = row.get("updated_at")
    return {
        "user_id": str(row.get("user_id") or cleaned_uid),
        "cos_rw_enabled": cos_rw,
        "cos_access": "rw" if cos_rw else "hidden",
        "updated_at": (
            updated_at.isoformat()
            if hasattr(updated_at, "isoformat")
            else updated_at
        ),
    }


async def upsert_user_cos_sharing(
    user_id: str,
    *,
    cos_rw_enabled: bool,
    conn: Any = None,
) -> dict[str, Any]:
    """Upsert ``dalton_user_sharing`` while locking the user's workspaces ``FOR UPDATE``."""
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        raise HTTPException(status_code=400, detail="user_id is required")

    cos_rw = bool(cos_rw_enabled)
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT workspace_id
                    FROM workspaces
                    WHERE user_id = %s
                      AND status != 'deleted'
                    FOR UPDATE
                    """,
                    (cleaned_uid,),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_user_sharing (
                        user_id, cos_rw_enabled, updated_at
                    )
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        cos_rw_enabled = EXCLUDED.cos_rw_enabled,
                        updated_at = NOW()
                    RETURNING user_id, cos_rw_enabled, updated_at
                    """,
                    (cleaned_uid, cos_rw),
                )
                row = await cur.fetchone()

    if not row:
        return {
            "user_id": cleaned_uid,
            "cos_rw_enabled": cos_rw,
            "cos_access": "rw" if cos_rw else "hidden",
            "updated_at": None,
        }

    saved_rw = bool(row.get("cos_rw_enabled", cos_rw))
    updated_at = row.get("updated_at")
    return {
        "user_id": str(row.get("user_id") or cleaned_uid),
        "cos_rw_enabled": saved_rw,
        "cos_access": "rw" if saved_rw else "hidden",
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
    conn: Any = None,
) -> dict[str, Any]:
    """Verify caller has ``need`` ('read' | 'write') access to workspace on ``channel`` ('web' | 'cos').

    - Owner always has full read/write access (including their own Home workspace).
    - When ``is_dalton_supabase_auth_enabled()`` is False, enforces strict owner check.
    - Non-owner team members:
      - Home or deleted/unbound workspaces -> HTTP 403.
      - ``hidden`` on ``channel`` -> HTTP 404 (does not leak existence).
      - ``ro`` on ``channel`` -> allows ``need='read'``, rejects ``need='write'`` with HTTP 403.
      - ``rw`` on ``channel`` -> allows both ``need='read'`` and ``need='write'``.
    - External-member isolation (roster-fixed tiers, checked before the
      Home/status checks, the owner membership check, and any sharing row):
      a non-owner external caller gets HTTP 404 on every colleague workspace;
      an external-owned workspace is HTTP 404 for every non-owner except
      admins, who get read-only access (HTTP 403 on ``need='write'``); an
      unresolvable owner tier (``unknown``) is HTTP 404 for everyone.
    Owners keep full access regardless of their own tier.
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
        if ws.get("status") == "deleted":
            raise HTTPException(status_code=404, detail="Workspace not found")
        ws["_caller_web_access"] = "owner"
        ws["_caller_cos_access"] = "owner"
        return ws

    # External-member isolation, decided before anything else — the Home
    # 403, the owner membership check, the sharing row — so no caller state
    # or owner-tier failure can leak a colleague workspace's existence.
    caller_tier = await get_user_tier(uid)
    if caller_tier == "external":
        raise HTTPException(status_code=404, detail="Workspace not found")

    owner_tier = await classify_owner_tier(owner_id)
    if owner_tier == "unknown":
        raise HTTPException(status_code=404, detail="Workspace not found")
    if owner_tier == "external":
        if (
            is_home_row(ws)
            or bool(ws.get("is_home"))
            or ws.get("status") in ("unbound", "deleted")
        ):
            raise HTTPException(status_code=404, detail="Workspace not found")
        if cleaned_channel == "cos":
            raise HTTPException(status_code=404, detail="Workspace not found")

    # Personal Home workspaces and deleted/unbound workspaces are never shared with colleagues.
    if (
        is_home_row(ws)
        or bool(ws.get("is_home"))
        or ws.get("status") in ("unbound", "deleted")
    ):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Resource owner must also be an allowlisted team member.
    try:
        await require_team_reader(owner_id)
    except HTTPException:
        if owner_tier == "external":
            raise HTTPException(status_code=404, detail="Workspace not found")
        raise

    ws_id = str(ws.get("workspace_id") or "").strip()
    if cleaned_channel == "cos":
        if is_colleague_dispatch_enabled():
            user_sharing = await get_user_cos_sharing(owner_id, conn=conn)
            level = (
                "rw"
                if bool(user_sharing.get("cos_rw_enabled", True))
                else "hidden"
            )
        else:
            sharing = await get_workspace_sharing(ws_id, conn=conn)
            level = _normalize_access_level(
                sharing.get("cos_access"), DEFAULT_COS_ACCESS
            )
    else:
        sharing = await get_workspace_sharing(ws_id, conn=conn)
        level = _normalize_access_level(
            sharing.get("web_access"), DEFAULT_WEB_ACCESS
        )

    if level == "hidden":
        raise HTTPException(status_code=404, detail="Workspace not found")
    if level == "ro" and cleaned_need == "write":
        raise HTTPException(status_code=403, detail="Forbidden")

    ws[f"_caller_{cleaned_channel}_access"] = level
    return ws


verify_workspace_access = require_workspace_access


async def require_thread_access(
    thread_id: str,
    user_id: str | None,
    channel: str = "web",
    need: str = "read",
    *,
    conn: Any = None,
    owner_id: str | None = None,
    workspace_owner_id: str | None = None,
    workspace_id: str | dict[str, Any] | None = None,
    is_tail_turn: bool = False,
) -> str:
    """Verify caller has ``need`` ('read' | 'write') access to ``thread_id`` on ``channel`` ('web' | 'cos').

    Distinguishes three actors:
    1. Thread owner A (`caller == thread_owner_id`): always allowed to read (even if B
       later sets workspace to `hidden` or soft-deletes it); write on a colleague's
       workspace checks workspace write access.
    2. Workspace owner B (`caller == workspace_owner_id != thread_owner_id`): allowed to
       read (`need='read'`), rejected on write (`need='write'`, 403).
    3. Other colleague C: allowed to read if workspace allows read on `channel`,
       rejected on write (`need='write'`, 403/404).
    """
    uid = (user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")

    cleaned_need = str(need or "read").strip().lower()
    if cleaned_need not in VALID_NEEDS:
        raise ValueError(f"Unsupported need: {need!r}")
    cleaned_channel = str(channel or "web").strip().lower()

    if not is_dalton_supabase_auth_enabled():
        if owner_id is not None:
            if owner_id != uid:
                raise HTTPException(status_code=403, detail="Forbidden")
            return owner_id
        await auth_api.require_thread_owner(thread_id, uid, conn=conn)
        return uid

    await require_team_reader(uid)

    meta: dict[str, Any] | None = None
    resolved_workspace_id = workspace_id
    if resolved_workspace_id is None or (
        owner_id is None and workspace_owner_id is None
    ):
        meta = await conv_db.get_thread_auth_meta(thread_id, conn=conn)
        if resolved_workspace_id is None:
            if not meta or not meta.get("workspace_id"):
                raise HTTPException(status_code=404, detail="Thread not found")
            resolved_workspace_id = meta["workspace_id"]

    thread_owner = str(
        owner_id
        or (meta or {}).get("thread_user_id")
        or (meta or {}).get("user_id")
        or ""
    ).strip()
    ws_owner = str(
        workspace_owner_id
        or (meta or {}).get("workspace_owner_id")
        or (
            resolved_workspace_id.get("user_id")
            if isinstance(resolved_workspace_id, dict)
            else ""
        )
        or ""
    ).strip()

    is_colleague_thread = bool(
        thread_owner
        and ws_owner
        and thread_owner != ws_owner
        and is_colleague_dispatch_enabled()
    )

    # 1. Colleague thread owner A (`uid == thread_owner != ws_owner`):
    #    Read access is exempt from workspace revocation/deletion only on the web channel;
    #    Chief of Staff ('cos') still requires workspace access on 'cos'.
    if is_colleague_thread and uid == thread_owner:
        if await get_user_tier(uid) == "external":
            raise HTTPException(status_code=404, detail="Thread not found")
        if cleaned_need == "read" and cleaned_channel == "web":
            return thread_owner
        if is_tail_turn:
            ws_obj = (
                resolved_workspace_id
                if isinstance(resolved_workspace_id, dict)
                else await ws_db.get_workspace(str(resolved_workspace_id))
            )
            if not ws_obj or ws_obj.get("status") in ("unbound", "deleted"):
                raise HTTPException(status_code=403, detail="Forbidden")
            return thread_owner
        if cleaned_need == "read":
            await require_workspace_access(
                resolved_workspace_id,
                uid,
                channel=channel,
                need="read",
                conn=conn,
            )
            return thread_owner
        try:
            await require_workspace_access(
                resolved_workspace_id,
                uid,
                channel=channel,
                need="write",
                conn=conn,
            )
        except HTTPException as exc:
            if exc.status_code not in (403, 404):
                raise
            ws_obj = (
                resolved_workspace_id
                if isinstance(resolved_workspace_id, dict)
                else await ws_db.get_workspace(str(resolved_workspace_id))
            )
            if not ws_obj or ws_obj.get("status") in ("unbound", "deleted"):
                raise HTTPException(status_code=403, detail="Forbidden") from exc
            raise HTTPException(
                status_code=403,
                detail={
                    "code": COLLEAGUE_ACCESS_REVOKED_CODE,
                    "message": "工作区所有者已收回访问权限",
                },
            ) from exc
        return thread_owner

    # 2. Workspace owner B viewing a colleague's thread inside B's workspace
    if (
        ws_owner
        and uid == ws_owner
        and thread_owner
        and thread_owner != uid
    ):
        if cleaned_need == "read":
            return thread_owner
        raise HTTPException(
            status_code=403,
            detail="该对话由同事发起，工作区所有者仅可查看",
        )

    # 3. Other colleague C (or when ws_owner was not yet resolved)
    ws = await require_workspace_access(
        resolved_workspace_id,
        uid,
        channel=channel,
        need=cleaned_need,
        conn=conn,
    )
    resolved_ws_owner = str(ws.get("user_id") or ws_owner or "").strip()
    resolved_thread_owner = thread_owner or resolved_ws_owner
    if not resolved_thread_owner:
        raise HTTPException(status_code=403, detail="Forbidden")

    if cleaned_need == "write" and uid != resolved_thread_owner:
        raise HTTPException(
            status_code=403,
            detail="该对话由同事发起，仅创建者可继续提问",
        )

    return resolved_thread_owner


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
    workspace_owner_id: str | None = None,
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
        workspace_owner_id=workspace_owner_id,
        workspace_id=workspace_id,
    )


async def build_dispatched_by_meta(
    user_id: str,
    workspace_owner_id: str | None = None,
    *,
    channel: str,
    name: str | None = None,
    email: str | None = None,
) -> dict[str, Any] | None:
    """Build ``metadata['dispatched_by']`` payload when ``user_id != workspace_owner_id``."""
    uid = str(user_id or "").strip()
    ws_owner = str(workspace_owner_id or "").strip()
    if not uid or (ws_owner and uid == ws_owner):
        return None
    resolved_name = name
    resolved_email = email
    if not resolved_name and not resolved_email:
        try:
            u_row = await user_db.get_user(uid)
            if u_row:
                resolved_name = u_row.get("name")
                resolved_email = u_row.get("email")
        except Exception:
            pass
    cleaned_channel = "cos" if str(channel).strip().lower() == "cos" else "web"
    return {
        "user_id": uid,
        "name": _display_name(resolved_name, resolved_email),
        "email": (resolved_email or "").strip() or None,
        "channel": cleaned_channel,
    }


def _extract_report_source_info(report_source: Any) -> tuple[str | None, str | None]:
    """Return ``(kind, id)`` from a ``ReportSource`` model or dict."""
    if report_source is None:
        return None, None
    if isinstance(report_source, Mapping):
        kind = str(report_source.get("kind") or "").strip().lower() or None
        item_id = (
            str(
                report_source.get("command_id")
                or report_source.get("task_id")
                or report_source.get("id")
                or ""
            ).strip()
            or None
        )
        return kind, item_id
    kind = str(getattr(report_source, "kind", "") or "").strip().lower() or None
    item_id = (
        str(
            getattr(report_source, "command_id", None)
            or getattr(report_source, "task_id", None)
            or getattr(report_source, "id", None)
            or ""
        ).strip()
        or None
    )
    return kind, item_id


def _fallback_thread_channel(thread_metadata: Mapping[str, Any] | None) -> str:
    if isinstance(thread_metadata, Mapping):
        inner = thread_metadata.get("metadata")
        meta = inner if isinstance(inner, Mapping) else thread_metadata
        disp = meta.get("dispatched_by")
        if isinstance(disp, Mapping):
            ch = str(disp.get("channel") or "").strip().lower()
            if ch in ("cos", "web"):
                return ch
    return "web"


def _normalize_report_source_kind(kind: str | None) -> str | None:
    if not kind:
        return None
    cleaned = str(kind).strip().lower()
    if cleaned in ("command", "command_report"):
        return "command"
    if cleaned in ("task", "task_report_back", "subagent"):
        return "task"
    return cleaned or None


async def resolve_turn_origin_channel(
    conn_or_pool: Any = None,
    *,
    conn: Any = None,
    thread_id: str | None = None,
    request: Any = None,
    thread_meta: Mapping[str, Any] | None = None,
    thread_metadata: Mapping[str, Any] | None = None,
    is_internal: bool = False,
    is_dispatch: bool = False,
    is_internal_dispatch: bool = False,
    request_origin: Any = None,
    query_type: str | None = None,
    report_source: Any = None,
    report_source_kind: str | None = None,
    report_source_id: str | None = None,
    retry_of_run_id: str | None = None,
    retry_run_id: str | None = None,
    wakeup_id: str | None = None,
    is_resume: bool = False,
    explicit_channel: str | None = None,
    raise_on_error: bool = False,
) -> str:
    """Resolve effective ``origin_channel`` ('cos' | 'web') by inheriting from the parent run."""
    if explicit_channel in ("cos", "web"):
        return explicit_channel
    # Support positional thread_id when called as resolve_turn_origin_channel(thread_id, ...)
    if isinstance(conn_or_pool, str) and thread_id is None:
        thread_id = conn_or_pool
        conn_or_pool = None
    db_conn = conn if conn is not None else conn_or_pool

    eff_meta = thread_metadata if thread_metadata is not None else thread_meta
    eff_internal_dispatch = bool(is_internal_dispatch or is_internal or is_dispatch)
    if request is not None:
        if request_origin is None and eff_internal_dispatch:
            request_origin = (
                getattr(request, "origin_thread_meta", None)
                or getattr(request, "origin", None)
                or getattr(request, "origin_home_thread_id", None)
                or getattr(request, "origin_dispatch_gen", None)
            )
        if query_type is None:
            query_type = getattr(request, "query_type", None)
        if report_source is None:
            report_source = getattr(request, "report_source", None)
        if retry_of_run_id is None:
            retry_of_run_id = getattr(request, "retry_of_run_id", None)
        if wakeup_id is None:
            wakeup_id = getattr(request, "wakeup_id", None)
            if wakeup_id is None:
                wakeup_payload = getattr(request, "wakeup", None)
                if isinstance(wakeup_payload, Mapping):
                    wakeup_id = wakeup_payload.get("wakeup_id") or wakeup_payload.get(
                        "id"
                    )
                    if wakeup_id is None:
                        notices = wakeup_payload.get("notices")
                        if isinstance(notices, (list, tuple)) and notices:
                            first_notice = notices[0]
                            if isinstance(first_notice, Mapping):
                                wakeup_id = first_notice.get(
                                    "wakeup_id"
                                ) or first_notice.get("id")
                            elif hasattr(first_notice, "wakeup_id"):
                                wakeup_id = getattr(first_notice, "wakeup_id", None)
        if not is_resume and getattr(request, "hitl_response", None):
            is_resume = True

    eff_retry_id = retry_of_run_id or retry_run_id
    rs_kind, rs_id = _extract_report_source_info(report_source)
    if report_source_kind:
        rs_kind = _normalize_report_source_kind(report_source_kind)
    else:
        rs_kind = _normalize_report_source_kind(rs_kind)
    if report_source_id:
        rs_id = str(report_source_id).strip() or None
    qtype = str(query_type or "").strip().lower()

    # 1. Direct Chief of Staff dispatch (server-authenticated internal dispatch only;
    # client-supplied ChatRequest.origin on a web request is never trusted)
    if (
        eff_internal_dispatch
        and bool(request_origin)
        and rs_kind is None
        and not wakeup_id
        and qtype not in ("system", "wakeup")
    ):
        return "cos"

    tid = str(thread_id or "").strip()

    try:
        # 2. Background command completion notice -> inherit from command_runs.run_id
        if rs_kind == "command" and rs_id and tid:
            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT r.metadata->>'origin_channel' AS origin_channel,
                               ct.metadata->'dispatched_by'->>'channel' AS thread_channel
                        FROM command_runs cr
                        LEFT JOIN conversation_responses r
                          ON r.conversation_response_id = cr.run_id
                        LEFT JOIN conversation_threads ct
                          ON ct.conversation_thread_id = cr.conversation_thread_id
                        WHERE cr.command_id = %s
                          AND cr.conversation_thread_id = %s
                        LIMIT 1
                        """,
                        (rs_id, tid),
                    )
                    row = await cur.fetchone()
            if row:
                ch = str(row.get("origin_channel") or "").strip().lower()
                if ch in ("cos", "web"):
                    return ch
                tch = str(row.get("thread_channel") or "").strip().lower()
                if tch in ("cos", "web"):
                    return tch
            return _fallback_thread_channel(eff_meta)

        # 3. Subagent completion report-back -> inherit from subagent_runs.parent_run_id
        if rs_kind == "task" and rs_id and tid:
            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT r.metadata->>'origin_channel' AS origin_channel,
                               ct.metadata->'dispatched_by'->>'channel' AS thread_channel
                        FROM subagent_runs sr
                        LEFT JOIN conversation_responses r
                          ON r.conversation_response_id = sr.parent_run_id
                        LEFT JOIN conversation_threads ct
                          ON ct.conversation_thread_id = sr.thread_id
                        WHERE sr.thread_id = %s
                          AND sr.task_id = %s
                        ORDER BY sr.started_at DESC
                        LIMIT 1
                        """,
                        (tid, rs_id),
                    )
                    row = await cur.fetchone()
            if row:
                ch = str(row.get("origin_channel") or "").strip().lower()
                if ch in ("cos", "web"):
                    return ch
                tch = str(row.get("thread_channel") or "").strip().lower()
                if tch in ("cos", "web"):
                    return tch
            return _fallback_thread_channel(eff_meta)

        # 4. Retry (/retry) -> inherit from retry_of_run_id
        if eff_retry_id:
            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT r.metadata->>'origin_channel' AS origin_channel,
                               ct.metadata->'dispatched_by'->>'channel' AS thread_channel
                        FROM conversation_responses r
                        LEFT JOIN conversation_threads ct
                          ON ct.conversation_thread_id = r.conversation_thread_id
                        WHERE r.conversation_response_id = %s
                        LIMIT 1
                        """,
                        (str(eff_retry_id),),
                    )
                    row = await cur.fetchone()
            if row:
                ch = str(row.get("origin_channel") or "").strip().lower()
                if ch in ("cos", "web"):
                    return ch
                tch = str(row.get("thread_channel") or "").strip().lower()
                if tch in ("cos", "web"):
                    return tch
            return _fallback_thread_channel(eff_meta)

        # 5. Wakeup turn -> inherit from wakeups.created_by_run_id (or nearest preceding run)
        if wakeup_id:
            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT COALESCE(
                                   r.metadata->>'origin_channel',
                                   (
                                       SELECT r_prev.metadata->>'origin_channel'
                                       FROM conversation_responses r_prev
                                       WHERE r_prev.conversation_thread_id = w.conversation_thread_id
                                         AND r_prev.created_at <= w.created_at
                                       ORDER BY r_prev.turn_index DESC, r_prev.created_at DESC
                                       LIMIT 1
                                   )
                               ) AS origin_channel,
                               ct.metadata->'dispatched_by'->>'channel' AS thread_channel
                        FROM wakeups w
                        LEFT JOIN conversation_responses r
                          ON r.conversation_response_id = w.created_by_run_id
                        LEFT JOIN conversation_threads ct
                          ON ct.conversation_thread_id = w.conversation_thread_id
                        WHERE w.wakeup_id = %s
                        LIMIT 1
                        """,
                        (str(wakeup_id),),
                    )
                    row = await cur.fetchone()
            if row:
                ch = str(row.get("origin_channel") or "").strip().lower()
                if ch in ("cos", "web"):
                    return ch
                tch = str(row.get("thread_channel") or "").strip().lower()
                if tch in ("cos", "web"):
                    return tch
            return _fallback_thread_channel(eff_meta)

        # 6. Interrupt resume (awaiting_resume / resume_feedback) -> inherit from latest interrupted run
        if (is_resume or qtype == "resume_feedback") and tid:
            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT r.metadata->>'origin_channel' AS origin_channel,
                               ct.metadata->'dispatched_by'->>'channel' AS thread_channel
                        FROM conversation_responses r
                        LEFT JOIN conversation_threads ct
                          ON ct.conversation_thread_id = r.conversation_thread_id
                        WHERE r.conversation_thread_id = %s
                          AND r.status = 'interrupted'
                        ORDER BY r.turn_index DESC, r.created_at DESC
                        LIMIT 1
                        """,
                        (tid,),
                    )
                    row = await cur.fetchone()
            if row:
                ch = str(row.get("origin_channel") or "").strip().lower()
                if ch in ("cos", "web"):
                    return ch
                tch = str(row.get("thread_channel") or "").strip().lower()
                if tch in ("cos", "web"):
                    return tch
            return _fallback_thread_channel(eff_meta)
    except Exception as exc:
        if raise_on_error:
            raise
        logger.warning(
            "Failed to resolve parent run origin_channel for thread_id=%s: %s",
            tid,
            exc,
        )
        return _fallback_thread_channel(eff_meta)

    # 7. Ordinary web message (no parent run)
    return "web"


def _extract_notice_wakeup_id(notice: Any) -> str | None:
    if isinstance(notice, Mapping):
        val = notice.get("wakeup_id") or notice.get("id")
    else:
        val = getattr(notice, "wakeup_id", None) or getattr(notice, "id", None)
    return str(val).strip() if val else None


def _filter_wakeup_message_lines(
    request: Any, total_notices: int, kept_indices: list[int]
) -> None:
    """Strip revoked notice lines from ``request.messages[-1].content`` when it is a ``<wakeup>`` block."""
    messages = getattr(request, "messages", None)
    if not isinstance(messages, list) or not messages:
        return
    last_msg = messages[-1]
    content = (
        last_msg.get("content")
        if isinstance(last_msg, dict)
        else getattr(last_msg, "content", None)
    )
    if not isinstance(content, str) or "<wakeup>" not in content:
        return
    try:
        from src.tools.wakeups.wording import (
            WAKE_CLOSE,
            WAKE_NOTE,
            WAKE_OPEN,
            WAKE_QUIET_HINT,
            WAKE_REPLY_LANGUAGE,
            render_wake_block,
        )

        raw_lines = content.splitlines()
        if (
            len(raw_lines) >= 3 + total_notices
            and raw_lines[0] == WAKE_OPEN
            and raw_lines[1] == WAKE_NOTE
            and raw_lines[-1] == WAKE_CLOSE
        ):
            # A started turn's footer is quiet hint, then the reply-language
            # line (R4/21), then the close; the footer's length varies with
            # which of those are present.
            footer_lines = 1  # WAKE_CLOSE
            if len(raw_lines) >= 2 and raw_lines[-2] == WAKE_REPLY_LANGUAGE:
                footer_lines += 1
            started_turn = False
            if raw_lines[-1 - footer_lines] == WAKE_QUIET_HINT:
                footer_lines += 1
                started_turn = True
            body_end = -footer_lines if footer_lines > 1 else -1
            notice_lines = raw_lines[2:body_end]
            if len(notice_lines) == total_notices:
                kept_lines = [notice_lines[i] for i in kept_indices]
                new_content = render_wake_block(
                    kept_lines, started_turn=started_turn
                )
                if isinstance(last_msg, dict):
                    last_msg["content"] = new_content
                else:
                    last_msg.content = new_content
    except Exception:
        pass


async def terminate_revoked_wakeup(
    thread_id: str | None,
    wakeup_id: str | None,
) -> None:
    """Terminate a wakeup whose origin channel was revoked (`end_wakeup(..., 'disabled')`)."""
    tid = str(thread_id or "").strip()
    wid = str(wakeup_id or "").strip()
    if not tid or not wid:
        return
    from src.server.services.wakeups.cancel import end_wakeup

    try:
        await end_wakeup(tid, wid, "disabled")
    except RuntimeError as exc:
        if "Conversation database pool is not open" in str(exc):
            return
        raise


def extract_wakeup_notice_ids(request: Any) -> list[str]:
    """Return all wakeup IDs present in ``request.wakeup.notices``."""
    if request is None:
        return []
    wakeup_payload = getattr(request, "wakeup", None)
    if not isinstance(wakeup_payload, Mapping):
        return []
    notices = wakeup_payload.get("notices")
    if not isinstance(notices, (list, tuple)):
        return []
    wids: list[str] = []
    for notice in notices:
        wid = _extract_notice_wakeup_id(notice)
        if wid:
            wids.append(wid)
    return wids


async def is_wakeup_notice_revoked(
    *,
    thread_id: str | None,
    wakeup_id: str | None,
    user_id: str | None,
    wakeup_row: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> bool:
    """Return True when ``wakeup_id`` belongs to a colleague thread whose origin channel write access has been revoked."""
    if not is_dalton_supabase_auth_enabled():
        return False
    tid = str(thread_id or "").strip()
    wid = str(wakeup_id or "").strip()
    uid = str(user_id or "").strip()
    if not tid or not wid or not uid:
        return False

    from src.server.database import conversation as conv_db

    thread = await conv_db.get_thread_by_id(tid)
    if not isinstance(thread, Mapping):
        return False

    ws_id = str(thread.get("workspace_id") or "").strip()
    if not ws_id:
        return False
    thread_meta = (
        thread.get("metadata")
        if isinstance(thread.get("metadata"), Mapping)
        else None
    )
    dispatched_by = (
        thread_meta.get("dispatched_by")
        if isinstance(thread_meta, Mapping)
        and isinstance(thread_meta.get("dispatched_by"), Mapping)
        else None
    )

    ws = await ws_db.get_workspace(ws_id)

    ws_owner_id = str((ws or {}).get("user_id") or "").strip()
    is_colleague_thread = bool(
        (ws_owner_id and ws_owner_id != uid)
        or (ws is None and dispatched_by is not None)
    )
    if not is_colleague_thread:
        return False

    if not is_colleague_dispatch_enabled():
        return True

    ch = await resolve_turn_origin_channel(
        thread_id=tid,
        wakeup_id=wid,
        thread_meta=thread_meta,
        query_type="wakeup",
        conn=conn,
        raise_on_error=True,
    )
    try:
        await require_workspace_access(
            ws if isinstance(ws, dict) else ws_id,
            uid,
            channel=ch,
            need="write",
            workspace=ws if isinstance(ws, dict) else None,
            conn=conn,
        )
    except HTTPException as exc:
        if exc.status_code in (403, 404):
            return True
        raise
    except ColleagueAccessRevokedError:
        return True
    return False


async def end_revoked_wake_jobs(
    *,
    thread_id: str,
    jobs: list[dict[str, Any]],
    default_wakeup_id: str,
) -> None:
    """Terminate revoked wakeup notices among a wake claim's head and riders."""
    from src.server.database.wakeups import fire as fire_db
    from src.server.services.wakeups.cancel import end_wakeup

    ended_any = False
    for job in jobs:
        payload = job.get("payload") or {}
        wid = str(payload.get("wakeup_id") or "").strip()
        uid = str(payload.get("user_id") or "").strip()
        job_id = str(job.get("hook_outbox_id") or "").strip()
        if not wid:
            continue
        if job_id and not await fire_db.job_owed(job_id):
            ended_any = True
            continue
        try:
            revoked = await is_wakeup_notice_revoked(
                thread_id=thread_id,
                wakeup_id=wid,
                user_id=uid,
            )
        except RuntimeError as exc:
            if "Conversation database pool is not open" in str(exc):
                revoked = False
            else:
                raise
        if revoked:
            await end_wakeup(thread_id, wid, "disabled")
            ended_any = True
    if not ended_any and default_wakeup_id:
        await end_wakeup(thread_id, default_wakeup_id, "disabled")


async def filter_wakeup_notices_by_access(
    thread_id: str | None,
    user_id: str | None,
    workspace_id: str | dict[str, Any] | None,
    request: Any,
    *,
    thread_meta: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> str | None:
    """Filter merged ``request.wakeup.notices`` by per-notice ``origin_channel`` write access.

    Inspects every notice in ``request.wakeup.notices``, resolves its
    ``origin_channel`` from ``wakeups.created_by_run_id``, and checks write
    access on that channel via ``require_workspace_access``. Terminates and
    removes revoked notices in-place from ``request.wakeup`` (and strips their
    corresponding lines from the ``<wakeup>`` user message when present). Raises
    ``ColleagueAccessRevokedError`` only when all notices in the batch are
    revoked. Returns the effective ``origin_channel`` of the first surviving
    notice, or ``None`` when ``request`` carries no wakeup notices.
    """
    if request is None:
        return None
    wakeup_payload = getattr(request, "wakeup", None)
    if not isinstance(wakeup_payload, Mapping):
        return None
    notices = wakeup_payload.get("notices")
    if not isinstance(notices, (list, tuple)) or not notices:
        return None

    kept_notices: list[Any] = []
    kept_indices: list[int] = []
    kept_channels: list[str] = []

    for idx, notice in enumerate(notices):
        notice_wid = _extract_notice_wakeup_id(notice)
        ch = await resolve_turn_origin_channel(
            thread_id=thread_id,
            wakeup_id=notice_wid,
            thread_meta=thread_meta,
            query_type="wakeup",
            conn=conn,
        )
        try:
            await require_workspace_access(
                workspace_id,
                user_id,
                channel=ch,
                need="write",
                workspace=workspace_id if isinstance(workspace_id, dict) else None,
                conn=conn,
            )
        except HTTPException as exc:
            if exc.status_code in (403, 404):
                await terminate_revoked_wakeup(thread_id, notice_wid)
                continue
            raise
        except ColleagueAccessRevokedError:
            await terminate_revoked_wakeup(thread_id, notice_wid)
            continue
        kept_notices.append(notice)
        kept_indices.append(idx)
        kept_channels.append(ch)

    if not kept_notices:
        raise ColleagueAccessRevokedError()

    if len(kept_notices) != len(notices):
        updated_wakeup = dict(wakeup_payload)
        updated_wakeup["notices"] = kept_notices
        try:
            request.wakeup = updated_wakeup
        except Exception:
            if isinstance(wakeup_payload, dict):
                wakeup_payload["notices"] = kept_notices
        _filter_wakeup_message_lines(request, len(notices), kept_indices)

    return kept_channels[0]


async def verify_locked_wakeup_notices(
    conn: Any,
    *,
    thread_id: str,
    turn_user_id: str,
    workspace: dict[str, Any],
    wakeup_notice_ids: list[str] | tuple[str, ...],
    thread_meta: Mapping[str, Any] | None = None,
) -> None:
    """Re-verify every wakeup notice's ``origin_channel`` inside START under ``FOR SHARE OF w``.

    If any notice in ``wakeup_notice_ids`` fails the write check under the lock,
    terminates every revoked notice via ``terminate_revoked_wakeup`` and raises
    ``ColleagueAccessRevokedError`` so the batch does not execute with a revoked
    notice while surviving notices remain pending for re-delivery.
    """
    revoked_wids: list[str] = []
    for raw_wid in wakeup_notice_ids:
        wid = str(raw_wid or "").strip()
        if not wid:
            continue
        ch = await resolve_turn_origin_channel(
            thread_id=thread_id,
            wakeup_id=wid,
            thread_meta=thread_meta,
            query_type="wakeup",
            conn=conn,
        )
        try:
            await require_workspace_access(
                dict(workspace),
                turn_user_id,
                channel=ch,
                need="write",
                workspace=dict(workspace),
                conn=conn,
            )
        except HTTPException as exc:
            if exc.status_code in (403, 404):
                revoked_wids.append(wid)
                continue
            raise
        except ColleagueAccessRevokedError:
            revoked_wids.append(wid)
            continue

    if revoked_wids:
        for wid in revoked_wids:
            await terminate_revoked_wakeup(thread_id, wid)
        raise ColleagueAccessRevokedError()


async def resolve_colleague_turn_admission(
    *,
    user_id: str,
    workspace_id: str | None,
    thread_id: str | None,
    request: Any,
    thread_meta: Mapping[str, Any] | None,
    is_existing_colleague_thread: bool,
    is_internal: bool,
    is_dispatch: bool,
    requested_workspace_fn: Any,
) -> tuple[dict[str, Any] | None, str, bool]:
    """Admit a turn's workspace, treating a turn as a tool-trimmed tail turn ONLY when normal write access was revoked."""
    origin_channel = "web"
    is_retry = bool(getattr(request, "retry_of_run_id", None))
    is_verified_retry_tail = False
    if is_existing_colleague_thread and is_retry:
        is_verified_retry_tail = await verify_server_tail_turn(
            thread_id=thread_id,
            request=request,
            is_internal=is_internal,
            is_dispatch=is_dispatch,
        )

    if is_existing_colleague_thread or is_dispatch or is_internal:
        filtered_wakeup_channel = (
            await filter_wakeup_notices_by_access(
                thread_id,
                user_id,
                workspace_id,
                request,
                thread_meta=thread_meta,
            )
            if is_existing_colleague_thread and not is_verified_retry_tail
            else None
        )
        origin_channel = (
            filtered_wakeup_channel
            if filtered_wakeup_channel is not None
            else await resolve_turn_origin_channel(
                thread_id=thread_id,
                request=request,
                thread_meta=thread_meta,
                is_internal=is_internal,
                is_dispatch=is_dispatch,
            )
        )

    if origin_channel != "web" or is_existing_colleague_thread:
        try:
            workspace = await requested_workspace_fn(
                user_id,
                workspace_id,
                channel=origin_channel,
                is_tail_turn=False,
                is_existing_colleague_thread=is_existing_colleague_thread,
            )
            return workspace, origin_channel, False
        except ColleagueAccessRevokedError:
            if is_existing_colleague_thread and (
                is_verified_retry_tail
                or await verify_server_tail_turn(
                    thread_id=thread_id,
                    request=request,
                    is_internal=is_internal,
                    is_dispatch=is_dispatch,
                )
            ):
                workspace = await requested_workspace_fn(
                    user_id,
                    workspace_id,
                    channel=origin_channel,
                    is_tail_turn=True,
                    is_existing_colleague_thread=True,
                )
                return workspace, origin_channel, True
            raise

    workspace = await requested_workspace_fn(user_id, workspace_id)
    return workspace, origin_channel, False


async def verify_server_tail_turn(
    conn_or_pool: Any = None,
    *,
    conn: Any = None,
    thread_id: str | None = None,
    request: Any = None,
    is_internal: bool = False,
    is_dispatch: bool = False,
    is_internal_dispatch: bool = False,
    query_type: str | None = None,
    report_source: Any = None,
    report_source_kind: str | None = None,
    report_source_id: str | None = None,
    retry_of_run_id: str | None = None,
    retry_run_id: str | None = None,
) -> bool:
    """Verify from Postgres ledger whether a request is one of the 3 allowed post-revocation tail turns:
    (1) background command completion notice (`command_runs.run_id`);
    (2) background subagent completion report-back (`subagent_runs.parent_run_id`);
    (3) resumable failure (`worker_lost` / `RESUMABLE`) `/retry` (`retry_of_run_id`).
    Never trusts client-supplied flags.
    """
    if isinstance(conn_or_pool, str) and thread_id is None:
        thread_id = conn_or_pool
        conn_or_pool = None
    db_conn = conn if conn is not None else conn_or_pool

    tid = str(thread_id or "").strip()
    if not tid:
        return False

    eff_internal_dispatch = bool(is_internal_dispatch or is_internal or is_dispatch)
    if request is not None:
        if query_type is None:
            query_type = getattr(request, "query_type", None)
        if report_source is None:
            report_source = getattr(request, "report_source", None)
        if retry_of_run_id is None:
            retry_of_run_id = getattr(request, "retry_of_run_id", None)

    eff_retry_id = retry_of_run_id or retry_run_id
    rs_kind, rs_id = _extract_report_source_info(report_source)
    if report_source_kind:
        rs_kind = _normalize_report_source_kind(report_source_kind)
        if rs_id is None and report_source_id:
            rs_id = str(report_source_id).strip() or None
        # When called directly with explicit report_source_kind + report_source_id in unit tests,
        # treat as internal system report unless explicitly overridden.
        if query_type is None:
            query_type = "system"
        if not is_internal_dispatch and not is_internal and not is_dispatch and request is None:
            eff_internal_dispatch = True
    else:
        rs_kind = _normalize_report_source_kind(rs_kind)
        if report_source_id:
            rs_id = str(report_source_id).strip() or None
        if rs_kind in ("command", "task") and rs_id and request is None:
            if query_type is None:
                query_type = "system"
            if not is_internal_dispatch and not is_internal and not is_dispatch:
                eff_internal_dispatch = True
    qtype = str(query_type or "").strip().lower()

    try:
        # (1) Background command completion notice
        if eff_internal_dispatch and qtype == "system" and rs_kind == "command" and rs_id:
            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT 1 AS ok
                        FROM command_runs cr
                        JOIN conversation_responses r
                          ON r.conversation_response_id = cr.run_id
                        WHERE cr.command_id = %s
                          AND cr.conversation_thread_id = %s
                          AND r.conversation_thread_id = %s
                          AND cr.result_delivered_at IS NULL
                          AND (
                              cr.status IN ('completed', 'failed')
                              OR (cr.status = 'stopped' AND cr.stopped_by = 'user')
                          )
                        LIMIT 1
                        """,
                        (rs_id, tid, tid),
                    )
                    row = await cur.fetchone()
            return bool(row)

        # (2) Background subagent completion report-back
        if eff_internal_dispatch and qtype == "system" and rs_kind == "task" and rs_id:
            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT 1 AS ok
                        FROM subagent_runs sr
                        JOIN conversation_responses r
                          ON r.conversation_response_id = sr.parent_run_id
                        WHERE sr.thread_id = %s
                          AND sr.task_id = %s
                          AND r.conversation_thread_id = %s
                          AND sr.result_delivered_at IS NULL
                          AND sr.status IN ('completed', 'error')
                        LIMIT 1
                        """,
                        (tid, rs_id, tid),
                    )
                    row = await cur.fetchone()
            return bool(row)

        # (3) Resumable failure /retry
        if eff_retry_id:
            from src.server.services.report_back.notify_turn import is_resumable_failure

            async with get_db_connection(db_conn) as owned_conn:
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT conversation_response_id, status, errors, metadata
                        FROM conversation_responses
                        WHERE conversation_thread_id = %s
                        ORDER BY turn_index DESC, created_at DESC
                        LIMIT 1
                        """,
                        (tid,),
                    )
                    row = await cur.fetchone()
            if not row:
                return False
            if str(row.get("conversation_response_id") or "") != str(eff_retry_id):
                return False
            if str(row.get("status") or "") != "error":
                return False
            meta = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
            errors = row.get("errors") or meta.get("errors")
            return bool(is_resumable_failure(errors))
    except Exception as exc:
        logger.warning(
            "Failed to verify server tail turn for thread_id=%s: %s",
            tid,
            exc,
        )
        return False

    return False


resolve_verified_tail_turn = verify_server_tail_turn


async def list_team_workspaces(current_user_id: str) -> list[dict[str, Any]]:
    """Return colleagues' visible web workspaces grouped by user, excluding caller's own, Home, and hidden workspaces."""
    if not is_dalton_supabase_auth_enabled():
        return []

    uid = await require_team_reader(current_user_id)
    # Fully isolated environment: external members see no colleague workspace.
    caller_tier = await get_user_tier(uid)
    if caller_tier == "external":
        return []
    allowed_emails = sorted(await get_team_member_emails())

    dispatch_enabled = is_colleague_dispatch_enabled()
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if dispatch_enabled:
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
                        CASE
                            WHEN us.cos_rw_enabled IS FALSE THEN 'hidden'
                            ELSE 'rw'
                        END AS cos_access
                    FROM workspaces w
                    JOIN users u ON w.user_id = u.user_id
                    LEFT JOIN dalton_workspace_sharing s ON s.workspace_id = w.workspace_id
                    LEFT JOIN dalton_user_sharing us ON us.user_id = w.user_id
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
            else:
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
                    ORDER BY
                        COALESCE(NULLIF(BTRIM(u.name), ''), LOWER(BTRIM(u.email))) ASC,
                        COALESCE(w.last_activity_at, w.updated_at) DESC,
                        w.updated_at DESC
                    """,
                    (uid, allowed_emails),
                )
            rows = [dict(r) for r in await cur.fetchall()]

    grouped: dict[str, dict[str, Any]] = {}
    default_cos = "rw" if dispatch_enabled else DEFAULT_COS_ACCESS
    owner_tiers: dict[str, str] = {}
    for row in rows:
        if is_home_row(row) or bool(row.get("is_home")):
            continue
        owner_uid = str(row["user_id"])
        owner_tier = owner_tiers.get(owner_uid)
        if owner_tier is None:
            owner_tier = _classify_owner_row_tier(owner_uid, row.get("owner_email"))
            owner_tiers[owner_uid] = owner_tier
        if owner_tier == "unknown":
            logger.warning(
                "Skipping workspace row with unresolvable owner tier "
                "(owner_user_id=%s, workspace_id=%s)",
                owner_uid,
                row.get("workspace_id"),
            )
            continue
        external_owned = owner_tier == "external"
        raw_web_access = row.get("web_access")
        web_access = _normalize_access_level(raw_web_access, DEFAULT_WEB_ACCESS)
        if web_access == "hidden":
            continue
        if external_owned:
            cos_access = "hidden"
        elif dispatch_enabled and row.get("cos_rw_enabled") is not None:
            cos_access = "rw" if bool(row.get("cos_rw_enabled")) else "hidden"
        else:
            cos_access = _normalize_access_level(row.get("cos_access"), default_cos)

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

    # Fully isolated environment: external members see no colleague workspace.
    caller_tier = await get_user_tier(uid)
    if caller_tier == "external":
        return []
    allowed_emails = sorted(await get_team_member_emails())

    dispatch_enabled = is_colleague_dispatch_enabled()
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if dispatch_enabled:
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
                        CASE
                            WHEN us.cos_rw_enabled IS FALSE THEN 'hidden'
                            ELSE 'rw'
                        END AS cos_access,
                        COALESCE(s.web_access, 'hidden') AS web_access
                    FROM workspaces w
                    JOIN users u ON w.user_id = u.user_id
                    LEFT JOIN dalton_workspace_sharing s ON s.workspace_id = w.workspace_id
                    LEFT JOIN dalton_user_sharing us ON us.user_id = w.user_id
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
            else:
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
                    ORDER BY
                        COALESCE(NULLIF(BTRIM(u.name), ''), LOWER(BTRIM(u.email))) ASC,
                        COALESCE(w.last_activity_at, w.updated_at) DESC,
                        w.updated_at DESC
                    """,
                    (uid, allowed_emails),
                )
            rows = [dict(r) for r in await cur.fetchall()]

    items: list[dict[str, Any]] = []
    # Sharing settings never apply to an external-owned workspace: admins see
    # it read-only (even when marked hidden), everyone else sees nothing. The
    # owner tier is classified from the joined email (no per-owner lookup).
    default_cos = "rw" if dispatch_enabled else DEFAULT_COS_ACCESS
    owner_tiers: dict[str, str] = {}
    for row in rows:
        if is_home_row(row) or bool(row.get("is_home")):
            continue
        owner_uid = str(row["user_id"])
        owner_tier = owner_tiers.get(owner_uid)
        if owner_tier is None:
            owner_tier = _classify_owner_row_tier(owner_uid, row.get("owner_email"))
            owner_tiers[owner_uid] = owner_tier
        if owner_tier == "unknown":
            logger.warning(
                "Skipping colleague CoS workspace row with unresolvable owner "
                "tier (owner_user_id=%s, workspace_id=%s)",
                owner_uid,
                row.get("workspace_id"),
            )
            continue
        external_owned = owner_tier == "external"
        if external_owned:
            continue
        cos_access = _normalize_access_level(
            row.get("cos_access"), default_cos
        )
        if cos_access == "hidden":
            continue
        web_access = _normalize_access_level(
            row.get("web_access"), DEFAULT_WEB_ACCESS
        )
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
                "access": cos_access,
                "cos_access": cos_access,
                "web_access": web_access,
                "read_only": cos_access != "rw",
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
