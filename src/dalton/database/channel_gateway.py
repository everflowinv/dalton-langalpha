"""Database persistence and transaction logic for CH-1 Channel Gateway (`I-1` ~ `I-9` & §1.5).

All SQL for the 11 `dalton_channel_*` tables (migrations `d030`, `d031`) lives here.

Global lock order across transactions:
0. Per-message / per-delivery advisory locks (`dalton_inbound:<platform>:<message_id>`, `dalton_delivery:<delivery_key>`)
1. Personal settings advisory + row locks (`dalton_user_settings:<user_id>`, `dalton_channel_user_settings` `FOR UPDATE`; sorted by `user_id` when locking multiple users)
2. Group chat advisory locks (`_acquire_group_chat_lock`; sorted by `platform`, `chat_address`)
3. Chat binding rows (`dalton_channel_chat_bindings`) / identity rows (`dalton_channel_identities`) / session generation rows (`dalton_channel_session_generations` `FOR UPDATE`)
4. Inbound event / thread watch / background run / delivery rows (`dalton_channel_inbound_events`, `dalton_channel_thread_watches`, `dalton_channel_background_runs`, `dalton_channel_deliveries` `FOR UPDATE`)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import random
import secrets
from typing import Any
from uuid import UUID, uuid5

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton import sharing as dalton_sharing
from src.dalton.access_tiers import get_user_tier
from src.dalton.settings import is_colleague_dispatch_enabled
from src.server.database import pool as db_pool
from src.server.database.conversation.threads_read import lookup_thread_by_external_id
from src.server.database.encryption import get_encryption_key
from src.server.database.home_workspace import get_home_workspace_id
from src.server.services.channel_settings import (
    _App,
    _Binding,
    _field,
    _get_writable_workspace_names,
    _problem_message,
)
from src.server.utils.pg_sanitize import normalize_uuid
from pydantic import ValidationError

logger = logging.getLogger(__name__)


def get_db_connection(existing_conn: Any = None):
    return db_pool.get_db_connection(existing_conn)


DALTON_CHANNEL_NAMESPACE = UUID("5c8b6e20-8a1f-4e92-b52c-1d9a40c80001")
SUPPORTED_PLATFORMS: tuple[str, ...] = ("feishu", "discord", "weixin")
GROUP_PLATFORMS: tuple[str, ...] = ("feishu", "discord")
BIND_CODE_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
BIND_CODE_TTL_SECONDS = 600
SESSION_INACTIVITY_TIMEOUT = timedelta(hours=6)
CHIEF_OF_STAFF_DISPLAY_NAME = "Chief of Staff"
USER_SETTINGS_PKEY_CONSTRAINT = "dalton_channel_user_settings_pkey"
_TX_RETRY_MAX_ATTEMPTS = 3
_TX_RETRY_BACKOFF_MIN_SECONDS = 0.02
_TX_RETRY_BACKOFF_MAX_SECONDS = 0.10


def _coerce_utc_datetime(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return (
                parsed
                if parsed.tzinfo is not None
                else parsed.replace(tzinfo=timezone.utc)
            )
        except ValueError:
            return None
    return None


def _is_session_expired(last_activity_at: Any, db_now: datetime) -> bool:
    dt = _coerce_utc_datetime(last_activity_at)
    if dt is None:
        return False
    return db_now >= dt + SESSION_INACTIVITY_TIMEOUT


async def _fetch_db_now(cur: Any) -> datetime:
    """Read database current timestamp (`clock_timestamp()`) after row locks are held."""
    await cur.execute("SELECT clock_timestamp() AS db_now")
    row = await cur.fetchone()
    if row and isinstance(row, dict):
        dt = _coerce_utc_datetime(row.get("db_now"))
        if dt is not None:
            return dt
    return datetime.now(timezone.utc)


async def _lock_or_create_session_row(
    cur: Any,
    *,
    platform: str,
    scope_key: str,
    dalton_user_id: str,
) -> tuple[dict[str, Any], bool, datetime]:
    """Ensure `(platform, scope_key, dalton_user_id)` exists, lock it `FOR UPDATE`, and return `(row, inserted_new, db_now)`."""
    await cur.execute(
        """
        INSERT INTO dalton_channel_session_generations (
            platform, scope_key, dalton_user_id, generation
        )
        VALUES (%s, %s, %s, 1)
        ON CONFLICT (platform, scope_key, dalton_user_id) DO NOTHING
        RETURNING platform
        """,
        (platform, scope_key, dalton_user_id),
    )
    inserted_new = (await cur.fetchone()) is not None
    await cur.execute(
        """
        SELECT generation, override_workspace_id, pending_interrupt,
               last_conversation_activity_at, selection_initialized_at
        FROM dalton_channel_session_generations
        WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
        FOR UPDATE
        """,
        (platform, scope_key, dalton_user_id),
    )
    fetched = await cur.fetchone()
    row = (
        dict(fetched)
        if fetched
        else {
            "generation": 1,
            "override_workspace_id": None,
            "pending_interrupt": None,
            "last_conversation_activity_at": None,
            "selection_initialized_at": None,
        }
    )
    db_now = await _fetch_db_now(cur)
    return row, inserted_new, db_now


def _is_retryable_tx_error(exc: BaseException) -> bool:
    """Return True for `DeadlockDetected` or `UniqueViolation` on `dalton_channel_user_settings_pkey`."""
    if isinstance(exc, psycopg.errors.DeadlockDetected):
        return True
    if isinstance(exc, psycopg.errors.UniqueViolation):
        diag = getattr(exc, "diag", None)
        return getattr(diag, "constraint_name", None) == USER_SETTINGS_PKEY_CONSTRAINT
    return False


async def _run_tx_with_retry(
    op_name: str,
    conn: Any,
    attempt_fn: Callable[[Any], Awaitable[Any]],
    *,
    max_attempts: int = _TX_RETRY_MAX_ATTEMPTS,
) -> Any:
    """Execute `attempt_fn(conn_arg)` with up to `max_attempts` attempts when `conn is None`.

    When `conn is not None`, the caller owns the connection/transaction and no retry is performed.
    When `conn is None`, each retry sleeps briefly (20–100 ms), logs a warning, and re-runs
    `attempt_fn(None)` on a freshly opened connection and transaction from scratch.
    """
    if conn is not None:
        return await attempt_fn(conn)

    for attempt in range(1, max_attempts + 1):
        try:
            return await attempt_fn(None)
        except Exception as exc:
            if not _is_retryable_tx_error(exc) or attempt >= max_attempts:
                raise
            logger.warning(
                "Retrying channel gateway transaction %s after %s (attempt %d/%d)",
                op_name,
                type(exc).__name__,
                attempt,
                max_attempts,
            )
            await asyncio.sleep(
                random.uniform(
                    _TX_RETRY_BACKOFF_MIN_SECONDS, _TX_RETRY_BACKOFF_MAX_SECONDS
                )
            )
    raise RuntimeError("Unreachable")


def hash_bind_code(code: str) -> str:
    """Return SHA-256 hex digest of the normalized uppercase bind code."""
    normalized = str(code or "").strip().upper()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def generate_bind_code_plaintext() -> str:
    """Generate an 8-character high-entropy alphanumeric code formatted as `DLT-XXXX-XXXX`."""
    part1 = "".join(secrets.choice(BIND_CODE_ALPHABET) for _ in range(4))
    part2 = "".join(secrets.choice(BIND_CODE_ALPHABET) for _ in range(4))
    return f"DLT-{part1}-{part2}"


def default_app_settings(platform: str) -> dict[str, Any]:
    """Return default `_App` structure (`src/server/services/channel_settings.py:74-78`)."""
    me_addr = f"{platform}:@me"
    return {
        "preferred": me_addr,
        "chats": {},
        "automation_output": {},
        "agent_messages": {
            "enabled": True,
            "allowed": [me_addr],
        },
    }


def bump_version_str(version: str | None) -> str:
    """Increment string version `'v1'` -> `'v2'`."""
    raw = str(version or "v1").strip()
    if raw.startswith("v") and raw[1:].isdigit():
        return f"v{int(raw[1:]) + 1}"
    if raw.isdigit():
        return f"v{int(raw) + 1}"
    return "v2"


def format_external_thread_id(
    platform: str,
    scope_key: str,
    dalton_user_id: str,
    generation: int,
    chat_type: str = "dm",
) -> str:
    """Format canonical `external_thread_id` per Design §3.2."""
    cleaned_scope = str(scope_key or "").strip()
    if chat_type == "dm" or cleaned_scope.startswith("dm:"):
        if not cleaned_scope.startswith("dm:"):
            cleaned_scope = f"dm:{cleaned_scope}"
        return f"{platform}:{cleaned_scope}:gen:{generation}"
    if not cleaned_scope.startswith("chat:"):
        cleaned_scope = f"chat:{cleaned_scope}"
    return f"{platform}:{cleaned_scope}:user:{dalton_user_id}:gen:{generation}"


def extract_chat_id(
    platform: str,
    *,
    chat_id: str | None = None,
    scope_key: str | None = None,
    chat_address: str | None = None,
) -> str:
    """Extract raw platform `chat_id` from `chat_id`, `scope_key`, or `chat_address`."""
    if chat_id and str(chat_id).strip():
        return str(chat_id).strip()
    s_key = str(scope_key or "").strip()
    if s_key.startswith("chat:"):
        rest = s_key[len("chat:") :]
        return rest.split(":thread:", 1)[0]
    addr = str(chat_address or "").strip()
    prefix = f"{platform}:"
    if addr.startswith(prefix):
        return addr[len(prefix) :]
    return addr


async def _fetch_workspace_row(
    cur: Any, workspace_id: str | None
) -> dict[str, Any] | None:
    ws_uuid = normalize_uuid(workspace_id)
    if not ws_uuid:
        return None
    await cur.execute(
        """
        SELECT workspace_id, user_id, name, status
        FROM workspaces
        WHERE workspace_id = %s AND status != 'deleted'
        """,
        (ws_uuid,),
    )
    row = await cur.fetchone()
    if not row:
        return None
    # ``workspaces`` has no is_home column: a Home is the id derived from its owner.
    from src.server.database.home_workspace import is_home_id

    out = dict(row)
    out["is_home"] = is_home_id(out.get("user_id"), out.get("workspace_id"))
    return out


async def _verify_workspace_write_permission(
    cur: Any,
    workspace_id: str,
    user_id: str,
    *,
    conn: Any = None,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Verify whether `user_id` has write permission on `workspace_id` (own or colleague shared)."""
    ws_row = await _fetch_workspace_row(cur, workspace_id)
    if ws_row is None:
        return False, "workspace_write_denied", None
    owner_id = str(ws_row.get("user_id") or "").strip()
    if owner_id == str(user_id).strip():
        return True, None, ws_row
    if not is_colleague_dispatch_enabled():
        return False, "colleague_dispatch_disabled", ws_row
    try:
        await dalton_sharing.require_workspace_access(
            ws_row,
            user_id,
            channel="web",
            need="write",
            workspace=ws_row,
            conn=conn,
        )
    except HTTPException:
        return False, "workspace_write_denied", ws_row
    return True, None, ws_row


async def _acquire_group_chat_lock(
    cur: Any, platform: str, chat_address: str
) -> None:
    """Acquire transaction-scoped advisory lock per group address so I-1 reads and I-3/I-5/Web group binding mutations see a consistent `(workspace_id, generation)` pair."""
    cleaned_plat = str(platform or "").strip().lower()
    cleaned_addr = str(chat_address or "").strip()
    if cleaned_addr and not cleaned_addr.startswith(f"{cleaned_plat}:"):
        cleaned_addr = f"{cleaned_plat}:{cleaned_addr}"
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"dalton_group_chat:{cleaned_plat}:{cleaned_addr}",),
    )


async def _user_can_manage_or_view_group_chat(
    cur: Any,
    *,
    user_id: str,
    user_tier: str,
    platform: str,
    chat_address: str,
    platform_user_id: str | None = None,
) -> bool:
    """Return True if `user_id` is `admin` or has a bound `platform_user_id` on `platform` with an inbound message record in `chat_address`."""
    if user_tier == "admin":
        return True
    if user_tier == "external":
        return False
    cleaned_plat = str(platform or "").strip().lower()
    cleaned_addr = str(chat_address or "").strip()
    eff_puid = str(platform_user_id or "").strip() if platform_user_id else ""
    if not eff_puid:
        await cur.execute(
            """
            SELECT platform_user_id FROM dalton_channel_identities
            WHERE user_id = %s AND platform = %s
            """,
            (user_id, cleaned_plat),
        )
        id_row = await cur.fetchone()
        if not id_row or not id_row.get("platform_user_id"):
            return False
        eff_puid = str(id_row["platform_user_id"]).strip()

    await cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM dalton_channel_inbound_events
            WHERE platform = %s
              AND chat_address = %s
              AND raw_payload->>'platform_user_id' = %s
        ) AS has_inbound
        """,
        (cleaned_plat, cleaned_addr, eff_puid),
    )
    row = await cur.fetchone()
    return bool(row and row.get("has_inbound"))


async def _verify_group_target_deliverable(
    cur: Any,
    *,
    user_id: str,
    user_tier: str | None = None,
    platform: str,
    chat_address: str,
    apps_cfg: dict[str, Any] | None = None,
    conn: Any = None,
) -> tuple[bool, str | None, str | None]:
    """Verify whether a group target (`platform`, `chat_address`) is currently deliverable for `user_id`.

    Shared between `I-8` (`register_automation_run`) and `I-6` Scenario B (`authorize_and_claim_delivery`).
    """
    cleaned_plat = str(platform or "").strip().lower()
    cleaned_addr = str(chat_address or "").strip()
    eff_tier = user_tier if user_tier is not None else await get_user_tier(user_id)
    if eff_tier == "external" or cleaned_plat == "weixin":
        return False, "Group chat delivery is not allowed for your account.", None

    await cur.execute(
        """
        SELECT chat_name, workspace_id
        FROM dalton_channel_chat_bindings
        WHERE platform = %s AND chat_address = %s
        """,
        (cleaned_plat, cleaned_addr),
    )
    cb_row = await cur.fetchone()

    if apps_cfg is None:
        await cur.execute(
            """
            SELECT apps FROM dalton_channel_user_settings
            WHERE user_id = %s
            """,
            (user_id,),
        )
        us_row = await cur.fetchone()
        apps_cfg = dict(us_row.get("apps") or {}) if us_row else {}

    app_chats = (
        (apps_cfg.get(cleaned_plat) or {}).get("chats")
        if isinstance(apps_cfg.get(cleaned_plat), dict)
        else {}
    ) or {}

    if not cb_row or cleaned_addr not in app_chats:
        return False, "Group chat is not bound in channel settings.", None

    bound_ws = cb_row.get("workspace_id") or (
        app_chats.get(cleaned_addr) or {}
    ).get("workspace_id")
    if bound_ws:
        ok, _, _ = await _verify_workspace_write_permission(
            cur, str(bound_ws), user_id, conn=conn
        )
        if not ok:
            return (
                False,
                "You do not have write access to the target group's workspace.",
                None,
            )

    chat_name = (
        cb_row.get("chat_name")
        or (app_chats.get(cleaned_addr) or {}).get("name")
        or cleaned_addr
    )
    return True, None, str(chat_name)



def _serialize_inbound_row(
    row: dict[str, Any],
    *,
    existing_status: str | None,
    linked: bool,
    allowed: bool,
    deny_reason: str | None,
    user_tier: str | None = None,
    workspace_status: str | None = None,
    workspace_owner_id: str | None = None,
    generation: int | None = None,
    pending_interrupt: Any = None,
) -> dict[str, Any]:
    return {
        "existing_status": existing_status,
        "linked": linked,
        "allowed": allowed,
        "deny_reason": deny_reason,
        "dalton_user_id": row.get("dalton_user_id"),
        "user_tier": user_tier,
        "event_kind": row.get("event_kind") or "message",
        "request_key": str(row["request_key"]) if row.get("request_key") else None,
        "retry_request_key": (
            str(row["retry_request_key"]) if row.get("retry_request_key") else None
        ),
        "resubmitted_request_key": (
            str(row["resubmitted_request_key"])
            if row.get("resubmitted_request_key")
            else None
        ),
        "steered_into_run_id": row.get("steered_into_run_id"),
        "thread_id": str(row["thread_id"]) if row.get("thread_id") else None,
        "run_id": row.get("run_id"),
        "turn_index": row.get("turn_index"),
        "workspace_id": str(row["workspace_id"]) if row.get("workspace_id") else None,
        "workspace_status": workspace_status,
        "workspace_owner_id": workspace_owner_id,
        "external_thread_id": row.get("external_thread_id"),
        "generation": generation,
        "fallback_pending": bool(row.get("fallback_pending", False)),
        "sent_to_current_count": int(row.get("sent_to_current_count") or 0),
        "uploaded_attachments": row.get("uploaded_attachments"),
        "raw_payload": row.get("raw_payload") or {},
        "pending_interrupt": pending_interrupt,
        "command_request": row.get("command_request"),
        "command_result": row.get("command_result"),
        "command_effect_status": row.get("command_effect_status"),
        "command_reply_pending": bool(row.get("command_reply_pending", False)),
    }


# ---------------------------------------------------------------------------
# I-1: Inbound Register & Resolve
# ---------------------------------------------------------------------------


async def register_and_resolve_inbound(
    *,
    platform: str,
    platform_user_id: str,
    chat_type: str,
    chat_address: str,
    thread_scope_key: str,
    message_id: str,
    request_key: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    weixin_context_token: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-1: Idempotently register inbound event and resolve identity, tier, workspace, and generation."""
    return await _run_tx_with_retry(
        "register_and_resolve_inbound",
        conn,
        lambda attempt_conn: _register_and_resolve_inbound_once(
            platform=platform,
            platform_user_id=platform_user_id,
            chat_type=chat_type,
            chat_address=chat_address,
            thread_scope_key=thread_scope_key,
            message_id=message_id,
            request_key=request_key,
            raw_payload=raw_payload,
            weixin_context_token=weixin_context_token,
            conn=attempt_conn,
        ),
    )


async def _register_and_resolve_inbound_once(
    *,
    platform: str,
    platform_user_id: str,
    chat_type: str,
    chat_address: str,
    thread_scope_key: str,
    message_id: str,
    request_key: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    weixin_context_token: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_platform = str(platform or "").strip().lower()
    cleaned_msg_id = str(message_id or "").strip()
    cleaned_chat_type = str(chat_type or "dm").strip().lower()
    cleaned_scope = str(thread_scope_key or "").strip()
    cleaned_address = str(chat_address or "").strip()
    cleaned_puid = str(platform_user_id or "").strip()

    req_uuid = normalize_uuid(request_key) or str(
        uuid5(DALTON_CHANNEL_NAMESPACE, f"{cleaned_platform}:{cleaned_msg_id}")
    )
    payload_dict = dict(raw_payload or {})
    if cleaned_puid:
        payload_dict["platform_user_id"] = cleaned_puid
    if weixin_context_token and "weixin_context_token" not in payload_dict:
        payload_dict["weixin_context_token"] = weixin_context_token

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_inbound:{cleaned_platform}:{cleaned_msg_id}",),
                )
                # 1. Idempotent insert into dalton_channel_inbound_events
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_inbound_events (
                        platform, message_id, request_key, chat_type,
                        chat_address, thread_scope_key, status,
                        fallback_pending, raw_payload, event_kind
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', FALSE, %s, 'message')
                    ON CONFLICT (platform, message_id) DO NOTHING
                    RETURNING *
                    """,
                    (
                        cleaned_platform,
                        cleaned_msg_id,
                        req_uuid,
                        cleaned_chat_type,
                        cleaned_address,
                        cleaned_scope,
                        Jsonb(payload_dict),
                    ),
                )
                inserted = await cur.fetchone()
                if inserted is None:
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (cleaned_platform, cleaned_msg_id),
                    )
                    existing = dict(await cur.fetchone())
                    existing_payload = existing.get("raw_payload")
                    eff_puid = cleaned_puid or (
                        str(existing_payload.get("platform_user_id") or "").strip()
                        if isinstance(existing_payload, dict)
                        else ""
                    )
                    gen_val = existing.get("session_generation")
                    if gen_val is not None:
                        gen_val = int(gen_val)
                    elif (
                        isinstance(existing_payload, dict)
                        and existing_payload.get("generation") is not None
                    ):
                        gen_val = int(existing_payload["generation"])
                    elif existing.get("external_thread_id"):
                        ext_tid = str(existing["external_thread_id"])
                        if ":gen:" in ext_tid:
                            gen_part = ext_tid.rsplit(":gen:", 1)[-1]
                            if gen_part.isdigit():
                                gen_val = int(gen_part)

                    # Re-verify identity on replay
                    ex_uid = existing.get("dalton_user_id")
                    if eff_puid:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_identities
                            WHERE platform = %s AND platform_user_id = %s
                            """,
                            (cleaned_platform, eff_puid),
                        )
                        id_replay = await cur.fetchone()
                        if not id_replay or (
                            ex_uid and str(id_replay["user_id"]) != str(ex_uid)
                        ):
                            return _serialize_inbound_row(
                                existing,
                                existing_status=existing["status"],
                                linked=False,
                                allowed=False,
                                deny_reason="not_linked",
                                generation=gen_val,
                            )
                        ex_uid = str(id_replay["user_id"])

                    if not ex_uid:
                        return _serialize_inbound_row(
                            existing,
                            existing_status=existing["status"],
                            linked=False,
                            allowed=False,
                            deny_reason="not_linked",
                            generation=gen_val,
                        )

                    ex_tier = await get_user_tier(ex_uid)
                    ex_chat_type = str(
                        existing.get("chat_type") or cleaned_chat_type
                    ).strip().lower()
                    if ex_chat_type == "group" and (
                        ex_tier == "external" or cleaned_platform == "weixin"
                    ):
                        return _serialize_inbound_row(
                            existing,
                            existing_status=existing["status"],
                            linked=True,
                            allowed=False,
                            deny_reason="external_member_group_forbidden",
                            user_tier=ex_tier,
                            generation=gen_val,
                        )

                    # Re-verify write permission on the original target workspace (never re-route replay to Home)
                    ex_ws_id = existing.get("workspace_id")
                    ws_status = None
                    ws_owner_id = ex_uid
                    home_ws_id = get_home_workspace_id(ex_uid)
                    if ex_ws_id:
                        if normalize_uuid(ex_ws_id) != normalize_uuid(home_ws_id):
                            ok, deny_reason, ws_row = (
                                await _verify_workspace_write_permission(
                                    cur, str(ex_ws_id), ex_uid, conn=owned_conn
                                )
                            )
                            if not ok or ws_row is None:
                                return _serialize_inbound_row(
                                    existing,
                                    existing_status=existing["status"],
                                    linked=True,
                                    allowed=False,
                                    deny_reason=deny_reason
                                    or "workspace_write_denied",
                                    user_tier=ex_tier,
                                    generation=gen_val,
                                )
                            ws_status = ws_row.get("status")
                            ws_owner_id = str(ws_row.get("user_id") or ex_uid or "")
                        else:
                            ws_row = await _fetch_workspace_row(cur, str(ex_ws_id))
                            if ws_row:
                                ws_status = ws_row.get("status")
                                ws_owner_id = str(
                                    ws_row.get("user_id") or ex_uid or ""
                                )

                    pending_int = existing.get("pending_interrupt_snapshot")
                    await cur.execute(
                        """
                        SELECT generation, pending_interrupt
                        FROM dalton_channel_session_generations
                        WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                        """,
                        (cleaned_platform, existing["thread_scope_key"], ex_uid),
                    )
                    gen_row = await cur.fetchone()
                    if gen_row:
                        cur_gen = int(gen_row.get("generation") or 1)
                        if gen_val is None or cur_gen == gen_val:
                            pending_int = gen_row.get("pending_interrupt")

                    return _serialize_inbound_row(
                        existing,
                        existing_status=existing["status"],
                        linked=bool(ex_uid),
                        allowed=existing["status"] != "ignored",
                        deny_reason=None,
                        user_tier=ex_tier,
                        workspace_status=ws_status,
                        workspace_owner_id=ws_owner_id,
                        generation=gen_val,
                        pending_interrupt=pending_int,
                    )

                inbound_row = dict(inserted)

                # 2. Lookup identity binding
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_identities
                    WHERE platform = %s AND platform_user_id = %s
                    """,
                    (cleaned_platform, cleaned_puid),
                )
                identity = await cur.fetchone()
                if not identity:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET status = 'ignored', updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        RETURNING *
                        """,
                        (cleaned_platform, cleaned_msg_id),
                    )
                    inbound_row = dict(await cur.fetchone())
                    return _serialize_inbound_row(
                        inbound_row,
                        existing_status=None,
                        linked=False,
                        allowed=False,
                        deny_reason="not_linked",
                    )

                dalton_user_id = str(identity["user_id"])
                if weixin_context_token:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_identities
                        SET weixin_context_token = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (weixin_context_token, identity["id"]),
                    )

                # 3. External member group chat veto
                user_tier = await get_user_tier(dalton_user_id)
                if cleaned_chat_type == "group" and (
                    user_tier == "external" or cleaned_platform == "weixin"
                ):
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET dalton_user_id = %s, status = 'ignored', updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        RETURNING *
                        """,
                        (dalton_user_id, cleaned_platform, cleaned_msg_id),
                    )
                    inbound_row = dict(await cur.fetchone())
                    return _serialize_inbound_row(
                        inbound_row,
                        existing_status=None,
                        linked=True,
                        allowed=False,
                        deny_reason="external_member_group_forbidden",
                        user_tier=user_tier,
                    )

                if cleaned_chat_type == "group":
                    await _acquire_group_chat_lock(cur, cleaned_platform, cleaned_address)

                # Lock personal session generation row and evaluate 6-hour inactivity / legacy initialization
                gen_row, inserted_new, db_now = await _lock_or_create_session_row(
                    cur,
                    platform=cleaned_platform,
                    scope_key=cleaned_scope,
                    dalton_user_id=dalton_user_id,
                )
                generation = int(gen_row["generation"])
                override_ws_id = (
                    str(gen_row["override_workspace_id"])
                    if gen_row.get("override_workspace_id")
                    else None
                )
                pending_interrupt = gen_row.get("pending_interrupt")
                selection_init_at = gen_row.get("selection_initialized_at")
                last_activity_at = gen_row.get("last_conversation_activity_at")

                if selection_init_at is None:
                    if not inserted_new and (
                        override_ws_id is not None or cleaned_chat_type == "group"
                    ):
                        generation += 1
                        pending_interrupt = None
                    override_ws_id = None
                elif _is_session_expired(last_activity_at, db_now):
                    if override_ws_id is not None:
                        override_ws_id = None
                        generation += 1
                        pending_interrupt = None

                if (
                    isinstance(pending_interrupt, dict)
                    and pending_interrupt.get("generation") is not None
                    and int(pending_interrupt["generation"]) != generation
                ):
                    pending_interrupt = None

                # 4. Resolve target workspace & write permission (personal selection for both DM and Group)
                target_ws_id: str | None = None
                workspace_status: str | None = None
                workspace_owner_id: str | None = None

                if override_ws_id:
                    ok, deny_reason, ws_row = (
                        await _verify_workspace_write_permission(
                            cur, override_ws_id, dalton_user_id, conn=owned_conn
                        )
                    )
                    if not ok or ws_row is None:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET dalton_user_id = %s, status = 'ignored', updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                            RETURNING *
                            """,
                            (dalton_user_id, cleaned_platform, cleaned_msg_id),
                        )
                        inbound_row = dict(await cur.fetchone())
                        return _serialize_inbound_row(
                            inbound_row,
                            existing_status=None,
                            linked=True,
                            allowed=False,
                            deny_reason=deny_reason or "workspace_write_denied",
                            user_tier=user_tier,
                        )
                    target_ws_id = str(ws_row["workspace_id"])
                    workspace_status = ws_row.get("status")
                    workspace_owner_id = str(ws_row["user_id"])

                if target_ws_id is None:
                    home_ws_id = get_home_workspace_id(dalton_user_id)
                    target_ws_id = home_ws_id
                    home_row = await _fetch_workspace_row(cur, home_ws_id)
                    workspace_status = home_row.get("status") if home_row else None
                    workspace_owner_id = (
                        str(home_row["user_id"]) if home_row else dalton_user_id
                    )

                # Update session row with initialized selection and activity timestamp
                await cur.execute(
                    """
                    UPDATE dalton_channel_session_generations
                    SET generation = %s,
                        override_workspace_id = %s,
                        pending_interrupt = %s,
                        selection_initialized_at = COALESCE(selection_initialized_at, %s),
                        last_conversation_activity_at = %s,
                        updated_at = %s
                    WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                    """,
                    (
                        generation,
                        override_ws_id,
                        Jsonb(pending_interrupt)
                        if pending_interrupt is not None
                        else None,
                        db_now,
                        db_now,
                        db_now,
                        cleaned_platform,
                        cleaned_scope,
                        dalton_user_id,
                    ),
                )

                # 5. Compute external_thread_id and update inbound row
                external_thread_id = format_external_thread_id(
                    cleaned_platform,
                    cleaned_scope,
                    dalton_user_id,
                    generation,
                    cleaned_chat_type,
                )
                payload_dict["generation"] = generation
                await cur.execute(
                    """
                    UPDATE dalton_channel_inbound_events
                    SET dalton_user_id = %s,
                        workspace_id = %s,
                        external_thread_id = %s,
                        session_generation = %s,
                        pending_interrupt_snapshot = %s,
                        raw_payload = %s,
                        updated_at = NOW()
                    WHERE platform = %s AND message_id = %s
                    RETURNING *
                    """,
                    (
                        dalton_user_id,
                        target_ws_id,
                        external_thread_id,
                        generation,
                        Jsonb(pending_interrupt)
                        if pending_interrupt is not None
                        else None,
                        Jsonb(payload_dict),
                        cleaned_platform,
                        cleaned_msg_id,
                    ),
                )
                inbound_row = dict(await cur.fetchone())
                return _serialize_inbound_row(
                    inbound_row,
                    existing_status=None,
                    linked=True,
                    allowed=True,
                    deny_reason=None,
                    user_tier=user_tier,
                    workspace_status=workspace_status,
                    workspace_owner_id=workspace_owner_id,
                    generation=generation,
                    pending_interrupt=pending_interrupt,
                )


# ---------------------------------------------------------------------------
# §1.5 #1 & I-2: Bind Code Generation, Consumption & Unbind
# ---------------------------------------------------------------------------


async def create_bind_code(
    user_id: str,
    platform: str,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Generate a 10-minute `DLT-XXXX-XXXX` bind code, revoking prior unused codes for `(user_id, platform)`."""
    cleaned_uid = str(user_id or "").strip()
    cleaned_platform = str(platform or "").strip().lower()
    if not cleaned_uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    if cleaned_platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform!r}")

    plaintext = generate_bind_code_plaintext()
    code_hash = hash_bind_code(plaintext)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=BIND_CODE_TTL_SECONDS)

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_bind_code:{cleaned_uid}:{cleaned_platform}",),
                )
                await cur.execute(
                    """
                    UPDATE dalton_channel_bind_codes
                    SET used_at = NOW(), revoked_reason = 'superseded'
                    WHERE user_id = %s AND platform = %s AND used_at IS NULL
                    """,
                    (cleaned_uid, cleaned_platform),
                )
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_bind_codes (
                        code_hash, user_id, platform, expires_at
                    )
                    VALUES (%s, %s, %s, %s)
                    """,
                    (code_hash, cleaned_uid, cleaned_platform, expires_at),
                )

    return {
        "code": plaintext,
        "platform": cleaned_platform,
        "expires_at": expires_at.isoformat(),
        "expires_in_seconds": BIND_CODE_TTL_SECONDS,
        "command": f"/bind {plaintext}",
    }


async def _ensure_platform_default_settings(
    cur: Any, user_id: str, platform: str
) -> None:
    await cur.execute(
        """
        SELECT version, apps FROM dalton_channel_user_settings
        WHERE user_id = %s FOR UPDATE
        """,
        (user_id,),
    )
    row = await cur.fetchone()
    default_app = default_app_settings(platform)
    if not row:
        await cur.execute(
            """
            INSERT INTO dalton_channel_user_settings (user_id, version, apps, updated_at)
            VALUES (%s, 'v1', %s, NOW())
            """,
            (user_id, Jsonb({platform: default_app})),
        )
    else:
        apps = dict(row.get("apps") or {})
        apps[platform] = default_app
        next_ver = bump_version_str(row.get("version"))
        await cur.execute(
            """
            UPDATE dalton_channel_user_settings
            SET version = %s, apps = %s, updated_at = NOW()
            WHERE user_id = %s
            """,
            (next_ver, Jsonb(apps), user_id),
        )


async def _bind_identity_in_tx(
    cur: Any,
    *,
    user_id: str,
    platform: str,
    platform_user_id: str,
    platform_username: str | None = None,
    platform_tenant_key: str | None = None,
    weixin_context_token: str | None = None,
) -> int:
    """Bind `(user_id, platform)` to `platform_user_id`, advance existing generations, and initialize settings."""
    # Lock order (same as update_user_channel_settings): personal settings locks
    # first, then session generation rows. Lock the binder's row and every
    # displaced user's row, sorted, before touching any generation row; otherwise
    # a concurrent settings save (settings row -> group lock -> generation rows)
    # and this bind (generation rows -> settings row) deadlock.
    await cur.execute(
        """
        SELECT user_id FROM dalton_channel_identities
        WHERE platform = %s AND platform_user_id = %s AND user_id != %s
        """,
        (platform, platform_user_id, user_id),
    )
    lock_uids = sorted({user_id, *(str(r["user_id"]) for r in await cur.fetchall())})
    for lock_uid in lock_uids:
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"dalton_user_settings:{lock_uid}",),
        )
        await cur.execute(
            """
            SELECT 1 FROM dalton_channel_user_settings
            WHERE user_id = %s FOR UPDATE
            """,
            (lock_uid,),
        )
    # If another Dalton user was previously bound to this same (platform, platform_user_id), unbind them and bump their generation
    await cur.execute(
        """
        DELETE FROM dalton_channel_identities
        WHERE platform = %s AND platform_user_id = %s AND user_id != %s
        RETURNING user_id
        """,
        (platform, platform_user_id, user_id),
    )
    displaced = await cur.fetchall()
    for d_row in displaced:
        old_uid = str(d_row["user_id"])
        await cur.execute(
            """
            SELECT scope_key FROM dalton_channel_session_generations
            WHERE platform = %s AND dalton_user_id = %s
            ORDER BY scope_key
            FOR UPDATE
            """,
            (platform, old_uid),
        )
        await cur.execute(
            """
            UPDATE dalton_channel_session_generations
            SET generation = generation + 1,
                override_workspace_id = NULL,
                pending_interrupt = NULL,
                selection_initialized_at = NOW(),
                last_conversation_activity_at = NULL,
                updated_at = NOW()
            WHERE platform = %s AND dalton_user_id = %s
            """,
            (platform, old_uid),
        )

    await cur.execute(
        """
        INSERT INTO dalton_channel_identities (
            user_id, platform, platform_user_id, platform_username,
            platform_tenant_key, weixin_context_token, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (user_id, platform) DO UPDATE SET
            platform_user_id = EXCLUDED.platform_user_id,
            platform_username = COALESCE(EXCLUDED.platform_username, dalton_channel_identities.platform_username),
            platform_tenant_key = COALESCE(EXCLUDED.platform_tenant_key, dalton_channel_identities.platform_tenant_key),
            weixin_context_token = COALESCE(EXCLUDED.weixin_context_token, dalton_channel_identities.weixin_context_token),
            updated_at = NOW()
        """,
        (
            user_id,
            platform,
            platform_user_id,
            platform_username,
            platform_tenant_key,
            weixin_context_token,
        ),
    )

    # Advance any existing session_generations rows for (platform, user_id)
    await cur.execute(
        """
        SELECT scope_key FROM dalton_channel_session_generations
        WHERE platform = %s AND dalton_user_id = %s
        ORDER BY scope_key
        FOR UPDATE
        """,
        (platform, user_id),
    )
    await cur.execute(
        """
        UPDATE dalton_channel_session_generations
        SET generation = generation + 1,
            override_workspace_id = NULL,
            pending_interrupt = NULL,
            selection_initialized_at = NOW(),
            last_conversation_activity_at = NOW(),
            updated_at = NOW()
        WHERE platform = %s AND dalton_user_id = %s
        """,
        (platform, user_id),
    )

    dm_scope = f"dm:{platform_user_id}"
    await cur.execute(
        """
        INSERT INTO dalton_channel_session_generations (
            platform, scope_key, dalton_user_id, generation, override_workspace_id,
            pending_interrupt, selection_initialized_at, last_conversation_activity_at
        )
        VALUES (%s, %s, %s, 1, NULL, NULL, NOW(), NOW())
        ON CONFLICT (platform, scope_key, dalton_user_id) DO NOTHING
        """,
        (platform, dm_scope, user_id),
    )
    await cur.execute(
        """
        SELECT generation FROM dalton_channel_session_generations
        WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
        """,
        (platform, dm_scope, user_id),
    )
    gen_row = await cur.fetchone()
    current_gen = int(gen_row["generation"]) if gen_row else 1

    await _ensure_platform_default_settings(cur, user_id, platform)
    return current_gen


async def consume_bind_code_or_unbind(
    *,
    action: str,
    platform: str,
    platform_user_id: str | None = None,
    platform_username: str | None = None,
    platform_tenant_key: str | None = None,
    weixin_context_token: str | None = None,
    code: str | None = None,
    is_group: bool = False,
    user_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-2: Consume bind code (with group-leak revocation and cross-platform protection) or unbind."""
    return await _run_tx_with_retry(
        "consume_bind_code_or_unbind",
        conn,
        lambda attempt_conn: _consume_bind_code_or_unbind_once(
            action=action,
            platform=platform,
            platform_user_id=platform_user_id,
            platform_username=platform_username,
            platform_tenant_key=platform_tenant_key,
            weixin_context_token=weixin_context_token,
            code=code,
            is_group=is_group,
            user_id=user_id,
            conn=attempt_conn,
        ),
    )


async def _consume_bind_code_or_unbind_once(
    *,
    action: str,
    platform: str,
    platform_user_id: str | None = None,
    platform_username: str | None = None,
    platform_tenant_key: str | None = None,
    weixin_context_token: str | None = None,
    code: str | None = None,
    is_group: bool = False,
    user_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_action = str(action or "bind").strip().lower()
    cleaned_platform = str(platform or "").strip().lower()

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                if cleaned_action == "bind":
                    cleaned_code = str(code or "").strip().upper()
                    if not cleaned_code:
                        return {"ok": False, "reason": "invalid_or_expired"}
                    code_hash = hash_bind_code(cleaned_code)

                    # 1. Group chat leak: revoke across ALL platforms immediately
                    if is_group:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_bind_codes
                            SET used_at = NOW(), revoked_reason = 'sent_in_group'
                            WHERE code_hash = %s AND used_at IS NULL
                            """,
                            (code_hash,),
                        )
                        return {"ok": False, "reason": "revoked_in_group"}

                    # 2. Private DM bind code consumption
                    await cur.execute(
                        """
                        SELECT code_hash, user_id, platform, expires_at, used_at, revoked_reason
                        FROM dalton_channel_bind_codes
                        WHERE code_hash = %s
                        FOR UPDATE
                        """,
                        (code_hash,),
                    )
                    row = await cur.fetchone()
                    if (
                        not row
                        or row.get("used_at") is not None
                        or row["expires_at"] <= datetime.now(timezone.utc)
                    ):
                        return {"ok": False, "reason": "invalid_or_expired"}

                    row_platform = str(row["platform"]).strip().lower()
                    if row_platform != cleaned_platform:
                        # Do NOT revoke when platform mismatches in DM
                        return {
                            "ok": False,
                            "reason": "platform_mismatch",
                            "expected_platform": row_platform,
                        }

                    cleaned_puid = str(platform_user_id or "").strip()
                    if not cleaned_puid:
                        return {"ok": False, "reason": "missing_platform_user_id"}

                    target_uid = str(row["user_id"])
                    await cur.execute(
                        """
                        UPDATE dalton_channel_bind_codes
                        SET used_at = NOW()
                        WHERE code_hash = %s
                        """,
                        (code_hash,),
                    )
                    current_gen = await _bind_identity_in_tx(
                        cur,
                        user_id=target_uid,
                        platform=cleaned_platform,
                        platform_user_id=cleaned_puid,
                        platform_username=platform_username,
                        platform_tenant_key=platform_tenant_key,
                        weixin_context_token=weixin_context_token,
                    )
                    return {
                        "ok": True,
                        "user_id": target_uid,
                        "platform": cleaned_platform,
                        "platform_user_id": cleaned_puid,
                        "generation": current_gen,
                    }

                if cleaned_action == "unbind":
                    target_uid = str(user_id or "").strip() or None
                    cleaned_puid = str(platform_user_id or "").strip() or None
                    if not target_uid and cleaned_puid:
                        await cur.execute(
                            """
                            SELECT user_id FROM dalton_channel_identities
                            WHERE platform = %s AND platform_user_id = %s
                            """,
                            (cleaned_platform, cleaned_puid),
                        )
                        id_row = await cur.fetchone()
                        if id_row:
                            target_uid = str(id_row["user_id"])

                    if not target_uid:
                        return {"ok": False, "reason": "not_linked"}

                    # Lock personal settings row FIRST (matching bind & update_user_channel_settings lock order)
                    # before deleting identity row or touching session generation rows.
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"dalton_user_settings:{target_uid}",),
                    )
                    await cur.execute(
                        """
                        SELECT version, apps FROM dalton_channel_user_settings
                        WHERE user_id = %s FOR UPDATE
                        """,
                        (target_uid,),
                    )
                    s_row = await cur.fetchone()

                    await cur.execute(
                        """
                        DELETE FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = %s
                        RETURNING platform_user_id
                        """,
                        (target_uid, cleaned_platform),
                    )
                    deleted_id = await cur.fetchone()
                    if not deleted_id and cleaned_puid:
                        dm_scope = f"dm:{cleaned_puid}"
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_session_generations (
                                platform, scope_key, dalton_user_id, generation
                            )
                            VALUES (%s, %s, %s, 1)
                            ON CONFLICT (platform, scope_key, dalton_user_id) DO NOTHING
                            """,
                            (cleaned_platform, dm_scope, target_uid),
                        )
                    elif deleted_id:
                        dm_scope = f"dm:{deleted_id['platform_user_id']}"
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_session_generations (
                                platform, scope_key, dalton_user_id, generation
                            )
                            VALUES (%s, %s, %s, 1)
                            ON CONFLICT (platform, scope_key, dalton_user_id) DO NOTHING
                            """,
                            (cleaned_platform, dm_scope, target_uid),
                        )

                    # Clean up dalton_channel_user_settings.apps[platform]
                    if s_row:
                        apps = dict(s_row.get("apps") or {})
                        if cleaned_platform in apps:
                            apps.pop(cleaned_platform, None)
                            await cur.execute(
                                """
                                UPDATE dalton_channel_user_settings
                                SET version = %s, apps = %s, updated_at = NOW()
                                WHERE user_id = %s
                                """,
                                (
                                    bump_version_str(s_row.get("version")),
                                    Jsonb(apps),
                                    target_uid,
                                ),
                            )

                    # Lock and update dalton_channel_session_generations BEFORE dalton_channel_thread_watches
                    # (matching global lock order: settings -> identities -> session_generations -> thread_watches).
                    await cur.execute(
                        """
                        SELECT scope_key FROM dalton_channel_session_generations
                        WHERE platform = %s AND dalton_user_id = %s
                        ORDER BY scope_key
                        FOR UPDATE
                        """,
                        (cleaned_platform, target_uid),
                    )
                    await cur.execute(
                        """
                        UPDATE dalton_channel_session_generations
                        SET generation = generation + 1,
                            override_workspace_id = NULL,
                            pending_interrupt = NULL,
                            selection_initialized_at = NOW(),
                            last_conversation_activity_at = NULL,
                            updated_at = NOW()
                        WHERE platform = %s AND dalton_user_id = %s
                        RETURNING scope_key, generation
                        """,
                        (cleaned_platform, target_uid),
                    )
                    updated_gens = {
                        str(r["scope_key"]): int(r["generation"])
                        for r in await cur.fetchall()
                    }

                    # Deactivate active thread watches on this platform AFTER locking session_generations
                    await cur.execute(
                        """
                        SELECT thread_id FROM dalton_channel_thread_watches
                        WHERE dalton_user_id = %s AND platform = %s AND watch_active = TRUE
                        ORDER BY thread_id
                        FOR UPDATE
                        """,
                        (target_uid, cleaned_platform),
                    )
                    await cur.execute(
                        """
                        UPDATE dalton_channel_thread_watches
                        SET watch_active = FALSE, updated_at = NOW()
                        WHERE dalton_user_id = %s AND platform = %s AND watch_active = TRUE
                        """,
                        (target_uid, cleaned_platform),
                    )
                    return {
                        "ok": True,
                        "user_id": target_uid,
                        "platform": cleaned_platform,
                        "generations": updated_gens,
                    }

                raise HTTPException(
                    status_code=400, detail=f"Unsupported action: {action!r}"
                )


# ---------------------------------------------------------------------------
# I-3: Session Commands (/workspace, /main, /<workspace_name>, /new, /stop)
# ---------------------------------------------------------------------------


async def _advance_group_chat_generations_in_tx(
    cur: Any,
    *,
    platform: str,
    chat_id: str,
    operator_scope_key: str | None = None,
    operator_user_id: str | None = None,
) -> int | None:
    """Increment `generation` and clear `pending_interrupt` for all rows in a group chat (`chat:<id>` and `chat:<id>:thread:%`)."""
    base_scope = f"chat:{chat_id}"
    eff_scope = operator_scope_key or base_scope
    if operator_user_id:
        await cur.execute(
            """
            INSERT INTO dalton_channel_session_generations (
                platform, scope_key, dalton_user_id, generation
            )
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (platform, scope_key, dalton_user_id) DO NOTHING
            """,
            (platform, eff_scope, operator_user_id),
        )
    await cur.execute(
        """
        UPDATE dalton_channel_session_generations
        SET generation = generation + 1,
            pending_interrupt = NULL,
            updated_at = NOW()
        WHERE platform = %s
          AND (
              scope_key = 'chat:' || %s
              OR scope_key LIKE 'chat:' || %s || ':thread:%%'
          )
        RETURNING scope_key, dalton_user_id, generation
        """,
        (platform, chat_id, chat_id),
    )
    rows = await cur.fetchall()
    if operator_user_id:
        for r in rows:
            if (
                str(r["dalton_user_id"]) == operator_user_id
                and str(r["scope_key"]) == eff_scope
            ):
                return int(r["generation"])
    return int(rows[0]["generation"]) if rows else None


async def execute_session_command(
    *,
    cmd: str,
    platform: str,
    dalton_user_id: str | None = None,
    platform_user_id: str | None = None,
    chat_type: str = "dm",
    chat_address: str | None = None,
    chat_id: str | None = None,
    chat_name: str | None = None,
    scope_key: str,
    message_id: str | None = None,
    request_key: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    weixin_context_token: str | None = None,
    workspace_id: str | None = None,
    workspace_query: str | None = None,
    target_kind: str | None = None,
    external_thread_id: str | None = None,
    is_internal_query: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """I-3: Atomically execute `workspace_list`, `workspace_switch`, `new`, and `stop` with inbound idempotency."""
    cleaned_cmd = str(cmd or "").strip().lower()
    return await _run_tx_with_retry(
        f"execute_session_command:{cleaned_cmd}",
        conn,
        lambda attempt_conn: _execute_session_command_once(
            cmd=cmd,
            platform=platform,
            dalton_user_id=dalton_user_id,
            platform_user_id=platform_user_id,
            chat_type=chat_type,
            chat_address=chat_address,
            chat_id=chat_id,
            chat_name=chat_name,
            scope_key=scope_key,
            message_id=message_id,
            request_key=request_key,
            raw_payload=raw_payload,
            weixin_context_token=weixin_context_token,
            workspace_id=workspace_id,
            workspace_query=workspace_query,
            target_kind=target_kind,
            external_thread_id=external_thread_id,
            is_internal_query=is_internal_query,
            conn=attempt_conn,
        ),
    )


async def _upsert_command_inbound_row(
    cur: Any,
    *,
    platform: str,
    message_id: str,
    request_key: str,
    chat_type: str,
    chat_address: str,
    scope_key: str,
    dalton_user_id: str | None,
    workspace_id: str | None,
    external_thread_id: str | None,
    session_generation: int | None,
    thread_id: str | None,
    run_id: str | None,
    status: str,
    command_request: dict[str, Any],
    command_result: dict[str, Any],
    command_effect_status: str,
    command_reply_pending: bool,
    raw_payload: dict[str, Any],
) -> None:
    await cur.execute(
        """
        INSERT INTO dalton_channel_inbound_events (
            platform, message_id, request_key, chat_type,
            chat_address, thread_scope_key, dalton_user_id,
            workspace_id, external_thread_id, session_generation,
            thread_id, run_id, status, fallback_pending,
            raw_payload, event_kind, command_request, command_result,
            command_effect_status, command_reply_pending, updated_at
        )
        VALUES (
            %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, FALSE,
            %s, 'command', %s, %s,
            %s, %s, NOW()
        )
        ON CONFLICT (platform, message_id) DO UPDATE SET
            dalton_user_id = COALESCE(EXCLUDED.dalton_user_id, dalton_channel_inbound_events.dalton_user_id),
            workspace_id = COALESCE(EXCLUDED.workspace_id, dalton_channel_inbound_events.workspace_id),
            external_thread_id = COALESCE(EXCLUDED.external_thread_id, dalton_channel_inbound_events.external_thread_id),
            session_generation = COALESCE(EXCLUDED.session_generation, dalton_channel_inbound_events.session_generation),
            thread_id = COALESCE(EXCLUDED.thread_id, dalton_channel_inbound_events.thread_id),
            run_id = COALESCE(EXCLUDED.run_id, dalton_channel_inbound_events.run_id),
            status = EXCLUDED.status,
            raw_payload = EXCLUDED.raw_payload,
            event_kind = 'command',
            command_request = EXCLUDED.command_request,
            command_result = EXCLUDED.command_result,
            command_effect_status = EXCLUDED.command_effect_status,
            command_reply_pending = EXCLUDED.command_reply_pending,
            updated_at = NOW()
        """,
        (
            platform,
            message_id,
            request_key,
            chat_type,
            chat_address,
            scope_key,
            dalton_user_id,
            normalize_uuid(workspace_id) if workspace_id else None,
            external_thread_id,
            session_generation,
            normalize_uuid(thread_id) if thread_id else None,
            run_id,
            status,
            Jsonb(raw_payload),
            Jsonb(command_request),
            Jsonb(command_result),
            command_effect_status,
            command_reply_pending,
        ),
    )


async def _execute_session_command_once(
    *,
    cmd: str,
    platform: str,
    dalton_user_id: str | None = None,
    platform_user_id: str | None = None,
    chat_type: str = "dm",
    chat_address: str | None = None,
    chat_id: str | None = None,
    chat_name: str | None = None,
    scope_key: str,
    message_id: str | None = None,
    request_key: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    weixin_context_token: str | None = None,
    workspace_id: str | None = None,
    workspace_query: str | None = None,
    target_kind: str | None = None,
    external_thread_id: str | None = None,
    is_internal_query: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_cmd = str(cmd or "").strip().lower()
    if cleaned_cmd not in ("workspace_list", "workspace_switch", "new", "stop"):
        raise HTTPException(
            status_code=400, detail=f"Unsupported session command: {cmd!r}"
        )

    cleaned_platform = str(platform or "").strip().lower()
    cleaned_uid = str(dalton_user_id or "").strip()
    cleaned_puid = str(platform_user_id or "").strip()
    cleaned_chat_type = str(chat_type or "dm").strip().lower()
    cleaned_scope = str(scope_key or "").strip()
    cleaned_address = str(chat_address or f"{cleaned_platform}:@me").strip()
    cleaned_msg_id = str(message_id or "").strip() or None
    cleaned_target_kind = str(target_kind or "").strip().lower() or None

    req_uuid = (
        normalize_uuid(request_key)
        or (
            str(uuid5(DALTON_CHANNEL_NAMESPACE, f"{cleaned_platform}:{cleaned_msg_id}"))
            if cleaned_msg_id
            else str(uuid5(DALTON_CHANNEL_NAMESPACE, f"{cleaned_platform}:{cleaned_scope}:{cleaned_cmd}"))
        )
    )
    payload_dict = dict(raw_payload or {})
    if cleaned_puid:
        payload_dict["platform_user_id"] = cleaned_puid
    if weixin_context_token and "weixin_context_token" not in payload_dict:
        payload_dict["weixin_context_token"] = weixin_context_token

    cmd_req_dict: dict[str, Any] = {
        "cmd": cleaned_cmd,
        "workspace_id": workspace_id,
        "workspace_query": workspace_query,
        "target_kind": cleaned_target_kind,
        "external_thread_id": external_thread_id,
    }

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                existing_inbound: dict[str, Any] | None = None
                orig_uid = ""
                orig_puid = ""
                if cleaned_msg_id:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"dalton_inbound:{cleaned_platform}:{cleaned_msg_id}",),
                    )
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (cleaned_platform, cleaned_msg_id),
                    )
                    ex_row = await cur.fetchone()
                    if ex_row:
                        existing_inbound = dict(ex_row)
                        orig_uid = str(
                            existing_inbound.get("dalton_user_id") or ""
                        ).strip()
                        if isinstance(existing_inbound.get("raw_payload"), dict):
                            orig_puid = str(
                                existing_inbound["raw_payload"].get("platform_user_id")
                                or ""
                            ).strip()
                            if not cleaned_puid and orig_puid:
                                cleaned_puid = orig_puid
                                payload_dict["platform_user_id"] = cleaned_puid

                async def _deny_and_ignore_command(
                    res_payload: dict[str, Any], *, uid_val: str | None
                ) -> dict[str, Any]:
                    if cleaned_msg_id:
                        await _upsert_command_inbound_row(
                            cur,
                            platform=cleaned_platform,
                            message_id=cleaned_msg_id,
                            request_key=req_uuid,
                            chat_type=cleaned_chat_type,
                            chat_address=cleaned_address,
                            scope_key=cleaned_scope,
                            dalton_user_id=uid_val,
                            workspace_id=None,
                            external_thread_id=None,
                            session_generation=None,
                            thread_id=None,
                            run_id=None,
                            status="ignored",
                            command_request=cmd_req_dict,
                            command_result=res_payload,
                            command_effect_status="done",
                            command_reply_pending=False,
                            raw_payload=payload_dict,
                        )
                    return res_payload

                # 1. Identity verification (including re-authorization on redelivery / startup recovery)
                if orig_puid and cleaned_puid and orig_puid != cleaned_puid:
                    return await _deny_and_ignore_command(
                        {
                            "ok": False,
                            "linked": False,
                            "allowed": False,
                            "deny_reason": "not_linked",
                        },
                        uid_val=None,
                    )

                if cleaned_puid:
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_identities
                        WHERE platform = %s AND platform_user_id = %s
                        """,
                        (cleaned_platform, cleaned_puid),
                    )
                    identity = await cur.fetchone()
                    if not identity:
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            },
                            uid_val=None,
                        )
                    resolved_identity_uid = str(identity["user_id"])
                    if (orig_uid and orig_uid != resolved_identity_uid) or (
                        cleaned_uid and cleaned_uid != resolved_identity_uid
                    ):
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            },
                            uid_val=None,
                        )
                    cleaned_uid = resolved_identity_uid
                    if weixin_context_token:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_identities
                            SET weixin_context_token = %s, updated_at = NOW()
                            WHERE id = %s
                            """,
                            (weixin_context_token, identity["id"]),
                        )
                elif cleaned_msg_id or existing_inbound is not None:
                    check_uid = cleaned_uid or orig_uid
                    if not check_uid or (
                        orig_uid and cleaned_uid and orig_uid != cleaned_uid
                    ):
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            },
                            uid_val=None,
                        )
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = %s
                        """,
                        (check_uid, cleaned_platform),
                    )
                    identity = await cur.fetchone()
                    if not identity:
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            },
                            uid_val=None,
                        )
                    if (
                        orig_puid
                        and str(identity.get("platform_user_id") or "").strip()
                        != orig_puid
                    ):
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            },
                            uid_val=None,
                        )
                    cleaned_uid = check_uid

                if not cleaned_uid:
                    return {
                        "ok": False,
                        "linked": False,
                        "allowed": False,
                        "deny_reason": "not_linked",
                    }

                # 2. Tier & group chat check
                user_tier = await get_user_tier(cleaned_uid)
                if cleaned_chat_type == "group" and (
                    user_tier == "external" or cleaned_platform == "weixin"
                ):
                    return await _deny_and_ignore_command(
                        {
                            "ok": False,
                            "linked": True,
                            "allowed": False,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "deny_reason": "external_member_group_forbidden",
                        },
                        uid_val=cleaned_uid,
                    )

                home_ws_id = get_home_workspace_id(cleaned_uid)

                # 3. Idempotent return for a command that already has a result for this message_id.
                # Success re-checks live write permission. Any other stored result is final.
                if existing_inbound is not None and isinstance(
                    existing_inbound.get("command_result"), dict
                ):
                    stored_res = dict(existing_inbound["command_result"])
                    if stored_res.get("ok") is True:
                        if cleaned_cmd == "workspace_list":
                            writable_names = await _get_writable_workspace_names(
                                cleaned_uid
                            )
                            stored_res["workspaces"] = [
                                {"workspace_id": wid, "name": name}
                                for wid, name in writable_names.items()
                            ]
                            cur_wid = str(
                                stored_res.get("current_workspace_id") or ""
                            ).strip()
                            if (
                                not stored_res.get("is_home")
                                and cur_wid
                                and normalize_uuid(cur_wid)
                                != normalize_uuid(home_ws_id)
                            ):
                                if cur_wid in writable_names:
                                    stored_res["current_workspace_name"] = (
                                        writable_names[cur_wid]
                                    )
                                else:
                                    ok, _, ws_row = (
                                        await _verify_workspace_write_permission(
                                            cur,
                                            cur_wid,
                                            cleaned_uid,
                                            conn=owned_conn,
                                        )
                                    )
                                    if not ok or ws_row is None:
                                        stored_res["current_workspace_id"] = (
                                            home_ws_id
                                        )
                                        stored_res["current_workspace_name"] = (
                                            CHIEF_OF_STAFF_DISPLAY_NAME
                                        )
                                        stored_res["is_home"] = True
                        else:
                            target_ws_id = stored_res.get("override_workspace_id") or (
                                None
                                if stored_res.get("is_home")
                                else stored_res.get("workspace_id")
                            )
                            if target_ws_id and normalize_uuid(
                                target_ws_id
                            ) != normalize_uuid(home_ws_id):
                                ok, perm_deny, _ = (
                                    await _verify_workspace_write_permission(
                                        cur,
                                        str(target_ws_id),
                                        cleaned_uid,
                                        conn=owned_conn,
                                    )
                                )
                                if not ok:
                                    denied_dup = await _deny_and_ignore_command(
                                        {
                                            "ok": False,
                                            "linked": True,
                                            "allowed": False,
                                            "dalton_user_id": cleaned_uid,
                                            "user_tier": user_tier,
                                            "deny_reason": perm_deny
                                            or "workspace_write_denied",
                                        },
                                        uid_val=cleaned_uid,
                                    )
                                    return {
                                        **denied_dup,
                                        "duplicate": True,
                                        "command_effect_status": "done",
                                        "command_reply_pending": False,
                                        "existing_status": "ignored",
                                    }

                        eff_stored_status = (
                            existing_inbound.get("command_effect_status") or "done"
                        )
                        if eff_stored_status == "pending_cancel" and not (
                            stored_res.get("thread_id") and stored_res.get("run_id")
                        ):
                            eff_stored_status = "done"
                            if cleaned_msg_id:
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_inbound_events
                                    SET command_effect_status = 'done',
                                        updated_at = NOW()
                                    WHERE platform = %s AND message_id = %s
                                    """,
                                    (cleaned_platform, cleaned_msg_id),
                                )
                        return {
                            **stored_res,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "duplicate": True,
                            "command_effect_status": eff_stored_status,
                            "command_reply_pending": bool(
                                existing_inbound.get("command_reply_pending", False)
                            ),
                            "existing_status": existing_inbound.get("status"),
                        }

                    # A stored denial or other non-success is final for this message_id.
                    # Resending it restores an unsent reply and does not run the command again,
                    # so a later rename or workspace switch cannot turn the old event into a new selection.
                    return {
                        **stored_res,
                        "dalton_user_id": cleaned_uid,
                        "user_tier": user_tier,
                        "duplicate": True,
                        "command_effect_status": existing_inbound.get(
                            "command_effect_status"
                        )
                        or "done",
                        "command_reply_pending": bool(
                            existing_inbound.get("command_reply_pending", False)
                        ),
                        "existing_status": existing_inbound.get("status"),
                    }

                # Read-only internal query path (e.g. GET /agent/session when message_id is omitted)
                if cleaned_cmd == "workspace_list" and is_internal_query and not cleaned_msg_id:
                    writable_names = await _get_writable_workspace_names(cleaned_uid)
                    await cur.execute(
                        """
                        SELECT generation, override_workspace_id,
                               last_conversation_activity_at, selection_initialized_at
                        FROM dalton_channel_session_generations
                        WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                        """,
                        (cleaned_platform, cleaned_scope, cleaned_uid),
                    )
                    g_row = await cur.fetchone()
                    db_now = await _fetch_db_now(cur)
                    eff_override_ws: str | None = None
                    gen_val = 1
                    if g_row:
                        gen_val = int(g_row.get("generation") or 1)
                        raw_ov = (
                            str(g_row["override_workspace_id"])
                            if g_row.get("override_workspace_id")
                            else None
                        )
                        init_at = g_row.get("selection_initialized_at")
                        act_at = g_row.get("last_conversation_activity_at")
                        if init_at is not None and not _is_session_expired(act_at, db_now):
                            eff_override_ws = raw_ov
                    current_ws_id = eff_override_ws or home_ws_id
                    current_ws_name = (
                        CHIEF_OF_STAFF_DISPLAY_NAME
                        if eff_override_ws is None
                        else (writable_names.get(eff_override_ws) or eff_override_ws)
                    )
                    return {
                        "ok": True,
                        "linked": True,
                        "allowed": True,
                        "dalton_user_id": cleaned_uid,
                        "user_tier": user_tier,
                        "current_workspace_id": current_ws_id,
                        "current_workspace_name": current_ws_name,
                        "is_home": eff_override_ws is None,
                        "home_workspace_id": home_ws_id,
                        "generation": gen_val,
                        "workspaces": [
                            {"workspace_id": wid, "name": name}
                            for wid, name in writable_names.items()
                        ],
                    }

                if cleaned_chat_type == "group":
                    eff_chat_id = extract_chat_id(
                        cleaned_platform,
                        chat_id=chat_id,
                        scope_key=cleaned_scope,
                        chat_address=cleaned_address,
                    )
                    eff_address = (
                        cleaned_address
                        if cleaned_address and not cleaned_address.endswith(":@me")
                        else f"{cleaned_platform}:{eff_chat_id}"
                    )
                    await _acquire_group_chat_lock(cur, cleaned_platform, eff_address)

                gen_row, inserted_new, db_now = await _lock_or_create_session_row(
                    cur,
                    platform=cleaned_platform,
                    scope_key=cleaned_scope,
                    dalton_user_id=cleaned_uid,
                )
                generation = int(gen_row["generation"])
                override_ws_id = (
                    str(gen_row["override_workspace_id"])
                    if gen_row.get("override_workspace_id")
                    else None
                )
                pending_interrupt = gen_row.get("pending_interrupt")
                selection_init_at = gen_row.get("selection_initialized_at")
                last_activity_at = gen_row.get("last_conversation_activity_at")
                is_uninitialized = selection_init_at is None
                is_expired = _is_session_expired(last_activity_at, db_now)

                cmd_result: dict[str, Any]
                effect_status = "done"

                if cleaned_cmd == "workspace_list":
                    writable_names = await _get_writable_workspace_names(cleaned_uid)
                    if is_uninitialized:
                        if not inserted_new and (
                            override_ws_id is not None or cleaned_chat_type == "group"
                        ):
                            generation += 1
                            pending_interrupt = None
                        override_ws_id = None
                    elif is_expired:
                        if override_ws_id is not None:
                            override_ws_id = None
                            generation += 1
                            pending_interrupt = None

                    await cur.execute(
                        """
                        UPDATE dalton_channel_session_generations
                        SET generation = %s,
                            override_workspace_id = %s,
                            pending_interrupt = %s,
                            selection_initialized_at = COALESCE(selection_initialized_at, %s),
                            last_conversation_activity_at = %s,
                            updated_at = %s
                        WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                        """,
                        (
                            generation,
                            override_ws_id,
                            Jsonb(pending_interrupt)
                            if pending_interrupt is not None
                            else None,
                            db_now,
                            db_now,
                            db_now,
                            cleaned_platform,
                            cleaned_scope,
                            cleaned_uid,
                        ),
                    )
                    current_ws_id = override_ws_id or home_ws_id
                    if override_ws_id is None:
                        current_ws_name = CHIEF_OF_STAFF_DISPLAY_NAME
                    else:
                        current_ws_name = writable_names.get(override_ws_id)
                        if not current_ws_name:
                            w_row = await _fetch_workspace_row(cur, override_ws_id)
                            current_ws_name = (
                                str(w_row["name"])
                                if w_row and w_row.get("name")
                                else override_ws_id
                            )
                    ext_id = format_external_thread_id(
                        cleaned_platform,
                        cleaned_scope,
                        cleaned_uid,
                        generation,
                        cleaned_chat_type,
                    )
                    cmd_result = {
                        "ok": True,
                        "linked": True,
                        "allowed": True,
                        "dalton_user_id": cleaned_uid,
                        "user_tier": user_tier,
                        "current_workspace_id": current_ws_id,
                        "current_workspace_name": current_ws_name,
                        "is_home": override_ws_id is None,
                        "home_workspace_id": home_ws_id,
                        "generation": generation,
                        "external_thread_id": ext_id,
                        "workspaces": [
                            {"workspace_id": wid, "name": name}
                            for wid, name in writable_names.items()
                        ],
                    }

                elif cleaned_cmd == "workspace_switch":
                    raw_id = str(workspace_id or "").strip()
                    raw_query = str(workspace_query or "").strip()
                    raw_target = raw_id or raw_query
                    is_home_target = (
                        cleaned_target_kind == "home"
                        or (
                            cleaned_target_kind not in ("shortcut_name", "exact_name")
                            and (
                                raw_target.lower()
                                in ("", "home", "main", "chief of staff", "none", "null")
                                or normalize_uuid(raw_target) == home_ws_id
                            )
                        )
                    )
                    resolved_ws_id: str | None = None
                    resolved_ws_name = CHIEF_OF_STAFF_DISPLAY_NAME
                    deny_reason: str | None = None
                    candidates: list[dict[str, str]] | None = None

                    if not is_home_target:
                        candidate_uuid = (
                            normalize_uuid(raw_id or raw_target)
                            if cleaned_target_kind not in ("shortcut_name", "exact_name")
                            else None
                        )
                        if candidate_uuid:
                            if candidate_uuid == home_ws_id:
                                is_home_target = True
                            else:
                                ok, perm_deny, ws_row = (
                                    await _verify_workspace_write_permission(
                                        cur,
                                        candidate_uuid,
                                        cleaned_uid,
                                        conn=owned_conn,
                                    )
                                )
                                if not ok or ws_row is None:
                                    deny_reason = (
                                        perm_deny or "workspace_write_denied"
                                    )
                                else:
                                    resolved_ws_id = str(ws_row["workspace_id"])
                                    resolved_ws_name = str(
                                        ws_row.get("name") or resolved_ws_id
                                    )
                        else:
                            writable_names = await _get_writable_workspace_names(
                                cleaned_uid
                            )
                            q_exact = raw_target.strip()
                            q_norm = q_exact.lower()
                            exact_matches = [
                                (wid, wname)
                                for wid, wname in writable_names.items()
                                if wname.strip() == q_exact
                            ]
                            matches = (
                                exact_matches
                                if exact_matches
                                else [
                                    (wid, wname)
                                    for wid, wname in writable_names.items()
                                    if wname.strip().lower() == q_norm
                                ]
                            )
                            if len(matches) > 1:
                                deny_reason = "workspace_ambiguous"
                                candidates = [
                                    {"workspace_id": wid, "name": wname}
                                    for wid, wname in matches
                                ]
                            elif len(matches) == 1:
                                matched_wid, matched_name = matches[0]
                                ok, perm_deny, ws_row = (
                                    await _verify_workspace_write_permission(
                                        cur,
                                        matched_wid,
                                        cleaned_uid,
                                        conn=owned_conn,
                                    )
                                )
                                if not ok or ws_row is None:
                                    deny_reason = (
                                        perm_deny or "workspace_write_denied"
                                    )
                                else:
                                    resolved_ws_id = str(ws_row["workspace_id"])
                                    resolved_ws_name = str(
                                        ws_row.get("name")
                                        or matched_name
                                        or resolved_ws_id
                                    )
                            else:
                                deny_reason = "workspace_not_found"

                    if deny_reason is not None:
                        cmd_result = {
                            "ok": False,
                            "linked": True,
                            "allowed": False,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "deny_reason": deny_reason,
                            "workspace_query": raw_target,
                        }
                        if candidates is not None:
                            cmd_result["candidates"] = candidates
                    else:
                        if is_uninitialized:
                            if resolved_ws_id is not None:
                                switched = True
                            elif not inserted_new and (
                                override_ws_id is not None
                                or cleaned_chat_type == "group"
                            ):
                                switched = True
                            else:
                                switched = False
                        elif is_expired:
                            if override_ws_id is not None or resolved_ws_id is not None:
                                switched = True
                            else:
                                switched = False
                        else:
                            switched = normalize_uuid(
                                override_ws_id
                            ) != normalize_uuid(resolved_ws_id)

                        if switched:
                            new_gen = 2 if inserted_new else generation + 1
                            await cur.execute(
                                """
                                UPDATE dalton_channel_session_generations
                                SET generation = %s,
                                    override_workspace_id = %s,
                                    pending_interrupt = NULL,
                                    selection_initialized_at = COALESCE(selection_initialized_at, %s),
                                    last_conversation_activity_at = %s,
                                    updated_at = %s
                                WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                                """,
                                (
                                    new_gen,
                                    resolved_ws_id,
                                    db_now,
                                    db_now,
                                    db_now,
                                    cleaned_platform,
                                    cleaned_scope,
                                    cleaned_uid,
                                ),
                            )
                        else:
                            new_gen = generation
                            await cur.execute(
                                """
                                UPDATE dalton_channel_session_generations
                                SET generation = %s,
                                    override_workspace_id = %s,
                                    pending_interrupt = %s,
                                    selection_initialized_at = COALESCE(selection_initialized_at, %s),
                                    last_conversation_activity_at = %s,
                                    updated_at = %s
                                WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                                """,
                                (
                                    new_gen,
                                    resolved_ws_id,
                                    Jsonb(pending_interrupt)
                                    if pending_interrupt is not None
                                    else None,
                                    db_now,
                                    db_now,
                                    db_now,
                                    cleaned_platform,
                                    cleaned_scope,
                                    cleaned_uid,
                                ),
                            )
                        ext_id = format_external_thread_id(
                            cleaned_platform,
                            cleaned_scope,
                            cleaned_uid,
                            new_gen,
                            cleaned_chat_type,
                        )
                        cmd_result = {
                            "ok": True,
                            "linked": True,
                            "allowed": True,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "workspace_id": resolved_ws_id or home_ws_id,
                            "workspace_name": resolved_ws_name,
                            "is_home": resolved_ws_id is None,
                            "override_workspace_id": resolved_ws_id,
                            "home_workspace_id": home_ws_id,
                            "generation": new_gen,
                            "external_thread_id": ext_id,
                            "switched": switched,
                        }

                elif cleaned_cmd == "new":
                    eff_override_ws = (
                        None if (is_uninitialized or is_expired) else override_ws_id
                    )
                    deny_reason = None
                    ws_name = CHIEF_OF_STAFF_DISPLAY_NAME
                    if eff_override_ws is not None:
                        ok, perm_deny, ws_row = (
                            await _verify_workspace_write_permission(
                                cur, eff_override_ws, cleaned_uid, conn=owned_conn
                            )
                        )
                        if not ok or ws_row is None:
                            deny_reason = perm_deny or "workspace_write_denied"
                        else:
                            eff_override_ws = str(ws_row["workspace_id"])
                            ws_name = str(ws_row.get("name") or eff_override_ws)

                    if deny_reason is not None:
                        cmd_result = {
                            "ok": False,
                            "linked": True,
                            "allowed": False,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "deny_reason": deny_reason,
                        }
                    else:
                        new_gen = 2 if inserted_new else generation + 1
                        await cur.execute(
                            """
                            UPDATE dalton_channel_session_generations
                            SET generation = %s,
                                override_workspace_id = %s,
                                pending_interrupt = NULL,
                                selection_initialized_at = COALESCE(selection_initialized_at, %s),
                                last_conversation_activity_at = %s,
                                updated_at = %s
                            WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                            """,
                            (
                                new_gen,
                                eff_override_ws,
                                db_now,
                                db_now,
                                db_now,
                                cleaned_platform,
                                cleaned_scope,
                                cleaned_uid,
                            ),
                        )
                        ext_id = format_external_thread_id(
                            cleaned_platform,
                            cleaned_scope,
                            cleaned_uid,
                            new_gen,
                            cleaned_chat_type,
                        )
                        cmd_result = {
                            "ok": True,
                            "linked": True,
                            "allowed": True,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "workspace_id": eff_override_ws or home_ws_id,
                            "workspace_name": ws_name,
                            "is_home": eff_override_ws is None,
                            "override_workspace_id": eff_override_ws,
                            "home_workspace_id": home_ws_id,
                            "generation": new_gen,
                            "external_thread_id": ext_id,
                        }

                else:  # cleaned_cmd == "stop"
                    expired_switched_to_home = False
                    if is_uninitialized:
                        if not inserted_new and (
                            override_ws_id is not None or cleaned_chat_type == "group"
                        ):
                            generation += 1
                            pending_interrupt = None
                            expired_switched_to_home = True
                        override_ws_id = None
                    elif is_expired:
                        if override_ws_id is not None:
                            override_ws_id = None
                            generation += 1
                            pending_interrupt = None
                            expired_switched_to_home = True

                    deny_reason = None
                    if override_ws_id is not None:
                        ok, perm_deny, ws_row = (
                            await _verify_workspace_write_permission(
                                cur, override_ws_id, cleaned_uid, conn=owned_conn
                            )
                        )
                        if not ok or ws_row is None:
                            deny_reason = perm_deny or "workspace_write_denied"

                    if deny_reason is not None:
                        cmd_result = {
                            "ok": False,
                            "linked": True,
                            "allowed": False,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "deny_reason": deny_reason,
                        }
                    else:
                        if expired_switched_to_home:
                            ext_id = format_external_thread_id(
                                cleaned_platform,
                                cleaned_scope,
                                cleaned_uid,
                                generation,
                                cleaned_chat_type,
                            )
                        else:
                            ext_id = (external_thread_id or "").strip() or format_external_thread_id(
                                cleaned_platform,
                                cleaned_scope,
                                cleaned_uid,
                                generation,
                                cleaned_chat_type,
                            )

                        thread_id: str | None = None
                        active_run_id: str | None = None
                        if not expired_switched_to_home:
                            thread_id = await lookup_thread_by_external_id(
                                cleaned_platform, ext_id, cleaned_uid
                            )
                            if not thread_id:
                                await cur.execute(
                                    """
                                    SELECT thread_id FROM dalton_channel_inbound_events
                                    WHERE platform = %s AND dalton_user_id = %s
                                      AND external_thread_id = %s AND thread_id IS NOT NULL
                                    ORDER BY created_at DESC
                                    LIMIT 1
                                    """,
                                    (cleaned_platform, cleaned_uid, ext_id),
                                )
                                in_row = await cur.fetchone()
                                if in_row and in_row.get("thread_id"):
                                    thread_id = str(in_row["thread_id"])

                            if thread_id:
                                await cur.execute(
                                    """
                                    SELECT conversation_response_id
                                    FROM conversation_responses
                                    WHERE conversation_thread_id = %s AND status = 'in_progress'
                                    ORDER BY run_seq DESC
                                    LIMIT 1
                                    """,
                                    (thread_id,),
                                )
                                r_row = await cur.fetchone()
                                if r_row and r_row.get("conversation_response_id"):
                                    active_run_id = str(
                                        r_row["conversation_response_id"]
                                    )
                                else:
                                    await cur.execute(
                                        """
                                        SELECT run_id FROM dalton_channel_inbound_events
                                        WHERE thread_id = %s AND status IN ('accepted', 'streaming', 'dispatched')
                                          AND run_id IS NOT NULL
                                        ORDER BY created_at DESC
                                        LIMIT 1
                                        """,
                                        (thread_id,),
                                    )
                                    in_run = await cur.fetchone()
                                    if in_run and in_run.get("run_id"):
                                        active_run_id = str(in_run["run_id"])
                                    else:
                                        await cur.execute(
                                            """
                                            SELECT run_id FROM dalton_channel_background_runs
                                            WHERE thread_id = %s AND status IN ('running', 'streaming')
                                              AND run_id IS NOT NULL
                                            ORDER BY created_at DESC
                                            LIMIT 1
                                            """,
                                            (thread_id,),
                                        )
                                        bg_run = await cur.fetchone()
                                        if bg_run and bg_run.get("run_id"):
                                            active_run_id = str(bg_run["run_id"])

                        effect_status = (
                            "pending_cancel"
                            if (thread_id and active_run_id and cleaned_msg_id)
                            else "done"
                        )
                        await cur.execute(
                            """
                            UPDATE dalton_channel_session_generations
                            SET generation = %s,
                                override_workspace_id = %s,
                                pending_interrupt = %s,
                                selection_initialized_at = COALESCE(selection_initialized_at, %s),
                                last_conversation_activity_at = %s,
                                updated_at = %s
                            WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                            """,
                            (
                                generation,
                                override_ws_id,
                                Jsonb(pending_interrupt)
                                if pending_interrupt is not None
                                else None,
                                db_now,
                                db_now,
                                db_now,
                                cleaned_platform,
                                cleaned_scope,
                                cleaned_uid,
                            ),
                        )
                        cmd_result = {
                            "ok": True,
                            "linked": True,
                            "allowed": True,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "workspace_id": override_ws_id or home_ws_id,
                            "is_home": override_ws_id is None,
                            "generation": generation,
                            "thread_id": thread_id,
                            "run_id": active_run_id,
                            "external_thread_id": ext_id,
                        }

                if cleaned_msg_id:
                    await _upsert_command_inbound_row(
                        cur,
                        platform=cleaned_platform,
                        message_id=cleaned_msg_id,
                        request_key=req_uuid,
                        chat_type=cleaned_chat_type,
                        chat_address=cleaned_address,
                        scope_key=cleaned_scope,
                        dalton_user_id=cleaned_uid,
                        workspace_id=cmd_result.get("workspace_id")
                        or cmd_result.get("current_workspace_id")
                        or home_ws_id,
                        external_thread_id=cmd_result.get("external_thread_id"),
                        session_generation=cmd_result.get("generation") or generation,
                        thread_id=cmd_result.get("thread_id"),
                        run_id=cmd_result.get("run_id"),
                        status="accepted",
                        command_request=cmd_req_dict,
                        command_result=cmd_result,
                        command_effect_status=effect_status,
                        command_reply_pending=True,
                        raw_payload=payload_dict,
                    )

                return {
                    **cmd_result,
                    "duplicate": False,
                    "command_effect_status": effect_status,
                    "command_reply_pending": bool(cleaned_msg_id),
                }


# ---------------------------------------------------------------------------
# I-4: Internal State Query
# ---------------------------------------------------------------------------


async def get_channel_internal_state(
    *,
    user_id: str,
    thread_id: str | None = None,
    run_id: str | None = None,
    platform: str | None = None,
    external_thread_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-4: Return user tier, home_workspace_id, identities, settings, chat_bindings, and current conversation route."""
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        raise HTTPException(status_code=400, detail="user_id is required")

    user_tier = await get_user_tier(cleaned_uid)
    home_ws_id = get_home_workspace_id(cleaned_uid)

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT platform, platform_user_id, platform_username,
                       platform_tenant_key, weixin_context_token,
                       created_at, updated_at
                FROM dalton_channel_identities
                WHERE user_id = %s
                """,
                (cleaned_uid,),
            )
            identities: dict[str, dict[str, Any]] = {}
            for row in await cur.fetchall():
                p = str(row["platform"])
                identities[p] = {
                    "platform": p,
                    "platform_user_id": row["platform_user_id"],
                    "platform_username": row.get("platform_username"),
                    "platform_tenant_key": row.get("platform_tenant_key"),
                    "weixin_context_token": row.get("weixin_context_token"),
                    "created_at": (
                        row["created_at"].isoformat()
                        if hasattr(row.get("created_at"), "isoformat")
                        else row.get("created_at")
                    ),
                    "updated_at": (
                        row["updated_at"].isoformat()
                        if hasattr(row.get("updated_at"), "isoformat")
                        else row.get("updated_at")
                    ),
                }

            await cur.execute(
                """
                SELECT version, apps, updated_at
                FROM dalton_channel_user_settings
                WHERE user_id = %s
                """,
                (cleaned_uid,),
            )
            s_row = await cur.fetchone()
            if s_row:
                settings_version = str(s_row["version"])
                settings_apps = dict(s_row.get("apps") or {})
            else:
                settings_version = "v1"
                settings_apps = {
                    p: default_app_settings(p) for p in sorted(identities.keys())
                }

            # Accessible group chat bindings
            chat_bindings: list[dict[str, Any]] = []
            if user_tier != "external":
                await cur.execute(
                    """
                    SELECT platform, chat_address, chat_name, workspace_id,
                           bound_by_user_id, updated_at
                    FROM dalton_channel_chat_bindings
                    ORDER BY updated_at DESC
                    """
                )
                for b_row in await cur.fetchall():
                    b_plat = str(b_row["platform"])
                    b_addr = str(b_row["chat_address"])
                    b_ws_id = (
                        str(b_row["workspace_id"])
                        if b_row.get("workspace_id")
                        else None
                    )
                    b_puid = (identities.get(b_plat) or {}).get("platform_user_id")
                    if not await _user_can_manage_or_view_group_chat(
                        cur,
                        user_id=cleaned_uid,
                        user_tier=user_tier,
                        platform=b_plat,
                        chat_address=b_addr,
                        platform_user_id=b_puid,
                    ):
                        continue
                    updated_dt = b_row.get("updated_at")
                    epoch_sec = (
                        int(updated_dt.timestamp())
                        if hasattr(updated_dt, "timestamp")
                        else None
                    )
                    chat_bindings.append(
                        {
                            "platform": b_plat,
                            "chat_address": b_addr,
                            "chat_name": b_row.get("chat_name"),
                            "workspace_id": b_ws_id,
                            "bound_by_user_id": b_row["bound_by_user_id"],
                            "last_used_at": epoch_sec,
                        }
                    )

            # Optional session routing lookup by (thread_id, run_id) or (platform, external_thread_id)
            current_conversation: dict[str, Any] | None = None
            resolved_thread_id: str | None = None

            if platform and external_thread_id:
                resolved_thread_id = await lookup_thread_by_external_id(
                    platform.strip().lower(), external_thread_id.strip(), cleaned_uid
                )
                if resolved_thread_id:
                    current_conversation = {
                        "platform": platform.strip().lower(),
                        "external_thread_id": external_thread_id.strip(),
                        "thread_id": resolved_thread_id,
                    }

            norm_tid = normalize_uuid(thread_id)
            cleaned_rid = str(run_id or "").strip() or None
            if cleaned_rid or norm_tid:
                clauses = ["dalton_user_id = %s"]
                params: list[Any] = [cleaned_uid]
                if cleaned_rid:
                    clauses.append("run_id = %s")
                    params.append(cleaned_rid)
                if norm_tid:
                    clauses.append("thread_id = %s")
                    params.append(norm_tid)
                if platform:
                    clauses.append("platform = %s")
                    params.append(platform.strip().lower())
                await cur.execute(
                    f"""
                    SELECT platform, chat_address, thread_id, run_id, external_thread_id
                    FROM dalton_channel_inbound_events
                    WHERE {" AND ".join(clauses)}
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    tuple(params),
                )
                in_match = await cur.fetchone()
                if in_match:
                    current_conversation = {
                        "address": in_match["chat_address"],
                        "platform": in_match["platform"],
                        "thread_id": (
                            str(in_match["thread_id"])
                            if in_match.get("thread_id")
                            else norm_tid
                        ),
                        "run_id": in_match.get("run_id") or cleaned_rid,
                        "external_thread_id": in_match.get("external_thread_id"),
                    }
                else:
                    bg_clauses = ["dalton_user_id = %s"]
                    bg_params: list[Any] = [cleaned_uid]
                    if cleaned_rid:
                        bg_clauses.append("run_id = %s")
                        bg_params.append(cleaned_rid)
                    if norm_tid:
                        bg_clauses.append("thread_id = %s")
                        bg_params.append(norm_tid)
                    if platform:
                        bg_clauses.append("platform = %s")
                        bg_params.append(platform.strip().lower())
                    await cur.execute(
                        f"""
                        SELECT platform, target_address, thread_id, run_id
                        FROM dalton_channel_background_runs
                        WHERE {" AND ".join(bg_clauses)}
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        tuple(bg_params),
                    )
                    bg_match = await cur.fetchone()
                    if bg_match:
                        current_conversation = {
                            "address": bg_match["target_address"],
                            "platform": bg_match["platform"],
                            "thread_id": str(bg_match["thread_id"]),
                            "run_id": bg_match["run_id"],
                            "external_thread_id": None,
                        }

    return {
        "user_id": cleaned_uid,
        "user_tier": user_tier,
        "home_workspace_id": home_ws_id,
        "identities": identities,
        "settings": {
            "version": settings_version,
            "apps": settings_apps,
        },
        "chat_bindings": chat_bindings,
        "current_conversation": current_conversation,
        "resolved_thread_id": resolved_thread_id,
    }


# ---------------------------------------------------------------------------
# I-5: Update User Channel Settings (with CAS & Colleague Workspace Validation)
# ---------------------------------------------------------------------------


def _is_dm_address(platform: str, address: str) -> bool:
    cleaned = str(address or "").strip()
    return cleaned == f"{platform}:@me" or cleaned.endswith(":@me")


async def update_user_channel_settings(
    *,
    user_id: str,
    version: str | None = None,
    settings: dict[str, Any] | None = None,
    automation_output_update: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-5: Validate and save `dalton_channel_user_settings`, syncing `dalton_channel_chat_bindings` and group generations."""
    return await _run_tx_with_retry(
        "update_user_channel_settings",
        conn,
        lambda attempt_conn: _update_user_channel_settings_once(
            user_id=user_id,
            version=version,
            settings=settings,
            automation_output_update=automation_output_update,
            conn=attempt_conn,
        ),
    )


async def _update_user_channel_settings_once(
    *,
    user_id: str,
    version: str | None = None,
    settings: dict[str, Any] | None = None,
    automation_output_update: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        raise HTTPException(status_code=400, detail="user_id is required")

    user_tier = await get_user_tier(cleaned_uid)

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_user_settings:{cleaned_uid}",),
                )
                await cur.execute(
                    """
                    SELECT version, apps FROM dalton_channel_user_settings
                    WHERE user_id = %s FOR UPDATE
                    """,
                    (cleaned_uid,),
                )
                row = await cur.fetchone()
                if row:
                    current_version = str(row["version"])
                    current_apps = json.loads(json.dumps(row.get("apps") or {}))
                else:
                    await cur.execute(
                        """
                        SELECT platform FROM dalton_channel_identities
                        WHERE user_id = %s
                        """,
                        (cleaned_uid,),
                    )
                    linked = [str(r["platform"]) for r in await cur.fetchall()]
                    current_version = "v1"
                    current_apps = {
                        p: default_app_settings(p) for p in sorted(linked)
                    }

                # Handle single-workspace automation_output update (`PUT /agent/automation-output`)
                if automation_output_update is not None and settings is None:
                    upd_ws = str(
                        automation_output_update.get("workspace_id") or ""
                    ).strip()
                    upd_platform = (
                        str(automation_output_update.get("platform") or "")
                        .strip()
                        .lower()
                    )
                    upd_addr = automation_output_update.get("address")
                    norm_ws = normalize_uuid(upd_ws) or upd_ws
                    candidate_apps = json.loads(json.dumps(current_apps))
                    if upd_platform not in candidate_apps:
                        candidate_apps[upd_platform] = default_app_settings(
                            upd_platform
                        )
                    auto_out = dict(
                        candidate_apps[upd_platform].get("automation_output") or {}
                    )
                    if upd_addr is None:
                        # Strictly delete the workspace_id key (never write null value)
                        auto_out.pop(norm_ws, None)
                        auto_out.pop(upd_ws, None)
                    else:
                        auto_out[norm_ws] = str(upd_addr).strip()
                    candidate_apps[upd_platform]["automation_output"] = auto_out
                    target_settings = candidate_apps
                    expected_version = version or current_version
                else:
                    if not isinstance(settings, dict):
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "invalid",
                                "message": "settings must be a JSON object",
                                "problems": [
                                    {
                                        "field": "",
                                        "message": "settings must be a JSON object",
                                    }
                                ],
                            },
                        )
                    target_settings = settings
                    expected_version = version

                if (
                    expected_version is not None
                    and str(expected_version) != current_version
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "version_conflict",
                            "message": f"Channel settings version conflict: expected {current_version}, got {expected_version}.",
                        },
                    )

                # Validate schema and collect all problems with {"field": ..., "message": ...}
                problems: list[dict[str, str]] = []
                validated_apps: dict[str, Any] = {}
                referenced_ws_ids: list[tuple[str, str]] = []

                for key, raw_app in target_settings.items():
                    if key == "default":
                        try:
                            b_model = _Binding.model_validate(raw_app)
                            validated_apps[key] = {
                                "workspace_id": (
                                    normalize_uuid(b_model.workspace_id)
                                    or b_model.workspace_id
                                    if b_model.workspace_id is not None
                                    else None
                                ),
                                **(b_model.model_extra or {}),
                            }
                            if b_model.workspace_id is not None:
                                referenced_ws_ids.append(
                                    (_field(key, "workspace_id"), b_model.workspace_id)
                                )
                        except ValidationError as exc:
                            for err in exc.errors():
                                problems.append(
                                    {
                                        "field": _field(key, *err["loc"]),
                                        "message": _problem_message(err),
                                    }
                                )
                        continue

                    try:
                        app_model = _App.model_validate(raw_app)
                    except ValidationError as exc:
                        for err in exc.errors():
                            problems.append(
                                {
                                    "field": _field(key, *err["loc"]),
                                    "message": _problem_message(err),
                                }
                            )
                        continue

                    # Rule ① (external tier group chat prohibition) & Rule ② (weixin group chat prohibition)
                    if app_model.preferred is not None and not _is_dm_address(
                        key, app_model.preferred
                    ):
                        if key == "weixin":
                            problems.append(
                                {
                                    "field": _field(key, "preferred"),
                                    "message": "Weixin only supports direct messages (weixin:@me).",
                                }
                            )
                        elif user_tier == "external":
                            problems.append(
                                {
                                    "field": _field(key, "preferred"),
                                    "message": "External members cannot configure group chats.",
                                }
                            )

                    chats_dict: dict[str, Any] = {}
                    for addr, b_model in app_model.chats.items():
                        if not _is_dm_address(key, addr):
                            if key == "weixin":
                                problems.append(
                                    {
                                        "field": _field(key, "chats", addr),
                                        "message": "Weixin only supports direct messages (weixin:@me).",
                                    }
                                )
                            elif user_tier == "external":
                                problems.append(
                                    {
                                        "field": _field(key, "chats", addr),
                                        "message": "External members cannot configure group chats.",
                                    }
                                )
                        ws_val = (
                            normalize_uuid(b_model.workspace_id) or b_model.workspace_id
                            if b_model.workspace_id is not None
                            else None
                        )
                        if ws_val is not None:
                            referenced_ws_ids.append(
                                (_field(key, "chats", addr, "workspace_id"), ws_val)
                            )
                        chats_dict[addr] = {
                            "workspace_id": ws_val,
                            **(
                                {"name": b_model.name}
                                if b_model.name is not None
                                else {}
                            ),
                            **(b_model.model_extra or {}),
                        }

                    auto_dict: dict[str, str] = {}
                    for ws_key, addr in app_model.automation_output.items():
                        norm_ws_key = normalize_uuid(ws_key) or ws_key
                        referenced_ws_ids.append(
                            (_field(key, "automation_output", ws_key), norm_ws_key)
                        )
                        if not _is_dm_address(key, addr):
                            if key == "weixin":
                                problems.append(
                                    {
                                        "field": _field(
                                            key, "automation_output", ws_key
                                        ),
                                        "message": "Weixin only supports direct messages (weixin:@me).",
                                    }
                                )
                            elif user_tier == "external":
                                problems.append(
                                    {
                                        "field": _field(
                                            key, "automation_output", ws_key
                                        ),
                                        "message": "External members cannot configure group chats.",
                                    }
                                )
                        auto_dict[norm_ws_key] = addr

                    for idx, allowed_addr in enumerate(
                        app_model.agent_messages.allowed
                    ):
                        if not _is_dm_address(key, allowed_addr):
                            if key == "weixin":
                                problems.append(
                                    {
                                        "field": _field(
                                            key, "agent_messages", "allowed", idx
                                        ),
                                        "message": "Weixin only supports direct messages (weixin:@me).",
                                    }
                                )
                            elif user_tier == "external":
                                problems.append(
                                    {
                                        "field": _field(
                                            key, "agent_messages", "allowed", idx
                                        ),
                                        "message": "External members cannot configure group chats.",
                                    }
                                )

                    validated_apps[key] = {
                        "preferred": app_model.preferred,
                        "chats": chats_dict,
                        "automation_output": auto_dict,
                        "agent_messages": {
                            "enabled": app_model.agent_messages.enabled,
                            "allowed": list(app_model.agent_messages.allowed),
                            **(app_model.agent_messages.model_extra or {}),
                        },
                        **(app_model.model_extra or {}),
                    }

                # Rule ③: Check workspace write permissions for all referenced workspace_ids
                if referenced_ws_ids:
                    writable_names = await _get_writable_workspace_names(
                        cleaned_uid, [ws for _, ws in referenced_ws_ids]
                    )
                    for field_path, ws_val in referenced_ws_ids:
                        norm_ws = normalize_uuid(ws_val)
                        if not norm_ws or norm_ws not in writable_names:
                            problems.append(
                                {
                                    "field": field_path,
                                    "message": f"{ws_val!r} is not a writable workspace for your account.",
                                }
                            )

                if problems:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "invalid",
                            "message": problems[0]["message"],
                            "problems": problems,
                        },
                    )

                # Compute changes
                changes: list[str] = []
                if validated_apps != current_apps:
                    for app_key, new_app_val in validated_apps.items():
                        old_app_val = current_apps.get(app_key)
                        if old_app_val != new_app_val:
                            changes.append(f"Updated {app_key} channel settings")
                    if not changes:
                        changes.append("Updated channels.json")
                    new_version = bump_version_str(current_version)
                else:
                    new_version = current_version

                def _group_chat_routing_cfg(cfg: Any) -> dict[str, Any]:
                    """Extract only routing-relevant fields (`workspace_id`) for group change comparison.

                    Read-only display labels (`name`, `workspace`) stripped by `channel_settings._stored()`
                    do not participate in group modification detection.
                    """
                    if not isinstance(cfg, dict):
                        return {"workspace_id": None}
                    raw_ws = cfg.get("workspace_id")
                    ws_val = (
                        (normalize_uuid(raw_ws) or raw_ws)
                        if raw_ws is not None
                        else None
                    )
                    return {"workspace_id": ws_val}

                # Sync dalton_channel_chat_bindings for group notification settings (independent of personal session generations)
                for app_key in GROUP_PLATFORMS:
                    old_app_cfg = (
                        current_apps.get(app_key)
                        if isinstance(current_apps.get(app_key), dict)
                        else {}
                    )
                    app_cfg = validated_apps.get(app_key)
                    if not isinstance(app_cfg, dict) and not old_app_cfg:
                        continue
                    old_raw_chats = {
                        addr: (cfg if isinstance(cfg, dict) else {})
                        for addr, cfg in (old_app_cfg.get("chats") or {}).items()
                        if not _is_dm_address(app_key, addr)
                    }
                    new_raw_chats = {
                        addr: (cfg if isinstance(cfg, dict) else {})
                        for addr, cfg in (
                            (app_cfg if isinstance(app_cfg, dict) else {}).get("chats")
                            or {}
                        ).items()
                        if not _is_dm_address(app_key, addr)
                    }
                    old_routing = {
                        addr: _group_chat_routing_cfg(cfg)
                        for addr, cfg in old_raw_chats.items()
                    }
                    new_routing = {
                        addr: _group_chat_routing_cfg(cfg)
                        for addr, cfg in new_raw_chats.items()
                    }
                    deleted_group_set = {
                        addr for addr in old_routing if addr not in new_routing
                    }
                    added_or_modified_group_set = {
                        addr
                        for addr, r_cfg in new_routing.items()
                        if addr not in old_routing or r_cfg != old_routing[addr]
                    }
                    changed_group_addrs = sorted(
                        deleted_group_set | added_or_modified_group_set
                    )

                    # 1. Verify permission and acquire per-group advisory locks in unified sorted order
                    for chat_addr in changed_group_addrs:
                        if not await _user_can_manage_or_view_group_chat(
                            cur,
                            user_id=cleaned_uid,
                            user_tier=user_tier,
                            platform=app_key,
                            chat_address=chat_addr,
                        ):
                            raise HTTPException(
                                status_code=403,
                                detail=f"You do not have permission to manage group chat {chat_addr!r}.",
                            )
                        await _acquire_group_chat_lock(cur, app_key, chat_addr)

                    # 2. After all locks are held, process deletions and additions/modifications
                    for chat_addr in changed_group_addrs:
                        await cur.execute(
                            """
                            SELECT workspace_id FROM dalton_channel_chat_bindings
                            WHERE platform = %s AND chat_address = %s
                            FOR UPDATE
                            """,
                            (app_key, chat_addr),
                        )
                        existing_binding = await cur.fetchone()
                        prev_ws_id = (
                            normalize_uuid(existing_binding["workspace_id"])
                            if existing_binding and existing_binding.get("workspace_id")
                            else None
                        )
                        if chat_addr in deleted_group_set:
                            old_cfg_ws_id = old_routing[chat_addr].get("workspace_id")
                            if (
                                existing_binding is not None
                                and prev_ws_id == old_cfg_ws_id
                            ):
                                await cur.execute(
                                    """
                                    DELETE FROM dalton_channel_chat_bindings
                                    WHERE platform = %s AND chat_address = %s
                                    """,
                                    (app_key, chat_addr),
                                )
                        else:
                            binding_cfg = new_raw_chats[chat_addr]
                            new_ws_id = new_routing[chat_addr].get("workspace_id")
                            chat_name_val = binding_cfg.get("name")
                            if existing_binding is not None and prev_ws_id == new_ws_id:
                                continue
                            await cur.execute(
                                """
                                INSERT INTO dalton_channel_chat_bindings (
                                    platform, chat_address, chat_name, workspace_id,
                                    bound_by_user_id, updated_at
                                )
                                VALUES (%s, %s, %s, %s, %s, NOW())
                                ON CONFLICT (platform, chat_address) DO UPDATE SET
                                    workspace_id = EXCLUDED.workspace_id,
                                    chat_name = COALESCE(EXCLUDED.chat_name, dalton_channel_chat_bindings.chat_name),
                                    bound_by_user_id = EXCLUDED.bound_by_user_id,
                                    updated_at = NOW()
                                """,
                                (
                                    app_key,
                                    chat_addr,
                                    chat_name_val,
                                    new_ws_id,
                                    cleaned_uid,
                                ),
                            )

                if row:
                    if changes:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_user_settings
                            SET version = %s, apps = %s, updated_at = NOW()
                            WHERE user_id = %s
                            """,
                            (new_version, Jsonb(validated_apps), cleaned_uid),
                        )
                else:
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_user_settings (
                            user_id, version, apps, updated_at
                        )
                        VALUES (%s, %s, %s, NOW())
                        """,
                        (cleaned_uid, new_version, Jsonb(validated_apps)),
                    )

                result: dict[str, Any] = {
                    "version": new_version,
                    "settings": validated_apps,
                    "changes": changes,
                }
                if automation_output_update is not None:
                    upd_addr = automation_output_update.get("address")
                    if upd_addr is None:
                        result["address"] = None
                        result["name"] = None
                    else:
                        result["address"] = str(upd_addr).strip()
                        result["name"] = str(upd_addr).strip()
                return result


# ---------------------------------------------------------------------------
# I-6: Outbound Delivery Authorize & Claim
# ---------------------------------------------------------------------------


async def authorize_and_claim_delivery(
    *,
    delivery_key: str,
    kind: str,
    user_id: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    execution_id: str | None = None,
    automation_execution_id: str | None = None,
    platform: str | None = None,
    target: str | None = None,
    inbound_message_id: str | None = None,
    has_non_empty_text: bool = True,
    total_chunks: int = 0,
    source_scope_key: str | None = None,
    source_generation: int | None = None,
    activity_origin: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-6: Atomically check idempotency, authorize outbound target across Scenarios A1/A2/B/C, and claim `in_flight`."""
    cleaned_key = str(delivery_key or "").strip()
    if not cleaned_key:
        raise HTTPException(status_code=400, detail="delivery_key is required")
    cleaned_kind = str(kind or "agent_send").strip().lower()
    if cleaned_kind == "data_task":
        from src.dalton.database.data_task_notifications import (
            authorize_and_claim_data_task_delivery,
        )

        return await authorize_and_claim_data_task_delivery(
            delivery_key=cleaned_key,
            run_id=run_id,
            target=target,
            has_non_empty_text=has_non_empty_text,
            total_chunks=total_chunks,
            conn=conn,
        )
    cleaned_uid = str(user_id or "").strip() or None
    cleaned_platform = str(platform or "").strip().lower() or None
    cleaned_target = str(target).strip() if target is not None and str(target).strip() else None
    cleaned_inbound_mid = str(inbound_message_id or "").strip() or None
    eff_exec_id = normalize_uuid(automation_execution_id or execution_id)
    norm_tid = normalize_uuid(thread_id)
    cleaned_rid = str(run_id or "").strip() or None
    eff_source_scope = str(source_scope_key or "").strip() or None
    eff_source_gen = int(source_generation) if source_generation is not None else None
    eff_activity_origin = str(activity_origin or "none").strip().lower()
    if eff_activity_origin not in (
        "conversation",
        "replay",
        "notification",
        "command",
        "none",
    ):
        eff_activity_origin = "none"

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                # Serialize concurrent claims on the same delivery_key
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_delivery:{cleaned_key}",),
                )
                # 1. Idempotency check
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    FOR UPDATE
                    """,
                    (cleaned_key,),
                )
                existing = await cur.fetchone()
                # True only when this claim may persist activity_origin conversation -> replay.
                replay_recovery = False
                if existing:
                    ex_scope = (
                        str(existing.get("source_scope_key") or "").strip() or None
                    )
                    ex_gen = (
                        int(existing["source_generation"])
                        if existing.get("source_generation") is not None
                        else None
                    )
                    ex_origin = (
                        str(existing.get("activity_origin") or "none")
                        .strip()
                        .lower()
                    )
                    ex_status = str(existing["status"])
                    scope_mismatch = (
                        eff_source_scope is not None and ex_scope != eff_source_scope
                    )
                    gen_mismatch = (
                        eff_source_gen is not None and ex_gen != eff_source_gen
                    )
                    origin_differs = (
                        activity_origin is not None
                        and ex_origin != eff_activity_origin
                    )
                    # Absent scope or generation matches only another absent value.
                    # conversation -> replay is allowed only for this same scope and generation.
                    # A failed row may be claimed again and stored as replay. A sent or partial
                    # row is already delivered: return it unchanged and do not send the body again.
                    same_scope_and_generation = (
                        ex_scope == eff_source_scope and ex_gen == eff_source_gen
                    )
                    conversation_to_replay = (
                        ex_origin == "conversation"
                        and eff_activity_origin == "replay"
                        and same_scope_and_generation
                    )
                    replay_recovery = ex_status == "failed" and conversation_to_replay
                    if (
                        ex_status in ("sent", "partial")
                        and conversation_to_replay
                    ):
                        ex_user = str(existing.get("user_id") or "").strip()
                        ex_plat = str(existing.get("platform") or "").strip().lower()
                        ex_target = str(existing.get("target_address") or "").strip()
                        got_user = str(cleaned_uid or "").strip()
                        got_plat = str(cleaned_platform or "").strip().lower()
                        got_target = str(cleaned_target or "").strip()
                        if (
                            ex_user != got_user
                            or ex_plat != got_plat
                            or ex_target != got_target
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "Delivery identity or destination does not match the existing record.",
                            }
                        return {
                            "claim_status": "already_completed",
                            "status": ex_status,
                            "first_response_json": existing.get("first_response_json"),
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": bool(existing.get("is_current", False)),
                        }
                    if scope_mismatch or gen_mismatch or (
                        origin_differs and not replay_recovery
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Delivery source metadata is immutable and does not match existing record.",
                        }
                    if ex_status in ("sent", "partial"):
                        return {
                            "claim_status": "already_completed",
                            "status": ex_status,
                            "first_response_json": existing.get("first_response_json"),
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": bool(existing.get("is_current", False)),
                        }
                    if ex_status in ("in_flight", "unknown"):
                        return {
                            "claim_status": "in_flight_or_unknown",
                            "status": "unknown",
                            "code": "in_flight",
                            "message": "An earlier attempt of this same call is still delivering it.",
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": bool(existing.get("is_current", False)),
                        }

                # Guard final text fallback (`fallback:<run_id>`) when an `unknown` current-turn text delivery exists (§1.3.3)
                if cleaned_kind == "fallback" or cleaned_key.startswith("fallback:"):
                    fallback_rid = cleaned_rid or (
                        cleaned_key.split(":", 1)[1].strip()
                        if cleaned_key.startswith("fallback:")
                        else None
                    )
                    if fallback_rid:
                        await cur.execute(
                            """
                            SELECT EXISTS (
                                SELECT 1 FROM dalton_channel_deliveries
                                WHERE run_id = %s
                                  AND is_current = TRUE
                                  AND has_non_empty_text = TRUE
                                  AND status = 'unknown'
                            ) AS has_unknown_text
                            """,
                            (fallback_rid,),
                        )
                        unk_row = await cur.fetchone()
                        if unk_row and unk_row.get("has_unknown_text"):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "unknown_delivery_blocks_fallback",
                                "message": "Run has an unknown current-conversation text delivery; final text fallback is skipped.",
                            }

                # 2. Scenario-based authorization
                resolved_platform: str | None = None
                resolved_target_address: str | None = None
                resolved_recipient_id: str | None = None
                resolved_user_id: str | None = cleaned_uid
                is_current = False
                weixin_ctx_token: str | None = None

                # Scenario A1: Direct notice replying to an inbound event
                if cleaned_kind == "notice" and cleaned_inbound_mid and cleaned_platform:
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (cleaned_platform, cleaned_inbound_mid),
                    )
                    in_row = await cur.fetchone()
                    if not in_row:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "no_current_conversation",
                            "message": "Inbound event not found.",
                        }
                    persisted_scope = (
                        str(in_row.get("thread_scope_key") or "").strip() or None
                    )
                    persisted_gen = (
                        int(in_row["session_generation"])
                        if in_row.get("session_generation") is not None
                        else None
                    )
                    if (
                        eff_source_scope is not None
                        and persisted_scope is not None
                        and eff_source_scope != persisted_scope
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "source_scope_key does not match inbound event.",
                        }
                    if (
                        eff_source_gen is not None
                        and persisted_gen is not None
                        and eff_source_gen != persisted_gen
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "source_generation does not match inbound event.",
                        }
                    if eff_activity_origin == "conversation":
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Direct notice cannot claim conversation activity origin.",
                        }
                    eff_source_scope = persisted_scope
                    eff_source_gen = persisted_gen

                    resolved_platform = str(in_row["platform"])
                    resolved_target_address = str(in_row["chat_address"])
                    raw_p = in_row.get("raw_payload") or {}
                    puid = str(raw_p.get("platform_user_id") or "")
                    resolved_user_id = (
                        str(in_row["dalton_user_id"])
                        if in_row.get("dalton_user_id")
                        else f"unlinked:{resolved_platform}:{puid}"
                    )
                    weixin_ctx_token = raw_p.get("weixin_context_token")
                    if in_row.get("dalton_user_id") and (
                        not puid or not weixin_ctx_token
                    ):
                        await cur.execute(
                            """
                            SELECT platform_user_id, weixin_context_token
                            FROM dalton_channel_identities
                            WHERE user_id = %s AND platform = %s
                            """,
                            (in_row["dalton_user_id"], resolved_platform),
                        )
                        id_row = await cur.fetchone()
                        if id_row:
                            if not puid:
                                puid = str(id_row.get("platform_user_id") or "")
                            if not weixin_ctx_token:
                                weixin_ctx_token = id_row.get("weixin_context_token")
                    if _is_dm_address(resolved_platform, resolved_target_address):
                        resolved_recipient_id = puid
                    else:
                        resolved_recipient_id = extract_chat_id(
                            resolved_platform, chat_address=resolved_target_address
                        )

                # Scenario B: Automation delivery
                elif eff_exec_id and (
                    cleaned_kind == "automation"
                    or (cleaned_kind == "agent_send" and eff_exec_id is not None)
                ):
                    if (
                        eff_source_scope is not None
                        or eff_source_gen is not None
                        or eff_activity_origin == "conversation"
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Automation deliveries cannot claim conversation session source.",
                        }
                    eff_source_scope = None
                    eff_source_gen = None

                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_automation_runs
                        WHERE execution_id = %s
                        """,
                        (eff_exec_id,),
                    )
                    auto_row = await cur.fetchone()
                    if not auto_row:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Automation run not found.",
                        }
                    owner_uid = str(auto_row["user_id"])
                    if cleaned_uid and cleaned_uid != owner_uid:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Automation run belongs to another user.",
                        }
                    resolved_user_id = owner_uid
                    targets_list = list(auto_row.get("targets") or [])
                    matched_target: dict[str, Any] | None = None
                    for t_item in targets_list:
                        if not isinstance(t_item, dict):
                            continue
                        if cleaned_target:
                            if t_item.get("address") == cleaned_target:
                                matched_target = t_item
                                break
                        elif t_item.get("ok") is True:
                            if not cleaned_platform or str(
                                t_item.get("address") or ""
                            ).startswith(f"{cleaned_platform}:"):
                                matched_target = t_item
                                break

                    if not matched_target or matched_target.get("ok") is not True:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Target is not authorized for this automation run.",
                        }
                    resolved_target_address = str(matched_target["address"])
                    resolved_platform = resolved_target_address.split(":", 1)[0].lower()
                    is_current = cleaned_target is None

                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = %s
                        """,
                        (resolved_user_id, resolved_platform),
                    )
                    id_row = await cur.fetchone()
                    if not id_row:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_linked",
                            "message": f"{resolved_platform} is not connected to your account.",
                        }
                    weixin_ctx_token = id_row.get("weixin_context_token")

                    if not _is_dm_address(resolved_platform, resolved_target_address):
                        # Real-time group binding & workspace write permission re-verification (shared with I-8)
                        grp_ok, grp_err, _ = await _verify_group_target_deliverable(
                            cur,
                            user_id=resolved_user_id,
                            platform=resolved_platform,
                            chat_address=resolved_target_address,
                            conn=owned_conn,
                        )
                        if not grp_ok:
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": grp_err
                                or "Group chat delivery is not allowed.",
                            }
                        resolved_recipient_id = extract_chat_id(
                            resolved_platform, chat_address=resolved_target_address
                        )
                    else:
                        resolved_recipient_id = str(id_row["platform_user_id"])

                # Scenario A2: Reply to existing Agent turn (current conversation)
                elif (
                    (cleaned_kind == "agent_send" and cleaned_target is None)
                    or cleaned_kind in ("fallback", "interrupt")
                    or (cleaned_kind == "notice" and cleaned_inbound_mid is None)
                ):
                    source_row: dict[str, Any] | None = None
                    if cleaned_rid or norm_tid:
                        in_clauses: list[str] = []
                        in_params: list[Any] = []
                        if cleaned_uid:
                            in_clauses.append("dalton_user_id = %s")
                            in_params.append(cleaned_uid)
                        if cleaned_rid:
                            in_clauses.append("run_id = %s")
                            in_params.append(cleaned_rid)
                        if norm_tid:
                            in_clauses.append("thread_id = %s")
                            in_params.append(norm_tid)
                        if in_clauses:
                            await cur.execute(
                                f"""
                                SELECT dalton_user_id, platform, chat_address AS target_address,
                                       thread_scope_key, workspace_id, session_generation,
                                       external_thread_id, raw_payload, thread_id, run_id
                                FROM dalton_channel_inbound_events
                                WHERE {" AND ".join(in_clauses)}
                                ORDER BY created_at DESC
                                LIMIT 1
                                """,
                                tuple(in_params),
                            )
                            found_in = await cur.fetchone()
                            if found_in:
                                source_row = dict(found_in)
                        if source_row is None and in_clauses:
                            await cur.execute(
                                f"""
                                SELECT dalton_user_id, platform, target_address,
                                       thread_id, run_id
                                FROM dalton_channel_background_runs
                                WHERE {" AND ".join(in_clauses)}
                                ORDER BY created_at DESC
                                LIMIT 1
                                """,
                                tuple(in_params),
                            )
                            found_bg = await cur.fetchone()
                            if found_bg:
                                source_row = dict(found_bg)
                        if source_row is None and norm_tid:
                            await cur.execute(
                                """
                                SELECT dalton_user_id, platform, target_address,
                                       scope_key, generation, thread_id
                                FROM dalton_channel_thread_watches
                                WHERE thread_id = %s
                                """,
                                (norm_tid,),
                            )
                            found_w = await cur.fetchone()
                            if found_w and (
                                not cleaned_uid
                                or str(found_w["dalton_user_id"]) == cleaned_uid
                            ):
                                source_row = dict(found_w)

                    if not source_row:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "no_current_conversation",
                            "message": "No current channel conversation found for this turn.",
                        }
                    if (
                        cleaned_platform
                        and str(source_row["platform"]).lower() != cleaned_platform
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "no_current_conversation",
                            "message": "Turn platform does not match source conversation.",
                        }

                    resolved_user_id = str(source_row["dalton_user_id"])
                    resolved_platform = str(source_row["platform"]).lower()
                    resolved_target_address = str(source_row["target_address"])
                    is_current = True

                    persisted_scope: str | None = None
                    persisted_gen: int | None = None
                    raw_sc = source_row.get("thread_scope_key") or source_row.get(
                        "scope_key"
                    )
                    if raw_sc:
                        persisted_scope = str(raw_sc).strip() or None
                    if source_row.get("session_generation") is not None:
                        persisted_gen = int(source_row["session_generation"])
                    elif source_row.get("generation") is not None:
                        persisted_gen = int(source_row["generation"])
                    elif (
                        isinstance(source_row.get("raw_payload"), dict)
                        and source_row["raw_payload"].get("generation") is not None
                    ):
                        persisted_gen = int(
                            source_row["raw_payload"]["generation"]
                        )
                    elif source_row.get("external_thread_id") and ":gen:" in str(
                        source_row["external_thread_id"]
                    ):
                        gen_p = (
                            str(source_row["external_thread_id"])
                            .rsplit(":gen:", 1)[-1]
                            .strip()
                        )
                        if gen_p.isdigit():
                            persisted_gen = int(gen_p)

                    lookup_tid = normalize_uuid(source_row.get("thread_id")) or norm_tid
                    if (persisted_scope is None or persisted_gen is None) and lookup_tid:
                        await cur.execute(
                            """
                            SELECT dalton_user_id, platform, target_address,
                                   scope_key, generation, thread_id
                            FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            """,
                            (lookup_tid,),
                        )
                        w_extra = await cur.fetchone()
                        if w_extra and str(w_extra.get("dalton_user_id") or "") == resolved_user_id:
                            if persisted_scope is None and w_extra.get("scope_key"):
                                persisted_scope = str(w_extra["scope_key"]).strip() or None
                            if persisted_gen is None and w_extra.get("generation") is not None:
                                persisted_gen = int(w_extra["generation"])
                    if (persisted_scope is None or persisted_gen is None) and lookup_tid:
                        await cur.execute(
                            """
                            SELECT dalton_user_id, platform, chat_address AS target_address,
                                   thread_scope_key, workspace_id, session_generation,
                                   external_thread_id, raw_payload, thread_id, run_id
                            FROM dalton_channel_inbound_events
                            WHERE dalton_user_id = %s AND thread_id = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (resolved_user_id, lookup_tid),
                        )
                        in_extra = await cur.fetchone()
                        if in_extra:
                            if persisted_scope is None and in_extra.get("thread_scope_key"):
                                persisted_scope = str(in_extra["thread_scope_key"]).strip() or None
                            if persisted_gen is None and in_extra.get("session_generation") is not None:
                                persisted_gen = int(in_extra["session_generation"])

                    if eff_source_scope is not None and (
                        persisted_scope is None or eff_source_scope != persisted_scope
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "source_scope_key does not match persisted conversation source.",
                        }
                    if eff_source_gen is not None and (
                        persisted_gen is None or eff_source_gen != persisted_gen
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "source_generation does not match persisted conversation source.",
                        }
                    if eff_activity_origin == "conversation" and (
                        persisted_scope is None or persisted_gen is None
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Conversation activity origin requires a persisted conversation source.",
                        }
                    eff_source_scope = persisted_scope
                    eff_source_gen = persisted_gen

                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = %s
                        """,
                        (resolved_user_id, resolved_platform),
                    )
                    id_row = await cur.fetchone()
                    if not id_row:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_linked",
                            "message": f"{resolved_platform} is not connected to your account.",
                        }
                    weixin_ctx_token = id_row.get("weixin_context_token")

                    if not _is_dm_address(resolved_platform, resolved_target_address):
                        if (
                            resolved_platform == "weixin"
                            or await get_user_tier(resolved_user_id) == "external"
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "Group chat delivery is not allowed.",
                            }
                        resolved_recipient_id = extract_chat_id(
                            resolved_platform, chat_address=resolved_target_address
                        )
                    else:
                        resolved_recipient_id = str(id_row["platform_user_id"])

                    # Verify write permission on the source thread's actual workspace (not group notification bindings)
                    source_ws_id = source_row.get("workspace_id")
                    eff_tid = normalize_uuid(source_row.get("thread_id")) or norm_tid
                    if source_ws_id is None and eff_tid:
                        await cur.execute(
                            """
                            SELECT workspace_id FROM conversation_threads
                            WHERE conversation_thread_id = %s
                            """,
                            (eff_tid,),
                        )
                        th_row = await cur.fetchone()
                        if th_row and th_row.get("workspace_id"):
                            source_ws_id = th_row["workspace_id"]
                    home_ws_for_user = get_home_workspace_id(resolved_user_id)
                    if source_ws_id and normalize_uuid(source_ws_id) != normalize_uuid(
                        home_ws_for_user
                    ):
                        ok, _, _ = await _verify_workspace_write_permission(
                            cur,
                            str(source_ws_id),
                            resolved_user_id,
                            conn=owned_conn,
                        )
                        if not ok:
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "Workspace write access revoked.",
                            }

                # Scenario C: Explicit target proactive send (kind == "agent_send" && target != null)
                else:
                    if not cleaned_uid or not cleaned_target or ":" not in cleaned_target:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Valid user_id and target address are required.",
                        }
                    resolved_user_id = cleaned_uid
                    resolved_target_address = cleaned_target
                    resolved_platform = cleaned_target.split(":", 1)[0].strip().lower()

                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = %s
                        """,
                        (resolved_user_id, resolved_platform),
                    )
                    id_row = await cur.fetchone()
                    if not id_row:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_linked",
                            "message": f"{resolved_platform} is not connected to your account.",
                        }
                    weixin_ctx_token = id_row.get("weixin_context_token")

                    is_dm = _is_dm_address(resolved_platform, resolved_target_address)
                    user_tier = await get_user_tier(resolved_user_id)
                    if not is_dm and (
                        user_tier == "external" or resolved_platform == "weixin"
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Group chat target is not allowed.",
                        }

                    # Check agent_messages.enabled and agent_messages.allowed
                    await cur.execute(
                        """
                        SELECT apps FROM dalton_channel_user_settings
                        WHERE user_id = %s
                        """,
                        (resolved_user_id,),
                    )
                    us_row = await cur.fetchone()
                    app_cfg = (
                        (us_row.get("apps") or {}).get(resolved_platform)
                        if us_row
                        else default_app_settings(resolved_platform)
                    )
                    if not isinstance(app_cfg, dict):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Channel settings not configured for this platform.",
                        }
                    agent_msgs = app_cfg.get("agent_messages") or {}
                    if not bool(agent_msgs.get("enabled")):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Proactive agent messages are disabled for this platform.",
                        }
                    allowed_list = list(agent_msgs.get("allowed") or [])
                    if resolved_target_address not in allowed_list:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": f"Target {resolved_target_address!r} is not in agent_messages.allowed.",
                        }

                    if not is_dm:
                        chats_cfg = app_cfg.get("chats") or {}
                        await cur.execute(
                            """
                            SELECT workspace_id FROM dalton_channel_chat_bindings
                            WHERE platform = %s AND chat_address = %s
                            """,
                            (resolved_platform, resolved_target_address),
                        )
                        cb_row = await cur.fetchone()
                        if resolved_target_address not in chats_cfg or not cb_row:
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "Group chat is not registered in channel settings.",
                            }
                        bound_ws = cb_row.get("workspace_id") or (
                            chats_cfg.get(resolved_target_address) or {}
                        ).get("workspace_id")
                        if bound_ws:
                            ok, _, _ = await _verify_workspace_write_permission(
                                cur,
                                str(bound_ws),
                                resolved_user_id,
                                conn=owned_conn,
                            )
                            if not ok:
                                return {
                                    "claim_status": "denied",
                                    "status": "failed",
                                    "code": "not_allowed",
                                    "message": "You do not have write access to the group's bound workspace.",
                                }
                        resolved_recipient_id = extract_chat_id(
                            resolved_platform, chat_address=resolved_target_address
                        )
                    else:
                        resolved_recipient_id = str(id_row["platform_user_id"])

                    # Check whether explicit target happens to match the turn's current conversation
                    persisted_c_scope: str | None = None
                    persisted_c_gen: int | None = None
                    if cleaned_rid or norm_tid:
                        await cur.execute(
                            """
                            SELECT chat_address, thread_scope_key, session_generation
                            FROM dalton_channel_inbound_events
                            WHERE dalton_user_id = %s
                              AND (%s::text IS NULL OR run_id = %s)
                              AND (%s::uuid IS NULL OR thread_id = %s)
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (
                                resolved_user_id,
                                cleaned_rid,
                                cleaned_rid,
                                norm_tid,
                                norm_tid,
                            ),
                        )
                        cur_in = await cur.fetchone()
                        if cur_in and str(cur_in["chat_address"]) == resolved_target_address:
                            is_current = True
                            if cur_in.get("thread_scope_key"):
                                persisted_c_scope = str(cur_in["thread_scope_key"]).strip() or None
                            if cur_in.get("session_generation") is not None:
                                persisted_c_gen = int(cur_in["session_generation"])

                    if is_current:
                        if eff_source_scope is not None and (
                            persisted_c_scope is None
                            or eff_source_scope != persisted_c_scope
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "source_scope_key does not match current conversation source.",
                            }
                        if eff_source_gen is not None and (
                            persisted_c_gen is None
                            or eff_source_gen != persisted_c_gen
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "source_generation does not match current conversation source.",
                            }
                        if eff_activity_origin == "conversation" and (
                            persisted_c_scope is None or persisted_c_gen is None
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "Conversation activity origin requires a persisted conversation source.",
                            }
                        eff_source_scope = persisted_c_scope
                        eff_source_gen = persisted_c_gen
                    else:
                        if (
                            eff_source_scope is not None
                            or eff_source_gen is not None
                            or eff_activity_origin == "conversation"
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "Non-current proactive target cannot claim conversation session source.",
                            }
                        eff_source_scope = None
                        eff_source_gen = None

                # Reclaim keeps the delivery key. A different user, platform, or destination
                # is rejected and does not retarget the stored row.
                if existing is not None:
                    ex_user = str(existing.get("user_id") or "").strip()
                    ex_plat = str(existing.get("platform") or "").strip().lower()
                    ex_target = str(existing.get("target_address") or "").strip()
                    got_user = str(resolved_user_id or "").strip()
                    got_plat = str(resolved_platform or "").strip().lower()
                    got_target = str(resolved_target_address or "").strip()
                    if (
                        ex_user != got_user
                        or ex_plat != got_plat
                        or ex_target != got_target
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Delivery identity or destination does not match the existing record.",
                        }

                # 3. Atomic claim. Scope and generation stay as first stored.
                # A failed conversation delivery reclaimed as replay persists activity_origin='replay'
                # so a later progress update does not extend last_conversation_activity_at.
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, thread_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, total_chunks, sent_chunks, platform_message_ids,
                        files_status, source_scope_key, source_generation, activity_origin,
                        created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        'in_flight', %s, 0, '[]'::jsonb,
                        '[]'::jsonb, %s, %s, %s,
                        NOW(), NOW()
                    )
                    ON CONFLICT (delivery_key) DO UPDATE SET
                        status = 'in_flight',
                        total_chunks = EXCLUDED.total_chunks,
                        error_code = NULL,
                        error_message = NULL,
                        activity_origin = CASE
                            WHEN %s
                             AND dalton_channel_deliveries.status = 'failed'
                             AND dalton_channel_deliveries.activity_origin = 'conversation'
                            THEN 'replay'
                            ELSE dalton_channel_deliveries.activity_origin
                        END,
                        updated_at = NOW()
                    """,
                    (
                        cleaned_key,
                        cleaned_kind,
                        resolved_user_id,
                        norm_tid,
                        cleaned_rid,
                        eff_exec_id,
                        is_current,
                        bool(has_non_empty_text),
                        resolved_platform,
                        resolved_target_address,
                        int(total_chunks or 0),
                        eff_source_scope,
                        eff_source_gen,
                        eff_activity_origin,
                        replay_recovery,
                    ),
                )

                home_ws_id = (
                    get_home_workspace_id(resolved_user_id)
                    if resolved_user_id
                    and not str(resolved_user_id).startswith("unlinked:")
                    else None
                )
                return {
                    "claim_status": "claimed",
                    "recipient_id": resolved_recipient_id,
                    "platform": resolved_platform,
                    "target_address": resolved_target_address,
                    "is_current": is_current,
                    "home_workspace_id": home_ws_id,
                    "weixin_context_token": weixin_ctx_token,
                }


# ---------------------------------------------------------------------------
# I-7: Deliveries Progress, Inbound Progress, Steer Reconcile, Recover
# ---------------------------------------------------------------------------


def _count_delivered_files(items: Any) -> int:
    if not isinstance(items, list):
        return 0
    return sum(
        1
        for item in items
        if isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() in ("sent", "linked")
    )


async def update_delivery_progress(
    *,
    delivery_key: str,
    sent_chunks: int | None = None,
    total_chunks: int | None = None,
    platform_message_ids: list[Any] | None = None,
    files_status: list[Any] | None = None,
    first_response_json: dict[str, Any] | None = None,
    status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-7 (1): Update `dalton_channel_deliveries`, increment `sent_to_current_count`, and update conversation activity timestamp."""
    return await _run_tx_with_retry(
        "update_delivery_progress",
        conn,
        lambda c: _update_delivery_progress_once(
            delivery_key=delivery_key,
            sent_chunks=sent_chunks,
            total_chunks=total_chunks,
            platform_message_ids=platform_message_ids,
            files_status=files_status,
            first_response_json=first_response_json,
            status=status,
            error_code=error_code,
            error_message=error_message,
            conn=c,
        ),
    )


async def _update_delivery_progress_once(
    *,
    delivery_key: str,
    sent_chunks: int | None = None,
    total_chunks: int | None = None,
    platform_message_ids: list[Any] | None = None,
    files_status: list[Any] | None = None,
    first_response_json: dict[str, Any] | None = None,
    status: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_key = str(delivery_key or "").strip()
    if not cleaned_key:
        raise HTTPException(status_code=400, detail="delivery_key is required")

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                # 0. Serialize with authorize_and_claim_delivery on the same delivery_key
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_delivery:{cleaned_key}",),
                )
                # 1. Pre-read immutable origin metadata to lock session generation row BEFORE delivery row
                await cur.execute(
                    """
                    SELECT user_id, platform, source_scope_key, source_generation, activity_origin
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    """,
                    (cleaned_key,),
                )
                pre_row = await cur.fetchone()
                if not pre_row:
                    raise HTTPException(
                        status_code=404, detail="Delivery record not found"
                    )

                locked_session_row: dict[str, Any] | None = None
                db_now: datetime | None = None
                src_uid = str(pre_row.get("user_id") or "").strip()
                src_plat = str(pre_row.get("platform") or "").strip().lower()
                src_scope = str(pre_row.get("source_scope_key") or "").strip()
                src_gen = pre_row.get("source_generation")
                act_origin = str(pre_row.get("activity_origin") or "none").strip().lower()

                if (
                    act_origin == "conversation"
                    and src_uid
                    and src_plat
                    and src_scope
                    and src_gen is not None
                ):
                    await cur.execute(
                        """
                        SELECT generation, override_workspace_id,
                               last_conversation_activity_at, selection_initialized_at
                        FROM dalton_channel_session_generations
                        WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                        FOR UPDATE
                        """,
                        (src_plat, src_scope, src_uid),
                    )
                    s_row = await cur.fetchone()
                    if s_row:
                        locked_session_row = dict(s_row)
                    db_now = await _fetch_db_now(cur)

                # 2. Lock delivery row FOR UPDATE
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    FOR UPDATE
                    """,
                    (cleaned_key,),
                )
                row = await cur.fetchone()
                if not row:
                    raise HTTPException(
                        status_code=404, detail="Delivery record not found"
                    )

                locked_uid = str(row.get("user_id") or "").strip()
                locked_plat = str(row.get("platform") or "").strip().lower()
                locked_scope = str(row.get("source_scope_key") or "").strip()
                locked_gen = row.get("source_generation")
                locked_origin = (
                    str(row.get("activity_origin") or "none").strip().lower()
                )

                prev_status = str(row["status"])
                new_status = str(status or prev_status)
                prev_sent_chunks = int(row.get("sent_chunks") or 0)
                req_sent_chunks = (
                    int(sent_chunks)
                    if sent_chunks is not None
                    else prev_sent_chunks
                )
                new_sent_chunks = max(prev_sent_chunks, req_sent_chunks)
                new_total_chunks = (
                    int(total_chunks)
                    if total_chunks is not None
                    else int(row.get("total_chunks") or 0)
                )
                new_msg_ids = (
                    platform_message_ids
                    if platform_message_ids is not None
                    else (row.get("platform_message_ids") or [])
                )
                prev_files = row.get("files_status") or []
                prev_delivered_files = _count_delivered_files(prev_files)
                if files_status is not None:
                    cand_delivered_files = _count_delivered_files(files_status)
                    new_files_status = (
                        files_status
                        if cand_delivered_files >= prev_delivered_files
                        else prev_files
                    )
                else:
                    cand_delivered_files = prev_delivered_files
                    new_files_status = prev_files

                new_first_resp = (
                    first_response_json
                    if first_response_json is not None
                    else row.get("first_response_json")
                )

                await cur.execute(
                    """
                    UPDATE dalton_channel_deliveries
                    SET sent_chunks = %s,
                        total_chunks = %s,
                        platform_message_ids = %s,
                        files_status = %s,
                        first_response_json = %s,
                        status = %s,
                        error_code = COALESCE(%s, error_code),
                        error_message = COALESCE(%s, error_message),
                        updated_at = NOW()
                    WHERE delivery_key = %s
                    """,
                    (
                        new_sent_chunks,
                        new_total_chunks,
                        Jsonb(new_msg_ids),
                        Jsonb(new_files_status),
                        Jsonb(new_first_resp) if new_first_resp is not None else None,
                        new_status,
                        error_code,
                        error_message,
                        cleaned_key,
                    ),
                )

                # 3. Conditionally advance last_conversation_activity_at when new conversation output was delivered
                made_new_output_progress = (
                    new_sent_chunks > prev_sent_chunks
                    or cand_delivered_files > prev_delivered_files
                )
                if (
                    made_new_output_progress
                    and locked_session_row is not None
                    and db_now is not None
                    and locked_origin == "conversation"
                    and locked_uid == src_uid
                    and locked_plat == src_plat
                    and locked_scope == src_scope
                    and locked_gen is not None
                    and src_gen is not None
                    and int(locked_gen) == int(src_gen)
                ):
                    cur_s_gen = int(locked_session_row.get("generation") or 0)
                    init_at = locked_session_row.get("selection_initialized_at")
                    last_act = locked_session_row.get("last_conversation_activity_at")
                    if (
                        cur_s_gen == int(locked_gen)
                        and init_at is not None
                        and not _is_session_expired(last_act, db_now)
                    ):
                        await cur.execute(
                            """
                            UPDATE dalton_channel_session_generations
                            SET last_conversation_activity_at = %s,
                                updated_at = %s
                            WHERE platform = %s
                              AND scope_key = %s
                              AND dalton_user_id = %s
                              AND generation = %s
                              AND selection_initialized_at IS NOT NULL
                            """,
                            (
                                db_now,
                                db_now,
                                locked_plat,
                                locked_scope,
                                locked_uid,
                                int(locked_gen),
                            ),
                        )

                updated_count = 0
                run_id_val = row.get("run_id")
                if (
                    bool(row.get("is_current"))
                    and bool(row.get("has_non_empty_text"))
                    and new_status in ("sent", "partial")
                    and prev_status not in ("sent", "partial")
                    and run_id_val
                ):
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET sent_to_current_count = sent_to_current_count + 1,
                            updated_at = NOW()
                        WHERE run_id = %s
                        RETURNING sent_to_current_count
                        """,
                        (str(run_id_val),),
                    )
                    in_res = await cur.fetchone()
                    if in_res:
                        updated_count = int(in_res["sent_to_current_count"])
                    await cur.execute(
                        """
                        UPDATE dalton_channel_background_runs
                        SET sent_to_current_count = sent_to_current_count + 1,
                            updated_at = NOW()
                        WHERE run_id = %s
                        RETURNING sent_to_current_count
                        """,
                        (str(run_id_val),),
                    )
                    bg_res = await cur.fetchone()
                    if bg_res and not updated_count:
                        updated_count = int(bg_res["sent_to_current_count"])

                return {
                    "ok": True,
                    "delivery_key": cleaned_key,
                    "status": new_status,
                    "sent_to_current_count": updated_count,
                }


async def update_inbound_progress(
    *,
    platform: str | None = None,
    message_id: str | None = None,
    status: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    turn_index: int | None = None,
    request_key: str | None = None,
    retry_request_key: str | None = None,
    resubmitted_request_key: str | None = None,
    steered_into_run_id: str | None = None,
    fallback_pending: bool | None = None,
    uploaded_attachments: list[Any] | None = None,
    raw_payload: dict[str, Any] | None = None,
    raw_payload_patch: dict[str, Any] | None = None,
    command_effect_status: str | None = None,
    command_reply_pending: bool | None = None,
    command_result: dict[str, Any] | None = None,
    session_interrupt_update: dict[str, Any] | None = None,
    background_run: dict[str, Any] | None = None,
    thread_watch: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-7 (2): Update inbound event, session pending_interrupt, thread_watches, and background_runs."""
    return await _run_tx_with_retry(
        "update_inbound_progress",
        conn,
        lambda c: _update_inbound_progress_once(
            platform=platform,
            message_id=message_id,
            status=status,
            thread_id=thread_id,
            run_id=run_id,
            turn_index=turn_index,
            request_key=request_key,
            retry_request_key=retry_request_key,
            resubmitted_request_key=resubmitted_request_key,
            steered_into_run_id=steered_into_run_id,
            fallback_pending=fallback_pending,
            uploaded_attachments=uploaded_attachments,
            raw_payload=raw_payload,
            raw_payload_patch=raw_payload_patch,
            command_effect_status=command_effect_status,
            command_reply_pending=command_reply_pending,
            command_result=command_result,
            session_interrupt_update=session_interrupt_update,
            background_run=background_run,
            thread_watch=thread_watch,
            conn=c,
        ),
    )


async def _update_inbound_progress_once(
    *,
    platform: str | None = None,
    message_id: str | None = None,
    status: str | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    turn_index: int | None = None,
    request_key: str | None = None,
    retry_request_key: str | None = None,
    resubmitted_request_key: str | None = None,
    steered_into_run_id: str | None = None,
    fallback_pending: bool | None = None,
    uploaded_attachments: list[Any] | None = None,
    raw_payload: dict[str, Any] | None = None,
    raw_payload_patch: dict[str, Any] | None = None,
    command_effect_status: str | None = None,
    command_reply_pending: bool | None = None,
    command_result: dict[str, Any] | None = None,
    session_interrupt_update: dict[str, Any] | None = None,
    background_run: dict[str, Any] | None = None,
    thread_watch: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    effective_run_id = str(run_id).strip() if run_id else None
    cleaned_p = platform.strip().lower() if platform and platform.strip() else None
    cleaned_mid = message_id.strip() if message_id and message_id.strip() else None

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                # 0. Acquire per-inbound advisory lock first (matches I-1 and I-3 lock ordering)
                if cleaned_p and cleaned_mid:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"dalton_inbound:{cleaned_p}:{cleaned_mid}",),
                    )

                # 1. Lock & update dalton_channel_session_generations FIRST (before inbound_events / watches / background_runs)
                watch_interrupt_patch: tuple[str, Any] | None = None
                session_is_current_for_interrupt: bool | None = None
                if session_interrupt_update:
                    si_plat = str(session_interrupt_update["platform"]).strip().lower()
                    si_scope = str(session_interrupt_update["scope_key"]).strip()
                    si_uid = str(session_interrupt_update["dalton_user_id"]).strip()
                    si_val = session_interrupt_update.get("pending_interrupt")
                    si_expected_gen = session_interrupt_update.get("generation")
                    if (
                        si_expected_gen is None
                        and isinstance(si_val, dict)
                        and si_val.get("generation") is not None
                    ):
                        si_expected_gen = int(si_val["generation"])
                    si_expected_tid = normalize_uuid(
                        session_interrupt_update.get("thread_id")
                        or (
                            si_val.get("thread_id")
                            if isinstance(si_val, dict)
                            else None
                        )
                        or thread_id
                        or (
                            thread_watch.get("thread_id")
                            if isinstance(thread_watch, dict)
                            else None
                        )
                    )
                    si_expected_rid = (
                        session_interrupt_update.get("run_id")
                        or (si_val.get("run_id") if isinstance(si_val, dict) else None)
                        or effective_run_id
                        or (
                            background_run.get("run_id")
                            if isinstance(background_run, dict)
                            else None
                        )
                    )
                    si_expected_iid = session_interrupt_update.get("interrupt_id") or (
                        si_val.get("interrupt_id")
                        if isinstance(si_val, dict)
                        else None
                    )

                    # Pre-read persisted source (without FOR UPDATE) to validate generation and scope before locking session
                    persisted_src: dict[str, Any] | None = None
                    if cleaned_p and cleaned_mid:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_inbound_events
                            WHERE platform = %s AND message_id = %s
                            """,
                            (cleaned_p, cleaned_mid),
                        )
                        in_src = await cur.fetchone()
                        if in_src:
                            persisted_src = dict(in_src)
                    if persisted_src is None and (si_expected_rid or si_expected_tid):
                        src_clauses: list[str] = ["dalton_user_id = %s"]
                        src_params: list[Any] = [si_uid]
                        if si_expected_rid:
                            src_clauses.append("run_id = %s")
                            src_params.append(str(si_expected_rid))
                        if si_expected_tid:
                            src_clauses.append("thread_id = %s")
                            src_params.append(si_expected_tid)
                        await cur.execute(
                            f"""
                            SELECT dalton_user_id, platform, chat_address AS target_address,
                                   thread_scope_key, workspace_id, session_generation,
                                   external_thread_id, raw_payload, thread_id, run_id
                            FROM dalton_channel_inbound_events
                            WHERE {" AND ".join(src_clauses)}
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            tuple(src_params),
                        )
                        in_by_rt = await cur.fetchone()
                        if in_by_rt:
                            persisted_src = dict(in_by_rt)
                    if (
                        (
                            persisted_src is None
                            or persisted_src.get("session_generation") is None
                        )
                        and si_expected_tid
                    ):
                        await cur.execute(
                            """
                            SELECT dalton_user_id, platform, target_address,
                                   scope_key, generation, thread_id
                            FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            """,
                            (si_expected_tid,),
                        )
                        w_src = await cur.fetchone()
                        if w_src:
                            persisted_src = dict(w_src)

                    src_plat_ok = True
                    src_uid_ok = True
                    src_scope_ok = True
                    persisted_gen_val: int | None = None
                    if persisted_src is not None:
                        p_plat = str(persisted_src.get("platform") or "").strip().lower()
                        p_uid = str(persisted_src.get("dalton_user_id") or "").strip()
                        p_scope = str(
                            persisted_src.get("thread_scope_key")
                            or persisted_src.get("scope_key")
                            or ""
                        ).strip()
                        src_plat_ok = not p_plat or p_plat == si_plat
                        src_uid_ok = not p_uid or p_uid == si_uid
                        src_scope_ok = not p_scope or p_scope == si_scope
                        if persisted_src.get("session_generation") is not None:
                            persisted_gen_val = int(persisted_src["session_generation"])
                        elif persisted_src.get("generation") is not None:
                            persisted_gen_val = int(persisted_src["generation"])
                        elif (
                            isinstance(persisted_src.get("raw_payload"), dict)
                            and persisted_src["raw_payload"].get("generation")
                            is not None
                        ):
                            persisted_gen_val = int(
                                persisted_src["raw_payload"]["generation"]
                            )
                        elif persisted_src.get("external_thread_id") and ":gen:" in str(
                            persisted_src["external_thread_id"]
                        ):
                            gen_s = (
                                str(persisted_src["external_thread_id"])
                                .rsplit(":gen:", 1)[-1]
                                .strip()
                            )
                            if gen_s.isdigit():
                                persisted_gen_val = int(gen_s)

                    s_row, _, s_db_now = await _lock_or_create_session_row(
                        cur,
                        platform=si_plat,
                        scope_key=si_scope,
                        dalton_user_id=si_uid,
                    )
                    cur_s_gen = int(s_row.get("generation") or 1)
                    cur_s_int = s_row.get("pending_interrupt")
                    is_s_expired = (
                        _is_session_expired(
                            s_row.get("last_conversation_activity_at"), s_db_now
                        )
                        and s_row.get("override_workspace_id") is not None
                    )

                    # Resolve authoritative generation for interrupt check (never allow None to match by default)
                    eff_expected_gen: int | None = None
                    gen_conflict = False
                    if (
                        si_expected_gen is not None
                        and persisted_gen_val is not None
                        and int(si_expected_gen) != persisted_gen_val
                    ):
                        gen_conflict = True
                    elif persisted_gen_val is not None and persisted_gen_val > 0:
                        eff_expected_gen = persisted_gen_val
                    elif (
                        si_expected_gen is not None
                        and int(si_expected_gen) > 0
                        and (
                            persisted_src is not None
                            or (
                                isinstance(thread_watch, dict)
                                and normalize_uuid(thread_watch.get("thread_id"))
                                == si_expected_tid
                                and str(thread_watch.get("dalton_user_id") or "").strip()
                                == si_uid
                                and str(thread_watch.get("platform") or "")
                                .strip()
                                .lower()
                                == si_plat
                                and str(thread_watch.get("scope_key") or "").strip()
                                == si_scope
                                and int(thread_watch.get("generation") or 0)
                                == int(si_expected_gen)
                            )
                            or (
                                si_val is None
                                and isinstance(cur_s_int, dict)
                                and (
                                    si_expected_iid is not None
                                    or si_expected_rid is not None
                                    or si_expected_tid is not None
                                )
                            )
                        )
                    ):
                        eff_expected_gen = int(si_expected_gen)

                    gen_matches_current = (
                        not is_s_expired
                        and not gen_conflict
                        and src_plat_ok
                        and src_uid_ok
                        and src_scope_ok
                        and eff_expected_gen is not None
                        and eff_expected_gen == cur_s_gen
                    )
                    session_is_current_for_interrupt = gen_matches_current

                    should_update_session_int = False
                    if si_val is not None:
                        if gen_matches_current:
                            should_update_session_int = True
                    else:
                        if (
                            gen_matches_current
                            and isinstance(cur_s_int, dict)
                            and (
                                si_expected_iid is not None
                                or si_expected_rid is not None
                                or si_expected_tid is not None
                            )
                        ):
                            should_update_session_int = True
                            if (
                                cur_s_int.get("generation") is not None
                                and int(cur_s_int["generation"]) != cur_s_gen
                            ):
                                should_update_session_int = False
                            elif (
                                si_expected_iid
                                and cur_s_int.get("interrupt_id")
                                and str(cur_s_int["interrupt_id"])
                                != str(si_expected_iid)
                            ):
                                should_update_session_int = False
                            elif (
                                si_expected_rid
                                and cur_s_int.get("run_id")
                                and str(cur_s_int["run_id"])
                                != str(si_expected_rid)
                            ):
                                should_update_session_int = False
                            elif (
                                si_expected_tid
                                and cur_s_int.get("thread_id")
                                and normalize_uuid(cur_s_int["thread_id"])
                                != si_expected_tid
                            ):
                                should_update_session_int = False

                    if should_update_session_int:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_session_generations
                            SET pending_interrupt = %s,
                                updated_at = NOW()
                            WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s
                            """,
                            (
                                Jsonb(si_val) if si_val is not None else None,
                                si_plat,
                                si_scope,
                                si_uid,
                            ),
                        )
                    if si_expected_tid:
                        watch_interrupt_patch = (si_expected_tid, si_val)

                # If command_effect_status == 'done' is being reported for a pending_cancel command row,
                # lock session row first and update activity ONLY on the first valid transition pending_cancel -> done
                if (
                    cleaned_p
                    and cleaned_mid
                    and command_effect_status == "done"
                    and status != "failed"
                    and not session_interrupt_update
                ):
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (cleaned_p, cleaned_mid),
                    )
                    cmd_pre = await cur.fetchone()
                    if (
                        cmd_pre
                        and cmd_pre.get("event_kind") == "command"
                        and cmd_pre.get("command_effect_status") == "pending_cancel"
                        and cmd_pre.get("dalton_user_id")
                        and cmd_pre.get("thread_scope_key")
                        and cmd_pre.get("session_generation") is not None
                    ):
                        c_uid = str(cmd_pre["dalton_user_id"])
                        c_scope = str(cmd_pre["thread_scope_key"])
                        c_gen = int(cmd_pre["session_generation"])
                        s_row, _, db_now = await _lock_or_create_session_row(
                            cur,
                            platform=cleaned_p,
                            scope_key=c_scope,
                            dalton_user_id=c_uid,
                        )
                        if int(
                            s_row.get("generation") or 0
                        ) == c_gen and not _is_session_expired(
                            s_row.get("last_conversation_activity_at"), db_now
                        ):
                            await cur.execute(
                                """
                                UPDATE dalton_channel_session_generations
                                SET last_conversation_activity_at = %s,
                                    updated_at = %s
                                WHERE platform = %s AND scope_key = %s AND dalton_user_id = %s AND generation = %s
                                """,
                                (db_now, db_now, cleaned_p, c_scope, c_uid, c_gen),
                            )

                # 2. Update dalton_channel_inbound_events
                if cleaned_p and cleaned_mid:
                    sets: list[str] = ["updated_at = NOW()"]
                    params: list[Any] = []
                    if status is not None:
                        sets.append("status = %s")
                        params.append(status)
                    if thread_id is not None:
                        sets.append("thread_id = %s")
                        params.append(normalize_uuid(thread_id))
                    if run_id is not None:
                        sets.append("run_id = %s")
                        params.append(str(run_id))
                    if turn_index is not None:
                        sets.append("turn_index = %s")
                        params.append(int(turn_index))
                    if request_key is not None:
                        sets.append("request_key = %s")
                        params.append(normalize_uuid(request_key))
                    if retry_request_key is not None:
                        sets.append("retry_request_key = %s")
                        params.append(normalize_uuid(retry_request_key))
                    if resubmitted_request_key is not None:
                        sets.append("resubmitted_request_key = %s")
                        params.append(normalize_uuid(resubmitted_request_key))
                    if steered_into_run_id is not None:
                        sets.append("steered_into_run_id = %s")
                        params.append(str(steered_into_run_id))
                    if fallback_pending is not None:
                        sets.append("fallback_pending = %s")
                        params.append(bool(fallback_pending))
                    if uploaded_attachments is not None:
                        sets.append("uploaded_attachments = %s")
                        params.append(Jsonb(uploaded_attachments))
                    if command_effect_status is not None:
                        sets.append("command_effect_status = %s")
                        params.append(str(command_effect_status))
                    if command_reply_pending is not None:
                        sets.append("command_reply_pending = %s")
                        params.append(bool(command_reply_pending))
                    if command_result is not None:
                        if not isinstance(command_result, dict):
                            raise HTTPException(
                                status_code=400,
                                detail="command_result must be an object",
                            )
                        sets.append("command_result = %s")
                        params.append(Jsonb(command_result))
                    if raw_payload is not None:
                        sets.append("raw_payload = %s")
                        params.append(Jsonb(raw_payload))
                    elif raw_payload_patch is not None:
                        sets.append(
                            "raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s"
                        )
                        params.append(Jsonb(raw_payload_patch))

                    params.extend([cleaned_p, cleaned_mid])
                    await cur.execute(
                        f"""
                        UPDATE dalton_channel_inbound_events
                        SET {", ".join(sets)}
                        WHERE platform = %s AND message_id = %s
                        RETURNING run_id, sent_to_current_count
                        """,
                        tuple(params),
                    )
                    in_row = await cur.fetchone()
                    if in_row and in_row.get("run_id") and not effective_run_id:
                        effective_run_id = str(in_row["run_id"])

                # 3. Update dalton_channel_thread_watches BEFORE dalton_channel_background_runs
                if thread_watch:
                    tw_tid = normalize_uuid(thread_watch["thread_id"])
                    tw_uid = str(thread_watch["dalton_user_id"]).strip()
                    tw_plat = str(thread_watch["platform"]).strip().lower()
                    tw_addr = str(thread_watch["target_address"]).strip()
                    tw_scope = str(thread_watch.get("scope_key") or "").strip()
                    raw_tw_gen = thread_watch.get("generation")
                    tw_gen = (
                        int(raw_tw_gen)
                        if raw_tw_gen is not None and int(raw_tw_gen) > 0
                        else 0
                    )
                    if not tw_scope or tw_scope == tw_addr or tw_gen <= 0:
                        await cur.execute(
                            """
                            SELECT dalton_user_id, platform, target_address,
                                   scope_key, generation, thread_id
                            FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            """,
                            (tw_tid,),
                        )
                        ex_tw = await cur.fetchone()
                        if ex_tw:
                            if (not tw_scope or tw_scope == tw_addr) and ex_tw.get(
                                "scope_key"
                            ):
                                tw_scope = str(ex_tw["scope_key"]).strip()
                            if tw_gen <= 0 and ex_tw.get("generation"):
                                tw_gen = int(ex_tw["generation"])
                    if not tw_scope:
                        tw_scope = tw_addr
                    if tw_gen <= 0:
                        tw_gen = 1

                    tw_last_turn = int(thread_watch.get("last_seen_turn_index") or 0)
                    tw_active = bool(thread_watch.get("watch_active", True))
                    has_tw_int = "pending_interrupt" in thread_watch
                    tw_int = thread_watch.get("pending_interrupt")
                    if not has_tw_int and watch_interrupt_patch and watch_interrupt_patch[0] == tw_tid:
                        has_tw_int = True
                        tw_int = watch_interrupt_patch[1]
                        watch_interrupt_patch = None

                    if has_tw_int:
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_thread_watches (
                                thread_id, dalton_user_id, platform, target_address,
                                scope_key, generation, last_seen_turn_index, watch_active,
                                pending_interrupt, created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                            ON CONFLICT (thread_id) DO UPDATE SET
                                target_address = EXCLUDED.target_address,
                                scope_key = EXCLUDED.scope_key,
                                generation = EXCLUDED.generation,
                                last_seen_turn_index = GREATEST(
                                    dalton_channel_thread_watches.last_seen_turn_index,
                                    EXCLUDED.last_seen_turn_index
                                ),
                                watch_active = EXCLUDED.watch_active,
                                pending_interrupt = EXCLUDED.pending_interrupt,
                                updated_at = NOW()
                            """,
                            (
                                tw_tid,
                                tw_uid,
                                tw_plat,
                                tw_addr,
                                tw_scope,
                                tw_gen,
                                tw_last_turn,
                                tw_active,
                                Jsonb(tw_int) if tw_int is not None else None,
                            ),
                        )
                    else:
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_thread_watches (
                                thread_id, dalton_user_id, platform, target_address,
                                scope_key, generation, last_seen_turn_index, watch_active,
                                created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                            ON CONFLICT (thread_id) DO UPDATE SET
                                target_address = EXCLUDED.target_address,
                                scope_key = EXCLUDED.scope_key,
                                generation = EXCLUDED.generation,
                                last_seen_turn_index = GREATEST(
                                    dalton_channel_thread_watches.last_seen_turn_index,
                                    EXCLUDED.last_seen_turn_index
                                ),
                                watch_active = EXCLUDED.watch_active,
                                updated_at = NOW()
                            """,
                            (
                                tw_tid,
                                tw_uid,
                                tw_plat,
                                tw_addr,
                                tw_scope,
                                tw_gen,
                                tw_last_turn,
                                tw_active,
                            ),
                        )

                if watch_interrupt_patch is not None:
                    patch_tid, patch_int = watch_interrupt_patch
                    await cur.execute(
                        """
                        UPDATE dalton_channel_thread_watches
                        SET pending_interrupt = %s,
                            updated_at = NOW()
                        WHERE thread_id = %s
                        """,
                        (
                            Jsonb(patch_int) if patch_int is not None else None,
                            patch_tid,
                        ),
                    )

                # 4. Update dalton_channel_background_runs
                if background_run:
                    bg_rid = str(background_run["run_id"]).strip()
                    bg_tid = normalize_uuid(background_run["thread_id"])
                    bg_uid = str(background_run["dalton_user_id"]).strip()
                    bg_plat = str(background_run["platform"]).strip().lower()
                    bg_addr = str(background_run["target_address"]).strip()
                    bg_tidx = background_run.get("turn_index")
                    bg_status = str(background_run.get("status") or "streaming")
                    bg_fb = bool(background_run.get("fallback_pending", True))
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_background_runs (
                            run_id, thread_id, dalton_user_id, platform, target_address,
                            turn_index, status, fallback_pending, created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        ON CONFLICT (run_id) DO UPDATE SET
                            turn_index = COALESCE(EXCLUDED.turn_index, dalton_channel_background_runs.turn_index),
                            status = EXCLUDED.status,
                            fallback_pending = EXCLUDED.fallback_pending,
                            updated_at = NOW()
                        """,
                        (
                            bg_rid,
                            bg_tid,
                            bg_uid,
                            bg_plat,
                            bg_addr,
                            int(bg_tidx) if bg_tidx is not None else None,
                            bg_status,
                            bg_fb,
                        ),
                    )
                    if not effective_run_id:
                        effective_run_id = bg_rid

                has_in_flight = False
                has_unknown = False
                sent_count = 0
                if effective_run_id:
                    await cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM dalton_channel_deliveries
                            WHERE run_id = %s
                              AND is_current = TRUE
                              AND has_non_empty_text = TRUE
                              AND status = 'in_flight'
                        ) AS in_flight
                        """,
                        (effective_run_id,),
                    )
                    has_in_flight = bool((await cur.fetchone())["in_flight"])
                    await cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1 FROM dalton_channel_deliveries
                            WHERE run_id = %s
                              AND is_current = TRUE
                              AND has_non_empty_text = TRUE
                              AND status = 'unknown'
                        ) AS has_unknown_text
                        """,
                        (effective_run_id,),
                    )
                    has_unknown = bool((await cur.fetchone())["has_unknown_text"])
                    await cur.execute(
                        """
                        SELECT COALESCE(MAX(sent_to_current_count), 0) AS cnt
                        FROM (
                            SELECT sent_to_current_count FROM dalton_channel_inbound_events WHERE run_id = %s
                            UNION ALL
                            SELECT sent_to_current_count FROM dalton_channel_background_runs WHERE run_id = %s
                        ) t
                        """,
                        (effective_run_id, effective_run_id),
                    )
                    sent_count = int((await cur.fetchone())["cnt"])

                return {
                    "ok": True,
                    "run_id": effective_run_id,
                    "has_in_flight_current_text": has_in_flight,
                    "has_unknown_current_text": has_unknown,
                    "has_in_flight_or_unknown_current_text": has_in_flight or has_unknown,
                    "sent_to_current_count": sent_count,
                    "session_is_current_for_interrupt": session_is_current_for_interrupt,
                }


async def reconcile_steer(
    *,
    platform: str,
    message_id: str,
    run_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-7 (3): Reconcile `hook_outbox` steer outcome with `conversation_responses.status` (§1.3.2 & §1.3.1 #3)."""
    return await _run_tx_with_retry(
        "reconcile_steer",
        conn,
        lambda c: _reconcile_steer_once(
            platform=platform,
            message_id=message_id,
            run_id=run_id,
            conn=c,
        ),
    )


async def _reconcile_steer_once(
    *,
    platform: str,
    message_id: str,
    run_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_platform = str(platform or "").strip().lower()
    cleaned_mid = str(message_id or "").strip()

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_inbound:{cleaned_platform}:{cleaned_mid}",),
                )
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_inbound_events
                    WHERE platform = %s AND message_id = %s
                    FOR UPDATE
                    """,
                    (cleaned_platform, cleaned_mid),
                )
                in_row = await cur.fetchone()
                if not in_row:
                    raise HTTPException(
                        status_code=404, detail="Inbound event not found"
                    )

                raw_p = dict(in_row.get("raw_payload") or {})
                credit_steer_key = raw_p.get("credit_steer_request_key")
                effective_req_key = str(credit_steer_key or in_row["request_key"])
                is_credit_steer = bool(credit_steer_key)

                await cur.execute(
                    """
                    SELECT status, payload
                    FROM hook_outbox
                    WHERE hook_type = 'steer' AND idempotency_key = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (f"steer:{effective_req_key}",),
                )
                outbox_row = await cur.fetchone()
                outbox_status = outbox_row["status"] if outbox_row else None
                outbox_payload = (outbox_row.get("payload") or {}) if outbox_row else {}
                outbox_outcome = outbox_payload.get("outcome")

                target_run_id = (
                    str(
                        run_id
                        or in_row.get("steered_into_run_id")
                        or in_row.get("run_id")
                        or outbox_payload.get("returned_run_id")
                        or outbox_payload.get("steered_run_id")
                        or ""
                    ).strip()
                    or None
                )

                run_status: str | None = None
                if target_run_id and normalize_uuid(target_run_id):
                    await cur.execute(
                        """
                        SELECT status FROM conversation_responses
                        WHERE conversation_response_id = %s
                        """,
                        (normalize_uuid(target_run_id),),
                    )
                    r_row = await cur.fetchone()
                    if r_row:
                        run_status = str(r_row["status"])

                # 1. Consumed by run ('delivered' | 'turn' | 'resumed')
                if outbox_status == "done" and outbox_outcome in (
                    "delivered",
                    "turn",
                    "resumed",
                ):
                    if is_credit_steer:
                        raw_p["credit_steer_state"] = "settled"
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET status = 'completed',
                            fallback_pending = FALSE,
                            raw_payload = %s,
                            updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        """,
                        (Jsonb(raw_p), cleaned_platform, cleaned_mid),
                    )
                    return {
                        "ok": True,
                        "reconcile_status": "consumed",
                        "outcome": outbox_outcome,
                        "run_status": run_status,
                        "inbound_status": "completed",
                        "resubmitted_request_key": None,
                    }

                # 2. Returned or dropped (or credit steer not_running when no outbox row was created AND the run has already reached a terminal state)
                terminal_run_statuses = (
                    "completed",
                    "error",
                    "failed",
                    "cancelled",
                    "interrupted",
                )
                if (
                    outbox_status == "done"
                    and outbox_outcome in ("returned", "dropped")
                ) or (
                    outbox_row is None
                    and is_credit_steer
                    and target_run_id is not None
                    and run_status in terminal_run_statuses
                ):
                    eff_outcome = outbox_outcome or "not_running"
                    if eff_outcome in ("returned", "not_running") and (
                        run_status == "completed"
                    ):
                        resub_key = str(
                            uuid5(
                                DALTON_CHANNEL_NAMESPACE,
                                f"{cleaned_platform}:{cleaned_mid}:returned:{target_run_id}",
                            )
                        )
                        if is_credit_steer:
                            raw_p["credit_steer_state"] = "resubmitted"
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET resubmitted_request_key = %s,
                                status = 'pending_resubmit',
                                fallback_pending = FALSE,
                                raw_payload = %s,
                                updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                            """,
                            (
                                resub_key,
                                Jsonb(raw_p),
                                cleaned_platform,
                                cleaned_mid,
                            ),
                        )
                        return {
                            "ok": True,
                            "reconcile_status": "pending_resubmit",
                            "outcome": eff_outcome,
                            "run_status": run_status,
                            "inbound_status": "pending_resubmit",
                            "resubmitted_request_key": resub_key,
                        }

                    new_in_status = (
                        "completed" if is_credit_steer else "returned_unresubmitted"
                    )
                    if is_credit_steer:
                        raw_p["credit_steer_state"] = "settled"
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET status = %s,
                            fallback_pending = FALSE,
                            raw_payload = %s,
                            updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        """,
                        (
                            new_in_status,
                            Jsonb(raw_p),
                            cleaned_platform,
                            cleaned_mid,
                        ),
                    )
                    return {
                        "ok": True,
                        "reconcile_status": "returned_unresubmitted",
                        "outcome": eff_outcome,
                        "run_status": run_status,
                        "inbound_status": new_in_status,
                        "resubmitted_request_key": None,
                    }

                return {
                    "ok": True,
                    "reconcile_status": "still_pending",
                    "outcome": outbox_outcome,
                    "run_status": run_status,
                    "inbound_status": in_row["status"],
                    "resubmitted_request_key": (
                        str(in_row["resubmitted_request_key"])
                        if in_row.get("resubmitted_request_key")
                        else None
                    ),
                }


async def _backfill_watch_notification_runs_in_tx(
    cur: Any, watch_rows: list[dict[str, Any]]
) -> None:
    """Scan `conversation_queries` (`type IN ('system', 'market_target', 'wakeup')`) > `last_seen_turn_index` and insert into `dalton_channel_background_runs`."""
    for watch in watch_rows:
        w_tid = watch["thread_id"]
        w_uid = str(watch["dalton_user_id"])
        w_plat = str(watch["platform"])
        w_addr = str(watch["target_address"])
        last_turn = int(watch.get("last_seen_turn_index") or 0)

        await cur.execute(
            """
            SELECT r.conversation_response_id AS run_id,
                   q.turn_index
            FROM conversation_queries q
            JOIN conversation_responses r
              ON r.conversation_thread_id = q.conversation_thread_id
             AND r.turn_index = q.turn_index
            WHERE q.conversation_thread_id = %s
              AND q.type IN ('system', 'market_target', 'wakeup')
              AND q.turn_index > %s
            ORDER BY q.turn_index ASC, r.run_seq ASC
            """,
            (w_tid, last_turn),
        )
        found_runs = await cur.fetchall()
        max_turn = last_turn
        for fr in found_runs:
            t_idx = int(fr["turn_index"])
            if t_idx > max_turn:
                max_turn = t_idx
            await cur.execute(
                """
                INSERT INTO dalton_channel_background_runs (
                    run_id, thread_id, dalton_user_id, platform, target_address,
                    turn_index, status, fallback_pending, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'streaming', TRUE, NOW(), NOW())
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    str(fr["run_id"]),
                    w_tid,
                    w_uid,
                    w_plat,
                    w_addr,
                    t_idx,
                ),
            )
        if max_turn > last_turn:
            await cur.execute(
                """
                UPDATE dalton_channel_thread_watches
                SET last_seen_turn_index = %s, updated_at = NOW()
                WHERE thread_id = %s
                """,
                (max_turn, w_tid),
            )


async def recover_channel_state(
    *,
    mode: str = "startup",
    thread_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-7 (4): Recover channel gateway state on `startup` or `watch_reconnect`."""
    cleaned_mode = str(mode or "startup").strip().lower()
    if cleaned_mode not in ("startup", "watch_reconnect"):
        raise HTTPException(status_code=400, detail=f"Unsupported recover mode: {mode!r}")
    norm_tid = normalize_uuid(thread_id) if thread_id else None

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                if cleaned_mode == "watch_reconnect":
                    # Strictly do NOT touch dalton_channel_deliveries in_flight rows!
                    if norm_tid:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            FOR UPDATE
                            """,
                            (norm_tid,),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_thread_watches
                            WHERE watch_active = TRUE
                            FOR UPDATE
                            """
                        )
                    watches = [dict(r) for r in await cur.fetchall()]
                    await _backfill_watch_notification_runs_in_tx(cur, watches)

                    if norm_tid:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_background_runs
                            WHERE thread_id = %s AND fallback_pending = TRUE
                            ORDER BY created_at ASC
                            """,
                            (norm_tid,),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_background_runs
                            WHERE fallback_pending = TRUE
                            ORDER BY created_at ASC
                            """
                        )
                    bg_runs = [dict(r) for r in await cur.fetchall()]
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_thread_watches
                        WHERE (%s::uuid IS NULL AND watch_active = TRUE)
                           OR thread_id = %s
                        """,
                        (norm_tid, norm_tid),
                    )
                    updated_watches = [dict(r) for r in await cur.fetchall()]
                    watch_by_tid = {
                        str(w["thread_id"]): w
                        for w in updated_watches
                        if w.get("thread_id")
                    }
                    for bgr in bg_runs:
                        w_match = watch_by_tid.get(str(bgr.get("thread_id") or ""))
                        if w_match:
                            if not bgr.get("scope_key") and w_match.get("scope_key"):
                                bgr["scope_key"] = w_match["scope_key"]
                            if bgr.get("generation") is None and w_match.get("generation") is not None:
                                bgr["generation"] = w_match["generation"]
                    return {
                        "mode": "watch_reconnect",
                        "background_runs": _json_safe_rows(bg_runs),
                        "thread_watches": _json_safe_rows(updated_watches),
                    }

                # mode == "startup"
                # ① Mark all lingering in_flight deliveries as unknown / gateway_restarted
                await cur.execute(
                    """
                    UPDATE dalton_channel_deliveries
                    SET status = 'unknown',
                        error_code = 'gateway_restarted',
                        updated_at = NOW()
                    WHERE status = 'in_flight'
                    """
                )

                # ② Backfill notification/wakeup turns for all active thread watches
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_thread_watches
                    WHERE watch_active = TRUE
                    FOR UPDATE
                    """
                )
                active_watches = [dict(r) for r in await cur.fetchall()]
                await _backfill_watch_notification_runs_in_tx(cur, active_watches)

                # Heal any historical pending_cancel command rows that lack a pinned target run_id
                await cur.execute(
                    """
                    UPDATE dalton_channel_inbound_events
                    SET command_effect_status = 'done',
                        updated_at = NOW()
                    WHERE command_effect_status = 'pending_cancel'
                      AND (thread_id IS NULL OR run_id IS NULL OR btrim(run_id) = '')
                    """
                )

                # ③ Fetch all 4 recovery sets
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_inbound_events
                    WHERE status IN ('pending', 'accepted', 'streaming', 'steered', 'pending_resubmit')
                       OR fallback_pending = TRUE
                       OR command_reply_pending = TRUE
                       OR command_effect_status = 'pending_cancel'
                    ORDER BY created_at ASC
                    """
                )
                inbound_events = [dict(r) for r in await cur.fetchall()]
                cleaned_inbound_events: list[dict[str, Any]] = []
                for r in inbound_events:
                    if r.get("command_effect_status") == "pending_cancel" and not (
                        r.get("thread_id") and str(r.get("run_id") or "").strip()
                    ):
                        r["command_effect_status"] = "done"
                        if (
                            r.get("status")
                            not in (
                                "pending",
                                "accepted",
                                "streaming",
                                "steered",
                                "pending_resubmit",
                            )
                            and not r.get("fallback_pending")
                            and not r.get("command_reply_pending")
                        ):
                            continue
                    cleaned_inbound_events.append(r)
                inbound_events = cleaned_inbound_events

                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_background_runs
                    WHERE fallback_pending = TRUE
                    ORDER BY created_at ASC
                    """
                )
                background_runs = [dict(r) for r in await cur.fetchall()]

                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_thread_watches
                    WHERE watch_active = TRUE
                    ORDER BY created_at ASC
                    """
                )
                thread_watches = [dict(r) for r in await cur.fetchall()]
                watch_by_tid = {
                    str(w["thread_id"]): w
                    for w in thread_watches
                    if w.get("thread_id")
                }
                for bgr in background_runs:
                    w_match = watch_by_tid.get(str(bgr.get("thread_id") or ""))
                    if w_match:
                        if not bgr.get("scope_key") and w_match.get("scope_key"):
                            bgr["scope_key"] = w_match["scope_key"]
                        if bgr.get("generation") is None and w_match.get("generation") is not None:
                            bgr["generation"] = w_match["generation"]

                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_automation_runs
                    WHERE status = 'finishing'
                    ORDER BY created_at ASC
                    """
                )
                automation_runs = [dict(r) for r in await cur.fetchall()]

                return {
                    "mode": "startup",
                    "inbound_events": _json_safe_rows(inbound_events),
                    "background_runs": _json_safe_rows(background_runs),
                    "thread_watches": _json_safe_rows(thread_watches),
                    "automation_runs": _json_safe_rows(automation_runs),
                }


def _json_safe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {}
        for k, v in row.items():
            if isinstance(v, UUID):
                item[k] = str(v)
            elif hasattr(v, "isoformat"):
                item[k] = v.isoformat()
            else:
                item[k] = v
        if "session_generation" in item and "generation" not in item:
            item["generation"] = item["session_generation"]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# I-8: Automation Runs Register & Finish
# ---------------------------------------------------------------------------


async def register_automation_run(
    *,
    execution_id: str,
    user_id: str,
    workspace_id: str | None = None,
    automation_name: str = "",
    entries: list[str],
    thread_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-8 (1): Idempotently register automation run and resolve `entries` into `targets`."""
    norm_exec_id = normalize_uuid(execution_id)
    if not norm_exec_id:
        raise HTTPException(status_code=400, detail="Valid execution_id UUID is required")
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        raise HTTPException(status_code=400, detail="user_id is required")
    norm_ws_id = normalize_uuid(workspace_id) if workspace_id else None
    norm_tid = normalize_uuid(thread_id) if thread_id else None
    user_tier = await get_user_tier(cleaned_uid)

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT targets FROM dalton_channel_automation_runs
                    WHERE execution_id = %s
                    """,
                    (norm_exec_id,),
                )
                existing = await cur.fetchone()
                if existing:
                    return {"targets": list(existing.get("targets") or [])}

                await cur.execute(
                    """
                    SELECT platform, platform_user_id, platform_username
                    FROM dalton_channel_identities
                    WHERE user_id = %s
                    """,
                    (cleaned_uid,),
                )
                identities = {
                    str(r["platform"]): dict(r) for r in await cur.fetchall()
                }

                await cur.execute(
                    """
                    SELECT apps FROM dalton_channel_user_settings
                    WHERE user_id = %s
                    """,
                    (cleaned_uid,),
                )
                us_row = await cur.fetchone()
                apps_cfg = dict(us_row.get("apps") or {}) if us_row else {}

                targets: list[dict[str, Any]] = []
                for raw_entry in entries or []:
                    entry_str = str(raw_entry or "").strip()
                    if not entry_str:
                        continue
                    if ":" not in entry_str:
                        plat = entry_str.lower()
                        app_obj = apps_cfg.get(plat) or {}
                        auto_out = (
                            app_obj.get("automation_output")
                            if isinstance(app_obj, dict)
                            else {}
                        ) or {}
                        resolved_addr = (
                            auto_out.get(norm_ws_id) if norm_ws_id else None
                        )
                        if not resolved_addr and isinstance(app_obj, dict):
                            resolved_addr = app_obj.get("preferred")
                        if not resolved_addr:
                            resolved_addr = f"{plat}:@me"
                        address = str(resolved_addr)
                    else:
                        plat = entry_str.split(":", 1)[0].strip().lower()
                        address = entry_str

                    id_info = identities.get(plat)
                    if not id_info:
                        targets.append(
                            {
                                "entry": entry_str,
                                "address": address if ":" in entry_str else None,
                                "name": None,
                                "ok": False,
                                "message": f"{plat.capitalize()} is not connected to your account.",
                            }
                        )
                        continue

                    if _is_dm_address(plat, address):
                        uname = id_info.get("platform_username") or cleaned_uid
                        targets.append(
                            {
                                "entry": entry_str,
                                "address": f"{plat}:@me",
                                "name": f"{uname} ({plat} DM)",
                                "ok": True,
                                "message": None,
                            }
                        )
                        continue

                    # Group chat target validation (shared with I-6 Scenario B)
                    grp_ok, grp_err, chat_name = await _verify_group_target_deliverable(
                        cur,
                        user_id=cleaned_uid,
                        user_tier=user_tier,
                        platform=plat,
                        chat_address=address,
                        apps_cfg=apps_cfg,
                        conn=owned_conn,
                    )
                    if not grp_ok:
                        targets.append(
                            {
                                "entry": entry_str,
                                "address": address,
                                "name": None,
                                "ok": False,
                                "message": grp_err,
                            }
                        )
                        continue

                    targets.append(
                        {
                            "entry": entry_str,
                            "address": address,
                            "name": chat_name,
                            "ok": True,
                            "message": None,
                        }
                    )

                await cur.execute(
                    """
                    INSERT INTO dalton_channel_automation_runs (
                        execution_id, user_id, workspace_id, thread_id,
                        automation_name, entries, status, targets,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'registered', %s, NOW(), NOW())
                    ON CONFLICT (execution_id) DO NOTHING
                    RETURNING targets
                    """,
                    (
                        norm_exec_id,
                        cleaned_uid,
                        norm_ws_id,
                        norm_tid,
                        str(automation_name or ""),
                        Jsonb(list(entries or [])),
                        Jsonb(targets),
                    ),
                )
                ins = await cur.fetchone()
                if not ins:
                    await cur.execute(
                        "SELECT targets FROM dalton_channel_automation_runs WHERE execution_id = %s",
                        (norm_exec_id,),
                    )
                    ins = await cur.fetchone()
                return {"targets": list((ins or {}).get("targets") or [])}


async def finish_automation_run(
    *,
    execution_id: str,
    status: str | None = None,
    thread_id: str | None = None,
    final_text: str | None = None,
    targets: list[dict[str, Any]] | None = None,
    mark_finished: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """I-8 (2): Claim/update or finalize `dalton_channel_automation_runs` for `/finish` (§1.2.7)."""
    norm_exec_id = normalize_uuid(execution_id)
    if not norm_exec_id:
        raise HTTPException(status_code=404, detail="Unknown execution_id")
    norm_tid = normalize_uuid(thread_id) if thread_id else None

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_automation_runs
                    WHERE execution_id = %s
                    FOR UPDATE
                    """,
                    (norm_exec_id,),
                )
                row = await cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="Unknown execution_id")

                prev_db_status = str(row["status"])
                if prev_db_status == "finished" and not mark_finished:
                    return {
                        "execution_id": norm_exec_id,
                        "status": "finished",
                        "previous_status": "finished",
                        "user_id": row["user_id"],
                        "workspace_id": (
                            str(row["workspace_id"])
                            if row.get("workspace_id")
                            else None
                        ),
                        "thread_id": (
                            str(row["thread_id"]) if row.get("thread_id") else None
                        ),
                        "automation_name": row.get("automation_name") or "",
                        "finish_status": row.get("finish_status"),
                        "final_text": row.get("final_text"),
                        "targets": list(row.get("targets") or []),
                    }

                if mark_finished and targets is not None:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_automation_runs
                        SET status = 'finished',
                            finish_status = COALESCE(%s, finish_status),
                            final_text = COALESCE(%s, final_text),
                            thread_id = COALESCE(%s, thread_id),
                            targets = %s,
                            updated_at = NOW()
                        WHERE execution_id = %s
                        RETURNING *
                        """,
                        (
                            status,
                            final_text,
                            norm_tid,
                            Jsonb(targets),
                            norm_exec_id,
                        ),
                    )
                    updated = dict(await cur.fetchone())
                    return {
                        "execution_id": norm_exec_id,
                        "status": "finished",
                        "previous_status": prev_db_status,
                        "user_id": updated["user_id"],
                        "workspace_id": (
                            str(updated["workspace_id"])
                            if updated.get("workspace_id")
                            else None
                        ),
                        "thread_id": (
                            str(updated["thread_id"])
                            if updated.get("thread_id")
                            else None
                        ),
                        "automation_name": updated.get("automation_name") or "",
                        "finish_status": updated.get("finish_status"),
                        "final_text": updated.get("final_text"),
                        "targets": list(updated.get("targets") or []),
                    }

                # Claim as 'finishing' and annotate each target with whether agent_send already reached it
                await cur.execute(
                    """
                    SELECT DISTINCT target_address
                    FROM dalton_channel_deliveries
                    WHERE execution_id = %s
                      AND kind = 'agent_send'
                      AND status IN ('sent', 'partial')
                    """,
                    (norm_exec_id,),
                )
                agent_sent_addrs = {
                    str(r["target_address"]) for r in await cur.fetchall()
                }
                enriched_targets: list[dict[str, Any]] = []
                for t_item in list(row.get("targets") or []):
                    if not isinstance(t_item, dict):
                        continue
                    t_copy = dict(t_item)
                    t_addr = str(t_copy.get("address") or "")
                    t_copy["agent_sent"] = t_addr in agent_sent_addrs
                    enriched_targets.append(t_copy)

                await cur.execute(
                    """
                    UPDATE dalton_channel_automation_runs
                    SET status = 'finishing',
                        finish_status = COALESCE(%s, finish_status),
                        final_text = COALESCE(%s, final_text),
                        thread_id = COALESCE(%s, thread_id),
                        targets = %s,
                        updated_at = NOW()
                    WHERE execution_id = %s
                    RETURNING *
                    """,
                    (
                        status,
                        final_text,
                        norm_tid,
                        Jsonb(enriched_targets),
                        norm_exec_id,
                    ),
                )
                updated = dict(await cur.fetchone())
                return {
                    "execution_id": norm_exec_id,
                    "status": "finishing",
                    "previous_status": prev_db_status,
                    "user_id": updated["user_id"],
                    "workspace_id": (
                        str(updated["workspace_id"])
                        if updated.get("workspace_id")
                        else None
                    ),
                    "thread_id": (
                        str(updated["thread_id"]) if updated.get("thread_id") else None
                    ),
                    "automation_name": updated.get("automation_name") or "",
                    "finish_status": updated.get("finish_status"),
                    "final_text": updated.get("final_text"),
                    "targets": enriched_targets,
                }


# ---------------------------------------------------------------------------
# I-9: Weixin Singleton State (pgp_sym_encrypt / pgp_sym_decrypt)
# ---------------------------------------------------------------------------


async def get_weixin_state(
    *, include_token: bool = True, conn: Any = None
) -> dict[str, Any]:
    """I-9 (GET): Read `dalton_channel_weixin_state`, decrypting `encrypted_bot_token` via `get_encryption_key()`."""
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            if include_token:
                key = get_encryption_key()
                await cur.execute(
                    """
                    SELECT singleton_id, ilink_bot_id, owner_user_id,
                           pgp_sym_decrypt(encrypted_bot_token, %s) AS bot_token,
                           base_url, get_updates_buf, status, updated_at
                    FROM dalton_channel_weixin_state
                    WHERE singleton_id = 1
                    """,
                    (key,),
                )
            else:
                await cur.execute(
                    """
                    SELECT singleton_id, ilink_bot_id, owner_user_id,
                           base_url, get_updates_buf, status, updated_at
                    FROM dalton_channel_weixin_state
                    WHERE singleton_id = 1
                    """
                )
            row = await cur.fetchone()
            if not row:
                res: dict[str, Any] = {
                    "configured": False,
                    "ilink_bot_id": None,
                    "owner_user_id": None,
                    "base_url": "https://ilinkai.weixin.qq.com",
                    "get_updates_buf": "",
                    "status": "unconfigured",
                    "updated_at": None,
                }
                if include_token:
                    res["bot_token"] = None
                return res

            updated_dt = row.get("updated_at")
            res = {
                "configured": True,
                "ilink_bot_id": row["ilink_bot_id"],
                "owner_user_id": row["owner_user_id"],
                "base_url": row["base_url"],
                "get_updates_buf": row["get_updates_buf"],
                "status": row["status"],
                "updated_at": (
                    updated_dt.isoformat()
                    if hasattr(updated_dt, "isoformat")
                    else updated_dt
                ),
            }
            if include_token:
                res["bot_token"] = row.get("bot_token")
            return res


async def upsert_weixin_state(
    *,
    ilink_bot_id: str | None = None,
    owner_user_id: str | None = None,
    bot_token: str | None = None,
    base_url: str | None = None,
    get_updates_buf: str | None = None,
    status: str | None = None,
    ilink_user_id: str | None = None,
    ilink_username: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """I-9 (PUT): Upsert `dalton_channel_weixin_state` using `pgp_sym_encrypt(bot_token, get_encryption_key())`."""
    # The bot_token path binds the owner (`_bind_identity_in_tx`), so it shares
    # the bind path's deadlock / first-settings-row races and retry boundary.
    return await _run_tx_with_retry(
        "upsert_weixin_state",
        conn,
        lambda attempt_conn: _upsert_weixin_state_once(
            ilink_bot_id=ilink_bot_id,
            owner_user_id=owner_user_id,
            bot_token=bot_token,
            base_url=base_url,
            get_updates_buf=get_updates_buf,
            status=status,
            ilink_user_id=ilink_user_id,
            ilink_username=ilink_username,
            conn=attempt_conn,
        ),
    )


async def _upsert_weixin_state_once(
    *,
    ilink_bot_id: str | None = None,
    owner_user_id: str | None = None,
    bot_token: str | None = None,
    base_url: str | None = None,
    get_updates_buf: str | None = None,
    status: str | None = None,
    ilink_user_id: str | None = None,
    ilink_username: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                if bot_token is not None:
                    if not ilink_bot_id or not owner_user_id:
                        raise HTTPException(
                            status_code=400,
                            detail="ilink_bot_id and owner_user_id are required when saving bot_token",
                        )
                    key = get_encryption_key()
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_weixin_state (
                            singleton_id, ilink_bot_id, owner_user_id,
                            encrypted_bot_token, base_url, get_updates_buf,
                            status, updated_at
                        )
                        VALUES (
                            1, %s, %s, pgp_sym_encrypt(%s, %s),
                            COALESCE(%s, 'https://ilinkai.weixin.qq.com'),
                            COALESCE(%s, ''),
                            COALESCE(%s, 'active'),
                            NOW()
                        )
                        ON CONFLICT (singleton_id) DO UPDATE SET
                            ilink_bot_id = EXCLUDED.ilink_bot_id,
                            owner_user_id = EXCLUDED.owner_user_id,
                            encrypted_bot_token = EXCLUDED.encrypted_bot_token,
                            base_url = EXCLUDED.base_url,
                            get_updates_buf = COALESCE(%s, dalton_channel_weixin_state.get_updates_buf),
                            status = COALESCE(%s, 'active'),
                            updated_at = NOW()
                        RETURNING ilink_bot_id, owner_user_id, base_url, get_updates_buf, status, updated_at
                        """,
                        (
                            ilink_bot_id.strip(),
                            owner_user_id.strip(),
                            bot_token,
                            key,
                            base_url,
                            get_updates_buf,
                            status,
                            get_updates_buf,
                            status,
                        ),
                    )
                    saved = dict(await cur.fetchone())
                    if ilink_user_id and ilink_user_id.strip():
                        await _bind_identity_in_tx(
                            cur,
                            user_id=owner_user_id.strip(),
                            platform="weixin",
                            platform_user_id=ilink_user_id.strip(),
                            platform_username=ilink_username,
                        )
                else:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_state
                        SET get_updates_buf = COALESCE(%s, get_updates_buf),
                            status = COALESCE(%s, status),
                            base_url = COALESCE(%s, base_url),
                            updated_at = NOW()
                        WHERE singleton_id = 1
                        RETURNING ilink_bot_id, owner_user_id, base_url, get_updates_buf, status, updated_at
                        """,
                        (get_updates_buf, status, base_url),
                    )
                    row = await cur.fetchone()
                    if not row:
                        raise HTTPException(
                            status_code=404,
                            detail="Weixin bot state is not initialized",
                        )
                    saved = dict(row)

                updated_dt = saved.get("updated_at")
                return {
                    "ok": True,
                    "ilink_bot_id": saved["ilink_bot_id"],
                    "owner_user_id": saved["owner_user_id"],
                    "base_url": saved["base_url"],
                    "get_updates_buf": saved["get_updates_buf"],
                    "status": saved["status"],
                    "updated_at": (
                        updated_dt.isoformat()
                        if hasattr(updated_dt, "isoformat")
                        else updated_dt
                    ),
                }
