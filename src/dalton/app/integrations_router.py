"""FastAPI router for personal calendar & mail OAuth integrations (§4.2, §4.3, §6).

Endpoints mounted under ``/api/v1/dalton/integrations``:
- ``GET    /api/v1/dalton/integrations``                              (mode="manage")
- ``POST   /api/v1/dalton/integrations/{provider}/{service}/authorize`` (mode="connect")
- ``GET    /api/v1/dalton/integrations/{provider}/callback``          (no Bearer; state-driven)
- ``POST   /api/v1/dalton/integrations/confirm``                      (mode="connect")
- ``PATCH  /api/v1/dalton/integrations/{provider}/{service}``         (mode="connect")
- ``DELETE /api/v1/dalton/integrations/{provider}/{service}``         (mode="manage")
- ``DELETE /api/v1/dalton/integrations/{provider}``                   (mode="manage")
- ``POST   /api/v1/dalton/integrations/purges/{purge_id}/retry-revoke`` (mode="manage")
- ``POST   /api/v1/dalton/integrations/feishu/events``                (no Bearer; mode="manage")

All handlers register as ``CORE_CAPABILITY`` in ``DALTON_ROUTE_CAPABILITY_REGISTRY``
and enforce per-service single-capability gating internally via
``require_personal_integration_capability(..., mode="connect" | "manage")`` (§6 item 1).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from src.dalton.app.channels_router import require_channel_service_token
from src.dalton.auth import resolve_dalton_create_user_email
from src.dalton.auth.dispatch import _read_local_session_from_request
from src.dalton.capabilities import (
    PERSONAL_INTEGRATION_CAPABILITIES,
    CapabilityId,
    effective_capabilities,
    get_connector_providers_summary,
    require_personal_integration_capability,
)
from src.dalton.company_identity import is_company_system_user
from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationRow,
    LocalPurgeStatus,
    PurgeLedgerRow,
    RemoteRevokeStatus,
    TriggerReason,
)
from src.dalton.deployment_credentials.catalog import (
    FEISHU_APP_ID_KEY,
    GOOGLE_OAUTH_CLIENT_ID_KEY,
)
from src.dalton.deployment_credentials.snapshot import get_credential_group
from src.dalton.integrations.constants import (
    DEFAULT_INTEGRATIONS_RETURN_TO,
    FEISHU_MAIL_SEND_SCOPE,
    GOOGLE_GMAIL_DRAFT_SCOPES,
    OAUTH_STATE_TTL_SECONDS,
    PERSONAL_INTEGRATION_CONNECTOR_SPECS,
    build_feishu_integration_events_url,
    build_integration_redirect_uri,
)
from src.dalton.integrations.oauth_clients import (
    FeishuOAuthClientProtocol,
    GoogleOAuthClientProtocol,
    get_feishu_oauth_client,
    get_google_oauth_client,
    get_google_remote_revoker,
)
from src.dalton.services.feishu_integration_events import (
    FeishuEventAuthError,
    FeishuEventNotConfiguredError,
    has_matching_feishu_event_connection,
    normalize_gateway_feishu_event,
    process_feishu_integration_event,
    verify_and_decode_feishu_event,
)
from src.dalton.services.integration_revoke import (
    RemoteRevoker,
    process_revoke,
    schedule_background_remote_revokes,
)
from src.dalton.services.user_integrations_lifecycle import (
    IntegrationNotFoundError,
    MissingRefreshTokenOnAccountSwitchError,
    begin_disconnect,
    begin_disconnect_all_services,
    finish_disconnect,
)
from src.dalton.services.user_integrations_oauth import (
    OAUTH_BROWSER_BINDING_COOKIE_NAME,
    PersonalIntegrationError,
    confirm_pending_integration,
    handle_oauth_callback,
    sanitize_return_to,
    start_oauth_authorization,
    validate_callback_location,
)
from src.dalton.settings import (
    get_dalton_auth_mode,
    get_dalton_public_app_url,
    is_dalton_enabled,
)
from src.server.database.shared_workspaces import is_holder
from src.server.utils.api import CurrentUserId, caller_of

logger = logging.getLogger(__name__)

_NO_STORE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"google", "feishu"})
_ALLOWED_SERVICES: frozenset[str] = frozenset({"calendar", "mail"})
_ALLOWED_PREFERENCE_KEYS: frozenset[str] = frozenset(
    {
        "primary_calendar_id",
        "tracker_calendar_id",
        "allow_create_draft",
        "allow_send_mail",
    }
)


# ---------------------------------------------------------------------------
# Injectable client getters (overridden in tests via FastAPI dependency_overrides
# or monkeypatch on this module)
# ---------------------------------------------------------------------------


def get_router_google_client() -> GoogleOAuthClientProtocol:
    return get_google_oauth_client()


def get_router_feishu_client() -> FeishuOAuthClientProtocol:
    return get_feishu_oauth_client()


def get_router_remote_revoker() -> RemoteRevoker:
    return get_google_remote_revoker()


# ---------------------------------------------------------------------------
# Interactive web-session dependency (supports local, supabase, oss; blocks tokens)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegrationSessionContext:
    user_id: str
    auth_mode: str
    session_id: str | None = None
    user_email: str | None = None


async def require_integration_web_session(
    request: Request,
    user_id: CurrentUserId,
) -> IntegrationSessionContext:
    """Ensure caller is a natural-person interactive web session (never service/CLI/PAT/holder)."""
    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    if "x-service-token" in request.headers:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "session_auth_required",
                "message": "Interactive web session authentication is required.",
            },
        )
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        caller = caller_of(scope)
        if caller is not None and caller.auth != "session":
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "session_auth_required",
                    "message": "Interactive web session authentication is required.",
                },
            )
    if is_company_system_user(user_id) or is_holder(user_id):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "session_auth_required",
                "message": "Interactive web session authentication is required.",
            },
        )

    auth_mode = get_dalton_auth_mode()
    session_id = _read_local_session_from_request(request) if auth_mode == "local" else None
    user_email = (
        resolve_dalton_create_user_email(user_id, None)
        if auth_mode == "supabase"
        else None
    )
    return IntegrationSessionContext(
        user_id=user_id,
        auth_mode=auth_mode,
        session_id=session_id,
        user_email=user_email,
    )


CurrentIntegrationSession = Annotated[
    IntegrationSessionContext, Depends(require_integration_web_session)
]


# ---------------------------------------------------------------------------
# Redacted APIRoute ensuring tokens/codes/states never echo in error payloads
# ---------------------------------------------------------------------------


class RedactedIntegrationsRoute(APIRoute):
    """Wraps route handlers to return safe, redacted JSON errors with ``no-store``."""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def _handled(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "code": "validation_error",
                        "message": "Invalid personal integration request payload.",
                    },
                    headers=_NO_STORE_HEADERS,
                )
            except PersonalIntegrationError as exc:
                body: dict[str, Any] = {
                    "code": exc.code,
                    "message": str(exc),
                }
                if exc.details:
                    body["details"] = exc.details
                return JSONResponse(
                    status_code=exc.status_code,
                    content=body,
                    headers=_NO_STORE_HEADERS,
                )
            except MissingRefreshTokenOnAccountSwitchError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "code": "missing_refresh_token_on_account_switch",
                        "message": "切换账号时未获得新的刷新令牌，已保留原账号连接不变。",
                    },
                    headers=_NO_STORE_HEADERS,
                )
            except IntegrationNotFoundError:
                return JSONResponse(
                    status_code=404,
                    content={
                        "code": "integration_not_found",
                        "message": "Integration connection not found.",
                    },
                    headers=_NO_STORE_HEADERS,
                )
            except FeishuEventNotConfiguredError as exc:
                return JSONResponse(
                    status_code=503,
                    content={"code": exc.code, "message": str(exc)},
                    headers=_NO_STORE_HEADERS,
                )
            except FeishuEventAuthError as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"code": exc.code, "message": str(exc)},
                    headers=_NO_STORE_HEADERS,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.error(
                    "[integrations-router] unhandled error (%s)",
                    type(exc).__name__,
                )
                return JSONResponse(
                    status_code=500,
                    content={
                        "code": "internal_error",
                        "message": "Personal integration request failed.",
                    },
                    headers=_NO_STORE_HEADERS,
                )

        return _handled


router = APIRouter(
    prefix="/api/v1/dalton/integrations",
    tags=["Dalton Personal Integrations"],
    route_class=RedactedIntegrationsRoute,
)


# ---------------------------------------------------------------------------
# Request models & safe serializers (zero secrets / tokens)
# ---------------------------------------------------------------------------


class AuthorizeIntegrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    switch_account: bool = False
    allow_create_draft: bool = False
    allow_send_mail: bool = False
    return_to: str | None = None


class ConfirmIntegrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_token: str
    confirm_account_switch: bool = False
    cancel: bool = False


class UpdatePreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary_calendar_id: str | None = None
    tracker_calendar_id: str | None = None
    allow_create_draft: bool | None = None
    allow_send_mail: bool | None = None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _active_client_id_for_provider(provider: str) -> str | None:
    if provider == "google":
        g = get_credential_group("google_oauth")
        return g.values.get(GOOGLE_OAUTH_CLIENT_ID_KEY) if g.ready else None
    if provider == "feishu":
        f = get_credential_group("feishu_app")
        return f.values.get(FEISHU_APP_ID_KEY) if f.ready else None
    return None


def _serialize_connection(row: IntegrationRow) -> dict[str, Any]:
    active_cid = _active_client_id_for_provider(row.provider)
    client_rotated = bool(active_cid and row.oauth_client_id != active_cid)
    return {
        "provider": row.provider,
        "service": row.service,
        "provider_account_id": row.provider_account_id,
        "provider_email": row.provider_email,
        "granted_scopes": row.granted_scopes.split() if row.granted_scopes else [],
        "status": row.status.value,
        "auth_epoch": row.auth_epoch,
        "authorized_at": _iso(row.authorized_at),
        "expires_at": _iso(row.expires_at),
        "last_refresh_at": _iso(row.last_refresh_at),
        "needs_reauth_at": _iso(row.needs_reauth_at),
        "last_error_code": row.last_error_code,
        "client_rotated": client_rotated,
        "preferences": dict(row.preferences or {}),
        "updated_at": _iso(row.updated_at),
    }


def _serialize_purge_ledger(row: PurgeLedgerRow) -> dict[str, Any]:
    return {
        "purge_id": row.purge_id,
        "provider": row.provider,
        "service": row.service,
        "provider_account_id": row.provider_account_id,
        "trigger_reason": row.trigger_reason,
        "local_purge_status": row.local_purge_status.value,
        "remote_revoke_status": row.remote_revoke_status.value,
        "retry_count": row.retry_count,
        "next_retry_at": _iso(row.next_retry_at),
        "last_remote_error": row.last_remote_error,
        "disconnected_at": _iso(row.disconnected_at),
        "local_completed_at": _iso(row.local_completed_at),
        "remote_completed_at": _iso(row.remote_completed_at),
    }


def _validate_provider_service(provider: str, service: str | None = None) -> tuple[str, str | None]:
    p = str(provider or "").strip().lower()
    if p not in _ALLOWED_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unsupported provider '{provider}'")
    if service is not None:
        s = str(service or "").strip().lower()
        if s not in _ALLOWED_SERVICES:
            raise HTTPException(status_code=404, detail=f"Unsupported service '{service}'")
        return p, s
    return p, None


def _build_callback_redirect_url(
    return_to: str,
    *,
    query_updates: dict[str, str],
    fragment_params: dict[str, str] | None = None,
) -> str:
    safe_target = sanitize_return_to(return_to)
    parts = urlsplit(safe_target)
    existing_q = dict(parse_qsl(parts.query, keep_blank_values=True))
    existing_q.update(query_updates)
    fragment = urlencode(fragment_params) if fragment_params else parts.fragment
    location = urlunsplit(
        ("", "", parts.path or "/", urlencode(existing_q), fragment)
    )
    if not validate_callback_location(location):
        fallback_parts = urlsplit(DEFAULT_INTEGRATIONS_RETURN_TO)
        fallback_q = dict(parse_qsl(fallback_parts.query, keep_blank_values=True))
        fallback_q.update(query_updates)
        location = urlunsplit(
            ("", "", fallback_parts.path, urlencode(fallback_q), fragment)
        )
    return location


def _use_secure_cookie(request: Request) -> bool:
    public_url = get_dalton_public_app_url().strip().lower()
    if public_url.startswith("https://"):
        return True
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip().lower()
    if forwarded_proto == "https":
        return True
    return str(request.url.scheme).lower() == "https"


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("")
async def list_personal_integrations_endpoint(
    session: CurrentIntegrationSession,
) -> JSONResponse:
    """List personal integration capabilities, readiness, connections, and purge/revoke ledgers (mode='manage')."""
    connections = await db.list_user_connections(session.user_id)
    recent_purges = await db.list_user_purge_ledgers(
        session.user_id, active_only=False, limit=20
    )
    active_purges = [
        p
        for p in recent_purges
        if p.local_purge_status == LocalPurgeStatus.RUNNING
        or p.remote_revoke_status
        in (RemoteRevokeStatus.PENDING, RemoteRevokeStatus.FAILED_RETRYABLE)
    ]
    has_existing = bool(connections or active_purges)
    has_any_history = bool(connections or recent_purges)
    require_personal_integration_capability(
        None, None, mode="manage", has_existing_records=has_any_history
    )

    active_caps = effective_capabilities()
    provider_summary = get_connector_providers_summary()

    connectors: list[dict[str, Any]] = []
    for spec in PERSONAL_INTEGRATION_CONNECTOR_SPECS:
        cap_enabled = CapabilityId(spec.capability_id) in active_caps
        p_info = provider_summary.get(spec.provider, {})
        connectors.append(
            {
                "provider": spec.provider,
                "service": spec.service,
                "capability_id": spec.capability_id,
                "batch": spec.batch,
                "enabled": cap_enabled,
                "ready": bool(p_info.get("ready")),
                "credentials_ready": bool(p_info.get("credentials_ready")),
                "langsmith_tracing_enabled": bool(
                    p_info.get("langsmith_tracing_enabled")
                ),
                "callback_url": build_integration_redirect_uri(spec.provider),
                "events_url": (
                    build_feishu_integration_events_url()
                    if spec.events_path is not None
                    else None
                ),
            }
        )

    return JSONResponse(
        status_code=200,
        content={
            "connectors": connectors,
            "connections": [_serialize_connection(c) for c in connections],
            "active_purges": [_serialize_purge_ledger(p) for p in active_purges],
            "recent_purges": [_serialize_purge_ledger(p) for p in recent_purges],
            "has_personal_integrations": has_existing,
        },
        headers=_NO_STORE_HEADERS,
    )


@router.post("/{provider}/{service}/authorize")
async def authorize_personal_integration_endpoint(
    provider: str,
    service: str,
    request: Request,
    session: CurrentIntegrationSession,
    body: AuthorizeIntegrationRequest | None = None,
) -> JSONResponse:
    """Start a PKCE OAuth authorization flow for ``(provider, service)`` (mode='connect')."""
    norm_provider, norm_service = _validate_provider_service(provider, service)
    assert norm_service is not None
    require_personal_integration_capability(
        norm_provider, norm_service, mode="connect"
    )
    req = body or AuthorizeIntegrationRequest()
    started = await start_oauth_authorization(
        user_id=session.user_id,
        provider=norm_provider,
        service=norm_service,
        auth_mode=session.auth_mode,
        session_id=session.session_id,
        user_email=session.user_email,
        switch_account=req.switch_account,
        allow_create_draft=req.allow_create_draft,
        allow_send_mail=req.allow_send_mail,
        return_to=req.return_to,
        revoker=get_router_remote_revoker() if norm_provider == "google" else None,
    )
    response = JSONResponse(
        status_code=200,
        content={
            "authorize_url": started.authorize_url,
            "provider": started.provider,
            "service": started.service,
            "intent": started.intent,
            "expires_in": started.expires_in,
        },
        headers=_NO_STORE_HEADERS,
    )
    if started.browser_binding_cookie:
        response.set_cookie(
            key=OAUTH_BROWSER_BINDING_COOKIE_NAME,
            value=started.browser_binding_cookie,
            max_age=OAUTH_STATE_TTL_SECONDS,
            path="/api/v1/dalton/integrations",
            httponly=True,
            samesite="lax",
            secure=_use_secure_cookie(request),
        )
    return response


@router.get("/{provider}/callback")
async def oauth_callback_personal_integration_endpoint(
    provider: str,
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    error_description: str | None = Query(default=None),
) -> Response:
    """Bearer-less top-level OAuth redirect callback (§4.2(1), §4.3, §6)."""
    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    norm_provider, _ = _validate_provider_service(provider)
    browser_binding_token = request.cookies.get(OAUTH_BROWSER_BINDING_COOKIE_NAME)

    result = await handle_oauth_callback(
        provider=norm_provider,
        code=code,
        state=state,
        error=error,
        error_description=error_description,
        browser_binding_token=browser_binding_token,
        google_client=get_router_google_client() if norm_provider == "google" else None,
        feishu_client=get_router_feishu_client() if norm_provider == "feishu" else None,
        revoker=get_router_remote_revoker() if norm_provider == "google" else None,
    )

    q_updates: dict[str, str] = {
        "integration_status": result.status,
        "provider": result.provider,
        "service": result.service,
    }
    if result.partial_grant:
        q_updates["partial_grant"] = "1"
    if result.error_code:
        q_updates["error_code"] = result.error_code

    frag: dict[str, str] | None = None
    if result.confirmation_token:
        frag = {"confirmation_token": result.confirmation_token}

    location = _build_callback_redirect_url(
        result.return_to,
        query_updates=q_updates,
        fragment_params=frag,
    )
    response = RedirectResponse(
        url=location,
        status_code=302,
        headers=_NO_STORE_HEADERS,
    )
    if browser_binding_token is not None:
        response.delete_cookie(
            key=OAUTH_BROWSER_BINDING_COOKIE_NAME,
            path="/api/v1/dalton/integrations",
            httponly=True,
            samesite="lax",
            secure=_use_secure_cookie(request),
        )
    return response


@router.post("/confirm")
async def confirm_personal_integration_endpoint(
    body: ConfirmIntegrationRequest,
    session: CurrentIntegrationSession,
) -> JSONResponse:
    """Confirm (or cancel) a staged Supabase or Google account-switch connection (§4.2(1))."""
    result = await confirm_pending_integration(
        user_id=session.user_id,
        confirmation_token=body.confirmation_token,
        confirm_account_switch=body.confirm_account_switch,
        cancel=body.cancel,
        revoker=get_router_remote_revoker(),
    )
    if result.status == "account_switch_confirmation_required":
        return JSONResponse(
            status_code=409,
            content={
                "code": "account_switch_confirmation_required",
                "status": result.status,
                "message": "换账号会断开旧账号并清除旧账号在对话中留下的个人数据，请确认后继续。",
                "provider": result.provider,
                "service": result.service,
                "previous_account_email": result.previous_account_email,
                "new_account_email": result.new_account_email,
            },
            headers=_NO_STORE_HEADERS,
        )
    if result.status == "cancelled":
        return JSONResponse(
            status_code=200,
            content={
                "status": "cancelled",
                "provider": result.provider,
                "service": result.service,
            },
            headers=_NO_STORE_HEADERS,
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": "connected",
            "provider": result.provider,
            "service": result.service,
            "connection": (
                _serialize_connection(result.connection)
                if result.connection is not None
                else None
            ),
            "purge_id": result.purge_id,
        },
        headers=_NO_STORE_HEADERS,
    )


@router.patch("/{provider}/{service}")
async def update_personal_integration_preferences_endpoint(
    provider: str,
    service: str,
    body: UpdatePreferencesRequest,
    session: CurrentIntegrationSession,
) -> JSONResponse:
    """Update preferences on an existing personal integration connection (mode='connect')."""
    norm_provider, norm_service = _validate_provider_service(provider, service)
    assert norm_service is not None
    require_personal_integration_capability(
        norm_provider, norm_service, mode="connect"
    )
    row = await db.get_connection(session.user_id, norm_provider, norm_service)
    if row is None:
        raise IntegrationNotFoundError("Integration connection not found.")

    patch = body.model_dump(exclude_none=True)
    granted_set = set(row.granted_scopes.split()) if row.granted_scopes else set()
    if patch.get("allow_create_draft") is True:
        if norm_provider != "google" or norm_service != "mail":
            raise PersonalIntegrationError(
                "allow_create_draft only applies to google/mail.",
                code="invalid_preference",
            )
        if not any(s in granted_set for s in GOOGLE_GMAIL_DRAFT_SCOPES):
            raise PersonalIntegrationError(
                "Current Gmail grant does not include draft creation scope; please re-authorize.",
                code="missing_required_scopes",
                status_code=400,
            )
    if patch.get("allow_send_mail") is True:
        if norm_provider != "feishu" or norm_service != "mail":
            raise PersonalIntegrationError(
                "allow_send_mail only applies to feishu/mail.",
                code="invalid_preference",
            )
        if FEISHU_MAIL_SEND_SCOPE not in granted_set:
            raise PersonalIntegrationError(
                "Current Feishu mail grant does not include send scope; please re-authorize.",
                code="missing_required_scopes",
                status_code=400,
            )

    merged = dict(row.preferences or {})
    for k, v in patch.items():
        if k in _ALLOWED_PREFERENCE_KEYS:
            merged[k] = v

    updated = await db.update_connection_preferences(
        session.user_id, norm_provider, norm_service, preferences=merged
    )
    if updated is None:
        raise IntegrationNotFoundError("Integration connection not found.")
    return JSONResponse(
        status_code=200,
        content={"connection": _serialize_connection(updated)},
        headers=_NO_STORE_HEADERS,
    )


@router.delete("/{provider}/{service}")
async def disconnect_personal_integration_service_endpoint(
    provider: str,
    service: str,
    session: CurrentIntegrationSession,
    mode: Literal["remote_if_sole", "local_only"] = Query(default="remote_if_sole"),
) -> JSONResponse:
    """Disconnect a single personal integration service and run Scheme 2A purge (mode='manage')."""
    norm_provider, norm_service = _validate_provider_service(provider, service)
    assert norm_service is not None
    has_existing = await db.has_user_any_integration_records(session.user_id)
    require_personal_integration_capability(
        norm_provider, norm_service, mode="manage", has_existing_records=has_existing
    )

    effective_mode = "local_only" if norm_provider == "feishu" else mode
    purge_id = await begin_disconnect(
        session.user_id,
        norm_provider,
        norm_service,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        remote_mode=effective_mode,
    )
    revoker = get_router_remote_revoker() if norm_provider == "google" else None
    finished = await finish_disconnect(purge_id, revoker=revoker)

    payload: dict[str, Any] = {
        "local_disconnected": True,
        "local_tokens_destroyed": True,
        "provider": norm_provider,
        "service": norm_service,
        "purge_id": purge_id,
        "local_purge_status": (
            finished.local_purge_status.value if finished is not None else "running"
        ),
        "remote_revoke_status": (
            finished.remote_revoke_status.value if finished is not None else "pending"
        ),
    }
    if norm_provider == "feishu":
        payload["remote_revocation_mode"] = "manual_or_ttl"
        payload["remote_access_token_max_ttl_seconds"] = 7200
    return JSONResponse(status_code=200, content=payload, headers=_NO_STORE_HEADERS)


@router.delete("/{provider}")
async def disconnect_personal_integration_provider_endpoint(
    provider: str,
    session: CurrentIntegrationSession,
    mode: Literal["remote_if_sole", "local_only"] = Query(default="remote_if_sole"),
) -> JSONResponse:
    """Disconnect all personal integration services for ``provider`` (mode='manage')."""
    norm_provider, _ = _validate_provider_service(provider)
    has_existing = await db.has_user_any_integration_records(session.user_id)
    require_personal_integration_capability(
        norm_provider, None, mode="manage", has_existing_records=has_existing
    )

    effective_mode = "local_only" if norm_provider == "feishu" else mode
    purge_ids = await begin_disconnect_all_services(
        session.user_id,
        norm_provider,
        trigger_reason=TriggerReason.USER_DISCONNECT,
        remote_mode=effective_mode,
    )
    # Complete ALL local 2A purges first so shared threads unblock immediately,
    # then progress Google remote revocations independently in the background (§4.2(3)).
    # If any ledger is currently held by another worker (finished is None or still running),
    # defer remote revocation to the due-revoke scanner so local purge always finishes first.
    results: list[dict[str, Any]] = []
    pending_remote_pids: list[str] = []
    all_local_completed = True
    for pid in purge_ids:
        finished = await finish_disconnect(
            pid,
            revoker=None,
            run_remote_revoke=False,
        )
        local_st = (
            finished.local_purge_status
            if finished is not None
            else LocalPurgeStatus.RUNNING
        )
        remote_st = (
            finished.remote_revoke_status
            if finished is not None
            else RemoteRevokeStatus.PENDING
        )
        if finished is None or local_st == LocalPurgeStatus.RUNNING:
            all_local_completed = False
        elif norm_provider == "google" and remote_st == RemoteRevokeStatus.PENDING:
            pending_remote_pids.append(pid)
        results.append(
            {
                "purge_id": pid,
                "local_purge_status": local_st.value,
                "remote_revoke_status": remote_st.value,
            }
        )

    if all_local_completed and pending_remote_pids and norm_provider == "google":
        schedule_background_remote_revokes(
            pending_remote_pids,
            get_router_remote_revoker(),
        )

    payload: dict[str, Any] = {
        "local_disconnected": True,
        "local_tokens_destroyed": True,
        "provider": norm_provider,
        "purges": results,
    }
    if norm_provider == "feishu":
        payload["remote_revocation_mode"] = "manual_or_ttl"
        payload["remote_access_token_max_ttl_seconds"] = 7200
    return JSONResponse(status_code=200, content=payload, headers=_NO_STORE_HEADERS)


@router.post("/purges/{purge_id}/retry-revoke")
async def retry_personal_integration_revoke_endpoint(
    purge_id: str,
    session: CurrentIntegrationSession,
) -> JSONResponse:
    """Manually retry a pending/retryable remote Google revocation owned by the caller (mode='manage')."""
    has_existing = await db.has_user_any_integration_records(session.user_id)
    require_personal_integration_capability(
        None, None, mode="manage", has_existing_records=has_existing
    )
    try:
        ledger = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
    except Exception:
        ledger = None
    if ledger is None or ledger.user_id != session.user_id:
        raise HTTPException(status_code=404, detail="Purge ledger not found")

    status_after = await process_revoke(purge_id, get_router_remote_revoker())
    refreshed = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
    return JSONResponse(
        status_code=200,
        content={
            "purge_id": purge_id,
            "remote_revoke_status": status_after.value,
            "ledger": _serialize_purge_ledger(refreshed) if refreshed is not None else None,
        },
        headers=_NO_STORE_HEADERS,
    )


@router.post("/feishu/events")
async def feishu_integration_events_endpoint(
    request: Request,
) -> JSONResponse:
    """Public Feishu event subscription endpoint for ``url_verification`` and ``auth.user_access_token.revoked_v4`` (§4.2(3), §8.3)."""
    require_personal_integration_capability(
        "feishu",
        None,
        mode="manage",
        allow_feishu_event_configured=True,
    )
    raw_body = await request.body()
    payload = verify_and_decode_feishu_event(
        raw_body=raw_body,
        headers=request.headers,
    )
    result = await process_feishu_integration_event(payload, execute_purge=True)
    if result.status == "challenge":
        return JSONResponse(
            status_code=200,
            content={"challenge": result.challenge or ""},
            headers=_NO_STORE_HEADERS,
        )
    return JSONResponse(
        status_code=200,
        content={
            "status": result.status,
            "event_id": result.event_id,
            "reason": result.reason,
            "purge_ids": list(result.purge_ids),
        },
        headers=_NO_STORE_HEADERS,
    )


@router.post(
    "/internal/feishu/events",
    dependencies=[Depends(require_channel_service_token)],
)
async def feishu_integration_internal_events_endpoint(
    request: Request,
) -> JSONResponse:
    """Internal service-token endpoint receiving ``auth.user_access_token.revoked_v4`` forwarded from dalton-channel-gateway (§4.2(3), §4.5(3))."""
    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    try:
        raw_payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={"code": "invalid_json", "message": "Invalid JSON body."},
            headers=_NO_STORE_HEADERS,
        )
    if not isinstance(raw_payload, dict):
        return JSONResponse(
            status_code=400,
            content={"code": "invalid_json", "message": "Event body must be a JSON object."},
            headers=_NO_STORE_HEADERS,
        )
    normalized = normalize_gateway_feishu_event(raw_payload)
    active_caps = effective_capabilities()
    has_matching_conn = False
    if not any(cap in active_caps for cap in PERSONAL_INTEGRATION_CAPABILITIES):
        has_matching_conn = await has_matching_feishu_event_connection(normalized)
    require_personal_integration_capability(
        "feishu",
        None,
        mode="manage",
        has_existing_records=has_matching_conn,
        allow_feishu_event_configured=True,
    )
    result = await process_feishu_integration_event(normalized, execute_purge=True)
    return JSONResponse(
        status_code=200,
        content={
            "status": result.status,
            "event_id": result.event_id,
            "reason": result.reason,
            "purge_ids": list(result.purge_ids),
        },
        headers=_NO_STORE_HEADERS,
    )


__all__ = [
    "AuthorizeIntegrationRequest",
    "ConfirmIntegrationRequest",
    "IntegrationSessionContext",
    "UpdatePreferencesRequest",
    "get_router_feishu_client",
    "get_router_google_client",
    "get_router_remote_revoker",
    "require_integration_web_session",
    "router",
]
