"""Backend gate for dedicated earnings tracker calendar (``POST /api/v1/dalton/mcp/tracker-calendar``).

Authenticates via ``X-Dalton-Bridge-Token`` and independently verifies the raw
``X-Dalton-Caller-Token`` HMAC signature (§5.3). Never logs tokens or event bodies.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from src.dalton.capabilities import (
    CapabilityId,
    require_capability,
    require_capability_dep,
)
from src.dalton.integrations.google_calendar import (
    GoogleCalendarClientProtocol,
    get_google_calendar_client,
)
from src.dalton.integrations.oauth_clients import (
    GoogleOAuthClientProtocol,
    get_google_oauth_client,
)
from src.dalton.integrations.tracker_calendar import (
    TRACKER_ALLOWED_OPERATIONS,
    authorize_tracker_calendar_execution,
    execute_tracker_calendar_legacy_gws,
    execute_tracker_calendar_official_mcp,
    register_default_tracker_purge_handler,
)
from src.dalton.sandbox_caller_identity import verify_caller_token
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_gcal_backend,
    get_tracker_calendar_owner_id,
    is_dalton_enabled,
)

logger = logging.getLogger(__name__)

# Register the Step 4 tracker calendar purge handler on router module load.
register_default_tracker_purge_handler()

_NO_STORE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

router = APIRouter(
    prefix="/api/v1/dalton/mcp",
    tags=["Dalton Tracker Calendar"],
    dependencies=[Depends(require_capability_dep(CapabilityId.FUND_AUTOMATIONS))],
)


class TrackerCalendarRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=25.0, ge=1.0, le=300.0)


def get_router_tracker_google_client() -> GoogleCalendarClientProtocol:
    return get_google_calendar_client()


def get_router_tracker_oauth_client() -> GoogleOAuthClientProtocol:
    return get_google_oauth_client()


def get_router_legacy_gws_http_client() -> Any:
    return None


def _verify_bridge_token(request: Request) -> None:
    expected = get_dalton_bridge_admin_token()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "bridge_token_not_configured",
                "message": "DALTON_BRIDGE_ADMIN_TOKEN is not configured.",
            },
        )
    supplied = str(request.headers.get("x-dalton-bridge-token") or "").strip()
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_bridge_token",
                "message": "Invalid or missing bridge token.",
            },
        )


def _verify_raw_caller_token(request: Request) -> dict[str, str]:
    scope = str(request.headers.get("x-dalton-caller-scope") or "").strip().lower()
    raw_token = str(request.headers.get("x-dalton-caller-token") or "").strip()
    if scope != "per-request" or not raw_token:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_caller_token",
                "message": "Valid per-request X-Dalton-Caller-Token is required.",
            },
        )
    claims = verify_caller_token(raw_token)
    if claims is None:
        raise HTTPException(
            status_code=401,
            detail={
                "code": "invalid_caller_token",
                "message": "Caller token signature is invalid or expired.",
            },
        )
    return claims


@router.post("/tracker-calendar")
async def tracker_calendar_mcp_endpoint(
    body: TrackerCalendarRequest,
    request: Request,
) -> JSONResponse:
    """Server-side gate for Richard earnings tracker calendar calls (§5.3)."""
    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    require_capability(CapabilityId.FUND_AUTOMATIONS)

    _verify_bridge_token(request)
    claims = _verify_raw_caller_token(request)

    operation = str(body.operation or "").strip()
    if operation not in TRACKER_ALLOWED_OPERATIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_operation",
                "message": "Unsupported tracker calendar operation.",
            },
        )

    owner_id = get_tracker_calendar_owner_id()
    if not owner_id:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "tracker_owner_not_configured",
                "message": "DALTON_TRACKER_CALENDAR_OWNER_ID is not configured.",
            },
        )
    if claims["user"].strip().lower() != owner_id.lower():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "tracker_owner_mismatch",
                "message": "Caller is not the configured tracker calendar owner.",
            },
        )

    if operation != "health_check":
        if claims["run_origin"] != "automation":
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "not_automation_run",
                    "message": "Tracker calendar operations require an active automation run.",
                },
            )
        authz = await authorize_tracker_calendar_execution(
            user_id=claims["user"],
            workspace_id=claims["workspace"],
            thread_id=claims["thread"],
            run_id=claims["run_id"],
            automation_id=claims["turn_origin_automation_id"],
            execution_id=claims["automation_execution_id"],
        )
        if authz.get("ok") is not True:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": str(authz.get("code") or "execution_not_authorized"),
                    "message": "Caller execution identity is not an active tracker automation run.",
                },
            )

    try:
        backend_mode = get_dalton_gcal_backend()
    except ValueError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "invalid_gcal_backend",
                "message": "DALTON_GCAL_BACKEND has an unsupported value.",
            },
        ) from None

    if backend_mode == "official_mcp":
        payload, calendar_id = await execute_tracker_calendar_official_mcp(
            owner_id=owner_id,
            operation=operation,
            arguments=body.arguments,
            google_client=get_router_tracker_google_client(),
            oauth_client=get_router_tracker_oauth_client(),
        )
    else:
        payload, calendar_id = await execute_tracker_calendar_legacy_gws(
            operation=operation,
            arguments=body.arguments,
            eff_timeout=body.timeout_s,
            http_client=get_router_legacy_gws_http_client(),
        )

    return JSONResponse(
        status_code=200,
        content={"payload": payload, "calendar_id": calendar_id},
        headers=_NO_STORE_HEADERS,
    )


__all__ = [
    "TrackerCalendarRequest",
    "get_router_legacy_gws_http_client",
    "get_router_tracker_google_client",
    "get_router_tracker_oauth_client",
    "router",
    "tracker_calendar_mcp_endpoint",
]
