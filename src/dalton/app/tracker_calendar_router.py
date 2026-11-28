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
from src.dalton.database import user_integrations as integrations_db
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
    is_valid_tracker_automation_id,
    register_default_tracker_purge_handler,
)
from src.dalton.sandbox_caller_identity import verify_caller_token
from src.dalton.settings import (
    get_dalton_bridge_admin_token,
    get_dalton_gcal_backend,
    get_tracker_calendar_automation_id,
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

    configured_auto_id = get_tracker_calendar_automation_id()
    if backend_mode == "official_mcp" and not is_valid_tracker_automation_id(configured_auto_id):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "tracker_automation_not_configured",
                "message": "DALTON_TRACKER_CALENDAR_AUTOMATION_ID must be a valid UUID under official_mcp.",
            },
        )

    require_execution_authz = operation != "health_check" or bool(
        body.arguments.get("ack_reconcile")
    )
    if require_execution_authz:
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
            err_code = str(authz.get("code") or "execution_not_authorized")
            status_code = 503 if err_code == "tracker_automation_not_configured" else 403
            raise HTTPException(
                status_code=status_code,
                detail={
                    "code": err_code,
                    "message": (
                        "DALTON_TRACKER_CALENDAR_AUTOMATION_ID must be a valid UUID."
                        if err_code == "tracker_automation_not_configured"
                        else "Caller execution identity is not an active tracker automation run."
                    ),
                },
            )

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
        if (
            operation == "health_check"
            and bool(body.arguments.get("ack_reconcile"))
            and isinstance(payload, dict)
            and payload.get("ok") is True
        ):
            await integrations_db.clear_pending_tracker_reconcile(owner_id)
            hc_data = payload.get("data")
            if isinstance(hc_data, dict):
                hc_data["tracker_reconcile_cleared"] = True

    return JSONResponse(
        status_code=200,
        content={"payload": payload, "calendar_id": calendar_id},
        headers=_NO_STORE_HEADERS,
    )


@router.get("/tracker-calendar/reconcile-status")
async def tracker_calendar_reconcile_status_endpoint(
    request: Request,
) -> JSONResponse:
    """Read-only endpoint exposing PostgreSQL ``tracker_reconcile_needed`` to the weekly scan.

    Works independently of ``DALTON_GCAL_BACKEND`` (including default ``legacy_gws``)
    without altering normal ``legacy_gws`` read/write paths.
    """
    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    require_capability(CapabilityId.FUND_AUTOMATIONS)

    _verify_bridge_token(request)
    claims = _verify_raw_caller_token(request)

    try:
        backend_mode = get_dalton_gcal_backend()
    except ValueError:
        backend_mode = "invalid"

    owner_id = get_tracker_calendar_owner_id()
    if not owner_id:
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "owner_configured": False,
                "tracker_reconcile_needed": False,
                "backend": backend_mode,
            },
            headers=_NO_STORE_HEADERS,
        )

    if claims["user"].strip().lower() != owner_id.lower():
        raise HTTPException(
            status_code=403,
            detail={
                "code": "tracker_owner_mismatch",
                "message": "Caller is not the configured tracker calendar owner.",
            },
        )

    pending = await integrations_db.has_pending_tracker_reconcile(owner_id)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "owner_configured": True,
            "tracker_reconcile_needed": bool(pending),
            "backend": backend_mode,
        },
        headers=_NO_STORE_HEADERS,
    )


@router.post("/tracker-calendar/reconcile-ack")
async def tracker_calendar_reconcile_ack_endpoint(
    request: Request,
) -> JSONResponse:
    """Clear PostgreSQL ``tracker_reconcile_needed`` once the weekly scan completes reconciliation.

    Authorized via the same active automation execution check as ``/tracker-calendar`` and
    works under both ``legacy_gws`` and ``official_mcp``.
    """
    if not is_dalton_enabled():
        raise HTTPException(status_code=404, detail="Dalton mode disabled")
    require_capability(CapabilityId.FUND_AUTOMATIONS)

    _verify_bridge_token(request)
    claims = _verify_raw_caller_token(request)

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
    if claims["run_origin"] != "automation":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "not_automation_run",
                "message": "Tracker reconcile acknowledgement requires an active automation run.",
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
        err_code = str(authz.get("code") or "execution_not_authorized")
        status_code = 503 if err_code == "tracker_automation_not_configured" else 403
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": err_code,
                "message": (
                    "DALTON_TRACKER_CALENDAR_AUTOMATION_ID must be a valid UUID."
                    if err_code == "tracker_automation_not_configured"
                    else "Caller execution identity is not an active tracker automation run."
                ),
            },
        )

    cleared = await integrations_db.clear_pending_tracker_reconcile(owner_id)
    return JSONResponse(
        status_code=200,
        content={
            "ok": True,
            "tracker_reconcile_cleared": True,
            "cleared_count": int(cleared),
            "backend": backend_mode,
        },
        headers=_NO_STORE_HEADERS,
    )


__all__ = [
    "TrackerCalendarRequest",
    "get_router_legacy_gws_http_client",
    "get_router_tracker_google_client",
    "get_router_tracker_oauth_client",
    "router",
    "tracker_calendar_mcp_endpoint",
    "tracker_calendar_reconcile_ack_endpoint",
    "tracker_calendar_reconcile_status_endpoint",
]
