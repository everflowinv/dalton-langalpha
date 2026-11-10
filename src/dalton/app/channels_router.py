"""FastAPI router for CH-1 Channel Gateway internal persistence (`I-1` ~ `I-9`, §1.4) and Web management (§1.5).

Mounted under `/api/v1/dalton` via `src/dalton/app/router.py`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field

from src.config import env
from src.dalton.access_tiers import get_user_tier
from src.dalton.auth import is_dalton_service_request
from src.dalton.database.channel_gateway import (
    SUPPORTED_PLATFORMS,
    authorize_and_claim_delivery,
    consume_bind_code_or_unbind,
    create_bind_code,
    execute_session_command,
    finish_automation_run,
    get_channel_internal_state,
    get_weixin_state,
    reconcile_steer,
    recover_channel_state,
    register_and_resolve_inbound,
    register_automation_run,
    update_delivery_progress,
    update_inbound_progress,
    update_user_channel_settings,
    upsert_weixin_state,
)
from src.server.utils.api import CurrentUserId

logger = logging.getLogger(__name__)

channels_router = APIRouter(prefix="/channels", tags=["Dalton Channels"])

_GATEWAY_PROXY_TIMEOUT = httpx.Timeout(45.0, connect=5.0)
_ADMIN_ONLY_WEIXIN_DETAIL = (
    "Only admin users can connect or replace the shared Weixin bot."
)


def require_channel_service_token(request: Request) -> None:
    """Validate `X-Service-Token` header via `is_dalton_service_request` (§1.1 #2)."""
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


def _resolve_internal_user_id(
    request: Request, body_or_query_user_id: str | None
) -> str:
    from src.dalton.company_identity import reject_company_system_auth

    uid = (
        str(body_or_query_user_id or "").strip()
        or str(request.headers.get("X-User-Id") or "").strip()
    )
    if not uid:
        raise HTTPException(status_code=400, detail="user_id or X-User-Id is required")
    reject_company_system_auth(uid)
    return uid


# ---------------------------------------------------------------------------
# Pydantic Request Models
# ---------------------------------------------------------------------------


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
    weixin_context_token: str | None = None


class BindCodeConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    action: str = "bind"
    platform: str
    platform_user_id: str | None = None
    platform_username: str | None = None
    platform_tenant_key: str | None = None
    weixin_context_token: str | None = None
    code: str | None = None
    is_group: bool = False
    user_id: str | None = None


class SessionCommandRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    cmd: str
    platform: str
    dalton_user_id: str | None = None
    chat_type: str = "dm"
    chat_address: str | None = None
    chat_id: str | None = None
    chat_name: str | None = None
    scope_key: str
    workspace_id: str | None = None
    workspace_query: str | None = None
    external_thread_id: str | None = None


class ChannelSettingsPutRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str | None = None
    version: str | None = None
    settings: dict[str, Any] | None = None
    automation_output_update: dict[str, Any] | None = None
    workspace_id: str | None = None
    platform: str | None = None
    address: str | None = None


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


class DeliveryProgressRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    delivery_key: str
    sent_chunks: int | None = None
    total_chunks: int | None = None
    platform_message_ids: list[Any] | None = None
    files_status: list[Any] | None = None
    first_response_json: dict[str, Any] | None = None
    status: str | None = None
    error_code: str | None = None
    error_message: str | None = None


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
    final_text: str | None = None
    targets: list[dict[str, Any]] | None = None
    mark_finished: bool = False


class WeixinStatePutRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    ilink_bot_id: str | None = None
    owner_user_id: str | None = None
    bot_token: str | None = None
    base_url: str | None = None
    get_updates_buf: str | None = None
    status: str | None = None
    ilink_user_id: str | None = None
    ilink_username: str | None = None


class BindCodeCreateRequest(BaseModel):
    platform: str


class WeixinVerifyCodeRequest(BaseModel):
    session_key: str
    verify_code: str


# ---------------------------------------------------------------------------
# §1.4 Internal Persistence Endpoints (I-1 ~ I-9)
# ---------------------------------------------------------------------------


@channels_router.post(
    "/internal/inbound/register-and-resolve",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_inbound_register_and_resolve_endpoint(
    body: InboundRegisterResolveRequest,
) -> dict[str, Any]:
    """I-1: Idempotently register inbound event and resolve user, tier, workspace, and generation."""
    return await register_and_resolve_inbound(
        platform=body.platform,
        platform_user_id=body.platform_user_id,
        chat_type=body.chat_type,
        chat_address=body.chat_address,
        thread_scope_key=body.thread_scope_key,
        message_id=body.message_id,
        request_key=body.request_key,
        raw_payload=body.raw_payload,
        weixin_context_token=body.weixin_context_token,
    )


@channels_router.post(
    "/internal/bind-code/consume",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_bind_code_consume_endpoint(
    body: BindCodeConsumeRequest,
    request: Request,
) -> dict[str, Any]:
    """I-2: Consume a bind code (or revoke on group leak) or unbind a platform."""
    eff_uid = body.user_id or (request.headers.get("X-User-Id") or "").strip() or None
    return await consume_bind_code_or_unbind(
        action=body.action,
        platform=body.platform,
        platform_user_id=body.platform_user_id,
        platform_username=body.platform_username,
        platform_tenant_key=body.platform_tenant_key,
        weixin_context_token=body.weixin_context_token,
        code=body.code,
        is_group=body.is_group,
        user_id=eff_uid,
    )


@channels_router.post(
    "/internal/session/command",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_session_command_endpoint(
    body: SessionCommandRequest,
    request: Request,
) -> dict[str, Any]:
    """I-3: Execute `/workspace`, `/new`, or `/stop` session command."""
    eff_uid = _resolve_internal_user_id(request, body.dalton_user_id)
    return await execute_session_command(
        cmd=body.cmd,
        platform=body.platform,
        dalton_user_id=eff_uid,
        chat_type=body.chat_type,
        chat_address=body.chat_address,
        chat_id=body.chat_id,
        chat_name=body.chat_name,
        scope_key=body.scope_key,
        workspace_id=body.workspace_id,
        workspace_query=body.workspace_query,
        external_thread_id=body.external_thread_id,
    )


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
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        raise


@channels_router.post(
    "/internal/deliveries/authorize-and-claim",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_deliveries_authorize_and_claim_endpoint(
    body: AuthorizeAndClaimDeliveryRequest,
    request: Request,
) -> dict[str, Any]:
    """I-6: Authorize outbound delivery target and atomically claim `in_flight` slot."""
    eff_uid = body.user_id or (request.headers.get("X-User-Id") or "").strip() or None
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
    )


@channels_router.post(
    "/internal/deliveries/progress",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_deliveries_progress_endpoint(
    body: DeliveryProgressRequest,
) -> dict[str, Any]:
    """I-7 (1): Update chunk/file progress and status on `dalton_channel_deliveries`."""
    return await update_delivery_progress(
        delivery_key=body.delivery_key,
        sent_chunks=body.sent_chunks,
        total_chunks=body.total_chunks,
        platform_message_ids=body.platform_message_ids,
        files_status=body.files_status,
        first_response_json=body.first_response_json,
        status=body.status,
        error_code=body.error_code,
        error_message=body.error_message,
    )


@channels_router.post(
    "/internal/inbound/progress",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_inbound_progress_endpoint(
    body: InboundProgressRequest,
) -> dict[str, Any]:
    """I-7 (2): Update inbound event progress, pending_interrupt, background_runs, and thread_watches."""
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
        uploaded_attachments=body.uploaded_attachments,
        raw_payload=body.raw_payload,
        raw_payload_patch=body.raw_payload_patch,
        session_interrupt_update=body.session_interrupt_update,
        background_run=body.background_run,
        thread_watch=body.thread_watch,
    )


@channels_router.post(
    "/internal/steers/reconcile",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_steers_reconcile_endpoint(
    body: SteerReconcileRequest,
) -> dict[str, Any]:
    """I-7 (3): Reconcile `hook_outbox` steer outcome with `conversation_responses.status`."""
    return await reconcile_steer(
        platform=body.platform,
        message_id=body.message_id,
        run_id=body.run_id,
    )


@channels_router.get(
    "/internal/recover",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_recover_endpoint(
    mode: str = Query(default="startup"),
    thread_id: str | None = Query(default=None),
) -> dict[str, Any]:
    """I-7 (4): Recover channel gateway state on `startup` or `watch_reconnect`."""
    return await recover_channel_state(mode=mode, thread_id=thread_id)


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
) -> dict[str, Any]:
    """I-8 (2): Claim or finalize automation run delivery settlement."""
    return await finish_automation_run(
        execution_id=execution_id,
        status=body.status,
        thread_id=body.thread_id,
        final_text=body.final_text,
        targets=body.targets,
        mark_finished=body.mark_finished,
    )


@channels_router.get(
    "/internal/weixin-state",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_weixin_state_get_endpoint() -> dict[str, Any]:
    """I-9 (1): Read singleton Weixin state with decrypted `bot_token`."""
    return await get_weixin_state(include_token=True)


@channels_router.put(
    "/internal/weixin-state",
    dependencies=[Depends(require_channel_service_token)],
)
async def internal_weixin_state_put_endpoint(
    body: WeixinStatePutRequest,
) -> dict[str, Any]:
    """I-9 (2): Upsert singleton Weixin state with `pgp_sym_encrypt`."""
    return await upsert_weixin_state(
        ilink_bot_id=body.ilink_bot_id,
        owner_user_id=body.owner_user_id,
        bot_token=body.bot_token,
        base_url=body.base_url,
        get_updates_buf=body.get_updates_buf,
        status=body.status,
        ilink_user_id=body.ilink_user_id,
        ilink_username=body.ilink_username,
    )


# ---------------------------------------------------------------------------
# §1.5 Web Frontend Channel Management Endpoints (4 Groups)
# ---------------------------------------------------------------------------


@channels_router.post("/bind-codes")
async def web_create_bind_code_endpoint(
    body: BindCodeCreateRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """§1.5 (1): Generate a 10-minute `DLT-XXXX-XXXX` bind code for the current user."""
    return await create_bind_code(user_id=user_id, platform=body.platform)


@channels_router.get("/bindings")
async def web_get_bindings_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """§1.5 (2): Return user's bindings across Feishu/Discord/Weixin and (for admin) global Weixin bot status."""
    state = await get_channel_internal_state(user_id=user_id)
    identities = {
        p: {k: v for k, v in info.items() if k != "weixin_context_token"}
        for p, info in state["identities"].items()
    }
    bindings: dict[str, dict[str, Any]] = {}
    for p in SUPPORTED_PLATFORMS:
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

    weixin_bot = None
    if state["user_tier"] == "admin":
        weixin_bot = await get_weixin_state(include_token=False)

    return {
        "user_id": state["user_id"],
        "user_tier": state["user_tier"],
        "home_workspace_id": state["home_workspace_id"],
        "bindings": bindings,
        "identities": identities,
        "settings": state["settings"],
        "chat_bindings": state["chat_bindings"],
        "weixin_bot": weixin_bot,
    }


@channels_router.put("/settings")
async def web_put_channel_settings_endpoint(
    body: ChannelSettingsPutRequest,
    user_id: CurrentUserId,
) -> Any:
    """Web group workspace & channel settings update (reuses `I-5` validation and generation advancement)."""
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
        if isinstance(exc.detail, dict) and "code" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        raise


@channels_router.delete("/bindings/{platform}")
async def web_delete_binding_endpoint(
    platform: str,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """§1.5 (3): Unbind platform account from Web UI (reuses `I-2` `action: "unbind"`)."""
    cleaned_platform = str(platform or "").strip().lower()
    if cleaned_platform not in SUPPORTED_PLATFORMS:
        raise HTTPException(
            status_code=400, detail=f"Unsupported platform: {platform!r}"
        )
    return await consume_bind_code_or_unbind(
        action="unbind",
        platform=cleaned_platform,
        user_id=user_id,
    )


async def _require_weixin_admin(user_id: str) -> str:
    tier = await get_user_tier(user_id)
    if tier != "admin":
        raise HTTPException(status_code=403, detail=_ADMIN_ONLY_WEIXIN_DETAIL)
    return user_id


async def _proxy_to_gateway_weixin(
    method: str,
    subpath: str,
    *,
    user_id: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_url = (
        os.getenv("CHANNEL_GATEWAY_URL")
        or getattr(env, "CHANNEL_GATEWAY_URL", "")
        or "http://127.0.0.1:8962"
    ).strip().rstrip("/")
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
            resp = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Channel gateway is unreachable: {exc}",
        ) from exc

    if resp.status_code >= 400:
        try:
            err_data = resp.json()
            detail = (
                err_data.get("detail")
                or err_data.get("message")
                or f"Gateway error ({resp.status_code})"
            )
        except Exception:
            detail = f"Gateway error ({resp.status_code})"
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


@channels_router.post("/weixin/qrcode/start")
async def web_weixin_qrcode_start_endpoint(
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """§1.5 (4a): Admin-only endpoint to start Weixin iLink Bot QR code login."""
    await _require_weixin_admin(user_id)
    return await _proxy_to_gateway_weixin(
        "POST",
        "/internal/weixin/qrcode/start",
        user_id=user_id,
    )


@channels_router.get("/weixin/qrcode/wait")
async def web_weixin_qrcode_wait_endpoint(
    user_id: CurrentUserId,
    session_key: str = Query(..., min_length=1),
) -> dict[str, Any]:
    """§1.5 (4b): Admin-only endpoint to poll Weixin QR code login status."""
    await _require_weixin_admin(user_id)
    return await _proxy_to_gateway_weixin(
        "GET",
        "/internal/weixin/qrcode/wait",
        user_id=user_id,
        params={"session_key": session_key},
    )


@channels_router.post("/weixin/qrcode/verify-code")
async def web_weixin_qrcode_verify_code_endpoint(
    body: WeixinVerifyCodeRequest,
    user_id: CurrentUserId,
) -> dict[str, Any]:
    """§1.5 (4c): Admin-only endpoint to submit phone verification code during Weixin QR login."""
    await _require_weixin_admin(user_id)
    return await _proxy_to_gateway_weixin(
        "POST",
        "/internal/weixin/qrcode/verify-code",
        user_id=user_id,
        json_body={
            "session_key": body.session_key,
            "verify_code": body.verify_code,
        },
    )
