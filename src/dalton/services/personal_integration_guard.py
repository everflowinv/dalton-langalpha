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
from psycopg.types.json import Jsonb

from src.dalton.colleague_outbound_guard import (
    OUTBOUND_GUARD_REFUSAL_MESSAGE,
    is_run_outbound_blocked,
)
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationStatus,
    lifecycle_lock_key,
    touch_user_lock_key,
)
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

PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE = (
    "本次结果含个人日历内容，已发送到你的私聊"
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
    request_key = str(cfg.get("request_key") or "").strip() or None
    external_thread_id = str(cfg.get("external_thread_id") or "").strip() or None
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
        "request_key": request_key,
        "external_thread_id": external_thread_id,
        "turn_index": turn_index,
    }


def _base_platform(platform: str | None) -> str | None:
    if not platform:
        return None
    base = platform.split(":", 1)[0].strip().lower()
    return base or None


def _is_channel_platform(platform: str | None) -> bool:
    base = _base_platform(platform)
    return bool(base and base in _CHANNEL_PLATFORMS)


def _is_dm_channel_address(platform: str | None, address: str | None) -> bool:
    """Return True iff ``address`` is a verified 1:1 DM target for ``platform``."""
    plat = _base_platform(platform)
    addr = str(address or "").strip()
    if not plat or not addr:
        return False
    low = addr.lower()
    if low == f"{plat}:@me":
        return True
    if plat == "weixin":
        # Weixin only supports 1:1 DM; accept ``weixin:@me`` or ``weixin:<account>:<peer>``
        return low.startswith("weixin:") and ":group:" not in low and ":chat:" not in low
    return low.startswith(f"{plat}:dm:")


def _is_non_dm_channel_external_id(external_id: str | None) -> bool:
    """Return True if ``conversation_threads.external_id`` names a channel group/channel."""
    raw = str(external_id or "").strip()
    if not raw or ":" not in raw:
        return False
    prefix, rest = raw.split(":", 1)
    if prefix.strip().lower() not in _CHANNEL_PLATFORMS:
        return False
    rest_low = rest.strip().lower()
    return not (rest_low.startswith("dm:") or rest_low.startswith("@me"))


async def _lookup_server_channel_record_tx(
    conn: Any,
    *,
    caller_user_id: str,
    thread_id: str,
    response_id: str,
    request_key: str | None,
    external_thread_id: str | None,
) -> dict[str, Any] | None:
    """Read authoritative server-side channel record for this turn/thread (never trusts gateway defaults)."""
    async with conn.cursor(row_factory=dict_row) as cur:
        norm_rk = normalize_uuid(request_key) if request_key else None
        if norm_rk is not None:
            await cur.execute(
                """
                SELECT dalton_user_id, platform, chat_type,
                       chat_address AS target_address,
                       thread_scope_key, external_thread_id,
                       thread_id, run_id
                FROM dalton_channel_inbound_events
                WHERE request_key = %s::uuid
                   OR retry_request_key = %s::uuid
                   OR resubmitted_request_key = %s::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (norm_rk, norm_rk, norm_rk),
            )
            row = await cur.fetchone()
            if row is not None:
                return {**dict(row), "_source_table": "inbound_events"}

        if response_id:
            await cur.execute(
                """
                SELECT dalton_user_id, platform, chat_type,
                       chat_address AS target_address,
                       thread_scope_key, external_thread_id,
                       thread_id, run_id
                FROM dalton_channel_inbound_events
                WHERE run_id = %s AND thread_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (response_id, thread_id),
            )
            row = await cur.fetchone()
            if row is not None:
                return {**dict(row), "_source_table": "inbound_events"}

        await cur.execute(
            """
            SELECT dalton_user_id, platform, chat_type,
                   chat_address AS target_address,
                   thread_scope_key, external_thread_id,
                   thread_id, run_id
            FROM dalton_channel_inbound_events
            WHERE thread_id = %s::uuid
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (thread_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            return {**dict(row), "_source_table": "inbound_events"}

        if external_thread_id:
            await cur.execute(
                """
                SELECT dalton_user_id, platform, chat_type,
                       chat_address AS target_address,
                       thread_scope_key, external_thread_id,
                       thread_id, run_id
                FROM dalton_channel_inbound_events
                WHERE external_thread_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (external_thread_id,),
            )
            row = await cur.fetchone()
            if row is not None:
                return {**dict(row), "_source_table": "inbound_events"}

        if response_id:
            await cur.execute(
                """
                SELECT dalton_user_id, platform, target_address,
                       thread_id, run_id
                FROM dalton_channel_background_runs
                WHERE run_id = %s AND thread_id = %s::uuid
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (response_id, thread_id),
            )
            row = await cur.fetchone()
            if row is not None:
                return {**dict(row), "_source_table": "background_runs"}

        await cur.execute(
            """
            SELECT dalton_user_id, platform, target_address,
                   scope_key AS thread_scope_key, thread_id
            FROM dalton_channel_thread_watches
            WHERE thread_id = %s::uuid
            LIMIT 1
            """,
            (thread_id,),
        )
        row = await cur.fetchone()
        if row is not None:
            return {**dict(row), "_source_table": "thread_watches"}

    return None


async def _verify_bound_channel_identity_tx(
    conn: Any,
    *,
    caller_user_id: str,
    platform: str,
    expected_platform_user_id: str | None = None,
) -> bool:
    """Verify ``caller_user_id`` has an active server-side binding on ``platform``."""
    base_plat = _base_platform(platform)
    if not base_plat:
        return False
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT platform_user_id
            FROM dalton_channel_identities
            WHERE user_id = %s AND platform = %s
            LIMIT 1
            """,
            (caller_user_id, base_plat),
        )
        ident = await cur.fetchone()
    if ident is None:
        return False
    bound_puid = str(ident.get("platform_user_id") or "").strip()
    if not bound_puid:
        return False
    if expected_platform_user_id and bound_puid != expected_platform_user_id:
        return False
    return True


async def _verify_channel_dm_surface_tx(
    conn: Any,
    *,
    caller_user_id: str,
    thread_id: str,
    response_id: str,
    request_key: str | None,
    external_thread_id: str | None,
    platform: str | None,
) -> tuple[str, str]:
    """Verify from authoritative server-side channel records that this channel turn is caller's 1:1 DM.

    Fail-closed: missing record, group/channel chat_type, non-DM scope/address,
    owner mismatch, or missing channel identity binding all deny access.
    Returns ``(verified_platform, "dm")``.
    """
    expected_plat = _base_platform(platform)
    rec = await _lookup_server_channel_record_tx(
        conn,
        caller_user_id=caller_user_id,
        thread_id=thread_id,
        response_id=response_id,
        request_key=request_key,
        external_thread_id=external_thread_id,
    )
    if rec is None:
        raise PersonalSurfaceDeniedError(
            "non_private_surface",
            NON_PRIVATE_SURFACE_GUIDE,
        )

    rec_user = str(rec.get("dalton_user_id") or "").strip()
    rec_plat = _base_platform(str(rec.get("platform") or ""))
    if not rec_user or rec_user != caller_user_id or not rec_plat:
        raise PersonalSurfaceDeniedError(
            "non_private_surface",
            NON_PRIVATE_SURFACE_GUIDE,
        )
    if expected_plat and rec_plat != expected_plat:
        raise PersonalSurfaceDeniedError(
            "non_private_surface",
            NON_PRIVATE_SURFACE_GUIDE,
        )

    src_table = str(rec.get("_source_table") or "")
    scope_key = str(rec.get("thread_scope_key") or "").strip()
    target_address = str(rec.get("target_address") or "").strip()

    if src_table == "background_runs":
        raise PersonalSurfaceDeniedError(
            "unattended_turn_unsupported",
            "个人日历只能在你本人的对话里使用，自动任务暂不支持。",
        )

    if src_table == "inbound_events":
        rec_chat_type = str(rec.get("chat_type") or "").strip().lower()
        if rec_chat_type != "dm":
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )
        if scope_key and not scope_key.lower().startswith("dm:"):
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )
        if not _is_dm_channel_address(rec_plat, target_address):
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )
    else:
        if scope_key and not scope_key.lower().startswith("dm:"):
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )
        if not _is_dm_channel_address(rec_plat, target_address):
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )

    expected_puid: str | None = None
    if scope_key.lower().startswith("dm:"):
        tail = scope_key[3:].strip()
        if tail:
            expected_puid = tail.split(":", 1)[0].strip() or None

    bound_ok = await _verify_bound_channel_identity_tx(
        conn,
        caller_user_id=caller_user_id,
        platform=rec_plat,
        expected_platform_user_id=expected_puid,
    )
    if not bound_ok:
        raise PersonalSurfaceDeniedError(
            "non_private_surface",
            NON_PRIVATE_SURFACE_GUIDE,
        )
    return rec_plat, "dm"


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
    expected_auth_epoch: int | None = None,
    expected_provider_account_id: str | None = None,
    record_touch_now: bool = True,
    conn: Any = None,
) -> PersonalSurfaceVerificationResult:
    """Verify the current turn runs on a private surface and optionally record the touch (§4.4).

    Acquires ``FOR UPDATE`` on ``conversation_threads`` and ``FOR SHARE`` on
    ``workspaces`` inside a short transaction that commits BEFORE any external HTTP
    call runs. Channel turns are verified strictly against authoritative server-side
    channel records and bound identities (never trusting missing gateway fields).
    """
    ctx = extract_surface_context(config)
    caller_user_id = ctx["caller_user_id"]
    thread_id = ctx["thread_id"]
    response_id = ctx["response_id"]
    agent_role = ctx["agent_role"]
    platform = ctx["platform"]
    chat_type = ctx["chat_type"]
    request_key = ctx["request_key"]
    external_thread_id = ctx["external_thread_id"]

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

    # Early fail-closed rejection when an explicit non-DM chat_type is present
    is_channel = _is_channel_platform(platform)
    if chat_type is not None and chat_type != "dm":
        raise PersonalSurfaceDeniedError(
            "non_private_surface",
            NON_PRIVATE_SURFACE_GUIDE,
        )

    async def _run(db_conn: Any) -> PersonalSurfaceVerificationResult:
        # Serialize account-generation re-verification & touch recording with
        # disconnect / account-switch Phase 1 using the EXACT same advisory lock
        # ordering (`lifecycle_lock_key -> touch_user_lock_key -> conversation_threads FOR UPDATE`)
        # inside one transaction so a concurrent disconnect can never miss a first touch.
        needs_lifecycle_lock = (
            record_touch_now
            or expected_auth_epoch is not None
            or expected_provider_account_id is not None
        )
        async with db_conn.cursor(row_factory=dict_row) as cur:
            if needs_lifecycle_lock:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (lifecycle_lock_key(caller_user_id, provider),),
                )
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (touch_user_lock_key(caller_user_id),),
                )
            await cur.execute(
                """
                SELECT t.conversation_thread_id,
                       t.workspace_id,
                       COALESCE(t.user_id, w.user_id) AS thread_owner,
                       t.external_id AS thread_external_id,
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
        thread_external_id = str(row.get("thread_external_id") or "").strip() or None

        if thread_owner != caller_user_id:
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )

        # Reject channel group/channel threads even if accessed later without platform
        if _is_non_dm_channel_external_id(thread_external_id) or _is_non_dm_channel_external_id(
            external_thread_id
        ):
            raise PersonalSurfaceDeniedError(
                "non_private_surface",
                NON_PRIVATE_SURFACE_GUIDE,
            )

        verified_platform = platform
        verified_chat_type = chat_type
        is_dm_channel = False
        if is_channel or chat_type == "dm":
            verified_platform, verified_chat_type = await _verify_channel_dm_surface_tx(
                db_conn,
                caller_user_id=caller_user_id,
                thread_id=thread_id,
                response_id=response_id,
                request_key=request_key,
                external_thread_id=external_thread_id or thread_external_id,
                platform=platform,
            )
            is_dm_channel = True

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

        # Atomic account generation re-verification when recording touch after token acquisition
        if expected_auth_epoch is not None or expected_provider_account_id is not None:
            if (
                conn_row is None
                or conn_row.status
                not in (IntegrationStatus.CONNECTED, IntegrationStatus.REFRESH_AMBIGUOUS)
                or (
                    expected_auth_epoch is not None
                    and int(conn_row.auth_epoch) != int(expected_auth_epoch)
                )
                or (
                    expected_provider_account_id is not None
                    and str(conn_row.provider_account_id) != str(expected_provider_account_id)
                )
            ):
                raise PersonalSurfaceDeniedError(
                    "connection_changed",
                    "日历连接状态或绑定账号已变更，已中止本次操作，请重试。",
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
        first_touch_event: dict[str, Any] | None = None
        if is_first_touch:
            notice = (
                FIRST_TOUCH_NOTICE_CALENDAR
                if service == "calendar"
                else FIRST_TOUCH_NOTICE_GENERIC
            )
            first_touch_event = {
                "turn_index": int(turn_index),
                "response_id": response_id or None,
                "provider": provider,
                "service": service,
                "message": notice,
            }
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE conversation_threads
                    SET metadata = jsonb_set(
                            COALESCE(metadata, '{}'::jsonb),
                            '{personal_first_touch_event}',
                            %s::jsonb,
                            true
                        ),
                        updated_at = NOW()
                    WHERE conversation_thread_id = %s::uuid
                      AND (metadata->'personal_first_touch_event') IS NULL
                    """,
                    (Jsonb(first_touch_event), thread_id),
                )

        return (
            PersonalSurfaceVerificationResult(
                caller_user_id=caller_user_id,
                workspace_id=ws_id,
                thread_id=thread_id,
                response_id=response_id,
                turn_index=turn_index,
                is_first_touch=is_first_touch,
                first_touch_notice=notice,
                platform=verified_platform,
                chat_type=verified_chat_type,
                is_dm_channel=is_dm_channel,
            ),
            first_touch_event,
        )

    if conn is not None:
        result, ft_event = await _run(conn)
    else:
        async with get_db_connection() as db_conn, db_conn.transaction():
            result, ft_event = await _run(db_conn)
    if ft_event is not None:
        _emit_live_first_touch_notice(ft_event)
    return result


def _emit_live_first_touch_notice(event_payload: Mapping[str, Any]) -> None:
    """Emit the first-touch privacy notice onto the live LangGraph custom stream (best-effort)."""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if callable(writer):
            writer({"type": "personal_integration_notice", **dict(event_payload)})
    except Exception:
        # Outside a live LangGraph stream (e.g. unit test invoking tool directly); DB event still replays.
        pass


async def claim_first_touch_notice_if_needed(
    thread_id: str,
    *,
    service: str = "calendar",
    conn: Any = None,
) -> str | None:
    """Persist and deduplicate the first-touch privacy notice per thread.

    Called once a personal integration call actually produces a result for the
    thread so failed/interrupted first calls do not drop the notice and later
    calls in the same thread do not repeat it.
    """
    tid = normalize_uuid(thread_id)
    if tid is None:
        return None

    async def _run(db_conn: Any) -> str | None:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_threads
                SET metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{personal_first_touch_notice_shown}',
                        'true'::jsonb,
                        true
                    ),
                    updated_at = NOW()
                WHERE conversation_thread_id = %s::uuid
                  AND COALESCE(has_personal_integration_data, FALSE) = TRUE
                  AND COALESCE((metadata->>'personal_first_touch_notice_shown')::boolean, FALSE) = FALSE
                RETURNING conversation_thread_id
                """,
                (tid,),
            )
            claimed = await cur.fetchone()
        if claimed is None:
            return None
        return (
            FIRST_TOUCH_NOTICE_CALENDAR
            if service == "calendar"
            else FIRST_TOUCH_NOTICE_GENERIC
        )

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)


async def check_personal_thread_send_allowed(
    *,
    thread_id: str | None,
    caller_user_id: str | None,
    run_id: str | None = None,
    target: str | None = None,
    turn_platform: str | None = None,
    conn: Any = None,
) -> tuple[bool, str | None]:
    """Gate ``send_message`` when the source thread carries personal integration data.

    Only verified 1:1 DM destinations owned by ``caller_user_id`` are allowed;
    any group chat, Discord channel, or unverified destination is refused.
    """
    if not is_dalton_enabled():
        return True, None
    tid = normalize_uuid(thread_id) if thread_id else None
    uid = str(caller_user_id or "").strip()
    if tid is None or not uid:
        return True, None
    if not await is_thread_personal_private(tid, conn=conn):
        return True, None

    cleaned_target = str(target or "").strip()
    refusal = (
        "当前对话包含个人日历/邮箱私密数据，仅允许向本人已验证的 1:1 私聊发送消息，"
        "不能发送到群聊、频道或未验证目标。"
    )

    async def _run(db_conn: Any) -> tuple[bool, str | None]:
        if cleaned_target:
            if ":" not in cleaned_target:
                return False, refusal
            plat_prefix = _base_platform(cleaned_target.split(":", 1)[0])
            if not plat_prefix or plat_prefix not in _CHANNEL_PLATFORMS:
                return False, refusal
            if not _is_dm_channel_address(plat_prefix, cleaned_target):
                return False, refusal
            bound_ok = await _verify_bound_channel_identity_tx(
                db_conn,
                caller_user_id=uid,
                platform=plat_prefix,
            )
            if not bound_ok:
                return False, refusal
            return True, None

        # No explicit target -> sending to current conversation; verify it is caller's 1:1 DM
        try:
            await _verify_channel_dm_surface_tx(
                db_conn,
                caller_user_id=uid,
                thread_id=tid,
                response_id=str(run_id or "").strip(),
                request_key=None,
                external_thread_id=None,
                platform=turn_platform,
            )
            return True, None
        except PersonalSurfaceDeniedError:
            return False, refusal

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn:
        return await _run(db_conn)


async def check_personal_thread_dispatch_allowed(
    *,
    source_thread_id: str | None,
    caller_user_id: str | None,
    target_workspace_id: str | None,
    target_thread_id: str | None = None,
    conn: Any = None,
) -> tuple[bool, str | None]:
    """Gate ``delegate_to_analyst`` when the source thread carries personal integration data.

    Any delegation from a thread containing personal integration data is refused
    (no privacy-flag inheritance) with a clear user-facing explanation.
    """
    _ = target_workspace_id, target_thread_id
    if not is_dalton_enabled():
        return True, None
    src_tid = normalize_uuid(source_thread_id) if source_thread_id else None
    uid = str(caller_user_id or "").strip()
    if src_tid is None or not uid:
        return True, None
    if not await is_thread_personal_private(src_tid, conn=conn):
        return True, None

    refusal = (
        "当前对话包含个人日历/邮箱私密数据，不支持向其他工作区或线程转派任务；"
        "请直接在当前私密对话中继续处理，或另开不含私密数据的研究对话后再转派。"
    )
    return False, refusal


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


async def get_thread_creator_user_id(
    thread_id: str,
    *,
    conn: Any = None,
) -> str | None:
    """Return ``conversation_threads.user_id`` for ``thread_id`` (fail-closed None)."""
    tid = normalize_uuid(thread_id) if thread_id else None
    if tid is None:
        return None
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT user_id
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s::uuid
                    LIMIT 1
                    """,
                    (tid,),
                )
                row = await cur.fetchone()
                if not isinstance(row, Mapping):
                    return None
                uid = str(row.get("user_id") or "").strip()
                return uid or None
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return None
        raise


def _parse_dm_notify_target(target: str) -> tuple[str, str] | None:
    """Parse ``feishu:user:<id>``, ``discord:user:<id>``, or ``weixin:user:<id>``."""
    cleaned = str(target or "").strip()
    parts = cleaned.split(":", 2)
    if len(parts) != 3:
        return None
    platform, kind, recipient = parts[0].strip().lower(), parts[1].strip().lower(), parts[2].strip()
    if kind != "user" or not recipient:
        return None
    if platform not in ("feishu", "discord", "weixin"):
        return None
    return platform, recipient


async def verify_personal_dm_target_bound(
    user_id: str,
    target: str,
    *,
    conn: Any = None,
) -> bool:
    """Verify ``target`` is currently bound to ``user_id`` in channel binding tables (fail-closed)."""
    uid = str(user_id or "").strip()
    parsed = _parse_dm_notify_target(target)
    if not uid or parsed is None:
        return False
    platform, recipient_id = parsed
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor() as cur:
                if platform == "weixin":
                    await cur.execute(
                        """
                        SELECT 1
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                          AND status = 'active'
                          AND ilink_user_id = %s
                        LIMIT 1
                        """,
                        (uid, recipient_id),
                    )
                    return (await cur.fetchone()) is not None
                await cur.execute(
                    """
                    SELECT 1
                    FROM dalton_channel_identities
                    WHERE user_id = %s
                      AND platform = %s
                      AND platform_user_id = %s
                    LIMIT 1
                    """,
                    (uid, platform, recipient_id),
                )
                return (await cur.fetchone()) is not None
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return False
        raise


async def list_bound_personal_dm_targets(
    user_id: str,
    *,
    conn: Any = None,
) -> list[str]:
    """Return canonical DM notify targets currently bound to ``user_id``."""
    uid = str(user_id or "").strip()
    if not uid:
        return []
    targets: list[str] = []
    seen: set[str] = set()
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT platform, platform_user_id
                    FROM dalton_channel_identities
                    WHERE user_id = %s
                      AND platform IN ('feishu', 'discord')
                    ORDER BY platform ASC
                    """,
                    (uid,),
                )
                for row in await cur.fetchall() or []:
                    plat = str(row.get("platform") or "").strip().lower()
                    puid = str(row.get("platform_user_id") or "").strip()
                    if plat in ("feishu", "discord") and puid:
                        addr = f"{plat}:user:{puid}"
                        if addr not in seen:
                            seen.add(addr)
                            targets.append(addr)
                await cur.execute(
                    """
                    SELECT ilink_user_id
                    FROM dalton_channel_weixin_accounts
                    WHERE user_id = %s
                      AND status = 'active'
                    ORDER BY created_at DESC
                    """,
                    (uid,),
                )
                for row in await cur.fetchall() or []:
                    wx_uid = str(row.get("ilink_user_id") or "").strip()
                    if wx_uid:
                        addr = f"weixin:user:{wx_uid}"
                        if addr not in seen:
                            seen.add(addr)
                            targets.append(addr)
    except RuntimeError as exc:
        if "pool is not open" in str(exc).lower():
            return []
        raise
    return targets


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
    "PERSONAL_INTEGRATION_GROUP_REDIRECT_NOTICE",
    "PERSONAL_INTEGRATION_NOT_SHARABLE_CODE",
    "PERSONAL_INTEGRATION_NOT_SHARABLE_MESSAGE",
    "PersonalIntegrationNotSharableError",
    "PersonalSurfaceDeniedError",
    "PersonalSurfaceVerificationResult",
    "assert_thread_sharable_tx",
    "check_personal_thread_dispatch_allowed",
    "check_personal_thread_send_allowed",
    "check_thread_surface_still_private",
    "claim_first_touch_notice_if_needed",
    "enforce_personal_thread_owner_only",
    "extract_surface_context",
    "get_thread_creator_user_id",
    "is_thread_personal_private",
    "list_bound_personal_dm_targets",
    "verify_personal_dm_target_bound",
    "verify_personal_integration_surface",
]
