"""Database persistence and transaction logic for CH-1 Channel Gateway (`I-1` ~ `I-8` & multi-account Weixin `d049`).

All SQL for the channel gateway tables and delivery state columns lives here
(migrations `d030`, `d031`, `d032`, `d036`, and `d049`; multi-account Weixin helpers
live in `src/dalton/database/weixin_multi_account.py`).

Global lock order across transactions (strictly ascending, zero exceptions, §2.1 (8)):
- Level 0a: `dalton_user_roles` (sorted `user_id` `FOR UPDATE`) -> `dalton_credential_holder` (`FOR SHARE`)
- Level 0b: `dalton_channel_gateway_lease` (`FOR UPDATE` for acquire/heartbeat/release; `FOR SHARE` for all gateway writes + `recover`) -> per-message / per-delivery advisory lock (`dalton_inbound:<platform>:<message_id>`, `dalton_delivery:<delivery_key>`)
- Level 1: Personal settings advisory + row locks (`dalton_user_settings:<user_id>`, `dalton_channel_user_settings` `FOR UPDATE`; sorted by `user_id` when locking multiple users)
- Level 2: Group chat advisory locks (`_acquire_group_chat_lock`; sorted by `platform`, `chat_address`)
- Level 3: Weixin accounts & occupation locks:
  - 3a: `dalton_channel_weixin_accounts` (sorted `account_id`: `FOR UPDATE` on mutations, `FOR NO KEY UPDATE` on active fast paths)
  - 3b: `dalton_weixin_bot:<bot_id>` -> sorted `dalton_weixin_ilink_user:<wx_uid>` advisory locks
- Level 4: `dalton_channel_weixin_login_sessions` (`FOR UPDATE`, sorted `session_key`)
- Level 5: `dalton_weixin_context:<bot>:<generation>:<sender>` advisory lock -> `dalton_channel_weixin_account_tokens` (`FOR UPDATE`)
- Level 6: `dalton_channel_chat_bindings` -> `dalton_channel_identities` -> `dalton_channel_session_generations` (`FOR UPDATE`)
- Level 7: `dalton_channel_inbound_events` -> `dalton_channel_thread_watches` -> `dalton_channel_background_runs` -> `dalton_channel_deliveries` (`FOR UPDATE`) -> `INSERT INTO dalton_channel_weixin_audit_logs`
- Level 8: `dalton_channel_weixin_config` (`singleton_id = 1 FOR UPDATE`, always last)
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import random
import re
import secrets
from typing import Any, NoReturn
from uuid import UUID, uuid5

from fastapi import HTTPException
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton import sharing as dalton_sharing
from src.dalton.access_tiers import (
    check_team_member_status_live,
    evaluate_weixin_member_access,
    get_user_tier,
)
from src.dalton.capabilities import (
    CapabilityId,
    has_capability,
    require_channel_platform_capability,
)
from src.dalton.company_identity import reject_company_system_auth
from src.dalton.database import weixin_multi_account as wx_db
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


class _WeixinSettingsLockSetChanged(Exception):
    """The users that must be locked changed after the settings set was taken.

    Callers lock the discovered set, then read it again before the singleton
    lock and again after the Weixin sender lock. A larger set raises this
    error instead of taking a new settings lock. `_run_tx_with_retry` runs the
    transaction again only when the caller did not pass a connection.
    """


_weixin_settings_discovery_hook: Callable[[], Awaitable[None]] | None = None
_weixin_before_sender_lock_hook: Callable[[], Awaitable[None]] | None = None


def _sorted_user_ids(*groups: Any) -> list[str]:
    found: set[str] = set()
    for group in groups:
        if group is None:
            continue
        items = [group] if isinstance(group, str) else list(group)
        for item in items:
            cleaned = str(item or "").strip()
            if cleaned:
                found.add(cleaned)
    return sorted(found)


async def _weixin_identity_user_ids_in_tx(
    cur: Any, platform_user_id: str | None
) -> set[str]:
    cleaned = str(platform_user_id or "").strip()
    if not cleaned:
        return set()
    await cur.execute(
        """
        SELECT user_id FROM dalton_channel_identities
        WHERE platform = %s AND platform_user_id = %s
        """,
        ("weixin", cleaned),
    )
    return {
        str(row["user_id"])
        for row in await cur.fetchall()
        if row.get("user_id")
    }


async def _weixin_saved_state_owner_ids_in_tx(cur: Any) -> set[str]:
    await cur.execute(
        """
        SELECT owner_user_id, login_owner_user_id
        FROM dalton_channel_weixin_state
        WHERE singleton_id = 1
        """
    )
    row = await cur.fetchone()
    if not row:
        return set()
    return set(_sorted_user_ids(row.get("owner_user_id"), row.get("login_owner_user_id")))


async def _lock_user_settings_rows_in_tx(cur: Any, user_ids: Any) -> frozenset[str]:
    """Lock personal settings advisory and row locks in sorted user-id order."""
    locked = _sorted_user_ids(user_ids)
    for lock_uid in locked:
        reject_company_system_auth(lock_uid)
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
    return frozenset(locked)


async def _preview_weixin_bind_settings_users(
    cur: Any,
    *,
    owner_user_ids: Any = None,
    platform_user_id: str | None = None,
    include_saved_state_owners: bool = False,
) -> set[str]:
    """Read the users a bind will need, then let a test change that set.

    The hook runs after the unlocked read and before any settings lock. The
    returned set is the pre-hook set. The caller locks that set, takes the
    singleton, and reads again.
    """
    found = set(_sorted_user_ids(owner_user_ids))
    if include_saved_state_owners:
        found.update(await _weixin_saved_state_owner_ids_in_tx(cur))
    found.update(await _weixin_identity_user_ids_in_tx(cur, platform_user_id))
    hook = _weixin_settings_discovery_hook
    if hook is not None:
        await hook()
    return found


async def _require_weixin_bind_settings_subset(
    cur: Any,
    *,
    owner_user_id: str | None,
    platform_user_id: str | None,
    already_locked: frozenset[str],
) -> None:
    required = set(await _weixin_identity_user_ids_in_tx(cur, platform_user_id))
    owner = str(owner_user_id or "").strip()
    if owner:
        required.add(owner)
    if not required.issubset(already_locked):
        raise _WeixinSettingsLockSetChanged()


def _is_retryable_tx_error(exc: BaseException) -> bool:
    """Return True for deadlock, settings-lock-set change, or settings primary-key conflict."""
    if isinstance(exc, (_WeixinSettingsLockSetChanged, psycopg.errors.DeadlockDetected)):
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



def _strip_sensitive_from_raw_payload(raw_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Remove plaintext tokens, QR tickets, verify codes, and CDN/AES keys before persisting raw_payload (§4.3)."""
    if not isinstance(raw_payload, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for k, v in raw_payload.items():
        if k in (
            "weixin_context_token",
            "context_token",
            "bot_token",
            "encrypted_weixin_context_token",
            "encrypted_bot_token",
            "weixin_media_meta",
            "weixin_attachments",
            "encrypted_weixin_media_meta",
            "qrcode",
            "login_qrcode",
            "encrypted_qrcode",
            "verify_code",
            "aes_key",
            "aeskey",
            "encrypt_query_param",
            "cdn_key",
        ):
            continue
        if k == "raw" and isinstance(v, dict):
            cleaned[k] = _strip_sensitive_from_raw_payload(v)
        else:
            cleaned[k] = v
    return cleaned


def _normalize_weixin_status(status: str | None, *, configured: bool = True) -> str:
    """Normalize Weixin bot lifecycle status across DB and API layers."""
    if not configured:
        return "unconfigured"
    s = str(status or "active").strip().lower()
    if s in ("expired", "session_expired"):
        return "expired"
    if s in ("active", "connected"):
        return "active"
    if s == "unconfigured":
        return "unconfigured"
    return s


_CONTEXT_ORDER_SOURCES = ("protocol", "platform_time", "receive")


def _context_order_source(source: str | None) -> str:
    cleaned = str(source or "").strip()
    return cleaned if cleaned in _CONTEXT_ORDER_SOURCES else ""


def _context_order_supersedes(
    incoming_source: str,
    incoming_value: int,
    existing_source: str,
    existing_value: int,
    *,
    protocol_high: int | None = None,
    platform_time_high: int | None = None,
) -> bool:
    """Whether a newly registered message replaces the saved context token.

    Each field keeps its own high-water. A later message may install a
    different field, including a message with neither protocol seq nor
    platform time, but an older value of a field already seen does not
    replace the current token. The two fields are not compared as numbers.
    Replay and bind must not call this.
    """
    existing = _context_order_source(existing_source)
    incoming = _context_order_source(incoming_source)
    if incoming == "receive":
        return True
    if incoming == "protocol":
        high = int(existing_value or 0) if existing == "protocol" else 0
        if protocol_high is not None:
            high = max(high, int(protocol_high))
        return int(incoming_value or 0) > high
    if incoming == "platform_time":
        high = int(existing_value or 0) if existing == "platform_time" else 0
        if platform_time_high is not None:
            high = max(high, int(platform_time_high))
        return int(incoming_value or 0) > high
    return False


def _saved_context_order_supersedes(
    incoming_source: str,
    incoming_value: int,
    existing_source: str,
    existing_value: int,
) -> bool:
    """Whether a stored order is newer than the current token.

    Protocol seq, platform time, and receive revisions are compared only with
    the same field. A larger number from another field does not replace the
    current token. An empty saved source does not outrank a real one.
    """
    existing = _context_order_source(existing_source)
    incoming = _context_order_source(incoming_source)
    if not existing:
        return bool(incoming)
    if incoming != existing:
        return False
    return incoming_value > existing_value


def _positive_context_seq(raw_val: Any) -> int | None:
    if isinstance(raw_val, bool) or raw_val is None:
        return None
    if isinstance(raw_val, (int, float)):
        parsed = int(raw_val)
        return parsed if parsed > 0 else None
    if isinstance(raw_val, str) and raw_val.strip().isdigit():
        parsed = int(raw_val.strip())
        return parsed if parsed > 0 else None
    return None


def _extract_weixin_context_order(
    raw_payload: dict[str, Any] | None,
    explicit_seq: int | None = None,
) -> tuple[str, int] | None:
    """Return `(source, value)` for this message, or None when both fields are absent.

    The explicit argument and payload `seq` / `weixin_context_seq` are protocol
    order. `create_time_ms` is platform time and is used only when no protocol
    seq is present. A missing pair is receive order, assigned in the token
    transaction. The numeric size does not choose the source.
    """
    explicit = _positive_context_seq(explicit_seq)
    if explicit is not None:
        return ("protocol", explicit)
    if not isinstance(raw_payload, dict):
        return None
    protocol = _positive_context_seq(raw_payload.get("seq"))
    if protocol is None:
        protocol = _positive_context_seq(raw_payload.get("weixin_context_seq"))
    if protocol is not None:
        return ("protocol", protocol)
    platform_time = _positive_context_seq(raw_payload.get("create_time_ms"))
    if platform_time is not None:
        return ("platform_time", platform_time)
    return None


def _payload_marks_invalid_inbound(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("invalid_inbound") or payload.get("invalid_inbound_event"):
        return True
    reason = payload.get("rejected_reason") or payload.get("deny_reason")
    return reason == "invalid_inbound_event"


def _weixin_context_lock_key(
    bot_id: str, bot_generation: int, platform_user_id: str
) -> str:
    """Advisory-lock name for one bot generation and sender."""
    return (
        "dalton_weixin_context:"
        f"{str(bot_id).strip()}:{int(bot_generation)}:{str(platform_user_id).strip()}"
    )


async def _lock_weixin_context_sender_in_tx(
    cur: Any,
    *,
    bot_id: str,
    bot_generation: int,
    platform_user_id: str,
) -> None:
    """Serialize first-row creation and later updates for one sender.

    Reentrant in the same transaction. Callers that lock several senders
    pass them in `(bot, generation, sender)` order.
    """
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (_weixin_context_lock_key(bot_id, bot_generation, platform_user_id),),
    )


async def _lock_weixin_context_token_rows_in_tx(
    cur: Any,
    *,
    bot_ids: list[str] | None = None,
    keep_bot_id: str | None = None,
    keep_generation: int | None = None,
    platform_user_id: str | None = None,
) -> None:
    """Lock token rows before a generation reset or unbind deletes them."""
    if platform_user_id:
        await cur.execute(
            """
            SELECT ilink_bot_id, bot_generation, platform_user_id
            FROM dalton_channel_weixin_context_tokens
            WHERE platform_user_id = %s
            ORDER BY ilink_bot_id, bot_generation, platform_user_id
            """,
            (str(platform_user_id).strip(),),
        )
    else:
        await cur.execute(
            """
            SELECT ilink_bot_id, bot_generation, platform_user_id
            FROM dalton_channel_weixin_context_tokens
            WHERE ilink_bot_id = ANY(%s)
              AND NOT (ilink_bot_id = %s AND bot_generation = %s)
            ORDER BY ilink_bot_id, bot_generation, platform_user_id
            """,
            (list(bot_ids or []), keep_bot_id or "", int(keep_generation or 0)),
        )
    for row in await cur.fetchall():
        await _lock_weixin_context_sender_in_tx(
            cur,
            bot_id=str(row["ilink_bot_id"]),
            bot_generation=int(row["bot_generation"]),
            platform_user_id=str(row["platform_user_id"]),
        )


async def _get_active_weixin_bot_in_tx(
    cur: Any, *, lock_generation: bool = False
) -> tuple[str | None, int, str]:
    """Return `(ilink_bot_id, bot_generation, normalized_status)` for the singleton Weixin bot.

    `lock_generation` takes `FOR SHARE` so a confirm or account switch that
    holds `FOR UPDATE` cannot publish a new generation until this transaction
    finishes writing the generation it read.
    """
    share = " FOR SHARE" if lock_generation else ""
    await cur.execute(
        f"""
        SELECT ilink_bot_id, bot_generation, status
        FROM dalton_channel_weixin_state
        WHERE singleton_id = 1{share}
        """
    )
    row = await cur.fetchone()
    if not row:
        return None, 1, "unconfigured"
    bot_id = str(row["ilink_bot_id"]).strip() if row.get("ilink_bot_id") else None
    bot_gen = int(row.get("bot_generation") or 1)
    status = _normalize_weixin_status(row.get("status"), configured=bool(bot_id))
    return bot_id, bot_gen, status


async def _upsert_weixin_context_token_in_tx(
    cur: Any,
    *,
    enc_key: str,
    bot_id: str,
    bot_generation: int,
    platform_user_id: str,
    context_token: str,
    incoming_seq: int | None = None,
    incoming_source: str | None = None,
) -> tuple[bool, int]:
    """Store the context token for a newly registered message.

    Protocol seq and platform time each keep a high-water on this bot
    generation and sender, including while the other field is current. A
    message with neither field still takes the next receive revision. The
    sender advisory lock covers the first row, and the conflict update never
    stores a smaller high. Replay of an existing message never reaches this
    function.
    """
    cleaned_bot_id = str(bot_id or "").strip()
    cleaned_puid = str(platform_user_id or "").strip()
    cleaned_token = str(context_token or "").strip()
    if not cleaned_bot_id or not cleaned_puid or not cleaned_token:
        return False, 0

    await _lock_weixin_context_sender_in_tx(
        cur,
        bot_id=cleaned_bot_id,
        bot_generation=int(bot_generation),
        platform_user_id=cleaned_puid,
    )
    await cur.execute(
        """
        SELECT seq, seq_source, protocol_seq_high, platform_time_high
        FROM dalton_channel_weixin_context_tokens
        WHERE ilink_bot_id = %s AND bot_generation = %s AND platform_user_id = %s
        FOR UPDATE
        """,
        (cleaned_bot_id, int(bot_generation), cleaned_puid),
    )
    existing = await cur.fetchone()
    source = _context_order_source(incoming_source)
    value = _positive_context_seq(incoming_seq) if source in ("protocol", "platform_time") else None
    if source in ("protocol", "platform_time") and value is None:
        source = ""

    protocol_high = 0
    platform_high = 0
    if existing is not None:
        existing_seq = int(existing.get("seq") or 0)
        existing_source = _context_order_source(existing.get("seq_source"))
        protocol_high = int(existing.get("protocol_seq_high") or 0)
        platform_high = int(existing.get("platform_time_high") or 0)
        if existing_source == "protocol":
            protocol_high = max(protocol_high, existing_seq)
        elif existing_source == "platform_time":
            platform_high = max(platform_high, existing_seq)
        if not source:
            source = "receive"
            value = existing_seq + 1 if existing_source == "receive" else 1
        if not _context_order_supersedes(
            source,
            int(value or 0),
            existing_source,
            existing_seq,
            protocol_high=protocol_high,
            platform_time_high=platform_high,
        ):
            return False, existing_seq
        effective_seq = int(value or 0)
    else:
        if not source:
            source = "receive"
            value = 1
        effective_seq = int(value or 1)

    if source == "protocol":
        protocol_high = effective_seq
    elif source == "platform_time":
        platform_high = effective_seq

    await cur.execute(
        """
        INSERT INTO dalton_channel_weixin_context_tokens (
            ilink_bot_id, bot_generation, platform_user_id,
            encrypted_context_token, seq, seq_source,
            protocol_seq_high, platform_time_high, updated_at
        )
        VALUES (%s, %s, %s, pgp_sym_encrypt(%s, %s), %s, %s, %s, %s, NOW())
        ON CONFLICT (ilink_bot_id, bot_generation, platform_user_id) DO UPDATE SET
            encrypted_context_token = EXCLUDED.encrypted_context_token,
            seq = CASE
                WHEN EXCLUDED.seq_source = 'receive' THEN
                    CASE
                        WHEN dalton_channel_weixin_context_tokens.seq_source = 'receive'
                        THEN dalton_channel_weixin_context_tokens.seq + 1
                        ELSE 1
                    END
                ELSE EXCLUDED.seq
            END,
            seq_source = EXCLUDED.seq_source,
            protocol_seq_high = GREATEST(
                dalton_channel_weixin_context_tokens.protocol_seq_high,
                CASE
                    WHEN dalton_channel_weixin_context_tokens.seq_source = 'protocol'
                    THEN dalton_channel_weixin_context_tokens.seq
                    ELSE 0
                END,
                EXCLUDED.protocol_seq_high,
                CASE
                    WHEN EXCLUDED.seq_source = 'protocol' THEN EXCLUDED.seq
                    ELSE 0
                END
            ),
            platform_time_high = GREATEST(
                dalton_channel_weixin_context_tokens.platform_time_high,
                CASE
                    WHEN dalton_channel_weixin_context_tokens.seq_source = 'platform_time'
                    THEN dalton_channel_weixin_context_tokens.seq
                    ELSE 0
                END,
                EXCLUDED.platform_time_high,
                CASE
                    WHEN EXCLUDED.seq_source = 'platform_time' THEN EXCLUDED.seq
                    ELSE 0
                END
            ),
            updated_at = NOW()
        WHERE EXCLUDED.seq_source = 'receive'
           OR (
                EXCLUDED.seq_source = 'protocol'
                AND EXCLUDED.seq > GREATEST(
                    dalton_channel_weixin_context_tokens.protocol_seq_high,
                    CASE
                        WHEN dalton_channel_weixin_context_tokens.seq_source = 'protocol'
                        THEN dalton_channel_weixin_context_tokens.seq
                        ELSE 0
                    END
                )
              )
           OR (
                EXCLUDED.seq_source = 'platform_time'
                AND EXCLUDED.seq > GREATEST(
                    dalton_channel_weixin_context_tokens.platform_time_high,
                    CASE
                        WHEN dalton_channel_weixin_context_tokens.seq_source = 'platform_time'
                        THEN dalton_channel_weixin_context_tokens.seq
                        ELSE 0
                    END
                )
              )
        RETURNING seq, seq_source
        """,
        (
            cleaned_bot_id,
            int(bot_generation),
            cleaned_puid,
            cleaned_token,
            enc_key,
            effective_seq,
            source,
            protocol_high,
            platform_high,
        ),
    )
    wrote = getattr(cur, "rowcount", None)
    if isinstance(wrote, int) and wrote == 0:
        await cur.execute(
            """
            SELECT seq, seq_source, protocol_seq_high, platform_time_high
            FROM dalton_channel_weixin_context_tokens
            WHERE ilink_bot_id = %s AND bot_generation = %s AND platform_user_id = %s
            """,
            (cleaned_bot_id, int(bot_generation), cleaned_puid),
        )
        current = await cur.fetchone()
        if existing is not None:
            fallback_seq = int(existing.get("seq") or 0)
        else:
            fallback_seq = 0
        return False, int(current["seq"]) if current else fallback_seq

    stored = await cur.fetchone()
    if isinstance(stored, dict) and stored.get("seq") is not None:
        effective_seq = int(stored["seq"])
        if stored.get("seq_source"):
            source = str(stored["seq_source"])

    await cur.execute(
        """
        UPDATE dalton_channel_identities
        SET weixin_context_token = NULL,
            encrypted_weixin_context_token = pgp_sym_encrypt(%s, %s),
            weixin_context_bot_id = %s,
            weixin_context_bot_generation = %s,
            weixin_context_seq = %s,
            weixin_context_seq_source = %s,
            weixin_context_updated_at = NOW(),
            updated_at = NOW()
        WHERE platform = 'weixin'
          AND platform_user_id = %s
          AND (
              weixin_context_bot_id IS DISTINCT FROM %s
              OR weixin_context_bot_generation IS DISTINCT FROM %s
              OR weixin_context_seq_source IS DISTINCT FROM %s
              OR (
                  weixin_context_seq_source = %s
                  AND COALESCE(weixin_context_seq, 0) < %s
              )
          )
        """,
        (
            cleaned_token,
            enc_key,
            cleaned_bot_id,
            int(bot_generation),
            effective_seq,
            source,
            cleaned_puid,
            cleaned_bot_id,
            int(bot_generation),
            source,
            source,
            effective_seq,
        ),
    )
    return True, effective_seq


def _context_candidate(
    source: str | None,
    value: int | None,
    token: Any,
) -> tuple[str, int, str] | None:
    plain = _plain_weixin_token(token)
    if not plain:
        return None
    return (_context_order_source(source), int(value or 0), plain)


def _prefer_saved_context_candidate(
    current: tuple[str, int, str] | None,
    incoming: tuple[str, int, str] | None,
) -> tuple[str, int, str] | None:
    """Keep the current token unless the other copy is newer in the same field."""
    if incoming is None:
        return current
    if current is None:
        return incoming
    if _saved_context_order_supersedes(incoming[0], incoming[1], current[0], current[1]):
        return incoming
    return current


async def _resolve_latest_weixin_context_token_in_tx(
    cur: Any,
    *,
    enc_key: str,
    bot_id: str | None,
    bot_generation: int,
    platform_user_id: str | None,
    inbound_message_id: str | None = None,
) -> str | None:
    """Return the current context token for this bot generation and sender.

    The context-token row is the current token. Identity and the named inbound
    message are copies. A copy replaces it only when the same field is strictly
    newer. Protocol seq, platform time, and receive revisions are not compared
    with each other.
    """
    cleaned_bot_id = str(bot_id or "").strip()
    cleaned_puid = str(platform_user_id or "").strip()
    if not cleaned_bot_id or not cleaned_puid:
        return None

    chosen: tuple[str, int, str] | None = None

    await cur.execute(
        """
        SELECT pgp_sym_decrypt(encrypted_context_token, %s) AS context_token,
               COALESCE(seq, 0) AS seq,
               seq_source
        FROM dalton_channel_weixin_context_tokens
        WHERE ilink_bot_id = %s AND bot_generation = %s AND platform_user_id = %s
        """,
        (enc_key, cleaned_bot_id, int(bot_generation), cleaned_puid),
    )
    ctx_row = await cur.fetchone()
    if ctx_row:
        chosen = _prefer_saved_context_candidate(
            chosen,
            _context_candidate(
                ctx_row.get("seq_source"),
                int(ctx_row.get("seq") or 0),
                ctx_row.get("context_token"),
            ),
        )

    await cur.execute(
        """
        SELECT pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS context_token,
               COALESCE(weixin_context_seq, 0) AS seq,
               weixin_context_seq_source AS seq_source
        FROM dalton_channel_identities
        WHERE platform = 'weixin'
          AND platform_user_id = %s
          AND weixin_context_bot_id = %s
          AND weixin_context_bot_generation = %s
          AND encrypted_weixin_context_token IS NOT NULL
        """,
        (enc_key, cleaned_puid, cleaned_bot_id, int(bot_generation)),
    )
    id_row = await cur.fetchone()
    if id_row:
        chosen = _prefer_saved_context_candidate(
            chosen,
            _context_candidate(
                id_row.get("seq_source"),
                int(id_row.get("seq") or 0),
                id_row.get("context_token"),
            ),
        )

    if inbound_message_id:
        await cur.execute(
            """
            SELECT pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS context_token,
                   COALESCE(weixin_context_seq, 0) AS seq,
                   raw_payload
            FROM dalton_channel_inbound_events
            WHERE platform = 'weixin'
              AND message_id = %s
              AND weixin_context_bot_id = %s
              AND weixin_context_bot_generation = %s
              AND encrypted_weixin_context_token IS NOT NULL
            """,
            (enc_key, str(inbound_message_id).strip(), cleaned_bot_id, int(bot_generation)),
        )
        in_row = await cur.fetchone()
        if in_row and in_row.get("context_token"):
            raw_payload = in_row.get("raw_payload")
            if isinstance(raw_payload, str):
                try:
                    parsed = json.loads(raw_payload)
                except json.JSONDecodeError:
                    parsed = None
                raw_payload = parsed if isinstance(parsed, dict) else None
            order = _extract_weixin_context_order(
                raw_payload if isinstance(raw_payload, dict) else None
            )
            if order is not None:
                chosen = _prefer_saved_context_candidate(
                    chosen,
                    _context_candidate(order[0], order[1], in_row.get("context_token")),
                )
            elif chosen is None:
                chosen = _context_candidate("", int(in_row.get("seq") or 0), in_row.get("context_token"))

    return chosen[2] if chosen is not None else None


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
    raw_p = _sanitize_native_payload_dict(
        _strip_sensitive_from_raw_payload(row.get("raw_payload") or {})
    )
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
        "raw_payload": raw_p,
        "pending_interrupt": pending_interrupt,
        "consumed_bind_result": row.get("consumed_bind_result"),
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
    weixin_account_id: str | None = None,
    weixin_context_token: str | None = None,
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    weixin_context_seq: int | None = None,
    weixin_media_meta: Any = None,
    unsupported_item_types: list[Any] | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """I-1: Idempotently register inbound event and resolve identity, tier, workspace, and generation."""
    if str(platform or "").strip().lower() == "weixin":
        return await _register_and_resolve_weixin_inbound(
            platform_user_id=platform_user_id,
            chat_type=chat_type,
            chat_address=chat_address,
            thread_scope_key=thread_scope_key,
            message_id=message_id,
            request_key=request_key,
            raw_payload=raw_payload,
            weixin_account_id=weixin_account_id,
            weixin_context_token=weixin_context_token,
            weixin_bot_id=weixin_bot_id,
            weixin_bot_generation=weixin_bot_generation,
            weixin_context_seq=weixin_context_seq,
            weixin_media_meta=weixin_media_meta,
            unsupported_item_types=unsupported_item_types,
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
            conn=conn,
        )
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
            weixin_bot_id=weixin_bot_id,
            weixin_bot_generation=weixin_bot_generation,
            weixin_context_seq=weixin_context_seq,
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
            conn=attempt_conn,
        ),
    )


async def _register_and_resolve_weixin_inbound(
    *,
    platform_user_id: str,
    chat_type: str,
    chat_address: str,
    thread_scope_key: str,
    message_id: str,
    request_key: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    weixin_account_id: str | None = None,
    weixin_context_token: str | None = None,
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    weixin_context_seq: int | None = None,
    weixin_media_meta: Any = None,
    unsupported_item_types: list[Any] | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """Multi-account Weixin I-1 path (§3.2 #3, D11, D16, §4.4, Codex ④)."""
    cleaned_msg_id = str(message_id or "").strip()
    cleaned_chat_type = str(chat_type or "dm").strip().lower()
    cleaned_puid = str(platform_user_id or "").strip()
    cleaned_scope = str(thread_scope_key or "").strip() or (
        f"dm:{cleaned_puid}" if cleaned_puid else "dm:"
    )
    cleaned_address = str(chat_address or "weixin:@me").strip()
    req_uuid = normalize_uuid(request_key) or str(
        uuid5(DALTON_CHANNEL_NAMESPACE, f"weixin:{cleaned_msg_id}")
    )
    raw_in = dict(raw_payload or {})
    eff_ctx_token = (
        str(weixin_context_token).strip()
        if weixin_context_token
        else str(raw_in.get("weixin_context_token") or "").strip() or None
    )
    eff_media_meta = (
        weixin_media_meta
        if weixin_media_meta is not None
        else (raw_in.get("weixin_media_meta") or raw_in.get("weixin_attachments"))
    )
    incoming_order = _extract_weixin_context_order(raw_in, weixin_context_seq)
    incoming_seq = incoming_order[1] if incoming_order is not None else None
    incoming_source = incoming_order[0] if incoming_order is not None else None
    payload_dict = _sanitize_native_payload_dict(_strip_sensitive_from_raw_payload(raw_in))
    if cleaned_puid:
        payload_dict["platform_user_id"] = cleaned_puid

    is_weixin_group = (
        cleaned_chat_type == "group"
        or bool(str(payload_dict.get("group_id") or "").strip())
        or cleaned_address.startswith("weixin:group:")
        or cleaned_scope.startswith("chat:")
    )
    is_invalid_inbound = (
        _payload_marks_invalid_inbound(payload_dict)
        or not cleaned_puid
        or cleaned_puid == "__invalid__"
    )
    if is_invalid_inbound:
        payload_dict["invalid_inbound_event"] = True
        payload_dict["deny_reason"] = "invalid_inbound_event"
        payload_dict.setdefault("rejected_reason", "invalid_inbound_event")

    eff_aid_param = normalize_uuid(weixin_account_id) or normalize_uuid(
        raw_in.get("weixin_account_id")
    )
    eff_bot_id_param = str(weixin_bot_id or raw_in.get("ilink_bot_id") or "").strip() or None
    eff_bot_gen_param = (
        int(weixin_bot_generation)
        if weixin_bot_generation is not None
        else (
            int(raw_in["bot_generation"])
            if raw_in.get("bot_generation") is not None
            else None
        )
    )

    # Loop allows D11 rollback+two-phase-relock if fast path discovers pending_verification
    for _verify_pass in range(2):
        pending_account_to_verify: str | None = None
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        platform="weixin",
                    )
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"dalton_inbound:weixin:{cleaned_msg_id}",),
                    )
                    # Check existing inbound (idempotent replay with full account/gen/sender/roster/switch verification)
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_inbound_events
                        WHERE platform = 'weixin' AND message_id = %s
                        """,
                        (cleaned_msg_id,),
                    )
                    existing = await cur.fetchone()
                    if existing is not None:
                        existing = dict(existing)
                        # R5 (Decision #3): Validate group, sender, required Weixin identity
                        # fields, stored match, live roster, and runtime switches BEFORE
                        # returning ANY replay or stored ignored row — zero member leakage!
                        if is_weixin_group:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "weixin_group_unsupported",
                                "existing_status": existing["status"],
                            }
                        if is_invalid_inbound:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "invalid_inbound_event",
                                "existing_status": existing["status"],
                            }
                        if (
                            not eff_aid_param
                            or not eff_bot_id_param
                            or eff_bot_gen_param is None
                            or eff_bot_gen_param < 1
                        ):
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "weixin_inbound_fields_required",
                                "existing_status": existing["status"],
                            }
                        ex_uid = existing.get("dalton_user_id")
                        ex_payload = existing.get("raw_payload") or {}
                        ex_puid = (
                            str(ex_payload.get("platform_user_id") or "").strip()
                            if isinstance(ex_payload, dict)
                            else ""
                        )
                        ex_acc_id = (
                            normalize_uuid(existing.get("weixin_account_id"))
                            if existing.get("weixin_account_id")
                            else None
                        )
                        ex_bot_id = str(existing.get("weixin_context_bot_id") or "").strip() or None
                        ex_bot_gen = (
                            int(existing["weixin_context_bot_generation"])
                            if existing.get("weixin_context_bot_generation") is not None
                            else None
                        )
                        # Cross-member / cross-account message_id collision guard (zero leakage)
                        if (ex_acc_id and eff_aid_param != ex_acc_id) or (
                            ex_puid and cleaned_puid != ex_puid
                        ):
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "weixin_message_id_account_mismatch",
                                "existing_status": existing["status"],
                            }
                        if (ex_bot_id and eff_bot_id_param != ex_bot_id) or (
                            ex_bot_gen is not None and eff_bot_gen_param != ex_bot_gen
                        ):
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "weixin_bot_generation_mismatch",
                                "existing_status": existing["status"],
                            }

                        # Re-verify account strictly by requested weixin_account_id
                        await cur.execute(
                            "SELECT * FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                            (str(eff_aid_param),),
                        )
                        acc_rep = await cur.fetchone()
                        if acc_rep is None:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                                "existing_status": existing["status"],
                            }
                        acc_rep = dict(acc_rep)
                        acc_rep_bot = str(acc_rep["ilink_bot_id"])
                        acc_rep_gen = int(acc_rep["bot_generation"])
                        acc_rep_wx = str(acc_rep.get("ilink_user_id") or "").strip()
                        ex_uid_str = str(acc_rep["user_id"])
                        if ex_uid and str(ex_uid) != ex_uid_str:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "weixin_message_id_account_mismatch",
                                "existing_status": existing["status"],
                            }
                        if eff_bot_id_param != acc_rep_bot or eff_bot_gen_param != acc_rep_gen:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "weixin_bot_generation_mismatch",
                                "existing_status": existing["status"],
                            }
                        if str(acc_rep["status"]) != "active":
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": f"weixin_account_{acc_rep['status']}",
                                "existing_status": existing["status"],
                            }
                        if acc_rep_wx and cleaned_puid != acc_rep_wx:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "weixin_sender_mismatch",
                                "existing_status": existing["status"],
                            }

                        reject_company_system_auth(ex_uid_str)
                        rep_roster = await check_team_member_status_live(
                            ex_uid_str, conn=owned_conn
                        )
                        if rep_roster == "lookup_error":
                            raise HTTPException(
                                status_code=503,
                                detail={
                                    "code": "roster_lookup_error",
                                    "message": "成员名册查询异常",
                                },
                            )
                        if rep_roster != "active":
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "member_not_in_roster",
                                "existing_status": existing["status"],
                            }

                        ex_tier = await get_user_tier(ex_uid_str, conn=owned_conn)
                        cfg_rep = await wx_db.ensure_and_lock_weixin_config_in_tx(
                            cur, for_update=False
                        )
                        rep_ok, rep_deny = evaluate_weixin_member_access(
                            ex_tier,
                            channel_enabled=bool(cfg_rep["enabled"]),
                            db_allow_external=cfg_rep.get("allow_external"),
                        )
                        if not rep_ok:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": rep_deny or "weixin_channel_disabled",
                                "existing_status": existing["status"],
                            }

                        ex_deny = (
                            str(ex_payload.get("deny_reason"))
                            if isinstance(ex_payload, dict) and ex_payload.get("deny_reason")
                            else None
                        )
                        if existing["status"] == "ignored":
                            if ex_acc_id is None and ex_uid is None:
                                return {
                                    "ok": False,
                                    "linked": True,
                                    "allowed": False,
                                    "deny_reason": ex_deny or "ignored",
                                    "existing_status": "ignored",
                                }
                            out_rep = _serialize_inbound_row(
                                existing,
                                existing_status="ignored",
                                linked=True,
                                allowed=False,
                                deny_reason=ex_deny or "ignored",
                                generation=existing.get("session_generation"),
                            )
                            return out_rep

                        out_rep = _serialize_inbound_row(
                            existing,
                            existing_status=existing["status"],
                            linked=True,
                            allowed=True,
                            deny_reason=None,
                            user_tier=ex_tier,
                            generation=existing.get("session_generation"),
                            pending_interrupt=existing.get("pending_interrupt_snapshot"),
                        )
                        return out_rep

                    # Early durable rejection for group or invalid payloads
                    if is_weixin_group or is_invalid_inbound:
                        d_reason = (
                            "weixin_group_unsupported"
                            if is_weixin_group
                            else "invalid_inbound_event"
                        )
                        payload_dict["deny_reason"] = d_reason
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_inbound_events (
                                platform, message_id, request_key, chat_type,
                                chat_address, thread_scope_key, status,
                                fallback_pending, raw_payload, event_kind
                            )
                            VALUES ('weixin', %s, %s, %s, %s, %s, 'ignored', FALSE, %s, 'message')
                            ON CONFLICT (platform, message_id) DO NOTHING
                            RETURNING *
                            """,
                            (
                                cleaned_msg_id,
                                req_uuid,
                                cleaned_chat_type,
                                cleaned_address,
                                cleaned_scope,
                                Jsonb(payload_dict),
                            ),
                        )
                        rej = dict(await cur.fetchone())
                        return _serialize_inbound_row(
                            rej,
                            existing_status=None,
                            linked=False,
                            allowed=False,
                            deny_reason=d_reason,
                        )

                    # Fast-path Level 3a FOR NO KEY UPDATE on dalton_channel_weixin_accounts
                    acc: dict[str, Any] | None = None
                    if eff_aid_param:
                        await cur.execute(
                            "SELECT * FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid FOR NO KEY UPDATE",
                            (eff_aid_param,),
                        )
                        acc = await cur.fetchone()
                    elif eff_bot_id_param:
                        await cur.execute(
                            "SELECT * FROM dalton_channel_weixin_accounts WHERE ilink_bot_id = %s FOR NO KEY UPDATE",
                            (eff_bot_id_param,),
                        )
                        acc = await cur.fetchone()
                    elif cleaned_puid:
                        await cur.execute(
                            "SELECT * FROM dalton_channel_weixin_accounts WHERE ilink_user_id = %s FOR NO KEY UPDATE",
                            (cleaned_puid,),
                        )
                        acc = await cur.fetchone()

                    if acc is None:
                        payload_dict["deny_reason"] = "not_linked"
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_inbound_events (
                                platform, message_id, request_key, chat_type,
                                chat_address, thread_scope_key, status,
                                fallback_pending, raw_payload, event_kind
                            )
                            VALUES ('weixin', %s, %s, %s, %s, %s, 'ignored', FALSE, %s, 'message')
                            ON CONFLICT (platform, message_id) DO NOTHING
                            RETURNING *
                            """,
                            (
                                cleaned_msg_id,
                                req_uuid,
                                cleaned_chat_type,
                                cleaned_address,
                                cleaned_scope,
                                Jsonb(payload_dict),
                            ),
                        )
                        rej = dict(await cur.fetchone())
                        return _serialize_inbound_row(
                            rej,
                            existing_status=None,
                            linked=False,
                            allowed=False,
                            deny_reason="not_linked",
                        )

                    acc = dict(acc)
                    acc_id = str(acc["account_id"])
                    acc_status = str(acc["status"])

                    # D11: pending_verification -> roll back this fast-path tx immediately to release Level 3a lock!
                    if acc_status == "pending_verification":
                        if wx_db._after_pending_verify_read_hook is not None:
                            await wx_db._after_pending_verify_read_hook()
                        pending_account_to_verify = acc_id
                        # Roll back by raising internally? No, nothing written yet — just break out of tx!
                    else:
                        # Validate bot_id, bot_generation, account status & sender match
                        acc_bot_id = str(acc["ilink_bot_id"])
                        acc_bot_gen = int(acc["bot_generation"])
                        acc_uid = str(acc["user_id"])
                        reject_company_system_auth(acc_uid)

                        if (
                            (eff_bot_id_param and eff_bot_id_param != acc_bot_id)
                            or (
                                eff_bot_gen_param is not None
                                and eff_bot_gen_param != acc_bot_gen
                            )
                        ):
                            return {
                                "existing_status": None,
                                "linked": True,
                                "allowed": False,
                                "deny_reason": "weixin_bot_generation_mismatch",
                                "dalton_user_id": acc_uid,
                            }

                        if acc_status != "active" or (
                            acc.get("ilink_user_id")
                            and str(acc["ilink_user_id"]) != cleaned_puid
                        ):
                            deny_r = (
                                "weixin_sender_mismatch"
                                if acc_status == "active"
                                else f"weixin_account_{acc_status}"
                            )
                            payload_dict["deny_reason"] = deny_r
                            await cur.execute(
                                """
                                INSERT INTO dalton_channel_inbound_events (
                                    platform, message_id, request_key, dalton_user_id,
                                    chat_type, chat_address, thread_scope_key, status,
                                    fallback_pending, raw_payload, event_kind,
                                    weixin_account_id
                                )
                                VALUES ('weixin', %s, %s, %s, %s, %s, %s, 'ignored', FALSE, %s, 'message', %s::uuid)
                                ON CONFLICT (platform, message_id) DO NOTHING
                                RETURNING *
                                """,
                                (
                                    cleaned_msg_id,
                                    req_uuid,
                                    acc_uid,
                                    cleaned_chat_type,
                                    cleaned_address,
                                    cleaned_scope,
                                    Jsonb(payload_dict),
                                    acc_id,
                                ),
                            )
                            rej = dict(await cur.fetchone())
                            return _serialize_inbound_row(
                                rej,
                                existing_status=None,
                                linked=True,
                                allowed=False,
                                deny_reason=deny_r,
                            )

                        # Three-state live roster check (§4.2)
                        roster_status = await check_team_member_status_live(
                            acc_uid, conn=owned_conn
                        )
                        if roster_status == "lookup_error":
                            raise HTTPException(
                                status_code=503,
                                detail={
                                    "code": "roster_lookup_error",
                                    "message": "成员名册查询异常，请保留游标稍后重试",
                                },
                            )
                        if roster_status != "active":
                            payload_dict["deny_reason"] = "member_not_in_roster"
                            await cur.execute(
                                """
                                INSERT INTO dalton_channel_inbound_events (
                                    platform, message_id, request_key, dalton_user_id,
                                    chat_type, chat_address, thread_scope_key, status,
                                    fallback_pending, raw_payload, event_kind,
                                    weixin_account_id
                                )
                                VALUES ('weixin', %s, %s, %s, %s, %s, %s, 'ignored', FALSE, %s, 'message', %s::uuid)
                                ON CONFLICT (platform, message_id) DO NOTHING
                                RETURNING *
                                """,
                                (
                                    cleaned_msg_id,
                                    req_uuid,
                                    acc_uid,
                                    cleaned_chat_type,
                                    cleaned_address,
                                    cleaned_scope,
                                    Jsonb(payload_dict),
                                    acc_id,
                                ),
                            )
                            rej = dict(await cur.fetchone())
                            return _serialize_inbound_row(
                                rej,
                                existing_status=None,
                                linked=True,
                                allowed=False,
                                deny_reason="member_not_in_roster",
                            )

                        user_tier = await get_user_tier(acc_uid, conn=owned_conn)
                        cfg = await wx_db.ensure_and_lock_weixin_config_in_tx(
                            cur, for_update=False
                        )
                        access_ok, access_deny = evaluate_weixin_member_access(
                            user_tier,
                            channel_enabled=bool(cfg["enabled"]),
                            db_allow_external=cfg.get("allow_external"),
                        )
                        if not access_ok:
                            payload_dict["deny_reason"] = access_deny or "weixin_channel_disabled"
                            await cur.execute(
                                """
                                INSERT INTO dalton_channel_inbound_events (
                                    platform, message_id, request_key, dalton_user_id,
                                    chat_type, chat_address, thread_scope_key, status,
                                    fallback_pending, raw_payload, event_kind,
                                    weixin_account_id
                                )
                                VALUES ('weixin', %s, %s, %s, %s, %s, %s, 'ignored', FALSE, %s, 'message', %s::uuid)
                                ON CONFLICT (platform, message_id) DO NOTHING
                                RETURNING *
                                """,
                                (
                                    cleaned_msg_id,
                                    req_uuid,
                                    acc_uid,
                                    cleaned_chat_type,
                                    cleaned_address,
                                    cleaned_scope,
                                    Jsonb(payload_dict),
                                    acc_id,
                                ),
                            )
                            rej = dict(await cur.fetchone())
                            return _serialize_inbound_row(
                                rej,
                                existing_status=None,
                                linked=True,
                                allowed=False,
                                deny_reason=access_deny or "weixin_channel_disabled",
                                user_tier=user_tier,
                            )

                        # Upsert context token into dalton_channel_weixin_account_tokens (Level 5)
                        enc_key = get_encryption_key()
                        stored_seq = 0
                        if eff_ctx_token:
                            _, stored_seq, _ = (
                                await wx_db.upsert_weixin_account_context_token_in_tx(
                                    cur,
                                    account_id=acc_id,
                                    ilink_bot_id=acc_bot_id,
                                    bot_generation=acc_bot_gen,
                                    platform_user_id=cleaned_puid,
                                    context_token=eff_ctx_token,
                                    incoming_seq=incoming_seq,
                                    incoming_source=incoming_source,
                                    enc_key=enc_key,
                                )
                            )
                        cur_tok, _, cur_stale = (
                            await wx_db.resolve_weixin_account_context_token_in_tx(
                                cur,
                                account_id=acc_id,
                                bot_generation=acc_bot_gen,
                                platform_user_id=cleaned_puid,
                                enc_key=enc_key,
                            )
                        )
                        has_usable_ctx = bool(cur_tok) and not cur_stale

                        # Insert inbound event row
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_inbound_events (
                                platform, message_id, request_key, dalton_user_id,
                                chat_type, chat_address, thread_scope_key, status,
                                fallback_pending, raw_payload, event_kind,
                                weixin_account_id, weixin_context_bot_id,
                                weixin_context_bot_generation, weixin_context_seq
                            )
                            VALUES (
                                'weixin', %s, %s, %s,
                                %s, %s, %s, 'pending',
                                FALSE, %s, 'message',
                                %s::uuid, %s, %s, %s
                            )
                            ON CONFLICT (platform, message_id) DO NOTHING
                            RETURNING *
                            """,
                            (
                                cleaned_msg_id,
                                req_uuid,
                                acc_uid,
                                cleaned_chat_type,
                                cleaned_address,
                                cleaned_scope,
                                Jsonb(payload_dict),
                                acc_id,
                                acc_bot_id,
                                acc_bot_gen,
                                stored_seq,
                            ),
                        )
                        inbound_row = dict(await cur.fetchone())
                        if eff_ctx_token:
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET encrypted_weixin_context_token = pgp_sym_encrypt(%s, %s)
                                WHERE platform = 'weixin' AND message_id = %s
                                """,
                                (eff_ctx_token, enc_key, cleaned_msg_id),
                            )

                        # Pure unsupported merged forward (§4.4 (1))
                        raw_text = str(
                            payload_dict.get("text")
                            or payload_dict.get("content")
                            or ""
                        ).strip()
                        has_media = bool(eff_media_meta)
                        if unsupported_item_types and not raw_text and not has_media:
                            payload_dict["deny_reason"] = "unsupported_merged_forward"
                            payload_dict["unsupported_item_types"] = list(
                                unsupported_item_types
                            )
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET status = 'ignored', raw_payload = %s, updated_at = NOW()
                                WHERE platform = 'weixin' AND message_id = %s
                                RETURNING *
                                """,
                                (Jsonb(payload_dict), cleaned_msg_id),
                            )
                            ign = dict(await cur.fetchone())
                            out_ign = _serialize_inbound_row(
                                ign,
                                existing_status=None,
                                linked=True,
                                allowed=False,
                                deny_reason="unsupported_merged_forward",
                                user_tier=user_tier,
                            )
                            out_ign["has_usable_weixin_context_token"] = has_usable_ctx
                            return out_ign

                        # Lock session generation row (Level 6)
                        gen_row, inserted_new, db_now = await _lock_or_create_session_row(
                            cur,
                            platform="weixin",
                            scope_key=cleaned_scope,
                            dalton_user_id=acc_uid,
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
                            if not inserted_new and override_ws_id is not None:
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

                        # Normal Weixin DM chat flow: resolve workspace & write permission
                        target_ws_id: str | None = None
                        workspace_status: str | None = None
                        workspace_owner_id: str | None = None
                        if override_ws_id:
                            ok_w, deny_w, ws_row = await _verify_workspace_write_permission(
                                cur, override_ws_id, acc_uid, conn=owned_conn
                            )
                            if not ok_w or ws_row is None:
                                await cur.execute(
                                    """
                                    UPDATE dalton_channel_inbound_events
                                    SET status = 'ignored', updated_at = NOW()
                                    WHERE platform = 'weixin' AND message_id = %s
                                    RETURNING *
                                    """,
                                    (cleaned_msg_id,),
                                )
                                rej_ws = dict(await cur.fetchone())
                                out_ws = _serialize_inbound_row(
                                    rej_ws,
                                    existing_status=None,
                                    linked=True,
                                    allowed=False,
                                    deny_reason=deny_w or "workspace_write_denied",
                                    user_tier=user_tier,
                                )
                                out_ws["has_usable_weixin_context_token"] = has_usable_ctx
                                return out_ws
                            target_ws_id = str(ws_row["workspace_id"])
                            workspace_status = ws_row.get("status")
                            workspace_owner_id = str(ws_row["user_id"])
                        if target_ws_id is None:
                            home_ws_id = get_home_workspace_id(acc_uid)
                            target_ws_id = home_ws_id
                            home_row = await _fetch_workspace_row(cur, home_ws_id)
                            workspace_status = home_row.get("status") if home_row else None
                            workspace_owner_id = (
                                str(home_row["user_id"]) if home_row else acc_uid
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
                            WHERE platform = 'weixin' AND scope_key = %s AND dalton_user_id = %s
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
                                cleaned_scope,
                                acc_uid,
                            ),
                        )

                        ext_tid = format_external_thread_id(
                            "weixin", cleaned_scope, acc_uid, generation, "dm"
                        )
                        payload_dict["generation"] = generation
                        if has_media:
                            media_json = json.dumps(eff_media_meta, ensure_ascii=False)
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET workspace_id = %s,
                                    external_thread_id = %s,
                                    session_generation = %s,
                                    pending_interrupt_snapshot = %s,
                                    raw_payload = %s,
                                    encrypted_weixin_media_meta = pgp_sym_encrypt(%s, %s),
                                    updated_at = NOW()
                                WHERE platform = 'weixin' AND message_id = %s
                                RETURNING *
                                """,
                                (
                                    target_ws_id,
                                    ext_tid,
                                    generation,
                                    Jsonb(pending_interrupt)
                                    if pending_interrupt is not None
                                    else None,
                                    Jsonb(payload_dict),
                                    media_json,
                                    enc_key,
                                    cleaned_msg_id,
                                ),
                            )
                        else:
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET workspace_id = %s,
                                    external_thread_id = %s,
                                    session_generation = %s,
                                    pending_interrupt_snapshot = %s,
                                    raw_payload = %s,
                                    updated_at = NOW()
                                WHERE platform = 'weixin' AND message_id = %s
                                RETURNING *
                                """,
                                (
                                    target_ws_id,
                                    ext_tid,
                                    generation,
                                    Jsonb(pending_interrupt)
                                    if pending_interrupt is not None
                                    else None,
                                    Jsonb(payload_dict),
                                    cleaned_msg_id,
                                ),
                            )
                        inbound_row = dict(await cur.fetchone())
                        out_ok = _serialize_inbound_row(
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
                        out_ok["weixin_account_id"] = acc_id
                        out_ok["weixin_bot_generation"] = acc_bot_gen
                        out_ok["has_usable_weixin_context_token"] = has_usable_ctx
                        return out_ok

        # Outside fast-path transaction: Level 3a lock released! Now run D11 two-phase verification (Level 1 -> Level 3a)
        if pending_account_to_verify is None:
            break
        ver_res = await wx_db.verify_pending_weixin_account_first_message(
            account_id=pending_account_to_verify,
            platform_user_id=cleaned_puid,
            expected_bot_id=eff_bot_id_param,
            expected_bot_generation=eff_bot_gen_param,
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
            inbound_context_token=eff_ctx_token,
            conn=conn,
        )
        if ver_res["outcome"] == "conflict_unbound":
            return {
                "existing_status": None,
                "linked": False,
                "allowed": False,
                "deny_reason": "ilink_user_conflict_auto_unbound",
                "one_time_weixin_reply": ver_res.get("one_time_weixin_reply"),
                "accounts_revision": ver_res.get("accounts_revision"),
            }
        if ver_res["outcome"] == "denied":
            return {
                "existing_status": None,
                "linked": True,
                "allowed": False,
                "deny_reason": ver_res.get("deny_reason") or "weixin_channel_disabled",
            }
        if ver_res["outcome"] == "stale_generation":
            return {
                "existing_status": None,
                "linked": True,
                "allowed": False,
                "deny_reason": "weixin_bot_generation_mismatch",
            }
        # Loop back to pass 2 with account now active!

    return {
        "existing_status": None,
        "linked": False,
        "allowed": False,
        "deny_reason": "not_linked",
    }


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
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    weixin_context_seq: int | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
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
    eff_ctx_token = (
        str(weixin_context_token).strip()
        if weixin_context_token
        else str((raw_payload or {}).get("weixin_context_token") or "").strip() or None
    )
    incoming_order = _extract_weixin_context_order(raw_payload, weixin_context_seq)
    incoming_seq = incoming_order[1] if incoming_order is not None else None
    incoming_source = incoming_order[0] if incoming_order is not None else None
    payload_dict = _sanitize_native_payload_dict(
        _strip_sensitive_from_raw_payload(raw_payload)
    )
    if cleaned_puid:
        payload_dict["platform_user_id"] = cleaned_puid

    is_weixin_group = cleaned_platform == "weixin" and (
        cleaned_chat_type == "group"
        or bool(str(payload_dict.get("group_id") or "").strip())
        or cleaned_address.startswith("weixin:group:")
        or cleaned_scope.startswith("chat:")
    )
    is_invalid_inbound = (
        _payload_marks_invalid_inbound(payload_dict)
        or not cleaned_puid
        or cleaned_puid == "__invalid__"
    )
    if is_invalid_inbound:
        payload_dict["invalid_inbound_event"] = True
        payload_dict["deny_reason"] = "invalid_inbound_event"
        payload_dict.setdefault("rejected_reason", "invalid_inbound_event")

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    gateway_epoch,
                    required=require_gateway_epoch,
                    platform=cleaned_platform,
                )
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_inbound:{cleaned_platform}:{cleaned_msg_id}",),
                )
                await cur.execute(
                    """
                    SELECT dalton_user_id, raw_payload
                    FROM dalton_channel_inbound_events
                    WHERE platform = %s AND message_id = %s
                    """,
                    (cleaned_platform, cleaned_msg_id),
                )
                pre_inbound = await cur.fetchone()
                if pre_inbound:
                    reject_company_system_auth(pre_inbound.get("dalton_user_id"))
                probe_puid = cleaned_puid or (
                    str((pre_inbound.get("raw_payload") or {}).get("platform_user_id") or "").strip()
                    if pre_inbound and isinstance(pre_inbound.get("raw_payload"), dict)
                    else ""
                )
                if probe_puid:
                    await cur.execute(
                        """
                        SELECT user_id FROM dalton_channel_identities
                        WHERE platform = %s AND platform_user_id = %s
                        """,
                        (cleaned_platform, probe_puid),
                    )
                    pre_identity = await cur.fetchone()
                    if pre_identity:
                        reject_company_system_auth(pre_identity.get("user_id"))

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
                    if existing.get("status") == "pending" and (
                        is_invalid_inbound
                        or _payload_marks_invalid_inbound(existing_payload)
                    ):
                        rejected_payload = (
                            dict(existing_payload)
                            if isinstance(existing_payload, dict)
                            else {}
                        )
                        rejected_payload.update(payload_dict)
                        rejected_payload["invalid_inbound_event"] = True
                        rejected_payload["deny_reason"] = "invalid_inbound_event"
                        rejected_payload.setdefault(
                            "rejected_reason", "invalid_inbound_event"
                        )
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET status = 'ignored', raw_payload = %s, updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                            RETURNING *
                            """,
                            (Jsonb(rejected_payload), cleaned_platform, cleaned_msg_id),
                        )
                        rejected_row = dict(await cur.fetchone())
                        return _serialize_inbound_row(
                            rejected_row,
                            existing_status="ignored",
                            linked=bool(rejected_row.get("dalton_user_id")),
                            allowed=False,
                            deny_reason="invalid_inbound_event",
                        )
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
                    ex_deny_reason = None
                    if isinstance(existing_payload, dict) and existing_payload.get("deny_reason"):
                        ex_deny_reason = str(existing_payload["deny_reason"])
                    if existing["status"] == "ignored" and not ex_deny_reason:
                        if is_weixin_group:
                            ex_deny_reason = "weixin_group_unsupported"
                        elif is_invalid_inbound or _payload_marks_invalid_inbound(existing_payload):
                            ex_deny_reason = "invalid_inbound_event"
                        elif not existing.get("dalton_user_id"):
                            ex_deny_reason = "not_linked"
                    if existing["status"] == "ignored" and ex_deny_reason in (
                        "weixin_group_unsupported",
                        "invalid_inbound_event",
                    ):
                        return _serialize_inbound_row(
                            existing,
                            existing_status="ignored",
                            linked=bool(existing.get("dalton_user_id")),
                            allowed=False,
                            deny_reason=ex_deny_reason,
                            generation=gen_val,
                        )

                    # Re-verify identity on replay
                    ex_uid = existing.get("dalton_user_id")
                    reject_company_system_auth(ex_uid)
                    if eff_puid:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_identities
                            WHERE platform = %s AND platform_user_id = %s
                            """,
                            (cleaned_platform, eff_puid),
                        )
                        id_replay = await cur.fetchone()
                        if id_replay:
                            reject_company_system_auth(id_replay.get("user_id"))
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

                    ex_tier = await get_user_tier(ex_uid, conn=owned_conn)
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
                        deny_reason=ex_deny_reason,
                        user_tier=ex_tier,
                        workspace_status=ws_status,
                        workspace_owner_id=ws_owner_id,
                        generation=gen_val,
                        pending_interrupt=pending_int,
                    )

                inbound_row = dict(inserted)

                # 1b. Early durable rejection for Weixin group messages or invalid inbound payloads
                if is_weixin_group:
                    payload_dict["deny_reason"] = "weixin_group_unsupported"
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET status = 'ignored', raw_payload = %s, updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        RETURNING *
                        """,
                        (Jsonb(payload_dict), cleaned_platform, cleaned_msg_id),
                    )
                    inbound_row = dict(await cur.fetchone())
                    return _serialize_inbound_row(
                        inbound_row,
                        existing_status=None,
                        linked=False,
                        allowed=False,
                        deny_reason="weixin_group_unsupported",
                    )

                if is_invalid_inbound:
                    payload_dict["deny_reason"] = "invalid_inbound_event"
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET status = 'ignored', raw_payload = %s, updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        RETURNING *
                        """,
                        (Jsonb(payload_dict), cleaned_platform, cleaned_msg_id),
                    )
                    inbound_row = dict(await cur.fetchone())
                    return _serialize_inbound_row(
                        inbound_row,
                        existing_status=None,
                        linked=False,
                        allowed=False,
                        deny_reason="invalid_inbound_event",
                    )

                # 1c. For valid Weixin DM messages, persist encrypted context_token with Bot & monotonic seq guard
                if cleaned_platform == "weixin" and eff_ctx_token:
                    active_bot_id, active_bot_gen, _ = await _get_active_weixin_bot_in_tx(
                        cur, lock_generation=True
                    )
                    req_bot_id = str(weixin_bot_id or payload_dict.get("ilink_bot_id") or "").strip() or None
                    req_bot_gen = (
                        int(weixin_bot_generation)
                        if weixin_bot_generation is not None
                        else (
                            int(payload_dict["bot_generation"])
                            if payload_dict.get("bot_generation") is not None
                            else None
                        )
                    )
                    eff_bot_id = active_bot_id or req_bot_id or "default"
                    eff_bot_gen = active_bot_gen if active_bot_id else (req_bot_gen or 1)
                    bot_matches = (
                        (active_bot_id is None or req_bot_id is None or req_bot_id == active_bot_id)
                        and (active_bot_id is None or req_bot_gen is None or req_bot_gen == active_bot_gen)
                    )
                    if bot_matches:
                        enc_key = get_encryption_key()
                        _, stored_seq = await _upsert_weixin_context_token_in_tx(
                            cur,
                            enc_key=enc_key,
                            bot_id=eff_bot_id,
                            bot_generation=eff_bot_gen,
                            platform_user_id=cleaned_puid,
                            context_token=eff_ctx_token,
                            incoming_seq=incoming_seq,
                            incoming_source=incoming_source,
                        )
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET encrypted_weixin_context_token = pgp_sym_encrypt(%s, %s),
                                weixin_context_bot_id = %s,
                                weixin_context_bot_generation = %s,
                                weixin_context_seq = %s,
                                updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                            """,
                            (
                                eff_ctx_token,
                                enc_key,
                                eff_bot_id,
                                eff_bot_gen,
                                stored_seq,
                                cleaned_platform,
                                cleaned_msg_id,
                            ),
                        )

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
                    payload_dict["deny_reason"] = "not_linked"
                    await cur.execute(
                        """
                        UPDATE dalton_channel_inbound_events
                        SET status = 'ignored', raw_payload = %s, updated_at = NOW()
                        WHERE platform = %s AND message_id = %s
                        RETURNING *
                        """,
                        (Jsonb(payload_dict), cleaned_platform, cleaned_msg_id),
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
                reject_company_system_auth(dalton_user_id)

                # 3. External member group chat veto
                user_tier = await get_user_tier(dalton_user_id, conn=owned_conn)
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
    reject_company_system_auth(cleaned_uid)
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
    cur: Any, user_id: str, platform: str, *, same_account: bool = True
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
        existing = apps.get(platform)
        # A rebind keeps the Dalton user's registered group chats and automation
        # output choices: resetting them silently dropped group targets
        # (2026-11-10). Group delivery is gated on the Dalton user (tier and
        # workspace access), not on the platform account. Personal routing
        # (preferred target, agent-message allowlist) is reset when the bound
        # account changes, so no address of the old account survives.
        # Only an explicit unbind clears the platform's settings.
        if isinstance(existing, dict):
            kept = existing if same_account else {
                k: v for k, v in existing.items() if k in ("chats", "automation_output")
            }
            merged = {**default_app, **kept}
        else:
            merged = default_app
        if merged == existing:
            return
        apps[platform] = merged
        next_ver = bump_version_str(row.get("version"))
        await cur.execute(
            """
            UPDATE dalton_channel_user_settings
            SET version = %s, apps = %s, updated_at = NOW()
            WHERE user_id = %s
            """,
            (next_ver, Jsonb(apps), user_id),
        )


def _plain_weixin_token(value: Any) -> str | None:
    """Return decrypted Weixin token text, or None when the value is empty."""
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8")
    text = str(value).strip()
    return text or None


async def _saved_weixin_bind_message_in_tx(
    cur: Any,
    *,
    enc_key: str,
    platform: str,
    inbound_message_id: str | None,
    bot_id: str,
    bot_generation: int,
) -> dict[str, Any] | None:
    """Load one inbound when its bot id and generation match the active bot.

    The message order comes from that payload's `seq` or `create_time_ms`.
    `weixin_context_seq` on the row can name a later token already stored for
    this bot generation, so it is not this message's order.
    """
    cleaned_mid = str(inbound_message_id or "").strip()
    if not cleaned_mid:
        return None
    await cur.execute(
        """
        SELECT raw_payload,
               weixin_context_seq,
               weixin_context_bot_id,
               weixin_context_bot_generation,
               pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS inbound_context_token
        FROM dalton_channel_inbound_events
        WHERE message_id = %s AND platform = %s
        """,
        (enc_key, cleaned_mid, platform),
    )
    row = await cur.fetchone()
    if not row:
        return None
    inbound_bot = str(row.get("weixin_context_bot_id") or "").strip() or None
    inbound_gen = row.get("weixin_context_bot_generation")
    if inbound_bot is not None and inbound_bot != bot_id:
        return None
    if inbound_gen is not None and int(inbound_gen) != int(bot_generation):
        return None
    raw_payload = row.get("raw_payload")
    if isinstance(raw_payload, str):
        try:
            parsed = json.loads(raw_payload)
        except json.JSONDecodeError:
            parsed = None
        raw_payload = parsed if isinstance(parsed, dict) else None
    elif not isinstance(raw_payload, dict):
        raw_payload = None
    order = _extract_weixin_context_order(raw_payload)
    return {
        "seq": order[1] if order is not None else None,
        "seq_source": order[0] if order is not None else None,
        "context_token": _plain_weixin_token(row.get("inbound_context_token")),
    }


async def _bind_identity_in_tx(
    cur: Any,
    *,
    user_id: str,
    platform: str,
    platform_user_id: str,
    platform_username: str | None = None,
    platform_tenant_key: str | None = None,
    weixin_context_token: str | None = None,
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    weixin_context_seq: int | None = None,
    inbound_message_id: str | None = None,
    settings_already_locked: frozenset[str] | None = None,
) -> int:
    """Bind `(user_id, platform)` to `platform_user_id`, advance existing generations, and initialize settings.

    A Weixin bind reads the saved message's own order fields. That saved order
    replaces the current token only when it is strictly newer in the same
    field. The identity then copies the current token and that token's source
    and value. An older message does not replace the current token.

    Settings locks come before the Weixin singleton. A caller that already holds
    the singleton passes `settings_already_locked` and this function does not
    take another settings lock. After the sender lock, the bind reads the
    owners again. A user who was not in the locked set raises
    `_WeixinSettingsLockSetChanged` before any identity change.
    """
    reject_company_system_auth(user_id)
    if str(platform or "").strip().lower() == "weixin":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "weixin_uses_qr_login",
                "message": "个人微信已切换为每人扫码连接，不再使用绑定码。",
            },
        )
    await cur.execute(
        """
        SELECT platform_user_id FROM dalton_channel_identities
        WHERE user_id = %s AND platform = %s
        """,
        (user_id, platform),
    )
    prior_identity = await cur.fetchone()
    same_account = bool(prior_identity) and str(
        prior_identity.get("platform_user_id") or ""
    ) == str(platform_user_id)
    await cur.execute(
        """
        SELECT user_id FROM dalton_channel_identities
        WHERE platform = %s AND platform_user_id = %s AND user_id != %s
        """,
        (platform, platform_user_id, user_id),
    )
    required_users = {user_id, *(str(r["user_id"]) for r in await cur.fetchall() if r.get("user_id"))}
    if settings_already_locked is None:
        hook = _weixin_settings_discovery_hook
        if hook is not None:
            await hook()
        held_settings = await _lock_user_settings_rows_in_tx(cur, required_users)
        await cur.execute(
            """
            SELECT user_id FROM dalton_channel_identities
            WHERE platform = %s AND platform_user_id = %s AND user_id != %s
            """,
            (platform, platform_user_id, user_id),
        )
        required_after = {
            user_id,
            *(str(r["user_id"]) for r in await cur.fetchall() if r.get("user_id")),
        }
        if not required_after.issubset(held_settings):
            raise _WeixinSettingsLockSetChanged()
    else:
        if not required_users.issubset(settings_already_locked):
            raise _WeixinSettingsLockSetChanged()
        held_settings = settings_already_locked
    if platform == "weixin":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "weixin_uses_qr_login",
                "message": "个人微信已切换为每人扫码连接，不再使用绑定码。",
            },
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

    eff_wx_token: str | None = None
    eff_wx_bot_id: str | None = None
    eff_wx_bot_gen: int | None = None
    eff_wx_seq = 0
    eff_wx_source = "protocol"

    if platform == "weixin":
        active_bot_id, active_bot_gen, _ = await _get_active_weixin_bot_in_tx(cur)
        eff_wx_bot_id = str(weixin_bot_id or active_bot_id or "default").strip()
        eff_wx_bot_gen = (
            int(weixin_bot_generation)
            if weixin_bot_generation is not None
            else int(active_bot_gen or 1)
        )
        bot_matches = (
            active_bot_id is None
            or (
                (weixin_bot_id is None or str(weixin_bot_id).strip() == active_bot_id)
                and (weixin_bot_generation is None or int(weixin_bot_generation) == int(active_bot_gen))
            )
        )
        enc_key = get_encryption_key()
        bind_token = _plain_weixin_token(weixin_context_token)
        bind_order = _extract_weixin_context_order(None, weixin_context_seq)
        bind_seq = bind_order[1] if bind_order is not None else None
        bind_source = bind_order[0] if bind_order is not None else None
        if bot_matches:
            saved_message = await _saved_weixin_bind_message_in_tx(
                cur,
                enc_key=enc_key,
                platform=platform,
                inbound_message_id=inbound_message_id,
                bot_id=eff_wx_bot_id,
                bot_generation=int(eff_wx_bot_gen),
            )
            if saved_message is not None:
                if saved_message["seq"] is not None and saved_message["seq_source"]:
                    bind_seq = int(saved_message["seq"])
                    bind_source = str(saved_message["seq_source"])
                if bind_token is None and saved_message["context_token"]:
                    bind_token = saved_message["context_token"]
        if bind_token and bot_matches:
            await cur.execute(
                """
                SELECT seq, seq_source FROM dalton_channel_weixin_context_tokens
                WHERE ilink_bot_id = %s AND bot_generation = %s AND platform_user_id = %s
                FOR UPDATE
                """,
                (eff_wx_bot_id, int(eff_wx_bot_gen), platform_user_id),
            )
            existing_token = await cur.fetchone()
            store_message_token = existing_token is None or (
                bind_seq is not None
                and bind_source is not None
                and _saved_context_order_supersedes(
                    bind_source,
                    int(bind_seq),
                    _context_order_source(existing_token.get("seq_source")),
                    int(existing_token.get("seq") or 0),
                )
            )
            if store_message_token:
                _, eff_wx_seq = await _upsert_weixin_context_token_in_tx(
                    cur,
                    enc_key=enc_key,
                    bot_id=eff_wx_bot_id,
                    bot_generation=eff_wx_bot_gen,
                    platform_user_id=platform_user_id,
                    context_token=bind_token,
                    incoming_seq=bind_seq,
                    incoming_source=bind_source,
                )
        if bot_matches:
            await cur.execute(
                """
                SELECT pgp_sym_decrypt(encrypted_context_token, %s) AS context_token,
                       COALESCE(seq, 0) AS seq,
                       seq_source
                FROM dalton_channel_weixin_context_tokens
                WHERE ilink_bot_id = %s AND bot_generation = %s AND platform_user_id = %s
                """,
                (enc_key, eff_wx_bot_id, eff_wx_bot_gen, platform_user_id),
            )
            ctx_row = await cur.fetchone()
            plain_token = (
                _plain_weixin_token(ctx_row.get("context_token")) if ctx_row else None
            )
            if ctx_row and plain_token:
                eff_wx_token = plain_token
                eff_wx_seq = int(ctx_row.get("seq") or 0)
                eff_wx_source = str(ctx_row.get("seq_source") or "protocol")
            elif bind_token:
                eff_wx_token = bind_token
                eff_wx_seq = int(bind_seq or 1)
                eff_wx_source = bind_source or "receive"

    if platform == "weixin" and eff_wx_token:
        enc_key = get_encryption_key()
        await cur.execute(
            """
            INSERT INTO dalton_channel_identities (
                user_id, platform, platform_user_id, platform_username,
                platform_tenant_key, weixin_context_token,
                encrypted_weixin_context_token, weixin_context_bot_id,
                weixin_context_bot_generation, weixin_context_seq,
                weixin_context_seq_source,
                weixin_context_updated_at, created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s, NULL,
                pgp_sym_encrypt(%s, %s), %s, %s, %s, %s, NOW(), NOW(), NOW()
            )
            ON CONFLICT (user_id, platform) DO UPDATE SET
                platform_user_id = EXCLUDED.platform_user_id,
                platform_username = COALESCE(EXCLUDED.platform_username, dalton_channel_identities.platform_username),
                platform_tenant_key = COALESCE(EXCLUDED.platform_tenant_key, dalton_channel_identities.platform_tenant_key),
                weixin_context_token = NULL,
                encrypted_weixin_context_token = EXCLUDED.encrypted_weixin_context_token,
                weixin_context_bot_id = EXCLUDED.weixin_context_bot_id,
                weixin_context_bot_generation = EXCLUDED.weixin_context_bot_generation,
                weixin_context_seq = EXCLUDED.weixin_context_seq,
                weixin_context_seq_source = EXCLUDED.weixin_context_seq_source,
                weixin_context_updated_at = NOW(),
                updated_at = NOW()
            """,
            (
                user_id,
                platform,
                platform_user_id,
                platform_username,
                platform_tenant_key,
                eff_wx_token,
                enc_key,
                eff_wx_bot_id,
                eff_wx_bot_gen,
                eff_wx_seq,
                eff_wx_source,
            ),
        )
    else:
        await cur.execute(
            """
            INSERT INTO dalton_channel_identities (
                user_id, platform, platform_user_id, platform_username,
                platform_tenant_key, weixin_context_token,
                encrypted_weixin_context_token, weixin_context_bot_id,
                weixin_context_bot_generation, weixin_context_seq,
                weixin_context_seq_source,
                weixin_context_updated_at, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL, NULL, 0, '', NULL, NOW(), NOW())
            ON CONFLICT (user_id, platform) DO UPDATE SET
                platform_user_id = EXCLUDED.platform_user_id,
                platform_username = COALESCE(EXCLUDED.platform_username, dalton_channel_identities.platform_username),
                platform_tenant_key = COALESCE(EXCLUDED.platform_tenant_key, dalton_channel_identities.platform_tenant_key),
                weixin_context_token = NULL,
                encrypted_weixin_context_token = NULL,
                weixin_context_bot_id = NULL,
                weixin_context_bot_generation = NULL,
                weixin_context_seq = 0,
                weixin_context_seq_source = '',
                weixin_context_updated_at = NULL,
                updated_at = NOW()
            """,
            (
                user_id,
                platform,
                platform_user_id,
                platform_username,
                platform_tenant_key,
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

    await _ensure_platform_default_settings(
        cur, user_id, platform, same_account=same_account
    )
    return current_gen


def _bind_confirmation_delivery_key(
    platform: str,
    message_id: str,
    action: str,
    result: dict[str, Any],
) -> str:
    reason = str(result.get("reason") or "")
    ok = bool(result.get("ok"))
    if action == "bind" and reason in ("missing_bind_code", "missing_argument"):
        suffix = "bind_usage"
    elif action == "bind":
        suffix = "bind_ok" if ok else "bind_err"
    else:
        suffix = "unbind_ok" if ok else "unbind_err"
    return f"notice:{platform}:{message_id}:{suffix}"


def _public_consumed_bind_result(
    action: str,
    result: dict[str, Any],
    *,
    platform: str,
    inbound_message_id: str | None,
) -> dict[str, Any]:
    """Saved bind/unbind result. The bind code is not part of this object."""
    public = {
        key: value
        for key, value in result.items()
        if key not in ("code", "bind_code", "arg")
    }
    public["action"] = action
    if inbound_message_id:
        public["delivery_key"] = _bind_confirmation_delivery_key(
            platform, inbound_message_id, action, public
        )
    return public


async def _record_consumed_bind_result_in_tx(
    cur: Any,
    *,
    platform: str,
    inbound_message_id: str | None,
    result: dict[str, Any],
    action: str,
    dalton_user_id: str | None = None,
    is_group: bool = False,
) -> dict[str, Any]:
    """Save the bind or unbind effect and leave the reply pending.

    Completion is recorded by the gateway only after the fixed delivery key
    is durably sent. This update does not consume the code again on replay.
    It does not change raw_payload, status, or an encrypted context token.
    """
    public = _public_consumed_bind_result(
        action,
        result,
        platform=platform,
        inbound_message_id=inbound_message_id,
    )
    if not inbound_message_id:
        return public
    command_request = {"action": action, "is_group": bool(is_group)}
    await cur.execute(
        """
        UPDATE dalton_channel_inbound_events
        SET consumed_bind_result = %s,
            dalton_user_id = COALESCE(%s, dalton_user_id),
            command_request = %s,
            command_result = %s,
            command_effect_status = 'done',
            command_reply_pending = TRUE,
            updated_at = NOW()
        WHERE platform = %s AND message_id = %s
        """,
        (
            Jsonb(public),
            dalton_user_id,
            Jsonb(command_request),
            Jsonb(public),
            platform,
            inbound_message_id,
        ),
    )
    return public


async def consume_bind_code_or_unbind(
    *,
    action: str,
    platform: str,
    platform_user_id: str | None = None,
    platform_username: str | None = None,
    platform_tenant_key: str | None = None,
    weixin_account_id: str | None = None,
    weixin_context_token: str | None = None,
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    inbound_message_id: str | None = None,
    code: str | None = None,
    is_group: bool = False,
    user_id: str | None = None,
    message_id: str | None = None,
    chat_type: str | None = None,
    chat_address: str | None = None,
    scope_key: str | None = None,
    request_key: str | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """I-2: Consume bind code (with group-leak revocation and cross-platform protection) or unbind."""
    cleaned_plat = str(platform or "").strip().lower()
    cleaned_act = str(action or "bind").strip().lower()
    if cleaned_plat == "weixin":
        if cleaned_act == "bind":
            if gateway_epoch is not None or require_gateway_epoch:
                async with get_db_connection(conn) as owned_conn:
                    async with owned_conn.transaction():
                        async with owned_conn.cursor(row_factory=dict_row) as cur:
                            await wx_db.verify_gateway_epoch_in_tx(
                                cur,
                                gateway_epoch,
                                required=require_gateway_epoch,
                                platform="weixin",
                            )
            return {
                "ok": False,
                "reason": "weixin_uses_qr_login",
                "message": "个人微信已切换为每人扫码连接，不再使用绑定码。",
            }
        if cleaned_act == "unbind":
            cleaned_ct = str(chat_type or "").strip().lower()
            if is_group or cleaned_ct == "group":
                if gateway_epoch is not None or require_gateway_epoch:
                    async with get_db_connection(conn) as owned_conn:
                        async with owned_conn.transaction():
                            async with owned_conn.cursor(row_factory=dict_row) as cur:
                                await wx_db.verify_gateway_epoch_in_tx(
                                    cur,
                                    gateway_epoch,
                                    required=require_gateway_epoch,
                                    platform="weixin",
                                )
                return {
                    "ok": False,
                    "reason": "weixin_group_unsupported",
                    "message": "个人微信不支持在群聊中执行解除连接。",
                }
            # R5 (Decision #2): Weixin command /unbind MUST carry all 4 fields:
            # weixin_account_id, weixin_bot_id, weixin_bot_generation (>= 1), and
            # DM sender platform_user_id. Missing any field returns HTTP 400.
            eff_uid = str(user_id or "").strip() or None
            eff_aid = normalize_uuid(weixin_account_id) if weixin_account_id else None
            eff_bot = str(weixin_bot_id or "").strip() or None
            eff_gen = (
                int(weixin_bot_generation)
                if weixin_bot_generation is not None
                else None
            )
            eff_puid = str(platform_user_id or "").strip() or None
            if (
                not eff_aid
                or not eff_bot
                or eff_gen is None
                or eff_gen < 1
                or not eff_puid
            ):
                if gateway_epoch is not None or require_gateway_epoch:
                    async with get_db_connection(conn) as owned_conn:
                        async with owned_conn.transaction():
                            async with owned_conn.cursor(row_factory=dict_row) as cur:
                                await wx_db.verify_gateway_epoch_in_tx(
                                    cur,
                                    gateway_epoch,
                                    required=require_gateway_epoch,
                                    platform="weixin",
                                )
                raise HTTPException(
                    status_code=400,
                    detail={
                        "code": "weixin_unbind_fields_required",
                        "message": "微信解绑指令缺少必需字段（weixin_account_id / weixin_bot_id / weixin_bot_generation / platform_user_id）。",
                    },
                )
            return await wx_db.unbind_weixin_account(
                target_user_id=eff_uid,
                account_id=eff_aid,
                operator_user_id=eff_uid or "system:weixin-unbind",
                action="member_unbind",
                via_wechat_command=True,
                gateway_epoch=gateway_epoch,
                require_gateway_epoch=require_gateway_epoch,
                expected_bot_id=eff_bot,
                expected_bot_generation=eff_gen,
                expected_sender_ilink_user_id=eff_puid,
                verify_live_roster=True,
                one_time_context_token=weixin_context_token,
                one_time_recipient_id=eff_puid,
                conn=conn,
            )
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
            weixin_bot_id=weixin_bot_id,
            weixin_bot_generation=weixin_bot_generation,
            inbound_message_id=inbound_message_id,
            code=code,
            is_group=is_group,
            user_id=user_id,
            message_id=message_id,
            chat_type=chat_type,
            chat_address=chat_address,
            scope_key=scope_key,
            request_key=request_key,
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
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
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    inbound_message_id: str | None = None,
    code: str | None = None,
    is_group: bool = False,
    user_id: str | None = None,
    message_id: str | None = None,
    chat_type: str | None = None,
    chat_address: str | None = None,
    scope_key: str | None = None,
    request_key: str | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_action = str(action or "bind").strip().lower()
    cleaned_platform = str(platform or "").strip().lower()
    inbound_cleaned = str(inbound_message_id or "").strip() or None
    message_cleaned = str(message_id or "").strip() or None
    cleaned_msg_id = inbound_cleaned or message_cleaned
    cleaned_inbound_mid = cleaned_msg_id
    cleaned_chat_type = (
        str(chat_type or ("group" if is_group else "dm")).strip().lower()
        or ("group" if is_group else "dm")
    )
    cleaned_puid_early = str(platform_user_id or "").strip()
    cleaned_address = str(chat_address or "").strip() or (
        f"{cleaned_platform}:@me"
        if cleaned_chat_type != "group"
        else f"{cleaned_platform}:group"
    )
    cleaned_scope = str(scope_key or "").strip() or (
        f"dm:{cleaned_puid_early}"
        if cleaned_chat_type != "group" and cleaned_puid_early
        else cleaned_address
    )
    req_uuid = normalize_uuid(request_key) or (
        str(uuid5(DALTON_CHANNEL_NAMESPACE, f"{cleaned_platform}:{cleaned_msg_id}"))
        if cleaned_msg_id
        else None
    )
    if user_id is not None:
        reject_company_system_auth(user_id)

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    gateway_epoch,
                    required=require_gateway_epoch,
                    platform=cleaned_platform,
                )
                existing_inbound: dict[str, Any] | None = None
                if cleaned_msg_id:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"dalton_inbound:{cleaned_platform}:{cleaned_msg_id}",),
                    )
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        FOR UPDATE
                        """,
                        (cleaned_platform, cleaned_msg_id),
                    )
                    existing_inbound = await cur.fetchone()
                    if existing_inbound:
                        consumed = existing_inbound.get("consumed_bind_result")
                        stored_result = existing_inbound.get("command_result")
                        replay_source = (
                            consumed
                            if isinstance(consumed, dict) and consumed
                            else stored_result
                            if isinstance(stored_result, dict)
                            else None
                        )
                        if isinstance(replay_source, dict):
                            replay = dict(replay_source)
                            replay["idempotent"] = True
                            replay["duplicate"] = True
                            replay["command_effect_status"] = (
                                existing_inbound.get("command_effect_status") or "done"
                            )
                            replay["command_reply_pending"] = bool(
                                existing_inbound.get("command_reply_pending", False)
                            )
                            return replay

                async def _finish_bind_result(result: dict[str, Any]) -> dict[str, Any]:
                    public = _public_consumed_bind_result(
                        cleaned_action,
                        result,
                        platform=cleaned_platform,
                        inbound_message_id=cleaned_msg_id,
                    )
                    if not cleaned_msg_id:
                        return public
                    dalton_uid = (
                        str(result["user_id"]) if result.get("user_id") else None
                    )
                    if existing_inbound is None:
                        if not req_uuid:
                            return public
                        raw_payload: dict[str, Any] = {}
                        if cleaned_puid_early:
                            raw_payload["platform_user_id"] = cleaned_puid_early
                        raw_payload = _strip_sensitive_from_raw_payload(raw_payload)
                        generation = result.get("generation")
                        await _upsert_command_inbound_row(
                            cur,
                            platform=cleaned_platform,
                            message_id=cleaned_msg_id,
                            request_key=req_uuid,
                            chat_type=cleaned_chat_type,
                            chat_address=cleaned_address,
                            scope_key=cleaned_scope,
                            dalton_user_id=dalton_uid,
                            workspace_id=None,
                            external_thread_id=None,
                            session_generation=(
                                int(generation)
                                if isinstance(generation, int) else None
                            ),
                            thread_id=None,
                            run_id=None,
                            status="completed" if result.get("ok") else "failed",
                            command_request={
                                "action": cleaned_action,
                                "is_group": bool(is_group),
                            },
                            command_result=dict(public),
                            command_effect_status="done",
                            command_reply_pending=True,
                            raw_payload=raw_payload,
                        )
                        plain_token = _plain_weixin_token(weixin_context_token)
                        if plain_token and cleaned_platform == "weixin":
                            enc_key = get_encryption_key()
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET encrypted_weixin_context_token = pgp_sym_encrypt(%s, %s),
                                    weixin_context_bot_id = %s,
                                    weixin_context_bot_generation = %s,
                                    weixin_context_seq = %s,
                                    updated_at = NOW()
                                WHERE platform = %s AND message_id = %s
                                """,
                                (
                                    plain_token,
                                    enc_key,
                                    str(weixin_bot_id or "").strip() or "default",
                                    int(weixin_bot_generation or 1),
                                    0,
                                    cleaned_platform,
                                    cleaned_msg_id,
                                ),
                            )
                    saved = await _record_consumed_bind_result_in_tx(
                        cur,
                        platform=cleaned_platform,
                        inbound_message_id=cleaned_msg_id,
                        result=result,
                        action=cleaned_action,
                        dalton_user_id=dalton_uid,
                        is_group=bool(is_group),
                    )
                    return {
                        **saved,
                        "duplicate": False,
                        "command_effect_status": "done",
                        "command_reply_pending": True,
                    }

                if cleaned_action == "bind":
                    cleaned_code = str(code or "").strip().upper()
                    if not cleaned_code:
                        return await _finish_bind_result(
                            {"ok": False, "reason": "invalid_or_expired"}
                        )
                    code_hash = hash_bind_code(cleaned_code)

                    # 1. Group chat leak: revoke across ALL platforms immediately
                    if is_group:
                        await cur.execute(
                            """
                            SELECT user_id FROM dalton_channel_bind_codes
                            WHERE code_hash = %s
                            FOR UPDATE
                            """,
                            (code_hash,),
                        )
                        grp_code_row = await cur.fetchone()
                        if grp_code_row:
                            reject_company_system_auth(grp_code_row.get("user_id"))
                        await cur.execute(
                            """
                            UPDATE dalton_channel_bind_codes
                            SET used_at = NOW(), revoked_reason = 'sent_in_group'
                            WHERE code_hash = %s AND used_at IS NULL
                            """,
                            (code_hash,),
                        )
                        return await _finish_bind_result(
                            {"ok": False, "reason": "revoked_in_group"}
                        )

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
                    if row:
                        reject_company_system_auth(row.get("user_id"))
                    if (
                        not row
                        or row.get("used_at") is not None
                        or row["expires_at"] <= datetime.now(timezone.utc)
                    ):
                        return await _finish_bind_result(
                            {"ok": False, "reason": "invalid_or_expired"}
                        )

                    row_platform = str(row["platform"]).strip().lower()
                    if row_platform != cleaned_platform:
                        # Do NOT revoke when platform mismatches in DM
                        return await _finish_bind_result(
                            {
                                "ok": False,
                                "reason": "platform_mismatch",
                                "expected_platform": row_platform,
                            }
                        )

                    cleaned_puid = str(platform_user_id or "").strip()
                    if not cleaned_puid:
                        return await _finish_bind_result(
                            {"ok": False, "reason": "missing_platform_user_id"}
                        )

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
                        weixin_bot_id=weixin_bot_id,
                        weixin_bot_generation=weixin_bot_generation,
                        inbound_message_id=cleaned_inbound_mid,
                    )
                    return await _finish_bind_result(
                        {
                            "ok": True,
                            "user_id": target_uid,
                            "platform": cleaned_platform,
                            "platform_user_id": cleaned_puid,
                            "generation": current_gen,
                        }
                    )

                if cleaned_action == "unbind":
                    target_uid = str(user_id or "").strip() or None
                    if target_uid:
                        reject_company_system_auth(target_uid)
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
                        return await _finish_bind_result(
                            {"ok": False, "reason": "not_linked"}
                        )
                    reject_company_system_auth(target_uid)

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

                    sender_puid = cleaned_puid
                    if cleaned_platform == "weixin" and not sender_puid:
                        await cur.execute(
                            """
                            SELECT platform_user_id FROM dalton_channel_identities
                            WHERE user_id = %s AND platform = %s
                            """,
                            (target_uid, cleaned_platform),
                        )
                        pre_id = await cur.fetchone()
                        if pre_id and pre_id.get("platform_user_id"):
                            sender_puid = str(pre_id["platform_user_id"])
                    if cleaned_platform == "weixin" and sender_puid:
                        # Lock the sender before the identity row. The high-water
                        # lives on the token rows deleted below.
                        await _lock_weixin_context_token_rows_in_tx(
                            cur, platform_user_id=sender_puid
                        )

                    await cur.execute(
                        """
                        DELETE FROM dalton_channel_identities
                        WHERE user_id = %s AND platform = %s
                        RETURNING platform_user_id
                        """,
                        (target_uid, cleaned_platform),
                    )
                    deleted_id = await cur.fetchone()
                    eff_deleted_puid = (
                        str(deleted_id["platform_user_id"])
                        if deleted_id and deleted_id.get("platform_user_id")
                        else cleaned_puid
                    )
                    if cleaned_platform == "weixin" and eff_deleted_puid:
                        await cur.execute(
                            """
                            DELETE FROM dalton_channel_weixin_context_tokens
                            WHERE platform_user_id = %s
                            """,
                            (eff_deleted_puid,),
                        )

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
                    return await _finish_bind_result(
                        {
                            "ok": True,
                            "user_id": target_uid,
                            "platform": cleaned_platform,
                            "generations": updated_gens,
                        }
                    )

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


def _sanitize_native_payload_dict(
    raw_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Strip any interaction or bot credentials from persisted raw_payload."""
    if not isinstance(raw_payload, dict):
        return {}
    cleaned = _strip_sensitive_from_raw_payload(raw_payload)
    for forbidden_key in (
        "token",
        "interaction_token",
        "interactionToken",
        "bot_token",
        "botToken",
    ):
        cleaned.pop(forbidden_key, None)
    if isinstance(cleaned.get("discord_interaction"), dict):
        di = _strip_sensitive_from_raw_payload(cleaned["discord_interaction"])
        for forbidden_key in (
            "token",
            "interaction_token",
            "interactionToken",
            "bot_token",
            "botToken",
        ):
            di.pop(forbidden_key, None)
        cleaned["discord_interaction"] = di
    if isinstance(cleaned.get("raw"), dict):
        raw_nested = dict(cleaned["raw"])
        for forbidden_key in (
            "token",
            "interaction_token",
            "interactionToken",
            "bot_token",
            "botToken",
        ):
            raw_nested.pop(forbidden_key, None)
        cleaned["raw"] = raw_nested
    return cleaned


def _normalize_session_cmd(cmd: str) -> str:
    return str(cmd or "").strip().lower()


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
    weixin_account_id: str | None = None,
    weixin_context_token: str | None = None,
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    weixin_context_source: str | None = None,
    weixin_context_seq: int | None = None,
    command_arg: str | None = None,
    workspace_id: str | None = None,
    workspace_query: str | None = None,
    target_kind: str | None = None,
    external_thread_id: str | None = None,
    is_internal_query: bool = False,
    native_interaction: bool = False,
    native_ack_status: str | None = None,
    native_reply_status: str | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """I-3: Atomically execute session commands (`workspace_list`, `workspace_switch`, `new`, `stop`)."""
    cleaned_cmd = _normalize_session_cmd(cmd)
    _ = command_arg
    return await _run_session_command_with_d11(
        cleaned_cmd=cleaned_cmd,
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
        weixin_account_id=weixin_account_id,
        weixin_context_token=weixin_context_token,
        weixin_bot_id=weixin_bot_id,
        weixin_bot_generation=weixin_bot_generation,
        weixin_context_source=weixin_context_source,
        weixin_context_seq=weixin_context_seq,
        workspace_id=workspace_id,
        workspace_query=workspace_query,
        target_kind=target_kind,
        external_thread_id=external_thread_id,
        is_internal_query=is_internal_query,
        native_interaction=native_interaction,
        native_ack_status=native_ack_status,
        native_reply_status=native_reply_status,
        gateway_epoch=gateway_epoch,
        require_gateway_epoch=require_gateway_epoch,
        conn=conn,
    )


class _PendingWeixinVerificationNeeded(Exception):
    """Internal sentinel to roll back fast-path I-3 tx and re-lock via D11 order."""

    def __init__(self, account_id: str) -> None:
        super().__init__(account_id)
        self.account_id = account_id


async def _run_session_command_with_d11(
    *,
    cleaned_cmd: str,
    platform: str,
    dalton_user_id: str | None,
    platform_user_id: str | None,
    chat_type: str,
    chat_address: str | None,
    chat_id: str | None,
    chat_name: str | None,
    scope_key: str,
    message_id: str | None,
    request_key: str | None,
    raw_payload: dict[str, Any] | None,
    weixin_account_id: str | None,
    weixin_context_token: str | None,
    weixin_bot_id: str | None,
    weixin_bot_generation: int | None,
    weixin_context_source: str | None,
    weixin_context_seq: int | None,
    workspace_id: str | None,
    workspace_query: str | None,
    target_kind: str | None,
    external_thread_id: str | None,
    is_internal_query: bool,
    native_interaction: bool,
    native_ack_status: str | None,
    native_reply_status: str | None,
    gateway_epoch: int | None,
    require_gateway_epoch: bool,
    conn: Any,
) -> dict[str, Any]:
    for _attempt in range(2):
        try:
            return await _run_tx_with_retry(
                f"execute_session_command:{cleaned_cmd}",
                conn,
                lambda attempt_conn: _execute_session_command_once(
                    cmd=cleaned_cmd,
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
                    weixin_account_id=weixin_account_id,
                    weixin_context_token=weixin_context_token,
                    weixin_bot_id=weixin_bot_id,
                    weixin_bot_generation=weixin_bot_generation,
                    weixin_context_source=weixin_context_source,
                    weixin_context_seq=weixin_context_seq,
                    workspace_id=workspace_id,
                    workspace_query=workspace_query,
                    target_kind=target_kind,
                    external_thread_id=external_thread_id,
                    is_internal_query=is_internal_query,
                    native_interaction=native_interaction,
                    native_ack_status=native_ack_status,
                    native_reply_status=native_reply_status,
                    gateway_epoch=gateway_epoch,
                    require_gateway_epoch=require_gateway_epoch,
                    conn=attempt_conn,
                ),
            )
        except _PendingWeixinVerificationNeeded as pending_exc:
            if _attempt == 1 or not str(platform_user_id or "").strip():
                return {
                    "ok": False,
                    "linked": False,
                    "allowed": False,
                    "deny_reason": "not_linked",
                }
            verify_res = await wx_db.verify_pending_weixin_account_first_message(
                account_id=pending_exc.account_id,
                platform_user_id=str(platform_user_id or "").strip(),
                expected_bot_id=(
                    str(weixin_bot_id).strip()
                    if weixin_bot_id and str(weixin_bot_id).strip()
                    else None
                ),
                expected_bot_generation=(
                    int(weixin_bot_generation)
                    if weixin_bot_generation is not None
                    else None
                ),
                gateway_epoch=gateway_epoch,
                require_gateway_epoch=require_gateway_epoch,
                inbound_context_token=_plain_weixin_token(weixin_context_token),
                conn=conn,
            )
            if verify_res.get("outcome") == "conflict_unbound":
                out: dict[str, Any] = {
                    "ok": False,
                    "linked": False,
                    "allowed": False,
                    "deny_reason": "ilink_user_conflict_auto_unbound",
                }
                if verify_res.get("one_time_weixin_reply"):
                    out["one_time_weixin_reply"] = verify_res["one_time_weixin_reply"]
                return out
            if verify_res.get("outcome") == "denied":
                return {
                    "ok": False,
                    "linked": True,
                    "allowed": False,
                    "deny_reason": verify_res.get("deny_reason") or "weixin_channel_disabled",
                }
            if verify_res.get("outcome") == "stale_generation":
                return {
                    "ok": False,
                    "linked": False,
                    "allowed": False,
                    "deny_reason": "weixin_bot_generation_superseded",
                }
            continue
    return {"ok": False, "linked": False, "allowed": False, "deny_reason": "not_linked"}


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
            Jsonb(
                _sanitize_native_payload_dict(
                    _strip_sensitive_from_raw_payload(raw_payload)
                )
            ),
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
    weixin_account_id: str | None = None,
    weixin_context_token: str | None = None,
    weixin_bot_id: str | None = None,
    weixin_bot_generation: int | None = None,
    weixin_context_source: str | None = None,
    weixin_context_seq: int | None = None,
    workspace_id: str | None = None,
    workspace_query: str | None = None,
    target_kind: str | None = None,
    external_thread_id: str | None = None,
    is_internal_query: bool = False,
    native_interaction: bool = False,
    native_ack_status: str | None = None,
    native_reply_status: str | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_cmd = str(cmd or "").strip().lower()
    if cleaned_cmd not in ("workspace_list", "workspace_switch", "new", "stop"):
        raise HTTPException(
            status_code=400, detail=f"Unsupported session command: {cmd!r}"
        )

    cleaned_platform = str(platform or "").strip().lower()
    if dalton_user_id is not None:
        reject_company_system_auth(dalton_user_id)
    cleaned_uid = str(dalton_user_id or "").strip()
    cleaned_puid = str(platform_user_id or "").strip()
    req_puid = cleaned_puid
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
    payload_dict = _sanitize_native_payload_dict(
        _strip_sensitive_from_raw_payload(dict(raw_payload or {}))
    )
    if cleaned_puid:
        payload_dict["platform_user_id"] = cleaned_puid
    context_stamp: tuple[str | None, str | None, str, str, int, int | None] | None = None

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
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    gateway_epoch,
                    required=require_gateway_epoch,
                    platform=cleaned_platform,
                )
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
                        reject_company_system_auth(orig_uid)
                        if isinstance(existing_inbound.get("raw_payload"), dict):
                            existing_raw = _sanitize_native_payload_dict(
                                _strip_sensitive_from_raw_payload(
                                    existing_inbound["raw_payload"]
                                )
                            )
                            orig_puid = str(
                                existing_raw.get("platform_user_id") or ""
                            ).strip()
                            payload_dict = {**existing_raw, **payload_dict}
                            if not cleaned_puid and orig_puid and not cleaned_uid:
                                cleaned_puid = orig_puid
                                payload_dict["platform_user_id"] = cleaned_puid

                is_native = bool(
                    native_interaction
                    or payload_dict.get("native_interaction") is True
                    or isinstance(payload_dict.get("discord_interaction"), dict)
                )
                eff_ack_status = (
                    str(
                        native_ack_status
                        or payload_dict.get("native_ack_status")
                        or ""
                    )
                    .strip()
                    .lower()
                    or None
                )
                eff_reply_status = (
                    str(
                        native_reply_status
                        or payload_dict.get("native_reply_status")
                        or ""
                    )
                    .strip()
                    .lower()
                    or None
                )
                if is_native:
                    payload_dict["native_interaction"] = True
                    eff_ack_status = eff_ack_status or "acknowledged"
                    payload_dict["native_ack_status"] = eff_ack_status

                if (
                    existing_inbound is not None
                    and isinstance(existing_inbound.get("command_result"), dict)
                    and existing_inbound["command_result"].get("not_executed") is True
                ):
                    stored_unexec = dict(existing_inbound["command_result"])
                    return {
                        **stored_unexec,
                        "duplicate": True,
                        "command_effect_status": "done",
                        "command_reply_pending": False,
                        "existing_status": existing_inbound.get("status") or "failed",
                        **(
                            {
                                "native_interaction": True,
                                "native_ack_status": payload_dict.get(
                                    "native_ack_status"
                                )
                                or "unknown",
                                "native_reply_status": payload_dict.get(
                                    "native_reply_status"
                                )
                                or "not_applicable",
                            }
                            if is_native
                            else {}
                        ),
                    }

                if (
                    is_native
                    and eff_ack_status in ("unknown", "failed")
                    and existing_inbound is None
                ):
                    resolved_unexec_uid: str | None = cleaned_uid or None
                    if not resolved_unexec_uid and cleaned_puid:
                        await cur.execute(
                            """
                            SELECT user_id FROM dalton_channel_identities
                            WHERE platform = %s AND platform_user_id = %s
                            """,
                            (cleaned_platform, cleaned_puid),
                        )
                        id_row = await cur.fetchone()
                        if id_row and id_row.get("user_id"):
                            candidate_uid = str(id_row["user_id"]).strip()
                            reject_company_system_auth(candidate_uid)
                            resolved_unexec_uid = candidate_uid
                    unexec_reason = (
                        "interaction_ack_unknown"
                        if eff_ack_status == "unknown"
                        else "interaction_ack_failed"
                    )
                    payload_dict["native_reply_status"] = "not_applicable"
                    unexec_res: dict[str, Any] = {
                        "ok": False,
                        "linked": bool(resolved_unexec_uid),
                        "allowed": False,
                        "deny_reason": unexec_reason,
                        "not_executed": True,
                    }
                    if resolved_unexec_uid:
                        unexec_res["dalton_user_id"] = resolved_unexec_uid
                    if cleaned_msg_id:
                        await _upsert_command_inbound_row(
                            cur,
                            platform=cleaned_platform,
                            message_id=cleaned_msg_id,
                            request_key=req_uuid,
                            chat_type=cleaned_chat_type,
                            chat_address=cleaned_address,
                            scope_key=cleaned_scope,
                            dalton_user_id=resolved_unexec_uid,
                            workspace_id=None,
                            external_thread_id=None,
                            session_generation=None,
                            thread_id=None,
                            run_id=None,
                            status="failed",
                            command_request=cmd_req_dict,
                            command_result=unexec_res,
                            command_effect_status="done",
                            command_reply_pending=False,
                            raw_payload=payload_dict,
                        )
                    return {
                        **unexec_res,
                        "duplicate": False,
                        "command_effect_status": "done",
                        "command_reply_pending": False,
                        "native_interaction": True,
                        "native_ack_status": eff_ack_status,
                        "native_reply_status": "not_applicable",
                    }

                async def _deny_and_ignore_command(
                    res_payload: dict[str, Any],
                    *,
                    uid_val: str | None,
                    reply_pending: bool = False,
                ) -> dict[str, Any]:
                    if is_native and cleaned_msg_id and not is_internal_query:
                        reply_pending = (
                            bool(existing_inbound.get("command_reply_pending", False))
                            if existing_inbound is not None
                            else (eff_ack_status == "acknowledged")
                        )
                        payload_dict["native_reply_status"] = (
                            eff_reply_status
                            or (
                                payload_dict.get("native_reply_status")
                                if existing_inbound is not None
                                else None
                            )
                            or ("pending" if reply_pending else "not_applicable")
                        )
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
                            command_reply_pending=reply_pending,
                            raw_payload=payload_dict,
                        )
                        return {
                            **res_payload,
                            "duplicate": existing_inbound is not None,
                            "command_effect_status": "done",
                            "command_reply_pending": reply_pending,
                            "native_interaction": True,
                            "native_ack_status": eff_ack_status or "acknowledged",
                            "native_reply_status": payload_dict["native_reply_status"],
                        }
                    # R9 (Decision #4): Never overwrite an existing (platform, message_id) row
                    # when the incoming request's sender mismatches or omits platform_user_id on
                    # a row already bound to a verified sender.
                    sender_mismatch = bool(
                        existing_inbound is not None
                        and (
                            (orig_puid and req_puid and orig_puid != req_puid)
                            or (orig_puid and not req_puid and bool(cleaned_uid))
                            or (orig_uid and cleaned_uid and orig_uid != cleaned_uid)
                            or (orig_uid and uid_val is not None and orig_uid != uid_val)
                        )
                    )
                    if cleaned_msg_id and not sender_mismatch:
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
                            command_reply_pending=bool(reply_pending),
                            raw_payload=payload_dict,
                        )
                    if reply_pending:
                        return {**res_payload, "command_reply_pending": True}
                    return res_payload

                def _private_weixin_command() -> bool:
                    return (
                        cleaned_platform == "weixin"
                        and cleaned_chat_type != "group"
                        and bool(cleaned_msg_id)
                        and bool(cleaned_puid)
                        and not _payload_marks_invalid_inbound(payload_dict)
                        and not str(payload_dict.get("group_id") or "").strip()
                        and not cleaned_scope.startswith("chat:")
                        and not cleaned_address.startswith("weixin:group:")
                    )

                async def _save_unbound_private_weixin_context() -> bool:
                    """In multi-account mode, unbound Weixin senders have no active account."""
                    return False

                # R9 (Decision #4): Check sender mismatch BEFORE ANY branch (including
                # invalid_inbound_event) that could mutate an existing (platform, message_id) row!
                if (orig_puid and req_puid and orig_puid != req_puid) or (
                    orig_uid and cleaned_uid and orig_uid != cleaned_uid
                ):
                    return {
                        "ok": False,
                        "linked": False,
                        "allowed": False,
                        "deny_reason": "not_linked",
                    }

                if (
                    cleaned_platform == "weixin"
                    and _payload_marks_invalid_inbound(payload_dict)
                ):
                    if existing_inbound is not None:
                        return {
                            "ok": False,
                            "linked": False,
                            "allowed": False,
                            "deny_reason": "invalid_inbound_event",
                        }
                    return await _deny_and_ignore_command(
                        {
                            "ok": False,
                            "linked": False,
                            "allowed": False,
                            "deny_reason": "invalid_inbound_event",
                        },
                        uid_val=None,
                    )

                wx_acc_row: dict[str, Any] | None = None
                pending_wx_account: dict[str, Any] | None = None
                is_readonly_query = (
                    cleaned_cmd == "workspace_list"
                    and bool(is_internal_query)
                    and not cleaned_msg_id
                )
                # R5 (Decision #1): Reject Weixin group commands BEFORE any account lookup
                # or first-message verification.
                if cleaned_platform == "weixin" and (
                    cleaned_chat_type == "group"
                    or bool(str(chat_id or "").strip())
                    or bool(str(payload_dict.get("group_id") or "").strip())
                    or cleaned_address.startswith("weixin:group:")
                    or cleaned_scope.startswith("chat:")
                ):
                    if existing_inbound is not None:
                        return {
                            "ok": False,
                            "linked": False,
                            "allowed": False,
                            "deny_reason": "weixin_group_unsupported",
                        }
                    return await _deny_and_ignore_command(
                        {
                            "ok": False,
                            "linked": False,
                            "allowed": False,
                            "deny_reason": "weixin_group_unsupported",
                        },
                        uid_val=None,
                    )

                eff_cmd_wx_bot = str(weixin_bot_id or "").strip() or None
                eff_cmd_wx_gen = (
                    int(weixin_bot_generation)
                    if weixin_bot_generation is not None
                    else None
                )

                if cleaned_puid:
                    if cleaned_platform == "weixin":
                        norm_wx_aid = normalize_uuid(weixin_account_id) if weixin_account_id else None
                        if norm_wx_aid:
                            await cur.execute(
                                "SELECT account_id::text AS account_id, user_id, ilink_user_id AS platform_user_id, ilink_bot_id, bot_generation, status FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid FOR NO KEY UPDATE",
                                (norm_wx_aid,),
                            )
                            raw_wx = await cur.fetchone()
                        else:
                            await cur.execute(
                                "SELECT account_id::text AS account_id, user_id, ilink_user_id AS platform_user_id, ilink_bot_id, bot_generation, status FROM dalton_channel_weixin_accounts WHERE ilink_user_id = %s AND status = 'active' FOR NO KEY UPDATE",
                                (cleaned_puid,),
                            )
                            raw_wx = await cur.fetchone()
                        wx_acc_row = dict(raw_wx) if raw_wx else None
                        if wx_acc_row is not None:
                            wx_st = str(wx_acc_row.get("status") or "")
                            wx_bot = str(wx_acc_row.get("ilink_bot_id") or "")
                            wx_gen = int(wx_acc_row.get("bot_generation") or 0)
                            if (eff_cmd_wx_bot and eff_cmd_wx_bot != wx_bot) or (
                                eff_cmd_wx_gen is not None and eff_cmd_wx_gen != wx_gen
                            ):
                                bot_deny = {
                                    "ok": False,
                                    "linked": True,
                                    "allowed": False,
                                    "deny_reason": (
                                        "weixin_bot_generation_superseded"
                                        if wx_st == "pending_verification"
                                        else "weixin_bot_generation_mismatch"
                                    ),
                                }
                                if existing_inbound is not None:
                                    return bot_deny
                                return await _deny_and_ignore_command(
                                    bot_deny,
                                    uid_val=None,
                                )
                            if wx_st == "pending_verification":
                                # Defer D11 first-message verification until AFTER all
                                # authorization (roster, standing, switches) succeeds,
                                # and NEVER verify on read-only internal queries!
                                pending_wx_account = wx_acc_row
                                identity = wx_acc_row
                            elif wx_st == "active":
                                if (
                                    wx_acc_row.get("platform_user_id")
                                    and str(wx_acc_row["platform_user_id"]) != cleaned_puid
                                ):
                                    sender_deny = {
                                        "ok": False,
                                        "linked": False,
                                        "allowed": False,
                                        "deny_reason": "weixin_sender_mismatch",
                                    }
                                    if existing_inbound is not None:
                                        return sender_deny
                                    return await _deny_and_ignore_command(
                                        sender_deny,
                                        uid_val=None,
                                    )
                                identity = wx_acc_row
                            else:
                                st_deny = {
                                    "ok": False,
                                    "linked": True,
                                    "allowed": False,
                                    "deny_reason": f"weixin_account_{wx_st}",
                                }
                                if existing_inbound is not None:
                                    return st_deny
                                return await _deny_and_ignore_command(
                                    st_deny,
                                    uid_val=None,
                                )
                        else:
                            identity = None
                    else:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_identities
                            WHERE platform = %s AND platform_user_id = %s
                            """,
                            (cleaned_platform, cleaned_puid),
                        )
                        identity = await cur.fetchone()
                    if not identity:
                        stored_inbound = existing_inbound
                        stored_res = (
                            stored_inbound.get("command_result")
                            if stored_inbound is not None
                            else None
                        )
                        # Replay saved denials without changing token order or
                        # reply state. Newly linked senders use the identity path.
                        if (
                            stored_inbound is not None
                            and isinstance(stored_res, dict)
                            and stored_res.get("ok") is False
                            and stored_res.get("deny_reason") == "not_linked"
                        ):
                            dup_reply_pending = bool(
                                stored_inbound.get("command_reply_pending", False)
                            )
                            return {
                                **dict(stored_res),
                                "duplicate": True,
                                "command_effect_status": stored_inbound.get(
                                    "command_effect_status"
                                )
                                or "done",
                                "command_reply_pending": dup_reply_pending,
                                "existing_status": stored_inbound.get("status"),
                                **(
                                    {
                                        "native_interaction": True,
                                        "native_ack_status": payload_dict.get(
                                            "native_ack_status"
                                        )
                                        or "acknowledged",
                                        "native_reply_status": payload_dict.get(
                                            "native_reply_status"
                                        )
                                        or (
                                            "pending"
                                            if dup_reply_pending
                                            else "sent"
                                        ),
                                    }
                                    if is_native
                                    else {}
                                ),
                            }
                        if await _save_unbound_private_weixin_context():
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                                "command_reply_pending": True,
                            }
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
                    reject_company_system_auth(resolved_identity_uid)
                    if (orig_uid and orig_uid != resolved_identity_uid) or (
                        cleaned_uid and cleaned_uid != resolved_identity_uid
                    ):
                        # Same platform_user_id rebound to a different Dalton user -> retire
                        # pending reply row so redelivery never leaks old owner's workspaces.
                        # (Different platform_user_id collision was already rejected at L5541
                        # without modifying the original member's row.)
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
                elif cleaned_msg_id or existing_inbound is not None:
                    check_uid = cleaned_uid or orig_uid
                    reject_company_system_auth(check_uid)
                    if not check_uid or (
                        orig_uid and cleaned_uid and orig_uid != cleaned_uid
                    ):
                        if existing_inbound is not None:
                            return {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            }
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            },
                            uid_val=None,
                        )
                    if cleaned_platform == "weixin":
                        await cur.execute(
                            """
                            SELECT account_id::text AS account_id, user_id,
                                   ilink_user_id AS platform_user_id,
                                   ilink_bot_id, bot_generation, status
                            FROM dalton_channel_weixin_accounts
                            WHERE user_id = %s AND status = 'active'
                            """,
                            (check_uid,),
                        )
                        identity = await cur.fetchone()
                    else:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_identities
                            WHERE user_id = %s AND platform = %s
                            """,
                            (check_uid, cleaned_platform),
                        )
                        identity = await cur.fetchone()
                    if identity:
                        reject_company_system_auth(identity.get("user_id"))
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
                        return {
                            "ok": False,
                            "linked": False,
                            "allowed": False,
                            "deny_reason": "not_linked",
                        }
                    cleaned_uid = check_uid

                if not cleaned_uid:
                    return {
                        "ok": False,
                        "linked": False,
                        "allowed": False,
                        "deny_reason": "not_linked",
                    }
                reject_company_system_auth(cleaned_uid)
                # DALTON-HOOK(dalton-notify): deny suspended accounts before reading user data or mutating channel session state
                from src.server.services.account import read_standing

                if (
                    await read_standing(cleaned_uid, unknown_on_cache_error=True)
                ).suspended:
                    return await _deny_and_ignore_command(
                        {
                            "ok": False,
                            "linked": False,
                            "allowed": False,
                            "deny_reason": "not_linked",
                        },
                        uid_val=None,
                    )

                # 2. Tier & group chat check
                user_tier = await get_user_tier(cleaned_uid, conn=owned_conn)
                if cleaned_platform == "weixin":
                    roster_status = await check_team_member_status_live(
                        cleaned_uid, conn=owned_conn
                    )
                    if roster_status == "lookup_error":
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "roster_lookup_error",
                                "message": "Unable to verify team membership right now.",
                            },
                        )
                    if roster_status == "removed":
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": True,
                                "allowed": False,
                                "dalton_user_id": cleaned_uid,
                                "user_tier": user_tier,
                                "deny_reason": "member_not_in_roster",
                            },
                            uid_val=cleaned_uid,
                        )
                    await cur.execute(
                        "SELECT enabled, allow_external FROM dalton_channel_weixin_config WHERE singleton_id = 1"
                    )
                    cfg_wx = await cur.fetchone()
                    ch_enabled = bool(cfg_wx["enabled"]) if cfg_wx is not None else True
                    db_allow_ext = cfg_wx["allow_external"] if cfg_wx is not None else None
                    access_ok, access_deny = evaluate_weixin_member_access(
                        user_tier,
                        channel_enabled=ch_enabled,
                        db_allow_external=db_allow_ext,
                    )
                    if not access_ok:
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": True,
                                "allowed": False,
                                "dalton_user_id": cleaned_uid,
                                "user_tier": user_tier,
                                "deny_reason": access_deny or "weixin_channel_disabled",
                            },
                            uid_val=cleaned_uid,
                        )
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
                            "deny_reason": (
                                "weixin_group_unsupported"
                                if cleaned_platform == "weixin"
                                else "external_member_group_forbidden"
                            ),
                        },
                        uid_val=cleaned_uid,
                    )

                # R5 (Decision #1): Only trigger D11 first-message verification AFTER all
                # authorization (account, bot/gen, DM/group, standing, live roster, switches)
                # has succeeded, and NEVER on read-only internal queries (e.g. workspace_list).
                if pending_wx_account is not None:
                    if is_readonly_query:
                        return {
                            "ok": False,
                            "linked": True,
                            "allowed": False,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "deny_reason": "weixin_account_pending_verification",
                        }
                    raise _PendingWeixinVerificationNeeded(
                        account_id=str(pending_wx_account["account_id"])
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
                                ok, perm_deny, ws_row = (
                                    await _verify_workspace_write_permission(
                                        cur,
                                        str(target_ws_id),
                                        cleaned_uid,
                                        conn=owned_conn,
                                    )
                                )
                                if not ok:
                                    was_reply_pending = bool(
                                        existing_inbound.get(
                                            "command_reply_pending", False
                                        )
                                    )
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
                                        "command_reply_pending": (
                                            was_reply_pending if is_native else False
                                        ),
                                        "existing_status": "ignored",
                                    }
                                if ws_row and ws_row.get("name"):
                                    stored_res["workspace_name"] = str(ws_row["name"])

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
                        dup_reply_pending = bool(
                            existing_inbound.get("command_reply_pending", False)
                        )
                        return {
                            **stored_res,
                            "dalton_user_id": cleaned_uid,
                            "user_tier": user_tier,
                            "duplicate": True,
                            "command_effect_status": eff_stored_status,
                            "command_reply_pending": dup_reply_pending,
                            "existing_status": existing_inbound.get("status"),
                            **(
                                {
                                    "native_interaction": True,
                                    "native_ack_status": payload_dict.get(
                                        "native_ack_status"
                                    )
                                    or "acknowledged",
                                    "native_reply_status": payload_dict.get(
                                        "native_reply_status"
                                    )
                                    or ("pending" if dup_reply_pending else "sent"),
                                }
                                if is_native
                                else {}
                            ),
                        }

                    # A stored denial or other non-success is final for this message_id.
                    # Resending it restores an unsent reply and does not run the command again,
                    # so a later rename or workspace switch cannot turn the old event into a new selection.
                    # Re-filter any stored ambiguous candidates against current write permissions and
                    # live workspace names so revoked workspaces are never disclosed on replay.
                    stored_res.pop("workspace_name", None)
                    stored_res.pop("workspaces", None)
                    raw_candidates = stored_res.get("candidates")
                    if isinstance(raw_candidates, list):
                        writable_names = await _get_writable_workspace_names(
                            cleaned_uid
                        )
                        visible_candidates: list[dict[str, Any]] = []
                        for cand in raw_candidates:
                            if not isinstance(cand, dict):
                                continue
                            c_wid = normalize_uuid(cand.get("workspace_id"))
                            if not c_wid:
                                continue
                            ok_cand, _, cand_row = (
                                await _verify_workspace_write_permission(
                                    cur,
                                    c_wid,
                                    cleaned_uid,
                                    conn=owned_conn,
                                )
                            )
                            if not ok_cand or cand_row is None:
                                continue
                            live_name = writable_names.get(c_wid) or str(
                                cand_row.get("name") or cand.get("name") or c_wid
                            )
                            visible_candidates.append(
                                {
                                    "workspace_id": c_wid,
                                    "name": live_name,
                                }
                            )
                        stored_res["candidates"] = visible_candidates
                        if (
                            not visible_candidates
                            and stored_res.get("deny_reason") == "workspace_ambiguous"
                        ):
                            stored_res["deny_reason"] = "workspace_write_denied"
                        if cleaned_msg_id:
                            await cur.execute(
                                """
                                UPDATE dalton_channel_inbound_events
                                SET command_result = %s,
                                    updated_at = NOW()
                                WHERE platform = %s AND message_id = %s
                                """,
                                (Jsonb(stored_res), cleaned_platform, cleaned_msg_id),
                            )

                    dup_reply_pending = bool(
                        existing_inbound.get("command_reply_pending", False)
                    )
                    return {
                        **stored_res,
                        "dalton_user_id": cleaned_uid,
                        "user_tier": user_tier,
                        "duplicate": True,
                        "command_effect_status": existing_inbound.get(
                            "command_effect_status"
                        )
                        or "done",
                        "command_reply_pending": dup_reply_pending,
                        "existing_status": existing_inbound.get("status"),
                        **(
                            {
                                "native_interaction": True,
                                "native_ack_status": payload_dict.get(
                                    "native_ack_status"
                                )
                                or "acknowledged",
                                "native_reply_status": payload_dict.get(
                                    "native_reply_status"
                                )
                                or ("pending" if dup_reply_pending else "sent"),
                            }
                            if is_native
                            else {}
                        ),
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
                    if (
                        eff_override_ws is not None
                        and eff_override_ws not in writable_names
                        and normalize_uuid(eff_override_ws) != normalize_uuid(home_ws_id)
                    ):
                        ok, _, ws_row = await _verify_workspace_write_permission(
                            cur, eff_override_ws, cleaned_uid, conn=owned_conn
                        )
                        if not ok or ws_row is None:
                            eff_override_ws = None
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
                        "command_effect_status": "done",
                        "command_reply_pending": False,
                        "workspaces": [
                            {"workspace_id": wid, "name": name}
                            for wid, name in writable_names.items()
                        ],
                    }

                async def _save_weixin_command_context() -> dict[str, Any] | None:
                    """Store Weixin command context token into multi-account `dalton_channel_weixin_account_tokens`."""
                    nonlocal context_stamp
                    if cleaned_platform != "weixin" or not cleaned_msg_id:
                        return None
                    plain_token = _plain_weixin_token(weixin_context_token)
                    req_bot = str(weixin_bot_id or "").strip() or None
                    req_gen = (
                        int(weixin_bot_generation)
                        if weixin_bot_generation is not None
                        else None
                    )
                    if plain_token is None and req_bot is None and req_gen is None:
                        return None
                    norm_wx_aid = normalize_uuid(weixin_account_id) if weixin_account_id else None
                    if norm_wx_aid:
                        await cur.execute(
                            """
                            SELECT account_id::text AS account_id, ilink_bot_id, bot_generation,
                                   ilink_user_id, status
                            FROM dalton_channel_weixin_accounts
                            WHERE account_id = %s::uuid
                            FOR NO KEY UPDATE
                            """,
                            (norm_wx_aid,),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT account_id::text AS account_id, ilink_bot_id, bot_generation,
                                   ilink_user_id, status
                            FROM dalton_channel_weixin_accounts
                            WHERE user_id = %s
                            FOR NO KEY UPDATE
                            """,
                            (cleaned_uid,),
                        )
                    acc_row = await cur.fetchone()
                    active_bot_id = str(acc_row["ilink_bot_id"]) if acc_row else None
                    active_bot_gen = int(acc_row["bot_generation"]) if acc_row else 1
                    bot_mismatch = (
                        not acc_row
                        or str(acc_row.get("status") or "") != "active"
                        or (req_bot is not None and req_bot != active_bot_id)
                        or (
                            req_gen is not None
                            and int(req_gen) != int(active_bot_gen)
                        )
                    )
                    if bot_mismatch:
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": True,
                                "allowed": False,
                                "dalton_user_id": cleaned_uid,
                                "user_tier": user_tier,
                                "deny_reason": (
                                    "weixin_bot_unavailable"
                                    if not active_bot_id
                                    else "weixin_bot_generation_mismatch"
                                ),
                            },
                            uid_val=cleaned_uid,
                        )
                    if not plain_token:
                        context_stamp = (
                            None,
                            None,
                            str(acc_row["account_id"]),
                            str(active_bot_id),
                            int(active_bot_gen),
                            None,
                        )
                        return None
                    sender = cleaned_puid or str(acc_row.get("ilink_user_id") or "").strip()
                    if not sender:
                        return await _deny_and_ignore_command(
                            {
                                "ok": False,
                                "linked": False,
                                "allowed": False,
                                "deny_reason": "not_linked",
                            },
                            uid_val=cleaned_uid,
                        )
                    order = _extract_weixin_context_order(
                        payload_dict, weixin_context_seq
                    )
                    requested_source = _context_order_source(weixin_context_source)
                    requested_value = _positive_context_seq(weixin_context_seq)
                    if requested_source == "receive":
                        order = None
                    elif (
                        requested_source in ("protocol", "platform_time")
                        and requested_value is not None
                    ):
                        order = (requested_source, requested_value)
                    enc_key = get_encryption_key()
                    _accepted, stored_seq, _ver = (
                        await wx_db.upsert_weixin_account_context_token_in_tx(
                            cur,
                            account_id=str(acc_row["account_id"]),
                            ilink_bot_id=str(active_bot_id),
                            bot_generation=int(active_bot_gen),
                            platform_user_id=sender,
                            context_token=plain_token,
                            incoming_seq=order[1] if order is not None else None,
                            incoming_source=order[0] if order is not None else None,
                            enc_key=enc_key,
                        )
                    )
                    context_stamp = (
                        plain_token,
                        enc_key,
                        str(acc_row["account_id"]),
                        str(active_bot_id),
                        int(active_bot_gen),
                        int(stored_seq),
                    )
                    return None

                context_denial = await _save_weixin_command_context()
                if context_denial is not None:
                    return context_denial

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

                    current_ws_name = CHIEF_OF_STAFF_DISPLAY_NAME
                    if override_ws_id is not None:
                        current_ws_name = writable_names.get(override_ws_id) or ""
                        if not current_ws_name:
                            ok, _, w_row = await _verify_workspace_write_permission(
                                cur, override_ws_id, cleaned_uid, conn=owned_conn
                            )
                            if not ok or w_row is None:
                                override_ws_id = None
                                generation += 1
                                pending_interrupt = None
                                current_ws_name = CHIEF_OF_STAFF_DISPLAY_NAME
                            else:
                                current_ws_name = (
                                    str(w_row["name"])
                                    if w_row and w_row.get("name")
                                    else override_ws_id
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
                    current_ws_id = override_ws_id or home_ws_id
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

                if is_native:
                    payload_dict["native_reply_status"] = (
                        eff_reply_status
                        or ("pending" if cleaned_msg_id else "not_applicable")
                    )

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
                        raw_payload=_sanitize_native_payload_dict(
                            _strip_sensitive_from_raw_payload(payload_dict)
                        ),
                    )
                    if context_stamp is not None:
                        (
                            _stamp_token,
                            _stamp_key,
                            stamp_aid,
                            stamp_bot,
                            stamp_gen,
                            stamp_seq,
                        ) = context_stamp
                        # R7 (Decision #7): Never duplicate context_token into the legacy
                        # dalton_channel_inbound_events.encrypted_weixin_context_token column;
                        # token ciphertext lives exclusively in dalton_channel_weixin_account_tokens.
                        await cur.execute(
                            """
                            UPDATE dalton_channel_inbound_events
                            SET weixin_account_id = %s::uuid,
                                weixin_context_bot_id = %s,
                                weixin_context_bot_generation = %s,
                                weixin_context_seq = COALESCE(%s, weixin_context_seq),
                                updated_at = NOW()
                            WHERE platform = %s AND message_id = %s
                            """,
                            (
                                stamp_aid,
                                stamp_bot,
                                stamp_gen,
                                stamp_seq,
                                cleaned_platform,
                                cleaned_msg_id,
                            ),
                        )

                return {
                    **cmd_result,
                    "duplicate": False,
                    "command_effect_status": effect_status,
                    "command_reply_pending": bool(cleaned_msg_id),
                    **(
                        {
                            "native_interaction": True,
                            "native_ack_status": payload_dict.get("native_ack_status")
                            or "acknowledged",
                            "native_reply_status": payload_dict.get(
                                "native_reply_status"
                            )
                            or ("pending" if cleaned_msg_id else "not_applicable"),
                        }
                        if is_native
                        else {}
                    ),
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
    include_secrets: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """I-4: Return user tier, home_workspace_id, identities, settings, chat_bindings, and current conversation route."""
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        raise HTTPException(status_code=400, detail="user_id is required")
    reject_company_system_auth(cleaned_uid)

    user_tier = await get_user_tier(cleaned_uid)
    home_ws_id = get_home_workspace_id(cleaned_uid)

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.cursor(row_factory=dict_row) as cur:
            weixin_cap_on = has_capability(CapabilityId.CHANNEL_WEIXIN)
            await cur.execute(
                """
                SELECT platform, platform_user_id, platform_username,
                       platform_tenant_key, created_at, updated_at
                FROM dalton_channel_identities
                WHERE user_id = %s AND platform <> 'weixin'
                """,
                (cleaned_uid,),
            )
            identities: dict[str, dict[str, Any]] = {}
            for row in await cur.fetchall():
                p = str(row["platform"])
                item_dict: dict[str, Any] = {
                    "platform": p,
                    "platform_user_id": row["platform_user_id"],
                    "platform_username": row.get("platform_username"),
                    "platform_tenant_key": row.get("platform_tenant_key"),
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
                if include_secrets:
                    item_dict["weixin_context_token"] = None
                identities[p] = item_dict

            # Multi-account Weixin identity projected from dalton_channel_weixin_accounts (§3.1 (2), D10)
            if weixin_cap_on:
                await cur.execute(
                    """
                    SELECT account_id::text AS account_id,
                           ilink_bot_id,
                           ilink_user_id,
                           ilink_username,
                           bot_generation,
                           status,
                           created_at,
                           updated_at
                    FROM dalton_channel_weixin_accounts
                    WHERE user_id = %s
                      AND status = 'active'
                      AND ilink_user_id IS NOT NULL
                    """,
                    (cleaned_uid,),
                )
                wx_row = await cur.fetchone()
                if wx_row:
                    wx_tok: str | None = None
                    if include_secrets:
                        wx_tok, _, _ = (
                            await wx_db.resolve_weixin_account_context_token_in_tx(
                                cur,
                                account_id=str(wx_row["account_id"]),
                                bot_generation=int(wx_row["bot_generation"]),
                                platform_user_id=str(wx_row["ilink_user_id"]),
                            )
                        )
                    wx_item: dict[str, Any] = {
                        "platform": "weixin",
                        "account_id": str(wx_row["account_id"]),
                        "ilink_bot_id": str(wx_row["ilink_bot_id"]),
                        "bot_generation": int(wx_row["bot_generation"]),
                        "platform_user_id": str(wx_row["ilink_user_id"]),
                        "platform_username": wx_row.get("ilink_username"),
                        "platform_tenant_key": None,
                        "created_at": (
                            wx_row["created_at"].isoformat()
                            if hasattr(wx_row.get("created_at"), "isoformat")
                            else wx_row.get("created_at")
                        ),
                        "updated_at": (
                            wx_row["updated_at"].isoformat()
                            if hasattr(wx_row.get("updated_at"), "isoformat")
                            else wx_row.get("updated_at")
                        ),
                    }
                    if include_secrets:
                        wx_item["weixin_context_token"] = wx_tok
                    identities["weixin"] = wx_item

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
            if not weixin_cap_on:
                settings_apps.pop("weixin", None)

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
                    SELECT platform, chat_address, thread_scope_key, thread_id, run_id, external_thread_id
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
                        "thread_scope_key": in_match.get("thread_scope_key"),
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
                        bg_scope_key = await _resolve_thread_scope_key_for_thread(
                            cur,
                            str(bg_match["thread_id"]),
                            target_address=str(bg_match["target_address"]),
                        )
                        current_conversation = {
                            "address": bg_match["target_address"],
                            "platform": bg_match["platform"],
                            "thread_scope_key": bg_scope_key,
                            "thread_id": str(bg_match["thread_id"]),
                            "run_id": bg_match["run_id"],
                            "external_thread_id": None,
                        }
                    elif norm_tid:
                        await cur.execute(
                            """
                            SELECT dalton_user_id, platform, target_address,
                                   scope_key AS thread_scope_key, thread_id
                            FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            """,
                            (norm_tid,),
                        )
                        w_match = await cur.fetchone()
                        if (
                            w_match
                            and str(w_match["dalton_user_id"]) == cleaned_uid
                            and (
                                not platform
                                or str(w_match["platform"]).lower()
                                == platform.strip().lower()
                            )
                        ):
                            w_scope_key = await _resolve_thread_scope_key_for_thread(
                                cur,
                                str(w_match["thread_id"]),
                                fallback_scope_key=(
                                    str(w_match["thread_scope_key"])
                                    if w_match.get("thread_scope_key")
                                    else None
                                ),
                                target_address=str(w_match["target_address"]),
                            )
                            current_conversation = {
                                "address": w_match["target_address"],
                                "platform": w_match["platform"],
                                "thread_scope_key": w_scope_key,
                                "thread_id": str(w_match["thread_id"]),
                                "run_id": cleaned_rid,
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


async def get_feishu_open_id_for_user(
    user_id: str,
    *,
    conn: Any = None,
) -> str:
    """Return the Feishu ``open_id`` bound to ``user_id``, or ``""`` when absent, ambiguous, or lookup fails."""
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        logger.info("feishu bound open_id lookup skipped: empty user_id")
        return ""
    try:
        async with get_db_connection(conn) as owned_conn:
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT platform_user_id
                    FROM dalton_channel_identities
                    WHERE user_id = %s AND platform = 'feishu'
                    """,
                    (cleaned_uid,),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        logger.warning(
            "feishu bound open_id lookup failed for user_id=%s: %s",
            cleaned_uid,
            type(exc).__name__,
        )
        return ""
    if not rows:
        logger.info(
            "feishu bound open_id lookup found no binding for user_id=%s",
            cleaned_uid,
        )
        return ""
    if len(rows) > 1:
        logger.warning(
            "feishu bound open_id lookup found multiple rows (%d) for user_id=%s",
            len(rows),
            cleaned_uid,
        )
        return ""
    first_row = rows[0]
    raw_open_id = first_row.get("platform_user_id") if isinstance(first_row, dict) else None
    open_id = str(raw_open_id or "").strip()
    if not open_id:
        logger.info(
            "feishu bound open_id lookup found empty platform_user_id for user_id=%s",
            cleaned_uid,
        )
        return ""
    return open_id


# ---------------------------------------------------------------------------
# I-5: Update User Channel Settings (with CAS & Colleague Workspace Validation)
# ---------------------------------------------------------------------------


def _is_dm_address(platform: str, address: str) -> bool:
    cleaned = str(address or "").strip()
    if cleaned == f"{platform}:@me" or cleaned.endswith(":@me"):
        return True
    if str(platform or "").strip().lower() == "weixin" and cleaned.startswith("weixin:dm:"):
        return True
    return False


def _normalize_dm_target_address(platform: str, address: str | None) -> str:
    """Normalize legacy Weixin DM addresses (``weixin:dm:<id>``) to canonical ``weixin:@me``."""
    cleaned = str(address or "").strip()
    if str(platform or "").strip().lower() == "weixin" and (
        cleaned == "weixin:@me" or cleaned.startswith("weixin:dm:")
    ):
        return "weixin:@me"
    return cleaned


_DISCORD_CHANNEL_ID_ENTRY_RE = re.compile(r"^[0-9]{17,20}$")
_DISCORD_MAX_SNOWFLAKE_INT = (1 << 64) - 1
_DISCORD_CANONICAL_CHAT_ADDR_RE = re.compile(r"^discord:([0-9]+)$")


def _is_valid_discord_channel_id(raw: str | None) -> bool:
    cleaned = str(raw or "").strip()
    if not _DISCORD_CHANNEL_ID_ENTRY_RE.fullmatch(cleaned):
        return False
    return int(cleaned) <= _DISCORD_MAX_SNOWFLAKE_INT


async def _find_discord_channel_references(
    cur: Any,
    *,
    channel_id: str,
    discord_cfg: dict[str, Any] | None,
    user_id: str | None = None,
) -> list[dict[str, str]]:
    """Return active report/routing references to Discord ``channel_id`` (excluding data tasks)."""
    cleaned_id = str(channel_id or "").strip()
    chat_addr = f"discord:{cleaned_id}"
    channel_target = f"discord:channel:{cleaned_id}"
    match_targets = {chat_addr, channel_target}
    refs: list[dict[str, str]] = []

    await cur.execute(
        """
        SELECT automation_id::text AS automation_id, name, metadata
        FROM automations
        WHERE deleted_at IS NULL
          AND metadata ? 'report_profile'
        ORDER BY name ASC, automation_id ASC
        """
    )
    for auto_row in await cur.fetchall():
        meta = auto_row.get("metadata")
        rp = meta.get("report_profile") if isinstance(meta, dict) else None
        if not isinstance(rp, dict):
            continue
        auto_id = str(auto_row.get("automation_id") or "")
        auto_name = str(auto_row.get("name") or auto_id).strip() or auto_id
        for rp_key, rp_val in rp.items():
            if not isinstance(rp_val, list):
                continue
            if any(str(item or "").strip() in match_targets for item in rp_val):
                refs.append(
                    {
                        "kind": "automation_report_profile",
                        "name": auto_name,
                        "field": f"report_profile.{rp_key}",
                        "automation_id": auto_id,
                    }
                )

    auto_out = (
        discord_cfg.get("automation_output")
        if isinstance(discord_cfg, dict)
        else None
    )
    preferred_addr = (
        str(discord_cfg.get("preferred") or "").strip()
        if isinstance(discord_cfg, dict)
        else ""
    )
    preferred_matches = preferred_addr in match_targets

    await cur.execute(
        """
        SELECT automation_id::text AS automation_id, user_id, workspace_id::text AS workspace_id, name, delivery_config
        FROM automations
        WHERE deleted_at IS NULL
          AND delivery_config ? 'methods'
        ORDER BY name ASC, automation_id ASC
        """
    )
    for auto_row in await cur.fetchall():
        raw_dc = auto_row.get("delivery_config")
        methods = raw_dc.get("methods") if isinstance(raw_dc, dict) else None
        if not isinstance(methods, list):
            continue
        cleaned_methods = [
            str(item or "").strip() for item in methods if str(item or "").strip()
        ]
        if not cleaned_methods:
            continue
        auto_id = str(auto_row.get("automation_id") or "")
        auto_name = str(auto_row.get("name") or auto_id).strip() or auto_id
        if any(item in match_targets for item in cleaned_methods):
            refs.append(
                {
                    "kind": "automation_delivery_config",
                    "name": auto_name,
                    "field": "delivery_config.methods",
                    "automation_id": auto_id,
                }
            )
        elif (
            preferred_matches
            and user_id is not None
            and str(auto_row.get("user_id") or "").strip() == str(user_id).strip()
            and any(item.lower() == "discord" for item in cleaned_methods)
        ):
            from src.server.database.home_workspace import is_home_id

            auto_ws = (
                normalize_uuid(auto_row.get("workspace_id"))
                or str(auto_row.get("workspace_id") or "").strip()
            )
            is_home_auto = not auto_ws or is_home_id(user_id, auto_ws)
            ws_override = (
                str(auto_out.get(auto_ws) or "").strip()
                if not is_home_auto and isinstance(auto_out, dict)
                else ""
            )
            if not ws_override or ws_override in match_targets:
                refs.append(
                    {
                        "kind": "automation_delivery_config",
                        "name": auto_name,
                        "field": "delivery_config.methods",
                        "automation_id": auto_id,
                    }
                )
    if isinstance(auto_out, dict):
        matched_ws_keys = [
            str(ws_key).strip()
            for ws_key, target_val in auto_out.items()
            if str(target_val or "").strip() in match_targets
        ]
        if matched_ws_keys:
            ws_name_by_id: dict[str, str] = {}
            valid_uuids = [
                norm for k in matched_ws_keys if (norm := normalize_uuid(k))
            ]
            if valid_uuids:
                await cur.execute(
                    """
                    SELECT workspace_id::text AS workspace_id, name
                    FROM workspaces
                    WHERE workspace_id = ANY(%s::uuid[])
                    """,
                    (valid_uuids,),
                )
                for ws_row in await cur.fetchall():
                    wid = normalize_uuid(ws_row.get("workspace_id")) or str(
                        ws_row.get("workspace_id") or ""
                    )
                    wname = str(ws_row.get("name") or "").strip()
                    if wid and wname:
                        ws_name_by_id[wid] = wname
            for ws_key in matched_ws_keys:
                norm_ws = normalize_uuid(ws_key) or ws_key
                refs.append(
                    {
                        "kind": "automation_output",
                        "name": ws_name_by_id.get(norm_ws, norm_ws),
                        "field": "automation_output",
                        "workspace_id": norm_ws,
                    }
                )

    agent_msgs = (
        discord_cfg.get("agent_messages") if isinstance(discord_cfg, dict) else None
    )
    allowed_list = agent_msgs.get("allowed") if isinstance(agent_msgs, dict) else None
    if isinstance(allowed_list, list) and any(
        str(item or "").strip() in match_targets for item in allowed_list
    ):
        refs.append(
            {
                "kind": "agent_messages_allowed",
                "name": "Discord",
                "field": "agent_messages.allowed",
            }
        )

    return refs


async def update_user_channel_settings(
    *,
    user_id: str,
    version: str | None = None,
    settings: dict[str, Any] | None = None,
    automation_output_update: dict[str, Any] | None = None,
    discord_channel_entry: dict[str, Any] | None = None,
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
            discord_channel_entry=discord_channel_entry,
            conn=attempt_conn,
        ),
    )


async def _update_user_channel_settings_once(
    *,
    user_id: str,
    version: str | None = None,
    settings: dict[str, Any] | None = None,
    automation_output_update: dict[str, Any] | None = None,
    discord_channel_entry: dict[str, Any] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        raise HTTPException(status_code=400, detail="user_id is required")
    reject_company_system_auth(cleaned_uid)

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
                        WHERE user_id = %s AND platform <> 'weixin'
                        """,
                        (cleaned_uid,),
                    )
                    linked = [str(r["platform"]) for r in await cur.fetchall()]
                    if has_capability(CapabilityId.CHANNEL_WEIXIN):
                        await cur.execute(
                            """
                            SELECT 1 FROM dalton_channel_weixin_accounts
                            WHERE user_id = %s AND status = 'active' AND ilink_user_id IS NOT NULL
                            """,
                            (cleaned_uid,),
                        )
                        if await cur.fetchone():
                            linked.append("weixin")
                    current_version = "v1"
                    current_apps = {
                        p: default_app_settings(p) for p in sorted(linked)
                    }

                skip_full_app_validation = False
                force_sync_discord_addrs: set[str] = set()
                validated_apps: dict[str, Any] = {}
                preserved_hidden_weixin: Any = None
                has_preserved_hidden_weixin = False

                if discord_channel_entry is not None and settings is None:
                    entry_action = (
                        str(discord_channel_entry.get("action") or "").strip().lower()
                    )
                    if entry_action == "remove":
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "unsupported_action",
                                "message": "本版不支持在此移除 Discord 频道",
                            },
                        )
                    if entry_action not in ("add", "rename"):
                        raise HTTPException(
                            status_code=400,
                            detail={
                                "code": "invalid",
                                "message": f"Unsupported Discord channel action: {entry_action!r}",
                            },
                        )
                    if not isinstance(version, str) or not version.strip():
                        raise HTTPException(
                            status_code=422,
                            detail="version is required.",
                        )
                    entry_channel_id = str(
                        discord_channel_entry.get("channel_id") or ""
                    ).strip()
                    if not _is_valid_discord_channel_id(entry_channel_id):
                        raise HTTPException(
                            status_code=422,
                            detail="Discord channel ID must be a 17-20 digit snowflake below 2^64.",
                        )
                    raw_name = discord_channel_entry.get("name")
                    cleaned_name = (
                        str(raw_name).strip() if isinstance(raw_name, str) else ""
                    )
                    if len(cleaned_name) > 255:
                        raise HTTPException(
                            status_code=422,
                            detail="Channel display name must be 255 characters or fewer.",
                        )
                    if str(version) != current_version:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "version_conflict",
                                "message": f"Channel settings version conflict: expected {current_version}, got {version}.",
                            },
                        )
                    candidate_apps = json.loads(json.dumps(current_apps))
                    discord_cfg = candidate_apps.get("discord")
                    if not isinstance(discord_cfg, dict):
                        discord_cfg = default_app_settings("discord")
                        candidate_apps["discord"] = discord_cfg
                    chats_map = discord_cfg.get("chats")
                    if not isinstance(chats_map, dict):
                        chats_map = {}
                        discord_cfg["chats"] = chats_map
                    chat_addr = f"discord:{entry_channel_id}"
                    if entry_action == "add":
                        if chat_addr in chats_map:
                            await cur.execute(
                                """
                                SELECT 1 FROM dalton_channel_chat_bindings
                                WHERE platform = %s AND chat_address = %s
                                """,
                                ("discord", chat_addr),
                            )
                            has_shared_binding = (await cur.fetchone()) is not None
                            if has_shared_binding:
                                raise HTTPException(
                                    status_code=400,
                                    detail={
                                        "code": "invalid",
                                        "message": "This Discord channel is already registered.",
                                        "problems": [
                                            {
                                                "field": _field("discord", "chats", chat_addr),
                                                "message": "This Discord channel is already registered.",
                                            }
                                        ],
                                    },
                                )
                            existing_entry = chats_map.get(chat_addr)
                            restored_entry = (
                                dict(existing_entry)
                                if isinstance(existing_entry, dict)
                                else {}
                            )
                            raw_existing_ws = restored_entry.get("workspace_id")
                            existing_ws = (
                                (
                                    normalize_uuid(raw_existing_ws)
                                    or str(raw_existing_ws).strip()
                                )
                                if raw_existing_ws is not None
                                and str(raw_existing_ws).strip()
                                else None
                            )
                            if existing_ws is not None:
                                ws_ok, ws_err, ws_row = await _verify_workspace_write_permission(
                                    cur,
                                    existing_ws,
                                    cleaned_uid,
                                    conn=owned_conn,
                                )
                                if ws_row is None:
                                    # The workspace is gone: the restored entry binds nothing.
                                    existing_ws = None
                                elif not ws_ok:
                                    # Refuse rather than drop a live workspace binding: clearing
                                    # it would lift that workspace's write check on delivery, and
                                    # a transient permission-lookup failure must not do that.
                                    raise HTTPException(
                                        status_code=403,
                                        detail={
                                            "code": ws_err or "workspace_write_denied",
                                            "message": "This channel is bound to a workspace you cannot write to.",
                                        },
                                    )
                            restored_entry["workspace_id"] = existing_ws
                            if cleaned_name:
                                restored_entry["name"] = cleaned_name
                            chats_map[chat_addr] = restored_entry
                            force_sync_discord_addrs.add(chat_addr)
                        else:
                            new_entry: dict[str, Any] = {"workspace_id": None}
                            if cleaned_name:
                                new_entry["name"] = cleaned_name
                            chats_map[chat_addr] = new_entry
                    else:  # rename
                        if chat_addr not in chats_map:
                            raise HTTPException(
                                status_code=404,
                                detail={
                                    "code": "not_found",
                                    "message": "Discord channel is not registered.",
                                },
                            )
                        if not cleaned_name:
                            raise HTTPException(
                                status_code=400,
                                detail={
                                    "code": "invalid",
                                    "message": "Channel name is required.",
                                    "problems": [
                                        {
                                            "field": _field(
                                                "discord", "chats", chat_addr, "name"
                                            ),
                                            "message": "Channel name is required.",
                                        }
                                    ],
                                },
                            )
                        existing_entry = chats_map.get(chat_addr)
                        existing_dict = (
                            dict(existing_entry)
                            if isinstance(existing_entry, dict)
                            else {}
                        )
                        if "workspace_id" not in existing_dict:
                            existing_dict["workspace_id"] = None
                        existing_dict["name"] = cleaned_name
                        chats_map[chat_addr] = existing_dict
                    validated_apps = candidate_apps
                    skip_full_app_validation = True
                # Handle single-workspace automation_output update (`PUT /agent/automation-output`)
                elif automation_output_update is not None and settings is None:
                    upd_ws = str(
                        automation_output_update.get("workspace_id") or ""
                    ).strip()
                    upd_platform = (
                        str(automation_output_update.get("platform") or "")
                        .strip()
                        .lower()
                    )
                    require_channel_platform_capability(upd_platform)
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
                    target_settings = dict(settings)
                    if not has_capability(CapabilityId.CHANNEL_WEIXIN):
                        if "weixin" in target_settings:
                            if target_settings["weixin"] != current_apps.get("weixin"):
                                require_channel_platform_capability("weixin")
                            target_settings.pop("weixin", None)
                        if "weixin" in current_apps:
                            # Preserve hidden weixin config byte-for-byte WITHOUT validating/rebuilding it
                            preserved_hidden_weixin = json.loads(
                                json.dumps(current_apps["weixin"])
                            )
                            has_preserved_hidden_weixin = True
                    expected_version = version

                if not skip_full_app_validation:
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

                    if not skip_full_app_validation and has_preserved_hidden_weixin:
                        validated_apps["weixin"] = preserved_hidden_weixin

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
                elif force_sync_discord_addrs:
                    changes.append("Restored discord shared binding")
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

                def _group_chat_name(cfg: Any) -> str | None:
                    if not isinstance(cfg, dict):
                        return None
                    raw_name = cfg.get("name")
                    if raw_name is None:
                        return None
                    cleaned = str(raw_name).strip()
                    return cleaned or None

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
                    old_names = {
                        addr: _group_chat_name(cfg)
                        for addr, cfg in old_raw_chats.items()
                    }
                    new_names = {
                        addr: _group_chat_name(cfg)
                        for addr, cfg in new_raw_chats.items()
                    }
                    deleted_group_set = {
                        addr for addr in old_routing if addr not in new_routing
                    }
                    routing_changed_set = {
                        addr
                        for addr, r_cfg in new_routing.items()
                        if addr not in old_routing or r_cfg != old_routing[addr]
                    }
                    name_changed_set = {
                        addr
                        for addr, n_name in new_names.items()
                        if addr in old_names
                        and n_name is not None
                        and n_name != old_names[addr]
                    }
                    changed_group_addrs = sorted(
                        deleted_group_set
                        | routing_changed_set
                        | name_changed_set
                        | (force_sync_discord_addrs if app_key == "discord" else set())
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
                            SELECT workspace_id, chat_name FROM dalton_channel_chat_bindings
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
                        prev_chat_name = (
                            str(existing_binding.get("chat_name") or "").strip() or None
                            if existing_binding
                            else None
                        )
                        if chat_addr in deleted_group_set:
                            if (
                                app_key == "discord"
                                and not skip_full_app_validation
                                and (m := _DISCORD_CANONICAL_CHAT_ADDR_RE.fullmatch(chat_addr))
                            ):
                                refs = await _find_discord_channel_references(
                                    cur,
                                    channel_id=m.group(1),
                                    discord_cfg=app_cfg if isinstance(app_cfg, dict) else None,
                                    user_id=cleaned_uid,
                                )
                                if refs:
                                    if user_tier == "admin":
                                        raise HTTPException(
                                            status_code=409,
                                            detail={
                                                "code": "channel_in_use",
                                                "message": "Cannot remove Discord channel while it is still referenced.",
                                                "references": refs,
                                            },
                                        )
                                    auto_count = len(
                                        {
                                            r["automation_id"]
                                            for r in refs
                                            if r.get("kind")
                                            in (
                                                "automation_report_profile",
                                                "automation_delivery_config",
                                            )
                                            and r.get("automation_id")
                                        }
                                    )
                                    ws_count = len(
                                        {
                                            r["workspace_id"]
                                            for r in refs
                                            if r.get("kind") == "automation_output"
                                            and r.get("workspace_id")
                                        }
                                    )
                                    other_count = sum(
                                        1
                                        for r in refs
                                        if r.get("kind")
                                        not in (
                                            "automation_report_profile",
                                            "automation_output",
                                        )
                                    )
                                    ref_count = auto_count + ws_count + other_count
                                    raise HTTPException(
                                        status_code=409,
                                        detail={
                                            "code": "channel_in_use",
                                            "message": f"该频道仍被 {ref_count} 个自动化或工作区引用，无法移除。",
                                            "reference_count": ref_count,
                                        },
                                    )
                            if existing_binding is not None:
                                await cur.execute(
                                    """
                                    SELECT EXISTS (
                                        SELECT 1 FROM dalton_channel_user_settings
                                        WHERE user_id <> %s
                                          AND jsonb_typeof(apps -> %s -> 'chats') = 'object'
                                          AND jsonb_exists(apps -> %s -> 'chats', %s)
                                    ) AS has_other_ref
                                    """,
                                    (cleaned_uid, app_key, app_key, chat_addr),
                                )
                                other_ref_row = await cur.fetchone()
                                if not (other_ref_row and other_ref_row.get("has_other_ref")):
                                    await cur.execute(
                                        """
                                        DELETE FROM dalton_channel_chat_bindings
                                        WHERE platform = %s AND chat_address = %s
                                        """,
                                        (app_key, chat_addr),
                                    )
                        else:
                            binding_cfg = new_raw_chats[chat_addr]
                            is_added = chat_addr not in old_routing
                            routing_changed = chat_addr in routing_changed_set
                            new_ws_id = new_routing[chat_addr].get("workspace_id")
                            chat_name_val = _group_chat_name(binding_cfg)

                            if existing_binding is None:
                                effective_ws_id = new_ws_id
                            elif (is_added and new_ws_id is None) or not routing_changed:
                                # Preserve existing shared workspace_id when adding with null
                                # workspace or when only renaming the chat.
                                effective_ws_id = prev_ws_id
                            else:
                                effective_ws_id = new_ws_id

                            ws_unchanged = (
                                existing_binding is not None
                                and effective_ws_id == prev_ws_id
                            )
                            name_unchanged = (
                                existing_binding is not None
                                and (chat_name_val is None or chat_name_val == prev_chat_name)
                            )
                            if ws_unchanged and name_unchanged:
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
                                    bound_by_user_id = CASE
                                        WHEN dalton_channel_chat_bindings.workspace_id IS DISTINCT FROM EXCLUDED.workspace_id
                                        THEN EXCLUDED.bound_by_user_id
                                        ELSE dalton_channel_chat_bindings.bound_by_user_id
                                    END,
                                    updated_at = NOW()
                                """,
                                (
                                    app_key,
                                    chat_addr,
                                    chat_name_val,
                                    effective_ws_id,
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


async def _resolve_thread_scope_key_for_thread(
    cur: Any,
    thread_id: str | None,
    *,
    fallback_scope_key: str | None = None,
    target_address: str | None = None,
) -> str | None:
    """Resolve the canonical thread scope key (`chat:...:thread:...`, `chat:...`, or `dm:...`)
    for `thread_id` from `dalton_channel_thread_watches` or `dalton_channel_inbound_events`.
    """
    cleaned_fallback = str(fallback_scope_key or "").strip() or None
    cleaned_target = str(target_address or "").strip() or None
    if cleaned_fallback and cleaned_fallback != cleaned_target:
        return cleaned_fallback

    norm_tid = normalize_uuid(thread_id)
    if norm_tid:
        await cur.execute(
            """
            SELECT scope_key FROM dalton_channel_thread_watches
            WHERE thread_id = %s
            """,
            (norm_tid,),
        )
        w_row = await cur.fetchone()
        w_scope = (
            str(w_row["scope_key"]).strip()
            if w_row and w_row.get("scope_key")
            else None
        )
        if w_scope and w_scope != cleaned_target:
            return w_scope

        await cur.execute(
            """
            SELECT thread_scope_key FROM dalton_channel_inbound_events
            WHERE thread_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (norm_tid,),
        )
        in_row = await cur.fetchone()
        in_scope = (
            str(in_row["thread_scope_key"]).strip()
            if in_row and in_row.get("thread_scope_key")
            else None
        )
        if in_scope:
            return in_scope

        if w_scope:
            return w_scope

    return cleaned_fallback


async def _find_current_conversation_source(
    cur: Any,
    *,
    user_id: str | None,
    run_id: str | None,
    thread_id: str | None,
) -> dict[str, Any] | None:
    """Resolve the current channel conversation source row and its `thread_scope_key`
    across `dalton_channel_inbound_events`, `dalton_channel_background_runs`, and
    `dalton_channel_thread_watches`.
    """
    cleaned_uid = str(user_id or "").strip() or None
    cleaned_rid = str(run_id or "").strip() or None
    norm_tid = normalize_uuid(thread_id)
    if not cleaned_rid and not norm_tid:
        return None

    source_row: dict[str, Any] | None = None
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
                   external_thread_id, raw_payload, thread_id, run_id,
                   weixin_account_id::text AS weixin_account_id,
                   weixin_context_bot_id,
                   weixin_context_bot_generation
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
            source_row["_source_table"] = "inbound_events"
            source_row["_matched_exact_run"] = bool(cleaned_rid)

    if source_row is None and in_clauses:
        await cur.execute(
            f"""
            SELECT dalton_user_id, platform, target_address,
                   thread_id, run_id,
                   weixin_account_id::text AS weixin_account_id,
                   weixin_recipient_id,
                   weixin_bot_generation
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
            source_row["_source_table"] = "background_runs"
            source_row["_matched_exact_run"] = bool(cleaned_rid)

    if source_row is None and norm_tid:
        await cur.execute(
            """
            SELECT dalton_user_id, platform, target_address,
                   scope_key, scope_key AS thread_scope_key, generation, thread_id,
                   weixin_account_id::text AS weixin_account_id,
                   weixin_recipient_id,
                   weixin_bot_generation
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
            source_row["_source_table"] = "thread_watches"
            source_row["_matched_exact_run"] = False

    if source_row is None and cleaned_rid and norm_tid:
        tid_clauses: list[str] = ["thread_id = %s"]
        tid_params: list[Any] = [norm_tid]
        if cleaned_uid:
            tid_clauses.insert(0, "dalton_user_id = %s")
            tid_params.insert(0, cleaned_uid)
        await cur.execute(
            f"""
            SELECT dalton_user_id, platform, chat_address AS target_address,
                   thread_scope_key, workspace_id, session_generation,
                   external_thread_id, raw_payload, thread_id, run_id,
                   weixin_account_id::text AS weixin_account_id,
                   weixin_context_bot_id,
                   weixin_context_bot_generation
            FROM dalton_channel_inbound_events
            WHERE {" AND ".join(tid_clauses)}
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tuple(tid_params),
        )
        found_tid_in = await cur.fetchone()
        if found_tid_in:
            source_row = dict(found_tid_in)
            source_row["_source_table"] = "inbound_events"
            source_row["_matched_exact_run"] = False
        else:
            await cur.execute(
                f"""
                SELECT dalton_user_id, platform, target_address,
                       thread_id, run_id,
                       weixin_account_id::text AS weixin_account_id,
                       weixin_recipient_id,
                       weixin_bot_generation
                FROM dalton_channel_background_runs
                WHERE {" AND ".join(tid_clauses)}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                tuple(tid_params),
            )
            found_tid_bg = await cur.fetchone()
            if found_tid_bg:
                source_row = dict(found_tid_bg)
                source_row["_source_table"] = "background_runs"
                source_row["_matched_exact_run"] = False

    if not source_row:
        return None

    eff_tid = (
        str(source_row["thread_id"])
        if source_row.get("thread_id")
        else norm_tid
    )
    source_row["thread_scope_key"] = await _resolve_thread_scope_key_for_thread(
        cur,
        eff_tid,
        fallback_scope_key=(
            str(source_row["thread_scope_key"])
            if source_row.get("thread_scope_key")
            else None
        ),
        target_address=(
            str(source_row["target_address"])
            if source_row.get("target_address")
            else None
        ),
    )
    return source_row


_DISCORD_GROUP_PARENT = re.compile(
    r"^discord:(?P<guild>[0-9]{1,32})/(?P<parent>[0-9]{1,32})$"
)
_DISCORD_CONFIRMED_THREAD = re.compile(
    r"^discord:(?P<guild>[0-9]{1,32})/(?P<parent>[0-9]{1,32}):thread:(?P<thread>[0-9]{1,32})$"
)


def _delivery_confirmed_thread_address(row: dict[str, Any] | None) -> str | None:
    """Return the stored Discord thread address, or None when this delivery has none."""
    if not row:
        return None
    raw = row.get("confirmed_thread_address")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _delivery_discord_body_phase(row: dict[str, Any] | None) -> str | None:
    """Return the stored Discord body phase, or None when this delivery has none."""
    if not row:
        return None
    raw = row.get("discord_body_phase")
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _delivery_attempt(row: dict[str, Any] | None) -> int:
    """Return the stored claim generation, or 0 when this row has none."""
    if not row:
        return 0
    raw = row.get("delivery_attempt")
    if raw is None or isinstance(raw, bool):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _requested_delivery_attempt(value: Any) -> int | None:
    """Return a progress attempt token, or reject a value that is not an integer.

    ``None`` means the caller did not name a generation. ``bool`` is rejected
    because it is a subclass of ``int`` and is not a generation.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        _reject_delivery_update(400, "delivery_attempt must be an integer")
    return value


def _reject_delivery_update(status_code: int, message: str) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": "invalid" if status_code < 409 else "conflict",
            "message": message,
        },
    )


def _reject_confirmed_thread(status_code: int, message: str) -> NoReturn:
    _reject_delivery_update(status_code, message)


async def _store_confirmed_discord_thread(
    cur: Any,
    *,
    row: dict[str, Any],
    delivery_key: str,
    requested: str,
    prev_status: str,
) -> str:
    """Persist the first confirmed Discord thread for an in-flight group delivery.

    The same address may be repeated. A different address, a DM, or a guild or
    parent other than the original target is rejected and does not overwrite.
    """
    if requested != requested.strip() or not requested or len(requested) > 255:
        _reject_confirmed_thread(
            400, "confirmed_thread_address is not a Discord thread address"
        )
    platform = str(row.get("platform") or "").strip().lower()
    target = str(row.get("target_address") or "").strip()
    if platform != "discord" or _is_dm_address(platform, target):
        _reject_confirmed_thread(
            400,
            "confirmed_thread_address requires a Discord group parent target",
        )
    parent = _DISCORD_GROUP_PARENT.fullmatch(target)
    confirmed = _DISCORD_CONFIRMED_THREAD.fullmatch(requested)
    if (
        parent is None
        or confirmed is None
        or parent.group("guild") != confirmed.group("guild")
        or parent.group("parent") != confirmed.group("parent")
    ):
        _reject_confirmed_thread(
            400,
            "confirmed_thread_address does not match the original Discord guild and parent",
        )
    current = _delivery_confirmed_thread_address(row)
    if current == requested:
        return requested
    if current is not None:
        _reject_confirmed_thread(
            409, "confirmed_thread_address is already set and cannot be changed"
        )
    if prev_status != "in_flight":
        _reject_confirmed_thread(
            409,
            "confirmed_thread_address can be stored only while the delivery is in flight",
        )
    await cur.execute(
        """
        UPDATE dalton_channel_deliveries
        SET confirmed_thread_address = %s,
            discord_body_phase = CASE
                WHEN discord_body_phase IS NULL THEN 'before_body'
                ELSE discord_body_phase
            END,
            updated_at = NOW()
        WHERE delivery_key = %s
          AND confirmed_thread_address IS NULL
          AND status = 'in_flight'
        """,
        (requested, delivery_key),
    )
    if getattr(cur, "rowcount", 0) != 1:
        _reject_confirmed_thread(409, "confirmed_thread_address was not stored")
    row["confirmed_thread_address"] = requested
    if _delivery_discord_body_phase(row) is None:
        row["discord_body_phase"] = "before_body"
    return requested


async def _store_discord_body_phase(
    cur: Any,
    *,
    row: dict[str, Any],
    delivery_key: str,
    requested: str,
    prev_status: str,
) -> str:
    """Move the Discord body phase forward, or accept the same value again.

    ``before_body`` proves no body request has been authorized. ``dispatching``
    means a body POST may already have reached Discord. A later call cannot
    return to ``before_body`` or clear the phase. A new value is written only
    while the delivery is in flight; repeating the stored value changes nothing.
    """
    platform = str(row.get("platform") or "").strip().lower()
    if platform != "discord":
        _reject_delivery_update(
            400, "discord_body_phase requires a Discord delivery"
        )
    if (
        requested != requested.strip()
        or requested not in ("before_body", "dispatching")
    ):
        _reject_delivery_update(
            400, "discord_body_phase is not a recognized Discord body phase"
        )
    current = _delivery_discord_body_phase(row)
    if current == requested:
        return requested
    if current == "dispatching" or (
        current == "before_body" and requested != "dispatching"
    ):
        _reject_delivery_update(409, "discord_body_phase cannot move backwards")
    if current not in (None, "before_body"):
        _reject_delivery_update(409, "discord_body_phase cannot move backwards")
    if prev_status != "in_flight":
        _reject_delivery_update(
            409,
            "discord_body_phase can change only while the delivery is in flight",
        )
    await cur.execute(
        """
        UPDATE dalton_channel_deliveries
        SET discord_body_phase = %s,
            updated_at = NOW()
        WHERE delivery_key = %s
          AND status = 'in_flight'
          AND (
                discord_body_phase IS NULL
                OR (discord_body_phase = 'before_body' AND %s = 'dispatching')
              )
        """,
        (requested, delivery_key, requested),
    )
    if getattr(cur, "rowcount", 0) != 1:
        _reject_delivery_update(409, "discord_body_phase was not stored")
    row["discord_body_phase"] = requested
    return requested


VALID_DELIVERY_STATUSES: frozenset[str] = frozenset(
    {"in_flight", "sent", "partial", "failed", "unknown"}
)
ALLOWED_DELIVERY_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "in_flight": frozenset({"in_flight", "sent", "partial", "failed", "unknown"}),
    "partial": frozenset({"partial", "sent", "failed", "unknown"}),
    "sent": frozenset({"sent"}),
    "failed": frozenset({"failed"}),
    "unknown": frozenset({"unknown"}),
}


def _validate_delivery_status_transition(
    *,
    prev_status: str,
    status: str | None,
    outcome: str | None,
    platform: str,
    kind: str,
    error_code: str | None,
    discord_body_phase: str | None,
    current_attempt: int,
    requested_attempt: int | None,
) -> str:
    """Validate and resolve the target delivery status for I-7 progress updates (R10 Codex #5).

    Enforces:
    - If both ``status`` and ``outcome`` are supplied, they must match and be in
      ``VALID_DELIVERY_STATUSES`` (rejecting unsupported values such as ``deferred``).
    - Discord ``unknown`` protection cannot be bypassed via ``outcome`` or any other field;
      only the exact ``gateway_restarted`` + ``dispatching`` + matching ``delivery_attempt``
      recovery terminal transition is permitted.
    - Terminal statuses (``sent``, ``failed``, ``unknown``) cannot regress to ``in_flight``
      or transition to another terminal status via I-7 (re-claim after ``failed`` goes
      exclusively through I-6 ``authorize_and_claim_delivery``).
    """
    clean_prev = str(prev_status or "").strip().lower()
    clean_status = str(status or "").strip().lower() or None
    clean_outcome = str(outcome or "").strip().lower() or None
    if clean_status and clean_outcome and clean_status != clean_outcome:
        _reject_delivery_update(
            400,
            f"Conflicting delivery status ({clean_status}) and outcome ({clean_outcome})",
        )
    requested = clean_status or clean_outcome
    if requested is None:
        return clean_prev
    if requested not in VALID_DELIVERY_STATUSES:
        _reject_delivery_update(
            400,
            f"Unsupported delivery status: {requested!r}",
        )
    clean_plat = str(platform or "").strip().lower()
    clean_kind = str(kind or "").strip().lower()
    recovery_terminal = (
        clean_plat == "discord"
        and clean_kind != "data_task"
        and clean_prev == "unknown"
        and requested_attempt is not None
        and requested_attempt == current_attempt
        and str(error_code or "").strip() == "gateway_restarted"
        and discord_body_phase == "dispatching"
        and requested in ("sent", "failed", "partial")
    )
    if (
        clean_plat == "discord"
        and clean_kind != "data_task"
        and clean_prev == "unknown"
        and requested != "unknown"
        and not recovery_terminal
    ):
        _reject_delivery_update(
            409, "Discord delivery status unknown cannot be changed"
        )
    if recovery_terminal:
        return requested
    allowed_targets = ALLOWED_DELIVERY_STATUS_TRANSITIONS.get(clean_prev, frozenset())
    if requested not in allowed_targets:
        _reject_delivery_update(
            409,
            f"Invalid delivery status transition: {clean_prev!r} -> {requested!r}",
        )
    return requested


async def _lock_weixin_account_for_key_share_in_tx(
    cur: Any,
    *,
    user_id: str | None = None,
    account_id: str | None = None,
) -> None:
    """Lock the member's Weixin account row ``FOR KEY SHARE`` BEFORE locking
    ``dalton_channel_session_generations`` or inserting/updating ``dalton_channel_deliveries``
    (R10 Codex #3), aligning I-6 lock order strictly with ``unbind_weixin_account``
    (``weixin_accounts -> session_generations -> deliveries``) to prevent AB-BA deadlocks.
    """
    norm_aid = normalize_uuid(account_id) if account_id else None
    clean_uid = str(user_id or "").strip() or None
    if norm_aid:
        await cur.execute(
            """
            SELECT account_id
            FROM dalton_channel_weixin_accounts
            WHERE account_id = %s::uuid
            FOR KEY SHARE
            """,
            (norm_aid,),
        )
        return
    if clean_uid and not clean_uid.startswith("unlinked:") and not clean_uid.startswith("__unlinked__:"):
        await cur.execute(
            """
            SELECT account_id
            FROM dalton_channel_weixin_accounts
            WHERE user_id = %s
            ORDER BY account_id ASC
            FOR KEY SHARE
            """,
            (clean_uid,),
        )


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
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """I-6: Atomically check idempotency, authorize outbound target across Scenarios A1/A2/B/C, and claim `in_flight`."""
    cleaned_key = str(delivery_key or "").strip()
    if not cleaned_key:
        raise HTTPException(status_code=400, detail="delivery_key is required")
    cleaned_kind = str(kind or "agent_send").strip().lower()
    if user_id is not None:
        reject_company_system_auth(user_id)
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
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
            conn=conn,
        )
    # DALTON-HOOK(dalton-notify): claim generic automation notifications before conversation auth
    if cleaned_kind == "automation_notify":
        from src.dalton.database.automation_notifications import (
            authorize_and_claim_automation_notify_delivery,
        )

        return await authorize_and_claim_automation_notify_delivery(
            delivery_key=cleaned_key,
            run_id=run_id,
            target=target,
            has_non_empty_text=has_non_empty_text,
            total_chunks=total_chunks,
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
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
                plat_hint = cleaned_platform or (
                    cleaned_target.split(":", 1)[0].strip().lower()
                    if cleaned_target and ":" in cleaned_target
                    else None
                )
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    gateway_epoch,
                    required=require_gateway_epoch,
                    platform=plat_hint,
                    enforce_post_lease_for_non_weixin=False,
                )
                # Serialize concurrent claims on the same delivery_key
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_delivery:{cleaned_key}",),
                )
                # R10 (Codex #3): Lock weixin_accounts FOR KEY SHARE -> session_generations
                # FOR SHARE -> deliveries FOR UPDATE (matching unbind_weixin_account order)
                # to prevent AB-BA deadlocks between I-6 claims and concurrent unbind.
                await cur.execute(
                    """
                    SELECT user_id, platform, source_scope_key,
                           weixin_account_id::text AS weixin_account_id
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    """,
                    (cleaned_key,),
                )
                pre_del = await cur.fetchone()
                if (
                    pre_del
                    and str(pre_del.get("platform") or "").strip().lower() == "weixin"
                ):
                    await _lock_weixin_account_for_key_share_in_tx(
                        cur,
                        user_id=pre_del.get("user_id"),
                        account_id=pre_del.get("weixin_account_id"),
                    )
                    if pre_del.get("source_scope_key") and pre_del.get("user_id"):
                        await cur.execute(
                            """
                            SELECT generation
                            FROM dalton_channel_session_generations
                            WHERE platform = 'weixin'
                              AND scope_key = %s
                              AND dalton_user_id = %s
                            FOR SHARE
                            """,
                            (
                                str(pre_del["source_scope_key"]).strip(),
                                str(pre_del["user_id"]).strip(),
                            ),
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
                    if str(existing.get("platform") or "").strip().lower() == "weixin":
                        await wx_db.verify_gateway_epoch_in_tx(
                            cur,
                            gateway_epoch,
                            required=require_gateway_epoch,
                            platform="weixin",
                        )
                    reject_company_system_auth(existing.get("user_id"))
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
                    ex_is_current = bool(existing.get("is_current", False))
                    stored_confirmed_thread = _delivery_confirmed_thread_address(existing)
                    stored_body_phase = _delivery_discord_body_phase(existing)
                    ex_scope_key: str | None = None
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
                    if ex_status in ("sent", "partial", "in_flight", "unknown"):
                        if (
                            cleaned_kind == "notice"
                            and cleaned_inbound_mid
                            and (existing.get("platform") or cleaned_platform)
                        ):
                            await cur.execute(
                                """
                                SELECT * FROM dalton_channel_inbound_events
                                WHERE platform = %s AND message_id = %s
                                """,
                                (
                                    str(existing.get("platform") or cleaned_platform),
                                    cleaned_inbound_mid,
                                ),
                            )
                            ex_in_row = await cur.fetchone()
                            if ex_in_row and ex_in_row.get("thread_scope_key"):
                                ex_scope_key = str(ex_in_row["thread_scope_key"])
                        elif ex_is_current:
                            ex_src = await _find_current_conversation_source(
                                cur,
                                user_id=(
                                    str(existing["user_id"])
                                    if existing.get("user_id")
                                    else cleaned_uid
                                ),
                                run_id=(
                                    str(existing["run_id"])
                                    if existing.get("run_id")
                                    else cleaned_rid
                                ),
                                thread_id=(
                                    str(existing["thread_id"])
                                    if existing.get("thread_id")
                                    else norm_tid
                                ),
                            )
                            if ex_src and ex_src.get("thread_scope_key"):
                                ex_scope_key = str(ex_src["thread_scope_key"])
                    if ex_status in ("sent", "partial") and conversation_to_replay:
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
                            "confirmed_thread_address": stored_confirmed_thread,
                            "discord_body_phase": stored_body_phase,
                            "delivery_attempt": _delivery_attempt(existing),
                            "thread_scope_key": ex_scope_key,
                            "is_current": ex_is_current,
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
                            "confirmed_thread_address": stored_confirmed_thread,
                            "discord_body_phase": stored_body_phase,
                            "delivery_attempt": _delivery_attempt(existing),
                            "thread_scope_key": ex_scope_key,
                            "is_current": ex_is_current,
                        }
                    # Startup rewrites in_flight to unknown and leaves the thread
                    # address and body phase in place. A stored thread proves where
                    # to send. It does not prove the body has not already reached
                    # Discord. Unknown resumes only when the phase is before_body,
                    # which is written with the thread before any body request is
                    # authorized. A null phase or dispatching stays blocked, as does
                    # unknown with no stored thread: an ambiguous create must not
                    # call Discord create again.
                    reclaim_before_body = (
                        ex_status == "unknown"
                        and stored_confirmed_thread is not None
                        and stored_body_phase == "before_body"
                    )
                    if ex_status in ("in_flight", "unknown") and not reclaim_before_body:
                        return {
                            "claim_status": "in_flight_or_unknown",
                            "status": "unknown",
                            "code": "in_flight",
                            "message": "An earlier attempt of this same call is still delivering it.",
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "confirmed_thread_address": stored_confirmed_thread,
                            "discord_body_phase": stored_body_phase,
                            "delivery_attempt": _delivery_attempt(existing),
                            "thread_scope_key": ex_scope_key,
                            "is_current": ex_is_current,
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
                resolved_thread_scope_key: str | None = None
                resolved_recipient_id: str | None = None
                resolved_user_id: str | None = cleaned_uid
                is_current = False
                weixin_ctx_token: str | None = None
                weixin_ctx_token_version: int | None = None
                weixin_source_row: dict[str, Any] | None = None
                weixin_source_kind: str | None = None  # "inbound" | "background_run" | "thread_watch" | "proactive"
                resolved_wx_account_id: str | None = None
                resolved_wx_bot_id: str | None = None
                resolved_wx_bot_gen: int | None = None

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
                    reject_company_system_auth(in_row.get("dalton_user_id"))
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
                    resolved_thread_scope_key = (
                        str(in_row["thread_scope_key"])
                        if in_row.get("thread_scope_key")
                        else None
                    )
                    raw_p = in_row.get("raw_payload") or {}
                    puid = str(raw_p.get("platform_user_id") or "")
                    in_dalton_uid = (
                        str(in_row["dalton_user_id"]).strip()
                        if in_row.get("dalton_user_id")
                        else None
                    )
                    if cleaned_uid:
                        if cleaned_uid.startswith("unlinked:") or cleaned_uid.startswith(
                            "__unlinked__:"
                        ):
                            parts = cleaned_uid.split(":", 2)
                            if len(parts) == 3 and parts[2] and parts[2] != puid:
                                return {
                                    "claim_status": "denied",
                                    "status": "failed",
                                    "code": "not_allowed",
                                    "message": "Unlinked notice recipient must match original inbound sender.",
                                }
                        elif in_dalton_uid and cleaned_uid != in_dalton_uid:
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "not_allowed",
                                "message": "Notice user_id does not match original inbound event user.",
                            }
                    if cleaned_target and _normalize_dm_target_address(
                        resolved_platform, cleaned_target
                    ) not in (
                        _normalize_dm_target_address(
                            resolved_platform, resolved_target_address
                        ),
                        f"{resolved_platform}:@me",
                        f"{resolved_platform}:{puid}",
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_allowed",
                            "message": "Notice target does not match original inbound conversation.",
                        }
                    # R9 (Decision #5) + R10 (Codex #3): Lock Weixin account FOR KEY SHARE
                    # BEFORE locking session_generations FOR SHARE so I-6 strictly follows
                    # unbind_weixin_account's lock order (accounts -> session_generations -> deliveries).
                    if (
                        resolved_platform == "weixin"
                        and persisted_scope
                        and persisted_gen is not None
                        and in_dalton_uid
                    ):
                        await _lock_weixin_account_for_key_share_in_tx(
                            cur,
                            user_id=in_dalton_uid,
                            account_id=in_row.get("weixin_account_id"),
                        )
                        await cur.execute(
                            """
                            SELECT generation
                            FROM dalton_channel_session_generations
                            WHERE platform = 'weixin'
                              AND scope_key = %s
                              AND dalton_user_id = %s
                            FOR SHARE
                            """,
                            (persisted_scope, in_dalton_uid),
                        )
                        live_n_sg = await cur.fetchone()
                        if (
                            live_n_sg is not None
                            and live_n_sg.get("generation") is not None
                            and int(live_n_sg["generation"]) != int(persisted_gen)
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "session_generation_superseded",
                                "message": "Weixin session generation was advanced after the source inbound event.",
                            }
                    if resolved_platform == "weixin" and (
                        not _is_dm_address(resolved_platform, resolved_target_address)
                        or str(in_row.get("chat_type") or "").lower() == "group"
                        or bool(str(raw_p.get("group_id") or "").strip())
                    ):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "weixin_group_unsupported",
                            "message": "Weixin group delivery is not supported.",
                        }
                    resolved_user_id = (
                        str(in_row["dalton_user_id"])
                        if in_row.get("dalton_user_id")
                        else f"unlinked:{resolved_platform}:{puid}"
                    )
                    if resolved_platform == "weixin":
                        weixin_source_row = dict(in_row)
                        weixin_source_kind = "inbound"
                    if in_row.get("dalton_user_id") and not puid:
                        if resolved_platform == "weixin":
                            await cur.execute(
                                """
                                SELECT ilink_user_id AS platform_user_id
                                FROM dalton_channel_weixin_accounts
                                WHERE user_id = %s AND status = 'active'
                                """,
                                (in_row["dalton_user_id"],),
                            )
                        else:
                            await cur.execute(
                                """
                                SELECT platform_user_id
                                FROM dalton_channel_identities
                                WHERE user_id = %s AND platform = %s
                                """,
                                (in_row["dalton_user_id"], resolved_platform),
                            )
                        id_row = await cur.fetchone()
                        if id_row and not puid:
                            puid = str(id_row.get("platform_user_id") or "")
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
                    reject_company_system_auth(owner_uid)
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

                    if resolved_platform == "weixin":
                        weixin_source_kind = "proactive"
                        await cur.execute(
                            """
                            SELECT ilink_user_id AS platform_user_id
                            FROM dalton_channel_weixin_accounts
                            WHERE user_id = %s AND status = 'active' AND ilink_user_id IS NOT NULL
                            """,
                            (resolved_user_id,),
                        )
                        id_row = await cur.fetchone()
                    else:
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
                    source_row = await _find_current_conversation_source(
                        cur,
                        user_id=cleaned_uid,
                        run_id=cleaned_rid,
                        thread_id=norm_tid,
                    )

                    if not source_row:
                        # R5 (Decision #4): For Weixin conversation deliveries with no
                        # verifiable source, record status='unknown' + audit log BEFORE
                        # returning denied (after capability & gateway_epoch checks).
                        if cleaned_platform == "weixin":
                            require_channel_platform_capability("weixin")
                            await wx_db.verify_gateway_epoch_in_tx(
                                cur,
                                gateway_epoch,
                                required=require_gateway_epoch,
                                platform="weixin",
                            )
                            miss_uid = cleaned_uid or "unlinked:weixin:"
                            await cur.execute(
                                """
                                INSERT INTO dalton_channel_deliveries (
                                    delivery_key, kind, user_id, thread_id, run_id, execution_id,
                                    is_current, has_non_empty_text, platform, target_address,
                                    status, total_chunks, sent_chunks, platform_message_ids,
                                    files_status, error_code, error_message,
                                    source_scope_key, source_generation, activity_origin,
                                    delivery_attempt, created_at, updated_at
                                )
                                VALUES (
                                    %s, %s, %s, %s, %s, %s,
                                    TRUE, %s, 'weixin', 'weixin:@me',
                                    'unknown', %s, 0, '[]'::jsonb,
                                    '[]'::jsonb, 'source_generation_unknown', %s,
                                    %s, %s, %s,
                                    1, NOW(), NOW()
                                )
                                ON CONFLICT (delivery_key) DO UPDATE SET
                                    status = 'unknown',
                                    error_code = EXCLUDED.error_code,
                                    error_message = EXCLUDED.error_message,
                                    updated_at = NOW()
                                """,
                                (
                                    cleaned_key,
                                    cleaned_kind,
                                    miss_uid,
                                    norm_tid,
                                    cleaned_rid,
                                    eff_exec_id,
                                    bool(has_non_empty_text),
                                    int(total_chunks or 0),
                                    "No verifiable Weixin conversation source found for this delivery.",
                                    eff_source_scope,
                                    eff_source_gen,
                                    eff_activity_origin,
                                ),
                            )
                            await wx_db.write_weixin_audit_log_in_tx(
                                cur,
                                action="ambiguous_legacy_delivery_blocked",
                                operator_user_id="system:delivery-guard",
                                target_user_id=(
                                    cleaned_uid
                                    if cleaned_uid
                                    and not str(cleaned_uid).startswith("unlinked:")
                                    else None
                                ),
                                metadata={
                                    "delivery_key": cleaned_key,
                                    "error_code": "source_generation_unknown",
                                    "source_kind": "missing_source",
                                    "run_id": cleaned_rid,
                                    "thread_id": norm_tid,
                                },
                            )
                            return {
                                "claim_status": "denied",
                                "status": "unknown",
                                "code": "source_generation_unknown",
                                "message": "No verifiable Weixin conversation source found for this delivery.",
                            }
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
                    reject_company_system_auth(resolved_user_id)
                    resolved_platform = str(source_row["platform"]).lower()
                    resolved_target_address = str(source_row["target_address"])
                    resolved_thread_scope_key = (
                        str(source_row["thread_scope_key"])
                        if source_row.get("thread_scope_key")
                        else None
                    )
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

                    # R7 (Decision #4) + R10 (Codex #3): Lock Weixin account FOR KEY SHARE
                    # BEFORE locking session_generations FOR SHARE to align with unbind lock order.
                    if (
                        resolved_platform == "weixin"
                        and eff_source_scope
                        and eff_source_gen is not None
                    ):
                        await _lock_weixin_account_for_key_share_in_tx(
                            cur,
                            user_id=resolved_user_id,
                            account_id=source_row.get("weixin_account_id"),
                        )
                        await cur.execute(
                            """
                            SELECT generation
                            FROM dalton_channel_session_generations
                            WHERE platform = 'weixin'
                              AND scope_key = %s
                              AND dalton_user_id = %s
                            FOR SHARE
                            """,
                            (eff_source_scope, resolved_user_id),
                        )
                        live_sg_row = await cur.fetchone()
                        if (
                            live_sg_row is not None
                            and live_sg_row.get("generation") is not None
                            and int(live_sg_row["generation"]) != int(eff_source_gen)
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "session_generation_superseded",
                                "message": "Weixin session generation was advanced after the source turn.",
                            }

                    if resolved_platform == "weixin":
                        weixin_source_row = dict(source_row)
                        src_tbl = str(source_row.get("_source_table") or "")
                        matched_exact_run = bool(source_row.get("_matched_exact_run"))
                        # R5 (Decision #4): Weixin conversation deliveries MUST match
                        # an exact persisted per-turn inbound_events or background_runs
                        # row by run_id. NEVER borrow thread_watches' latest generation
                        # when run_id is absent or unmatched!
                        if (
                            not cleaned_rid
                            or not matched_exact_run
                            or src_tbl not in ("inbound_events", "background_runs")
                        ):
                            weixin_source_kind = "unmatched_run"
                        elif src_tbl == "background_runs":
                            weixin_source_kind = "background_run"
                        else:
                            weixin_source_kind = "inbound"
                        await cur.execute(
                            """
                            SELECT ilink_user_id AS platform_user_id
                            FROM dalton_channel_weixin_accounts
                            WHERE user_id = %s AND status = 'active' AND ilink_user_id IS NOT NULL
                            """,
                            (resolved_user_id,),
                        )
                        id_row = await cur.fetchone()
                    else:
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
                            or await get_user_tier(resolved_user_id, conn=owned_conn)
                            == "external"
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

                    if resolved_platform == "weixin":
                        weixin_source_kind = "proactive"
                        await cur.execute(
                            """
                            SELECT ilink_user_id AS platform_user_id
                            FROM dalton_channel_weixin_accounts
                            WHERE user_id = %s AND status = 'active' AND ilink_user_id IS NOT NULL
                            """,
                            (resolved_user_id,),
                        )
                        id_row = await cur.fetchone()
                    else:
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
                    user_tier = await get_user_tier(resolved_user_id, conn=owned_conn)
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

                    # Check whether explicit target matches the turn's current conversation
                    persisted_c_scope: str | None = None
                    persisted_c_gen: int | None = None
                    if cleaned_rid or norm_tid:
                        cur_source = await _find_current_conversation_source(
                            cur,
                            user_id=resolved_user_id,
                            run_id=cleaned_rid,
                            thread_id=norm_tid,
                        )
                        if (
                            cur_source
                            and str(cur_source["platform"]).lower() == resolved_platform
                            and _normalize_dm_target_address(
                                resolved_platform, cur_source.get("target_address")
                            )
                            == _normalize_dm_target_address(
                                resolved_platform, resolved_target_address
                            )
                        ):
                            is_current = True
                            resolved_thread_scope_key = (
                                str(cur_source["thread_scope_key"])
                                if cur_source.get("thread_scope_key")
                                else None
                            )
                            raw_c_scope = cur_source.get("thread_scope_key") or cur_source.get(
                                "scope_key"
                            )
                            if raw_c_scope:
                                persisted_c_scope = str(raw_c_scope).strip() or None
                            if cur_source.get("session_generation") is not None:
                                persisted_c_gen = int(cur_source["session_generation"])
                            elif cur_source.get("generation") is not None:
                                persisted_c_gen = int(cur_source["generation"])
                            elif (
                                isinstance(cur_source.get("raw_payload"), dict)
                                and cur_source["raw_payload"].get("generation") is not None
                            ):
                                persisted_c_gen = int(
                                    cur_source["raw_payload"]["generation"]
                                )
                            # R7 (Decision #4): When kind=agent_send specifies target equal
                            # to the current Weixin conversation address (e.g. `weixin:@me`),
                            # enforce the EXACT same source run_id / bot_generation / account
                            # verification as Scenario A2 (never bypass via "proactive")!
                            if resolved_platform == "weixin":
                                lookup_c_tid = (
                                    normalize_uuid(cur_source.get("thread_id")) or norm_tid
                                )
                                if (
                                    persisted_c_scope is None or persisted_c_gen is None
                                ) and lookup_c_tid:
                                    await cur.execute(
                                        """
                                        SELECT dalton_user_id, scope_key, generation
                                        FROM dalton_channel_thread_watches
                                        WHERE thread_id = %s
                                        """,
                                        (lookup_c_tid,),
                                    )
                                    wc_extra = await cur.fetchone()
                                    if (
                                        wc_extra
                                        and str(wc_extra.get("dalton_user_id") or "")
                                        == resolved_user_id
                                    ):
                                        if persisted_c_scope is None and wc_extra.get(
                                            "scope_key"
                                        ):
                                            persisted_c_scope = (
                                                str(wc_extra["scope_key"]).strip() or None
                                            )
                                        if (
                                            persisted_c_gen is None
                                            and wc_extra.get("generation") is not None
                                        ):
                                            persisted_c_gen = int(wc_extra["generation"])
                                weixin_source_row = dict(cur_source)
                                src_c_tbl = str(cur_source.get("_source_table") or "")
                                matched_c_exact = bool(
                                    cur_source.get("_matched_exact_run")
                                )
                                if (
                                    not cleaned_rid
                                    or not matched_c_exact
                                    or src_c_tbl not in ("inbound_events", "background_runs")
                                ):
                                    weixin_source_kind = "unmatched_run"
                                elif src_c_tbl == "background_runs":
                                    weixin_source_kind = "background_run"
                                else:
                                    weixin_source_kind = "inbound"

                    if is_current:
                        if (
                            resolved_platform == "weixin"
                            and persisted_c_scope
                            and persisted_c_gen is not None
                        ):
                            await _lock_weixin_account_for_key_share_in_tx(
                                cur,
                                user_id=resolved_user_id,
                                account_id=(
                                    weixin_source_row.get("weixin_account_id")
                                    if isinstance(weixin_source_row, dict)
                                    else None
                                ),
                            )
                            await cur.execute(
                                """
                                SELECT generation
                                FROM dalton_channel_session_generations
                                WHERE platform = 'weixin'
                                  AND scope_key = %s
                                  AND dalton_user_id = %s
                                FOR SHARE
                                """,
                                (persisted_c_scope, resolved_user_id),
                            )
                            live_c_sg = await cur.fetchone()
                            if (
                                live_c_sg is not None
                                and live_c_sg.get("generation") is not None
                                and int(live_c_sg["generation"]) != int(persisted_c_gen)
                            ):
                                return {
                                    "claim_status": "denied",
                                    "status": "failed",
                                    "code": "session_generation_superseded",
                                    "message": "Weixin session generation was advanced after the source turn.",
                                }
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
                # is rejected and does not retarget the stored row. The confirmed thread
                # and body phase are omitted from the upsert so a reclaim keeps both.
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
                            "confirmed_thread_address": _delivery_confirmed_thread_address(
                                existing
                            ),
                            "discord_body_phase": _delivery_discord_body_phase(existing),
                            "delivery_attempt": _delivery_attempt(existing),
                        }

                if resolved_user_id and not str(resolved_user_id).startswith("unlinked:"):
                    reject_company_system_auth(resolved_user_id)
                require_channel_platform_capability(resolved_platform)

                # Multi-account Weixin authorization, source generation check, and token resolution (§3.2 #4, D18, Codex ①)
                if resolved_platform == "weixin":
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        platform="weixin",
                    )
                    async def _block_ambiguous_weixin_delivery(
                        err_code: str, err_msg: str, meta: dict[str, Any]
                    ) -> dict[str, Any]:
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_deliveries (
                                delivery_key, kind, user_id, thread_id, run_id, execution_id,
                                is_current, has_non_empty_text, platform, target_address,
                                status, total_chunks, sent_chunks, platform_message_ids,
                                files_status, error_code, error_message,
                                source_scope_key, source_generation, activity_origin,
                                delivery_attempt, created_at, updated_at
                            )
                            VALUES (
                                %s, %s, %s, %s, %s, %s,
                                %s, %s, 'weixin', %s,
                                'unknown', %s, 0, '[]'::jsonb,
                                '[]'::jsonb, %s, %s,
                                %s, %s, %s,
                                1, NOW(), NOW()
                            )
                            ON CONFLICT (delivery_key) DO UPDATE SET
                                status = 'unknown',
                                error_code = EXCLUDED.error_code,
                                error_message = EXCLUDED.error_message,
                                updated_at = NOW()
                            """,
                            (
                                cleaned_key,
                                cleaned_kind,
                                resolved_user_id or "unlinked:weixin:",
                                norm_tid,
                                cleaned_rid,
                                eff_exec_id,
                                is_current,
                                bool(has_non_empty_text),
                                resolved_target_address or "weixin:@me",
                                int(total_chunks or 0),
                                err_code,
                                err_msg,
                                eff_source_scope,
                                eff_source_gen,
                                eff_activity_origin,
                            ),
                        )
                        await wx_db.write_weixin_audit_log_in_tx(
                            cur,
                            action="ambiguous_legacy_delivery_blocked",
                            operator_user_id="system:delivery-guard",
                            target_user_id=(
                                resolved_user_id
                                if resolved_user_id
                                and not str(resolved_user_id).startswith("unlinked:")
                                else None
                            ),
                            weixin_account_id=meta.get("source_account_id"),
                            ilink_bot_id=meta.get("source_bot_id"),
                            ilink_user_id=meta.get("source_recipient_id"),
                            metadata={"delivery_key": cleaned_key, "error_code": err_code, **meta},
                        )
                        return {
                            "claim_status": "denied",
                            "status": "unknown",
                            "code": err_code,
                            "message": err_msg,
                        }

                    if weixin_source_kind == "unmatched_run":
                        return await _block_ambiguous_weixin_delivery(
                            "source_generation_unknown",
                            "Weixin conversation delivery has no persisted per-turn inbound or background_run source matching run_id.",
                            {
                                "source_kind": "unmatched_run",
                                "run_id": cleaned_rid,
                                "thread_id": norm_tid,
                                "fallback_table": (
                                    str(weixin_source_row.get("_source_table") or "")
                                    if weixin_source_row
                                    else None
                                ),
                            },
                        )

                    if resolved_user_id and not str(resolved_user_id).startswith("unlinked:"):
                        roster_st = await check_team_member_status_live(
                            resolved_user_id, conn=owned_conn
                        )
                        if roster_st == "lookup_error":
                            raise HTTPException(
                                status_code=503,
                                detail={
                                    "code": "roster_lookup_error",
                                    "retryable": True,
                                    "message": "Team member roster lookup failed; retry later.",
                                },
                            )
                        if roster_st != "active":
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "member_not_in_roster",
                                "message": "Member is no longer in the team roster.",
                            }
                        wx_user_tier = await get_user_tier(resolved_user_id, conn=owned_conn)
                        wx_cfg = await wx_db.ensure_and_lock_weixin_config_in_tx(
                            cur, for_update=False
                        )
                        wx_acc_ok, wx_deny = evaluate_weixin_member_access(
                            wx_user_tier,
                            channel_enabled=bool(wx_cfg["enabled"]),
                            db_allow_external=wx_cfg.get("allow_external"),
                        )
                        if not wx_acc_ok:
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": wx_deny or "weixin_channel_disabled",
                                "message": "Weixin channel access is currently disabled for this member.",
                            }

                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id,
                               user_id,
                               ilink_bot_id,
                               ilink_user_id,
                               bot_generation,
                               status
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                        FOR KEY SHARE
                        """,
                        (resolved_user_id,),
                    )
                    wx_acc = await cur.fetchone()
                    if wx_acc is None:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "not_linked",
                            "message": "Weixin account is not linked.",
                        }
                    if str(wx_acc["status"]) == "expired":
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "weixin_session_expired",
                            "message": "Weixin bot session has expired; please re-scan QR code.",
                        }
                    if str(wx_acc["status"]) != "active" or not wx_acc.get("ilink_user_id"):
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": f"weixin_account_{wx_acc['status']}",
                            "message": "Weixin account is not active.",
                        }

                    cur_aid = str(wx_acc["account_id"])
                    cur_bot_id = str(wx_acc["ilink_bot_id"])
                    cur_bot_gen = int(wx_acc["bot_generation"])
                    cur_wx_uid = str(wx_acc["ilink_user_id"])

                    # Verify source account / recipient / bot_generation for conversation & background_run deliveries
                    if weixin_source_kind == "inbound" and weixin_source_row is not None:
                        src_aid = (
                            str(weixin_source_row["weixin_account_id"])
                            if weixin_source_row.get("weixin_account_id")
                            else None
                        )
                        src_bot_id = weixin_source_row.get("weixin_context_bot_id")
                        src_gen = weixin_source_row.get("weixin_context_bot_generation")
                        src_raw = weixin_source_row.get("raw_payload") or {}
                        src_puid = (
                            str(src_raw.get("platform_user_id") or "").strip()
                            if isinstance(src_raw, dict)
                            else ""
                        )
                        if not src_aid or src_gen is None:
                            return await _block_ambiguous_weixin_delivery(
                                "source_generation_unknown",
                                "Legacy Weixin inbound event lacks verifiable source account or bot generation.",
                                {
                                    "source_kind": "inbound",
                                    "source_account_id": src_aid,
                                    "source_bot_id": src_bot_id,
                                    "source_recipient_id": src_puid or None,
                                },
                            )
                        if src_aid != cur_aid or (src_puid and src_puid != cur_wx_uid):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "weixin_account_rebound",
                                "message": "Weixin account was rebound after the source message.",
                            }
                        if int(src_gen) != cur_bot_gen or (
                            src_bot_id and str(src_bot_id) != cur_bot_id
                        ):
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "weixin_bot_generation_superseded",
                                "message": "Weixin bot generation was superseded by a rescan.",
                            }

                    elif weixin_source_kind == "unmatched_run":
                        return await _block_ambiguous_weixin_delivery(
                            "source_generation_unknown",
                            "Weixin conversation delivery has no persisted per-turn inbound or background_run source matching run_id.",
                            {
                                "source_kind": "unmatched_run",
                                "run_id": cleaned_rid,
                                "thread_id": norm_tid,
                                "fallback_table": (
                                    str(weixin_source_row.get("_source_table") or "")
                                    if weixin_source_row
                                    else None
                                ),
                            },
                        )

                    elif weixin_source_kind in ("background_run", "thread_watch") and weixin_source_row is not None:
                        src_aid = (
                            str(weixin_source_row["weixin_account_id"])
                            if weixin_source_row.get("weixin_account_id")
                            else None
                        )
                        src_recip = str(
                            weixin_source_row.get("weixin_recipient_id") or ""
                        ).strip()
                        src_gen = weixin_source_row.get("weixin_bot_generation")
                        if not src_aid or not src_recip or src_gen is None:
                            return await _block_ambiguous_weixin_delivery(
                                "source_generation_unknown",
                                "Background run or thread watch lacks verifiable Weixin source account, recipient, or bot generation.",
                                {
                                    "source_kind": weixin_source_kind,
                                    "source_account_id": src_aid,
                                    "source_recipient_id": src_recip or None,
                                    "run_id": cleaned_rid,
                                },
                            )
                        if src_aid != cur_aid or src_recip != cur_wx_uid:
                            return {
                                "claim_status": "denied",
                                "status": "failed",
                                "code": "weixin_account_rebound",
                                "message": "Weixin account was rebound after this watch run.",
                            }
                        if int(src_gen) != cur_bot_gen:
                            # Codex ①: pre-rescan background run rejected with old generation -> mark unknown + audit
                            return await _block_ambiguous_weixin_delivery(
                                "weixin_bot_generation_superseded",
                                "Weixin background run belongs to a pre-rescan bot generation.",
                                {
                                    "source_kind": weixin_source_kind,
                                    "source_account_id": src_aid,
                                    "source_recipient_id": src_recip,
                                    "source_bot_generation": int(src_gen),
                                    "current_bot_generation": cur_bot_gen,
                                    "run_id": cleaned_rid,
                                },
                            )

                    resolved_wx_account_id = cur_aid
                    resolved_wx_bot_id = cur_bot_id
                    resolved_wx_bot_gen = cur_bot_gen
                    resolved_recipient_id = cur_wx_uid

                    enc_key = get_encryption_key()
                    weixin_ctx_token, weixin_ctx_token_version, is_stale = (
                        await wx_db.resolve_weixin_account_context_token_in_tx(
                            cur,
                            account_id=cur_aid,
                            bot_generation=cur_bot_gen,
                            platform_user_id=cur_wx_uid,
                            enc_key=enc_key,
                        )
                    )
                    if is_stale:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "weixin_context_token_stale",
                            "message": "Weixin context_token is stale; ask user to send a message to reactivate.",
                        }
                    if not weixin_ctx_token:
                        return {
                            "claim_status": "denied",
                            "status": "failed",
                            "code": "missing_weixin_context_token",
                            "message": "No valid Weixin context_token for recipient under current bot session.",
                        }

                # 3. Atomic claim. Scope and generation stay as first stored.
                # confirmed_thread_address and discord_body_phase are omitted so a
                # reclaim keeps the first confirmed Discord thread and its body phase.
                # delivery_attempt is a SQL literal on insert and an increment on
                # conflict, not another bind parameter. A failed conversation
                # delivery reclaimed as replay persists activity_origin='replay' so
                # a later progress update does not extend last_conversation_activity_at.
                next_attempt = 1 if existing is None else _delivery_attempt(existing) + 1
                await cur.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, thread_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, total_chunks, sent_chunks, platform_message_ids,
                        files_status, source_scope_key, source_generation, activity_origin,
                        delivery_attempt,
                        weixin_account_id, weixin_bot_id, weixin_bot_generation, weixin_recipient_id,
                        created_at, updated_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        'in_flight', %s, 0, '[]'::jsonb,
                        '[]'::jsonb, %s, %s, %s,
                        1,
                        %s::uuid, %s, %s, %s,
                        NOW(), NOW()
                    )
                    ON CONFLICT (delivery_key) DO UPDATE SET
                        status = 'in_flight',
                        is_current = EXCLUDED.is_current,
                        has_non_empty_text = EXCLUDED.has_non_empty_text,
                        platform = EXCLUDED.platform,
                        target_address = EXCLUDED.target_address,
                        total_chunks = EXCLUDED.total_chunks,
                        error_code = NULL,
                        error_message = NULL,
                        delivery_attempt = dalton_channel_deliveries.delivery_attempt + 1,
                        weixin_account_id = COALESCE(EXCLUDED.weixin_account_id, dalton_channel_deliveries.weixin_account_id),
                        weixin_bot_id = COALESCE(EXCLUDED.weixin_bot_id, dalton_channel_deliveries.weixin_bot_id),
                        weixin_bot_generation = COALESCE(EXCLUDED.weixin_bot_generation, dalton_channel_deliveries.weixin_bot_generation),
                        weixin_recipient_id = COALESCE(EXCLUDED.weixin_recipient_id, dalton_channel_deliveries.weixin_recipient_id),
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
                        resolved_wx_account_id,
                        resolved_wx_bot_id,
                        resolved_wx_bot_gen,
                        resolved_recipient_id if resolved_platform == "weixin" else None,
                        replay_recovery,
                    ),
                )

                home_ws_id = (
                    get_home_workspace_id(resolved_user_id)
                    if resolved_user_id
                    and not str(resolved_user_id).startswith("unlinked:")
                    else None
                )
                resp_claim: dict[str, Any] = {
                    "claim_status": "claimed",
                    "recipient_id": resolved_recipient_id,
                    "platform": resolved_platform,
                    "target_address": resolved_target_address,
                    "confirmed_thread_address": _delivery_confirmed_thread_address(existing),
                    "discord_body_phase": _delivery_discord_body_phase(existing),
                    "delivery_attempt": next_attempt,
                    "thread_scope_key": resolved_thread_scope_key,
                    "is_current": is_current,
                    "home_workspace_id": home_ws_id,
                    "weixin_context_token": weixin_ctx_token,
                }
                if resolved_platform == "weixin":
                    resp_claim["weixin_account_id"] = resolved_wx_account_id
                    resp_claim["weixin_bot_id"] = resolved_wx_bot_id
                    resp_claim["weixin_bot_generation"] = resolved_wx_bot_gen
                    resp_claim["weixin_context_token_version"] = weixin_ctx_token_version
                return resp_claim


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
    outcome: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    confirmed_thread_address: str | None = None,
    discord_body_phase: str | None = None,
    delivery_attempt: int | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
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
            outcome=outcome,
            error_code=error_code,
            error_message=error_message,
            confirmed_thread_address=confirmed_thread_address,
            discord_body_phase=discord_body_phase,
            delivery_attempt=delivery_attempt,
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
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
    outcome: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    confirmed_thread_address: str | None = None,
    discord_body_phase: str | None = None,
    delivery_attempt: int | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_key = str(delivery_key or "").strip()
    if not cleaned_key:
        raise HTTPException(status_code=400, detail="delivery_key is required")

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    gateway_epoch,
                    required=require_gateway_epoch,
                    enforce_post_lease_for_non_weixin=False,
                )
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
                reject_company_system_auth(pre_row.get("user_id"))

                locked_session_row: dict[str, Any] | None = None
                db_now: datetime | None = None
                src_uid = str(pre_row.get("user_id") or "").strip()
                src_plat = str(pre_row.get("platform") or "").strip().lower()
                require_channel_platform_capability(src_plat)
                if src_plat == "weixin":
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        platform="weixin",
                    )
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
                reject_company_system_auth(row.get("user_id"))

                locked_uid = str(row.get("user_id") or "").strip()
                locked_plat = str(row.get("platform") or "").strip().lower()
                locked_scope = str(row.get("source_scope_key") or "").strip()
                locked_gen = row.get("source_generation")
                locked_origin = (
                    str(row.get("activity_origin") or "none").strip().lower()
                )

                prev_status = str(row["status"])
                # A named generation must still be the current one before any
                # column changes. Omitted generations keep the previous update
                # rules for callers that have not adopted the token.
                current_attempt = _delivery_attempt(row)
                requested_attempt = _requested_delivery_attempt(delivery_attempt)
                if (
                    requested_attempt is not None
                    and requested_attempt != current_attempt
                ):
                    _reject_delivery_update(
                        409, "delivery_attempt is no longer the current attempt"
                    )
                # R11 (Codex #3): Prevent I-7 callers from forging or overwriting error_code
                # to bypass Discord unknown protection (e.g. writing 'gateway_restarted' on
                # an existing unknown row or onto a Discord delivery).
                if (
                    locked_plat == "discord"
                    and error_code is not None
                    and str(error_code).strip().lower() == "gateway_restarted"
                ):
                    _reject_delivery_update(
                        409,
                        "Delivery error_code 'gateway_restarted' is reserved for backend startup recovery",
                    )
                # R10 (Codex #5): Centralized delivery status transition validation &
                # Discord unknown protection (cannot be bypassed via outcome/status mismatch).
                locked_kind = str(row.get("kind") or "").strip().lower()
                new_status = _validate_delivery_status_transition(
                    prev_status=prev_status,
                    status=status,
                    outcome=outcome,
                    platform=locked_plat,
                    kind=locked_kind,
                    error_code=row.get("error_code"),
                    discord_body_phase=_delivery_discord_body_phase(row),
                    current_attempt=current_attempt,
                    requested_attempt=requested_attempt,
                )
                eff_error_code = error_code
                eff_error_message = error_message
                if prev_status == "unknown" and new_status == "unknown":
                    if (
                        error_code is not None
                        and str(error_code).strip()
                        != str(row.get("error_code") or "").strip()
                    ):
                        _reject_delivery_update(
                            409,
                            "Delivery error_code cannot be modified while status is unknown",
                        )
                    eff_error_code = None
                    eff_error_message = None
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
                # Write the thread and body phase before the status update. A later
                # replacement of first_response_json does not mention either column.
                # The first confirmed thread also records before_body when no phase
                # is stored yet. A rejected phase or status change rolls this
                # transaction back, so neither column changes on its own.
                stored_confirmed_thread = _delivery_confirmed_thread_address(row)
                if confirmed_thread_address is not None:
                    stored_confirmed_thread = await _store_confirmed_discord_thread(
                        cur,
                        row=row,
                        delivery_key=cleaned_key,
                        requested=confirmed_thread_address,
                        prev_status=prev_status,
                    )
                stored_body_phase = _delivery_discord_body_phase(row)
                if discord_body_phase is not None:
                    stored_body_phase = await _store_discord_body_phase(
                        cur,
                        row=row,
                        delivery_key=cleaned_key,
                        requested=discord_body_phase,
                        prev_status=prev_status,
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
                        error_code = COALESCE(%s::varchar, error_code),
                        error_message = COALESCE(%s::text, error_message),
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
                        eff_error_code,
                        eff_error_message,
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
                    "confirmed_thread_address": stored_confirmed_thread,
                    "discord_body_phase": stored_body_phase,
                    "delivery_attempt": current_attempt,
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
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
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
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
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
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    effective_run_id = str(run_id).strip() if run_id else None
    cleaned_p = platform.strip().lower() if platform and platform.strip() else None
    cleaned_mid = message_id.strip() if message_id and message_id.strip() else None
    if cleaned_p:
        require_channel_platform_capability(cleaned_p)
    if thread_watch and thread_watch.get("platform"):
        require_channel_platform_capability(str(thread_watch["platform"]))
    if background_run and background_run.get("platform"):
        require_channel_platform_capability(str(background_run["platform"]))

    eff_plat_hint = (
        cleaned_p
        or (
            str(thread_watch["platform"]).strip().lower()
            if isinstance(thread_watch, dict) and thread_watch.get("platform")
            else None
        )
        or (
            str(background_run["platform"]).strip().lower()
            if isinstance(background_run, dict) and background_run.get("platform")
            else None
        )
        or (
            str(session_interrupt_update["platform"]).strip().lower()
            if isinstance(session_interrupt_update, dict)
            and session_interrupt_update.get("platform")
            else None
        )
    )

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    gateway_epoch,
                    required=require_gateway_epoch,
                    platform=eff_plat_hint,
                    enforce_post_lease_for_non_weixin=False,
                )
                # R9 (Decision #1): Resolve authoritative DB platform BEFORE any write to
                # thread_watches, session_interrupt_update, or background_runs.
                # - Existing rows: DB platform is authoritative; if DB row is 'weixin', enforce
                #   valid X-Gateway-Epoch regardless of caller payload; if caller platform differs
                #   from DB platform, reject 409 platform_mismatch with zero mutation.
                # - New rows: derive platform from persisted conversation source (inbound_events /
                #   thread_watches), reject 409 on mismatch, and enforce epoch when source is weixin.
                def _reject_platform_mismatch(msg: str) -> None:
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "platform_mismatch", "message": msg},
                    )

                async def _enforce_db_platform(
                    db_plat: str | None,
                    req_plat: str | None,
                    entity_label: str,
                ) -> str | None:
                    clean_db = str(db_plat or "").strip().lower() or None
                    clean_req = str(req_plat or "").strip().lower() or None
                    if clean_db:
                        require_channel_platform_capability(clean_db)
                        if clean_db == "weixin":
                            await wx_db.verify_gateway_epoch_in_tx(
                                cur,
                                gateway_epoch,
                                required=require_gateway_epoch,
                                platform="weixin",
                            )
                        if clean_req and clean_req != clean_db:
                            _reject_platform_mismatch(
                                f"Request platform {clean_req!r} does not match persisted {entity_label} platform {clean_db!r}."
                            )
                        return clean_db
                    if clean_req == "weixin":
                        await wx_db.verify_gateway_epoch_in_tx(
                            cur,
                            gateway_epoch,
                            required=require_gateway_epoch,
                            platform="weixin",
                        )
                    return clean_req

                _CHANNEL_PLATFORMS = ("weixin", "feishu", "discord")

                async def _lookup_persisted_thread_platform(
                    tid_val: str | None,
                    rid_val: str | None = None,
                ) -> str | None:
                    if tid_val:
                        await cur.execute(
                            """
                            SELECT platform FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            FOR SHARE
                            """,
                            (tid_val,),
                        )
                        w_p = await cur.fetchone()
                        if w_p and w_p.get("platform"):
                            return str(w_p["platform"]).strip().lower()
                    if rid_val:
                        await cur.execute(
                            """
                            SELECT platform FROM dalton_channel_background_runs
                            WHERE run_id = %s
                            FOR SHARE
                            """,
                            (rid_val,),
                        )
                        bg_p = await cur.fetchone()
                        if bg_p and bg_p.get("platform"):
                            return str(bg_p["platform"]).strip().lower()
                    if tid_val:
                        await cur.execute(
                            """
                            SELECT platform FROM dalton_channel_inbound_events
                            WHERE thread_id = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (tid_val,),
                        )
                        in_tp = await cur.fetchone()
                        if in_tp and in_tp.get("platform"):
                            return str(in_tp["platform"]).strip().lower()
                    if rid_val:
                        await cur.execute(
                            """
                            SELECT platform FROM dalton_channel_inbound_events
                            WHERE run_id = %s
                            ORDER BY created_at DESC
                            LIMIT 1
                            """,
                            (rid_val,),
                        )
                        in_rp = await cur.fetchone()
                        if in_rp and in_rp.get("platform"):
                            return str(in_rp["platform"]).strip().lower()
                    # R10/R11 (Codex #1): First-write platform derivation from conversation_threads.platform
                    # under FOR SHARE BEFORE consulting request (platform, message_id), so an unrelated
                    # inbound event can never shadow a persisted Weixin thread.
                    if tid_val:
                        await cur.execute(
                            """
                            SELECT platform FROM conversation_threads
                            WHERE conversation_thread_id = %s
                            FOR SHARE
                            """,
                            (tid_val,),
                        )
                        ct_p = await cur.fetchone()
                        if ct_p and ct_p.get("platform"):
                            cand_ct = str(ct_p["platform"]).strip().lower()
                            if cand_ct in _CHANNEL_PLATFORMS:
                                return cand_ct
                    if rid_val:
                        await cur.execute(
                            """
                            SELECT ct.platform
                            FROM conversation_responses cr
                            JOIN conversation_threads ct
                              ON ct.conversation_thread_id = cr.conversation_thread_id
                            WHERE cr.conversation_response_id = %s
                            LIMIT 1
                            FOR SHARE OF ct
                            """,
                            (rid_val,),
                        )
                        cr_p = await cur.fetchone()
                        if cr_p and cr_p.get("platform"):
                            cand_cr = str(cr_p["platform"]).strip().lower()
                            if cand_cr in _CHANNEL_PLATFORMS:
                                return cand_cr
                    # Only consult (cleaned_p, cleaned_mid) if it belongs to this thread/run (or has no conflicting binding).
                    if cleaned_p and cleaned_mid:
                        await cur.execute(
                            """
                            SELECT platform, thread_id, run_id FROM dalton_channel_inbound_events
                            WHERE platform = %s AND message_id = %s
                            """,
                            (cleaned_p, cleaned_mid),
                        )
                        in_p = await cur.fetchone()
                        if in_p and in_p.get("platform"):
                            in_tid = (
                                normalize_uuid(in_p.get("thread_id"))
                                if in_p.get("thread_id")
                                else None
                            )
                            in_rid = str(in_p.get("run_id") or "").strip() or None
                            tid_conflict = bool(tid_val and in_tid and in_tid != tid_val)
                            rid_conflict = bool(rid_val and in_rid and in_rid != rid_val)
                            if not tid_conflict and not rid_conflict:
                                return str(in_p["platform"]).strip().lower()
                    return None

                # Pre-check thread_watch, session_interrupt_update, and background_run DB platforms
                resolved_tw_plat: str | None = None
                if thread_watch:
                    pre_tw_tid = normalize_uuid(thread_watch.get("thread_id"))
                    pre_tw_req_plat = str(thread_watch.get("platform") or "").strip().lower() or None
                    db_tw_plat = await _lookup_persisted_thread_platform(pre_tw_tid, effective_run_id)
                    resolved_tw_plat = await _enforce_db_platform(
                        db_tw_plat, pre_tw_req_plat, "thread_watch"
                    )

                resolved_si_plat: str | None = None
                if session_interrupt_update:
                    pre_si_val = session_interrupt_update.get("pending_interrupt")
                    pre_si_tid = normalize_uuid(
                        session_interrupt_update.get("thread_id")
                        or (pre_si_val.get("thread_id") if isinstance(pre_si_val, dict) else None)
                        or thread_id
                        or (thread_watch.get("thread_id") if isinstance(thread_watch, dict) else None)
                    )
                    pre_si_rid = (
                        session_interrupt_update.get("run_id")
                        or (pre_si_val.get("run_id") if isinstance(pre_si_val, dict) else None)
                        or effective_run_id
                        or (background_run.get("run_id") if isinstance(background_run, dict) else None)
                    )
                    pre_si_req_plat = str(session_interrupt_update.get("platform") or "").strip().lower() or None
                    db_si_plat = await _lookup_persisted_thread_platform(
                        pre_si_tid, str(pre_si_rid).strip() if pre_si_rid else None
                    )
                    resolved_si_plat = await _enforce_db_platform(
                        db_si_plat, pre_si_req_plat, "session_interrupt_update"
                    )

                resolved_bg_plat: str | None = None
                if background_run:
                    pre_bg_rid = str(background_run.get("run_id") or "").strip() or None
                    pre_bg_tid = normalize_uuid(background_run.get("thread_id"))
                    pre_bg_req_plat = str(background_run.get("platform") or "").strip().lower() or None
                    db_bg_plat: str | None = None
                    if pre_bg_rid:
                        await cur.execute(
                            """
                            SELECT platform FROM dalton_channel_background_runs
                            WHERE run_id = %s
                            """,
                            (pre_bg_rid,),
                        )
                        ex_bg_p = await cur.fetchone()
                        if ex_bg_p and ex_bg_p.get("platform"):
                            db_bg_plat = str(ex_bg_p["platform"]).strip().lower()
                    if db_bg_plat is None:
                        db_bg_plat = await _lookup_persisted_thread_platform(pre_bg_tid, pre_bg_rid)
                    resolved_bg_plat = await _enforce_db_platform(
                        db_bg_plat, pre_bg_req_plat, "background_run"
                    )

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
                    si_plat = resolved_si_plat or str(session_interrupt_update["platform"]).strip().lower()
                    si_scope = str(session_interrupt_update["scope_key"]).strip()
                    si_uid = str(session_interrupt_update["dalton_user_id"]).strip()
                    reject_company_system_auth(si_uid)
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
                        reject_company_system_auth(persisted_src.get("dalton_user_id"))
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
                    # R10 (Codex #1): Re-verify target thread_watch under FOR SHARE after
                    # acquiring session lock (preserving session -> thread_watch lock order)
                    # so a concurrently committed Weixin watch rolls back this interrupt write.
                    if si_expected_tid:
                        await cur.execute(
                            """
                            SELECT platform FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            FOR SHARE
                            """,
                            (si_expected_tid,),
                        )
                        live_si_tw = await cur.fetchone()
                        if live_si_tw and live_si_tw.get("platform"):
                            await _enforce_db_platform(
                                live_si_tw["platform"], si_plat, "session_interrupt_update"
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
                    if cmd_pre:
                        reject_company_system_auth(cmd_pre.get("dalton_user_id"))
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
                    await cur.execute(
                        """
                        SELECT dalton_user_id FROM dalton_channel_inbound_events
                        WHERE platform = %s AND message_id = %s
                        """,
                        (cleaned_p, cleaned_mid),
                    )
                    pre_in_upd = await cur.fetchone()
                    if pre_in_upd:
                        reject_company_system_auth(pre_in_upd.get("dalton_user_id"))
                    sets: list[str] = ["updated_at = NOW()"]
                    params: list[Any] = []
                    if status is not None:
                        sets.append("status = %s")
                        params.append(status)
                        if cleaned_p == "weixin" and str(status).strip().lower() in (
                            "completed",
                            "ignored",
                            "failed",
                            "returned_unresubmitted",
                        ):
                            sets.append("encrypted_weixin_media_meta = NULL")
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
                        params.append(
                            Jsonb(
                                _sanitize_native_payload_dict(
                                    _strip_sensitive_from_raw_payload(raw_payload)
                                )
                            )
                        )
                    elif raw_payload_patch is not None:
                        sets.append(
                            "raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s"
                        )
                        params.append(
                            Jsonb(
                                _sanitize_native_payload_dict(
                                    _strip_sensitive_from_raw_payload(
                                        raw_payload_patch
                                    )
                                )
                            )
                        )

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
                    reject_company_system_auth(tw_uid)
                    tw_plat = resolved_tw_plat or str(thread_watch["platform"]).strip().lower()
                    tw_addr = str(thread_watch["target_address"]).strip()
                    tw_scope = str(thread_watch.get("scope_key") or "").strip()
                    raw_tw_gen = thread_watch.get("generation")
                    tw_gen = (
                        int(raw_tw_gen)
                        if raw_tw_gen is not None and int(raw_tw_gen) > 0
                        else 0
                    )
                    # R10 (Codex #1): Lock existing thread_watch row FOR UPDATE before writing
                    # and re-verify persisted platform (rolling back if a Weixin row committed
                    # after pre-check).
                    await cur.execute(
                        """
                        SELECT dalton_user_id, platform, target_address,
                               scope_key, generation, thread_id
                        FROM dalton_channel_thread_watches
                        WHERE thread_id = %s
                        FOR UPDATE
                        """,
                        (tw_tid,),
                    )
                    ex_tw = await cur.fetchone()
                    if ex_tw:
                        if ex_tw.get("platform"):
                            tw_plat = await _enforce_db_platform(
                                ex_tw["platform"], tw_plat, "thread_watch"
                            ) or tw_plat
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

                    tw_wx_aid: str | None = None
                    tw_wx_recip: str | None = None
                    tw_wx_gen: int | None = None
                    if tw_plat == "weixin":
                        await cur.execute(
                            """
                            SELECT account_id::text AS account_id, ilink_user_id, bot_generation
                            FROM dalton_channel_weixin_accounts
                            WHERE user_id = %s AND status = 'active'
                            """,
                            (tw_uid,),
                        )
                        tw_wx_row = await cur.fetchone()
                        if tw_wx_row:
                            tw_wx_aid = str(tw_wx_row["account_id"])
                            tw_wx_recip = tw_wx_row.get("ilink_user_id")
                            tw_wx_gen = int(tw_wx_row["bot_generation"])

                    if has_tw_int:
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_thread_watches (
                                thread_id, dalton_user_id, platform, target_address,
                                scope_key, generation, last_seen_turn_index, watch_active,
                                pending_interrupt,
                                weixin_account_id, weixin_recipient_id, weixin_bot_generation,
                                created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s, NOW(), NOW())
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
                                weixin_account_id = COALESCE(EXCLUDED.weixin_account_id, dalton_channel_thread_watches.weixin_account_id),
                                weixin_recipient_id = COALESCE(EXCLUDED.weixin_recipient_id, dalton_channel_thread_watches.weixin_recipient_id),
                                weixin_bot_generation = COALESCE(EXCLUDED.weixin_bot_generation, dalton_channel_thread_watches.weixin_bot_generation),
                                updated_at = NOW()
                            WHERE dalton_channel_thread_watches.platform = EXCLUDED.platform
                            RETURNING platform
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
                                tw_wx_aid,
                                tw_wx_recip,
                                tw_wx_gen,
                            ),
                        )
                    else:
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_thread_watches (
                                thread_id, dalton_user_id, platform, target_address,
                                scope_key, generation, last_seen_turn_index, watch_active,
                                weixin_account_id, weixin_recipient_id, weixin_bot_generation,
                                created_at, updated_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s, NOW(), NOW())
                            ON CONFLICT (thread_id) DO UPDATE SET
                                target_address = EXCLUDED.target_address,
                                scope_key = EXCLUDED.scope_key,
                                generation = EXCLUDED.generation,
                                last_seen_turn_index = GREATEST(
                                    dalton_channel_thread_watches.last_seen_turn_index,
                                    EXCLUDED.last_seen_turn_index
                                ),
                                watch_active = EXCLUDED.watch_active,
                                weixin_account_id = COALESCE(EXCLUDED.weixin_account_id, dalton_channel_thread_watches.weixin_account_id),
                                weixin_recipient_id = COALESCE(EXCLUDED.weixin_recipient_id, dalton_channel_thread_watches.weixin_recipient_id),
                                weixin_bot_generation = COALESCE(EXCLUDED.weixin_bot_generation, dalton_channel_thread_watches.weixin_bot_generation),
                                updated_at = NOW()
                            WHERE dalton_channel_thread_watches.platform = EXCLUDED.platform
                            RETURNING platform
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
                                tw_wx_aid,
                                tw_wx_recip,
                                tw_wx_gen,
                            ),
                        )
                    tw_written = await cur.fetchone()
                    if tw_written is None:
                        await cur.execute(
                            "SELECT platform FROM dalton_channel_thread_watches WHERE thread_id = %s",
                            (tw_tid,),
                        )
                        conf_tw = await cur.fetchone()
                        await _enforce_db_platform(
                            conf_tw["platform"] if conf_tw else None,
                            tw_plat,
                            "thread_watch",
                        )
                        _reject_platform_mismatch(
                            f"Concurrent thread_watch platform conflict on thread {tw_tid}."
                        )

                if watch_interrupt_patch is not None:
                    patch_tid, patch_int = watch_interrupt_patch
                    patch_req_plat = resolved_si_plat or (
                        str(session_interrupt_update.get("platform") or "").strip().lower()
                        if isinstance(session_interrupt_update, dict)
                        and session_interrupt_update.get("platform")
                        else None
                    )
                    # R11 (Codex #1 / Grok med): Lock watch row FOR UPDATE first, read its
                    # authoritative DB platform, and enforce epoch + platform match before updating.
                    await cur.execute(
                        """
                        SELECT platform
                        FROM dalton_channel_thread_watches
                        WHERE thread_id = %s
                        FOR UPDATE
                        """,
                        (patch_tid,),
                    )
                    locked_watch_for_patch = await cur.fetchone()
                    if locked_watch_for_patch is not None:
                        locked_patch_plat = await _enforce_db_platform(
                            locked_watch_for_patch.get("platform"),
                            patch_req_plat,
                            "session_interrupt_update",
                        )
                        await cur.execute(
                            """
                            UPDATE dalton_channel_thread_watches
                            SET pending_interrupt = %s,
                                updated_at = NOW()
                            WHERE thread_id = %s
                              AND platform = %s
                            """,
                            (
                                Jsonb(patch_int) if patch_int is not None else None,
                                patch_tid,
                                locked_patch_plat,
                            ),
                        )

                # 4. Update dalton_channel_background_runs (inherit Weixin account/recipient/bot_generation from thread_watches, §3.2 #4)
                if background_run:
                    bg_rid = str(background_run["run_id"]).strip()
                    bg_tid = normalize_uuid(background_run["thread_id"])
                    bg_uid = str(background_run["dalton_user_id"]).strip()
                    reject_company_system_auth(bg_uid)
                    bg_plat = resolved_bg_plat or str(background_run["platform"]).strip().lower()
                    bg_addr = str(background_run["target_address"]).strip()
                    bg_tidx = background_run.get("turn_index")
                    bg_status = str(background_run.get("status") or "streaming")
                    bg_fb = bool(background_run.get("fallback_pending", True))

                    bg_wx_aid: str | None = (
                        normalize_uuid(background_run.get("weixin_account_id"))
                        if bg_plat == "weixin" and background_run.get("weixin_account_id")
                        else None
                    )
                    bg_wx_recip: str | None = (
                        str(background_run.get("weixin_recipient_id") or "").strip() or None
                        if bg_plat == "weixin"
                        else None
                    )
                    bg_wx_gen: int | None = None
                    if bg_plat == "weixin":
                        # R5 (Decision #4): ALWAYS compute authoritative Weixin source
                        # fields from DB (thread_watches + accounts + conversation_queries
                        # turn creation timestamp vs bot_generation_updated_at), NEVER
                        # trusting caller-supplied weixin_bot_generation blindly!
                        watch_gen: int | None = None
                        if bg_tid:
                            await cur.execute(
                                """
                                SELECT weixin_account_id::text AS weixin_account_id,
                                       weixin_recipient_id,
                                       weixin_bot_generation
                                FROM dalton_channel_thread_watches
                                WHERE thread_id = %s AND platform = 'weixin'
                                """,
                                (bg_tid,),
                            )
                            w_src = await cur.fetchone()
                            if w_src:
                                if w_src.get("weixin_account_id"):
                                    bg_wx_aid = str(w_src["weixin_account_id"])
                                if w_src.get("weixin_recipient_id"):
                                    bg_wx_recip = str(w_src["weixin_recipient_id"])
                                if w_src.get("weixin_bot_generation") is not None:
                                    watch_gen = int(w_src["weixin_bot_generation"])

                        acc_g_row = None
                        if bg_wx_aid:
                            await cur.execute(
                                """
                                SELECT account_id::text AS account_id,
                                       ilink_user_id,
                                       bot_generation,
                                       prev_bot_generation,
                                       bot_generation_updated_at
                                FROM dalton_channel_weixin_accounts
                                WHERE account_id = %s::uuid
                                """,
                                (bg_wx_aid,),
                            )
                            acc_g_row = await cur.fetchone()
                        elif bg_uid:
                            await cur.execute(
                                """
                                SELECT account_id::text AS account_id,
                                       ilink_user_id,
                                       bot_generation,
                                       prev_bot_generation,
                                       bot_generation_updated_at
                                FROM dalton_channel_weixin_accounts
                                WHERE user_id = %s
                                """,
                                (bg_uid,),
                            )
                            acc_g_row = await cur.fetchone()
                            if acc_g_row:
                                if bg_wx_aid is None and acc_g_row.get("account_id"):
                                    bg_wx_aid = str(acc_g_row["account_id"])
                                if bg_wx_recip is None and acc_g_row.get("ilink_user_id"):
                                    bg_wx_recip = str(acc_g_row["ilink_user_id"])

                        if acc_g_row is not None:
                            acc_cur_gen = (
                                int(acc_g_row["bot_generation"])
                                if acc_g_row.get("bot_generation") is not None
                                else watch_gen
                            )
                            eff_cur_gen = watch_gen if watch_gen is not None else acc_cur_gen
                            acc_prev_gen = (
                                int(acc_g_row["prev_bot_generation"])
                                if acc_g_row.get("prev_bot_generation") is not None
                                else None
                            )
                            acc_gen_updated_at = wx_db._coerce_utc(
                                acc_g_row.get("bot_generation_updated_at")
                            )
                            if acc_gen_updated_at is None:
                                bg_wx_gen = eff_cur_gen
                            else:
                                # Prefer DB conversation_queries.created_at over any caller timestamp
                                turn_created_at: datetime | None = None
                                if bg_rid:
                                    await cur.execute(
                                        """
                                        SELECT q.created_at AS turn_created_at
                                        FROM conversation_queries q
                                        JOIN conversation_responses r
                                          ON r.conversation_thread_id = q.conversation_thread_id
                                         AND r.turn_index = q.turn_index
                                        WHERE r.conversation_response_id = %s
                                        LIMIT 1
                                        """,
                                        (bg_rid,),
                                    )
                                    tq_row = await cur.fetchone()
                                    if tq_row:
                                        turn_created_at = wx_db._coerce_utc(
                                            tq_row.get("turn_created_at")
                                        )
                                if turn_created_at is None and bg_tid and bg_tidx is not None:
                                    await cur.execute(
                                        """
                                        SELECT created_at AS turn_created_at
                                        FROM conversation_queries
                                        WHERE conversation_thread_id = %s
                                          AND turn_index = %s
                                        LIMIT 1
                                        """,
                                        (bg_tid, int(bg_tidx)),
                                    )
                                    tq_row2 = await cur.fetchone()
                                    if tq_row2:
                                        turn_created_at = wx_db._coerce_utc(
                                            tq_row2.get("turn_created_at")
                                        )
                                if turn_created_at is not None and eff_cur_gen is not None:
                                    if turn_created_at < acc_gen_updated_at:
                                        bg_wx_gen = (
                                            acc_prev_gen
                                            if acc_prev_gen is not None
                                            else max(1, eff_cur_gen - 1)
                                        )
                                    else:
                                        bg_wx_gen = eff_cur_gen
                                else:
                                    # Cannot verify DB turn creation timestamp against rescan ->
                                    # fail closed (None) so I-6 marks unknown + writes audit.
                                    bg_wx_gen = None
                        else:
                            bg_wx_gen = watch_gen
                    else:
                        bg_wx_aid = None
                        bg_wx_recip = None
                        bg_wx_gen = None

                    # R10 (Codex #1): Lock existing background_run FOR UPDATE and enforce
                    # platform condition on ON CONFLICT DO UPDATE so a concurrently committed
                    # Weixin row can never be overwritten by a spoofed non-Weixin request.
                    if bg_tid:
                        await cur.execute(
                            """
                            SELECT platform FROM dalton_channel_thread_watches
                            WHERE thread_id = %s
                            FOR SHARE
                            """,
                            (bg_tid,),
                        )
                        live_bg_tw = await cur.fetchone()
                        if live_bg_tw and live_bg_tw.get("platform"):
                            bg_plat = await _enforce_db_platform(
                                live_bg_tw["platform"], bg_plat, "background_run"
                            ) or bg_plat
                    await cur.execute(
                        """
                        SELECT platform FROM dalton_channel_background_runs
                        WHERE run_id = %s
                        FOR UPDATE
                        """,
                        (bg_rid,),
                    )
                    live_bg_row = await cur.fetchone()
                    if live_bg_row and live_bg_row.get("platform"):
                        bg_plat = await _enforce_db_platform(
                            live_bg_row["platform"], bg_plat, "background_run"
                        ) or bg_plat

                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_background_runs (
                            run_id, thread_id, dalton_user_id, platform, target_address,
                            turn_index, status, fallback_pending,
                            weixin_account_id, weixin_recipient_id, weixin_bot_generation,
                            created_at, updated_at
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::uuid, %s, %s, NOW(), NOW())
                        ON CONFLICT (run_id) DO UPDATE SET
                            turn_index = COALESCE(EXCLUDED.turn_index, dalton_channel_background_runs.turn_index),
                            status = EXCLUDED.status,
                            fallback_pending = EXCLUDED.fallback_pending,
                            weixin_account_id = COALESCE(dalton_channel_background_runs.weixin_account_id, EXCLUDED.weixin_account_id),
                            weixin_recipient_id = COALESCE(dalton_channel_background_runs.weixin_recipient_id, EXCLUDED.weixin_recipient_id),
                            weixin_bot_generation = COALESCE(dalton_channel_background_runs.weixin_bot_generation, EXCLUDED.weixin_bot_generation),
                            updated_at = NOW()
                        WHERE dalton_channel_background_runs.platform = EXCLUDED.platform
                        RETURNING platform
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
                            bg_wx_aid,
                            bg_wx_recip,
                            bg_wx_gen,
                        ),
                    )
                    bg_written = await cur.fetchone()
                    if bg_written is None:
                        await cur.execute(
                            "SELECT platform FROM dalton_channel_background_runs WHERE run_id = %s",
                            (bg_rid,),
                        )
                        conf_bg = await cur.fetchone()
                        await _enforce_db_platform(
                            conf_bg["platform"] if conf_bg else None,
                            bg_plat,
                            "background_run",
                        )
                        _reject_platform_mismatch(
                            f"Concurrent background_run platform conflict on run {bg_rid}."
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
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
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
            gateway_epoch=gateway_epoch,
            require_gateway_epoch=require_gateway_epoch,
            conn=c,
        ),
    )


async def _reconcile_steer_once(
    *,
    platform: str,
    message_id: str,
    run_id: str | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_platform = str(platform or "").strip().lower()
    cleaned_mid = str(message_id or "").strip()

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await wx_db.verify_gateway_epoch_in_tx(
                    cur,
                    gateway_epoch,
                    required=require_gateway_epoch,
                    platform=cleaned_platform,
                    enforce_post_lease_for_non_weixin=False,
                )
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
                reject_company_system_auth(in_row.get("dalton_user_id"))

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
    """Scan `conversation_queries` (`type IN ('system', 'market_target', 'wakeup')`) > `last_seen_turn_index` and insert into `dalton_channel_background_runs`.

    For Weixin watches (Codex ①), compares each turn's registration time (`q.created_at`)
    against `dalton_channel_weixin_accounts.bot_generation_updated_at`: turns registered
    before a rescan confirmation retain `prev_bot_generation` (so `I-6` blocks them as
    superseded and writes audit), while turns registered at or after rescan confirmation
    inherit the watch's updated `weixin_bot_generation`.
    """
    for watch in watch_rows:
        w_tid = watch["thread_id"]
        w_uid = str(watch["dalton_user_id"])
        w_plat = str(watch["platform"])
        w_addr = str(watch["target_address"])
        last_turn = int(watch.get("last_seen_turn_index") or 0)

        w_wx_aid = (
            str(watch["weixin_account_id"])
            if watch.get("weixin_account_id")
            else None
        )
        w_wx_recip = (
            str(watch["weixin_recipient_id"]).strip()
            if watch.get("weixin_recipient_id")
            else None
        )
        w_wx_gen = (
            int(watch["weixin_bot_generation"])
            if watch.get("weixin_bot_generation") is not None
            else None
        )
        prev_bot_gen: int | None = None
        gen_updated_at: datetime | None = None
        if w_plat == "weixin" and w_wx_aid:
            await cur.execute(
                """
                SELECT prev_bot_generation, bot_generation_updated_at
                FROM dalton_channel_weixin_accounts
                WHERE account_id = %s::uuid
                """,
                (w_wx_aid,),
            )
            acc_meta = await cur.fetchone()
            if acc_meta:
                if acc_meta.get("prev_bot_generation") is not None:
                    prev_bot_gen = int(acc_meta["prev_bot_generation"])
                gen_updated_at = wx_db._coerce_utc(acc_meta.get("bot_generation_updated_at"))

        await cur.execute(
            """
            SELECT r.conversation_response_id AS run_id,
                   q.turn_index,
                   q.created_at AS turn_created_at
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
            eff_bg_wx_gen = w_wx_gen
            if w_plat == "weixin" and gen_updated_at is not None:
                turn_dt = wx_db._coerce_utc(fr.get("turn_created_at"))
                if turn_dt is not None and turn_dt < gen_updated_at:
                    if prev_bot_gen is not None:
                        eff_bg_wx_gen = prev_bot_gen
                    elif w_wx_gen is not None and w_wx_gen > 1:
                        eff_bg_wx_gen = w_wx_gen - 1

            await cur.execute(
                """
                INSERT INTO dalton_channel_background_runs (
                    run_id, thread_id, dalton_user_id, platform, target_address,
                    turn_index, status, fallback_pending,
                    weixin_account_id, weixin_recipient_id, weixin_bot_generation,
                    created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'streaming', TRUE, %s::uuid, %s, %s, NOW(), NOW())
                ON CONFLICT (run_id) DO NOTHING
                """,
                (
                    str(fr["run_id"]),
                    w_tid,
                    w_uid,
                    w_plat,
                    w_addr,
                    t_idx,
                    w_wx_aid if w_plat == "weixin" else None,
                    w_wx_recip if w_plat == "weixin" else None,
                    eff_bg_wx_gen if w_plat == "weixin" else None,
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


async def _enrich_recovery_scope_keys(
    cur: Any,
    background_runs: list[dict[str, Any]],
    thread_watches: list[dict[str, Any]],
) -> None:
    """Populate `scope_key` and `thread_scope_key` on recovered background runs and thread watches."""
    for w in thread_watches:
        resolved_w_scope = await _resolve_thread_scope_key_for_thread(
            cur,
            str(w["thread_id"]) if w.get("thread_id") else None,
            fallback_scope_key=str(w["scope_key"]) if w.get("scope_key") else None,
            target_address=str(w["target_address"]) if w.get("target_address") else None,
        )
        if resolved_w_scope:
            w["scope_key"] = resolved_w_scope
            w["thread_scope_key"] = resolved_w_scope

    for bg in background_runs:
        resolved_bg_scope = await _resolve_thread_scope_key_for_thread(
            cur,
            str(bg["thread_id"]) if bg.get("thread_id") else None,
            fallback_scope_key=(
                str(bg.get("thread_scope_key") or bg.get("scope_key") or "").strip()
                or None
            ),
            target_address=str(bg["target_address"]) if bg.get("target_address") else None,
        )
        bg["scope_key"] = resolved_bg_scope
        bg["thread_scope_key"] = resolved_bg_scope


def _platform_enabled_for_recovery(platform: str | None) -> bool:
    cleaned = str(platform or "").strip().lower()
    if cleaned == "feishu":
        return has_capability(CapabilityId.CHANNEL_FEISHU)
    if cleaned == "discord":
        return has_capability(CapabilityId.CHANNEL_DISCORD)
    if cleaned in ("weixin", "wechat"):
        return has_capability(CapabilityId.CHANNEL_WEIXIN)
    return True


async def recover_channel_state(
    *,
    mode: str = "startup",
    thread_id: str | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """I-7 (4): Recover channel gateway state on `startup` or `watch_reconnect` (§3.4, Codex ③, ④)."""
    for attempt in range(1, _TX_RETRY_MAX_ATTEMPTS + 1):
        try:
            return await _run_tx_with_retry(
                "recover_channel_state",
                conn,
                lambda c: _recover_channel_state_once(
                    mode=mode,
                    thread_id=thread_id,
                    gateway_epoch=gateway_epoch,
                    require_gateway_epoch=require_gateway_epoch,
                    conn=c,
                ),
            )
        except wx_db.WeixinLockSetChanged:
            if attempt >= _TX_RETRY_MAX_ATTEMPTS:
                raise
            await asyncio.sleep(0.02 * attempt)
    raise RuntimeError("recover_channel_state retry loop exhausted")


async def _recover_channel_state_once(
    *,
    mode: str = "startup",
    thread_id: str | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_mode = str(mode or "startup").strip().lower()
    if cleaned_mode not in ("startup", "watch_reconnect"):
        raise HTTPException(status_code=400, detail=f"Unsupported recover mode: {mode!r}")
    norm_tid = normalize_uuid(thread_id) if thread_id else None
    weixin_on = has_capability(CapabilityId.CHANNEL_WEIXIN)

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await wx_db.verify_gateway_epoch_in_tx(
                    cur, gateway_epoch, required=require_gateway_epoch
                )
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
                    watches = [
                        dict(r)
                        for r in await cur.fetchall()
                        if _platform_enabled_for_recovery(r.get("platform"))
                    ]
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
                    bg_runs = [
                        dict(r)
                        for r in await cur.fetchall()
                        if _platform_enabled_for_recovery(r.get("platform"))
                    ]
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_thread_watches
                        WHERE (%s::uuid IS NULL AND watch_active = TRUE)
                           OR thread_id = %s
                        """,
                        (norm_tid, norm_tid),
                    )
                    updated_watches = [
                        dict(r)
                        for r in await cur.fetchall()
                        if _platform_enabled_for_recovery(r.get("platform"))
                    ]
                    await _enrich_recovery_scope_keys(cur, bg_runs, updated_watches)
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
                            if (
                                bgr.get("generation") is None
                                and w_match.get("generation") is not None
                            ):
                                bgr["generation"] = w_match["generation"]
                    return {
                        "mode": "watch_reconnect",
                        "background_runs": _json_safe_rows(bg_runs),
                        "thread_watches": _json_safe_rows(updated_watches),
                    }

                # mode == "startup"
                # R7 (Decision #1): Align lock order strictly with unbind_weixin_account:
                #   1) dalton_channel_weixin_accounts (ORDER BY account_id ASC FOR KEY SHARE)
                #      BEFORE locking thread_watches — so backfill INSERT into
                #      dalton_channel_background_runs (FK -> weixin_accounts KEY SHARE)
                #      NEVER deadlocks against unbind's accounts FOR UPDATE -> watches FOR UPDATE!
                #   2) dalton_channel_thread_watches (ORDER BY thread_id ASC FOR UPDATE)
                #   3) dalton_channel_deliveries (ORDER BY delivery_key ASC FOR UPDATE SKIP LOCKED)
                #   4) dalton_channel_inbound_events repairs (ORDER BY platform, message_id FOR UPDATE SKIP LOCKED)
                # R5 (Decision #7): When channel_weixin is disabled, NEVER touch or mutate ANY
                # Weixin accounts, watches, deliveries, or inbound events.
                if weixin_on:
                    # R9 (Decision #8): Eliminate lock gap between weixin_accounts and thread_watches.
                    # 1) Lock existing accounts FOR KEY SHARE in ascending account_id order.
                    # 2) Lock ONLY active watches whose weixin_account_id is NULL or already in
                    #    locked_aids, so no watch is ever locked before its account.
                    # 3) If any newly committed active watch references an unlocked account, raise
                    #    WeixinLockSetChanged so _run_tx_with_retry rolls back and retries cleanly.
                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id
                        FROM dalton_channel_weixin_accounts
                        ORDER BY account_id ASC
                        FOR KEY SHARE
                        """
                    )
                    locked_aids = [
                        str(r["account_id"])
                        for r in await cur.fetchall()
                        if r.get("account_id")
                    ]
                    if locked_aids:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_thread_watches
                            WHERE watch_active = TRUE
                              AND (weixin_account_id IS NULL OR weixin_account_id = ANY(%s::uuid[]))
                            ORDER BY thread_id ASC
                            FOR UPDATE
                            """,
                            (locked_aids,),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT * FROM dalton_channel_thread_watches
                            WHERE watch_active = TRUE
                              AND weixin_account_id IS NULL
                            ORDER BY thread_id ASC
                            FOR UPDATE
                            """
                        )
                    active_watches = [
                        dict(r)
                        for r in await cur.fetchall()
                        if _platform_enabled_for_recovery(r.get("platform"))
                    ]
                    if locked_aids:
                        await cur.execute(
                            """
                            SELECT 1
                            FROM dalton_channel_thread_watches
                            WHERE watch_active = TRUE
                              AND weixin_account_id IS NOT NULL
                              AND NOT (weixin_account_id = ANY(%s::uuid[]))
                            LIMIT 1
                            """,
                            (locked_aids,),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT 1
                            FROM dalton_channel_thread_watches
                            WHERE watch_active = TRUE
                              AND weixin_account_id IS NOT NULL
                            LIMIT 1
                            """
                        )
                    if await cur.fetchone() is not None:
                        raise wx_db.WeixinLockSetChanged()
                else:
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_thread_watches
                        WHERE watch_active = TRUE
                          AND platform <> 'weixin'
                        ORDER BY thread_id ASC
                        FOR UPDATE
                        """
                    )
                    active_watches = [
                        dict(r)
                        for r in await cur.fetchall()
                        if _platform_enabled_for_recovery(r.get("platform"))
                    ]
                await _backfill_watch_notification_runs_in_tx(cur, active_watches)

                # ① Mark lingering in_flight deliveries for ENABLED platforms as unknown / gateway_restarted
                # using FOR UPDATE SKIP LOCKED so rows concurrently locked by unbind FK SET NULL
                # never trigger 40P01.
                if weixin_on:
                    await cur.execute(
                        """
                        SELECT delivery_key
                        FROM dalton_channel_deliveries
                        WHERE status = 'in_flight'
                        ORDER BY delivery_key ASC
                        FOR UPDATE SKIP LOCKED
                        """
                    )
                else:
                    await cur.execute(
                        """
                        SELECT delivery_key
                        FROM dalton_channel_deliveries
                        WHERE status = 'in_flight'
                          AND platform <> 'weixin'
                        ORDER BY delivery_key ASC
                        FOR UPDATE SKIP LOCKED
                        """
                    )
                locked_in_flight_keys = [
                    str(r["delivery_key"])
                    for r in await cur.fetchall()
                    if r.get("delivery_key")
                ]
                settled_in_flight: list[dict[str, Any]] = []
                if locked_in_flight_keys:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_deliveries
                        SET status = 'unknown',
                            error_code = 'gateway_restarted',
                            updated_at = NOW()
                        WHERE delivery_key = ANY(%s::text[])
                          AND status = 'in_flight'
                        RETURNING delivery_key, user_id, platform,
                                  weixin_account_id::text AS weixin_account_id,
                                  weixin_bot_id, weixin_bot_generation, weixin_recipient_id
                        """,
                        (locked_in_flight_keys,),
                    )
                    settled_in_flight = [dict(r) for r in await cur.fetchall()]
                if weixin_on:
                    for d_item in settled_in_flight:
                        if str(d_item.get("platform") or "").lower() != "weixin":
                            continue
                        has_verifiable_src = bool(
                            d_item.get("weixin_account_id")
                            and d_item.get("weixin_bot_generation") is not None
                            and d_item.get("weixin_recipient_id")
                        )
                        if not has_verifiable_src:
                            d_uid = str(d_item.get("user_id") or "").strip() or None
                            if d_uid and d_uid.startswith("unlinked:"):
                                d_uid = None
                            await wx_db.write_weixin_audit_log_in_tx(
                                cur,
                                action="ambiguous_legacy_delivery_blocked",
                                operator_user_id="system:delivery-guard",
                                target_user_id=d_uid,
                                weixin_account_id=d_item.get("weixin_account_id"),
                                ilink_bot_id=d_item.get("weixin_bot_id"),
                                ilink_user_id=d_item.get("weixin_recipient_id"),
                                metadata={
                                    "delivery_key": str(d_item.get("delivery_key") or ""),
                                    "settled_by": "startup_recovery",
                                    "error_code": "gateway_restarted",
                                },
                            )

                # ② Heal invalid pending inbound events (SKIP LOCKED; skip platform='weixin' when disabled)
                if weixin_on:
                    await cur.execute(
                        """
                        WITH locked AS (
                            SELECT platform, message_id
                            FROM dalton_channel_inbound_events
                            WHERE status = 'pending'
                              AND (
                                COALESCE(raw_payload->>'invalid_inbound_event', '') IN ('true', 'True')
                                OR COALESCE(raw_payload->>'invalid_inbound', '') IN ('true', 'True')
                                OR raw_payload->>'rejected_reason' = 'invalid_inbound_event'
                                OR raw_payload->>'deny_reason' = 'invalid_inbound_event'
                              )
                            ORDER BY platform ASC, message_id ASC
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE dalton_channel_inbound_events ie
                        SET status = 'ignored',
                            raw_payload = COALESCE(ie.raw_payload, '{}'::jsonb)
                                || jsonb_build_object(
                                    'invalid_inbound_event', TRUE,
                                    'deny_reason', 'invalid_inbound_event'
                                ),
                            updated_at = NOW()
                        FROM locked l
                        WHERE ie.platform = l.platform
                          AND ie.message_id = l.message_id
                        """
                    )
                else:
                    await cur.execute(
                        """
                        WITH locked AS (
                            SELECT platform, message_id
                            FROM dalton_channel_inbound_events
                            WHERE status = 'pending'
                              AND platform <> 'weixin'
                              AND (
                                COALESCE(raw_payload->>'invalid_inbound_event', '') IN ('true', 'True')
                                OR COALESCE(raw_payload->>'invalid_inbound', '') IN ('true', 'True')
                                OR raw_payload->>'rejected_reason' = 'invalid_inbound_event'
                                OR raw_payload->>'deny_reason' = 'invalid_inbound_event'
                              )
                            ORDER BY platform ASC, message_id ASC
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE dalton_channel_inbound_events ie
                        SET status = 'ignored',
                            raw_payload = COALESCE(ie.raw_payload, '{}'::jsonb)
                                || jsonb_build_object(
                                    'invalid_inbound_event', TRUE,
                                    'deny_reason', 'invalid_inbound_event'
                                ),
                            updated_at = NOW()
                        FROM locked l
                        WHERE ie.platform = l.platform
                          AND ie.message_id = l.message_id
                        """
                    )

                # Heal any historical pending_cancel command rows that lack a pinned target run_id
                # (SKIP LOCKED; skip platform='weixin' when capability disabled)
                if weixin_on:
                    await cur.execute(
                        """
                        WITH locked AS (
                            SELECT platform, message_id
                            FROM dalton_channel_inbound_events
                            WHERE command_effect_status = 'pending_cancel'
                              AND (thread_id IS NULL OR run_id IS NULL OR btrim(run_id) = '')
                            ORDER BY platform ASC, message_id ASC
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE dalton_channel_inbound_events ie
                        SET command_effect_status = 'done',
                            updated_at = NOW()
                        FROM locked l
                        WHERE ie.platform = l.platform
                          AND ie.message_id = l.message_id
                        """
                    )
                else:
                    await cur.execute(
                        """
                        WITH locked AS (
                            SELECT platform, message_id
                            FROM dalton_channel_inbound_events
                            WHERE command_effect_status = 'pending_cancel'
                              AND platform <> 'weixin'
                              AND (thread_id IS NULL OR run_id IS NULL OR btrim(run_id) = '')
                            ORDER BY platform ASC, message_id ASC
                            FOR UPDATE SKIP LOCKED
                        )
                        UPDATE dalton_channel_inbound_events ie
                        SET command_effect_status = 'done',
                            updated_at = NOW()
                        FROM locked l
                        WHERE ie.platform = l.platform
                          AND ie.message_id = l.message_id
                        """
                    )

                # ③ Fetch all recovery sets (filtered by active capabilities; decrypt Weixin media meta)
                enc_key: str | None = None
                if weixin_on:
                    try:
                        enc_key = get_encryption_key()
                    except Exception:
                        enc_key = None

                if enc_key:
                    await cur.execute(
                        """
                        SELECT ie.*,
                               CASE
                                   WHEN ie.encrypted_weixin_media_meta IS NOT NULL
                                   THEN pgp_sym_decrypt(ie.encrypted_weixin_media_meta, %s)
                                   ELSE NULL
                               END AS decrypted_weixin_media_json
                        FROM dalton_channel_inbound_events ie
                        WHERE ie.status IN ('pending', 'accepted', 'streaming', 'steered', 'pending_resubmit')
                           OR ie.fallback_pending = TRUE
                           OR ie.command_reply_pending = TRUE
                           OR ie.command_effect_status = 'pending_cancel'
                        ORDER BY ie.created_at ASC
                        """,
                        (enc_key,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT ie.*
                        FROM dalton_channel_inbound_events ie
                        WHERE ie.status IN ('pending', 'accepted', 'streaming', 'steered', 'pending_resubmit')
                           OR ie.fallback_pending = TRUE
                           OR ie.command_reply_pending = TRUE
                           OR ie.command_effect_status = 'pending_cancel'
                        ORDER BY ie.created_at ASC
                        """
                    )
                inbound_events = [
                    dict(r)
                    for r in await cur.fetchall()
                    if not _payload_marks_invalid_inbound(r.get("raw_payload"))
                    and _platform_enabled_for_recovery(r.get("platform"))
                ]
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
                    # Attach decrypted Weixin media attachments to top-level AND raw_payload copy (§3.4 #2)
                    dec_json = r.pop("decrypted_weixin_media_json", None)
                    if dec_json:
                        try:
                            dec_atts = json.loads(dec_json)
                        except Exception:
                            dec_atts = None
                        if dec_atts is not None:
                            r["decrypted_weixin_attachments"] = dec_atts
                            raw_copy = (
                                dict(r.get("raw_payload") or {})
                                if isinstance(r.get("raw_payload"), dict)
                                else {}
                            )
                            raw_copy["weixin_attachments"] = dec_atts
                            r["_recovery_raw_payload_with_weixin_attachments"] = raw_copy
                    cleaned_inbound_events.append(r)
                inbound_events = cleaned_inbound_events

                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_background_runs
                    WHERE fallback_pending = TRUE
                    ORDER BY created_at ASC
                    """
                )
                background_runs = [
                    dict(r)
                    for r in await cur.fetchall()
                    if _platform_enabled_for_recovery(r.get("platform"))
                ]

                await cur.execute(
                    """
                    SELECT * FROM dalton_channel_thread_watches
                    WHERE watch_active = TRUE
                    ORDER BY created_at ASC
                    """
                )
                thread_watches = [
                    dict(r)
                    for r in await cur.fetchall()
                    if _platform_enabled_for_recovery(r.get("platform"))
                ]
                await _enrich_recovery_scope_keys(cur, background_runs, thread_watches)
                watch_by_tid = {
                    str(w["thread_id"]): w for w in thread_watches if w.get("thread_id")
                }
                for bgr in background_runs:
                    w_match = watch_by_tid.get(str(bgr.get("thread_id") or ""))
                    if w_match:
                        if not bgr.get("scope_key") and w_match.get("scope_key"):
                            bgr["scope_key"] = w_match["scope_key"]
                        if (
                            bgr.get("generation") is None
                            and w_match.get("generation") is not None
                        ):
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
        recovery_raw_override = row.get("_recovery_raw_payload_with_weixin_attachments")
        for k, v in row.items():
            if k in (
                "encrypted_weixin_context_token",
                "encrypted_bot_token",
                "encrypted_weixin_media_meta",
                "_recovery_raw_payload_with_weixin_attachments",
            ):
                continue
            if isinstance(v, (bytes, bytearray, memoryview)):
                continue
            if k == "raw_payload" and isinstance(v, dict):
                sanitized = _sanitize_native_payload_dict(
                    _strip_sensitive_from_raw_payload(v)
                )
                if (
                    isinstance(recovery_raw_override, dict)
                    and "weixin_attachments" in recovery_raw_override
                ):
                    sanitized["weixin_attachments"] = recovery_raw_override[
                        "weixin_attachments"
                    ]
                item[k] = sanitized
            elif k in (
                "command_request",
                "command_result",
                "consumed_bind_result",
            ) and isinstance(v, dict):
                item[k] = _strip_sensitive_from_raw_payload(v)
            elif isinstance(v, UUID):
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
    reject_company_system_auth(cleaned_uid)
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
                    WHERE user_id = %s AND platform <> 'weixin'
                    """,
                    (cleaned_uid,),
                )
                identities = {
                    str(r["platform"]): dict(r) for r in await cur.fetchall()
                }
                if has_capability(CapabilityId.CHANNEL_WEIXIN):
                    await cur.execute(
                        """
                        SELECT ilink_user_id AS platform_user_id,
                               ilink_username AS platform_username
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                          AND status = 'active'
                          AND ilink_user_id IS NOT NULL
                        """,
                        (cleaned_uid,),
                    )
                    wx_acc_auto = await cur.fetchone()
                    if wx_acc_auto:
                        identities["weixin"] = {
                            "platform": "weixin",
                            "platform_user_id": str(wx_acc_auto["platform_user_id"]),
                            "platform_username": wx_acc_auto.get("platform_username"),
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
                reject_company_system_auth(row.get("user_id"))

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


