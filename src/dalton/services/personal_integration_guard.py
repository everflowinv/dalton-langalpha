"""Server-side private surface guard, first-touch notice, and thread visibility checks (§4.4).

Enforces the same caller-identity and effective privacy policy for both
``chief_of_staff`` and ``analyst`` turns:
- Caller identity is always extracted from ``RunnableConfig`` (never model args).
- Analyst turns blocked by ``colleague_outbound_guard`` are denied.
- Group chats / channels (``chat_type == 'group'``) and organization shared
  workspaces (``shared_workspaces`` / holder-owned) short-circuit with private guidance.
- Non-Home personal workspaces must have effective ``cos_access == 'hidden'`` AND
  ``web_access == 'hidden'`` under the active sharing configuration.
- Threads with ``is_shared`` or ``workspace_shared_at IS NOT NULL`` are refused.
- Threads with ``personal_purge_in_progress`` or connections in
  ``disconnect_pending`` / ``revoked_purging`` pause admission.
- Under a short ``FOR UPDATE`` transaction (released BEFORE any external HTTP),
  records the touch via ``record_touch(...)`` and emits the first-touch notice on
  the ``FALSE -> TRUE`` transition of ``has_personal_integration_data``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import Any

from fastapi import HTTPException
from psycopg.rows import dict_row

from src.dalton.colleague_outbound_guard import (
    OUTBOUND_GUARD_REFUSAL_MESSAGE,
    is_run_outbound_blocked,
)
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import IntegrationStatus
from src.dalton.services.purge_fence import (
    PURGE_IN_PROGRESS_CODE,
    PURGE_IN_PROGRESS_MESSAGE,
)
from src.dalton.settings import (
    is_colleague_dispatch_enabled,
    is_dalton_enabled,
    is_dalton_sharing_auth_enabled,
)
from src.server.database.home_workspace import is_home_row, is_home_stand_in_row
from src.server.database.pool import get_db_connection
from src.server.database.shared_workspaces import is_holder
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)

PERSONAL_INTEGRATION_NOT_SHARABLE_CODE = "personal_integration_thread_not_sharable"
PERSONAL_INTEGRATION_NOT_SHARABLE_MESSAGE = (
    "该对话包含个人日历或邮箱私密内容，请新建纯研究对话后再分享"
)

FIRST_TOUCH_NOTICE_CALENDAR = (
    "提示：本对话已接入你的个人日历私密数据，仅限创建者本人访问；"
    "后续若断开该连接，将从本轮起向后清除同线程产生的后续对话内容，纯研究讨论建议另开新线程。"
)
FIRST_TOUCH_NOTICE_GENERIC = (
    "提示：本对话已接入个人日历/邮箱私密数据，仅限创建者本人访问；"
    "后续若断开该连接，将从本轮起向后清除同线程产生的后续对话内容，纯研究讨论建议另开新线程。"
)

NON_PRIVATE_SURFACE_GUIDE = (
    "个人日历仅支持在私密会话中使用，请前往本人私人 Home 会话、1:1 私聊，"
    "或仅本人可见的私密工作区中操作。"
)

_CHANNEL_PLATFORMS: frozenset[str] = frozenset(
    {"feishu", "discord", "weixin", "telegram", "slack"}
)


class PersonalIntegrationNotSharableError(HTTPException):
    """403 raised when attempting to share a thread containing personal integration data."""

    def __init__(self) -> None:
        super().__init__(
            status_code=403,
            detail={
                "code": PERSONAL_INTEGRATION_NOT_SHARABLE_CODE,
                "message": PERSONAL_INTEGRATION_NOT_SHARABLE_MESSAGE,
            },
        )


class PersonalSurfaceDeniedError(RuntimeError):
    """Domain refusal raised when a thread or surface cannot access personal integrations."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        is_purge_wait: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.is_purge_wait = is_purge_wait


@dataclass(frozen=True, slots=True)
class PersonalSurfaceVerificationResult:
    caller_user_id: str
    workspace_id: str
    thread_id: str
    response_id: str
    turn_index: int
    is_first_touch: bool
    first_touch_notice: str | None
    platform: str | None = None
    chat_type: str | None = None
    is_dm_channel: bool = False


def extract_surface_context(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract authoritative caller/thread/run/surface fields from ``RunnableConfig``."""
    cfg = config.get("configurable") if isinstance(config, Mapping) else None
    if not isinstance(cfg, Mapping):
        cfg = {}
    surface = cfg.get("surface")
    surface_map = surface if isinstance(surface, Mapping) else {}

    caller_user_id = str(cfg.get("user_id") or "").strip()
    workspace_id = str(
        cfg.get("turn_admitted_workspace_id") or cfg.get("workspace_id") or ""
    ).strip()
    thread_id = str(cfg.get("thread_id") or "").strip()
    response_id = str(
        cfg.get("run_id") or cfg.get("response_id") or ""
    ).strip()
    if not response_id and isinstance(config, Mapping):
        response_id = str(config.get("run_id") or "").strip()
    agent_role = str(cfg.get("agent_role") or "").strip().lower()
    platform = str(
        cfg.get("platform") or surface_map.get("platform") or ""
    ).strip().lower() or None
    chat_type = str(
        cfg.get("chat_type")
        or cfg.get("channel_chat_type")
        or surface_map.get("chat_type")
        or ""
    ).strip().lower() or None
    raw_turn_index = cfg.get("turn_index")
    turn_index: int | None = None
    if isinstance(raw_turn_index, int) and not isinstance(raw_turn_index, bool):
        turn_index = raw_turn_index

    return {
        "caller_user_id": caller_user_id,
        "workspace_id": workspace_id,
        "thread_id": thread_id,
        "response_id": response_id,
        "agent_role": agent_role,
        "platform": platform,
        "chat_type": chat_type,
        "turn_index": turn_index,
    }


def _is_channel_platform(platform: str | None) -> bool:
    if not platform:
        return False
    base = platform.split(":", 1)[0].strip().lower()
    return base in _CHANNEL_PLATFORMS


async def _is_workspace_effectively_private_tx(
    conn: Any,
    *,
    workspace_id: str,
    ws_owner: str,
    ws_config: Any,
    caller_user_id: str,
) -> bool:
    """Evaluate workspace effective privacy policy under an active DB transaction (§4.4 item 2)."""
    if str(ws_owner) != str(caller_user_id):
        return False
    if is_holder(ws_owner):
        return False

    ws_stub = {"user_id": ws_owner, "config": ws_config}
    if is_home_row(ws_stub) or is_home_stand_in_row(ws_stub):
        return True

    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT 1 FROM shared_workspaces WHERE workspace_id = %s::uuid LIMIT 1",
            (workspace_id,),
        )
        if await cur.fetchone() is not None:
            return False

        if not is_dalton_sharing_auth_enabled():
            return True

        await cur.execute(
            """
            SELECT cos_access, web_access
            FROM dalton_workspace_sharing
            WHERE workspace_id = %s::uuid
            """,
            (workspace_id,),
        )
        ws_share = await cur.fetchone()

        if is_colleague_dispatch_enabled():
            await cur.execute(
                """
                SELECT cos_rw_enabled
                FROM dalton_user_sharing
                WHERE user_id = %s
                """,
                (ws_owner,),
            )
            user_share = await cur.fetchone()
            cos_rw_enabled = (
                bool(user_share["cos_rw_enabled"])
                if user_share is not None and user_share.get("cos_rw_enabled") is not None
                else True
            )
            effective_cos = "hidden" if not cos_rw_enabled else "rw"
            effective_web = (
                str(ws_share.get("web_access") or "hidden").strip().lower()
                if ws_share is not None
                else "hidden"
            )
            return effective_cos == "hidden" and effective_web == "hidden"

        # Colleague dispatch disabled: absent row defaults to cos_access='ro' (§4.4 item 2.2)
        if ws_share is None:
            return False
        effective_cos = str(ws_share.get("cos_access") or "ro").strip().lower()
        effective_web = str(ws_share.get("web_access") or "hidden").strip().lower()
        return effective_cos == "hidden" and effective_web == "hidden"


async def _resolve_turn_index_tx(
    conn: Any,
    *,
    thread_id: str,
    response_id: str,
    hint: int | None,
) -> int:
    if hint is not None and hint >= 0:
        return hint
    async with conn.cursor(row_factory=dict_row) as cur:
        if normalize_uuid(response_id) is not None:
            await cur.execute(
                """
                SELECT turn_index
                FROM conversation_responses
                WHERE conversation_response_id = %s::uuid
                """,
                (response_id,),
            )
            row = await cur.fetchone()
            if row is not None and row.get("turn_index") is not None:
                return int(row["turn_index"])
        await cur.execute(
            """
            SELECT COALESCE(MAX(turn_index), 0) AS max_turn
            FROM conversation_queries
            WHERE conversation_thread_id = %s::uuid
            """,
            (thread_id,),
        )
        q_row = await cur.fetchone()
        if q_row is not None and q_row.get("max_turn") is not None:
            return int(q_row["max_turn"])
    return 0


async def verify_personal_integration_surface(
    config: Mapping[str, Any] | None,
    *,
    provider: str,
    service: str,
    auth_epoch: int | None = None,
    provider_account_id: str | None = None,
    record_touch_now: bool = True,
    conn: Any = None,
) -> PersonalSurfaceVerificationResult:
    """Verify the current turn runs on a private surface and atomically record the touch (§4.4).

    Acquires ``FOR UPDATE`` on ``conversation_threads`` and ``FOR SHARE`` on
    ``workspaces`` inside a short transaction that commits BEFORE any external HTTP
    call runs.
    """
    ctx = extract_surface_context(config)
    caller_user_id = ctx["caller_user_id"]
    thread_id = ctx["thread_id"]
    response_id = ctx["response_id"]
    agent_role = ctx["agent_role"]
    platform = ctx["platform"]
    chat_type = ctx["chat_type"]

    if not caller_user_id:
        raise PersonalSurfaceDeniedError(
            "missing_identity",
            "无法识别当前调用者身份，请在本人登录会话中使用个人日历功能。",
        )
    if not thread_id or normalize_uuid(thread_id) is None:
        raise PersonalSurfaceDeniedError(
            "missing_thread",
            NON_PRIVATE_SURFACE_GUIDE,
        )

    # Analyst outbound block check (§4.4 item 1)
    if agent_role == "analyst" and is_dalton_sharing_auth_enabled():
        if await is_run_outbound_blocked(config):
            raise PersonalSurfaceDeniedError(
                "colleague_outbound_blocked",
                OUTBOUND_GUARD_REFUSAL_MESSAGE,
            )

    # Channel surface check: allow 1:1 DM only; reject groups/channels (§4.4 item 2.3)
    is_channel = _is_channel_platform(platform)
    if chat_type == "group" or (is_channel and chat_type not in (None, "dm")):
        raise PersonalSurfaceDeniedError(
            "non_private_surface",
            NON_PRIVATE_SURFACE_GUIDE,
        )
    is_dm_channel = bool(is_channel and chat_type == "dm")

    async def _run(db_conn: Any) -> PersonalSurfaceVerificationResult:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT t.conversation_thread_id,
                       t.workspace_id,
                       COALESCE(t.user_id, w.user_id) AS thread_owner,
                       t.is_shared,
                       t.workspace_shared_at,
                       t.has_personal_integration_data,
                       t.personal_purge_in_progress,
                       w.user_id AS ws_owner,
                       w.config AS ws_config,
                       w.status AS ws_status
                FROM conversation_threads t
                JOIN workspaces w ON w.workspace_id = t.workspace_id
                WHERE t.conversation_thread_id = %s::uuid
                FOR UPDATE OF t
                FOR SHARE OF w
                """,
                (thread_id,),
            )
            row = await cur.fetchone()

        if row is None:
            raise PersonalSurfaceDeniedError(
                "thread_not_found",
                NON_PRIVATE_SURFACE_GUIDE,
            )

        ws_id = str(row["workspace_id"])
        thread_owner = str(row["thread_owner"] or "").strip()
        ws_owner = str(row["ws_owner"] or "").strip()

        if thread_owner != caller_user_id:
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )

        if bool(row.get("personal_purge_in_progress")):
            raise PersonalSurfaceDeniedError(
                PURGE_IN_PROGRESS_CODE,
                PURGE_IN_PROGRESS_MESSAGE,
                is_purge_wait=True,
            )

        # Check connection & active purge ledger state
        conn_row = await db.get_connection(
            caller_user_id, provider, service, conn=db_conn
        )
        if conn_row is not None and conn_row.status in (
            IntegrationStatus.DISCONNECT_PENDING,
            IntegrationStatus.REVOKED_PURGING,
        ):
            raise PersonalSurfaceDeniedError(
                PURGE_IN_PROGRESS_CODE,
                PURGE_IN_PROGRESS_MESSAGE,
                is_purge_wait=True,
            )
        if await db.has_running_local_purge_for_service(
            caller_user_id, provider, service, conn=db_conn
        ):
            raise PersonalSurfaceDeniedError(
                PURGE_IN_PROGRESS_CODE,
                PURGE_IN_PROGRESS_MESSAGE,
                is_purge_wait=True,
            )

        if bool(row.get("is_shared")) or row.get("workspace_shared_at") is not None:
            raise PersonalSurfaceDeniedError(
                "thread_shared",
                NON_PRIVATE_SURFACE_GUIDE,
            )

        ws_private = await _is_workspace_effectively_private_tx(
            db_conn,
            workspace_id=ws_id,
            ws_owner=ws_owner,
            ws_config=row.get("ws_config"),
            caller_user_id=caller_user_id,
        )
        if not ws_private:
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )

        turn_index = await _resolve_turn_index_tx(
            db_conn,
            thread_id=thread_id,
            response_id=response_id,
            hint=ctx["turn_index"],
        )

        is_first_touch = False
        if (
            record_touch_now
            and auth_epoch is not None
            and provider_account_id
            and normalize_uuid(response_id) is not None
        ):
            is_first_touch = await db.record_touch(
                thread_id=thread_id,
                response_id=response_id,
                provider=provider,
                service=service,
                auth_epoch=int(auth_epoch),
                turn_index=turn_index,
                user_id=caller_user_id,
                provider_account_id=provider_account_id,
                conn=db_conn,
            )

        notice: str | None = None
        if is_first_touch:
            notice = (
                FIRST_TOUCH_NOTICE_CALENDAR
                if service == "calendar"
                else FIRST_TOUCH_NOTICE_GENERIC
            )

        return PersonalSurfaceVerificationResult(
            caller_user_id=caller_user_id,
            workspace_id=ws_id,
            thread_id=thread_id,
            response_id=response_id,
            turn_index=turn_index,
            is_first_touch=is_first_touch,
            first_touch_notice=notice,
            platform=platform,
            chat_type=chat_type,
            is_dm_channel=is_dm_channel,
        )

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)


async def check_thread_surface_still_private(
    thread_id: str,
    caller_user_id: str,
    *,
    conn: Any = None,
) -> bool:
    """Re-verify during HITL resume that the thread and workspace remain private (§4.4 item 5)."""
    tid = normalize_uuid(thread_id)
    if tid is None or not caller_user_id:
        return False

    async def _run(db_conn: Any) -> bool:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT t.workspace_id,
                       COALESCE(t.user_id, w.user_id) AS thread_owner,
                       t.is_shared,
                       t.workspace_shared_at,
                       t.personal_purge_in_progress,
                       w.user_id AS ws_owner,
                       w.config AS ws_config
                FROM conversation_threads t
                JOIN workspaces w ON w.workspace_id = t.workspace_id
                WHERE t.conversation_thread_id = %s::uuid
                FOR SHARE OF t
                """,
                (tid,),
            )
            row = await cur.fetchone()
        if row is None:
            return False
        if str(row.get("thread_owner") or "").strip() != str(caller_user_id).strip():
            return False
        if bool(row.get("personal_purge_in_progress")):
            return False
        if bool(row.get("is_shared")) or row.get("workspace_shared_at") is not None:
            return False
        return await _is_workspace_effectively_private_tx(
            db_conn,
            workspace_id=str(row["workspace_id"]),
            ws_owner=str(row["ws_owner"] or ""),
            ws_config=row.get("ws_config"),
            caller_user_id=caller_user_id,
        )

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)


async def is_thread_personal_private(
    thread_id: str,
    *,
    meta: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> bool:
    """Return True iff ``conversation_threads.has_personal_integration_data`` is TRUE."""
    if not is_dalton_enabled():
        return False
    if meta is not None and "has_personal_integration_data" in meta:
        return bool(meta.get("has_personal_integration_data"))
    tid = normalize_uuid(thread_id)
    if tid is None:
        return False
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(has_personal_integration_data, FALSE)
                        AS has_personal_integration_data
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s::uuid
                    """,
                    (tid,),
                )
                row = await cur.fetchone()
                return bool(row and row.get("has_personal_integration_data"))
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return False
        raise


async def enforce_personal_thread_owner_only(
    thread_id: str,
    caller_user_id: str,
    *,
    thread_owner: str | None = None,
    meta: Mapping[str, Any] | None = None,
    conn: Any = None,
) -> None:
    """Raise 404 ``Thread not found`` if a non-owner tries to access a personal-integration thread (§4.4 item 3)."""
    if not is_dalton_enabled():
        return
    uid = str(caller_user_id or "").strip()
    owner = str(
        thread_owner
        or (meta or {}).get("thread_user_id")
        or (meta or {}).get("user_id")
        or ""
    ).strip()
    if owner and uid == owner:
        return
    if await is_thread_personal_private(thread_id, meta=meta, conn=conn):
        raise HTTPException(status_code=404, detail="Thread not found")


async def assert_thread_sharable_tx(conn: Any, thread_id: str) -> None:
    """Inside an active transaction, lock the thread row and refuse sharing if personal data is present."""
    if not is_dalton_enabled():
        return
    tid = normalize_uuid(thread_id)
    if tid is None:
        return
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT COALESCE(has_personal_integration_data, FALSE)
                AS has_personal_integration_data
            FROM conversation_threads
            WHERE conversation_thread_id = %s::uuid
            FOR UPDATE
            """,
            (tid,),
        )
        row = await cur.fetchone()
    if row is not None and bool(row.get("has_personal_integration_data")):
        raise PersonalIntegrationNotSharableError()


__all__ = [
    "FIRST_TOUCH_NOTICE_CALENDAR",
    "FIRST_TOUCH_NOTICE_GENERIC",
    "NON_PRIVATE_SURFACE_GUIDE",
    "PERSONAL_INTEGRATION_NOT_SHARABLE_CODE",
    "PERSONAL_INTEGRATION_NOT_SHARABLE_MESSAGE",
    "PersonalIntegrationNotSharableError",
    "PersonalSurfaceDeniedError",
    "PersonalSurfaceVerificationResult",
    "assert_thread_sharable_tx",
    "check_thread_surface_still_private",
    "enforce_personal_thread_owner_only",
    "extract_surface_context",
    "is_thread_personal_private",
    "verify_personal_integration_surface",
]
