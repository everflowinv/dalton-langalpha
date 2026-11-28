"""FastAPI router for Dalton Channel Gateway internal persistence (§3.1) and Web management.

Mounted under `/api/v1/dalton` via `src/dalton/app/router.py`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Literal
from urllib.parse import parse_qsl, urlparse

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.config import env
from src.dalton.access_tiers import get_user_tier, is_admin_user
from src.dalton.auth import is_dalton_service_request
from src.dalton.capabilities import (
    CapabilityId,
    has_capability,
    require_capability,
    require_capability_dep,
    require_channel_platform_capability,
)
from src.dalton.database.channel_gateway import (
    SUPPORTED_PLATFORMS,
    authorize_and_claim_delivery,
    consume_bind_code_or_unbind,
    create_bind_code,
    execute_session_command,
    finish_automation_run,
    get_channel_internal_state,
    lookup_bot_topic,
    reconcile_steer,
    recover_channel_state,
    register_and_resolve_inbound,
    register_automation_run,
    resolve_authorized_automation_run_platforms,
    update_delivery_progress,
    update_inbound_progress,
    update_user_channel_settings,
    verify_dm_delivery_recipient,
)
from src.dalton.database import weixin_multi_account as wx_db
from src.dalton.database.weixin_multi_account import StaleGatewayEpochError
from src.dalton import channel_action_tokens as cat_svc
from src.dalton.settings import redact_weixin_secrets
from src.server.utils.api import CurrentUserId, ServiceUserId

logger = logging.getLogger(__name__)

channels_router = APIRouter(
    prefix="/channels",
    tags=["Dalton Channels"],
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_GATEWAY))],
)

_GATEWAY_PROXY_TIMEOUT = httpx.Timeout(45.0, connect=5.0)
_ADMIN_ONLY_WEIXIN_DETAIL = (
    "Only admin users can manage team Weixin channel settings."
)


def require_channel_service_token(request: Request) -> None:
    """Validate `X-Service-Token` header via `is_dalton_service_request`."""
    sent = request.headers.get("X-Service-Token", "")
    configured = os.getenv("INTERNAL_SERVICE_TOKEN") or getattr(
        env, "INTERNAL_SERVICE_TOKEN", ""
    )
    if not is_dalton_service_request(
        sent_token=sent, configured_token=configured or ""
    ):
        raise HTTPException(
            status_code=401, detail="Invalid or missing X-Service-Token"
        )


def _parse_gateway_epoch(
    request: Request,
    *,
    required: bool = False,
) -> int | None:
    """Parse `X-Gateway-Epoch` header; raise `StaleGatewayEpochError` (409) when malformed or required-and-missing."""
    raw = request.headers.get("X-Gateway-Epoch")
    if raw is None or not str(raw).strip():
        if required:
            raise StaleGatewayEpochError("Missing X-Gateway-Epoch header")
        return None
    try:
        val = int(str(raw).strip())
    except ValueError as exc:
        raise StaleGatewayEpochError("Invalid X-Gateway-Epoch header") from exc
    if val <= 0:
        raise StaleGatewayEpochError("Invalid X-Gateway-Epoch header")
    return val


def _is_readonly_session_command(
    *,
    cmd: str | None,
    message_id: str | None,
    is_internal_query: bool = False,
) -> bool:
    """Server-authoritative check for read-only I-3 session queries (never trust `is_internal_query` alone)."""
    cleaned_cmd = str(cmd or "").strip().lower()
    cleaned_mid = str(message_id or "").strip()
    return cleaned_cmd == "workspace_list" and not cleaned_mid and bool(is_internal_query)


def _handle_structured_http_error(exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    raise exc


def _resolve_internal_user_id(
    request: Request, body_or_query_user_id: str | None
) -> str:
    from src.dalton.company_identity import reject_company_system_auth

    header_uid = str(request.headers.get("X-User-Id") or "").strip()
    body_uid = str(body_or_query_user_id or "").strip()
    if header_uid:
        reject_company_system_auth(header_uid)
    if body_uid:
        reject_company_system_auth(body_uid)
    uid = body_uid or header_uid
    if not uid:
        raise HTTPException(status_code=400, detail="user_id or X-User-Id is required")
    return uid


def _resolve_optional_internal_user_id(
    request: Request,
    *candidate_user_ids: str | None,
) -> str | None:
    from src.dalton.company_identity import reject_company_system_auth

    header_uid = str(request.headers.get("X-User-Id") or "").strip() or None
    if header_uid is not None:
        reject_company_system_auth(header_uid)
    for raw_uid in candidate_user_ids:
        if raw_uid is not None:
            reject_company_system_auth(raw_uid)
    for raw_uid in (*candidate_user_ids, header_uid):
        cleaned = str(raw_uid or "").strip() or None
        if cleaned is not None:
            return cleaned
    return None


def _reject_nested_company_user_ids(*containers: dict[str, Any] | None) -> None:
    from src.dalton.company_identity import reject_company_system_auth

    for item in containers:
        if isinstance(item, dict):
            for key in (
                "user_id",
                "dalton_user_id",
                "owner_user_id",
                "login_owner_user_id",
            ):
                if key in item and item[key] is not None:
                    reject_company_system_auth(item[key])


# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------


class GatewayLeaseAcquireRequest(BaseModel):
    instance_id: str = Field(min_length=1, max_length=255)
    ttl_seconds: int = Field(default=45, ge=5, le=300)


class GatewayLeaseHeartbeatRequest(BaseModel):
    instance_id: str = Field(min_length=1, max_length=255)
    epoch: int = Field(ge=1)
    ttl_seconds: int = Field(default=45, ge=5, le=300)


class GatewayLeaseReleaseRequest(BaseModel):
    instance_id: str = Field(min_length=1, max_length=255)
    epoch: int = Field(ge=1)


class WeixinAccountsReconcileRequest(BaseModel):
    since_revision: int | None = None


class WeixinAccountCursorRequest(BaseModel):
    expected_bot_generation: int
    expected_ilink_bot_id: str
    expected_get_updates_buf: str | None = None
    get_updates_buf: str


class WeixinAccountStatusRequest(BaseModel):
    action: str
    expected_bot_generation: int
    expected_ilink_bot_id: str
    error_code: str | None = None
    recipient_platform_user_id: str | None = None
    expected_token_version: int | None = None


class WeixinLoginSessionPatchRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str
    expected_login_version: int | None = None
    expected_bot_generation: int | None = None
    status: str | None = None
    qrcode: str | None = None
    qrcode_img_content: str | None = None
    redirect_base_url: str | None = None
    expires_in_seconds: int | None = None
    ilink_bot_id: str | None = None
    bot_token: str | None = None
    base_url: str | None = None
    cdn_base_url: str | None = None
    ilink_user_id: str | None = None
    ilink_username: str | None = None


class PreflightSendRequest(BaseModel):
    delivery_attempt: int | None = None
    chunk_index: int | None = None


class VerifyDmRecipientRequest(BaseModel):
    delivery_attempt: int | None = None
    resolved_recipient_id: str | None = None


class TopicsLookupRequest(BaseModel):
    platform: str
    chat_address: str
    match_mode: str
    external_topic_id: str | None = None
    canonical_scope_key: str | None = None
    root_message_id: str | None = None
    initiator_platform_user_id: str | None = None
    pending_limit: int | None = Field(default=None, ge=1)
    pending_offset: int = Field(default=0, ge=0)


class InboundRegisterResolveRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str
    platform_user_id: str
    chat_type: str = "dm"
    chat_address: str
    thread_scope_key: str
    message_id: str
    request_key: str | None = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    external_topic_id: str | None = None
    root_message_id: str | None = None
    weixin_account_id: str | None = None
    weixin_context_token: str | None = None
    weixin_bot_id: str | None = None
    weixin_bot_generation: int | None = None
    weixin_context_seq: int | None = None
    weixin_media_meta: list[Any] | None = None
    unsupported_item_types: list[Any] | None = None


class BindCodeConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str = "bind"
    platform: str
    platform_user_id: str | None = None
    platform_username: str | None = None
    platform_tenant_key: str | None = None
    weixin_account_id: str | None = None
    weixin_context_token: str | None = None
    weixin_bot_id: str | None = None
    weixin_bot_generation: int | None = None
    inbound_message_id: str | None = None
    code: str | None = None
    is_group: bool = False
    user_id: str | None = None
    message_id: str | None = None
    chat_type: str | None = None
    chat_address: str | None = None
    scope_key: str | None = None
    request_key: str | None = None


class SessionCommandRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    cmd: str
    platform: str
    dalton_user_id: str | None = None
    platform_user_id: str | None = None
    chat_type: str = "dm"
    chat_address: str | None = None
    chat_id: str | None = None
    chat_name: str | None = None
    scope_key: str
    workspace_id: str | None = None
    workspace_query: str | None = None
    target_kind: str | None = None
    external_thread_id: str | None = None
    message_id: str | None = None
    request_key: str | None = None
    raw_payload: dict[str, Any] | None = None
    weixin_account_id: str | None = None
    weixin_context_token: str | None = None
    weixin_bot_id: str | None = None
    weixin_bot_generation: int | None = None
    weixin_context_source: str | None = None
    weixin_context_seq: int | None = None
    command_arg: str | None = None
    is_internal_query: bool = False
    native_interaction: bool = False
    native_ack_status: str | None = None
    native_reply_status: str | None = None


class ChannelSettingsPutRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str | None = None
    version: str | None = None
    settings: dict[str, Any] | None = None
    automation_output_update: dict[str, Any] | None = None
    workspace_id: str | None = None
    platform: str | None = None
    address: str | None = None


class DiscordChannelEntryRequest(BaseModel):
    action: str
    channel_id: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    name: str | None = None


class AuthorizeAndClaimDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    delivery_key: str
    kind: str = "agent_send"
    user_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    execution_id: str | None = None
    automation_execution_id: str | None = None
    platform: str | None = None
    target: str | None = None
    inbound_message_id: str | None = None
    has_non_empty_text: bool = True
    total_chunks: int = 0
    source_scope_key: str | None = None
    source_generation: int | None = None
    activity_origin: str | None = "none"
    requested_files: list[dict[str, Any]] | None = None
    source_delivery_key: str | None = None
    attempt_id: str | None = None
    reclaim_stale_attempt_id: str | None = None
    reclaim_stale_source_attempt_id: str | None = None
    retry_dedup_by_target: bool = False
    payload_digest: str | None = None


class DeliveryProgressRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    delivery_key: str
    sent_chunks: int | None = None
    total_chunks: int | None = None
    platform_message_ids: list[Any] | None = None
    files_status: list[Any] | None = None
    first_response_json: dict[str, Any] | None = None
    status: str | None = None
    outcome: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    confirmed_thread_address: str | None = None
    discord_body_phase: str | None = None
    delivery_attempt: int | None = None
    claim_attempt_id: str | None = None
    bot_topic_registration: dict[str, Any] | None = None


class ActionTokenMintRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str
    dalton_user_id: str
    initiator_platform_user_id: str
    origin_chat_type: str
    origin_chat_address: str
    origin_scope_key: str
    session_generation: int
    thread_id: str
    run_id: str
    interrupt_id: str
    interrupt_value: dict[str, Any]
    pending_interrupt_count: int = 1
    card_chat_type: str | None = None
    card_delivery_address: str | None = None
    prompt_summary: dict[str, Any] | None = None
    approval_snapshots: dict[str, Any] | None = None


class ActionTokenBindCardRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    approval_nonce: str | None = None
    token_hash: str | None = None
    phase: str = "confirm_bound"
    platform: str | None = None
    card_channel_id: str | None = None
    card_message_id: str | None = None
    send_reconcile_meta: dict[str, Any] | None = None


class ActionTokenInspectGateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    approval_nonce: str
    clicker_platform_user_id: str
    channel_id: str
    message_id: str


class ActionTokenConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    approval_nonce: str
    decision: str
    clicker_platform_user_id: str
    platform: str | None = None
    channel_id: str | None = None
    message_id: str | None = None
    reason: str | None = None
    answer_text: str | None = None
    option_index: int | None = None


class InterruptResumeClaimTextRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str
    message_id: str
    dalton_user_id: str
    scope_key: str
    generation: int
    thread_id: str
    run_id: str
    interrupt_id: str
    resume_payload: dict[str, Any]


class InterruptResumeAckRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    thread_id: str | None = None
    run_id: str | None = None
    interrupt_id: str | None = None
    claim_id: str | None = None
    outcome: str | None = None
    settle_delivery: bool = False
    error: str | None = None
    card_results: list[dict[str, Any]] | None = None
    late_card_results: list[dict[str, Any]] | None = None


class InterruptRuntimeConfigRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    mode: str | None = None
    interactive_actions_enabled: bool | None = None
    interrupt_ledger_enabled: bool | None = None
    bt2_link_recovery_enabled: bool | None = None


class InboundProgressRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str | None = None
    message_id: str | None = None
    status: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    turn_index: int | None = None
    request_key: str | None = None
    retry_request_key: str | None = None
    resubmitted_request_key: str | None = None
    steered_into_run_id: str | None = None
    fallback_pending: bool | None = None
    command_effect_status: str | None = None
    command_reply_pending: bool | None = None
    command_result: dict[str, Any] | None = None
    uploaded_attachments: list[Any] | None = None
    raw_payload: dict[str, Any] | None = None
    raw_payload_patch: dict[str, Any] | None = None
    session_interrupt_update: dict[str, Any] | None = None
    background_run: dict[str, Any] | None = None
    thread_watch: dict[str, Any] | None = None


class SteerReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    platform: str
    message_id: str
    run_id: str | None = None


class AutomationRunRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    execution_id: str
    user_id: str | None = None
    workspace_id: str | None = None
    automation_name: str = ""
    entries: list[str] = Field(default_factory=list)
    thread_id: str | None = None


class AutomationRunFinishRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    final_text: str | None = None
    targets: list[dict[str, Any]] | None = None
    mark_finished: bool = False


class BindCodeCreateRequest(BaseModel):
    platform: str


class WeixinVerifyCodeRequest(BaseModel):
    session_key: str
    verify_code: str


class WeixinAdminConfigPutRequest(BaseModel):
    enabled: bool | None = None
    allow_external: bool | None = None


class WeixinAdminUnbindRequest(BaseModel):
    reason: str | None = None
    note: str | None = None


_WEIXIN_QR_PUBLIC_WHITELIST: tuple[str, ...] = (
    "session_key",
    "status",
    "qrcode_url",
    "qrcode_img_content",
    "expires_at",
    "account_id",
    "account_status",
    "ilink_bot_id",
    "ilink_user_id",
    "masked_ilink_user_id",
    "ilink_username",
    "bot_generation",
    "accounts_revision",
    "connected",
    "already_connected",
    "pending_verification",
    "verify_code_accepted",
    "need_rescan",
    "revoked",
    "code",
    "message",
)

_FORBIDDEN_URL_PARAM_KEYS: frozenset[str] = frozenset(
    {
        "token",
        "bot_token",
        "context_token",
        "weixin_context_token",
        "access_token",
        "secret",
        "password",
        "key",
        "aeskey",
        "aes_key",
    }
)


def _sanitize_public_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.startswith("data:image/"):
        return raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.scheme not in ("https", "http"):
        return None
    for k, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if k.strip().lower() in _FORBIDDEN_URL_PARAM_KEYS:
            return None
    return raw


def _shape_public_weixin_qr_response(raw: dict[str, Any]) -> dict[str, Any]:
    if set(raw.keys()) == {"proxied", "user_id"}:
        return dict(raw)
    out: dict[str, Any] = {}
    for key in _WEIXIN_QR_PUBLIC_WHITELIST:
        if key not in raw:
            continue
        val = raw[key]
        if key == "qrcode_url":
            sanitized = _sanitize_public_url(val)
            if sanitized is not None:
                out[key] = sanitized
        elif key == "qrcode_img_content":
            if isinstance(val, str) and val.strip():
                if val.strip().startswith(("http://", "https://")):
                    sanitized = _sanitize_public_url(val)
                    if sanitized is not None:
                        out[key] = sanitized
                else:
                    out[key] = val.strip()
        elif key == "message" and isinstance(val, str):
            out[key] = redact_weixin_secrets(val)
        else:
            out[key] = val
    if (
        out.get("status") == "confirmed"
        and "pending_verification" not in out
        and out.get("account_status") in ("active", "pending_verification")
    ):
        out["pending_verification"] = out.get("account_status") == "pending_verification"
    return out


# ---------------------------------------------------------------------------
# §3.1 (1) Gateway Lease Endpoints
# ---------------------------------------------------------------------------


@channels_router.post(
    "/internal/gateway-lease/acquire",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_gateway_lease_acquire_endpoint(
    body: GatewayLeaseAcquireRequest,
) -> dict[str, Any]:
    """Acquire or renew the single-instance gateway lease."""
    return await wx_db.acquire_gateway_lease(
        instance_id=body.instance_id,
        ttl_seconds=body.ttl_seconds,
    )


@channels_router.post(
    "/internal/gateway-lease/heartbeat",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_gateway_lease_heartbeat_endpoint(
    body: GatewayLeaseHeartbeatRequest,
) -> Any:
    """Extend an active gateway lease; returns HTTP 409 `stale_gateway_epoch` if lost."""
    try:
        return await wx_db.heartbeat_gateway_lease(
            instance_id=body.instance_id,
            epoch=body.epoch,
            ttl_seconds=body.ttl_seconds,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/gateway-lease/release",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_gateway_lease_release_endpoint(
    body: GatewayLeaseReleaseRequest,
) -> dict[str, Any]:
    """Voluntarily release the gateway lease on clean shutdown."""
    return await wx_db.release_gateway_lease(
        instance_id=body.instance_id,
        epoch=body.epoch,
    )


# ---------------------------------------------------------------------------
# §3.1 (1) Weixin Multi-Account Internal Endpoints
# ---------------------------------------------------------------------------


@channels_router.post(
    "/internal/weixin/accounts/reconcile",
    dependencies=[
        Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN)),
        Depends(require_channel_service_token),
    ],
)
async def internal_weixin_accounts_reconcile_endpoint(
    body: WeixinAccountsReconcileRequest,
    request: Request,
) -> Any:
    """15s / sync-triggered reconcile: evaluate roster/switches and return full snapshot when changed."""
    try:
        epoch = _parse_gateway_epoch(request, required=True)
        return await wx_db.reconcile_weixin_accounts(
            gateway_epoch=int(epoch or 0),
            since_revision=body.since_revision,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/weixin/accounts/{account_id}",
    dependencies=[
        Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN)),
        Depends(require_channel_service_token),
    ],
)
async def internal_weixin_account_detail_endpoint(
    account_id: str,
    recipient_platform_user_id: str | None = Query(default=None),
    inbound_message_id: str | None = Query(default=None),
) -> Any:
    """Read-only account detail with decrypted `bot_token` and optional `context_token`."""
    try:
        return await wx_db.get_weixin_account_detail(
            account_id=account_id,
            recipient_platform_user_id=recipient_platform_user_id,
            inbound_message_id=inbound_message_id,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/weixin/accounts/{account_id}/cursor",
    dependencies=[
        Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN)),
        Depends(require_channel_service_token),
    ],
)
async def internal_weixin_account_cursor_endpoint(
    account_id: str,
    body: WeixinAccountCursorRequest,
    request: Request,
) -> Any:
    """Advance per-account `get_updates_buf` cursor with epoch, bot_generation, and cursor CAS."""
    try:
        epoch = _parse_gateway_epoch(request, required=True)
        return await wx_db.update_weixin_account_cursor(
            account_id=account_id,
            gateway_epoch=int(epoch or 0),
            expected_bot_generation=body.expected_bot_generation,
            expected_ilink_bot_id=body.expected_ilink_bot_id,
            expected_get_updates_buf=body.expected_get_updates_buf,
            get_updates_buf=body.get_updates_buf,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/weixin/accounts/{account_id}/status",
    dependencies=[
        Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN)),
        Depends(require_channel_service_token),
    ],
)
async def internal_weixin_account_status_endpoint(
    account_id: str,
    body: WeixinAccountStatusRequest,
    request: Request,
) -> Any:
    """Mark session expired, context_token stale (version CAS), or reset corrupt cursor."""
    try:
        epoch = _parse_gateway_epoch(request, required=True)
        return await wx_db.update_weixin_account_status(
            account_id=account_id,
            gateway_epoch=int(epoch or 0),
            action=body.action,
            expected_bot_generation=body.expected_bot_generation,
            expected_ilink_bot_id=body.expected_ilink_bot_id,
            error_code=body.error_code,
            recipient_platform_user_id=body.recipient_platform_user_id,
            expected_token_version=body.expected_token_version,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/weixin/login-sessions/{session_key}",
    dependencies=[
        Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN)),
        Depends(require_channel_service_token),
    ],
)
async def internal_weixin_login_session_get_endpoint(
    session_key: str,
) -> Any:
    """Read login session (with decrypted qrcode for gateway polling) and re-check confirmed validity."""
    try:
        return await wx_db.get_weixin_login_session(
            session_key=session_key,
            include_qrcode=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.patch(
    "/internal/weixin/login-sessions/{session_key}",
    dependencies=[
        Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN)),
        Depends(require_channel_service_token),
    ],
)
async def internal_weixin_login_session_patch_endpoint(
    session_key: str,
    body: WeixinLoginSessionPatchRequest,
    request: Request,
) -> Any:
    """Conditional `save_qr` / `update` / `confirm` on a QR login session (with `X-Gateway-Epoch`)."""
    try:
        epoch = _parse_gateway_epoch(request, required=True)
        res = await wx_db.patch_weixin_login_session(
            session_key=session_key,
            gateway_epoch=int(epoch or 0),
            action=body.action,
            expected_login_version=body.expected_login_version,
            expected_bot_generation=body.expected_bot_generation,
            status=body.status,
            qrcode=body.qrcode,
            qrcode_img_content=body.qrcode_img_content,
            redirect_base_url=body.redirect_base_url,
            expires_in_seconds=body.expires_in_seconds,
            ilink_bot_id=body.ilink_bot_id,
            bot_token=body.bot_token,
            base_url=body.base_url,
            cdn_base_url=body.cdn_base_url,
            ilink_user_id=body.ilink_user_id,
            ilink_username=body.ilink_username,
        )
        if (
            str(body.action or "").strip().lower() == "confirm"
            and res.get("ok")
            and res.get("accounts_revision") is not None
        ):
            await wx_db.notify_gateway_weixin_accounts_sync(
                accounts_revision=int(res["accounts_revision"]),
                reason="account_confirmed",
            )
        return res
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/deliveries/{delivery_key}/preflight-send",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_deliveries_preflight_send_endpoint(
    delivery_key: str,
    body: PreflightSendRequest,
    request: Request,
) -> Any:
    """Real-time re-authorization before every iLink outbound chunk / media upload / sendmessage."""
    try:
        # R7 (Decision #8): Do NOT parse X-Gateway-Epoch before platform capability check;
        # disabled capability must return 404 even when the header is missing or malformed.
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await wx_db.preflight_weixin_send(
            delivery_key=delivery_key,
            gateway_epoch=raw_epoch,
            delivery_attempt=body.delivery_attempt,
            chunk_index=body.chunk_index,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/deliveries/{delivery_key}/verify-dm-recipient",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_deliveries_verify_dm_recipient_endpoint(
    delivery_key: str,
    body: VerifyDmRecipientRequest,
    request: Request,
) -> Any:
    """Re-verify DM delivery's pinned platform recipient and current active binding before sending a file."""
    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await verify_dm_delivery_recipient(
            delivery_key=delivery_key,
            delivery_attempt=body.delivery_attempt,
            resolved_recipient_id=body.resolved_recipient_id,
            gateway_epoch=raw_epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/topics/lookup",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_topics_lookup_endpoint(
    body: TopicsLookupRequest,
) -> Any:
    """Read-only lookup of a registered bot topic for inbound follow-up or outbound routing."""
    try:
        return await lookup_bot_topic(
            platform=body.platform,
            chat_address=body.chat_address,
            match_mode=body.match_mode,
            external_topic_id=body.external_topic_id,
            canonical_scope_key=body.canonical_scope_key,
            root_message_id=body.root_message_id,
            initiator_platform_user_id=body.initiator_platform_user_id,
            pending_limit=body.pending_limit,
            pending_offset=body.pending_offset,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/deliverables/download",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_deliverables_download_endpoint(
    request: Request,
    delivery_key: str = Query(..., min_length=1),
    delivery_attempt: int = Query(..., ge=1),
    workspace_id: str = Query(..., min_length=1),
    path: str = Query(..., min_length=1),
) -> Any:
    """Download a verified DM turn deliverable after recipient, binding, and FileStamp/SHA-256 checks."""
    from urllib.parse import quote as _url_quote

    from src.dalton.channel_deliverables import download_verified_deliverable

    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        content_bytes, mime_type, filename = await download_verified_deliverable(
            delivery_key=delivery_key,
            delivery_attempt=delivery_attempt,
            workspace_id=workspace_id,
            path=path,
            gateway_epoch=raw_epoch,
        )
        encoded_name = _url_quote(filename, safe="")
        return Response(
            content=content_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
                "Cache-Control": "no-store",
            },
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/snapshots/status",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_snapshots_status_endpoint(
    run_id: str = Query(..., min_length=1),
    thread_id: str | None = Query(default=None),
    platform: str | None = Query(default=None),
) -> Any:
    """Query Postgres-authoritative chart_snapshots status (pending/ready/failed) for cross-process polling."""
    from src.dalton.channel_chart_snapshots import get_run_chart_snapshots_status

    try:
        return await get_run_chart_snapshots_status(
            run_id=run_id,
            thread_id=thread_id,
            platform=platform,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/snapshots/{snapshot_key:path}",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_snapshots_download_endpoint(
    snapshot_key: str,
    request: Request,
    run_id: str = Query(..., min_length=1),
    target_address: str = Query(..., min_length=1),
    delivery_key: str | None = Query(default=None),
    delivery_attempt: int | None = Query(default=None),
) -> Any:
    """Download a verified DM-only PNG chart snapshot after strict key, run, and owner DM binding checks."""
    from urllib.parse import quote as _url_quote

    from src.dalton.channel_chart_snapshots import download_verified_chart_snapshot

    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        content_bytes, mime_type, filename = await download_verified_chart_snapshot(
            snapshot_key=snapshot_key,
            run_id=run_id,
            target_address=target_address,
            delivery_key=delivery_key,
            delivery_attempt=delivery_attempt,
            gateway_epoch=raw_epoch,
        )
        encoded_name = _url_quote(filename, safe="")
        return Response(
            content=content_bytes,
            media_type=mime_type,
            headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{encoded_name}",
                "Cache-Control": "no-store",
            },
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/threads/{thread_id}/runs/{run_id}/replay",
)
async def internal_thread_run_replay_endpoint(
    thread_id: str,
    run_id: str,
    x_user_id: ServiceUserId,
    execution_id: str | None = Query(default=None),
    source: Literal["auto", "checkpoint"] = Query(default="auto"),
) -> Any:
    """Read-only SSE replay of a single target turn addressed directly by ``run_id``.

    Enforces account admission via ``ServiceUserId``, platform sub-capability checks
    resolved from authorized automation targets, and the same thread ownership /
    shared-workspace read checks as ``GET /api/v1/threads/{thread_id}/messages/replay``.
    Never writes back replay caches (``persist=False``).
    """
    from src.server.database.conversation.replay_rows import get_replay_thread_data
    from src.server.database.pool import DATABASE_UNAVAILABLE
    from src.server.database.shared_workspaces import is_holder
    from src.server.services.history.replay import (
        CheckpointReplayUnavailable,
        finish_lines,
        read_replay_page,
    )
    from src.server.services.shared_workspaces.access import thread_access
    from src.server.services.shared_workspaces.viewer import name_turn_senders
    from src.server.utils.api import database_unavailable

    cleaned_run_id = str(run_id or "").strip()
    if not cleaned_run_id:
        raise HTTPException(status_code=400, detail="run_id is required")

    try:
        authorized_platforms = await resolve_authorized_automation_run_platforms(
            thread_id=thread_id,
            run_id=cleaned_run_id,
            user_id=x_user_id,
            execution_id=execution_id,
        )
        if not authorized_platforms:
            raise HTTPException(
                status_code=403,
                detail="No authorized channel automation target for this run",
            )
        enabled_platform_found = False
        last_cap_exc: HTTPException | None = None
        for plat in authorized_platforms:
            try:
                require_channel_platform_capability(plat)
                enabled_platform_found = True
                break
            except HTTPException as exc:
                last_cap_exc = exc
        if not enabled_platform_found:
            raise HTTPException(
                status_code=403,
                detail=(
                    last_cap_exc.detail
                    if last_cap_exc is not None
                    else "Channel platform capability is disabled"
                ),
            )

        rows = await get_replay_thread_data(thread_id)
        if rows is None:
            raise HTTPException(status_code=404, detail="Thread not found")

        if is_holder(rows.owner_id):
            if rows.owner_id != x_user_id:
                await thread_access(thread_id, x_user_id)
            is_non_owner_reader = False
        else:
            from src.dalton.settings import is_dalton_multiuser_auth_enabled

            is_non_owner_reader = False
            if is_dalton_multiuser_auth_enabled():
                from src.dalton.sharing import require_thread_access

                await require_thread_access(
                    thread_id,
                    x_user_id,
                    channel="web",
                    need="read",
                    owner_id=rows.owner_id,
                    workspace_owner_id=rows.workspace_owner_id,
                    workspace_id=rows.thread.get("workspace_id"),
                )
                is_non_owner_reader = rows.owner_id != x_user_id
            elif rows.owner_id != x_user_id:
                raise HTTPException(status_code=403, detail="Forbidden")

        responses_map = getattr(rows, "responses_by_turn", None)
        target_turn_index: int | None = None
        target_resp_row: dict[str, Any] | None = None
        if isinstance(responses_map, dict):
            for t_idx, resp_row in responses_map.items():
                if not isinstance(resp_row, dict):
                    continue
                rid = str(
                    resp_row.get("conversation_response_id")
                    or resp_row.get("run_id")
                    or ""
                ).strip()
                if rid == cleaned_run_id:
                    target_turn_index = int(t_idx)
                    target_resp_row = resp_row
                    break

        if target_turn_index is None or target_resp_row is None:
            raise HTTPException(status_code=404, detail="Run not found in thread")

        try:
            page, resolved_source = await read_replay_page(
                rows,
                source=source,
                before_turn=target_turn_index + 1,
                limit=1,
                persist=False,
            )
        except CheckpointReplayUnavailable as exc:
            raise HTTPException(
                status_code=409,
                detail=f"Checkpoint replay unavailable: {exc}",
            )

        target_lines: list[Any] = []
        matched_page_run = False
        current_turn_index: int | None = None
        current_run_id: str | None = None
        for line in page.lines:
            try:
                payload = json.loads(line.data_json)
            except Exception:
                payload = None
            if not isinstance(payload, dict):
                continue
            raw_ti = payload.get("turn_index")
            ev_ti: int | None = None
            if isinstance(raw_ti, int) and not isinstance(raw_ti, bool):
                ev_ti = raw_ti
            elif isinstance(raw_ti, str) and raw_ti.strip().isdigit():
                ev_ti = int(raw_ti.strip())
            ev_rid = (
                str(
                    payload.get("response_id")
                    or payload.get("run_id")
                    or payload.get("report_run_id")
                    or ""
                ).strip()
                or None
            )
            if line.event in ("user_message", "turn_separator", "turn_start"):
                current_turn_index = ev_ti
                current_run_id = ev_rid
            eff_ti = ev_ti if ev_ti is not None else current_turn_index
            eff_rid = ev_rid or current_run_id
            # BW2: `read_replay_page(limit=1)` can include both a prior committed turn and
            # a subsequent uncheckpointed target turn. Emit ONLY events for the target turn.
            if eff_ti is not None and eff_ti != target_turn_index:
                continue
            if eff_rid is not None and eff_rid != cleaned_run_id:
                continue
            if eff_ti != target_turn_index and eff_rid != cleaned_run_id:
                continue
            if ev_rid == cleaned_run_id:
                matched_page_run = True
            target_lines.append(line)

        await finish_lines(thread_id, target_lines)
        if is_holder(rows.owner_id):
            await name_turn_senders(thread_id, target_lines)

        target_queries = [
            q
            for q in (getattr(rows, "queries", None) or ())
            if isinstance(q, dict) and q.get("turn_index") == target_turn_index
        ]

        async def event_generator():
            seq = 0
            for line in target_lines:
                seq += 1
                yield f"id: {seq}\nevent: {line.event}\ndata: {line.data_json}\n\n"

            done: dict[str, object] = {
                "thread_id": thread_id,
                "turn_index": target_turn_index,
            }
            if matched_page_run:
                done["run_id"] = cleaned_run_id
            if not is_non_owner_reader and has_capability(CapabilityId.CHANNEL_GATEWAY):
                from src.dalton.channel_chart_snapshots import (
                    resolve_replay_chart_snapshots_by_run,
                )
                from src.dalton.colleague_outbound_guard import (
                    is_run_row_outbound_blocked,
                )

                blocked = is_run_row_outbound_blocked(target_resp_row)
                done["outbound_blocked_by_run"] = {cleaned_run_id: blocked}
                if not blocked:
                    resp_meta = target_resp_row.get("metadata")
                    if isinstance(resp_meta, dict):
                        td = resp_meta.get("turn_deliverables")
                        if isinstance(td, list) and td:
                            done["turn_deliverables_by_run"] = {cleaned_run_id: td}
                chart_snapshots_by_run = await resolve_replay_chart_snapshots_by_run(
                    thread_id=thread_id,
                    queries=target_queries,
                    responses_by_turn={target_turn_index: target_resp_row},
                )
                if chart_snapshots_by_run:
                    done["chart_snapshots_by_run"] = chart_snapshots_by_run

            seq += 1
            yield f"id: {seq}\nevent: replay_done\ndata: {json.dumps(done, default=str)}\n\n"

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Replay-Source": resolved_source,
            },
        )
    except DATABASE_UNAVAILABLE as exc:
        raise database_unavailable(
            f"replaying run {cleaned_run_id} in thread {thread_id}", logger, exc
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


# ---------------------------------------------------------------------------
# Shared Internal Persistence Endpoints (I-1 ~ I-8)
# ---------------------------------------------------------------------------


@channels_router.post(
    "/internal/inbound/register-and-resolve",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_inbound_register_and_resolve_endpoint(
    body: InboundRegisterResolveRequest,
    request: Request,
) -> Any:
    """I-1: Idempotently register inbound event and resolve user, tier, workspace, and generation."""
    require_channel_platform_capability(body.platform)
    _resolve_optional_internal_user_id(
        request,
        getattr(body, "user_id", None),
        getattr(body, "dalton_user_id", None),
    )
    _reject_nested_company_user_ids(body.raw_payload)
    try:
        epoch = _parse_gateway_epoch(request, required=False)
        return await register_and_resolve_inbound(
            platform=body.platform,
            platform_user_id=body.platform_user_id,
            chat_type=body.chat_type,
            chat_address=body.chat_address,
            thread_scope_key=body.thread_scope_key,
            message_id=body.message_id,
            request_key=body.request_key,
            raw_payload=body.raw_payload,
            external_topic_id=body.external_topic_id,
            root_message_id=body.root_message_id,
            weixin_account_id=body.weixin_account_id,
            weixin_context_token=body.weixin_context_token,
            weixin_bot_id=body.weixin_bot_id,
            weixin_bot_generation=body.weixin_bot_generation,
            weixin_context_seq=body.weixin_context_seq,
            weixin_media_meta=body.weixin_media_meta,
            unsupported_item_types=body.unsupported_item_types,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/bind-code/consume",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_bind_code_consume_endpoint(
    body: BindCodeConsumeRequest,
    request: Request,
) -> Any:
    """I-2: Consume a bind code (or revoke on group leak) or unbind a platform."""
    require_channel_platform_capability(body.platform)
    eff_uid = _resolve_optional_internal_user_id(
        request,
        body.user_id,
        getattr(body, "dalton_user_id", None),
    )
    try:
        epoch = _parse_gateway_epoch(request, required=False)
        return await consume_bind_code_or_unbind(
            action=body.action,
            platform=body.platform,
            platform_user_id=body.platform_user_id,
            platform_username=body.platform_username,
            platform_tenant_key=body.platform_tenant_key,
            weixin_account_id=body.weixin_account_id,
            weixin_context_token=body.weixin_context_token,
            weixin_bot_id=body.weixin_bot_id,
            weixin_bot_generation=body.weixin_bot_generation,
            inbound_message_id=body.inbound_message_id,
            code=body.code,
            is_group=body.is_group,
            user_id=eff_uid,
            message_id=body.message_id,
            chat_type=body.chat_type,
            chat_address=body.chat_address,
            scope_key=body.scope_key,
            request_key=body.request_key,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/session/command",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_session_command_endpoint(
    body: SessionCommandRequest,
    request: Request,
) -> Any:
    """I-3: Execute session commands (`/workspace`, `/main`, `/new`, `/stop`)."""
    require_channel_platform_capability(body.platform)
    eff_uid = _resolve_optional_internal_user_id(
        request,
        body.dalton_user_id,
        getattr(body, "user_id", None),
    )
    _reject_nested_company_user_ids(body.raw_payload)
    if not eff_uid and not str(body.platform_user_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail="dalton_user_id, X-User-Id, or platform_user_id is required",
        )
    try:
        is_readonly = _is_readonly_session_command(
            cmd=body.cmd,
            message_id=body.message_id,
            is_internal_query=bool(body.is_internal_query),
        )
        require_epoch = not is_readonly
        epoch = _parse_gateway_epoch(request, required=False)
        return await execute_session_command(
            cmd=body.cmd,
            platform=body.platform,
            dalton_user_id=eff_uid,
            platform_user_id=body.platform_user_id,
            chat_type=body.chat_type,
            chat_address=body.chat_address,
            chat_id=body.chat_id,
            chat_name=body.chat_name,
            scope_key=body.scope_key,
            workspace_id=body.workspace_id,
            workspace_query=body.workspace_query,
            target_kind=body.target_kind,
            external_thread_id=body.external_thread_id,
            message_id=body.message_id,
            request_key=body.request_key,
            raw_payload=body.raw_payload,
            weixin_account_id=body.weixin_account_id,
            weixin_context_token=body.weixin_context_token,
            weixin_bot_id=body.weixin_bot_id,
            weixin_bot_generation=body.weixin_bot_generation,
            weixin_context_source=body.weixin_context_source,
            weixin_context_seq=body.weixin_context_seq,
            command_arg=body.command_arg,
            is_internal_query=is_readonly,
            native_interaction=body.native_interaction,
            native_ack_status=body.native_ack_status,
            native_reply_status=body.native_reply_status,
            gateway_epoch=epoch,
            require_gateway_epoch=require_epoch,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/state",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_state_endpoint(
    request: Request,
    user_id: str | None = Query(default=None),
    thread_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    platform: str | None = Query(default=None),
    external_thread_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """I-4: Query user tier, home_workspace_id, identities, settings, chat_bindings, and current conversation."""
    if platform:
        require_channel_platform_capability(platform)
    eff_uid = _resolve_internal_user_id(request, user_id)
    return await get_channel_internal_state(
        user_id=eff_uid,
        thread_id=thread_id,
        run_id=run_id,
        platform=platform,
        external_thread_id=external_thread_id,
    )


@channels_router.put(
    "/internal/settings",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_settings_put_endpoint(
    body: ChannelSettingsPutRequest,
    request: Request,
) -> Any:
    """I-5: Validate and update `dalton_channel_user_settings` with string `version` CAS."""
    eff_uid = _resolve_internal_user_id(request, body.user_id)
    auto_upd = body.automation_output_update
    if (
        auto_upd is None
        and body.settings is None
        and body.workspace_id is not None
        and body.platform is not None
    ):
        auto_upd = {
            "workspace_id": body.workspace_id,
            "platform": body.platform,
            "address": body.address,
        }
    try:
        return await update_user_channel_settings(
            user_id=eff_uid,
            version=body.version,
            settings=body.settings,
            automation_output_update=auto_upd,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/deliveries/authorize-and-claim",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_deliveries_authorize_and_claim_endpoint(
    body: AuthorizeAndClaimDeliveryRequest,
    request: Request,
) -> Any:
    """I-6: Authorize outbound delivery target and atomically claim `in_flight` slot."""
    eff_uid = _resolve_optional_internal_user_id(
        request,
        body.user_id,
        getattr(body, "dalton_user_id", None),
    )
    try:
        epoch = _parse_gateway_epoch(request, required=False)
        return await authorize_and_claim_delivery(
            delivery_key=body.delivery_key,
            kind=body.kind,
            user_id=eff_uid,
            thread_id=body.thread_id,
            run_id=body.run_id,
            execution_id=body.execution_id,
            automation_execution_id=body.automation_execution_id,
            platform=body.platform,
            target=body.target,
            inbound_message_id=body.inbound_message_id,
            has_non_empty_text=body.has_non_empty_text,
            total_chunks=body.total_chunks,
            source_scope_key=body.source_scope_key,
            source_generation=body.source_generation,
            activity_origin=body.activity_origin,
            requested_files=body.requested_files,
            source_delivery_key=body.source_delivery_key,
            attempt_id=body.attempt_id,
            reclaim_stale_attempt_id=body.reclaim_stale_attempt_id,
            reclaim_stale_source_attempt_id=body.reclaim_stale_source_attempt_id,
            retry_dedup_by_target=body.retry_dedup_by_target,
            payload_digest=body.payload_digest,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/deliveries/progress",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_deliveries_progress_endpoint(
    body: DeliveryProgressRequest,
    request: Request,
) -> Any:
    """I-7 (1): Update chunk/file progress and status on `dalton_channel_deliveries`."""
    _resolve_optional_internal_user_id(
        request,
        getattr(body, "user_id", None),
        getattr(body, "dalton_user_id", None),
    )
    try:
        epoch = _parse_gateway_epoch(request, required=False)
        return await update_delivery_progress(
            delivery_key=body.delivery_key,
            sent_chunks=body.sent_chunks,
            total_chunks=body.total_chunks,
            platform_message_ids=body.platform_message_ids,
            files_status=body.files_status,
            first_response_json=body.first_response_json,
            status=body.status,
            outcome=body.outcome,
            error_code=body.error_code,
            error_message=body.error_message,
            confirmed_thread_address=body.confirmed_thread_address,
            discord_body_phase=body.discord_body_phase,
            delivery_attempt=body.delivery_attempt,
            claim_attempt_id=body.claim_attempt_id,
            bot_topic_registration=body.bot_topic_registration,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/inbound/progress",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_inbound_progress_endpoint(
    body: InboundProgressRequest,
    request: Request,
) -> Any:
    """I-7 (2): Update inbound event progress, pending_interrupt, background_runs, and thread_watches."""
    if body.platform:
        require_channel_platform_capability(body.platform)
    elif isinstance(body.thread_watch, dict) and body.thread_watch.get("platform"):
        require_channel_platform_capability(str(body.thread_watch.get("platform")))
    elif isinstance(body.session_interrupt_update, dict) and body.session_interrupt_update.get("platform"):
        require_channel_platform_capability(str(body.session_interrupt_update.get("platform")))
    _resolve_optional_internal_user_id(
        request,
        getattr(body, "user_id", None),
        getattr(body, "dalton_user_id", None),
    )
    _reject_nested_company_user_ids(
        body.session_interrupt_update,
        body.background_run,
        body.thread_watch,
        body.raw_payload,
        body.raw_payload_patch,
    )
    try:
        epoch = _parse_gateway_epoch(request, required=False)
        return await update_inbound_progress(
            platform=body.platform,
            message_id=body.message_id,
            status=body.status,
            thread_id=body.thread_id,
            run_id=body.run_id,
            turn_index=body.turn_index,
            request_key=body.request_key,
            retry_request_key=body.retry_request_key,
            resubmitted_request_key=body.resubmitted_request_key,
            steered_into_run_id=body.steered_into_run_id,
            fallback_pending=body.fallback_pending,
            command_effect_status=body.command_effect_status,
            command_reply_pending=body.command_reply_pending,
            command_result=body.command_result,
            uploaded_attachments=body.uploaded_attachments,
            raw_payload=body.raw_payload,
            raw_payload_patch=body.raw_payload_patch,
            session_interrupt_update=body.session_interrupt_update,
            background_run=body.background_run,
            thread_watch=body.thread_watch,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/steers/reconcile",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_steers_reconcile_endpoint(
    body: SteerReconcileRequest,
    request: Request,
) -> Any:
    """I-7 (3): Reconcile `hook_outbox` steer outcome with `conversation_responses.status`."""
    require_channel_platform_capability(body.platform)
    _resolve_optional_internal_user_id(
        request,
        getattr(body, "user_id", None),
        getattr(body, "dalton_user_id", None),
    )
    try:
        epoch = _parse_gateway_epoch(request, required=False)
        return await reconcile_steer(
            platform=body.platform,
            message_id=body.message_id,
            run_id=body.run_id,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/internal/recover",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_recover_endpoint(
    request: Request,
    mode: str = Query(default="startup"),
    thread_id: str | None = Query(default=None),
) -> Any:
    """I-7 (4): Recover channel gateway state on `startup`, `watch_reconnect`, or `interrupt_resumes`."""
    _resolve_optional_internal_user_id(request)
    try:
        # Check enabled platform capabilities BEFORE parsing X-Gateway-Epoch (§3.1 #5)
        if not cat_svc.get_enabled_channel_platforms():
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "channel_platforms_disabled",
                    "message": "All channel platform capabilities are disabled.",
                },
            )
        epoch = _parse_gateway_epoch(request, required=False)
        return await recover_channel_state(
            mode=mode,
            thread_id=thread_id,
            gateway_epoch=epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


# ---------------------------------------------------------------------------
# Channel Phase 2 Batch 3: Interactive Action Tokens & Interrupt Resumes
# ---------------------------------------------------------------------------


@channels_router.post(
    "/internal/action-tokens/mint",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_action_tokens_mint_endpoint(
    body: ActionTokenMintRequest,
    request: Request,
) -> Any:
    """Classify an HITL interrupt (L0–L3 / credit_pause) and mint an opaque action token."""
    _resolve_optional_internal_user_id(request, body.dalton_user_id)
    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await cat_svc.mint_channel_action_token(
            platform=body.platform,
            dalton_user_id=body.dalton_user_id,
            initiator_platform_user_id=body.initiator_platform_user_id,
            origin_chat_type=body.origin_chat_type,
            origin_chat_address=body.origin_chat_address,
            origin_scope_key=body.origin_scope_key,
            session_generation=body.session_generation,
            thread_id=body.thread_id,
            run_id=body.run_id,
            interrupt_id=body.interrupt_id,
            interrupt_value=body.interrupt_value,
            pending_interrupt_count=body.pending_interrupt_count,
            card_chat_type=body.card_chat_type,
            card_delivery_address=body.card_delivery_address,
            prompt_summary=body.prompt_summary,
            approval_snapshots=body.approval_snapshots,
            gateway_epoch=raw_epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/action-tokens/bind-card",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_action_tokens_bind_card_endpoint(
    body: ActionTokenBindCardRequest,
    request: Request,
) -> Any:
    """Persist pre-send `send_in_flight`, confirm card message binding, or record `send_failed` / `send_unknown`."""
    _resolve_optional_internal_user_id(request)
    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await cat_svc.bind_channel_action_card(
            approval_nonce=body.approval_nonce,
            token_hash=body.token_hash,
            phase=body.phase,
            platform=body.platform,
            card_channel_id=body.card_channel_id,
            card_message_id=body.card_message_id,
            send_reconcile_meta=body.send_reconcile_meta,
            gateway_epoch=raw_epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/action-tokens/inspect-gate",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_action_tokens_inspect_gate_endpoint(
    body: ActionTokenInspectGateRequest,
    request: Request,
) -> Any:
    """Verify Discord group gate card click, write `gate_verified_at`, and mint ephemeral detail token."""
    _resolve_optional_internal_user_id(request)
    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await cat_svc.inspect_discord_gate_token(
            approval_nonce=body.approval_nonce,
            clicker_platform_user_id=body.clicker_platform_user_id,
            channel_id=body.channel_id,
            message_id=body.message_id,
            gateway_epoch=raw_epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/action-tokens/consume",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_action_tokens_consume_endpoint(
    body: ActionTokenConsumeRequest,
    request: Request,
) -> Any:
    """Verify channel card button click and atomically claim the interrupt resume slot."""
    _resolve_optional_internal_user_id(request)
    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await cat_svc.consume_channel_action_token(
            approval_nonce=body.approval_nonce,
            decision=body.decision,
            clicker_platform_user_id=body.clicker_platform_user_id,
            platform=body.platform,
            channel_id=body.channel_id,
            message_id=body.message_id,
            reason=body.reason,
            answer_text=body.answer_text,
            option_index=body.option_index,
            gateway_epoch=raw_epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/interrupt-resumes/claim-text",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_interrupt_resumes_claim_text_endpoint(
    body: InterruptResumeClaimTextRequest,
    request: Request,
) -> Any:
    """Claim an `ask_user_question` or `credit_pause` interrupt via channel text reply."""
    _resolve_optional_internal_user_id(request, body.dalton_user_id)
    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await cat_svc.claim_channel_text_interrupt_resume(
            platform=body.platform,
            message_id=body.message_id,
            dalton_user_id=body.dalton_user_id,
            scope_key=body.scope_key,
            generation=body.generation,
            thread_id=body.thread_id,
            run_id=body.run_id,
            interrupt_id=body.interrupt_id,
            resume_payload=body.resume_payload,
            gateway_epoch=raw_epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/interrupt-resumes/ack",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_interrupt_resumes_ack_endpoint(
    body: InterruptResumeAckRequest,
    request: Request,
) -> Any:
    """Ack origin-channel dispatch/delivery settlement or per-card / late-card terminal patches."""
    _resolve_optional_internal_user_id(request)
    try:
        raw_epoch = request.headers.get("X-Gateway-Epoch")
        return await cat_svc.ack_channel_interrupt_resume(
            thread_id=body.thread_id,
            run_id=body.run_id,
            interrupt_id=body.interrupt_id,
            claim_id=body.claim_id,
            outcome=body.outcome,
            settle_delivery=body.settle_delivery,
            error=body.error,
            card_results=body.card_results,
            late_card_results=body.late_card_results,
            gateway_epoch=raw_epoch,
            require_gateway_epoch=True,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/interrupt-runtime-config",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_interrupt_runtime_config_endpoint(
    body: InterruptRuntimeConfigRequest,
    request: Request,
) -> Any:
    """Update single-row 7b-0 runtime barrier config or enter three-switch production drain mode."""
    _resolve_optional_internal_user_id(request)
    try:
        cleaned_mode = str(body.mode or "").strip().lower()
        if cleaned_mode == "enter_drain":
            return await cat_svc.enter_interrupt_drain_mode()
        if (
            body.interrupt_ledger_enabled is not None
            or body.bt2_link_recovery_enabled is not None
        ):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "drain_mode_required",
                    "message": "Only interactive_actions_enabled may be changed outside mode='enter_drain'.",
                },
            )
        return await cat_svc.set_interrupt_runtime_config(
            interactive_actions_enabled=body.interactive_actions_enabled,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/internal/automation-runs",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_automation_runs_register_endpoint(
    body: AutomationRunRegisterRequest,
    request: Request,
) -> dict[str, Any]:
    """I-8 (1): Idempotently register automation run and resolve target entries."""
    eff_uid = _resolve_internal_user_id(request, body.user_id)
    return await register_automation_run(
        execution_id=body.execution_id,
        user_id=eff_uid,
        workspace_id=body.workspace_id,
        automation_name=body.automation_name,
        entries=body.entries,
        thread_id=body.thread_id,
    )


@channels_router.post(
    "/internal/automation-runs/{execution_id}/finish",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_automation_runs_finish_endpoint(
    execution_id: str,
    body: AutomationRunFinishRequest,
    request: Request,
) -> dict[str, Any]:
    """I-8 (2): Claim or finalize automation run delivery settlement."""
    _resolve_optional_internal_user_id(
        request,
        getattr(body, "user_id", None),
    )
    return await finish_automation_run(
        execution_id=execution_id,
        status=body.status,
        thread_id=body.thread_id,
        run_id=body.run_id,
        final_text=body.final_text,
        targets=body.targets,
        mark_finished=body.mark_finished,
    )


# ---------------------------------------------------------------------------
# §3.1 (2) Web Frontend Channel Management Endpoints
# ---------------------------------------------------------------------------


@channels_router.post("/bind-codes")
async def web_create_bind_code_endpoint(
    body: BindCodeCreateRequest,
    user_id: CurrentUserId,
) -> Any:
    """Generate a 10-minute `DLT-XXXX-XXXX` bind code (Feishu/Discord only; Weixin uses QR login)."""
    require_channel_platform_capability(body.platform)
    if str(body.platform or "").strip().lower() == "weixin":
        return JSONResponse(
            status_code=400,
            content={
                "code": "weixin_uses_qr_login",
                "message": "个人微信现已改为扫码直接连接，不再使用绑定码。",
            },
        )
    return await create_bind_code(user_id=user_id, platform=body.platform)


@channels_router.get("/bindings")
async def web_get_bindings_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """Return user's bindings across enabled platforms; omits Weixin keys when `channel_weixin` is off."""
    weixin_cap = has_capability(CapabilityId.CHANNEL_WEIXIN)
    state = await get_channel_internal_state(user_id=user_id, include_secrets=False)
    identities = {
        p: {
            k: v
            for k, v in info.items()
            if k not in ("weixin_context_token", "encrypted_weixin_context_token")
        }
        for p, info in state["identities"].items()
        if p != "weixin" or weixin_cap
    }
    bindings: dict[str, dict[str, Any]] = {}
    for p in SUPPORTED_PLATFORMS:
        if p == "weixin" and not weixin_cap:
            continue
        info = identities.get(p)
        if info:
            bindings[p] = {
                "linked": True,
                "platform": p,
                "platform_user_id": info["platform_user_id"],
                "platform_username": info.get("platform_username"),
                "updated_at": info.get("updated_at"),
            }
        else:
            bindings[p] = {
                "linked": False,
                "platform": p,
                "platform_user_id": None,
                "platform_username": None,
                "updated_at": None,
            }

    resp: dict[str, Any] = {
        "user_id": state["user_id"],
        "user_tier": state["user_tier"],
        "home_workspace_id": state["home_workspace_id"],
        "bindings": bindings,
        "identities": identities,
        "settings": state["settings"],
        "chat_bindings": state["chat_bindings"],
    }
    if weixin_cap:
        wx_view = await wx_db.get_member_weixin_binding_view(
            user_id=user_id,
            user_tier=str(state.get("user_tier") or "external"),
        )
        resp["weixin_account"] = wx_view
        if wx_view.get("linked"):
            bindings["weixin"] = {
                "linked": True,
                "platform": "weixin",
                "account_id": wx_view.get("account_id"),
                "status": wx_view.get("status"),
                "platform_user_id": wx_view.get("platform_user_id"),
                "masked_ilink_user_id": wx_view.get("masked_ilink_user_id"),
                "platform_username": wx_view.get("ilink_username"),
                "has_usable_context_token": wx_view.get("has_usable_context_token"),
                "updated_at": wx_view.get("updated_at"),
            }
    return resp


@channels_router.put("/settings")
async def web_put_channel_settings_endpoint(
    body: ChannelSettingsPutRequest,
    user_id: CurrentUserId,
) -> Any:
    """Web group workspace & channel settings update (reuses `I-5` validation and generation advancement)."""
    if body.user_id is not None:
        from src.dalton.company_identity import reject_company_system_auth

        reject_company_system_auth(body.user_id)
    auto_upd = body.automation_output_update
    if (
        auto_upd is None
        and body.settings is None
        and body.workspace_id is not None
        and body.platform is not None
    ):
        auto_upd = {
            "workspace_id": body.workspace_id,
            "platform": body.platform,
            "address": body.address,
        }
    try:
        return await update_user_channel_settings(
            user_id=user_id,
            version=body.version,
            settings=body.settings,
            automation_output_update=auto_upd,
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/discord-channels",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_DISCORD))],
)
async def web_mutate_discord_channel_endpoint(
    body: DiscordChannelEntryRequest,
    user_id: CurrentUserId,
) -> Any:
    """Admin-only single-entry mutation (add/rename) for `apps.discord.chats`."""
    from src.dalton.auth_guards import raise_admin_required

    tier = await get_user_tier(user_id)
    if tier != "admin":
        raise_admin_required("Only admin users can manage Discord channels.")
    cleaned_action = str(body.action or "").strip().lower()
    if cleaned_action == "remove":
        return JSONResponse(
            status_code=400,
            content={
                "code": "unsupported_action",
                "message": "本版不支持在此移除 Discord 频道",
            },
        )
    try:
        return await update_user_channel_settings(
            user_id=user_id,
            version=body.version,
            discord_channel_entry={
                "action": cleaned_action,
                "channel_id": body.channel_id,
                "name": body.name,
            },
        )
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.delete("/bindings/{platform}")
async def web_delete_binding_endpoint(
    platform: str,
    user_id: CurrentUserId,
) -> Any:
    """Unbind platform account from Web UI (checks platform capability)."""
    cleaned_platform = str(platform or "").strip().lower()
    if cleaned_platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported platform: {platform!r}"
        )
    require_channel_platform_capability(cleaned_platform)
    if cleaned_platform == "weixin":
        try:
            res = await wx_db.unbind_weixin_account(
                target_user_id=user_id,
                operator_user_id=user_id,
                action="member_unbind",
            )
            if res.get("accounts_revision") is not None:
                await wx_db.notify_gateway_weixin_accounts_sync(
                    accounts_revision=int(res["accounts_revision"]),
                    reason="member_unbind",
                )
            return res
        except HTTPException as exc:
            return _handle_structured_http_error(exc)
    return await consume_bind_code_or_unbind(
        action="unbind",
        platform=cleaned_platform,
        user_id=user_id,
    )


async def _require_weixin_admin(user_id: str) -> str:
    from src.dalton.auth_guards import raise_admin_required

    if not await is_admin_user(user_id):
        raise_admin_required(_ADMIN_ONLY_WEIXIN_DETAIL)
    return user_id


_PUBLIC_WEIXIN_QR_ERROR_MAP: dict[str, tuple[int, str, dict[str, Any]]] = {
    "session_revoked": (
        409,
        "登录会话已被撤销，请重新获取二维码。",
        {"revoked": True, "need_rescan": True},
    ),
    "login_session_revoked": (
        409,
        "登录会话已被撤销，请重新获取二维码。",
        {"revoked": True, "need_rescan": True},
    ),
    "session_expired": (
        409,
        "二维码已过期，请重新获取二维码。",
        {"need_rescan": True},
    ),
    "qr_expired": (
        409,
        "二维码已过期，请重新获取二维码。",
        {"need_rescan": True},
    ),
    "invalid_verify_code": (
        400,
        "验证码无效，请核对后重新输入。",
        {},
    ),
    "verify_code_expired": (
        409,
        "验证码已过期，请重新扫码连接。",
        {"need_rescan": True},
    ),
    "weixin_login_failed": (
        502,
        "微信登录校验失败，请重新获取二维码并扫码。",
        {"need_rescan": True},
    ),
    "weixin_rate_limited": (
        429,
        "微信服务请求过于频繁，请稍后再试。",
        {},
    ),
    "ilink_user_already_bound": (
        409,
        "该微信号已绑定其他团队成员账号，无法重复绑定。",
        {},
    ),
    "weixin_capacity_full": (
        409,
        "团队微信并发连接数已达上限，请联系管理员处理。",
        {},
    ),
    "weixin_channel_disabled": (
        403,
        "个人微信渠道当前已关闭。",
        {},
    ),
    "external_access_disabled": (
        403,
        "管理员当前未向外部成员开放个人微信渠道。",
        {},
    ),
    "member_not_in_roster": (
        403,
        "当前账号不在团队成员名册中，无法连接个人微信。",
        {},
    ),
    "roster_lookup_error": (
        503,
        "成员名册查询暂时异常，请稍后重试。",
        {},
    ),
    "stale_bot_generation": (
        409,
        "原连接已被其他扫码更新，请重新获取二维码。",
        {"need_rescan": True},
    ),
    "stale_gateway_epoch": (
        503,
        "微信网关正在切换中，请稍后重试。",
        {},
    ),
    "gateway_unreachable": (
        503,
        "微信网关暂时不可用，请稍后重试。",
        {},
    ),
}


def _build_public_weixin_qr_error(
    raw_code: str | None,
    upstream_status: int,
) -> HTTPException:
    """Build a fixed Chinese public QR error strictly from known error codes (never echo gateway `detail`)."""
    cleaned_code = str(raw_code or "").strip().lower()
    if cleaned_code in _PUBLIC_WEIXIN_QR_ERROR_MAP:
        status_code, msg, extra = _PUBLIC_WEIXIN_QR_ERROR_MAP[cleaned_code]
        return HTTPException(
            status_code=status_code,
            detail={
                "code": cleaned_code,
                "message": msg,
                **extra,
            },
        )
    safe_status = 503 if upstream_status >= 500 else (409 if upstream_status == 409 else 400)
    return HTTPException(
        status_code=safe_status,
        detail={
            "code": "weixin_qr_gateway_error",
            "message": "微信扫码服务暂时异常，请稍后重试。",
        },
    )


async def _await_request_disconnect(request: Request) -> None:
    while True:
        if await request.is_disconnected():
            return
        await asyncio.sleep(0.05)


async def _proxy_to_gateway_weixin(
    method: str,
    subpath: str,
    *,
    user_id: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    request: Request | None = None,
) -> dict[str, Any]:
    from src.dalton.settings import get_dalton_channel_gateway_url

    require_capability(CapabilityId.CHANNEL_GATEWAY, CapabilityId.CHANNEL_WEIXIN)
    base_url = get_dalton_channel_gateway_url().strip().rstrip("/")
    if not base_url:
        raise _build_public_weixin_qr_error("gateway_unreachable", 503)
    token = (
        os.getenv("INTERNAL_SERVICE_TOKEN")
        or getattr(env, "INTERNAL_SERVICE_TOKEN", "")
        or ""
    ).strip()
    headers = {
        "X-Service-Token": token,
        "X-User-Id": user_id,
    }
    url = f"{base_url}{subpath}"
    try:
        async with httpx.AsyncClient(timeout=_GATEWAY_PROXY_TIMEOUT) as client:
            if request is None:
                resp = await client.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_body,
                )
            else:
                req_task = asyncio.create_task(
                    client.request(
                        method,
                        url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )
                )
                disconnect_task = asyncio.create_task(_await_request_disconnect(request))
                try:
                    done, _ = await asyncio.wait(
                        {req_task, disconnect_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if req_task in done:
                        resp = req_task.result()
                    else:
                        req_task.cancel()
                        await asyncio.gather(req_task, return_exceptions=True)
                        raise HTTPException(
                            status_code=499,
                            detail={
                                "code": "client_disconnected",
                                "message": "客户端已取消请求。",
                            },
                        )
                finally:
                    if not disconnect_task.done():
                        disconnect_task.cancel()
                        await asyncio.gather(disconnect_task, return_exceptions=True)
    except httpx.HTTPError as exc:
        raise _build_public_weixin_qr_error("gateway_unreachable", 503) from exc

    if resp.status_code >= 400:
        err_code: str | None = None
        try:
            err_data = resp.json()
            if isinstance(err_data, dict):
                if isinstance(err_data.get("code"), str):
                    err_code = err_data["code"]
                elif isinstance(err_data.get("detail"), dict) and isinstance(
                    err_data["detail"].get("code"), str
                ):
                    err_code = err_data["detail"]["code"]
        except Exception:
            err_code = None
        raise _build_public_weixin_qr_error(err_code, resp.status_code)

    return resp.json()


@channels_router.post(
    "/weixin/qrcode/start",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN))],
)
async def web_weixin_qrcode_start_endpoint(
    user_id: CurrentUserId,
) -> Any:
    """Start personal Weixin QR login: pre-writes `starting` session before calling gateway (D14)."""
    try:
        start_res = await wx_db.start_weixin_login_session(user_id=user_id)
        raw = await _proxy_to_gateway_weixin(
            "POST",
            "/internal/weixin/qrcode/start",
            user_id=user_id,
            json_body={
                "session_key": start_res["session_key"],
                "user_id": user_id,
                "login_version": start_res["login_version"],
                "expected_bot_generation": start_res.get("expected_bot_generation"),
            },
        )
        shaped = _shape_public_weixin_qr_response(raw)
        if "session_key" not in shaped:
            shaped["session_key"] = start_res["session_key"]
        return shaped
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.get(
    "/weixin/qrcode/wait",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN))],
)
async def web_weixin_qrcode_wait_endpoint(
    request: Request,
    user_id: CurrentUserId,
    session_key: str = Query(..., min_length=1),
) -> Any:
    """Poll personal Weixin QR login status (verifies ownership & D7 confirmed validity)."""
    try:
        sess = await wx_db.get_weixin_login_session(
            session_key=session_key,
            user_id=user_id,
            include_qrcode=False,
        )
        st = str(sess.get("status") or "")
        if st == "confirmed" and sess.get("connected") is True:
            confirmed = sess.get("confirmed_result") or {}
            account_status = sess.get("account_status") or confirmed.get("account_status")
            pending_verification = account_status == "pending_verification"
            return _shape_public_weixin_qr_response(
                {
                    "session_key": session_key,
                    "status": "confirmed",
                    "connected": True,
                    "already_connected": True,
                    "account_id": confirmed.get("account_id"),
                    "account_status": account_status,
                    "pending_verification": pending_verification,
                    "bot_generation": confirmed.get("bot_generation"),
                    "accounts_revision": confirmed.get("accounts_revision"),
                }
            )
        if st in ("superseded", "expired", "failed"):
            return _shape_public_weixin_qr_response(
                {
                    "session_key": session_key,
                    "status": st,
                    "connected": False,
                    "revoked": st == "superseded",
                    "need_rescan": True,
                    "message": "登录会话已失效，请重新获取二维码。"
                    if st != "superseded"
                    else "登录会话已被撤销，请重新获取二维码。",
                }
            )
        raw = await _proxy_to_gateway_weixin(
            "GET",
            "/internal/weixin/qrcode/wait",
            user_id=user_id,
            params={"session_key": session_key},
            request=request,
        )
        return _shape_public_weixin_qr_response(raw)
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.post(
    "/weixin/qrcode/verify-code",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN))],
)
async def web_weixin_qrcode_verify_code_endpoint(
    body: WeixinVerifyCodeRequest,
    user_id: CurrentUserId,
) -> Any:
    """Submit phone verification code during personal Weixin QR login."""
    try:
        sess = await wx_db.get_weixin_login_session(
            session_key=body.session_key,
            user_id=user_id,
            include_qrcode=False,
        )
        st = str(sess.get("status") or "")
        if st in ("superseded", "expired", "failed"):
            return JSONResponse(
                status_code=409,
                content={
                    "code": "session_revoked" if st == "superseded" else "session_expired",
                    "status": st,
                    "revoked": st == "superseded",
                    "need_rescan": True,
                    "message": "登录会话已失效，请重新获取二维码。",
                },
            )
        raw = await _proxy_to_gateway_weixin(
            "POST",
            "/internal/weixin/qrcode/verify-code",
            user_id=user_id,
            json_body={
                "session_key": body.session_key,
                "verify_code": body.verify_code,
            },
        )
        return _shape_public_weixin_qr_response(raw)
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


# ---------------------------------------------------------------------------
# §3.1 (2) Admin Weixin Management Endpoints
# ---------------------------------------------------------------------------


@channels_router.get(
    "/weixin/admin/overview",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN))],
)
async def web_weixin_admin_overview_endpoint(
    user_id: CurrentUserId,
) -> Any:
    """Admin overview: switches, slot usage, gateway lease, masked member states, and latest 50 audits."""
    await _require_weixin_admin(user_id)
    return await wx_db.get_weixin_admin_overview()


@channels_router.put(
    "/weixin/admin/config",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN))],
)
async def web_weixin_admin_config_put_endpoint(
    body: WeixinAdminConfigPutRequest,
    user_id: CurrentUserId,
) -> Any:
    """Admin update of `enabled` / `allow_external` switches with unified account re-evaluation."""
    await _require_weixin_admin(user_id)
    try:
        res = await wx_db.update_weixin_admin_config(
            operator_user_id=user_id,
            enabled=body.enabled,
            allow_external=body.allow_external,
        )
        if res.get("accounts_revision") is not None:
            await wx_db.notify_gateway_weixin_accounts_sync(
                accounts_revision=int(res["accounts_revision"]),
                reason="admin_config_updated",
            )
        return res
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


def _format_admin_unbind_reason(body: WeixinAdminUnbindRequest | None) -> str:
    return wx_db.validate_admin_unbind_reason(
        body.reason if body is not None else None,
        note=body.note if body is not None else None,
    )


@channels_router.delete(
    "/weixin/admin/accounts/{account_id}",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN))],
)
async def web_weixin_admin_delete_account_endpoint(
    account_id: str,
    user_id: CurrentUserId,
    body: WeixinAdminUnbindRequest | None = Body(default=None),
) -> Any:
    """Admin unbind a bound Weixin account by `account_id` with audit reason."""
    await _require_weixin_admin(user_id)
    try:
        res = await wx_db.unbind_weixin_account(
            account_id=account_id,
            operator_user_id=user_id,
            action="admin_unbind",
            reason=_format_admin_unbind_reason(body),
        )
        if res.get("accounts_revision") is not None:
            await wx_db.notify_gateway_weixin_accounts_sync(
                accounts_revision=int(res["accounts_revision"]),
                reason="admin_unbind",
            )
        return res
    except HTTPException as exc:
        return _handle_structured_http_error(exc)


@channels_router.delete(
    "/weixin/admin/users/{target_user_id}/weixin",
    dependencies=[Depends(require_capability_dep(CapabilityId.CHANNEL_WEIXIN))],
)
async def web_weixin_admin_delete_user_weixin_endpoint(
    target_user_id: str,
    user_id: CurrentUserId,
    body: WeixinAdminUnbindRequest | None = Body(default=None),
) -> Any:
    """Admin unbind/revoke by `target_user_id` (also revokes `starting`/`wait` sessions with no account)."""
    await _require_weixin_admin(user_id)
    try:
        res = await wx_db.unbind_weixin_account(
            target_user_id=target_user_id,
            operator_user_id=user_id,
            action="admin_unbind",
            reason=_format_admin_unbind_reason(body),
        )
        if res.get("accounts_revision") is not None:
            await wx_db.notify_gateway_weixin_accounts_sync(
                accounts_revision=int(res["accounts_revision"]),
                reason="admin_unbind",
            )
        return res
    except HTTPException as exc:
        return _handle_structured_http_error(exc)
