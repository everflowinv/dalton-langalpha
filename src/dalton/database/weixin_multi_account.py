"""Database persistence and domain logic for multi-account personal Weixin (`d049`).

Implements the single-source-of-truth backend layer specified in
``docs/dalton/features/weixin-multi-account.md``:

- Deployment-scoped single gateway instance lease & same-transaction ``FOR SHARE``
  epoch fencing (``dalton_channel_gateway_lease``).
- Runtime config (``dalton_channel_weixin_config``) with per-tier access evaluation
  (``evaluate_weixin_member_access``) and unified account re-evaluation across
  toggle re-opens and 15-second reconcile.
- Multi-account credentials, identity occupation (including ``prev_ilink_user_id``
  protection during rescan verification), cursor CAS, status updates, and
  single-instance slot capacity enforcement (default 200).
- QR login session lifecycle (``starting`` pre-write + conditional state updates +
  two-phase lock ordering + strict 5-field ``confirmed_result`` whitelist).
- Multi-account encrypted context tokens with monotonic dual watermarks and
  ``token_version`` stale-fencing.
- Audit logging with fixed non-empty default reasons across all 9 action types.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import datetime, timezone
import logging
import os
import random
import secrets
from typing import Any
from uuid import UUID

from fastapi import HTTPException
import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.config import env
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
from src.dalton.settings import (
    get_dalton_channel_gateway_url,
    get_dalton_config,
    get_weixin_max_concurrent_accounts,
    redact_weixin_secrets,
)
from src.server.database import pool as db_pool
from src.server.database.encryption import get_encryption_key
from src.server.utils.pg_sanitize import normalize_uuid

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants, Audit Defaults, Exceptions & Concurrency Test Hooks
# ---------------------------------------------------------------------------

STALE_GATEWAY_EPOCH_CODE = "stale_gateway_epoch"
WEIXIN_CAPACITY_FULL_CODE = "weixin_capacity_full"
LOGIN_SESSION_REVOKED_CODE = "login_session_revoked"
ILINK_USER_ALREADY_BOUND_CODE = "ilink_user_already_bound"
ILINK_USER_ALREADY_BOUND_MESSAGE = (
    "该微信号已关联团队内另一账号，请先在原账号解除连接后再试"
)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
DALTON_CHANNEL_NAMESPACE = UUID("5c8b6e20-8a1f-4e92-b52c-1d9a40c80001")

# Initialized empty per §5.2 until live iLink stale context_token errcodes are confirmed.
CONTEXT_TOKEN_STALE_ERRCODES: frozenset[str] = frozenset()

CONFIRMED_RESULT_WHITELIST: tuple[str, ...] = (
    "account_id",
    "connected",
    "already_connected",
    "bot_generation",
    "accounts_revision",
)

ADMIN_UNBIND_PRESET_REASONS: tuple[str, ...] = (
    "离职交接",
    "手机遗失应急",
    "误绑微信号",
    "其他原因",
)

AUDIT_DEFAULT_REASONS: dict[str, str] = {
    "admin_unbind": "管理员代为解除微信连接",
    "member_unbind": "成员本人主动解除微信连接",
    "admin_toggle_channel": "管理员更新团队个人微信接入开关",
    "admin_toggle_external": "管理员更新外部成员个人微信接入开关",
    "member_removed_revoke": "成员移出团队名册或角色移除，系统自动回收微信连接",
    "member_reinstated_restore": "成员恢复团队名册资格，系统自动恢复微信连接",
    "disabled_account_reclaimed": "在职成员扫码接管已离职成员释放的微信号",
    "conflict_auto_unbound": "首条消息核验发现微信号已被团队内其他成员关联，自动解除待验证连接",
    "ambiguous_legacy_delivery_blocked": "历史后台运行或投递缺少唯一来源账号与机器人代际，置为 unknown 并阻断外发",
}

_TX_RETRY_MAX_ATTEMPTS = 4
_TX_RETRY_BACKOFF_MIN_SECONDS = 0.02
_TX_RETRY_BACKOFF_MAX_SECONDS = 0.08
_USER_SETTINGS_PKEY_CONSTRAINT = "dalton_channel_user_settings_pkey"

# Optional async barrier hooks used only by real-Postgres interleaving tests (§7.1 #2, #3).
_after_lease_share_hook: Callable[[], Awaitable[None]] | None = None
_before_lease_update_hook: Callable[[], Awaitable[None]] | None = None
_after_discovery_hook: Callable[[], Awaitable[None]] | None = None
_after_user_locks_hook: Callable[[], Awaitable[None]] | None = None
_after_pending_verify_read_hook: Callable[[], Awaitable[None]] | None = None


class StaleGatewayEpochError(HTTPException):
    """Raised (HTTP 409) when ``X-Gateway-Epoch`` is missing, expired, or superseded."""

    def __init__(self, message: str = "Gateway lease epoch is stale or expired.") -> None:
        super().__init__(
            status_code=409,
            detail={
                "code": STALE_GATEWAY_EPOCH_CODE,
                "message": message,
            },
        )


class WeixinLockSetChanged(Exception):
    """Internal signal to roll back and re-acquire Level 1/3a locks over a wider set."""


class NeedsTwoPhaseVerificationLock(Exception):
    """Internal signal when ``I-1``/``I-3`` fast path hits ``pending_verification`` (D11)."""


def get_db_connection(existing_conn: Any = None):
    return db_pool.get_db_connection(existing_conn)


def mask_ilink_user_id(ilink_user_id: str | None) -> str | None:
    """Return last-4 masked Weixin user ID (e.g. ``****a1b2``) for admin views & audit."""
    cleaned = str(ilink_user_id or "").strip()
    if not cleaned:
        return None
    if len(cleaned) <= 4:
        return f"****{cleaned}"
    return f"****{cleaned[-4:]}"


def _coerce_utc(val: Any) -> datetime | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
    if isinstance(val, str):
        raw = val.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


def validate_admin_unbind_reason(
    reason: str | None,
    *,
    note: str | None = None,
) -> str:
    """Enforce preset-only enum for admin unbind reasons (no free-text reason or note)."""
    cleaned_note = str(note or "").strip()
    cleaned_reason = str(reason or "").strip()
    if cleaned_note or cleaned_reason not in ADMIN_UNBIND_PRESET_REASONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_admin_unbind_reason",
                "message": "管理员代解除必须选择预设原因（离职交接 / 手机遗失应急 / 误绑微信号 / 其他原因），不支持自由文本。",
            },
        )
    return cleaned_reason


def _normalize_audit_reason(action: str, reason: str | None) -> str:
    cleaned = str(reason or "").strip()
    if action == "admin_unbind":
        if cleaned in ADMIN_UNBIND_PRESET_REASONS:
            return cleaned
        return AUDIT_DEFAULT_REASONS["admin_unbind"]
    if action == "member_unbind":
        return AUDIT_DEFAULT_REASONS["member_unbind"]
    if not cleaned:
        cleaned = AUDIT_DEFAULT_REASONS.get(action, f"微信渠道操作：{action}")
    return cleaned[:500]


def _shape_confirmed_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {k: raw[k] for k in CONFIRMED_RESULT_WHITELIST if k in raw}


def _sorted_unique_strs(*groups: Any) -> list[str]:
    out: set[str] = set()
    for group in groups:
        if group is None:
            continue
        items = [group] if isinstance(group, str) else list(group)
        for item in items:
            s = str(item or "").strip()
            if s:
                out.add(s)
    return sorted(out)


def _is_retryable_tx_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            WeixinLockSetChanged,
            NeedsTwoPhaseVerificationLock,
            psycopg.errors.DeadlockDetected,
        ),
    ):
        return True
    if isinstance(exc, psycopg.errors.UniqueViolation):
        diag = getattr(exc, "diag", None)
        return getattr(diag, "constraint_name", None) == _USER_SETTINGS_PKEY_CONSTRAINT
    return False


async def _run_tx_with_retry(
    op_name: str,
    conn: Any,
    attempt_fn: Callable[[Any], Awaitable[Any]],
    *,
    max_attempts: int = _TX_RETRY_MAX_ATTEMPTS,
) -> Any:
    if conn is not None:
        return await attempt_fn(conn)
    for attempt in range(1, max_attempts + 1):
        try:
            return await attempt_fn(None)
        except Exception as exc:
            if not _is_retryable_tx_error(exc) or attempt >= max_attempts:
                raise
            logger.warning(
                "Retrying weixin multi-account tx %s after %s (attempt %d/%d)",
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


async def _fetch_db_now(cur: Any) -> datetime:
    await cur.execute("SELECT clock_timestamp() AS db_now")
    row = await cur.fetchone()
    if isinstance(row, Mapping):
        dt = _coerce_utc(row.get("db_now"))
        if dt is not None:
            return dt
    return datetime.now(timezone.utc)


def _default_weixin_app_settings() -> dict[str, Any]:
    return {
        "preferred": "weixin:@me",
        "chats": {},
        "automation_output": {},
        "agent_messages": {
            "enabled": True,
            "allowed": ["weixin:@me"],
        },
    }


def _bump_version_str(version: str | None) -> str:
    raw = str(version or "v1").strip()
    if raw.startswith("v") and raw[1:].isdigit():
        return f"v{int(raw[1:]) + 1}"
    if raw.isdigit():
        return f"v{int(raw) + 1}"
    return "v2"


# ---------------------------------------------------------------------------
# Level 0b: Deployment-Scoped Single Gateway Lease & FOR SHARE Epoch Fence
# ---------------------------------------------------------------------------


async def acquire_gateway_lease(
    *,
    instance_id: str,
    ttl_seconds: int = 45,
    conn: Any = None,
) -> dict[str, Any]:
    """Acquire or renew the singleton gateway lease (Level 0b ``FOR UPDATE``)."""
    cleaned_id = str(instance_id or "").strip()
    if not cleaned_id or len(cleaned_id) > 128:
        raise HTTPException(
            status_code=400,
            detail="instance_id must be a non-empty string (<= 128 chars)",
        )
    ttl = max(5, min(int(ttl_seconds or 45), 300))

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                if _before_lease_update_hook is not None:
                    await _before_lease_update_hook()
                await cur.execute(
                    """
                    SELECT instance_id, epoch, acquired_at, expires_at,
                           clock_timestamp() AS db_now
                    FROM dalton_channel_gateway_lease
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_gateway_lease (
                            singleton_id, instance_id, epoch, acquired_at, expires_at, updated_at
                        )
                        VALUES (1, %s, 1, NOW(), NOW() + make_interval(secs => %s), NOW())
                        ON CONFLICT (singleton_id) DO NOTHING
                        RETURNING instance_id, epoch, expires_at
                        """,
                        (cleaned_id, ttl),
                    )
                    inserted = await cur.fetchone()
                    if inserted is not None:
                        return {
                            "granted": True,
                            "epoch": int(inserted["epoch"]),
                            "expires_at": _iso(inserted["expires_at"]),
                        }
                    await cur.execute(
                        """
                        SELECT instance_id, epoch, acquired_at, expires_at,
                               clock_timestamp() AS db_now
                        FROM dalton_channel_gateway_lease
                        WHERE singleton_id = 1
                        FOR UPDATE
                        """
                    )
                    row = await cur.fetchone()

                assert row is not None
                db_now = _coerce_utc(row.get("db_now")) or datetime.now(timezone.utc)
                expires_at = _coerce_utc(row.get("expires_at")) or db_now
                holder_id = str(row["instance_id"])
                cur_epoch = int(row["epoch"])

                if holder_id == cleaned_id and expires_at > db_now:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_gateway_lease
                        SET expires_at = NOW() + make_interval(secs => %s),
                            updated_at = NOW()
                        WHERE singleton_id = 1
                        RETURNING epoch, expires_at
                        """,
                        (ttl,),
                    )
                    upd = await cur.fetchone()
                    return {
                        "granted": True,
                        "epoch": int(upd["epoch"]),
                        "expires_at": _iso(upd["expires_at"]),
                    }

                if expires_at <= db_now:
                    await cur.execute(
                        """
                        UPDATE dalton_channel_gateway_lease
                        SET instance_id = %s,
                            epoch = epoch + 1,
                            acquired_at = NOW(),
                            expires_at = NOW() + make_interval(secs => %s),
                            updated_at = NOW()
                        WHERE singleton_id = 1
                        RETURNING epoch, expires_at
                        """,
                        (cleaned_id, ttl),
                    )
                    upd = await cur.fetchone()
                    return {
                        "granted": True,
                        "epoch": int(upd["epoch"]),
                        "expires_at": _iso(upd["expires_at"]),
                    }

                remaining_ms = max(
                    1000, min(3000, int((expires_at - db_now).total_seconds() * 1000))
                )
                return {
                    "granted": False,
                    "epoch": cur_epoch,
                    "expires_at": _iso(expires_at),
                    "retry_after_ms": remaining_ms,
                }


async def heartbeat_gateway_lease(
    *,
    instance_id: str,
    epoch: int,
    ttl_seconds: int = 45,
    conn: Any = None,
) -> dict[str, Any]:
    """Extend an active gateway lease; raises HTTP 409 on lost or expired lease."""
    cleaned_id = str(instance_id or "").strip()
    if not cleaned_id:
        raise HTTPException(status_code=400, detail="instance_id is required")
    ttl = max(5, min(int(ttl_seconds or 45), 300))

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT instance_id, epoch, expires_at, clock_timestamp() AS db_now
                    FROM dalton_channel_gateway_lease
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                row = await cur.fetchone()
                if row is None:
                    raise StaleGatewayEpochError("Gateway lease does not exist.")
                db_now = _coerce_utc(row.get("db_now")) or datetime.now(timezone.utc)
                expires_at = _coerce_utc(row.get("expires_at")) or db_now
                if (
                    str(row["instance_id"]) != cleaned_id
                    or int(row["epoch"]) != int(epoch)
                    or expires_at <= db_now
                ):
                    raise StaleGatewayEpochError(
                        "Gateway lease is held by another instance or has expired."
                    )
                await cur.execute(
                    """
                    UPDATE dalton_channel_gateway_lease
                    SET expires_at = NOW() + make_interval(secs => %s),
                        updated_at = NOW()
                    WHERE singleton_id = 1
                    RETURNING epoch, expires_at
                    """,
                    (ttl,),
                )
                upd = await cur.fetchone()
                return {
                    "ok": True,
                    "epoch": int(upd["epoch"]),
                    "expires_at": _iso(upd["expires_at"]),
                }


async def release_gateway_lease(
    *,
    instance_id: str,
    epoch: int,
    conn: Any = None,
) -> dict[str, Any]:
    """Voluntarily release the gateway lease if still held by ``(instance_id, epoch)``."""
    cleaned_id = str(instance_id or "").strip()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT instance_id, epoch
                    FROM dalton_channel_gateway_lease
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                row = await cur.fetchone()
                if (
                    row is None
                    or str(row["instance_id"]) != cleaned_id
                    or int(row["epoch"]) != int(epoch)
                ):
                    return {"ok": True, "released": False}
                await cur.execute(
                    """
                    UPDATE dalton_channel_gateway_lease
                    SET expires_at = NOW() - INTERVAL '1 second',
                        updated_at = NOW()
                    WHERE singleton_id = 1
                    """
                )
                return {"ok": True, "released": True}


async def verify_gateway_epoch_in_tx(
    cur: Any,
    gateway_epoch: int | None,
    *,
    required: bool = False,
    platform: str | None = None,
    enforce_post_lease_for_non_weixin: bool = True,
) -> int | None:
    """Level 0b same-transaction ``FOR SHARE`` epoch check (D12).

    Centralized Batch A / Batch B rollout & outgoing-mode gate:
    - When ``platform == "weixin"``: ALWAYS requires a valid non-expired
      ``X-Gateway-Epoch`` matching the active lease row under ``FOR SHARE``.
    - When ``enforce_post_lease_for_non_weixin=False`` (used by I-6 / I-7 so
      gateway ``outgoing`` mode can deliver Feishu / Discord reports and
      notifications without holding a lease): non-Weixin requests omitting
      ``X-Gateway-Epoch`` are allowed even after a lease row exists.
    - When ``enforce_post_lease_for_non_weixin=True`` (I-1 / I-2 / I-3 / recover):
      non-Weixin requests omitting ``X-Gateway-Epoch`` are allowed only when no
      lease row exists yet; once a lease row exists, a valid epoch is required.
    - Any request explicitly supplying ``X-Gateway-Epoch`` is always validated
      against the active lease row.
    """
    cleaned_plat = str(platform or "").strip().lower() or None
    is_weixin = cleaned_plat == "weixin"
    if gateway_epoch is None and not required:
        return None
    if gateway_epoch is None and not is_weixin and not enforce_post_lease_for_non_weixin:
        return None
    await cur.execute(
        """
        SELECT epoch, expires_at, clock_timestamp() AS db_now
        FROM dalton_channel_gateway_lease
        WHERE singleton_id = 1
        FOR SHARE
        """
    )
    row = await cur.fetchone()
    if _after_lease_share_hook is not None:
        await _after_lease_share_hook()
    if row is None:
        if is_weixin or gateway_epoch is not None:
            raise StaleGatewayEpochError("No active gateway lease found.")
        return None
    if gateway_epoch is None:
        raise StaleGatewayEpochError("Missing X-Gateway-Epoch header.")
    db_now = _coerce_utc(row.get("db_now")) or datetime.now(timezone.utc)
    expires_at = _coerce_utc(row.get("expires_at")) or db_now
    if int(row["epoch"]) != int(gateway_epoch) or expires_at <= db_now:
        raise StaleGatewayEpochError(
            f"Stale gateway epoch: got {gateway_epoch}, active {row['epoch']}."
        )
    return int(row["epoch"])


# ---------------------------------------------------------------------------
# Global Lock Order Primitives (§2.1 (8))
# ---------------------------------------------------------------------------


async def lock_user_settings_in_tx(cur: Any, user_ids: Any) -> frozenset[str]:
    """Level 1: acquire user advisory & settings row locks in ascending ``user_id`` order."""
    ordered = _sorted_unique_strs(user_ids)
    for uid in ordered:
        reject_company_system_auth(uid)
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"dalton_user_settings:{uid}",),
        )
        await cur.execute(
            "SELECT 1 FROM dalton_channel_user_settings WHERE user_id = %s FOR UPDATE",
            (uid,),
        )
    return frozenset(ordered)


async def lock_weixin_accounts_in_tx(
    cur: Any,
    account_ids: Iterable[str] | Sequence[str] | set[str],
    *,
    for_update: bool = True,
) -> dict[str, dict[str, Any]]:
    """Level 3a: lock ``dalton_channel_weixin_accounts`` rows in ascending ``account_id`` order."""
    ordered = _sorted_unique_strs(account_ids)
    if not ordered:
        return {}
    clause = "FOR UPDATE" if for_update else "FOR NO KEY UPDATE"
    locked: dict[str, dict[str, Any]] = {}
    for aid in ordered:
        norm = normalize_uuid(aid)
        if not norm:
            continue
        await cur.execute(
            f"SELECT * FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid {clause}",
            (norm,),
        )
        row = await cur.fetchone()
        if row is not None:
            locked[str(row["account_id"])] = dict(row)
    return locked


async def lock_weixin_bot_and_users_in_tx(
    cur: Any,
    *,
    bot_ids: Sequence[str | None] = (),
    ilink_user_ids: Sequence[str | None] = (),
) -> None:
    """Level 3b: advisory locks for ``ilink_bot_id`` then sorted ``ilink_user_id`` values."""
    for bot_id in _sorted_unique_strs(bot_ids):
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"dalton_weixin_bot:{bot_id}",),
        )
    for wx_uid in _sorted_unique_strs(ilink_user_ids):
        await cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (f"dalton_weixin_ilink_user:{wx_uid}",),
        )


async def ensure_and_lock_weixin_config_in_tx(
    cur: Any,
    *,
    for_update: bool = False,
) -> dict[str, Any]:
    """Read (or Level 8 ``FOR UPDATE`` lock) the singleton ``dalton_channel_weixin_config`` row."""
    await cur.execute(
        """
        INSERT INTO dalton_channel_weixin_config (singleton_id, enabled, accounts_revision)
        VALUES (1, TRUE, 1)
        ON CONFLICT (singleton_id) DO NOTHING
        """
    )
    suffix = " FOR UPDATE" if for_update else ""
    await cur.execute(
        f"""
        SELECT singleton_id, enabled, allow_external, accounts_revision, updated_by, updated_at
        FROM dalton_channel_weixin_config
        WHERE singleton_id = 1{suffix}
        """
    )
    row = await cur.fetchone()
    if row is None:
        return {
            "singleton_id": 1,
            "enabled": True,
            "allow_external": None,
            "accounts_revision": 1,
            "updated_by": None,
            "updated_at": None,
        }
    return dict(row)


async def write_weixin_audit_log_in_tx(
    cur: Any,
    *,
    action: str,
    operator_user_id: str,
    target_user_id: str | None = None,
    weixin_account_id: str | None = None,
    ilink_bot_id: str | None = None,
    ilink_user_id: str | None = None,
    masked_ilink_user_id: str | None = None,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Level 7: insert one audit row with non-empty reason and masked Weixin ID."""
    masked = masked_ilink_user_id or mask_ilink_user_id(ilink_user_id)
    eff_reason = _normalize_audit_reason(action, reason)
    safe_meta = redact_weixin_secrets(dict(metadata or {}))
    norm_aid = normalize_uuid(weixin_account_id) if weixin_account_id else None
    await cur.execute(
        """
        INSERT INTO dalton_channel_weixin_audit_logs (
            action, operator_user_id, target_user_id, weixin_account_id,
            ilink_bot_id, masked_ilink_user_id, reason, metadata, created_at
        )
        VALUES (%s, %s, %s, %s::uuid, %s, %s, %s, %s, NOW())
        """,
        (
            action,
            str(operator_user_id or "system:unknown").strip(),
            str(target_user_id).strip() if target_user_id else None,
            norm_aid,
            str(ilink_bot_id).strip() if ilink_bot_id else None,
            masked,
            eff_reason,
            Jsonb(safe_meta),
        ),
    )


async def _ensure_weixin_user_settings_in_tx(cur: Any, user_id: str) -> None:
    """Ensure ``dalton_channel_user_settings.apps.weixin`` exists (caller holds Level 1 lock)."""
    await cur.execute(
        "SELECT version, apps FROM dalton_channel_user_settings WHERE user_id = %s FOR UPDATE",
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        await cur.execute(
            """
            INSERT INTO dalton_channel_user_settings (user_id, version, apps, updated_at)
            VALUES (%s, 'v1', %s, NOW())
            ON CONFLICT (user_id) DO UPDATE SET
                apps = dalton_channel_user_settings.apps || EXCLUDED.apps,
                updated_at = NOW()
            """,
            (user_id, Jsonb({"weixin": _default_weixin_app_settings()})),
        )
        return
    apps = dict(row.get("apps") or {})
    if "weixin" not in apps:
        apps["weixin"] = _default_weixin_app_settings()
        await cur.execute(
            """
            UPDATE dalton_channel_user_settings
            SET version = %s, apps = %s, updated_at = NOW()
            WHERE user_id = %s
            """,
            (_bump_version_str(row.get("version")), Jsonb(apps), user_id),
        )


async def _remove_weixin_user_settings_in_tx(cur: Any, user_id: str) -> None:
    """Remove ``apps.weixin`` from ``dalton_channel_user_settings`` (caller holds Level 1 lock)."""
    await cur.execute(
        "SELECT version, apps FROM dalton_channel_user_settings WHERE user_id = %s FOR UPDATE",
        (user_id,),
    )
    row = await cur.fetchone()
    if row is None:
        return
    apps = dict(row.get("apps") or {})
    if "weixin" in apps:
        apps.pop("weixin", None)
        await cur.execute(
            """
            UPDATE dalton_channel_user_settings
            SET version = %s, apps = %s, updated_at = NOW()
            WHERE user_id = %s
            """,
            (_bump_version_str(row.get("version")), Jsonb(apps), user_id),
        )


async def _advance_weixin_sessions_and_close_watches_in_tx(
    cur: Any,
    *,
    user_id: str,
    account_id: str | None = None,
    scope_key: str | None = None,
) -> dict[str, int]:
    """Level 6 -> Level 7: advance Weixin session generations and deactivate thread watches."""
    if scope_key:
        await cur.execute(
            """
            INSERT INTO dalton_channel_session_generations (
                platform, scope_key, dalton_user_id, generation
            )
            VALUES ('weixin', %s, %s, 1)
            ON CONFLICT (platform, scope_key, dalton_user_id) DO NOTHING
            """,
            (scope_key, user_id),
        )
    await cur.execute(
        """
        SELECT scope_key
        FROM dalton_channel_session_generations
        WHERE platform = 'weixin' AND dalton_user_id = %s
        ORDER BY scope_key ASC
        FOR UPDATE
        """,
        (user_id,),
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
        WHERE platform = 'weixin' AND dalton_user_id = %s
        RETURNING scope_key, generation
        """,
        (user_id,),
    )
    updated_gens = {
        str(r["scope_key"]): int(r["generation"]) for r in await cur.fetchall()
    }

    if account_id:
        norm_aid = normalize_uuid(account_id)
        await cur.execute(
            """
            SELECT thread_id
            FROM dalton_channel_thread_watches
            WHERE platform = 'weixin'
              AND watch_active = TRUE
              AND (dalton_user_id = %s OR weixin_account_id = %s::uuid)
            ORDER BY thread_id ASC
            FOR UPDATE
            """,
            (user_id, norm_aid),
        )
        await cur.execute(
            """
            UPDATE dalton_channel_thread_watches
            SET watch_active = FALSE, updated_at = NOW()
            WHERE platform = 'weixin'
              AND watch_active = TRUE
              AND (dalton_user_id = %s OR weixin_account_id = %s::uuid)
            """,
            (user_id, norm_aid),
        )
    else:
        await cur.execute(
            """
            SELECT thread_id
            FROM dalton_channel_thread_watches
            WHERE platform = 'weixin' AND dalton_user_id = %s AND watch_active = TRUE
            ORDER BY thread_id ASC
            FOR UPDATE
            """,
            (user_id,),
        )
        await cur.execute(
            """
            UPDATE dalton_channel_thread_watches
            SET watch_active = FALSE, updated_at = NOW()
            WHERE platform = 'weixin' AND dalton_user_id = %s AND watch_active = TRUE
            """,
            (user_id,),
        )
    return updated_gens


async def _supersede_pending_login_sessions_in_tx(cur: Any, user_id: str) -> int:
    """Level 4: mark all pending login sessions (including ``starting``) as ``superseded``."""
    await cur.execute(
        """
        SELECT session_key
        FROM dalton_channel_weixin_login_sessions
        WHERE user_id = %s
          AND status IN ('starting', 'wait', 'scaned', 'verify_code')
        ORDER BY session_key ASC
        FOR UPDATE
        """,
        (user_id,),
    )
    rows = await cur.fetchall()
    if not rows:
        return 0
    await cur.execute(
        """
        UPDATE dalton_channel_weixin_login_sessions
        SET status = 'superseded',
            qrcode_img_content = NULL,
            login_version = login_version + 1,
            updated_at = NOW()
        WHERE user_id = %s
          AND status IN ('starting', 'wait', 'scaned', 'verify_code')
        """,
        (user_id,),
    )
    return len(rows)


async def _count_active_poller_slots_in_tx(
    cur: Any,
    *,
    exclude_user_id: str | None = None,
) -> int:
    if exclude_user_id:
        await cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM dalton_channel_weixin_accounts
            WHERE status IN ('active', 'pending_verification')
              AND user_id <> %s
            """,
            (exclude_user_id,),
        )
    else:
        await cur.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM dalton_channel_weixin_accounts
            WHERE status IN ('active', 'pending_verification')
            """
        )
    row = await cur.fetchone()
    return int((row or {}).get("cnt") or 0)


async def _is_ilink_user_occupied_by_other_in_tx(
    cur: Any,
    *,
    ilink_user_id: str | None,
    exclude_account_id: str | None = None,
) -> dict[str, Any] | None:
    """Return another member's occupying account row if ``ilink_user_id`` is held."""
    cleaned = str(ilink_user_id or "").strip()
    if not cleaned:
        return None
    if exclude_account_id:
        norm_aid = normalize_uuid(exclude_account_id)
        await cur.execute(
            """
            SELECT *
            FROM dalton_channel_weixin_accounts
            WHERE COALESCE(ilink_user_id, prev_ilink_user_id) = %s
              AND account_id <> %s::uuid
              AND (
                  status IN ('active', 'pending_verification', 'expired')
                  OR disabled_reason = 'external_access_disabled'
              )
            LIMIT 1
            """,
            (cleaned, norm_aid),
        )
    else:
        await cur.execute(
            """
            SELECT *
            FROM dalton_channel_weixin_accounts
            WHERE COALESCE(ilink_user_id, prev_ilink_user_id) = %s
              AND (
                  status IN ('active', 'pending_verification', 'expired')
                  OR disabled_reason = 'external_access_disabled'
              )
            LIMIT 1
            """,
            (cleaned,),
        )
    row = await cur.fetchone()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Multi-Account Context Tokens (`dalton_channel_weixin_account_tokens`)
# ---------------------------------------------------------------------------


_CONTEXT_ORDER_SOURCES = ("protocol", "platform_time", "receive")


def _context_order_source(source: str | None) -> str:
    cleaned = str(source or "").strip()
    return cleaned if cleaned in _CONTEXT_ORDER_SOURCES else ""


def _positive_int(raw: Any) -> int | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = int(raw)
        return val if val > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        val = int(raw.strip())
        return val if val > 0 else None
    return None


def _context_order_supersedes(
    incoming_source: str,
    incoming_value: int,
    existing_source: str,
    existing_value: int,
    *,
    protocol_high: int,
    platform_time_high: int,
) -> bool:
    if incoming_source == "receive":
        return True
    if incoming_source == "protocol":
        high = max(protocol_high, existing_value if existing_source == "protocol" else 0)
        return incoming_value > high
    if incoming_source == "platform_time":
        high = max(
            platform_time_high,
            existing_value if existing_source == "platform_time" else 0,
        )
        return incoming_value > high
    return False


async def upsert_weixin_account_context_token_in_tx(
    cur: Any,
    *,
    account_id: str,
    ilink_bot_id: str,
    bot_generation: int,
    platform_user_id: str,
    context_token: str,
    incoming_seq: int | None = None,
    incoming_source: str | None = None,
    enc_key: str | None = None,
) -> tuple[bool, int, int]:
    """Level 5: upsert context token in ``dalton_channel_weixin_account_tokens``.

    Increments ``token_version`` and clears ``stale_at`` / ``stale_reason`` ONLY when
    this newly registered message installs or supersedes the watermark (§5.2).
    Returns ``(updated, stored_seq, token_version)``.
    """
    norm_aid = normalize_uuid(account_id)
    cleaned_bot = str(ilink_bot_id or "").strip()
    cleaned_puid = str(platform_user_id or "").strip()
    cleaned_tok = str(context_token or "").strip()
    if not norm_aid or not cleaned_bot or not cleaned_puid or not cleaned_tok:
        return False, 0, 0

    key = enc_key or get_encryption_key()
    await cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"dalton_weixin_context:{cleaned_bot}:{int(bot_generation)}:{cleaned_puid}",),
    )
    await cur.execute(
        """
        SELECT seq, seq_source, protocol_seq_high, platform_time_high, token_version
        FROM dalton_channel_weixin_account_tokens
        WHERE account_id = %s::uuid
          AND bot_generation = %s
          AND platform_user_id = %s
        FOR UPDATE
        """,
        (norm_aid, int(bot_generation), cleaned_puid),
    )
    existing = await cur.fetchone()

    source = _context_order_source(incoming_source)
    value = _positive_int(incoming_seq) if source in ("protocol", "platform_time") else None
    if source in ("protocol", "platform_time") and value is None:
        source = ""

    protocol_high = 0
    platform_high = 0
    if existing is not None:
        ex_seq = int(existing.get("seq") or 0)
        ex_src = _context_order_source(existing.get("seq_source"))
        ex_ver = int(existing.get("token_version") or 1)
        protocol_high = max(
            int(existing.get("protocol_seq_high") or 0),
            ex_seq if ex_src == "protocol" else 0,
        )
        platform_high = max(
            int(existing.get("platform_time_high") or 0),
            ex_seq if ex_src == "platform_time" else 0,
        )
        if not source:
            source = "receive"
            value = ex_seq + 1 if ex_src == "receive" else 1
        if not _context_order_supersedes(
            source,
            int(value or 0),
            ex_src,
            ex_seq,
            protocol_high=protocol_high,
            platform_time_high=platform_high,
        ):
            return False, ex_seq, ex_ver
        eff_seq = int(value or 0)
        next_ver = ex_ver + 1
    else:
        if not source:
            source = "receive"
            value = 1
        eff_seq = int(value or 1)
        next_ver = 1

    if source == "protocol":
        protocol_high = max(protocol_high, eff_seq)
    elif source == "platform_time":
        platform_high = max(platform_high, eff_seq)

    await cur.execute(
        """
        INSERT INTO dalton_channel_weixin_account_tokens (
            account_id, ilink_bot_id, bot_generation, platform_user_id,
            encrypted_context_token, seq, seq_source,
            protocol_seq_high, platform_time_high, token_version,
            stale_at, stale_reason, updated_at
        )
        VALUES (
            %s::uuid, %s, %s, %s,
            pgp_sym_encrypt(%s, %s), %s, %s,
            %s, %s, %s,
            NULL, NULL, NOW()
        )
        ON CONFLICT (account_id, bot_generation, platform_user_id) DO UPDATE SET
            ilink_bot_id = EXCLUDED.ilink_bot_id,
            encrypted_context_token = EXCLUDED.encrypted_context_token,
            seq = CASE
                WHEN EXCLUDED.seq_source = 'receive' THEN
                    CASE
                        WHEN dalton_channel_weixin_account_tokens.seq_source = 'receive'
                        THEN dalton_channel_weixin_account_tokens.seq + 1
                        ELSE 1
                    END
                ELSE EXCLUDED.seq
            END,
            seq_source = EXCLUDED.seq_source,
            protocol_seq_high = GREATEST(
                dalton_channel_weixin_account_tokens.protocol_seq_high,
                CASE
                    WHEN dalton_channel_weixin_account_tokens.seq_source = 'protocol'
                    THEN dalton_channel_weixin_account_tokens.seq
                    ELSE 0
                END,
                EXCLUDED.protocol_seq_high
            ),
            platform_time_high = GREATEST(
                dalton_channel_weixin_account_tokens.platform_time_high,
                CASE
                    WHEN dalton_channel_weixin_account_tokens.seq_source = 'platform_time'
                    THEN dalton_channel_weixin_account_tokens.seq
                    ELSE 0
                END,
                EXCLUDED.platform_time_high
            ),
            token_version = dalton_channel_weixin_account_tokens.token_version + 1,
            stale_at = NULL,
            stale_reason = NULL,
            updated_at = NOW()
        WHERE
            EXCLUDED.seq_source = 'receive'
            OR (
                EXCLUDED.seq_source = 'protocol'
                AND EXCLUDED.seq >= GREATEST(
                    dalton_channel_weixin_account_tokens.protocol_seq_high,
                    CASE
                        WHEN dalton_channel_weixin_account_tokens.seq_source = 'protocol'
                        THEN dalton_channel_weixin_account_tokens.seq
                        ELSE 0
                    END
                )
            )
            OR (
                EXCLUDED.seq_source = 'platform_time'
                AND EXCLUDED.seq >= GREATEST(
                    dalton_channel_weixin_account_tokens.platform_time_high,
                    CASE
                        WHEN dalton_channel_weixin_account_tokens.seq_source = 'platform_time'
                        THEN dalton_channel_weixin_account_tokens.seq
                        ELSE 0
                    END
                )
            )
        RETURNING seq, token_version
        """,
        (
            norm_aid,
            cleaned_bot,
            int(bot_generation),
            cleaned_puid,
            cleaned_tok,
            key,
            eff_seq,
            source,
            protocol_high,
            platform_high,
            next_ver,
        ),
    )
    stored = await cur.fetchone()
    if stored is None:
        await cur.execute(
            """
            SELECT seq, token_version
            FROM dalton_channel_weixin_account_tokens
            WHERE account_id = %s::uuid
              AND bot_generation = %s
              AND platform_user_id = %s
            """,
            (norm_aid, int(bot_generation), cleaned_puid),
        )
        cur_row = await cur.fetchone()
        return (
            False,
            int(cur_row["seq"]) if cur_row else 0,
            int(cur_row["token_version"]) if cur_row else 0,
        )
    stored_seq = int(stored["seq"])
    stored_ver = int(stored["token_version"])

    await cur.execute(
        """
        UPDATE dalton_channel_weixin_accounts
        SET last_inbound_at = NOW(), updated_at = NOW()
        WHERE account_id = %s::uuid
        """,
        (norm_aid,),
    )
    return True, stored_seq, stored_ver


async def resolve_weixin_account_context_token_in_tx(
    cur: Any,
    *,
    account_id: str,
    bot_generation: int,
    platform_user_id: str,
    enc_key: str | None = None,
) -> tuple[str | None, int | None, bool]:
    """Read current context token strictly from ``dalton_channel_weixin_account_tokens`` (§5.2).

    Returns ``(decrypted_token, token_version, is_stale)``.
    """
    norm_aid = normalize_uuid(account_id)
    cleaned_puid = str(platform_user_id or "").strip()
    if not norm_aid or not cleaned_puid:
        return None, None, False
    key = enc_key or get_encryption_key()
    await cur.execute(
        """
        SELECT pgp_sym_decrypt(encrypted_context_token, %s) AS context_token,
               token_version,
               stale_at
        FROM dalton_channel_weixin_account_tokens
        WHERE account_id = %s::uuid
          AND bot_generation = %s
          AND platform_user_id = %s
        """,
        (key, norm_aid, int(bot_generation), cleaned_puid),
    )
    row = await cur.fetchone()
    if row is None:
        return None, None, False
    ver = int(row.get("token_version") or 1)
    if row.get("stale_at") is not None:
        return None, ver, True
    tok = str(row.get("context_token") or "").strip() or None
    return tok, ver, False


# ---------------------------------------------------------------------------
# Unified Account Re-Evaluation (Codex ② + §3.1 (2) + §4.2)
# ---------------------------------------------------------------------------


async def reevaluate_weixin_accounts(
    *,
    operator_user_id: str = "system:roster-reconciler",
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    target_user_ids: Sequence[str] | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Single canonical re-evaluation function for toggle re-opens & periodic reconcile.

    Evaluates every candidate account against:
    1. Live three-state roster (``active`` | ``removed`` | ``lookup_error``).
    2. ``evaluate_weixin_member_access(tier, channel_enabled, db_allow_external)``.
    3. Weixin ID uniqueness & single-instance slot capacity (``expired`` costs 0 slots).

    - ``lookup_error`` leaves the account untouched (zero state/revision mutation).
    - ``removed`` disables active/pending/expired/external-paused accounts into
      ``disabled (member_not_in_roster)`` preserving original ``prev_status``.
    - ``active`` + ``external_access_disabled`` disables active/pending/expired external
      accounts into ``disabled (external_access_disabled)`` preserving ``prev_status``.
    - ``active`` + access allowed + currently ``disabled`` (either ``member_not_in_roster``
      or ``external_access_disabled``) restores to ``prev_status`` when the Weixin ID is
      unoccupied and slot capacity permits (handling both toggle reopen orders, Codex ②).
    """

    async def _attempt(attempt_conn: Any) -> dict[str, Any]:
        max_slots = get_weixin_max_concurrent_accounts()
        # Phase 1: unlocked discovery of candidate accounts & their live roster/tier.
        # Wrap Phase-1 reads in short committed transactions so non-autocommit
        # connections return to IDLE before Phase 2's top-level transaction.
        async with get_db_connection(attempt_conn) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    if target_user_ids:
                        await cur.execute(
                            """
                            SELECT account_id::text AS account_id, user_id, ilink_bot_id,
                                   ilink_user_id, prev_ilink_user_id, status, prev_status, disabled_reason
                            FROM dalton_channel_weixin_accounts
                            WHERE user_id = ANY(%s::text[])
                            ORDER BY account_id ASC
                            """,
                            (list(target_user_ids),),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT account_id::text AS account_id, user_id, ilink_bot_id,
                                   ilink_user_id, prev_ilink_user_id, status, prev_status, disabled_reason
                            FROM dalton_channel_weixin_accounts
                            ORDER BY account_id ASC
                            """
                        )
                    discovered = [dict(r) for r in await cur.fetchall()]

            if _after_discovery_hook is not None:
                await _after_discovery_hook()

            # Perform live roster & tier checks outside row locks to keep transactions brief
            live_status: dict[str, str] = {}
            live_tier: dict[str, str] = {}
            lookup_error_count = 0
            async with owned_conn.transaction():
                for acc in discovered:
                    uid = str(acc["user_id"])
                    st = await check_team_member_status_live(uid, conn=owned_conn)
                    live_status[uid] = st
                    if st == "lookup_error":
                        lookup_error_count += 1
                    elif st == "active":
                        live_tier[uid] = await get_user_tier(uid, conn=owned_conn)

            # §4.2: If ANY member encounters lookup_error during a reconcile round,
            # abort the ENTIRE round without modifying any account status or accounts_revision.
            if lookup_error_count > 0:
                async with owned_conn.transaction():
                    async with owned_conn.cursor(row_factory=dict_row) as cur:
                        await verify_gateway_epoch_in_tx(
                            cur,
                            gateway_epoch,
                            required=require_gateway_epoch,
                            platform="weixin",
                        )
                        cfg_abort = await ensure_and_lock_weixin_config_in_tx(
                            cur, for_update=False
                        )
                        return {
                            "changed": False,
                            "aborted_on_lookup_error": True,
                            "lookup_error_count": lookup_error_count,
                            "disabled_count": 0,
                            "restored_count": 0,
                            "accounts_revision": int(cfg_abort["accounts_revision"]),
                        }

            # Determine which accounts actually need mutation
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    cfg_peek = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)

            mutations: list[tuple[dict[str, Any], str]] = []
            for acc in discovered:
                uid = str(acc["user_id"])
                st = live_status.get(uid, "lookup_error")
                if st == "lookup_error":
                    continue
                cur_status = str(acc["status"])
                cur_reason = acc.get("disabled_reason")
                if st == "removed":
                    if not (
                        cur_status == "disabled"
                        and cur_reason == "member_not_in_roster"
                    ):
                        mutations.append((acc, "disable_removed"))
                    continue

                tier = live_tier.get(uid, "external")
                access_ok, deny_code = evaluate_weixin_member_access(
                    tier,
                    channel_enabled=bool(cfg_peek["enabled"]),
                    db_allow_external=cfg_peek.get("allow_external"),
                )
                if cur_status == "disabled":
                    if access_ok:
                        mutations.append((acc, "restore"))
                else:
                    if not access_ok and deny_code == "external_access_disabled":
                        mutations.append((acc, "disable_external"))

            if not mutations and not require_gateway_epoch:
                return {
                    "changed": False,
                    "disabled_count": 0,
                    "restored_count": 0,
                    "accounts_revision": int(cfg_peek["accounts_revision"]),
                }

            # Phase 2: acquire locks in strict §2.1 (8) order inside one transaction
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        platform="weixin",
                    )
                    if not mutations:
                        cfg_now = await ensure_and_lock_weixin_config_in_tx(
                            cur, for_update=False
                        )
                        return {
                            "changed": False,
                            "disabled_count": 0,
                            "restored_count": 0,
                            "accounts_revision": int(cfg_now["accounts_revision"]),
                        }

                    affected_uids = [str(m[0]["user_id"]) for m in mutations]
                    affected_aids = [str(m[0]["account_id"]) for m in mutations]
                    affected_bots = [str(m[0]["ilink_bot_id"]) for m in mutations]
                    affected_wx_uids = [
                        w
                        for m in mutations
                        for w in (
                            m[0].get("ilink_user_id"),
                            m[0].get("prev_ilink_user_id"),
                        )
                        if w
                    ]

                    # Level 1 -> Level 3a -> Level 3b
                    await lock_user_settings_in_tx(cur, affected_uids)
                    if _after_user_locks_hook is not None:
                        await _after_user_locks_hook()
                    locked_accs = await lock_weixin_accounts_in_tx(
                        cur, affected_aids, for_update=True
                    )
                    await lock_weixin_bot_and_users_in_tx(
                        cur, bot_ids=affected_bots, ilink_user_ids=affected_wx_uids
                    )

                    # Re-read config (we will update at Level 8 at transaction end)
                    cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                    cur_slots = await _count_active_poller_slots_in_tx(cur)
                    disabled_count = 0
                    restored_count = 0

                    for orig_acc, planned_op in mutations:
                        aid = str(orig_acc["account_id"])
                        acc = locked_accs.get(aid)
                        if acc is None:
                            continue
                        uid = str(acc["user_id"])
                        cur_status = str(acc["status"])
                        cur_reason = acc.get("disabled_reason")
                        eff_wx_uid = acc.get("ilink_user_id") or acc.get("prev_ilink_user_id")

                        if planned_op == "disable_removed":
                            if (
                                cur_status == "disabled"
                                and cur_reason == "member_not_in_roster"
                            ):
                                continue
                            orig_prev = (
                                acc.get("prev_status")
                                if cur_status == "disabled"
                                else cur_status
                            )
                            if orig_prev not in ("active", "pending_verification", "expired"):
                                orig_prev = "active"
                            if cur_status in ("active", "pending_verification"):
                                cur_slots = max(0, cur_slots - 1)
                            await cur.execute(
                                """
                                UPDATE dalton_channel_weixin_accounts
                                SET status = 'disabled',
                                    prev_status = %s,
                                    disabled_reason = 'member_not_in_roster',
                                    updated_at = NOW()
                                WHERE account_id = %s::uuid
                                """,
                                (orig_prev, aid),
                            )
                            await _supersede_pending_login_sessions_in_tx(cur, uid)
                            await _advance_weixin_sessions_and_close_watches_in_tx(
                                cur, user_id=uid, account_id=aid
                            )
                            await write_weixin_audit_log_in_tx(
                                cur,
                                action="member_removed_revoke",
                                operator_user_id=operator_user_id,
                                target_user_id=uid,
                                weixin_account_id=aid,
                                ilink_bot_id=acc.get("ilink_bot_id"),
                                ilink_user_id=eff_wx_uid,
                                metadata={"prev_status": orig_prev},
                            )
                            disabled_count += 1

                        elif planned_op == "disable_external":
                            if cur_status == "disabled":
                                continue
                            tier = live_tier.get(uid, "external")
                            acc_ok, d_code = evaluate_weixin_member_access(
                                tier,
                                channel_enabled=bool(cfg["enabled"]),
                                db_allow_external=cfg.get("allow_external"),
                            )
                            if acc_ok or d_code != "external_access_disabled":
                                continue
                            if cur_status in ("active", "pending_verification"):
                                cur_slots = max(0, cur_slots - 1)
                            await cur.execute(
                                """
                                UPDATE dalton_channel_weixin_accounts
                                SET status = 'disabled',
                                    prev_status = %s,
                                    disabled_reason = 'external_access_disabled',
                                    updated_at = NOW()
                                WHERE account_id = %s::uuid
                                """,
                                (cur_status, aid),
                            )
                            await _supersede_pending_login_sessions_in_tx(cur, uid)
                            await _advance_weixin_sessions_and_close_watches_in_tx(
                                cur, user_id=uid, account_id=aid
                            )
                            disabled_count += 1

                        elif planned_op == "restore":
                            if cur_status != "disabled":
                                continue
                            tier = live_tier.get(uid, "external")
                            acc_ok, _ = evaluate_weixin_member_access(
                                tier,
                                channel_enabled=bool(cfg["enabled"]),
                                db_allow_external=cfg.get("allow_external"),
                            )
                            if not acc_ok:
                                continue
                            target_status = str(acc.get("prev_status") or "active")
                            if target_status not in (
                                "active",
                                "pending_verification",
                                "expired",
                            ):
                                target_status = "active"
                            if eff_wx_uid:
                                occ = await _is_ilink_user_occupied_by_other_in_tx(
                                    cur,
                                    ilink_user_id=eff_wx_uid,
                                    exclude_account_id=aid,
                                )
                                if occ is not None:
                                    continue
                            needs_slot = target_status in (
                                "active",
                                "pending_verification",
                            )
                            if needs_slot and cur_slots >= max_slots:
                                continue
                            await cur.execute(
                                """
                                UPDATE dalton_channel_weixin_accounts
                                SET status = %s,
                                    prev_status = NULL,
                                    disabled_reason = NULL,
                                    updated_at = NOW()
                                WHERE account_id = %s::uuid
                                """,
                                (target_status, aid),
                            )
                            if needs_slot:
                                cur_slots += 1
                            if cur_reason == "member_not_in_roster":
                                await write_weixin_audit_log_in_tx(
                                    cur,
                                    action="member_reinstated_restore",
                                    operator_user_id=operator_user_id,
                                    target_user_id=uid,
                                    weixin_account_id=aid,
                                    ilink_bot_id=acc.get("ilink_bot_id"),
                                    ilink_user_id=eff_wx_uid,
                                    metadata={"restored_status": target_status},
                                )
                            restored_count += 1

                    # Level 8: bump accounts_revision at end of transaction if anything changed
                    changed = (disabled_count + restored_count) > 0
                    if changed:
                        await ensure_and_lock_weixin_config_in_tx(cur, for_update=True)
                        # Re-check poller slot capacity AFTER acquiring Level 8 config lock
                        # so concurrent confirms + restores can never exceed max_slots.
                        if restored_count > 0:
                            post_slots = await _count_active_poller_slots_in_tx(cur)
                            if post_slots > max_slots:
                                raise WeixinLockSetChanged()
                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_config
                            SET accounts_revision = accounts_revision + 1,
                                updated_at = NOW()
                            WHERE singleton_id = 1
                            RETURNING accounts_revision
                            """
                        )
                        rev_row = await cur.fetchone()
                        new_rev = int(rev_row["accounts_revision"])
                    else:
                        new_rev = int(cfg["accounts_revision"])

                    return {
                        "changed": changed,
                        "disabled_count": disabled_count,
                        "restored_count": restored_count,
                        "accounts_revision": new_rev,
                    }

    return await _run_tx_with_retry("reevaluate_weixin_accounts", conn, _attempt)


async def revoke_weixin_account_on_member_removed_in_tx(
    cur: Any,
    *,
    user_id: str,
    operator_user_id: str = "system:roster-reconciler",
    reason: str | None = None,
) -> bool:
    """In-transaction revocation called by ``delete_user_role`` (after Level 0a locks).

    Follows Level 1 -> Level 3a -> Level 3b -> Level 4 -> Level 6 -> Level 7 -> Level 8.
    Returns True if an account or pending login session was revoked.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False
    await lock_user_settings_in_tx(cur, [uid])
    await cur.execute(
        """
        SELECT *
        FROM dalton_channel_weixin_accounts
        WHERE user_id = %s
        ORDER BY account_id ASC
        FOR UPDATE
        """,
        (uid,),
    )
    acc = await cur.fetchone()
    superseded = await _supersede_pending_login_sessions_in_tx(cur, uid)
    if acc is None:
        return superseded > 0

    aid = str(acc["account_id"])
    cur_status = str(acc["status"])
    cur_reason = acc.get("disabled_reason")
    eff_wx_uid = acc.get("ilink_user_id") or acc.get("prev_ilink_user_id")
    await lock_weixin_bot_and_users_in_tx(
        cur,
        bot_ids=[acc.get("ilink_bot_id")],
        ilink_user_ids=[acc.get("ilink_user_id"), acc.get("prev_ilink_user_id")],
    )
    if cur_status == "disabled" and cur_reason == "member_not_in_roster":
        return superseded > 0

    orig_prev = (
        acc.get("prev_status") if cur_status == "disabled" else cur_status
    )
    if orig_prev not in ("active", "pending_verification", "expired"):
        orig_prev = "active"

    await cur.execute(
        """
        UPDATE dalton_channel_weixin_accounts
        SET status = 'disabled',
            prev_status = %s,
            disabled_reason = 'member_not_in_roster',
            updated_at = NOW()
        WHERE account_id = %s::uuid
        """,
        (orig_prev, aid),
    )
    await _advance_weixin_sessions_and_close_watches_in_tx(
        cur, user_id=uid, account_id=aid
    )
    await write_weixin_audit_log_in_tx(
        cur,
        action="member_removed_revoke",
        operator_user_id=operator_user_id,
        target_user_id=uid,
        weixin_account_id=aid,
        ilink_bot_id=acc.get("ilink_bot_id"),
        ilink_user_id=eff_wx_uid,
        reason=reason,
        metadata={"prev_status": orig_prev},
    )
    await ensure_and_lock_weixin_config_in_tx(cur, for_update=True)
    await cur.execute(
        """
        UPDATE dalton_channel_weixin_config
        SET accounts_revision = accounts_revision + 1,
            updated_at = NOW()
        WHERE singleton_id = 1
        """
    )
    return True


# ---------------------------------------------------------------------------
# Internal Gateway Endpoints: Reconcile, Account Detail, Cursor, Status
# ---------------------------------------------------------------------------


async def reconcile_weixin_accounts(
    *,
    gateway_epoch: int,
    since_revision: int | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """``POST .../internal/weixin/accounts/reconcile`` (§3.1, §3.2 #1, §4.2)."""
    # Run live roster & switch re-evaluation BEFORE deciding changed=False
    await reevaluate_weixin_accounts(
        operator_user_id="system:roster-reconciler",
        gateway_epoch=gateway_epoch,
        require_gateway_epoch=True,
        conn=conn,
    )

    max_slots = get_weixin_max_concurrent_accounts()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await verify_gateway_epoch_in_tx(
                    cur, gateway_epoch, required=True, platform="weixin"
                )
                cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                enabled = bool(cfg["enabled"])
                db_allow_ext = cfg.get("allow_external")
                eff_allow_ext = (
                    bool(db_allow_ext)
                    if db_allow_ext is not None
                    else bool(get_dalton_config().weixin.allow_external)
                )
                rev = int(cfg["accounts_revision"])

                if not enabled:
                    return {
                        "changed": since_revision is None or int(since_revision) != rev,
                        "enabled": False,
                        "allow_external": eff_allow_ext,
                        "max_concurrent_accounts": max_slots,
                        "accounts_revision": rev,
                        "accounts": [],
                    }

                if since_revision is not None and int(since_revision) == rev:
                    return {
                        "changed": False,
                        "enabled": True,
                        "allow_external": eff_allow_ext,
                        "max_concurrent_accounts": max_slots,
                        "accounts_revision": rev,
                        "accounts": [],
                    }

                enc_key = get_encryption_key()
                await cur.execute(
                    """
                    SELECT account_id::text AS account_id,
                           user_id,
                           ilink_bot_id,
                           ilink_user_id,
                           prev_ilink_user_id,
                           ilink_username,
                           pgp_sym_decrypt(encrypted_bot_token, %s) AS bot_token,
                           base_url,
                           cdn_base_url,
                           get_updates_buf,
                           status,
                           bot_generation,
                           last_inbound_at,
                           updated_at
                    FROM dalton_channel_weixin_accounts
                    WHERE status IN ('active', 'pending_verification')
                    ORDER BY created_at ASC, account_id ASC
                    """,
                    (enc_key,),
                )
                accounts = []
                for r in await cur.fetchall():
                    accounts.append(
                        {
                            "account_id": str(r["account_id"]),
                            "user_id": str(r["user_id"]),
                            "ilink_bot_id": str(r["ilink_bot_id"]),
                            "ilink_user_id": r.get("ilink_user_id"),
                            "prev_ilink_user_id": r.get("prev_ilink_user_id"),
                            "ilink_username": r.get("ilink_username"),
                            "bot_token": str(r["bot_token"]),
                            "base_url": str(r["base_url"]),
                            "cdn_base_url": str(r["cdn_base_url"]),
                            "get_updates_buf": str(r.get("get_updates_buf") or ""),
                            "status": str(r["status"]),
                            "bot_generation": int(r["bot_generation"]),
                            "last_inbound_at": _iso(r.get("last_inbound_at")),
                            "updated_at": _iso(r.get("updated_at")),
                        }
                    )
                return {
                    "changed": True,
                    "enabled": True,
                    "allow_external": eff_allow_ext,
                    "max_concurrent_accounts": max_slots,
                    "accounts_revision": rev,
                    "accounts": accounts,
                }


async def get_weixin_account_detail(
    *,
    account_id: str,
    recipient_platform_user_id: str | None = None,
    inbound_message_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """``GET .../internal/weixin/accounts/{account_id}`` (read-only, §3.1)."""
    norm_aid = normalize_uuid(account_id)
    if not norm_aid:
        raise HTTPException(status_code=400, detail="Invalid account_id")

    enc_key = get_encryption_key()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT account_id::text AS account_id,
                           user_id,
                           ilink_bot_id,
                           ilink_user_id,
                           prev_ilink_user_id,
                           ilink_username,
                           pgp_sym_decrypt(encrypted_bot_token, %s) AS bot_token,
                           base_url,
                           cdn_base_url,
                           get_updates_buf,
                           bot_generation,
                           status,
                           last_inbound_at,
                           updated_at
                    FROM dalton_channel_weixin_accounts
                    WHERE account_id = %s::uuid
                    """,
                    (enc_key, norm_aid),
                )
                row = await cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Weixin account not found")

                target_puid = (
                    str(recipient_platform_user_id or "").strip()
                    or str(row.get("ilink_user_id") or "").strip()
                )
                ctx_tok: str | None = None
                ctx_ver: int | None = None
                if target_puid:
                    ctx_tok, ctx_ver, _stale = (
                        await resolve_weixin_account_context_token_in_tx(
                            cur,
                            account_id=norm_aid,
                            bot_generation=int(row["bot_generation"]),
                            platform_user_id=target_puid,
                            enc_key=enc_key,
                        )
                    )
                elif inbound_message_id:
                    await cur.execute(
                        """
                        SELECT pgp_sym_decrypt(encrypted_weixin_context_token, %s) AS ctx
                        FROM dalton_channel_inbound_events
                        WHERE platform = 'weixin'
                          AND message_id = %s
                          AND weixin_account_id = %s::uuid
                          AND weixin_context_bot_generation = %s
                          AND encrypted_weixin_context_token IS NOT NULL
                        """,
                        (
                            enc_key,
                            str(inbound_message_id).strip(),
                            norm_aid,
                            int(row["bot_generation"]),
                        ),
                    )
                    in_row = await cur.fetchone()
                    if in_row and in_row.get("ctx"):
                        ctx_tok = str(in_row["ctx"]).strip() or None

                return {
                    "account_id": str(row["account_id"]),
                    "user_id": str(row["user_id"]),
                    "ilink_bot_id": str(row["ilink_bot_id"]),
                    "ilink_user_id": row.get("ilink_user_id"),
                    "ilink_username": row.get("ilink_username"),
                    "bot_token": str(row["bot_token"]),
                    "base_url": str(row["base_url"]),
                    "cdn_base_url": str(row["cdn_base_url"]),
                    "get_updates_buf": str(row.get("get_updates_buf") or ""),
                    "bot_generation": int(row["bot_generation"]),
                    "status": str(row["status"]),
                    "weixin_context_token": ctx_tok,
                    "weixin_context_token_version": ctx_ver,
                }


async def update_weixin_account_cursor(
    *,
    account_id: str,
    gateway_epoch: int,
    expected_bot_generation: int,
    expected_ilink_bot_id: str,
    expected_get_updates_buf: str,
    get_updates_buf: str,
    conn: Any = None,
) -> dict[str, Any]:
    """``POST .../internal/weixin/accounts/{account_id}/cursor`` (§3.1)."""
    norm_aid = normalize_uuid(account_id)
    if not norm_aid:
        raise HTTPException(status_code=400, detail="Invalid account_id")

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await verify_gateway_epoch_in_tx(
                    cur, gateway_epoch, required=True, platform="weixin"
                )
                await cur.execute(
                    """
                    SELECT account_id, ilink_bot_id, bot_generation, get_updates_buf, status
                    FROM dalton_channel_weixin_accounts
                    WHERE account_id = %s::uuid
                    FOR NO KEY UPDATE
                    """,
                    (norm_aid,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Weixin account not found")
                cur_gen = int(row["bot_generation"])
                cur_bot = str(row["ilink_bot_id"])
                cur_buf = str(row.get("get_updates_buf") or "")
                cur_status = str(row["status"])

                if (
                    cur_gen != int(expected_bot_generation)
                    or cur_bot != str(expected_ilink_bot_id or "").strip()
                ):
                    return {
                        "ok": False,
                        "updated": False,
                        "stale_generation": True,
                        "cas_mismatch": False,
                        "bot_generation": cur_gen,
                        "get_updates_buf": cur_buf,
                        "status": cur_status,
                    }

                if cur_buf != str(expected_get_updates_buf or ""):
                    return {
                        "ok": False,
                        "updated": False,
                        "stale_generation": False,
                        "cas_mismatch": True,
                        "bot_generation": cur_gen,
                        "get_updates_buf": cur_buf,
                        "status": cur_status,
                    }

                new_buf = str(get_updates_buf or "")
                await cur.execute(
                    """
                    UPDATE dalton_channel_weixin_accounts
                    SET get_updates_buf = %s, updated_at = NOW()
                    WHERE account_id = %s::uuid
                    """,
                    (new_buf, norm_aid),
                )
                return {
                    "ok": True,
                    "updated": True,
                    "stale_generation": False,
                    "cas_mismatch": False,
                    "bot_generation": cur_gen,
                    "get_updates_buf": new_buf,
                    "status": cur_status,
                }


async def update_weixin_account_status(
    *,
    account_id: str,
    gateway_epoch: int,
    action: str,
    expected_bot_generation: int,
    expected_ilink_bot_id: str,
    error_code: str | None = None,
    recipient_platform_user_id: str | None = None,
    expected_token_version: int | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """``POST .../internal/weixin/accounts/{account_id}/status`` (§3.1, §5.2)."""
    norm_aid = normalize_uuid(account_id)
    if not norm_aid:
        raise HTTPException(status_code=400, detail="Invalid account_id")
    cleaned_action = str(action or "").strip()
    if cleaned_action not in (
        "mark_session_expired",
        "mark_context_token_stale",
        "reset_corrupt_cursor",
    ):
        raise HTTPException(status_code=400, detail=f"Unsupported status action: {action!r}")

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await verify_gateway_epoch_in_tx(
                    cur, gateway_epoch, required=True, platform="weixin"
                )
                lock_mode = (
                    "FOR UPDATE"
                    if cleaned_action == "mark_session_expired"
                    else "FOR NO KEY UPDATE"
                )
                await cur.execute(
                    f"""
                    SELECT account_id, ilink_bot_id, ilink_user_id, bot_generation, status
                    FROM dalton_channel_weixin_accounts
                    WHERE account_id = %s::uuid
                    {lock_mode}
                    """,
                    (norm_aid,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Weixin account not found")
                cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                cur_rev = int(cfg["accounts_revision"])
                cur_gen = int(row["bot_generation"])
                cur_bot = str(row["ilink_bot_id"])
                if (
                    cur_gen != int(expected_bot_generation)
                    or cur_bot != str(expected_ilink_bot_id or "").strip()
                ):
                    return {
                        "ok": False,
                        "updated": False,
                        "stale_generation": True,
                        "version_mismatch": False,
                        "accounts_revision": cur_rev,
                    }

                if cleaned_action == "mark_session_expired":
                    if str(row["status"]) in ("active", "pending_verification"):
                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_accounts
                            SET status = 'expired',
                                last_error_code = %s,
                                last_error_at = NOW(),
                                updated_at = NOW()
                            WHERE account_id = %s::uuid
                            """,
                            (str(error_code or "-14")[:64], norm_aid),
                        )
                        await ensure_and_lock_weixin_config_in_tx(cur, for_update=True)
                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_config
                            SET accounts_revision = accounts_revision + 1, updated_at = NOW()
                            WHERE singleton_id = 1
                            RETURNING accounts_revision
                            """
                        )
                        cur_rev = int((await cur.fetchone())["accounts_revision"])
                        return {
                            "ok": True,
                            "updated": True,
                            "stale_generation": False,
                            "version_mismatch": False,
                            "accounts_revision": cur_rev,
                        }
                    return {
                        "ok": True,
                        "updated": False,
                        "stale_generation": False,
                        "version_mismatch": False,
                        "accounts_revision": cur_rev,
                    }

                if cleaned_action == "reset_corrupt_cursor":
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_accounts
                        SET get_updates_buf = '',
                            last_error_code = %s,
                            last_error_at = NOW(),
                            updated_at = NOW()
                        WHERE account_id = %s::uuid
                        """,
                        (str(error_code or "corrupt_cursor")[:64], norm_aid),
                    )
                    return {
                        "ok": True,
                        "updated": True,
                        "stale_generation": False,
                        "version_mismatch": False,
                        "accounts_revision": cur_rev,
                    }

                # mark_context_token_stale (§5.2: whitelist + token_version CAS)
                clean_err = str(error_code or "").strip()
                if not clean_err or clean_err not in CONTEXT_TOKEN_STALE_ERRCODES:
                    return {
                        "ok": True,
                        "updated": False,
                        "stale_generation": False,
                        "version_mismatch": False,
                        "accounts_revision": cur_rev,
                    }
                puid = (
                    str(recipient_platform_user_id or "").strip()
                    or str(row.get("ilink_user_id") or "").strip()
                )
                if not puid or expected_token_version is None:
                    return {
                        "ok": False,
                        "updated": False,
                        "stale_generation": False,
                        "version_mismatch": True,
                        "accounts_revision": cur_rev,
                    }
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_weixin_context:{cur_bot}:{cur_gen}:{puid}",),
                )
                await cur.execute(
                    """
                    UPDATE dalton_channel_weixin_account_tokens
                    SET stale_at = NOW(),
                        stale_reason = %s,
                        updated_at = NOW()
                    WHERE account_id = %s::uuid
                      AND bot_generation = %s
                      AND platform_user_id = %s
                      AND token_version = %s
                    RETURNING token_version
                    """,
                    (clean_err[:64], norm_aid, cur_gen, puid, int(expected_token_version)),
                )
                upd = await cur.fetchone()
                if upd is None:
                    return {
                        "ok": False,
                        "updated": False,
                        "stale_generation": False,
                        "version_mismatch": True,
                        "accounts_revision": cur_rev,
                    }
                return {
                    "ok": True,
                    "updated": True,
                    "stale_generation": False,
                    "version_mismatch": False,
                    "accounts_revision": cur_rev,
                }


# ---------------------------------------------------------------------------
# QR Login Sessions (`starting` -> `wait` -> `scaned`/`verify_code` -> `confirmed`)
# ---------------------------------------------------------------------------


async def start_weixin_login_session(
    *,
    user_id: str,
    conn: Any = None,
) -> dict[str, Any]:
    """Pre-write a ``status = 'starting'`` login session BEFORE calling iLink (D14)."""
    cleaned_uid = str(user_id or "").strip()
    if not cleaned_uid:
        raise HTTPException(status_code=400, detail="user_id is required")
    reject_company_system_auth(cleaned_uid)

    if conn is not None:
        async with conn.transaction():
            member_status = await check_team_member_status_live(cleaned_uid, conn=conn)
            user_tier = (
                await get_user_tier(cleaned_uid, conn=conn)
                if member_status == "active"
                else "external"
            )
    else:
        member_status = await check_team_member_status_live(cleaned_uid, conn=None)
        user_tier = (
            await get_user_tier(cleaned_uid, conn=None)
            if member_status == "active"
            else "external"
        )
    if member_status == "lookup_error":
        raise HTTPException(
            status_code=503,
            detail={"code": "roster_lookup_error", "message": "成员名册查询异常，请稍后重试"},
        )
    if member_status != "active":
        raise HTTPException(
            status_code=403,
            detail={"code": "member_not_in_roster", "message": "当前账号不在团队成员名册中"},
        )
    max_slots = get_weixin_max_concurrent_accounts()

    async def _attempt(attempt_conn: Any) -> dict[str, Any]:
        async with get_db_connection(attempt_conn) as owned_conn:
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    # Level 1 user lock
                    await lock_user_settings_in_tx(cur, [cleaned_uid])
                    if _after_user_locks_hook is not None:
                        await _after_user_locks_hook()

                    # Level 3a: check existing account for this user
                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, status, bot_generation
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                        FOR UPDATE
                        """,
                        (cleaned_uid,),
                    )
                    existing_acc = await cur.fetchone()
                    cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                    access_ok, deny_code = evaluate_weixin_member_access(
                        user_tier,
                        channel_enabled=bool(cfg["enabled"]),
                        db_allow_external=cfg.get("allow_external"),
                    )
                    if not access_ok:
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": deny_code or "weixin_channel_disabled",
                                "message": "当前无法连接个人微信",
                            },
                        )

                    already_holds_slot = existing_acc is not None and str(
                        existing_acc["status"]
                    ) in ("active", "pending_verification")
                    if not already_holds_slot:
                        cur_slots = await _count_active_poller_slots_in_tx(
                            cur, exclude_user_id=cleaned_uid
                        )
                        if cur_slots >= max_slots:
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "code": WEIXIN_CAPACITY_FULL_CODE,
                                    "message": f"单实例微信轮询槽位已满（上限 {max_slots}）",
                                },
                            )

                    # Level 4: supersede any previous pending session and insert starting
                    await _supersede_pending_login_sessions_in_tx(cur, cleaned_uid)
                    session_key = f"wxlogin_{secrets.token_urlsafe(24)}"
                    target_aid = (
                        str(existing_acc["account_id"]) if existing_acc else None
                    )
                    expected_gen = (
                        int(existing_acc["bot_generation"]) if existing_acc else None
                    )
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_weixin_login_sessions (
                            session_key, user_id, target_account_id,
                            login_version, expected_bot_generation,
                            status, encrypted_qrcode, qrcode_img_content,
                            redirect_base_url, expires_at, created_at, updated_at
                        )
                        VALUES (
                            %s, %s, %s::uuid,
                            1, %s,
                            'starting', NULL, NULL,
                            %s, NOW() + INTERVAL '60 seconds', NOW(), NOW()
                        )
                        RETURNING session_key, user_id, target_account_id::text AS target_account_id,
                                  login_version, expected_bot_generation, status, expires_at
                        """,
                        (
                            session_key,
                            cleaned_uid,
                            target_aid,
                            expected_gen,
                            DEFAULT_BASE_URL,
                        ),
                    )
                    inserted = dict(await cur.fetchone())
                    return {
                        "session_key": str(inserted["session_key"]),
                        "user_id": str(inserted["user_id"]),
                        "target_account_id": inserted.get("target_account_id"),
                        "login_version": int(inserted["login_version"]),
                        "expected_bot_generation": inserted.get("expected_bot_generation"),
                        "status": str(inserted["status"]),
                        "expires_at": _iso(inserted.get("expires_at")),
                    }

    return await _run_tx_with_retry("start_weixin_login_session", conn, _attempt)


async def get_weixin_login_session(
    *,
    session_key: str,
    user_id: str | None = None,
    include_qrcode: bool = True,
    conn: Any = None,
) -> dict[str, Any]:
    """``GET .../internal/weixin/login-sessions/{session_key}`` (D7)."""
    cleaned_key = str(session_key or "").strip()
    if not cleaned_key:
        raise HTTPException(status_code=400, detail="session_key is required")

    enc_key = get_encryption_key() if include_qrcode else None
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                if include_qrcode and enc_key:
                    await cur.execute(
                        """
                        SELECT session_key, user_id, target_account_id::text AS target_account_id,
                               login_version, expected_bot_generation, status,
                               CASE WHEN encrypted_qrcode IS NOT NULL
                                    THEN pgp_sym_decrypt(encrypted_qrcode, %s)
                                    ELSE NULL END AS qrcode,
                               qrcode_img_content, redirect_base_url, expires_at,
                               confirmed_result, created_at, updated_at,
                               clock_timestamp() AS db_now
                        FROM dalton_channel_weixin_login_sessions
                        WHERE session_key = %s
                        """,
                        (enc_key, cleaned_key),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT session_key, user_id, target_account_id::text AS target_account_id,
                               login_version, expected_bot_generation, status,
                               qrcode_img_content, redirect_base_url, expires_at,
                               confirmed_result, created_at, updated_at,
                               clock_timestamp() AS db_now
                        FROM dalton_channel_weixin_login_sessions
                        WHERE session_key = %s
                        """,
                        (cleaned_key,),
                    )
                row = await cur.fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="Login session not found")
                if user_id and str(row["user_id"]) != str(user_id).strip():
                    raise HTTPException(status_code=404, detail="Login session not found")

                status = str(row["status"])
                db_now = _coerce_utc(row.get("db_now")) or datetime.now(timezone.utc)
                expires_at = _coerce_utc(row.get("expires_at"))

                if status == "confirmed":
                    confirmed = (
                        dict(row.get("confirmed_result") or {})
                        if isinstance(row.get("confirmed_result"), Mapping)
                        else {}
                    )
                    conf_aid = normalize_uuid(confirmed.get("account_id"))
                    conf_gen = confirmed.get("bot_generation")
                    still_valid = False
                    if conf_aid and conf_gen is not None:
                        await cur.execute(
                            """
                            SELECT account_id, user_id, status, bot_generation
                            FROM dalton_channel_weixin_accounts
                            WHERE account_id = %s::uuid
                            """,
                            (conf_aid,),
                        )
                        acc = await cur.fetchone()
                        if (
                            acc is not None
                            and str(acc["user_id"]) == str(row["user_id"])
                            and str(acc["status"]) in ("active", "pending_verification")
                            and int(acc["bot_generation"]) == int(conf_gen)
                        ):
                            still_valid = True
                    if not still_valid:
                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_login_sessions
                            SET status = 'expired',
                                qrcode_img_content = NULL,
                                updated_at = NOW()
                            WHERE session_key = %s
                            """,
                            (cleaned_key,),
                        )
                        return {
                            "session_key": cleaned_key,
                            "user_id": str(row["user_id"]),
                            "status": "expired",
                            "connected": False,
                            "already_connected": False,
                            "login_version": int(row["login_version"]),
                            "expires_at": _iso(expires_at),
                        }
                    shaped = _shape_confirmed_result(confirmed)
                    shaped["connected"] = True
                    shaped["already_connected"] = True
                    live_account_status = str(acc["status"]) if acc is not None else None
                    if live_account_status:
                        shaped["account_status"] = live_account_status
                    return {
                        "session_key": cleaned_key,
                        "user_id": str(row["user_id"]),
                        "status": "confirmed",
                        "account_status": live_account_status,
                        "login_version": int(row["login_version"]),
                        "expires_at": _iso(expires_at),
                        "confirmed_result": shaped,
                        **shaped,
                    }

                if (
                    status in ("starting", "wait", "scaned", "verify_code")
                    and expires_at is not None
                    and expires_at <= db_now
                ):
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_login_sessions
                        SET status = 'expired',
                            qrcode_img_content = NULL,
                            updated_at = NOW()
                        WHERE session_key = %s
                        """,
                        (cleaned_key,),
                    )
                    status = "expired"

                resp: dict[str, Any] = {
                    "session_key": cleaned_key,
                    "user_id": str(row["user_id"]),
                    "target_account_id": row.get("target_account_id"),
                    "login_version": int(row["login_version"]),
                    "expected_bot_generation": row.get("expected_bot_generation"),
                    "status": status,
                    "qrcode_img_content": (
                        row.get("qrcode_img_content")
                        if status in ("wait", "scaned", "verify_code")
                        else None
                    ),
                    "redirect_base_url": str(row.get("redirect_base_url") or DEFAULT_BASE_URL),
                    "expires_at": _iso(expires_at),
                    "connected": False,
                    "already_connected": False,
                }
                if include_qrcode:
                    resp["qrcode"] = (
                        row.get("qrcode")
                        if status in ("wait", "scaned", "verify_code")
                        else None
                    )
                return resp


async def patch_weixin_login_session(
    *,
    session_key: str,
    gateway_epoch: int,
    action: str,
    expected_login_version: int | None = None,
    expected_bot_generation: int | None = None,
    status: str | None = None,
    qrcode: str | None = None,
    qrcode_img_content: str | None = None,
    redirect_base_url: str | None = None,
    expires_in_seconds: int | None = None,
    ilink_bot_id: str | None = None,
    bot_token: str | None = None,
    base_url: str | None = None,
    cdn_base_url: str | None = None,
    ilink_user_id: str | None = None,
    ilink_username: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """``PATCH .../internal/weixin/login-sessions/{session_key}`` (``save_qr`` | ``update`` | ``confirm``)."""
    cleaned_key = str(session_key or "").strip()
    cleaned_action = str(action or "").strip().lower()
    if not cleaned_key:
        raise HTTPException(status_code=400, detail="session_key is required")
    if cleaned_action not in ("save_qr", "update", "confirm"):
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action!r}")

    if cleaned_action == "save_qr":
        return await _patch_login_session_save_qr(
            session_key=cleaned_key,
            gateway_epoch=gateway_epoch,
            qrcode=qrcode,
            qrcode_img_content=qrcode_img_content,
            redirect_base_url=redirect_base_url,
            conn=conn,
        )
    if cleaned_action == "update":
        return await _patch_login_session_update(
            session_key=cleaned_key,
            gateway_epoch=gateway_epoch,
            expected_login_version=expected_login_version,
            status=status,
            redirect_base_url=redirect_base_url,
            conn=conn,
        )
    return await _patch_login_session_confirm(
        session_key=cleaned_key,
        gateway_epoch=gateway_epoch,
        expected_login_version=expected_login_version,
        expected_bot_generation=expected_bot_generation,
        ilink_bot_id=ilink_bot_id,
        bot_token=bot_token,
        base_url=base_url,
        cdn_base_url=cdn_base_url,
        ilink_user_id=ilink_user_id,
        ilink_username=ilink_username,
        conn=conn,
    )


async def _patch_login_session_save_qr(
    *,
    session_key: str,
    gateway_epoch: int,
    qrcode: str | None,
    qrcode_img_content: str | None,
    redirect_base_url: str | None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_qr = str(qrcode or "").strip()
    if not cleaned_qr:
        raise HTTPException(status_code=400, detail="qrcode is required for save_qr")
    enc_key = get_encryption_key()

    async def _attempt(attempt_conn: Any) -> dict[str, Any]:
        async with get_db_connection(attempt_conn) as owned_conn:
            # Phase 1: short read transaction so non-autocommit connections return to IDLE
            # before Phase 2 acquires Level 0b/1/4 locks in a real top-level transaction.
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT user_id FROM dalton_channel_weixin_login_sessions WHERE session_key = %s",
                        (session_key,),
                    )
                    peek = await cur.fetchone()
                    if peek is None:
                        raise HTTPException(
                            status_code=409,
                            detail={"code": LOGIN_SESSION_REVOKED_CODE, "message": "扫码会话已撤销或不存在"},
                        )
                    owner_uid = str(peek["user_id"])

                member_status = await check_team_member_status_live(owner_uid, conn=owned_conn)
                if member_status == "lookup_error":
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "roster_lookup_error", "message": "成员名册查询异常"},
                    )
                if member_status != "active":
                    raise HTTPException(
                        status_code=409,
                        detail={"code": LOGIN_SESSION_REVOKED_CODE, "message": "成员资格已失效，扫码会话已撤销"},
                    )
                user_tier = await get_user_tier(owner_uid, conn=owned_conn)

            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await verify_gateway_epoch_in_tx(
                    cur, gateway_epoch, required=True, platform="weixin"
                )
                    await lock_user_settings_in_tx(cur, [owner_uid])
                    cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                    access_ok, deny_code = evaluate_weixin_member_access(
                        user_tier,
                        channel_enabled=bool(cfg["enabled"]),
                        db_allow_external=cfg.get("allow_external"),
                    )
                    if not access_ok:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": deny_code or LOGIN_SESSION_REVOKED_CODE,
                                "message": "渠道或外部成员开关已关闭，扫码会话已撤销",
                            },
                        )
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_login_sessions
                        SET status = 'wait',
                            encrypted_qrcode = pgp_sym_encrypt(%s, %s),
                            qrcode_img_content = %s,
                            redirect_base_url = COALESCE(NULLIF(%s::text, ''), redirect_base_url),
                            expires_at = NOW() + INTERVAL '5 minutes',
                            login_version = 2,
                            updated_at = NOW()
                        WHERE session_key = %s
                          AND user_id = %s
                          AND status = 'starting'
                          AND login_version = 1
                          AND expires_at > NOW()
                        RETURNING session_key, status, login_version, expires_at, redirect_base_url
                        """,
                        (
                            cleaned_qr,
                            enc_key,
                            qrcode_img_content,
                            str(redirect_base_url or "").strip(),
                            session_key,
                            owner_uid,
                        ),
                    )
                    updated = await cur.fetchone()
                    if updated is None:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": LOGIN_SESSION_REVOKED_CODE,
                                "message": "扫码会话已撤销或已过期",
                            },
                        )
                    return {
                        "ok": True,
                        "revoked": False,
                        "session_key": str(updated["session_key"]),
                        "status": str(updated["status"]),
                        "login_version": int(updated["login_version"]),
                        "expires_at": _iso(updated["expires_at"]),
                        "redirect_base_url": str(updated["redirect_base_url"]),
                    }

    return await _run_tx_with_retry("save_qr_weixin_login_session", conn, _attempt)


async def _patch_login_session_update(
    *,
    session_key: str,
    gateway_epoch: int,
    expected_login_version: int | None,
    status: str | None,
    redirect_base_url: str | None,
    conn: Any = None,
) -> dict[str, Any]:
    cleaned_status = str(status or "").strip().lower() or None
    if cleaned_status is not None and cleaned_status not in (
        "wait",
        "scaned",
        "verify_code",
        "expired",
    ):
        raise HTTPException(
            status_code=400, detail=f"Invalid update status: {status!r}"
        )

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await verify_gateway_epoch_in_tx(
                    cur, gateway_epoch, required=True, platform="weixin"
                )
                ver_clause = (
                    " AND login_version = %s"
                    if expected_login_version is not None
                    else ""
                )
                clear_img = cleaned_status == "expired"
                params: list[Any] = [
                    cleaned_status,
                    str(redirect_base_url or "").strip(),
                    clear_img,
                    session_key,
                ]
                if expected_login_version is not None:
                    params.append(int(expected_login_version))

                await cur.execute(
                    f"""
                    UPDATE dalton_channel_weixin_login_sessions
                    SET status = COALESCE(%s::text, status),
                        redirect_base_url = COALESCE(NULLIF(%s::text, ''), redirect_base_url),
                        qrcode_img_content = CASE WHEN %s::boolean THEN NULL ELSE qrcode_img_content END,
                        login_version = login_version + 1,
                        updated_at = NOW()
                    WHERE session_key = %s
                      AND status IN ('wait', 'scaned', 'verify_code')
                      AND expires_at > NOW()
                      {ver_clause}
                    RETURNING session_key, status, login_version, redirect_base_url, expires_at
                    """,
                    tuple(params),
                )
                updated = await cur.fetchone()
                if updated is None:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": LOGIN_SESSION_REVOKED_CODE,
                            "message": "扫码会话已撤销或已过期",
                        },
                    )
                return {
                    "ok": True,
                    "revoked": False,
                    "session_key": str(updated["session_key"]),
                    "status": str(updated["status"]),
                    "login_version": int(updated["login_version"]),
                    "redirect_base_url": str(updated["redirect_base_url"]),
                    "expires_at": _iso(updated["expires_at"]),
                }


async def _patch_login_session_confirm(
    *,
    session_key: str,
    gateway_epoch: int,
    expected_login_version: int | None,
    expected_bot_generation: int | None = None,
    ilink_bot_id: str | None,
    bot_token: str | None,
    base_url: str | None,
    cdn_base_url: str | None,
    ilink_user_id: str | None,
    ilink_username: str | None,
    conn: Any = None,
) -> dict[str, Any]:
    """Atomic two-phase lock confirmation (§3.2 #2, D5, D6, D14, D15, D18, Codex ①)."""
    cleaned_bot = str(ilink_bot_id or "").strip()
    cleaned_token = str(bot_token or "").strip()
    cleaned_wx_uid = str(ilink_user_id or "").strip() or None
    cleaned_wx_name = str(ilink_username or "").strip() or None
    cleaned_base = str(base_url or "").strip() or DEFAULT_BASE_URL
    cleaned_cdn = str(cdn_base_url or "").strip() or DEFAULT_CDN_BASE_URL
    if not cleaned_bot or not cleaned_token:
        raise HTTPException(
            status_code=400, detail="ilink_bot_id and bot_token are required for confirm"
        )
    enc_key = get_encryption_key()
    max_slots = get_weixin_max_concurrent_accounts()

    async def _attempt(attempt_conn: Any) -> dict[str, Any]:
        async with get_db_connection(attempt_conn) as owned_conn:
            # Phase 1: short read transaction so non-autocommit connections return to IDLE
            # before hooks and before Phase 2's top-level transaction.
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT session_key, user_id, target_account_id::text AS target_account_id,
                               login_version, expected_bot_generation, status
                        FROM dalton_channel_weixin_login_sessions
                        WHERE session_key = %s
                        """,
                        (session_key,),
                    )
                    sess_peek = await cur.fetchone()
                    if sess_peek is None:
                        raise HTTPException(
                            status_code=409,
                            detail={"code": LOGIN_SESSION_REVOKED_CODE, "message": "扫码会话已撤销或不存在"},
                        )
                    owner_uid = str(sess_peek["user_id"])
                    reject_company_system_auth(owner_uid)

                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, user_id, ilink_bot_id,
                               ilink_user_id, prev_ilink_user_id
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                           OR ilink_bot_id = %s
                           OR (%s::text IS NOT NULL AND COALESCE(ilink_user_id, prev_ilink_user_id) = %s::text)
                        """,
                        (owner_uid, cleaned_bot, cleaned_wx_uid, cleaned_wx_uid),
                    )
                    discovered_accs = [dict(r) for r in await cur.fetchall()]

            if _after_discovery_hook is not None:
                await _after_discovery_hook()

            # Live roster + tier check in a short committed read transaction before Phase 2
            async with owned_conn.transaction():
                member_status = await check_team_member_status_live(owner_uid, conn=owned_conn)
                if member_status == "lookup_error":
                    raise HTTPException(
                        status_code=503,
                        detail={"code": "roster_lookup_error", "message": "成员名册查询异常"},
                    )
                if member_status != "active":
                    raise HTTPException(
                        status_code=403,
                        detail={"code": "member_not_in_roster", "message": "成员已不在团队名册中"},
                    )
                user_tier = await get_user_tier(owner_uid, conn=owned_conn)

            discovered_uids = _sorted_unique_strs(
                owner_uid, [r["user_id"] for r in discovered_accs]
            )
            discovered_aids = _sorted_unique_strs(
                [r["account_id"] for r in discovered_accs]
            )
            discovered_bots = _sorted_unique_strs(
                cleaned_bot, [r.get("ilink_bot_id") for r in discovered_accs]
            )
            discovered_wx_uids = _sorted_unique_strs(
                cleaned_wx_uid,
                [r.get("ilink_user_id") for r in discovered_accs],
                [r.get("prev_ilink_user_id") for r in discovered_accs],
            )

            # Phase 2: strict lock order Level 0b -> Level 1 -> Level 3a -> Level 3b -> Level 4 -> Level 6/7 -> Level 8
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await verify_gateway_epoch_in_tx(
                    cur, gateway_epoch, required=True, platform="weixin"
                )
                    locked_uids = await lock_user_settings_in_tx(cur, discovered_uids)
                    if _after_user_locks_hook is not None:
                        await _after_user_locks_hook()
                    locked_accs = await lock_weixin_accounts_in_tx(
                        cur, discovered_aids, for_update=True
                    )
                    await lock_weixin_bot_and_users_in_tx(
                        cur, bot_ids=discovered_bots, ilink_user_ids=discovered_wx_uids
                    )

                    # Re-verify no new matching accounts/users appeared after locks
                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, user_id, ilink_bot_id,
                               ilink_user_id, prev_ilink_user_id
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                           OR ilink_bot_id = %s
                           OR (%s::text IS NOT NULL AND COALESCE(ilink_user_id, prev_ilink_user_id) = %s::text)
                        """,
                        (owner_uid, cleaned_bot, cleaned_wx_uid, cleaned_wx_uid),
                    )
                    recheck_rows = [dict(r) for r in await cur.fetchall()]
                    req_uids = set(
                        _sorted_unique_strs(
                            owner_uid, [r["user_id"] for r in recheck_rows]
                        )
                    )
                    req_aids = set(
                        _sorted_unique_strs([r["account_id"] for r in recheck_rows])
                    )
                    if not req_uids.issubset(locked_uids) or not req_aids.issubset(
                        set(locked_accs.keys())
                    ):
                        raise WeixinLockSetChanged()

                    # Level 4: lock login session & validate status condition
                    await cur.execute(
                        """
                        SELECT *
                        FROM dalton_channel_weixin_login_sessions
                        WHERE session_key = %s
                        FOR UPDATE
                        """,
                        (session_key,),
                    )
                    sess = await cur.fetchone()
                    db_now = await _fetch_db_now(cur)
                    if (
                        sess is None
                        or str(sess["status"]) not in ("wait", "scaned", "verify_code")
                        or (
                            expected_login_version is not None
                            and int(sess["login_version"]) != int(expected_login_version)
                        )
                        or (
                            _coerce_utc(sess.get("expires_at")) is not None
                            and _coerce_utc(sess.get("expires_at")) <= db_now
                        )
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": LOGIN_SESSION_REVOKED_CODE,
                                "message": "扫码会话已撤销或已过期",
                            },
                        )

                    # Locate existing own account & any conflicting accounts
                    own_acc = next(
                        (a for a in locked_accs.values() if str(a["user_id"]) == owner_uid),
                        None,
                    )
                    expected_gen = (
                        expected_bot_generation
                        if expected_bot_generation is not None
                        else sess.get("expected_bot_generation")
                    )
                    if expected_gen is not None and own_acc is not None:
                        if int(own_acc["bot_generation"]) != int(expected_gen):
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "code": "stale_bot_generation",
                                    "message": "原账号已被其他扫码更新，请重新扫码",
                                },
                            )

                    # Re-check runtime switch
                    cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                    access_ok, deny_code = evaluate_weixin_member_access(
                        user_tier,
                        channel_enabled=bool(cfg["enabled"]),
                        db_allow_external=cfg.get("allow_external"),
                    )
                    if not access_ok:
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": deny_code or "weixin_channel_disabled",
                                "message": "渠道或外部成员开关已关闭",
                            },
                        )

                    # Check ilink_bot_id conflict with another member
                    bot_conflict = next(
                        (
                            a
                            for a in locked_accs.values()
                            if str(a["ilink_bot_id"]) == cleaned_bot
                            and str(a["user_id"]) != owner_uid
                        ),
                        None,
                    )
                    if bot_conflict is not None:
                        if (
                            str(bot_conflict["status"]) == "disabled"
                            and bot_conflict.get("disabled_reason") == "member_not_in_roster"
                        ):
                            old_aid = str(bot_conflict["account_id"])
                            await cur.execute(
                                "DELETE FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                                (old_aid,),
                            )
                            await write_weixin_audit_log_in_tx(
                                cur,
                                action="disabled_account_reclaimed",
                                operator_user_id=owner_uid,
                                target_user_id=str(bot_conflict["user_id"]),
                                weixin_account_id=old_aid,
                                ilink_bot_id=str(bot_conflict["ilink_bot_id"]),
                                ilink_user_id=bot_conflict.get("ilink_user_id")
                                or bot_conflict.get("prev_ilink_user_id"),
                            )
                        else:
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "code": ILINK_USER_ALREADY_BOUND_CODE,
                                    "message": ILINK_USER_ALREADY_BOUND_MESSAGE,
                                },
                            )

                    # Check ilink_user_id occupation by another member (D6)
                    if cleaned_wx_uid:
                        for other in list(locked_accs.values()):
                            if str(other["user_id"]) == owner_uid:
                                continue
                            other_wx = other.get("ilink_user_id") or other.get(
                                "prev_ilink_user_id"
                            )
                            if other_wx != cleaned_wx_uid:
                                continue
                            if (
                                str(other["status"]) == "disabled"
                                and other.get("disabled_reason") == "member_not_in_roster"
                            ):
                                old_aid = str(other["account_id"])
                                await cur.execute(
                                    "DELETE FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                                    (old_aid,),
                                )
                                await write_weixin_audit_log_in_tx(
                                    cur,
                                    action="disabled_account_reclaimed",
                                    operator_user_id=owner_uid,
                                    target_user_id=str(other["user_id"]),
                                    weixin_account_id=old_aid,
                                    ilink_bot_id=str(other["ilink_bot_id"]),
                                    ilink_user_id=other_wx,
                                )
                            else:
                                raise HTTPException(
                                    status_code=409,
                                    detail={
                                        "code": ILINK_USER_ALREADY_BOUND_CODE,
                                        "message": ILINK_USER_ALREADY_BOUND_MESSAGE,
                                    },
                                )

                    # Slot capacity gate (D18): rescan of an already-slotted own account passes
                    already_held_slot = own_acc is not None and str(own_acc["status"]) in (
                        "active",
                        "pending_verification",
                    )
                    other_slots = await _count_active_poller_slots_in_tx(
                        cur, exclude_user_id=owner_uid
                    )
                    if other_slots >= max_slots:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": WEIXIN_CAPACITY_FULL_CODE,
                                "message": f"单实例微信轮询槽位已满（上限 {max_slots}）",
                            },
                        )

                    # Upsert account row & handle rescan generation / watches / sessions
                    if own_acc is not None:
                        aid = str(own_acc["account_id"])
                        old_gen = int(own_acc["bot_generation"])
                        new_gen = old_gen + 1
                        old_eff_wx = own_acc.get("ilink_user_id") or own_acc.get(
                            "prev_ilink_user_id"
                        )
                        if cleaned_wx_uid:
                            new_status = "active"
                            new_wx_uid = cleaned_wx_uid
                            new_prev_wx = None
                        else:
                            new_status = "pending_verification"
                            new_wx_uid = None
                            new_prev_wx = old_eff_wx

                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_accounts
                            SET ilink_bot_id = %s,
                                ilink_user_id = %s,
                                prev_ilink_user_id = %s,
                                ilink_username = COALESCE(%s::text, ilink_username),
                                encrypted_bot_token = pgp_sym_encrypt(%s, %s),
                                base_url = %s,
                                cdn_base_url = %s,
                                get_updates_buf = '',
                                status = %s,
                                prev_status = NULL,
                                disabled_reason = NULL,
                                prev_bot_generation = %s,
                                bot_generation = %s,
                                bot_generation_updated_at = %s,
                                last_error_code = NULL,
                                last_error_at = NULL,
                                updated_at = %s
                            WHERE account_id = %s::uuid
                            """,
                            (
                                cleaned_bot,
                                new_wx_uid,
                                new_prev_wx,
                                cleaned_wx_name,
                                cleaned_token,
                                enc_key,
                                cleaned_base,
                                cleaned_cdn,
                                new_status,
                                old_gen,
                                new_gen,
                                db_now,
                                db_now,
                                aid,
                            ),
                        )
                        # Clear old generation tokens
                        await cur.execute(
                            """
                            DELETE FROM dalton_channel_weixin_account_tokens
                            WHERE account_id = %s::uuid AND bot_generation <> %s
                            """,
                            (aid, new_gen),
                        )

                        is_switched_wx = (
                            cleaned_wx_uid is not None
                            and old_eff_wx is not None
                            and cleaned_wx_uid != old_eff_wx
                        )
                        if is_switched_wx:
                            await _advance_weixin_sessions_and_close_watches_in_tx(
                                cur,
                                user_id=owner_uid,
                                account_id=aid,
                                scope_key=f"dm:{old_eff_wx}",
                            )
                        else:
                            # Codex ①: Same-account rescan updates thread_watches bot_generation in the SAME tx!
                            await cur.execute(
                                """
                                UPDATE dalton_channel_thread_watches
                                SET weixin_bot_generation = %s,
                                    weixin_recipient_id = COALESCE(%s::text, weixin_recipient_id),
                                    updated_at = %s
                                WHERE platform = 'weixin'
                                  AND weixin_account_id = %s::uuid
                                """,
                                (new_gen, cleaned_wx_uid, db_now, aid),
                            )
                    else:
                        new_gen = 1
                        new_status = "active" if cleaned_wx_uid else "pending_verification"
                        await cur.execute(
                            """
                            INSERT INTO dalton_channel_weixin_accounts (
                                user_id, ilink_bot_id, ilink_user_id, prev_ilink_user_id,
                                ilink_username, encrypted_bot_token, base_url, cdn_base_url,
                                get_updates_buf, status, bot_generation, created_at, updated_at
                            )
                            VALUES (
                                %s, %s, %s, NULL,
                                %s, pgp_sym_encrypt(%s, %s), %s, %s,
                                '', %s, 1, NOW(), NOW()
                            )
                            RETURNING account_id::text AS account_id
                            """,
                            (
                                owner_uid,
                                cleaned_bot,
                                cleaned_wx_uid,
                                cleaned_wx_name,
                                cleaned_token,
                                enc_key,
                                cleaned_base,
                                cleaned_cdn,
                                new_status,
                            ),
                        )
                        aid = str((await cur.fetchone())["account_id"])

                    if new_status == "active":
                        await _ensure_weixin_user_settings_in_tx(cur, owner_uid)

                    # Level 8: conditional config bump (0 rows -> rollback!)
                    default_allow_ext = bool(get_dalton_config().weixin.allow_external)
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_config
                        SET accounts_revision = accounts_revision + 1,
                            updated_at = NOW()
                        WHERE singleton_id = 1
                          AND enabled = TRUE
                          AND (%s <> 'external' OR COALESCE(allow_external, %s) = TRUE)
                        RETURNING accounts_revision
                        """,
                        (user_tier, default_allow_ext),
                    )
                    rev_row = await cur.fetchone()
                    if rev_row is None:
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": "weixin_channel_disabled",
                                "message": "渠道或外部成员开关已关闭，确认已回滚",
                            },
                        )
                    # Re-check poller slot capacity AFTER acquiring Level 8 config lock
                    # so two concurrent confirms at max_slots - 1 serialize and never exceed max_slots.
                    if not already_held_slot:
                        total_slots_after_lock = await _count_active_poller_slots_in_tx(cur)
                        if total_slots_after_lock > max_slots:
                            raise HTTPException(
                                status_code=409,
                                detail={
                                    "code": WEIXIN_CAPACITY_FULL_CODE,
                                    "message": f"单实例微信轮询槽位已满（上限 {max_slots}）",
                                },
                            )
                    new_rev = int(rev_row["accounts_revision"])

                    confirmed_snapshot = _shape_confirmed_result(
                        {
                            "account_id": aid,
                            "connected": True,
                            "already_connected": False,
                            "bot_generation": new_gen,
                            "accounts_revision": new_rev,
                        }
                    )
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_login_sessions
                        SET status = 'confirmed',
                            target_account_id = %s::uuid,
                            qrcode_img_content = NULL,
                            confirmed_result = %s,
                            login_version = login_version + 1,
                            updated_at = NOW()
                        WHERE session_key = %s
                        """,
                        (aid, Jsonb(confirmed_snapshot), session_key),
                    )
                    return {
                        "ok": True,
                        "revoked": False,
                        "status": "confirmed",
                        "account_status": new_status,
                        "ilink_bot_id": cleaned_bot,
                        "ilink_user_id": cleaned_wx_uid,
                        "ilink_username": cleaned_wx_name,
                        "confirmed_result": confirmed_snapshot,
                        **confirmed_snapshot,
                    }

    return await _run_tx_with_retry("confirm_weixin_login_session", conn, _attempt)


# ---------------------------------------------------------------------------
# Unbind & Admin Revoke (Member / Admin by account_id / Admin by user_id)
# ---------------------------------------------------------------------------


async def unbind_weixin_account(
    *,
    target_user_id: str | None = None,
    account_id: str | None = None,
    operator_user_id: str,
    action: str = "member_unbind",
    reason: str | None = None,
    via_wechat_command: bool = False,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    expected_bot_id: str | None = None,
    expected_bot_generation: int | None = None,
    expected_sender_ilink_user_id: str | None = None,
    verify_live_roster: bool = False,
    one_time_context_token: str | None = None,
    one_time_recipient_id: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Atomically unbind a Weixin account or revoke pending login sessions (§1.1 #4, §1.2 #3).

    Strictly follows §2.1 (8) lock order:
    Level 0b -> Level 1 -> Level 3a -> Level 3b -> Level 4 -> Level 6 -> Level 7 -> Level 8.
    """
    norm_aid = normalize_uuid(account_id) if account_id else None
    cleaned_uid = str(target_user_id or "").strip() or None
    cleaned_exp_bot = str(expected_bot_id or "").strip() or None
    cleaned_exp_sender = str(expected_sender_ilink_user_id or "").strip() or None
    if cleaned_uid:
        reject_company_system_auth(cleaned_uid)

    async def _attempt(attempt_conn: Any) -> dict[str, Any]:
        # R7 (Decision #10): Enforce preset reason whitelist for admin_unbind on all entrypoints.
        if action == "admin_unbind":
            validate_admin_unbind_reason(reason)
        # R5 (Decision #2): Defense-in-depth enforcement when via_wechat_command=True
        if via_wechat_command and (
            not norm_aid
            or not cleaned_exp_bot
            or expected_bot_generation is None
            or int(expected_bot_generation) < 1
            or not cleaned_exp_sender
        ):
            if gateway_epoch is not None or require_gateway_epoch:
                async with get_db_connection(attempt_conn) as chk_conn:
                    async with chk_conn.transaction():
                        async with chk_conn.cursor(row_factory=dict_row) as cur_chk:
                            await verify_gateway_epoch_in_tx(
                                cur_chk,
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
        async with get_db_connection(attempt_conn) as owned_conn:
            # Phase 1: discover target user_id and account_id in a short read tx
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    eff_uid = cleaned_uid
                    eff_aid = norm_aid
                    if eff_aid and not eff_uid:
                        await cur.execute(
                            "SELECT user_id FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                            (eff_aid,),
                        )
                        r = await cur.fetchone()
                        if r is None:
                            if via_wechat_command:
                                return {"ok": False, "reason": "not_linked"}
                            raise HTTPException(
                                status_code=404, detail="Weixin account not found"
                            )
                        eff_uid = str(r["user_id"])
                    elif eff_uid and not eff_aid:
                        await cur.execute(
                            "SELECT account_id::text AS account_id FROM dalton_channel_weixin_accounts WHERE user_id = %s",
                            (eff_uid,),
                        )
                        r = await cur.fetchone()
                        if r is not None:
                            eff_aid = str(r["account_id"])
                    if not eff_uid:
                        if via_wechat_command:
                            return {"ok": False, "reason": "not_linked"}
                        raise HTTPException(status_code=404, detail="Weixin account not found")

            # Phase 2: strict lock order
            # Level 0b -> Level 1 -> Level 3a -> Level 3b -> Level 4 -> Level 6 -> Level 7 -> Level 8
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        platform="weixin",
                    )
                    await lock_user_settings_in_tx(cur, [eff_uid])

                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, user_id, ilink_bot_id,
                               ilink_user_id, prev_ilink_user_id, base_url, cdn_base_url,
                               bot_generation, status,
                               pgp_sym_decrypt(encrypted_bot_token, %s) AS bot_token
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s
                        FOR UPDATE
                        """,
                        (get_encryption_key(), eff_uid),
                    )
                    acc = await cur.fetchone()
                    if norm_aid and (acc is None or str(acc["account_id"]) != norm_aid):
                        if via_wechat_command:
                            return {"ok": False, "reason": "not_linked"}
                        raise HTTPException(
                            status_code=404, detail="Weixin account not found"
                        )

                    # Level 3b BEFORE Level 4 (§2.1 (8))
                    if acc is not None:
                        await lock_weixin_bot_and_users_in_tx(
                            cur,
                            bot_ids=[str(acc["ilink_bot_id"])],
                            ilink_user_ids=[
                                acc.get("ilink_user_id"),
                                acc.get("prev_ilink_user_id"),
                            ],
                        )

                    # Authorize Weixin /unbind in the SAME transaction BEFORE any mutation!
                    verified_sender_for_reply: str | None = None
                    if via_wechat_command or cleaned_exp_bot or expected_bot_generation is not None or cleaned_exp_sender or verify_live_roster:
                        if acc is None:
                            return {"ok": False, "reason": "not_linked"}
                        acc_bot = str(acc["ilink_bot_id"])
                        acc_gen = int(acc["bot_generation"])
                        acc_wx_uid = str(acc.get("ilink_user_id") or "").strip() or None
                        acc_prev_wx_uid = str(acc.get("prev_ilink_user_id") or "").strip() or None
                        if cleaned_exp_bot and cleaned_exp_bot != acc_bot:
                            return {
                                "ok": False,
                                "reason": "weixin_bot_generation_mismatch",
                            }
                        if expected_bot_generation is not None and int(expected_bot_generation) != acc_gen:
                            return {
                                "ok": False,
                                "reason": "weixin_bot_generation_mismatch",
                            }
                        if cleaned_exp_sender:
                            expected_owner_wx = (
                                acc_wx_uid
                                if acc_wx_uid is not None
                                else (
                                    acc_prev_wx_uid
                                    if str(acc.get("status") or "") == "pending_verification"
                                    else None
                                )
                            )
                            if not expected_owner_wx or cleaned_exp_sender != expected_owner_wx:
                                return {
                                    "ok": False,
                                    "reason": "weixin_sender_mismatch",
                                }
                            verified_sender_for_reply = expected_owner_wx
                        elif acc_wx_uid:
                            verified_sender_for_reply = acc_wx_uid
                        if verify_live_roster:
                            roster_st = await check_team_member_status_live(
                                eff_uid, conn=owned_conn
                            )
                            if roster_st == "lookup_error":
                                raise HTTPException(
                                    status_code=503,
                                    detail={
                                        "code": "roster_lookup_error",
                                        "message": "成员名册查询异常",
                                    },
                                )
                            if roster_st != "active":
                                return {
                                    "ok": False,
                                    "reason": "member_not_in_roster",
                                }

                    # Level 4: supersede pending login sessions
                    superseded_sessions = await _supersede_pending_login_sessions_in_tx(
                        cur, eff_uid
                    )

                    one_time_reply: dict[str, Any] | None = None
                    deleted_aid: str | None = None
                    deleted_bot: str | None = None
                    deleted_wx_uid: str | None = None
                    updated_gens: dict[str, int] = {}

                    if acc is not None:
                        deleted_aid = str(acc["account_id"])
                        deleted_bot = str(acc["ilink_bot_id"])
                        deleted_wx_uid = acc.get("ilink_user_id") or acc.get(
                            "prev_ilink_user_id"
                        )
                        await _remove_weixin_user_settings_in_tx(cur, eff_uid)
                        updated_gens = await _advance_weixin_sessions_and_close_watches_in_tx(
                            cur,
                            user_id=eff_uid,
                            account_id=deleted_aid,
                            scope_key=(
                                f"dm:{deleted_wx_uid}" if deleted_wx_uid else None
                            ),
                        )
                        # Physical delete releases Weixin ID occupation and cascades account_tokens
                        await cur.execute(
                            "DELETE FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                            (deleted_aid,),
                        )
                        clean_one_time_ctx = str(one_time_context_token or "").strip()
                        if via_wechat_command and clean_one_time_ctx:
                            # D16: ONLY reply to the verified owner sender using the inbound context_token
                            # (never combine current bot credentials with an unverified caller recipient).
                            recip = (
                                verified_sender_for_reply
                                or (
                                    str(one_time_recipient_id or "").strip()
                                    if str(one_time_recipient_id or "").strip()
                                    and str(one_time_recipient_id or "").strip()
                                    == str(deleted_wx_uid or "").strip()
                                    else ""
                                )
                                or str(deleted_wx_uid or "").strip()
                            )
                            if recip and (
                                not str(one_time_recipient_id or "").strip()
                                or str(one_time_recipient_id or "").strip() == recip
                            ):
                                one_time_reply = {
                                    "bot_token": str(acc["bot_token"]),
                                    "base_url": str(acc["base_url"]),
                                    "ilink_bot_id": deleted_bot,
                                    "recipient_id": recip,
                                    "context_token": clean_one_time_ctx,
                                    "text": "已解除当前个人微信连接。如需继续使用，请在设置页重新扫码连接。",
                                }
                    else:
                        await _remove_weixin_user_settings_in_tx(cur, eff_uid)

                    eff_reason = reason
                    if not eff_reason and action == "member_unbind" and via_wechat_command:
                        eff_reason = "成员通过微信 /unbind 指令主动解除连接"

                    await write_weixin_audit_log_in_tx(
                        cur,
                        action=action,
                        operator_user_id=operator_user_id,
                        target_user_id=eff_uid,
                        weixin_account_id=deleted_aid,
                        ilink_bot_id=deleted_bot,
                        ilink_user_id=deleted_wx_uid,
                        reason=eff_reason,
                        metadata={
                            "superseded_login_sessions": superseded_sessions,
                            "via_wechat_command": bool(via_wechat_command),
                            "one_time_reply_issued": one_time_reply is not None,
                        },
                    )

                    await ensure_and_lock_weixin_config_in_tx(cur, for_update=True)
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_config
                        SET accounts_revision = accounts_revision + 1,
                            updated_at = NOW()
                        WHERE singleton_id = 1
                        RETURNING accounts_revision
                        """
                    )
                    new_rev = int((await cur.fetchone())["accounts_revision"])

                    return {
                        "ok": True,
                        "user_id": eff_uid,
                        "platform": "weixin",
                        "account_id": deleted_aid,
                        "deleted_account": deleted_aid is not None,
                        "superseded_login_sessions": superseded_sessions,
                        "generations": updated_gens,
                        "accounts_revision": new_rev,
                        "one_time_weixin_reply": one_time_reply,
                    }

    return await _run_tx_with_retry("unbind_weixin_account", conn, _attempt)


# ---------------------------------------------------------------------------
# Admin Overview & Config Update (§3.1 (2))
# ---------------------------------------------------------------------------


async def get_weixin_admin_overview(*, conn: Any = None) -> dict[str, Any]:
    """``GET /api/v1/dalton/channels/weixin/admin/overview``."""
    max_slots = get_weixin_max_concurrent_accounts()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                db_allow_ext = cfg.get("allow_external")
                eff_allow_ext = (
                    bool(db_allow_ext)
                    if db_allow_ext is not None
                    else bool(get_dalton_config().weixin.allow_external)
                )
                await cur.execute(
                    """
                    SELECT instance_id, epoch, acquired_at, expires_at,
                           (expires_at > clock_timestamp()) AS active
                    FROM dalton_channel_gateway_lease
                    WHERE singleton_id = 1
                    """
                )
                lease_row = await cur.fetchone()
                gateway_lease = (
                    {
                        "instance_id": str(lease_row["instance_id"]),
                        "epoch": int(lease_row["epoch"]),
                        "acquired_at": _iso(lease_row.get("acquired_at")),
                        "expires_at": _iso(lease_row.get("expires_at")),
                        "active": bool(lease_row.get("active")),
                    }
                    if lease_row
                    else None
                )

                await cur.execute(
                    """
                    SELECT a.account_id::text AS account_id,
                           a.user_id,
                           a.ilink_bot_id,
                           a.ilink_user_id,
                           a.prev_ilink_user_id,
                           a.ilink_username,
                           a.status,
                           a.prev_status,
                           a.disabled_reason,
                           a.bot_generation,
                           a.last_inbound_at,
                           a.last_error_code,
                           a.last_error_at,
                           a.created_at,
                           a.updated_at,
                           u.email AS user_email,
                           u.name AS user_name
                    FROM dalton_channel_weixin_accounts a
                    LEFT JOIN users u ON u.user_id = a.user_id
                    ORDER BY a.updated_at DESC
                    """
                )
                acc_rows = [dict(r) for r in await cur.fetchall()]

                await cur.execute(
                    """
                    SELECT s.session_key,
                           s.user_id,
                           s.target_account_id::text AS target_account_id,
                           s.status,
                           s.expires_at,
                           s.updated_at,
                           u.email AS user_email,
                           u.name AS user_name
                    FROM dalton_channel_weixin_login_sessions s
                    LEFT JOIN users u ON u.user_id = s.user_id
                    WHERE s.status IN ('starting', 'wait', 'scaned', 'verify_code')
                      AND s.expires_at > NOW()
                    ORDER BY s.updated_at DESC
                    """
                )
                pending_sessions = [dict(r) for r in await cur.fetchall()]

                await cur.execute(
                    """
                    SELECT audit_id::text AS audit_id,
                           action,
                           operator_user_id,
                           target_user_id,
                           weixin_account_id::text AS weixin_account_id,
                           ilink_bot_id,
                           masked_ilink_user_id,
                           reason,
                           metadata,
                           created_at
                    FROM dalton_channel_weixin_audit_logs
                    ORDER BY created_at DESC
                    LIMIT 50
                    """
                )
                audit_logs = [
                    {
                        "audit_id": str(r["audit_id"]),
                        "action": str(r["action"]),
                        "operator_user_id": str(r["operator_user_id"]),
                        "target_user_id": r.get("target_user_id"),
                        "weixin_account_id": r.get("weixin_account_id"),
                        "ilink_bot_id": r.get("ilink_bot_id"),
                        "masked_ilink_user_id": r.get("masked_ilink_user_id"),
                        "reason": str(r["reason"]),
                        "metadata": r.get("metadata") or {},
                        "created_at": _iso(r.get("created_at")),
                    }
                    for r in await cur.fetchall()
                ]

            members: list[dict[str, Any]] = []
            seen_uids: set[str] = set()
            active_poller_count = 0
            connected_count = 0
            pending_verification_count = 0
            expired_count = 0

            pending_by_uid = {str(s["user_id"]): s for s in pending_sessions}

            for r in acc_rows:
                uid = str(r["user_id"])
                seen_uids.add(uid)
                st = str(r["status"])
                if st in ("active", "pending_verification"):
                    active_poller_count += 1
                if st == "active":
                    connected_count += 1
                elif st == "pending_verification":
                    pending_verification_count += 1
                elif st == "expired":
                    expired_count += 1
                tier = await get_user_tier(uid, conn=owned_conn)
                eff_wx = r.get("ilink_user_id") or r.get("prev_ilink_user_id")
                members.append(
                    {
                        "account_id": str(r["account_id"]),
                        "user_id": uid,
                        "user_email": r.get("user_email"),
                        "user_name": r.get("user_name"),
                        "user_tier": tier,
                        "ilink_bot_id": str(r["ilink_bot_id"]),
                        "ilink_username": r.get("ilink_username"),
                        "masked_ilink_user_id": mask_ilink_user_id(eff_wx),
                        "status": st,
                        "prev_status": r.get("prev_status"),
                        "disabled_reason": r.get("disabled_reason"),
                        "bot_generation": int(r["bot_generation"]),
                        "has_pending_login": uid in pending_by_uid,
                        "pending_login_status": (
                            pending_by_uid[uid]["status"] if uid in pending_by_uid else None
                        ),
                        "last_inbound_at": _iso(r.get("last_inbound_at")),
                        "last_error_code": r.get("last_error_code"),
                        "updated_at": _iso(r.get("updated_at")),
                    }
                )

            for s in pending_sessions:
                uid = str(s["user_id"])
                if uid in seen_uids:
                    continue
                tier = await get_user_tier(uid, conn=owned_conn)
                members.append(
                    {
                        "account_id": None,
                        "user_id": uid,
                        "user_email": s.get("user_email"),
                        "user_name": s.get("user_name"),
                        "user_tier": tier,
                        "ilink_bot_id": None,
                        "ilink_username": None,
                        "masked_ilink_user_id": None,
                        "status": "login_pending",
                        "prev_status": None,
                        "disabled_reason": None,
                        "bot_generation": None,
                        "has_pending_login": True,
                        "pending_login_status": str(s["status"]),
                        "last_inbound_at": None,
                        "last_error_code": None,
                        "updated_at": _iso(s.get("updated_at")),
                    }
                )

            return {
                "enabled": bool(cfg["enabled"]),
                "allow_external": eff_allow_ext,
                "db_allow_external": db_allow_ext,
                "accounts_revision": int(cfg["accounts_revision"]),
                "max_concurrent_accounts": max_slots,
                "active_poller_count": active_poller_count,
                "connected_count": connected_count,
                "pending_verification_count": pending_verification_count,
                "expired_count": expired_count,
                "gateway_lease": gateway_lease,
                "accounts": members,
                "audit_logs": audit_logs,
            }


async def update_weixin_admin_config(
    *,
    operator_user_id: str,
    enabled: bool | None = None,
    allow_external: bool | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """``PUT /api/v1/dalton/channels/weixin/admin/config`` (§3.1 (2), Codex ②).

    Updates the runtime switches, writes audit logs, and invokes the unified
    ``reevaluate_weixin_accounts`` function so disabling external access pauses
    external accounts (saving ``prev_status``) and reopening EITHER switch
    restores eligible paused accounts back to their ``prev_status``.
    """
    cleaned_op = str(operator_user_id or "").strip()
    if not cleaned_op:
        raise HTTPException(status_code=400, detail="operator_user_id is required")

    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=True)
                old_enabled = bool(cfg["enabled"])
                old_db_ext = cfg.get("allow_external")
                default_ext = bool(get_dalton_config().weixin.allow_external)
                old_eff_ext = old_db_ext if old_db_ext is not None else default_ext

                new_enabled = old_enabled if enabled is None else bool(enabled)
                new_db_ext = old_db_ext if allow_external is None else bool(allow_external)
                new_eff_ext = new_db_ext if new_db_ext is not None else default_ext

                await cur.execute(
                    """
                    UPDATE dalton_channel_weixin_config
                    SET enabled = %s,
                        allow_external = %s,
                        accounts_revision = accounts_revision + 1,
                        updated_by = %s,
                        updated_at = NOW()
                    WHERE singleton_id = 1
                    RETURNING enabled, allow_external, accounts_revision, updated_at
                    """,
                    (new_enabled, new_db_ext, cleaned_op),
                )
                upd = dict(await cur.fetchone())

                if enabled is not None and new_enabled != old_enabled:
                    await write_weixin_audit_log_in_tx(
                        cur,
                        action="admin_toggle_channel",
                        operator_user_id=cleaned_op,
                        reason=(
                            "管理员开启团队个人微信接入"
                            if new_enabled
                            else "管理员暂停团队个人微信接入"
                        ),
                        metadata={"old_enabled": old_enabled, "new_enabled": new_enabled},
                    )
                if allow_external is not None and new_eff_ext != old_eff_ext:
                    await write_weixin_audit_log_in_tx(
                        cur,
                        action="admin_toggle_external",
                        operator_user_id=cleaned_op,
                        reason=(
                            "管理员允许外部成员连接个人微信"
                            if new_eff_ext
                            else "管理员关闭外部成员连接个人微信"
                        ),
                        metadata={
                            "old_allow_external": old_eff_ext,
                            "new_allow_external": new_eff_ext,
                        },
                    )

        # Unified re-evaluation handles BOTH disabling external accounts and restoring
        # paused accounts when EITHER enabled or allow_external reopens (Codex ②).
        reeval = await reevaluate_weixin_accounts(
            operator_user_id=cleaned_op,
            conn=owned_conn,
        )
        final_rev = max(int(upd["accounts_revision"]), int(reeval["accounts_revision"]))
        return {
            "ok": True,
            "enabled": bool(upd["enabled"]),
            "allow_external": (
                bool(upd["allow_external"])
                if upd.get("allow_external") is not None
                else default_ext
            ),
            "accounts_revision": final_rev,
            "disabled_count": int(reeval["disabled_count"]),
            "restored_count": int(reeval["restored_count"]),
        }


async def get_member_weixin_binding_view(
    *,
    user_id: str,
    user_tier: str | None = None,
    include_secrets: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """Project one member's Weixin account from ``dalton_channel_weixin_accounts``."""
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            eff_tier = (
                str(user_tier).strip().lower()
                if user_tier is not None
                else await get_user_tier(user_id, conn=owned_conn)
            )
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                access_ok, access_deny = evaluate_weixin_member_access(
                    eff_tier,
                    channel_enabled=bool(cfg["enabled"]),
                    db_allow_external=cfg.get("allow_external"),
                )
                await cur.execute(
                    """
                    SELECT account_id::text AS account_id,
                           user_id,
                           ilink_bot_id,
                           ilink_user_id,
                           prev_ilink_user_id,
                           ilink_username,
                           status,
                           prev_status,
                           disabled_reason,
                           bot_generation,
                           last_inbound_at,
                           created_at,
                           updated_at
                    FROM dalton_channel_weixin_accounts
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                acc = await cur.fetchone()
                if acc is None:
                    return {
                        "linked": False,
                        "platform": "weixin",
                        "status": "paused" if not access_ok else "unconfigured",
                        "channel_enabled": bool(cfg["enabled"]),
                        "member_allowed": access_ok,
                        "deny_reason": access_deny,
                        "account_id": None,
                        "platform_user_id": None,
                        "masked_ilink_user_id": None,
                        "platform_username": None,
                        "ilink_username": None,
                        "active_push_ready": False,
                        "last_inbound_at": None,
                        "updated_at": None,
                    }

                eff_wx = acc.get("ilink_user_id") or acc.get("prev_ilink_user_id")
                st = str(acc["status"])
                ctx_tok: str | None = None
                has_usable_tok = False
                if st == "active" and acc.get("ilink_user_id"):
                    if include_secrets:
                        ctx_tok, _, stale = await resolve_weixin_account_context_token_in_tx(
                            cur,
                            account_id=str(acc["account_id"]),
                            bot_generation=int(acc["bot_generation"]),
                            platform_user_id=str(acc["ilink_user_id"]),
                        )
                        has_usable_tok = bool(ctx_tok) and not stale
                    else:
                        await cur.execute(
                            """
                            SELECT 1
                            FROM dalton_channel_weixin_account_tokens
                            WHERE account_id = %s::uuid
                              AND bot_generation = %s
                              AND platform_user_id = %s
                              AND stale_at IS NULL
                            LIMIT 1
                            """,
                            (
                                str(acc["account_id"]),
                                int(acc["bot_generation"]),
                                str(acc["ilink_user_id"]),
                            ),
                        )
                        has_usable_tok = (await cur.fetchone()) is not None

                display_status = st
                if not access_ok and st in ("active", "pending_verification", "disabled"):
                    display_status = "paused"

                out: dict[str, Any] = {
                    "linked": st in ("active", "pending_verification"),
                    "platform": "weixin",
                    "account_id": str(acc["account_id"]),
                    "status": display_status,
                    "raw_status": st,
                    "disabled_reason": acc.get("disabled_reason"),
                    "channel_enabled": bool(cfg["enabled"]),
                    "member_allowed": access_ok,
                    "deny_reason": access_deny,
                    "platform_user_id": acc.get("ilink_user_id"),
                    "masked_ilink_user_id": mask_ilink_user_id(eff_wx),
                    "platform_username": acc.get("ilink_username"),
                    "ilink_username": acc.get("ilink_username"),
                    "bot_generation": int(acc["bot_generation"]),
                    "active_push_ready": has_usable_tok,
                    "last_inbound_at": _iso(acc.get("last_inbound_at")),
                    "created_at": _iso(acc.get("created_at")),
                    "updated_at": _iso(acc.get("updated_at")),
                }
                if include_secrets:
                    out["weixin_context_token"] = ctx_tok
                return out


# ---------------------------------------------------------------------------
# Outbound Preflight Re-Authorization (`POST .../deliveries/{key}/preflight-send`)
# ---------------------------------------------------------------------------


def _preflight_deny(deny_code: str, *, retryable: bool = False) -> dict[str, Any]:
    return {
        "authorized": False,
        "decision": "defer" if retryable else "deny",
        "retryable": bool(retryable),
        "deny_code": deny_code,
    }


async def preflight_weixin_send(
    *,
    delivery_key: str,
    gateway_epoch: int | str | None,
    delivery_attempt: int | None = None,
    chunk_index: int = 0,
    conn: Any = None,
) -> dict[str, Any]:
    """Real-time re-authorization before every iLink outbound send/upload (§3.2 #4)."""
    cleaned_key = str(delivery_key or "").strip()
    if not cleaned_key:
        raise HTTPException(status_code=400, detail="delivery_key is required")

    async with get_db_connection(conn) as owned_conn:
        # Phase 1a: peek delivery row FIRST so platform capability (404 when disabled)
        # is checked BEFORE parsing/enforcing X-Gateway-Epoch (409) (R5 Decision #7 / R7 Decision #8).
        peek: dict[str, Any] | None = None
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT delivery_key, user_id, platform, status, delivery_attempt,
                           weixin_account_id::text AS weixin_account_id,
                           weixin_bot_id, weixin_bot_generation, weixin_recipient_id
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    """,
                    (cleaned_key,),
                )
                row_p = await cur.fetchone()
                peek = dict(row_p) if row_p is not None else None

        peek_plat = (
            str(peek.get("platform") or "").strip().lower()
            if peek is not None
            else "weixin"
        )
        require_channel_platform_capability(peek_plat or "weixin")
        if peek is not None and peek_plat != "weixin":
            return _preflight_deny("not_weixin_delivery")

        # Parse/validate X-Gateway-Epoch ONLY AFTER platform capability check passes.
        parsed_epoch: int | None = None
        if isinstance(gateway_epoch, bool):
            raise StaleGatewayEpochError("Invalid X-Gateway-Epoch header")
        elif isinstance(gateway_epoch, int):
            if gateway_epoch <= 0:
                raise StaleGatewayEpochError("Invalid X-Gateway-Epoch header")
            parsed_epoch = gateway_epoch
        elif gateway_epoch is not None and str(gateway_epoch).strip():
            try:
                parsed_epoch = int(str(gateway_epoch).strip())
            except ValueError as exc:
                raise StaleGatewayEpochError("Invalid X-Gateway-Epoch header") from exc
            if parsed_epoch <= 0:
                raise StaleGatewayEpochError("Invalid X-Gateway-Epoch header")

        # Phase 1b: short transaction checking epoch FOR SHARE, committed immediately
        # so FOR SHARE is NEVER held across external roster lookup.
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await verify_gateway_epoch_in_tx(
                    cur, parsed_epoch, required=True, platform="weixin"
                )

        if peek is None:
            return _preflight_deny("delivery_not_found")
        if delivery_attempt is None or int(delivery_attempt) <= 0:
            return _preflight_deny("delivery_attempt_required")

        owner_uid = str(peek.get("user_id") or "").strip()
        if owner_uid and not owner_uid.startswith("unlinked:"):
            async with owned_conn.transaction():
                member_status = await check_team_member_status_live(owner_uid, conn=owned_conn)
                if member_status == "lookup_error":
                    return _preflight_deny("roster_lookup_error", retryable=True)
                if member_status != "active":
                    return _preflight_deny("member_not_in_roster")
                user_tier = await get_user_tier(owner_uid, conn=owned_conn)
        else:
            return _preflight_deny("member_not_in_roster")

        enc_key = get_encryption_key()
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await verify_gateway_epoch_in_tx(
                    cur, parsed_epoch, required=True, platform="weixin"
                )
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_delivery:{cleaned_key}",),
                )
                await cur.execute(
                    """
                    SELECT delivery_key, user_id, platform, status, delivery_attempt,
                           weixin_account_id::text AS weixin_account_id,
                           weixin_bot_id, weixin_bot_generation, weixin_recipient_id
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    """,
                    (cleaned_key,),
                )
                d_row = await cur.fetchone()
                if d_row is None:
                    return _preflight_deny("delivery_not_found")
                d_plat = str(d_row.get("platform") or "").strip().lower()
                require_channel_platform_capability(d_plat or "weixin")
                if d_plat != "weixin":
                    return _preflight_deny("not_weixin_delivery")
                if str(d_row.get("status") or "") != "in_flight":
                    return _preflight_deny("delivery_not_in_flight")
                if int(d_row.get("delivery_attempt") or 1) != int(delivery_attempt):
                    return _preflight_deny("delivery_attempt_mismatch")

                cfg = await ensure_and_lock_weixin_config_in_tx(cur, for_update=False)
                access_ok, deny_code = evaluate_weixin_member_access(
                    user_tier,
                    channel_enabled=bool(cfg["enabled"]),
                    db_allow_external=cfg.get("allow_external"),
                )
                if not access_ok:
                    return _preflight_deny(deny_code or "weixin_channel_disabled")

                d_aid = normalize_uuid(d_row.get("weixin_account_id"))
                d_gen = d_row.get("weixin_bot_generation")
                d_recip = str(d_row.get("weixin_recipient_id") or "").strip()
                if not d_aid or d_gen is None or not d_recip:
                    return _preflight_deny("source_generation_unknown")

                await cur.execute(
                    """
                    SELECT account_id::text AS account_id,
                           user_id,
                           ilink_bot_id,
                           ilink_user_id,
                           pgp_sym_decrypt(encrypted_bot_token, %s) AS bot_token,
                           base_url,
                           cdn_base_url,
                           bot_generation,
                           status
                    FROM dalton_channel_weixin_accounts
                    WHERE account_id = %s::uuid
                    """,
                    (enc_key, d_aid),
                )
                acc = await cur.fetchone()
                if acc is None:
                    return _preflight_deny("weixin_account_unbound")
                if str(acc["status"]) != "active":
                    return _preflight_deny(f"weixin_account_{acc['status']}")
                if int(acc["bot_generation"]) != int(d_gen):
                    return _preflight_deny("weixin_bot_generation_superseded")
                if str(acc.get("ilink_user_id") or "").strip() != d_recip:
                    return _preflight_deny("weixin_account_rebound")

                ctx_tok, ctx_ver, is_stale = (
                    await resolve_weixin_account_context_token_in_tx(
                        cur,
                        account_id=d_aid,
                        bot_generation=int(acc["bot_generation"]),
                        platform_user_id=d_recip,
                        enc_key=enc_key,
                    )
                )
                if is_stale:
                    return _preflight_deny("weixin_context_token_stale")
                if not ctx_tok:
                    return _preflight_deny("missing_weixin_context_token")

                return {
                    "authorized": True,
                    "decision": "allow",
                    "retryable": False,
                    "chunk_index": int(chunk_index or 0),
                    "account_id": str(acc["account_id"]),
                    "ilink_bot_id": str(acc["ilink_bot_id"]),
                    "bot_generation": int(acc["bot_generation"]),
                    "bot_token": str(acc["bot_token"]),
                    "base_url": str(acc["base_url"]),
                    "cdn_base_url": str(acc["cdn_base_url"]),
                    "recipient_id": d_recip,
                    "weixin_context_token": ctx_tok,
                    "weixin_context_token_version": ctx_ver,
                }


# ---------------------------------------------------------------------------
# Pending-Verification First Message (D11 & D16)
# ---------------------------------------------------------------------------


class _WeixinAccessDeniedRollback(Exception):
    """Internal control-flow rollback when admin pause/config change wins before commit."""

    def __init__(self, deny_reason: str, account: dict[str, Any] | None = None) -> None:
        super().__init__(deny_reason)
        self.deny_reason = deny_reason
        self.account = account


async def verify_pending_weixin_account_first_message(
    *,
    account_id: str,
    platform_user_id: str,
    expected_bot_id: str | None = None,
    expected_bot_generation: int | None = None,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    inbound_context_token: str | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Two-phase lock verification when ``I-1``/``I-3`` hits ``pending_verification`` (D11, D16).

    Must be called AFTER rolling back any transaction that held Level 3a before Level 1.
    Acquires locks strictly in §2.1 (8) order:
    Level 0b -> Level 1 (all affected users sorted) -> Level 3a (all affected accounts sorted) -> Level 3b.

    Outcomes:
    - ``{"outcome": "verified", "account": {...}, "switched": bool}``:
      sets ``ilink_user_id = platform_user_id, prev_ilink_user_id = NULL, status = 'active'``,
      ensures ``apps.weixin``, and if switched away from ``prev_ilink_user_id``, advances
      sessions and closes old watches.
    - ``{"outcome": "conflict_unbound", "one_time_weixin_reply": {...} | None, "accounts_revision": int}``:
      another member already occupies ``platform_user_id``; cancels active collect buffers,
      physically deletes the ``pending_verification`` account, writes ``conflict_auto_unbound``
      audit log, bumps ``accounts_revision``, and returns a one-time reply envelope (D16).
    - ``{"outcome": "not_pending", "account": {...} | None}``:
      concurrently already transitioned or removed.
    """
    norm_aid = normalize_uuid(account_id)
    cleaned_puid = str(platform_user_id or "").strip()
    if not norm_aid or not cleaned_puid:
        return {"outcome": "not_pending", "account": None}

    async def _attempt(attempt_conn: Any) -> dict[str, Any]:
        async with get_db_connection(attempt_conn) as owned_conn:
            # Phase 1: short read transaction so non-autocommit connections return to IDLE
            # before hooks and before Phase 2's top-level transaction.
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, user_id, ilink_bot_id,
                               ilink_user_id, prev_ilink_user_id
                        FROM dalton_channel_weixin_accounts
                        WHERE account_id = %s::uuid
                           OR COALESCE(ilink_user_id, prev_ilink_user_id) = %s
                        """,
                        (norm_aid, cleaned_puid),
                    )
                    discovered = [dict(r) for r in await cur.fetchall()]

            if _after_discovery_hook is not None:
                await _after_discovery_hook()

            if not any(str(r["account_id"]) == norm_aid for r in discovered):
                return {"outcome": "not_pending", "account": None}

            disc_uids = _sorted_unique_strs([r["user_id"] for r in discovered])
            disc_aids = _sorted_unique_strs([r["account_id"] for r in discovered])
            disc_bots = _sorted_unique_strs([r.get("ilink_bot_id") for r in discovered])
            disc_wx_uids = _sorted_unique_strs(
                cleaned_puid,
                [r.get("ilink_user_id") for r in discovered],
                [r.get("prev_ilink_user_id") for r in discovered],
            )

            # Phase 2: strict lock order Level 0b -> Level 1 -> Level 3a -> Level 3b
            async with owned_conn.transaction():
                async with owned_conn.cursor(row_factory=dict_row) as cur:
                    await verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        platform="weixin",
                    )
                    locked_uids = await lock_user_settings_in_tx(cur, disc_uids)
                    if _after_user_locks_hook is not None:
                        await _after_user_locks_hook()
                    locked_accs = await lock_weixin_accounts_in_tx(
                        cur, disc_aids, for_update=True
                    )
                    await lock_weixin_bot_and_users_in_tx(
                        cur, bot_ids=disc_bots, ilink_user_ids=disc_wx_uids
                    )

                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, user_id
                        FROM dalton_channel_weixin_accounts
                        WHERE account_id = %s::uuid
                           OR COALESCE(ilink_user_id, prev_ilink_user_id) = %s
                        """,
                        (norm_aid, cleaned_puid),
                    )
                    recheck = [dict(r) for r in await cur.fetchall()]
                    req_uids = {str(r["user_id"]) for r in recheck}
                    req_aids = {str(r["account_id"]) for r in recheck}
                    if not req_uids.issubset(locked_uids) or not req_aids.issubset(
                        set(locked_accs.keys())
                    ):
                        raise WeixinLockSetChanged()

                    acc = locked_accs.get(norm_aid)
                    if acc is None:
                        return {"outcome": "not_pending", "account": None}
                    if (
                        expected_bot_id
                        and str(acc["ilink_bot_id"]) != str(expected_bot_id).strip()
                    ) or (
                        expected_bot_generation is not None
                        and int(acc["bot_generation"]) != int(expected_bot_generation)
                    ):
                        return {"outcome": "stale_generation", "account": acc}
                    if str(acc["status"]) != "pending_verification":
                        return {"outcome": "not_pending", "account": acc}

                    owner_uid = str(acc["user_id"])
                    old_prev_wx = acc.get("prev_ilink_user_id")

                    # R7 (Decision #2): Check company auth, live roster, and runtime switches
                    # INSIDE the same transaction BEFORE activating, deleting conflicting accounts,
                    # reclaiming disabled rows, or issuing any one-time reply!
                    reject_company_system_auth(owner_uid)
                    roster_st = await check_team_member_status_live(
                        owner_uid, conn=owned_conn
                    )
                    if roster_st == "lookup_error":
                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "roster_lookup_error",
                                "message": "成员名册查询异常",
                            },
                        )
                    if roster_st != "active":
                        return {
                            "outcome": "denied",
                            "deny_reason": "member_not_in_roster",
                            "account": acc,
                        }
                    user_tier = await get_user_tier(owner_uid, conn=owned_conn)
                    cfg_v = await ensure_and_lock_weixin_config_in_tx(
                        cur, for_update=False
                    )
                    acc_ok, acc_deny = evaluate_weixin_member_access(
                        user_tier,
                        channel_enabled=bool(cfg_v["enabled"]),
                        db_allow_external=cfg_v.get("allow_external"),
                    )
                    if not acc_ok:
                        return {
                            "outcome": "denied",
                            "deny_reason": acc_deny or "weixin_channel_disabled",
                            "account": acc,
                        }

                    # Check occupation conflict with another member
                    conflict_other = await _is_ilink_user_occupied_by_other_in_tx(
                        cur, ilink_user_id=cleaned_puid, exclude_account_id=norm_aid
                    )
                    if conflict_other is not None:
                        # Auto-unbind pending_verification account + audit + D16 one-time reply
                        enc_key = get_encryption_key()
                        await cur.execute(
                            """
                            SELECT pgp_sym_decrypt(encrypted_bot_token, %s) AS bot_token,
                                   base_url, ilink_bot_id
                            FROM dalton_channel_weixin_accounts
                            WHERE account_id = %s::uuid
                            """,
                            (enc_key, norm_aid),
                        )
                        cred_row = await cur.fetchone()
                        # Level 4 BEFORE Level 6 (§2.1 (8))
                        await _supersede_pending_login_sessions_in_tx(cur, owner_uid)
                        await _remove_weixin_user_settings_in_tx(cur, owner_uid)
                        await _advance_weixin_sessions_and_close_watches_in_tx(
                            cur, user_id=owner_uid, account_id=norm_aid
                        )
                        await cur.execute(
                            "DELETE FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                            (norm_aid,),
                        )
                        one_time_reply: dict[str, Any] | None = None
                        clean_ctx = str(inbound_context_token or "").strip()
                        if clean_ctx and cred_row is not None:
                            one_time_reply = {
                                "bot_token": str(cred_row["bot_token"]),
                                "base_url": str(cred_row["base_url"]),
                                "ilink_bot_id": str(cred_row["ilink_bot_id"]),
                                "recipient_id": cleaned_puid,
                                "context_token": clean_ctx,
                                "text": ILINK_USER_ALREADY_BOUND_MESSAGE,
                            }
                        await write_weixin_audit_log_in_tx(
                            cur,
                            action="conflict_auto_unbound",
                            operator_user_id="system:delivery-guard",
                            target_user_id=owner_uid,
                            weixin_account_id=norm_aid,
                            ilink_bot_id=str(acc["ilink_bot_id"]),
                            ilink_user_id=cleaned_puid,
                            metadata={
                                "conflicting_user_id": str(conflict_other["user_id"]),
                                "one_time_reply_issued": one_time_reply is not None,
                            },
                        )
                        cfg_c_locked = await ensure_and_lock_weixin_config_in_tx(
                            cur, for_update=True
                        )
                        c_ok_locked, c_deny_locked = evaluate_weixin_member_access(
                            user_tier,
                            channel_enabled=bool(cfg_c_locked["enabled"]),
                            db_allow_external=cfg_c_locked.get("allow_external"),
                        )
                        if not c_ok_locked:
                            raise _WeixinAccessDeniedRollback(
                                c_deny_locked or "weixin_channel_disabled", acc
                            )
                        default_ext_c = bool(get_dalton_config().weixin.allow_external)
                        await cur.execute(
                            """
                            UPDATE dalton_channel_weixin_config
                            SET accounts_revision = accounts_revision + 1,
                                updated_at = NOW()
                            WHERE singleton_id = 1
                              AND enabled = TRUE
                              AND (%s::text <> 'external' OR COALESCE(allow_external, %s::boolean) = TRUE)
                            RETURNING accounts_revision
                            """,
                            (str(user_tier), default_ext_c),
                        )
                        rev_c_row = await cur.fetchone()
                        if rev_c_row is None:
                            raise _WeixinAccessDeniedRollback(
                                "weixin_channel_disabled", acc
                            )
                        new_rev = int(rev_c_row["accounts_revision"])
                        return {
                            "outcome": "conflict_unbound",
                            "accounts_revision": new_rev,
                            "one_time_weixin_reply": one_time_reply,
                        }

                    # Also reclaim any member_not_in_roster disabled row holding cleaned_puid
                    for other in locked_accs.values():
                        if str(other["account_id"]) == norm_aid:
                            continue
                        other_wx = other.get("ilink_user_id") or other.get(
                            "prev_ilink_user_id"
                        )
                        if (
                            other_wx == cleaned_puid
                            and str(other["status"]) == "disabled"
                            and other.get("disabled_reason") == "member_not_in_roster"
                        ):
                            old_other_aid = str(other["account_id"])
                            await cur.execute(
                                "DELETE FROM dalton_channel_weixin_accounts WHERE account_id = %s::uuid",
                                (old_other_aid,),
                            )
                            await write_weixin_audit_log_in_tx(
                                cur,
                                action="disabled_account_reclaimed",
                                operator_user_id=owner_uid,
                                target_user_id=str(other["user_id"]),
                                weixin_account_id=old_other_aid,
                                ilink_bot_id=str(other["ilink_bot_id"]),
                                ilink_user_id=other_wx,
                            )

                    # Verify & promote pending_verification -> active
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_accounts
                        SET ilink_user_id = %s,
                            prev_ilink_user_id = NULL,
                            status = 'active',
                            updated_at = NOW()
                        WHERE account_id = %s::uuid
                        RETURNING *
                        """,
                        (cleaned_puid, norm_aid),
                    )
                    updated_acc = dict(await cur.fetchone())
                    await _ensure_weixin_user_settings_in_tx(cur, owner_uid)

                    switched = bool(old_prev_wx and old_prev_wx != cleaned_puid)
                    if switched:
                        await _advance_weixin_sessions_and_close_watches_in_tx(
                            cur,
                            user_id=owner_uid,
                            account_id=norm_aid,
                            scope_key=f"dm:{old_prev_wx}",
                        )
                    else:
                        # Fill weixin_recipient_id on any watches backfilled while pending_verification
                        await cur.execute(
                            """
                            UPDATE dalton_channel_thread_watches
                            SET weixin_recipient_id = %s,
                                updated_at = NOW()
                            WHERE platform = 'weixin'
                              AND weixin_account_id = %s::uuid
                              AND weixin_recipient_id IS NULL
                            """,
                            (cleaned_puid, norm_aid),
                        )

                    cfg_v_locked = await ensure_and_lock_weixin_config_in_tx(
                        cur, for_update=True
                    )
                    v_ok_locked, v_deny_locked = evaluate_weixin_member_access(
                        user_tier,
                        channel_enabled=bool(cfg_v_locked["enabled"]),
                        db_allow_external=cfg_v_locked.get("allow_external"),
                    )
                    if not v_ok_locked:
                        raise _WeixinAccessDeniedRollback(
                            v_deny_locked or "weixin_channel_disabled", acc
                        )
                    default_ext_v = bool(get_dalton_config().weixin.allow_external)
                    await cur.execute(
                        """
                        UPDATE dalton_channel_weixin_config
                        SET accounts_revision = accounts_revision + 1,
                            updated_at = NOW()
                        WHERE singleton_id = 1
                          AND enabled = TRUE
                          AND (%s::text <> 'external' OR COALESCE(allow_external, %s::boolean) = TRUE)
                        RETURNING accounts_revision
                        """,
                        (str(user_tier), default_ext_v),
                    )
                    rev_v_row = await cur.fetchone()
                    if rev_v_row is None:
                        raise _WeixinAccessDeniedRollback(
                            "weixin_channel_disabled", acc
                        )
                    new_rev = int(rev_v_row["accounts_revision"])
                    return {
                        "outcome": "verified",
                        "switched": switched,
                        "accounts_revision": new_rev,
                        "account": updated_acc,
                    }

    try:
        return await _run_tx_with_retry(
            "verify_pending_weixin_account_first_message", conn, _attempt
        )
    except _WeixinAccessDeniedRollback as exc:
        return {
            "outcome": "denied",
            "deny_reason": exc.deny_reason,
            "account": exc.account,
        }


# ---------------------------------------------------------------------------
# Backend -> Gateway Sync Nudge (§3.1)
# ---------------------------------------------------------------------------


async def notify_gateway_weixin_accounts_sync(
    *,
    accounts_revision: int,
    reason: str,
) -> dict[str, Any] | None:
    """Best-effort nudge ``POST /internal/weixin/accounts/sync`` to the gateway."""
    if not (
        has_capability(CapabilityId.CHANNEL_GATEWAY)
        and has_capability(CapabilityId.CHANNEL_WEIXIN)
    ):
        return None
    base_url = get_dalton_channel_gateway_url().strip().rstrip("/")
    if not base_url:
        return None
    token = (
        os.getenv("INTERNAL_SERVICE_TOKEN")
        or getattr(env, "INTERNAL_SERVICE_TOKEN", "")
        or ""
    ).strip()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.5)) as client:
            resp = await client.post(
                f"{base_url}/internal/weixin/accounts/sync",
                headers={"X-Service-Token": token},
                json={
                    "accounts_revision": int(accounts_revision),
                    "reason": str(reason),
                },
            )
            if resp.status_code < 400:
                return resp.json()
    except Exception as exc:
        logger.debug(
            "Weixin accounts sync nudge failed (non-fatal, revision=%s, reason=%s): %s",
            accounts_revision,
            reason,
            type(exc).__name__,
        )
    return None


async def verify_weixin_upload_headers(
    *,
    request: Any,
    user_id: str,
    conn: Any = None,
) -> None:
    """R13 (Gateway R6 companion): Verify Weixin account generation & gateway epoch
    before writing uploaded workspace files to sandbox disk.

    Activated ONLY when ``X-Weixin-Account-Id`` or ``X-Weixin-Bot-Generation`` is present
    (Feishu / Discord / Web uploads that omit these headers are untouched).
    Raises:
        409 ``stale_gateway_epoch`` when ``X-Gateway-Epoch`` is missing/invalid/stale;
        409 ``weixin_bot_generation_superseded`` when the account was unbound, belongs to
            another user, is inactive, or its ``bot_generation`` differs from the header;
        400 when ``X-Weixin-Account-Id`` or ``X-Weixin-Bot-Generation`` is malformed.
    """
    headers = getattr(request, "headers", None)
    if headers is None:
        return
    raw_aid = str(headers.get("X-Weixin-Account-Id") or "").strip()
    raw_gen = str(headers.get("X-Weixin-Bot-Generation") or "").strip()
    raw_epoch = str(headers.get("X-Gateway-Epoch") or "").strip()

    if not raw_aid and not raw_gen:
        return

    require_channel_platform_capability("weixin")

    norm_aid = normalize_uuid(raw_aid) if raw_aid else None
    if not norm_aid:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_weixin_upload_headers",
                "message": "X-Weixin-Account-Id must be a valid UUID.",
            },
        )
    try:
        req_gen = int(raw_gen)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_weixin_upload_headers",
                "message": "X-Weixin-Bot-Generation must be a positive integer.",
            },
        ) from None
    if req_gen < 1:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_weixin_upload_headers",
                "message": "X-Weixin-Bot-Generation must be a positive integer.",
            },
        )

    req_epoch: int | None = None
    if raw_epoch:
        try:
            req_epoch = int(raw_epoch)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "stale_gateway_epoch",
                    "message": "Invalid X-Gateway-Epoch header.",
                },
            ) from None

    clean_uid = str(user_id or "").strip()
    async with get_db_connection(conn) as owned_conn:
        async with owned_conn.transaction():
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                await verify_gateway_epoch_in_tx(
                    cur,
                    req_epoch,
                    required=True,
                    platform="weixin",
                )
                await cur.execute(
                    """
                    SELECT account_id::text AS account_id,
                           user_id,
                           status,
                           bot_generation
                    FROM dalton_channel_weixin_accounts
                    WHERE account_id = %s::uuid
                    FOR KEY SHARE
                    """,
                    (norm_aid,),
                )
                acc = await cur.fetchone()
                if (
                    acc is None
                    or str(acc.get("user_id") or "").strip() != clean_uid
                    or str(acc.get("status") or "").strip().lower()
                    not in ("active", "pending_verification")
                    or int(acc.get("bot_generation") or 0) != req_gen
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "weixin_bot_generation_superseded",
                            "message": "Weixin account was unbound or rotated before file upload.",
                        },
                    )
