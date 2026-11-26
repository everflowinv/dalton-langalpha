"""Plan and claim generic automation notifications by ``delivery_id``.

The gateway receives only ``delivery_id``. This module reads the saved
``dalton_notify_deliveries`` payload, re-checks eligibility, and returns one
platform item. Feishu sends the stored Card 2.0. Discord sends the stored
text. Rows that carry images, ``feishu_cards``, or ``__img__`` are refused.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from src.dalton.database.channel_gateway import (
    _lock_weixin_account_for_key_share_in_tx,
)
from src.dalton.notify.channel_transport import UNCERTAIN_NOTIFY_ERROR_CODES
from src.dalton.notify.dispatch import (
    _default_notify_admin,
    _resolve_unit_managed_notify_gate,
    _split_target_channel,
    dalton_kind_for_stored_category,
    evaluate_user_notify_eligibility,
)
from src.dalton.settings import get_eve_tracking_chat_id
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_FEISHU_USER_RE = re.compile(r"^feishu:user:(ou_[A-Za-z0-9_-]+)$")
_FEISHU_CHAT_RE = re.compile(r"^feishu:chat:(oc_[A-Za-z0-9_-]+)$")
_DISCORD_USER_RE = re.compile(r"^discord:user:([0-9]+)$")
_DISCORD_CHANNEL_RE = re.compile(r"^discord:channel:([0-9]{5,32})$")
_WEIXIN_USER_RE = re.compile(r"^weixin:user:([^\s:]{1,255})$")
_GATEWAY_NOTIFY_SOURCES = frozenset(
    {
        "automation_completion",
        "notify_test",
        "report_delivery_admin_notice",
        "report_delivery_missing_profile_notice",
    }
)
_NOTICE_SOURCES = frozenset(
    {
        "report_delivery_admin_notice",
        "report_delivery_missing_profile_notice",
    }
)
_RETRYABLE_CHANNEL_FAILURE = "target_failed"


def automation_notify_delivery_key(delivery_id: str) -> str:
    """Platform idempotency key for the single item of one notify row."""
    return f"automation_notify:{delivery_id}:item:0"


def _normalize_delivery_id(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except ValueError:
        return None


def _payload_mapping(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _contains_img_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return "__img__" in value
    if isinstance(value, Mapping):
        if str(value.get("tag") or "") == "__img__":
            return True
        return any(_contains_img_placeholder(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_img_placeholder(item) for item in value)
    return False


def _media_block_code(payload: Mapping[str, Any], card: Any) -> str | None:
    cards = payload.get("feishu_cards")
    if cards not in (None, []):
        return "feishu_cards_not_supported"
    images = payload.get("images")
    if images not in (None, []):
        return "images_not_supported"
    if _contains_img_placeholder(card):
        return "image_placeholder_not_supported"
    return None


def _parse_target(target: str) -> dict[str, str] | None:
    cleaned = (target or "").strip()
    feishu_user = _FEISHU_USER_RE.match(cleaned)
    if feishu_user:
        recipient = feishu_user.group(1)
        return {
            "platform": "feishu",
            "target_kind": "dm",
            "canonical_target_address": cleaned,
            "recipient_id": recipient,
        }
    feishu_chat = _FEISHU_CHAT_RE.match(cleaned)
    if feishu_chat:
        recipient = feishu_chat.group(1)
        return {
            "platform": "feishu",
            "target_kind": "group",
            "canonical_target_address": cleaned,
            "recipient_id": recipient,
        }
    discord_user = _DISCORD_USER_RE.match(cleaned)
    if discord_user:
        return {
            "platform": "discord",
            "target_kind": "dm",
            "canonical_target_address": "discord:@me",
            "recipient_id": discord_user.group(1),
        }
    discord_channel = _DISCORD_CHANNEL_RE.match(cleaned)
    if discord_channel:
        return {
            "platform": "discord",
            "target_kind": "group",
            "canonical_target_address": cleaned,
            "recipient_id": discord_channel.group(1),
        }
    weixin_user = _WEIXIN_USER_RE.match(cleaned)
    if weixin_user:
        return {
            "platform": "weixin",
            "target_kind": "dm",
            "canonical_target_address": "weixin:@me",
            "recipient_id": weixin_user.group(1),
        }
    return None


def _deny(
    status: str,
    code: str,
    message: str,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "authorized": False,
        "status": status,
        "code": code,
        "message": message,
    }
    body.update(extra)
    return body


def _automation_view(row: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any] | None:
    auto_id = str(row.get("automation_id") or "").strip()
    name = str(payload.get("automation_name") or "").strip()
    category = str(payload.get("category") or "").strip()
    meta: dict[str, Any] = {}
    if category and category != "custom":
        meta = {"dalton_kind": dalton_kind_for_stored_category(category)}
    if not (auto_id or name or meta):
        return None
    view: dict[str, Any] = {"name": name, "metadata": meta}
    if auto_id:
        view["automation_id"] = auto_id
    user_id = str(row.get("user_id") or "").strip()
    if user_id:
        view["user_id"] = user_id
    return view


async def _parent_is_deleted(automation_id: str) -> bool:
    cleaned = (automation_id or "").strip()
    if not cleaned or cleaned.lower().startswith("data:"):
        return False
    try:
        UUID(cleaned)
    except ValueError:
        return False
    from src.dalton.database.automation_recycle import automation_is_soft_deleted

    return await automation_is_soft_deleted(cleaned)


async def _load_delivery_row(cur: Any, delivery_id: str) -> dict[str, Any] | None:
    await cur.execute(
        """
        SELECT d.delivery_id, d.execution_id, d.automation_id, d.user_id, d.channel, d.target,
               d.status, d.attempt_count, d.error_code, d.payload_json,
               ae.conversation_thread_id::text AS conversation_thread_id,
               a.workspace_id::text AS workspace_id
        FROM dalton_notify_deliveries d
        LEFT JOIN automation_executions ae
          ON d.execution_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         AND ae.automation_execution_id = d.execution_id::uuid
        LEFT JOIN automations a
          ON d.automation_id ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         AND a.automation_id = d.automation_id::uuid
        WHERE d.delivery_id = %s::uuid
        """,
        (delivery_id,),
    )
    row = await cur.fetchone()
    return dict(row) if isinstance(row, Mapping) else None


async def _existing_channel_status(cur: Any, delivery_key: str) -> str | None:
    await cur.execute(
        """
        SELECT status
        FROM dalton_channel_deliveries
        WHERE delivery_key = %s
        """,
        (delivery_key,),
    )
    row = await cur.fetchone()
    if not isinstance(row, Mapping):
        return None
    status = str(row.get("status") or "").strip()
    return status or None


def _gateway_notification_block(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Refuse any row this path must not send.

    Accepted rows are ``transport=gateway`` completions and test pushes.
    Data-task ids, report rows, and every other transport are refused.
    """
    target = str(row.get("target") or "").strip()
    auto_id = str(row.get("automation_id") or "").strip()
    route_key = str(payload.get("route_key") or "").strip()
    content_kind = str(payload.get("content_kind") or "").strip()
    source = str(payload.get("source") or "").strip()
    transport = str(payload.get("transport") or "").strip().lower()
    if (
        transport != "gateway"
        or source not in _GATEWAY_NOTIFY_SOURCES
        or auto_id.lower().startswith("data:")
        or route_key.lower().startswith("data:")
        or bool(content_kind)
    ):
        return _deny(
            "failed",
            "not_gateway_notification",
            "Only a gateway automation completion or test push can be sent on this path.",
            requested_target=target,
        )
    return None


async def _dm_identity_matches(
    cur: Any,
    user_id: str,
    target: str,
) -> bool:
    parsed = _parse_target(target)
    if not parsed or parsed.get("target_kind") != "dm":
        return False
    if parsed["platform"] == "weixin":
        await cur.execute(
            """
            SELECT 1 FROM dalton_channel_weixin_accounts
            WHERE user_id = %s
              AND status = 'active'
              AND ilink_user_id = %s
            LIMIT 1
            """,
            (user_id, parsed["recipient_id"]),
        )
        return (await cur.fetchone()) is not None
    await cur.execute(
        """
        SELECT 1 FROM dalton_channel_identities
        WHERE user_id = %s AND platform = %s AND platform_user_id = %s
        LIMIT 1
        """,
        (user_id, parsed["platform"], parsed["recipient_id"]),
    )
    return (await cur.fetchone()) is not None


async def _eligibility(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    target: str,
    *,
    cur: Any = None,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]],
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]],
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]],
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]],
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None,
) -> tuple[bool, str | None]:
    user_id = str(row.get("user_id") or "").strip()
    source = str(payload.get("source") or "")
    if source == "report_delivery_missing_profile_notice":
        if not user_id:
            return False, "missing_user_id"
        if not (target.startswith("feishu:user:") or target.startswith("discord:user:")):
            return False, "invalid_notice_target"
        configured = [
            _split_target_channel(item)[1]
            for item in await resolve_targets_fn(user_id)
            if str(item).strip()
        ]
        if target in configured:
            return True, None
        if cur is not None and await _dm_identity_matches(cur, user_id, target):
            return True, None
        return False, "target_removed"

    if source == "report_delivery_admin_notice":
        if not user_id:
            return False, "missing_user_id"
        from src.dalton.access_tiers import is_admin_user

        is_admin = await is_admin_user(user_id)
        if not is_admin and is_admin_fn is not None:
            is_admin = bool(await is_admin_fn(user_id))
        if not is_admin:
            return False, "not_admin"
        configured = [
            _split_target_channel(item)[1]
            for item in await resolve_targets_fn(user_id)
            if str(item).strip()
        ]
        if target in configured:
            return True, None
        if cur is not None and await _dm_identity_matches(cur, user_id, target):
            return True, None
        return False, "target_removed"

    if source == "notify_test":
        if not user_id or not await resolve_enabled_fn(user_id):
            return False, "toggle_off"
        configured = [
            _split_target_channel(item)[1]
            for item in await resolve_targets_fn(user_id)
            if str(item).strip()
        ]
        if target in configured:
            return True, None
        if target.startswith("feishu:chat:") or target.startswith("discord:channel:"):
            admin_check = is_admin_fn if is_admin_fn is not None else _default_notify_admin
            if await admin_check(user_id):
                return True, None
        return False, "target_removed"

    automation = _automation_view(row, payload)
    gate_ws = str(payload.get("workspace_id") or "").strip()
    auto_id_clean = str(row.get("automation_id") or "").strip()
    if not gate_ws and cur is not None and auto_id_clean:
        try:
            UUID(auto_id_clean)
        except ValueError:
            auto_id_clean = ""
        if auto_id_clean:
            await cur.execute(
                "SELECT workspace_id::text FROM automations WHERE automation_id = %s::uuid",
                (auto_id_clean,),
            )
            a_ws_row = await cur.fetchone()
            if isinstance(a_ws_row, Mapping):
                gate_ws = str(a_ws_row.get("workspace_id") or "").strip()
    if gate_ws and isinstance(automation, dict):
        automation["workspace_id"] = gate_ws
    gate_ok, gate_reason, _, gate_include_chats = await _resolve_unit_managed_notify_gate(
        user_id,
        gate_ws,
        automation,
        stamped_unit_id=str(payload.get("coverage_unit_id") or "").strip(),
        stamped_unit_version=payload.get("coverage_unit_version"),
        target=target,
        conn=getattr(cur, "connection", None) if cur is not None else None,
    )
    if not gate_ok:
        return False, gate_reason
    allowed, reason, _targets = await evaluate_user_notify_eligibility(
        user_id,
        automation,
        target=target,
        include_chat_targets=gate_include_chats,
        resolve_enabled_fn=resolve_enabled_fn,
        resolve_overrides_fn=resolve_overrides_fn,
        resolve_targets_fn=resolve_targets_fn,
        resolve_routes_fn=resolve_routes_fn,
        is_admin_fn=is_admin_fn,
    )
    return allowed, reason


async def decide_automation_notification_plan(
    cur: Any,
    row: Mapping[str, Any] | None,
    *,
    delivery_id: str,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]],
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]],
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]],
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]],
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None,
) -> dict[str, Any]:
    """Build the gateway plan for one already-loaded notify row."""
    if row is None:
        return _deny("failed", "delivery_not_found", "Notify delivery row was not found.")
    payload = _payload_mapping(row.get("payload_json"))
    blocked = _gateway_notification_block(row, payload)
    if blocked is not None:
        return blocked
    target = str(row.get("target") or "").strip()
    status = str(row.get("status") or "").strip().lower()
    if status == "sent":
        return _deny(
            "skipped",
            "already_sent",
            "This notification was already sent.",
            requested_target=target,
        )
    if status != "pending":
        return _deny(
            "failed",
            "not_pending",
            "Only a pending notify row can be sent.",
            requested_target=target,
        )
    card = payload.get("card")
    media_code = _media_block_code(payload, card)
    if media_code:
        return _deny(
            "failed",
            media_code,
            "This notification carries images or extra cards and was not sent.",
            requested_target=target,
        )
    parsed = _parse_target(target)
    if parsed is None:
        return _deny(
            "failed",
            "unsupported_platform",
            "Only Feishu, Discord, and Weixin targets can be sent on this path.",
            requested_target=target,
        )
    if parsed["platform"] == "feishu":
        if not isinstance(card, Mapping) or not card:
            return _deny(
                "failed",
                "missing_card",
                "The saved Feishu card is missing.",
                requested_target=target,
            )
    else:
        text = str(payload.get("text") or "").strip()
        if not text:
            return _deny(
                "failed",
                "missing_text",
                "The saved text is missing.",
                requested_target=target,
            )

    source = str(payload.get("source") or "")
    auto_id = str(row.get("automation_id") or "").strip()
    if auto_id and source not in _NOTICE_SOURCES and await _parent_is_deleted(auto_id):
        return _deny(
            "skipped",
            "task_deleted",
            "The automation was deleted.",
            requested_target=target,
        )

    allowed, skip_reason = await _eligibility(
        row,
        payload,
        target,
        cur=cur,
        resolve_enabled_fn=resolve_enabled_fn,
        resolve_overrides_fn=resolve_overrides_fn,
        resolve_targets_fn=resolve_targets_fn,
        resolve_routes_fn=resolve_routes_fn,
        is_admin_fn=is_admin_fn,
    )
    if not allowed:
        code = skip_reason or "toggle_off"
        return _deny(
            "skipped",
            code,
            "Notification settings no longer allow this target.",
            requested_target=target,
        )

    if parsed["platform"] == "feishu" and parsed["target_kind"] == "group":
        if get_eve_tracking_chat_id() and parsed["recipient_id"] == get_eve_tracking_chat_id():
            logger.info(
                "[DALTON_NOTIFY] Skipping Eve tracking chat delivery_id=%s code=eve_tracking_mirror_pending",
                delivery_id,
            )
            return _deny(
                "skipped",
                "eve_tracking_mirror_pending",
                "Eve tracking mirror is not sent until its contract lives in this repository.",
                requested_target=target,
            )

    delivery_key = automation_notify_delivery_key(delivery_id)
    existing_status = await _existing_channel_status(cur, delivery_key)
    user_id = str(row.get("user_id") or "").strip()
    if parsed["platform"] == "feishu":
        item: dict[str, Any] = {
            "item_index": 0,
            "delivery_key": delivery_key,
            "kind": "feishu_card",
            "card": dict(card) if isinstance(card, Mapping) else {},
            "images": [],
            "existing_status": existing_status,
        }
    else:
        item = {
            "item_index": 0,
            "delivery_key": delivery_key,
            "kind": "text",
            "text": str(payload.get("text") or ""),
            "existing_status": existing_status,
        }
    plan: dict[str, Any] = {
        "authorized": True,
        "status": "authorized",
        "delivery_id": delivery_id,
        "run_id": delivery_id,
        "owner_user_id": user_id,
        "platform": parsed["platform"],
        "target_kind": parsed["target_kind"],
        "requested_target": target,
        "canonical_target_address": parsed["canonical_target_address"],
        "recipient_id": parsed["recipient_id"],
        "items": [item],
    }
    thread_id = (
        str(
            payload.get("thread_id")
            or payload.get("conversation_thread_id")
            or row.get("conversation_thread_id")
            or row.get("thread_id")
            or ""
        ).strip()
        or None
    )
    workspace_id = (
        str(
            payload.get("workspace_id")
            or row.get("workspace_id")
            or ""
        ).strip()
        or None
    )
    plan["thread_id"] = thread_id
    plan["workspace_id"] = workspace_id
    return plan


def _default_resolvers() -> dict[str, Any]:
    from src.dalton.llm.model_prefs import (
        resolve_dalton_notify_enabled,
        resolve_dalton_notify_overrides,
        resolve_dalton_notify_routes,
        resolve_dalton_notify_targets,
    )

    return {
        "resolve_enabled_fn": resolve_dalton_notify_enabled,
        "resolve_overrides_fn": resolve_dalton_notify_overrides,
        "resolve_targets_fn": resolve_dalton_notify_targets,
        "resolve_routes_fn": resolve_dalton_notify_routes,
        "is_admin_fn": None,
    }


async def authorize_automation_notification_plan(
    *,
    delivery_id: str,
    conn: Any = None,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None = None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None = None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None = None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None = None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Authorize one notify row and return the redacted one-item delivery plan."""
    normalized = _normalize_delivery_id(delivery_id)
    if normalized is None:
        return _deny("failed", "invalid_delivery_id", "delivery_id must be a UUID.")
    defaults = _default_resolvers()
    enabled = resolve_enabled_fn or defaults["resolve_enabled_fn"]
    overrides = resolve_overrides_fn or defaults["resolve_overrides_fn"]
    targets = resolve_targets_fn or defaults["resolve_targets_fn"]
    routes = resolve_routes_fn or defaults["resolve_routes_fn"]
    async with get_db_connection(conn) as owned, owned.transaction():
        async with owned.cursor(row_factory=dict_row) as cur:
            row = await _load_delivery_row(cur, normalized)
            return await decide_automation_notification_plan(
                cur,
                row,
                delivery_id=normalized,
                resolve_enabled_fn=enabled,
                resolve_overrides_fn=overrides,
                resolve_targets_fn=targets,
                resolve_routes_fn=routes,
                is_admin_fn=is_admin_fn,
            )


def _claim_denied(status: str, code: str, message: str) -> dict[str, Any]:
    mapped = status if status in ("failed", "unknown") else "failed"
    return {
        "claim_status": "denied",
        "status": mapped,
        "code": code,
        "message": message,
    }


async def authorize_and_claim_automation_notify_delivery(
    *,
    delivery_key: str,
    run_id: str | None = None,
    target: str | None = None,
    has_non_empty_text: bool = True,
    total_chunks: int = 0,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None = None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None = None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None = None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None = None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """I-6 ``kind='automation_notify'``: re-authorize the row and claim ``in_flight``."""
    from src.dalton.capabilities import require_channel_platform_capability
    from src.dalton.database import weixin_multi_account as wx_db

    cleaned_key = str(delivery_key or "").strip()
    prefix = "automation_notify:"
    suffix = ":item:0"
    if not (
        cleaned_key.startswith(prefix)
        and cleaned_key.endswith(suffix)
    ):
        return _claim_denied(
            "failed",
            "not_allowed",
            "delivery_key does not match an automation notification item.",
        )
    delivery_id = _normalize_delivery_id(cleaned_key[len(prefix) : -len(suffix)])
    if delivery_id is None or cleaned_key != automation_notify_delivery_key(delivery_id):
        return _claim_denied(
            "failed",
            "not_allowed",
            "delivery_key does not match an automation notification item.",
        )
    supplied_run = str(run_id or "").strip()
    if supplied_run and _normalize_delivery_id(supplied_run) != delivery_id:
        return _claim_denied(
            "failed",
            "not_allowed",
            "run_id does not match this automation notification.",
        )

    defaults = _default_resolvers()
    enabled = resolve_enabled_fn or defaults["resolve_enabled_fn"]
    overrides = resolve_overrides_fn or defaults["resolve_overrides_fn"]
    targets = resolve_targets_fn or defaults["resolve_targets_fn"]
    routes = resolve_routes_fn or defaults["resolve_routes_fn"]

    async with get_db_connection(conn) as owned:
        async with owned.transaction():
            async with owned.cursor(row_factory=dict_row) as cur:
                if gateway_epoch is not None or require_gateway_epoch:
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        enforce_post_lease_for_non_weixin=False,
                    )
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_delivery:{cleaned_key}",),
                )
                # R11 (Codex #2 / Grok high): Align failed re-claim lock order with
                # unbind_weixin_account (weixin_accounts FOR KEY SHARE -> deliveries FOR UPDATE)
                # to prevent AB-BA deadlocks when re-claiming a failed Weixin delivery.
                await cur.execute(
                    """
                    SELECT user_id, platform,
                           weixin_account_id::text AS weixin_account_id
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    """,
                    (cleaned_key,),
                )
                pre_del = await cur.fetchone()
                existing = None
                if pre_del is not None:
                    if str(pre_del.get("platform") or "").strip().lower() == "weixin":
                        await _lock_weixin_account_for_key_share_in_tx(
                            cur,
                            user_id=pre_del.get("user_id"),
                            account_id=pre_del.get("weixin_account_id"),
                        )
                    await cur.execute(
                        """
                        SELECT * FROM dalton_channel_deliveries
                        WHERE delivery_key = %s
                        FOR UPDATE
                        """,
                        (cleaned_key,),
                    )
                    existing = await cur.fetchone()
                if existing:
                    if str(existing.get("platform") or "").strip().lower() == "weixin" and (
                        gateway_epoch is not None or require_gateway_epoch
                    ):
                        await wx_db.verify_gateway_epoch_in_tx(
                            cur,
                            gateway_epoch,
                            required=require_gateway_epoch,
                            platform="weixin",
                        )
                    ex_status = str(existing["status"])
                    if ex_status in ("sent", "partial"):
                        return {
                            "claim_status": "already_completed",
                            "status": ex_status,
                            "first_response_json": existing.get("first_response_json"),
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": False,
                        }
                    if ex_status in ("in_flight", "unknown"):
                        return {
                            "claim_status": "in_flight_or_unknown",
                            "status": "unknown",
                            "code": "in_flight" if ex_status == "in_flight" else "unknown",
                            "message": "An earlier attempt of this automation notification is in flight or uncertain.",
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": False,
                        }

                row = await _load_delivery_row(cur, delivery_id)
                plan = await decide_automation_notification_plan(
                    cur,
                    row,
                    delivery_id=delivery_id,
                    resolve_enabled_fn=enabled,
                    resolve_overrides_fn=overrides,
                    resolve_targets_fn=targets,
                    resolve_routes_fn=routes,
                    is_admin_fn=is_admin_fn,
                )
                if not plan.get("authorized"):
                    return _claim_denied(
                        str(plan.get("status") or "failed"),
                        str(plan.get("code") or "not_allowed"),
                        str(plan.get("message") or "Automation notification is not authorized."),
                    )
                requested = str(plan.get("requested_target") or "")
                supplied_target = str(target or "").strip()
                if supplied_target and supplied_target != requested:
                    return _claim_denied(
                        "failed",
                        "not_allowed",
                        "target does not match this automation notification.",
                    )
                expected_key = automation_notify_delivery_key(delivery_id)
                if cleaned_key != expected_key:
                    return _claim_denied(
                        "failed",
                        "not_allowed",
                        "delivery_key does not match an authorized item.",
                    )

                owner_user_id = str(plan.get("owner_user_id") or "")
                require_channel_platform_capability(plan["platform"])
                if plan["platform"] == "weixin" and (
                    gateway_epoch is not None or require_gateway_epoch
                ):
                    await wx_db.verify_gateway_epoch_in_tx(
                        cur,
                        gateway_epoch,
                        required=require_gateway_epoch,
                        platform="weixin",
                    )

                wx_account_id: str | None = None
                wx_bot_id: str | None = None
                wx_bot_gen: int | None = None
                wx_recipient_id: str | None = None
                wx_ctx_token: str | None = None
                wx_ctx_version: int | None = None

                if plan["platform"] == "weixin":
                    from src.dalton.access_tiers import (
                        check_team_member_status_live,
                        evaluate_weixin_member_access,
                        get_user_tier,
                    )
                    from src.server.database.encryption import get_encryption_key

                    roster = await check_team_member_status_live(
                        owner_user_id, conn=owned
                    )
                    if roster == "lookup_error":
                        from fastapi import HTTPException

                        raise HTTPException(
                            status_code=503,
                            detail={
                                "code": "roster_lookup_error",
                                "retryable": True,
                                "message": "Unable to verify team membership right now.",
                            },
                        )
                    if roster != "active":
                        return _claim_denied(
                            "failed",
                            "member_not_in_roster",
                            "Owner is no longer in the team roster.",
                        )
                    await cur.execute(
                        "SELECT enabled, allow_external FROM dalton_channel_weixin_config WHERE singleton_id = 1"
                    )
                    cfg_row = await cur.fetchone()
                    user_tier = await get_user_tier(owner_user_id)
                    acc_ok, acc_deny = evaluate_weixin_member_access(
                        user_tier,
                        channel_enabled=bool(cfg_row["enabled"]) if cfg_row else True,
                        db_allow_external=cfg_row["allow_external"] if cfg_row else None,
                    )
                    if not acc_ok:
                        return _claim_denied(
                            "failed",
                            acc_deny or "weixin_channel_disabled",
                            "Weixin channel is disabled for this member.",
                        )
                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, ilink_bot_id,
                               bot_generation, ilink_user_id, status
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s AND status = 'active'
                        FOR KEY SHARE
                        """,
                        (owner_user_id,),
                    )
                    wx_acc = await cur.fetchone()
                    if (
                        not wx_acc
                        or str(wx_acc.get("ilink_user_id") or "").strip()
                        != str(plan["recipient_id"])
                    ):
                        return _claim_denied(
                            "failed",
                            "not_linked",
                            "Owner has no matching active Weixin account.",
                        )
                    wx_account_id = str(wx_acc["account_id"])
                    wx_bot_id = str(wx_acc["ilink_bot_id"])
                    wx_bot_gen = int(wx_acc["bot_generation"])
                    wx_recipient_id = str(plan["recipient_id"])
                    enc_key = get_encryption_key()
                    wx_ctx_token, wx_ctx_version, is_stale = (
                        await wx_db.resolve_weixin_account_context_token_in_tx(
                            cur,
                            enc_key=enc_key,
                            account_id=wx_account_id,
                            bot_generation=wx_bot_gen,
                            platform_user_id=wx_recipient_id,
                        )
                    )
                    if is_stale:
                        return _claim_denied(
                            "failed",
                            "weixin_context_token_stale",
                            "Weixin reply token expired; send any message to your bot first.",
                        )
                    if not wx_ctx_token:
                        return _claim_denied(
                            "failed",
                            "weixin_context_token_missing",
                            "Weixin reply token is not activated yet; send any message to your bot first.",
                        )

                if plan["platform"] == "weixin":
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_deliveries (
                            delivery_key, kind, user_id, thread_id, run_id, execution_id,
                            is_current, has_non_empty_text, platform, target_address,
                            status, total_chunks, sent_chunks, platform_message_ids,
                            files_status,
                            weixin_account_id, weixin_bot_id, weixin_bot_generation, weixin_recipient_id,
                            created_at, updated_at
                        )
                        VALUES (
                            %s, 'automation_notify', %s, NULL, %s, %s::uuid,
                            FALSE, %s, %s, %s,
                            'in_flight', %s, 0, '[]'::jsonb,
                            '[]'::jsonb,
                            %s::uuid, %s, %s, %s,
                            NOW(), NOW()
                        )
                        ON CONFLICT (delivery_key) DO UPDATE SET
                            status = 'in_flight',
                            total_chunks = EXCLUDED.total_chunks,
                            weixin_account_id = EXCLUDED.weixin_account_id,
                            weixin_bot_id = EXCLUDED.weixin_bot_id,
                            weixin_bot_generation = EXCLUDED.weixin_bot_generation,
                            weixin_recipient_id = EXCLUDED.weixin_recipient_id,
                            error_code = NULL,
                            error_message = NULL,
                            updated_at = NOW()
                        """,
                        (
                            cleaned_key,
                            owner_user_id,
                            delivery_id,
                            delivery_id,
                            bool(has_non_empty_text),
                            plan["platform"],
                            plan["canonical_target_address"],
                            int(total_chunks or 0),
                            wx_account_id,
                            wx_bot_id,
                            wx_bot_gen,
                            wx_recipient_id,
                        ),
                    )
                else:
                    await cur.execute(
                        """
                        INSERT INTO dalton_channel_deliveries (
                            delivery_key, kind, user_id, thread_id, run_id, execution_id,
                            is_current, has_non_empty_text, platform, target_address,
                            status, total_chunks, sent_chunks, platform_message_ids,
                            files_status, created_at, updated_at
                        )
                        VALUES (
                            %s, 'automation_notify', %s, NULL, %s, %s::uuid,
                            FALSE, %s, %s, %s,
                            'in_flight', %s, 0, '[]'::jsonb,
                            '[]'::jsonb, NOW(), NOW()
                        )
                        ON CONFLICT (delivery_key) DO UPDATE SET
                            status = 'in_flight',
                            total_chunks = EXCLUDED.total_chunks,
                            error_code = NULL,
                            error_message = NULL,
                            updated_at = NOW()
                        """,
                        (
                            cleaned_key,
                            owner_user_id,
                            delivery_id,
                            delivery_id,
                            bool(has_non_empty_text),
                            plan["platform"],
                            plan["canonical_target_address"],
                            int(total_chunks or 0),
                        ),
                    )
                claim_res: dict[str, Any] = {
                    "claim_status": "claimed",
                    "recipient_id": plan["recipient_id"],
                    "platform": plan["platform"],
                    "target_address": plan["canonical_target_address"],
                    "is_current": False,
                    "home_workspace_id": get_home_workspace_id(owner_user_id or "automation-notify"),
                    "weixin_context_token": wx_ctx_token,
                }
                if plan["platform"] == "weixin":
                    claim_res["weixin_account_id"] = wx_account_id
                    claim_res["weixin_bot_id"] = wx_bot_id
                    claim_res["weixin_bot_generation"] = wx_bot_gen
                    claim_res["weixin_context_token_version"] = wx_ctx_version
                return claim_res


def _outer_attempt_count(row: Mapping[str, Any]) -> int | None:
    try:
        return int(row.get("attempt_count") or 0)
    except (TypeError, ValueError):
        return None


async def _apply_channel_reconcile(
    cur: Any,
    row: Mapping[str, Any],
    *,
    channel_status: str,
) -> str | None:
    """Conditionally project one platform status onto the outer row.

    ``sent`` clears the uncertain error. ``failed`` becomes a retryable
    ``failed`` and leaves ``attempt_count`` unchanged, so the existing retry
    cap still applies. Any other platform status is left untouched.
    """
    if channel_status == "sent":
        new_status, new_code = "sent", None
    elif channel_status == "failed":
        new_status, new_code = "failed", _RETRYABLE_CHANNEL_FAILURE
    else:
        return None
    attempt_count = _outer_attempt_count(row)
    delivery_id = _normalize_delivery_id(str(row.get("delivery_id") or ""))
    expected_error = str(row.get("error_code") or "")
    if attempt_count is None or delivery_id is None or not expected_error:
        return None
    await cur.execute(
        """
        UPDATE dalton_notify_deliveries
        SET status = %s,
            error_code = %s,
            updated_at = NOW()
        WHERE delivery_id = %s::uuid
          AND status = 'pending'
          AND COALESCE(error_code, '') = %s
          AND attempt_count = %s
        """,
        (new_status, new_code, delivery_id, expected_error, attempt_count),
    )
    if not (isinstance(cur.rowcount, int) and cur.rowcount > 0):
        return None
    return new_status


async def reconcile_uncertain_automation_notify_deliveries(
    *,
    limit: int = 5,
    max_duration_s: float = 30.0,
    started_at: float | None = None,
    mark_delivery_fn: Callable[..., Awaitable[Any]] | None = None,
    conn: Any = None,
    get_connection: Callable[..., Any] | None = None,
) -> int:
    """Read platform rows for uncertain gateway completions and test pushes.

    Looks up ``automation_notify:{delivery_id}:item:0`` and does not send.
    ``in_flight``, ``unknown``, and a missing platform row stay pending.
    Returns how many outer rows were marked ``sent``. The retry scan passes
    its own connection factory so this read uses that scan's database.
    """
    eff_limit = max(1, int(limit or 5))
    t0 = started_at if started_at is not None else time.monotonic()
    uncertain_codes = sorted(UNCERTAIN_NOTIFY_ERROR_CODES)
    sent_count = 0
    # Resolve at call time so a direct unit test can patch this module's pool.
    connection_factory = get_db_connection if get_connection is None else get_connection
    try:
        async with connection_factory(conn) as owned_conn:
            async with owned_conn.cursor(row_factory=dict_row) as cur:
                # The EXISTS filter keeps only rows the platform already settled.
                # Applied before the row limit so still-unknown old rows cannot
                # fill every scan and starve later rows the platform confirmed.
                # Alias ``n``: data-task retry fakes treat
                # ``FROM dalton_notify_deliveries d`` plus ORDER BY as their own scan.
                await cur.execute(
                    """
                    SELECT n.delivery_id, n.execution_id, n.user_id,
                           n.channel, n.target, n.status, n.attempt_count,
                           n.error_code, n.payload_json
                    FROM dalton_notify_deliveries n
                    WHERE n.status = 'pending'
                      AND COALESCE(n.error_code, '') = ANY(%s)
                      AND COALESCE(n.payload_json ->> 'transport', '') = 'gateway'
                      AND COALESCE(n.payload_json ->> 'source', '') IN (
                          'automation_completion',
                          'notify_test',
                          'report_delivery_missing_profile_notice'
                      )
                      AND COALESCE(n.automation_id, '') NOT LIKE 'data:%%'
                      AND COALESCE(n.payload_json ->> 'route_key', '') NOT LIKE 'data:%%'
                      AND COALESCE(n.payload_json ->> 'content_kind', '') = ''
                      AND EXISTS (
                          SELECT 1
                          FROM dalton_channel_deliveries c
                          WHERE c.delivery_key = 'automation_notify:'
                                || n.delivery_id::text || ':item:0'
                            AND c.status IN ('sent', 'failed')
                      )
                    ORDER BY n.updated_at ASC, n.created_at ASC
                    LIMIT %s
                    """,
                    (uncertain_codes, eff_limit),
                )
                candidates = await cur.fetchall()
        for row in candidates or []:
            if time.monotonic() - t0 >= max_duration_s:
                break
            if not isinstance(row, Mapping):
                continue
            payload = _payload_mapping(row.get("payload_json"))
            if _gateway_notification_block(row, payload) is not None:
                continue
            delivery_id = _normalize_delivery_id(str(row.get("delivery_id") or ""))
            if delivery_id is None:
                continue
            try:
                async with connection_factory(conn) as row_conn:
                    async with row_conn.transaction():
                        async with row_conn.cursor(row_factory=dict_row) as tx_cur:
                            channel_status = await _existing_channel_status(
                                tx_cur,
                                automation_notify_delivery_key(delivery_id),
                            )
                            if channel_status not in ("sent", "failed"):
                                continue
                            applied = await _apply_channel_reconcile(
                                tx_cur,
                                row,
                                channel_status=channel_status,
                            )
                if applied is None:
                    continue
                if mark_delivery_fn is not None:
                    await mark_delivery_fn(
                        str(row.get("execution_id") or "").strip(),
                        str(row.get("channel") or "").strip().lower(),
                        str(row.get("target") or "").strip(),
                        status=applied,
                        error_code=None if applied == "sent" else _RETRYABLE_CHANNEL_FAILURE,
                        expected_status="pending",
                        expected_attempt_count=_outer_attempt_count(row),
                    )
                if applied == "sent":
                    sent_count += 1
            except Exception:
                logger.warning(
                    "[DALTON_NOTIFY] Automation notify reconcile skipped delivery_id=%s",
                    delivery_id,
                    exc_info=True,
                )
    except Exception:
        logger.warning(
            "[DALTON_NOTIFY] Automation notify reconcile scan failed",
            exc_info=True,
        )
    return sent_count
