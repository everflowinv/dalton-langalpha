"""Personal integration HITL confirmation & Google MCP elicitation verification (§5.2).

Validates web-session resume decisions for ``DALTON_INTEGRATION_APPROVAL_TYPES``
in ``checkpoint_handler.settle_resume_request`` and provides canonical confirm-token
hashing, L2 second-confirmation checks, Google MCP ``<request_key>`` schema
validation, and 1:1 DM deep-link notification helpers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import hmac
import json
import logging
from typing import Any

from fastapi import HTTPException

from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import IntegrationStatus
from src.dalton.services.personal_integration_guard import (
    check_thread_surface_still_private,
)
from src.dalton.settings import get_dalton_public_app_url

logger = logging.getLogger(__name__)

DALTON_INTEGRATION_APPROVAL_TYPES: frozenset[str] = frozenset(
    {
        "dalton_google_mcp_elicitation",
        "dalton_calendar_create_event",
        "dalton_calendar_update_event",
        "dalton_calendar_delete_event",
        "dalton_mail_create_draft",
        "dalton_mail_send",
    }
)

L1_APPROVAL_TTL_SECONDS = 30 * 60  # 30 minutes (§5.2)
L2_APPROVAL_TTL_SECONDS = 10 * 60  # 10 minutes (§5.2)
ELICITATION_APPROVAL_TTL_SECONDS = 10 * 60  # 10 minutes (§5.2)


def canonical_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Compute a deterministic SHA-256 hex digest of a normalized action payload."""
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compute_calendar_confirm_token(
    *,
    approval_id: str,
    caller_user_id: str,
    provider: str,
    provider_account_id: str,
    auth_epoch: int,
    calendar_id: str,
    action: str,
    event_id: str | None,
    pre_read_updated_marker: str | None,
    normalized_payload: Mapping[str, Any],
) -> str:
    """Bind confirm_token to the full action identity tuple (§5.2)."""
    material = {
        "approval_id": str(approval_id),
        "caller_user_id": str(caller_user_id),
        "provider": str(provider),
        "provider_account_id": str(provider_account_id),
        "auth_epoch": int(auth_epoch),
        "calendar_id": str(calendar_id),
        "action": str(action),
        "event_id": str(event_id or ""),
        "pre_read_updated_marker": str(pre_read_updated_marker or ""),
        "normalized_payload": dict(normalized_payload),
    }
    return canonical_payload_sha256(material)


def compute_elicitation_confirm_token(
    *,
    approval_id: str,
    caller_user_id: str,
    provider: str,
    service: str,
    provider_account_id: str,
    auth_epoch: int,
    payload_sha256: str,
) -> str:
    """Compute a confirm token bound to one Google MCP elicitation approval row."""
    return canonical_payload_sha256(
        {
            "approval_id": str(approval_id),
            "caller_user_id": str(caller_user_id),
            "provider": str(provider),
            "service": str(service),
            "provider_account_id": str(provider_account_id),
            "auth_epoch": int(auth_epoch),
            "payload_sha256": str(payload_sha256),
        }
    )


def _extract_first_decision(answer: Any) -> dict[str, Any]:
    decisions = (
        answer.get("decisions")
        if isinstance(answer, Mapping)
        else getattr(answer, "decisions", None)
    )
    if (
        not isinstance(decisions, Sequence)
        or isinstance(decisions, (str, bytes))
        or not decisions
    ):
        return {}
    first = decisions[0]
    if isinstance(first, Mapping):
        return dict(first)
    if hasattr(first, "model_dump"):
        return dict(first.model_dump())
    return {
        "type": getattr(first, "type", None),
        "message": getattr(first, "message", None),
        "confirm_token": getattr(first, "confirm_token", None),
        "second_confirm_acknowledged": getattr(first, "second_confirm_acknowledged", None),
        "confirmed_payload_sha256": getattr(first, "confirmed_payload_sha256", None),
        "elicitation_responses": getattr(first, "elicitation_responses", None),
    }


def validate_elicitation_responses(
    input_requests_map: Mapping[str, Any],
    elicitation_responses: Mapping[str, Any],
) -> tuple[bool, str | None, bool]:
    """Validate ``elicitation_responses`` against Google MCP ``inputRequests`` schema (§3.1, §5.2).

    Returns ``(ok, error_message, is_decline_or_cancel)``.
    """
    if not isinstance(input_requests_map, Mapping) or not input_requests_map:
        return False, "Missing inputRequests definition for elicitation.", False
    if not isinstance(elicitation_responses, Mapping) or not elicitation_responses:
        return False, "Missing elicitation_responses for Google form confirmation.", False

    expected_keys = set(input_requests_map.keys())
    provided_keys = set(elicitation_responses.keys())
    if expected_keys != provided_keys:
        return (
            False,
            f"elicitation_responses keys {sorted(provided_keys)} must match {sorted(expected_keys)}.",
            False,
        )

    any_decline_or_cancel = False
    for req_key, req_def in input_requests_map.items():
        resp = elicitation_responses.get(req_key)
        if not isinstance(resp, Mapping):
            return False, f"Response for '{req_key}' must be an object.", False
        action = str(resp.get("action") or "").strip().lower()
        if action in ("decline", "cancel"):
            any_decline_or_cancel = True
            if "content" in resp and resp["content"] is not None:
                return (
                    False,
                    f"Decline/cancel response for '{req_key}' must omit 'content'.",
                    True,
                )
            continue
        if action != "accept":
            return (
                False,
                f"Invalid elicitation action '{action}' for '{req_key}'; expected accept, decline, or cancel.",
                False,
            )

        content = resp.get("content")
        if not isinstance(content, Mapping):
            return False, f"Accept response for '{req_key}' requires an object 'content'.", False

        params = req_def.get("params") if isinstance(req_def, Mapping) else None
        schema = params.get("requestedSchema") if isinstance(params, Mapping) else None
        if isinstance(schema, Mapping):
            err = _validate_against_requested_schema(req_key, schema, content)
            if err:
                return False, err, False

    return True, None, any_decline_or_cancel


def _validate_against_requested_schema(
    req_key: str,
    schema: Mapping[str, Any],
    content: Mapping[str, Any],
) -> str | None:
    properties = schema.get("properties")
    required = schema.get("required")
    if isinstance(required, list):
        for field_name in required:
            if field_name not in content or content[field_name] is None:
                return f"Missing required field '{field_name}' in '{req_key}' response."

    if isinstance(properties, Mapping):
        for field_name, value in content.items():
            prop = properties.get(field_name)
            if not isinstance(prop, Mapping):
                continue
            enum_values = prop.get("enum")
            if isinstance(enum_values, list) and value not in enum_values:
                return (
                    f"Field '{field_name}' in '{req_key}' must be one of {enum_values}."
                )
            expected_type = prop.get("type")
            if expected_type == "string" and not isinstance(value, str):
                return f"Field '{field_name}' in '{req_key}' must be a string."
            if expected_type == "boolean" and not isinstance(value, bool):
                return f"Field '{field_name}' in '{req_key}' must be a boolean."
            if expected_type == "integer" and (
                not isinstance(value, int) or isinstance(value, bool)
            ):
                return f"Field '{field_name}' in '{req_key}' must be an integer."
            if expected_type == "number" and (
                not isinstance(value, (int, float)) or isinstance(value, bool)
            ):
                return f"Field '{field_name}' in '{req_key}' must be a number."
    return None


async def verify_personal_integration_interrupt_resume(
    *,
    thread_id: str,
    action_req: Mapping[str, Any],
    answer: Any,
    principal: Any,
    raw_request: Any = None,
) -> bool:
    """Verify resume decision for a personal integration approval interrupt (§5.2).

    Returns ``True`` when the resume should proceed as-is, or ``False`` when
    the confirmation snapshot/privacy surface drifted and the caller should stamp
    ``scope_changed=True`` so the tool cleanly aborts.

    Raises ``HTTPException`` (400/403) for malformed or unauthorized resume payloads.
    """
    _ = raw_request
    caller_user_id = str(getattr(principal, "user_id", "") or "").strip()
    expected_caller = str(action_req.get("caller_user_id") or "").strip()
    if expected_caller and caller_user_id != expected_caller:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "confirm_initiator_mismatch",
                "message": "Only the token owner can confirm this personal integration action.",
            },
        )

    decision = _extract_first_decision(answer)
    decision_type = str(decision.get("type") or "").strip().lower()
    action_type = str(action_req.get("type") or "").strip()
    approval_id = str(action_req.get("approval_id") or "").strip()

    # Reject always passes through (for elicitation, marks local approval rejected)
    if decision_type == "reject":
        if approval_id:
            row = await db.get_action_approval(approval_id)
            if row is not None and row.caller_user_id == caller_user_id and row.status == "pending":
                if action_type == "dalton_google_mcp_elicitation":
                    elic = decision.get("elicitation_responses")
                    if elic is not None and isinstance(row.input_requests_map, Mapping):
                        ok, err, _ = validate_elicitation_responses(
                            row.input_requests_map, elic
                        )
                        if not ok:
                            raise HTTPException(
                                status_code=400,
                                detail={
                                    "code": "invalid_elicitation_response",
                                    "message": err or "Invalid elicitation response.",
                                },
                            )
        return True

    if not approval_id:
        return False

    row = await db.get_action_approval(approval_id)
    if row is None or row.caller_user_id != caller_user_id:
        return False
    if str(row.thread_id) != str(thread_id):
        return False
    if row.status != "pending":
        return False
    if row.expires_at is not None:
        exp = row.expires_at if row.expires_at.tzinfo is not None else row.expires_at.replace(tzinfo=UTC)
        if exp <= datetime.now(UTC):
            return False

    # Verify confirm_token matches
    expected_token = str(action_req.get("confirm_token") or "").strip()
    provided_token = str(decision.get("confirm_token") or "").strip()
    if expected_token and (
        not provided_token or not hmac.compare_digest(provided_token, expected_token)
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "confirm_token_mismatch",
                "message": "确认凭据无效或已过期，请重新发起操作。",
            },
        )

    # Check if thread privacy surface drifted since interrupt was raised (§4.4 item 5)
    still_private = await check_thread_surface_still_private(thread_id, caller_user_id)
    if not still_private:
        return False

    # Check connection still connected at the same auth_epoch
    conn_row = await db.get_connection(caller_user_id, row.provider, row.service)
    if (
        conn_row is None
        or conn_row.status != IntegrationStatus.CONNECTED
        or conn_row.auth_epoch != row.auth_epoch
        or conn_row.provider_account_id != row.provider_account_id
    ):
        return False

    # Google MCP elicitation schema check (§5.2)
    if action_type == "dalton_google_mcp_elicitation":
        elic = decision.get("elicitation_responses")
        if not isinstance(elic, Mapping):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_elicitation_response",
                    "message": "请完成 Google 表单确认选项后再提交。",
                },
            )
        ok, err, is_decline = validate_elicitation_responses(
            row.input_requests_map or {}, elic
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "invalid_elicitation_response",
                    "message": err or "Google 表单响应格式无效。",
                },
            )
        if is_decline:
            await db.mark_action_approval_status(
                approval_id, "rejected", caller_user_id=caller_user_id
            )
        return True

    # L2 second confirmation check (§5.2)
    requires_second = bool(action_req.get("requires_second_confirm")) or (
        action_type == "dalton_mail_send"
    )
    if requires_second:
        ack = decision.get("second_confirm_acknowledged")
        confirmed_sha = str(decision.get("confirmed_payload_sha256") or "").strip()
        if ack is not True or not confirmed_sha or not hmac.compare_digest(
            confirmed_sha, row.payload_sha256
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "second_confirmation_required",
                    "message": "该操作需要勾选二次确认并核对操作摘要后再提交。",
                },
            )

    return True


def build_private_thread_web_deep_link(thread_id: str) -> str:
    """Build the web login deep link for approving a personal integration action from 1:1 DM (§4.4 item 6)."""
    origin = get_dalton_public_app_url().strip().rstrip("/")
    path = f"/chat/t/{thread_id}"
    return f"{origin}{path}" if origin else path


__all__ = [
    "DALTON_INTEGRATION_APPROVAL_TYPES",
    "ELICITATION_APPROVAL_TTL_SECONDS",
    "L1_APPROVAL_TTL_SECONDS",
    "L2_APPROVAL_TTL_SECONDS",
    "build_private_thread_web_deep_link",
    "canonical_payload_sha256",
    "compute_calendar_confirm_token",
    "compute_elicitation_confirm_token",
    "validate_elicitation_responses",
    "verify_personal_integration_interrupt_resume",
]
