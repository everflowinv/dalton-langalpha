"""Gateway plan & I-6 claim authorization for ``dalton_failure_notice_targets``.

Enforces the ``(claim_token, claim_expires_at)`` lease fence under PostgreSQL
``FOR UPDATE`` lock on ``dalton_failure_notice_targets`` so late HTTP requests
arriving after lease expiration or reconciliation are deterministically denied
with ``claim_expired_or_superseded``.

Covers all four unattended subject kinds:
- ``personal_automation``
- ``company_automation``
- ``data_task``
- ``wakeup``
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from src.dalton.capabilities import require_channel_platform_capability
from src.dalton.notify.failure_receipts import (
    FAILURE_NOTICE_SUBJECT_KINDS,
    failure_notice_delivery_key,
)
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_FEISHU_USER_RE = re.compile(r"^feishu:user:(ou_[A-Za-z0-9_-]+)$")
_DISCORD_USER_RE = re.compile(r"^discord:user:([0-9]+)$")
_WEIXIN_USER_RE = re.compile(r"^weixin:user:([^\s:]{1,255})$")

CLAIM_EXPIRED_OR_SUPERSEDED = "claim_expired_or_superseded"


def _normalize_uuid(raw: Any) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except ValueError:
        return None


def _parse_dm_target(target: str | None) -> dict[str, str] | None:
    cleaned = str(target or "").strip()
    m_fs = _FEISHU_USER_RE.match(cleaned)
    if m_fs:
        return {
            "platform": "feishu",
            "target_kind": "dm",
            "canonical_target_address": cleaned,
            "recipient_id": m_fs.group(1),
        }
    m_dc = _DISCORD_USER_RE.match(cleaned)
    if m_dc:
        return {
            "platform": "discord",
            "target_kind": "dm",
            "canonical_target_address": "discord:@me",
            "recipient_id": m_dc.group(1),
        }
    m_wx = _WEIXIN_USER_RE.match(cleaned)
    if m_wx:
        return {
            "platform": "weixin",
            "target_kind": "dm",
            "canonical_target_address": "weixin:@me",
            "recipient_id": m_wx.group(1),
        }
    return None


def _payload_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str) and raw.strip():
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _deny_plan(status: str, code: str, message: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "authorized": False,
        "status": status,
        "code": code,
        "message": message,
    }
    out.update(extra)
    return out


def _deny_claim(status: str, code: str, message: str) -> dict[str, Any]:
    return {
        "claim_status": "denied",
        "status": status,
        "code": code,
        "message": message,
    }


def _extract_target_receipt_id(
    target_receipt_id: str | None,
    delivery_key: str | None,
) -> str | None:
    direct = _normalize_uuid(target_receipt_id)
    if direct is not None:
        return direct
    cleaned_key = str(delivery_key or "").strip()
    prefix = "failure_notice:"
    suffix = ":item:0"
    if cleaned_key.startswith(prefix) and cleaned_key.endswith(suffix):
        inner = cleaned_key[len(prefix) : -len(suffix)]
        parsed = _normalize_uuid(inner)
        if parsed is not None and cleaned_key == failure_notice_delivery_key(parsed):
            return parsed
    return None


def _is_lease_valid(
    row: Mapping[str, Any],
    *,
    claim_token: str,
    now: datetime | None = None,
) -> bool:
    if str(row.get("status") or "").strip().lower() != "sending":
        return False
    stored_token = _normalize_uuid(row.get("claim_token"))
    if stored_token is None or stored_token != claim_token:
        return False
    if "lease_valid" in row and row["lease_valid"] is not None:
        return bool(row["lease_valid"])
    exp = row.get("claim_expires_at")
    if not isinstance(exp, datetime):
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    return exp > ref_now


async def _dm_identity_matches(
    cur: Any,
    *,
    user_id: str,
    platform: str,
    recipient_id: str,
) -> bool:
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
            (user_id, recipient_id),
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
        (user_id, platform, recipient_id),
    )
    return (await cur.fetchone()) is not None


async def authorize_failure_notice_plan(
    *,
    target_receipt_id: str,
    claim_token: str,
    conn: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authorize ``(target_receipt_id, claim_token)`` and return the gateway send plan."""
    tr_id = _normalize_uuid(target_receipt_id)
    tok = _normalize_uuid(claim_token)
    if tr_id is None or tok is None:
        return _deny_plan(
            "failed",
            CLAIM_EXPIRED_OR_SUPERSEDED,
            "Invalid failure notice receipt id or claim token.",
        )

    async with get_db_connection(conn) as owned:
        async with owned.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    t.target_receipt_id::text AS target_receipt_id,
                    t.notice_id::text AS notice_id,
                    t.channel,
                    t.target,
                    t.status,
                    t.claim_token::text AS claim_token,
                    t.claim_expires_at,
                    (t.claim_expires_at IS NOT NULL AND t.claim_expires_at > NOW()) AS lease_valid,
                    t.payload_json,
                    n.subject_kind,
                    n.subject_id,
                    n.source_key,
                    n.recipient_user_id,
                    n.failure_reason
                FROM dalton_failure_notice_targets t
                JOIN dalton_failure_notices n ON n.notice_id = t.notice_id
                WHERE t.target_receipt_id = %s::uuid
                """,
                (tr_id,),
            )
            row = await cur.fetchone()
            if not isinstance(row, Mapping):
                return _deny_plan(
                    "failed",
                    "not_allowed",
                    "Failure notice target was not found.",
                )

            target = str(row.get("target") or "").strip()
            if not _is_lease_valid(row, claim_token=tok, now=now):
                return _deny_plan(
                    "failed",
                    CLAIM_EXPIRED_OR_SUPERSEDED,
                    "Failure notice claim lease expired or was superseded.",
                    requested_target=target,
                )

            subject_kind = str(row.get("subject_kind") or "").strip()
            if subject_kind not in FAILURE_NOTICE_SUBJECT_KINDS:
                return _deny_plan(
                    "failed",
                    "not_allowed",
                    "Unsupported failure notice subject_kind.",
                    requested_target=target,
                )

            parsed = _parse_dm_target(target)
            if parsed is None:
                return _deny_plan(
                    "failed",
                    "invalid_target",
                    "Failure notice target must be a Feishu, Discord, or Weixin DM.",
                    requested_target=target,
                )

            recipient_user_id = str(row.get("recipient_user_id") or "").strip()
            if not recipient_user_id:
                return _deny_plan(
                    "failed",
                    "dm_binding_missing",
                    "Failure notice recipient_user_id is missing.",
                    requested_target=target,
                )

            if not await _dm_identity_matches(
                cur,
                user_id=recipient_user_id,
                platform=parsed["platform"],
                recipient_id=parsed["recipient_id"],
            ):
                return _deny_plan(
                    "failed",
                    "dm_binding_missing",
                    "Recipient does not hold a matching active DM binding for this target.",
                    requested_target=target,
                )

            payload = _payload_dict(row.get("payload_json"))
            d_key = failure_notice_delivery_key(tr_id)
            await cur.execute(
                """
                SELECT status
                FROM dalton_channel_deliveries
                WHERE delivery_key = %s
                """,
                (d_key,),
            )
            ch_row = await cur.fetchone()
            existing_status = (
                str(ch_row.get("status") or "").strip() or None
                if isinstance(ch_row, Mapping)
                else None
            )

            if parsed["platform"] == "feishu" and isinstance(payload.get("card"), Mapping):
                item: dict[str, Any] = {
                    "item_index": 0,
                    "delivery_key": d_key,
                    "kind": "feishu_card",
                    "card": dict(payload["card"]),
                    "images": [],
                    "existing_status": existing_status,
                }
            else:
                text = str(payload.get("text") or "").strip()
                if not text:
                    return _deny_plan(
                        "failed",
                        "invalid_target",
                        "Failure notice payload text is missing.",
                        requested_target=target,
                    )
                item = {
                    "item_index": 0,
                    "delivery_key": d_key,
                    "kind": "text",
                    "text": text,
                    "existing_status": existing_status,
                }

            return {
                "authorized": True,
                "status": "authorized",
                "target_receipt_id": tr_id,
                "run_id": tr_id,
                "subject_kind": subject_kind,
                "owner_user_id": recipient_user_id,
                "platform": parsed["platform"],
                "target_kind": "dm",
                "requested_target": target,
                "canonical_target_address": parsed["canonical_target_address"],
                "recipient_id": parsed["recipient_id"],
                "items": [item],
            }


async def authorize_and_claim_failure_notice(
    *,
    target_receipt_id: str | None = None,
    delivery_key: str | None = None,
    claim_token: str | None = None,
    run_id: str | None = None,
    target: str | None = None,
    has_non_empty_text: bool = True,
    total_chunks: int = 0,
    gateway_epoch: int | None = None,
    require_gateway_epoch: bool = False,
    conn: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """I-6 ``kind='failure_notice'``: lock target receipt, verify lease, and claim ``in_flight``."""
    from src.dalton.database import weixin_multi_account as wx_db

    tr_id = _extract_target_receipt_id(target_receipt_id, delivery_key)
    if tr_id is None:
        return _deny_claim(
            "failed",
            "not_allowed",
            "target_receipt_id / delivery_key does not match a failure notice item.",
        )

    expected_key = failure_notice_delivery_key(tr_id)
    cleaned_key = str(delivery_key or expected_key).strip()
    if cleaned_key != expected_key:
        return _deny_claim(
            "failed",
            "not_allowed",
            "delivery_key does not match failure_notice:{target_receipt_id}:item:0.",
        )

    supplied_run = str(run_id or "").strip()
    if supplied_run and _normalize_uuid(supplied_run) != tr_id:
        return _deny_claim(
            "failed",
            "not_allowed",
            "run_id does not match target_receipt_id.",
        )

    tok = _normalize_uuid(claim_token)
    if tok is None:
        return _deny_claim(
            "failed",
            CLAIM_EXPIRED_OR_SUPERSEDED,
            "Missing or invalid claim_token for failure notice claim.",
        )

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

                # Lock order: dalton_failure_notice_targets FOR UPDATE first (matches reconciler!)
                await cur.execute(
                    """
                    SELECT
                        t.target_receipt_id::text AS target_receipt_id,
                        t.notice_id::text AS notice_id,
                        t.channel,
                        t.target,
                        t.status,
                        t.claim_token::text AS claim_token,
                        t.claim_expires_at,
                        (t.claim_expires_at IS NOT NULL AND t.claim_expires_at > NOW()) AS lease_valid,
                        t.payload_json,
                        n.subject_kind,
                        n.subject_id,
                        n.source_key,
                        n.recipient_user_id,
                        n.failure_reason
                    FROM dalton_failure_notice_targets t
                    JOIN dalton_failure_notices n ON n.notice_id = t.notice_id
                    WHERE t.target_receipt_id = %s::uuid
                    FOR UPDATE OF t
                    """,
                    (tr_id,),
                )
                row = await cur.fetchone()
                if not isinstance(row, Mapping):
                    return _deny_claim(
                        "failed",
                        "not_allowed",
                        "Failure notice target was not found.",
                    )

                # Check idempotent replay in dalton_channel_deliveries first
                await cur.execute(
                    """
                    SELECT *
                    FROM dalton_channel_deliveries
                    WHERE delivery_key = %s
                    FOR UPDATE
                    """,
                    (cleaned_key,),
                )
                existing = await cur.fetchone()
                if isinstance(existing, Mapping):
                    ex_status = str(existing.get("status") or "").strip().lower()
                    if ex_status in ("sent", "partial"):
                        return {
                            "claim_status": "already_completed",
                            "status": ex_status,
                            "first_response_json": existing.get("first_response_json"),
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": False,
                        }

                # Enforce claim_token + claim_expires_at lease fence
                if not _is_lease_valid(row, claim_token=tok, now=now):
                    return _deny_claim(
                        "failed",
                        CLAIM_EXPIRED_OR_SUPERSEDED,
                        "Failure notice claim lease expired or was superseded.",
                    )

                if isinstance(existing, Mapping):
                    ex_status = str(existing.get("status") or "").strip().lower()
                    if ex_status in ("in_flight", "unknown"):
                        return {
                            "claim_status": "in_flight_or_unknown",
                            "status": "unknown",
                            "code": "in_flight" if ex_status == "in_flight" else "unknown",
                            "message": "An earlier failure notice claim is still in flight or unknown.",
                            "platform": existing["platform"],
                            "target_address": existing["target_address"],
                            "is_current": False,
                        }

                subject_kind = str(row.get("subject_kind") or "").strip()
                if subject_kind not in FAILURE_NOTICE_SUBJECT_KINDS:
                    return _deny_claim(
                        "failed",
                        "not_allowed",
                        "Unsupported failure notice subject_kind.",
                    )

                stored_target = str(row.get("target") or "").strip()
                supplied_target = str(target or "").strip()
                if supplied_target and supplied_target != stored_target:
                    return _deny_claim(
                        "failed",
                        "not_allowed",
                        "Supplied target does not match failure notice target.",
                    )

                parsed = _parse_dm_target(stored_target)
                if parsed is None:
                    return _deny_claim(
                        "failed",
                        "invalid_target",
                        "Failure notice target must be a Feishu, Discord, or Weixin DM.",
                    )

                recipient_user_id = str(row.get("recipient_user_id") or "").strip()
                if not recipient_user_id:
                    return _deny_claim(
                        "failed",
                        "dm_binding_missing",
                        "Failure notice recipient_user_id is missing.",
                    )

                require_channel_platform_capability(parsed["platform"])

                if not await _dm_identity_matches(
                    cur,
                    user_id=recipient_user_id,
                    platform=parsed["platform"],
                    recipient_id=parsed["recipient_id"],
                ):
                    return _deny_claim(
                        "failed",
                        "dm_binding_missing",
                        "Recipient does not hold a matching active DM binding for this target.",
                    )

                wx_account_id: str | None = None
                wx_bot_id: str | None = None
                wx_bot_gen: int | None = None
                wx_recipient_id: str | None = None
                wx_ctx_token: str | None = None
                wx_ctx_version: int | None = None

                if parsed["platform"] == "weixin":
                    if gateway_epoch is not None or require_gateway_epoch:
                        await wx_db.verify_gateway_epoch_in_tx(
                            cur,
                            gateway_epoch,
                            required=require_gateway_epoch,
                            platform="weixin",
                        )
                    from src.server.database.encryption import get_encryption_key

                    await cur.execute(
                        """
                        SELECT account_id::text AS account_id, ilink_bot_id,
                               bot_generation, ilink_user_id, status
                        FROM dalton_channel_weixin_accounts
                        WHERE user_id = %s AND status = 'active'
                        FOR KEY SHARE
                        """,
                        (recipient_user_id,),
                    )
                    wx_acc = await cur.fetchone()
                    if (
                        not isinstance(wx_acc, Mapping)
                        or str(wx_acc.get("ilink_user_id") or "").strip() != parsed["recipient_id"]
                    ):
                        return _deny_claim(
                            "failed",
                            "weixin_account_inactive",
                            "Recipient has no matching active Weixin account.",
                        )
                    wx_account_id = str(wx_acc["account_id"])
                    wx_bot_id = str(wx_acc["ilink_bot_id"])
                    wx_bot_gen = int(wx_acc["bot_generation"])
                    wx_recipient_id = parsed["recipient_id"]
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
                        return _deny_claim(
                            "failed",
                            "weixin_context_token_stale",
                            "Weixin reply token expired.",
                        )
                    if not wx_ctx_token:
                        return _deny_claim(
                            "failed",
                            "weixin_session_expired",
                            "Weixin reply token is missing.",
                        )

                exec_uuid = _normalize_uuid(row.get("subject_id"))
                if parsed["platform"] == "weixin":
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
                            %s, 'failure_notice', %s, NULL, %s, %s::uuid,
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
                            recipient_user_id,
                            tr_id,
                            exec_uuid,
                            bool(has_non_empty_text),
                            parsed["platform"],
                            parsed["canonical_target_address"],
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
                            %s, 'failure_notice', %s, NULL, %s, %s::uuid,
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
                            recipient_user_id,
                            tr_id,
                            exec_uuid,
                            bool(has_non_empty_text),
                            parsed["platform"],
                            parsed["canonical_target_address"],
                            int(total_chunks or 0),
                        ),
                    )

                claim_res: dict[str, Any] = {
                    "claim_status": "claimed",
                    "recipient_id": parsed["recipient_id"],
                    "platform": parsed["platform"],
                    "target_address": parsed["canonical_target_address"],
                    "is_current": False,
                    "subject_kind": subject_kind,
                    "home_workspace_id": get_home_workspace_id(
                        recipient_user_id or "failure-notice"
                    ),
                    "weixin_context_token": wx_ctx_token,
                }
                if parsed["platform"] == "weixin":
                    claim_res["weixin_account_id"] = wx_account_id
                    claim_res["weixin_bot_id"] = wx_bot_id
                    claim_res["weixin_bot_generation"] = wx_bot_gen
                    claim_res["weixin_context_token_version"] = wx_ctx_version
                return claim_res
