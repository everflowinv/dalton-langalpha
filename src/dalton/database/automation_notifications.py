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

from src.dalton.notify.channel_transport import UNCERTAIN_NOTIFY_ERROR_CODES
from src.dalton.notify.dispatch import (
    _default_notify_admin,
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
_GATEWAY_NOTIFY_SOURCES = frozenset({"automation_completion", "notify_test"})
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
        SELECT delivery_id, execution_id, automation_id, user_id, channel, target,
               status, attempt_count, error_code, payload_json
        FROM dalton_notify_deliveries
        WHERE delivery_id = %s::uuid
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


async def _eligibility(
    row: Mapping[str, Any],
    payload: Mapping[str, Any],
    target: str,
    *,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]],
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]],
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]],
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]],
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None,
) -> tuple[bool, str | None]:
    user_id = str(row.get("user_id") or "").strip()
    source = str(payload.get("source") or "")
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
    allowed, reason, _targets = await evaluate_user_notify_eligibility(
        user_id,
        automation,
        target=target,
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
            "Only Feishu and Discord targets can be sent on this path.",
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
                "The saved Discord text is missing.",
                requested_target=target,
            )

    auto_id = str(row.get("automation_id") or "").strip()
    if auto_id and await _parent_is_deleted(auto_id):
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
    conn: Any = None,
    resolve_enabled_fn: Callable[[str | None], Awaitable[bool]] | None = None,
    resolve_overrides_fn: Callable[[str | None], Awaitable[dict[str, bool]]] | None = None,
    resolve_targets_fn: Callable[[str | None], Awaitable[list[str]]] | None = None,
    resolve_routes_fn: Callable[[str | None], Awaitable[dict[str, list[str]]]] | None = None,
    is_admin_fn: Callable[[str | None], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """I-6 ``kind='automation_notify'``: re-authorize the row and claim ``in_flight``."""
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
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"dalton_delivery:{cleaned_key}",),
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
                return {
                    "claim_status": "claimed",
                    "recipient_id": plan["recipient_id"],
                    "platform": plan["platform"],
                    "target_address": plan["canonical_target_address"],
                    "is_current": False,
                    "home_workspace_id": get_home_workspace_id(owner_user_id or "automation-notify"),
                    "weixin_context_token": None,
                }


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
                          'notify_test'
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
