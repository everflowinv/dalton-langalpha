"""FastAPI routers for local authentication and first-administrator bootstrap.

Routes (registered ONLY when ``DALTON_AUTH_MODE == "local"``):
- ``POST /api/v1/dalton/auth/local/login``
- ``POST /api/v1/dalton/auth/local/refresh`` (Origin-checked; clears cookie on actual 401 response)
- ``POST /api/v1/dalton/auth/local/logout`` (Origin-checked)
- ``POST /api/v1/dalton/auth/local/change-password``
- ``GET  /api/v1/dalton/bootstrap/status``
- ``POST /api/v1/dalton/bootstrap/admin``
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from src.dalton.auth.bootstrap import bootstrap_first_admin, get_bootstrap_status
from src.dalton.auth.dispatch import _read_local_session_from_request
from src.dalton.auth.sessions import (
    authenticate_local_login,
    change_local_password,
    extract_client_ip,
    refresh_local_session,
    revoke_session_for_logout,
)
from src.dalton.auth.tokens import (
    REFRESH_COOKIE_NAME,
    clear_refresh_cookie,
    issue_local_ws_ticket,
    set_refresh_cookie,
    verify_cookie_csrf_origin,
)
from src.dalton.settings import get_dalton_auth_mode
from src.server.utils.api import CurrentUserId


def _require_local_auth_mode() -> None:
    if get_dalton_auth_mode() != "local":
        raise HTTPException(status_code=404, detail="Local authentication is not enabled")


local_auth_router = APIRouter(
    prefix="/api/v1/dalton/auth/local",
    tags=["dalton-local-auth"],
    dependencies=[Depends(_require_local_auth_mode)],
)

bootstrap_router = APIRouter(
    prefix="/api/v1/dalton/bootstrap",
    tags=["dalton-bootstrap"],
    dependencies=[Depends(_require_local_auth_mode)],
)


class LocalLoginRequest(BaseModel):
    """Login body accepting ``identifier`` (or ``email`` / ``username``) plus ``password``."""

    identifier: str | None = Field(default=None, max_length=320)
    email: str | None = Field(default=None, max_length=320)
    username: str | None = Field(default=None, max_length=64)
    password: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _resolve_identifier(self) -> LocalLoginRequest:
        chosen = (
            (self.identifier or "").strip()
            or (self.email or "").strip()
            or (self.username or "").strip()
        )
        if not chosen:
            raise ValueError("One of identifier, email, or username is required")
        self.identifier = chosen
        return self


class LocalLogoutRequest(BaseModel):
    """Optional caller identity binding so a stale tab cannot revoke another account's cookie."""

    sub: str | None = Field(default=None, max_length=128)
    session_id: str | None = Field(default=None, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class BootstrapAdminRequest(BaseModel):
    bootstrap_token: str = Field(min_length=1, max_length=512)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)
    username: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=120)


@local_auth_router.post("/login")
async def local_login(
    body: LocalLoginRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    """Authenticate a local account via email or username and password."""
    result = await authenticate_local_login(
        identifier=body.identifier or "",
        password=body.password,
        client_ip=extract_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_refresh_cookie(response, request, result.refresh_token)
    return {
        "access_token": result.access_token,
        "token_type": "bearer",
        "expires_in": result.expires_in,
        "user": {
            "user_id": result.user_id,
            "email": result.email,
            "username": result.username,
            "tier": result.tier,
        },
    }


@local_auth_router.post("/refresh", response_model=None)
async def local_refresh(
    request: Request,
    response: Response,
) -> Response | dict[str, object]:
    """Rotate the refresh session cookie (same stable ``sid``) and return a new access token."""
    verify_cookie_csrf_origin(request)
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME, "")
    try:
        result = await refresh_local_session(
            raw_refresh_token=raw_refresh,
            client_ip=extract_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            # Attach cookie-clearing Set-Cookie directly onto the returned 401 JSONResponse.
            err_response = JSONResponse(
                status_code=401,
                content={"detail": exc.detail},
                headers=dict(exc.headers or {}),
            )
            clear_refresh_cookie(err_response, request)
            return err_response
        raise

    set_refresh_cookie(response, request, result.refresh_token)
    return {
        "access_token": result.access_token,
        "token_type": "bearer",
        "expires_in": result.expires_in,
    }


@local_auth_router.post("/logout", response_model=None)
async def local_logout(
    request: Request,
    response: Response,
    body: LocalLogoutRequest | None = Body(default=None),
) -> Response | dict[str, str]:
    """Revoke the current refresh session by stable ``sid`` and clear the refresh cookie.

    - Cookie session belongs to a different account/session than ``body.sub``/``body.session_id``:
      revokes NO session, leaves the active cookie intact, and returns 409.
    - Active session revoked (current or unexpired rotated history): clears cookie & returns 200.
    - Expired rotated historical token: revokes NO session, clears cookie & returns 200.
    - Missing/unknown token or already-revoked session: clears cookie & returns 401.
    """
    verify_cookie_csrf_origin(request)
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    outcome = await revoke_session_for_logout(
        raw_refresh,
        expected_user_id=(body.sub if body is not None else None),
        expected_session_id=(body.session_id if body is not None else None),
        reason="logout",
    )
    if outcome == "mismatch":
        return JSONResponse(
            status_code=409,
            content={"detail": "Active cookie session belongs to another account"},
        )
    if outcome == "not_found":
        err_response = JSONResponse(
            status_code=401,
            content={"detail": "Active session not found or already revoked"},
        )
        clear_refresh_cookie(err_response, request)
        return err_response
    clear_refresh_cookie(response, request)
    return {"status": "logged_out"}


@local_auth_router.post("/change-password")
async def local_change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    user_id: CurrentUserId,
) -> dict[str, object]:
    """Change the authenticated user's password and revoke all of their sessions."""
    revoked_count = await change_local_password(
        user_id=user_id,
        current_password=body.current_password,
        new_password=body.new_password,
        client_ip=extract_client_ip(request),
    )
    clear_refresh_cookie(response, request)
    return {
        "status": "password_changed",
        "sessions_revoked": revoked_count,
    }


@local_auth_router.post("/ws-ticket")
async def local_ws_ticket(
    request: Request,
    user_id: CurrentUserId,
) -> dict[str, object]:
    """Mint a 60-second HMAC-signed WebSocket handshake ticket bound to the caller's active session."""
    session_id = _read_local_session_from_request(request)
    if not session_id:
        raise HTTPException(
            status_code=401,
            detail="Active local session is required to issue a WebSocket ticket",
        )
    ticket, expires_in = issue_local_ws_ticket(
        user_id=user_id,
        session_id=session_id,
    )
    return {
        "ticket": ticket,
        "expires_in": expires_in,
    }


@bootstrap_router.get("/status")
async def bootstrap_status() -> dict[str, bool]:
    """Unauthenticated probe returning only whether initial admin bootstrap is needed."""
    return await get_bootstrap_status()


@bootstrap_router.post("/admin", status_code=201)
async def bootstrap_admin(
    body: BootstrapAdminRequest,
    request: Request,
    response: Response,
) -> dict[str, object]:
    """Create the initial administrator account and permanently close bootstrap."""
    result = await bootstrap_first_admin(
        bootstrap_token=body.bootstrap_token,
        email=body.email,
        password=body.password,
        username=body.username,
        name=body.name,
        client_ip=extract_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    set_refresh_cookie(response, request, result.refresh_token)
    return {
        "user": {
            "user_id": result.user_id,
            "email": result.email,
            "username": result.username,
            "name": result.name,
            "tier": result.tier,
        },
        "access_token": result.access_token,
        "token_type": "bearer",
        "expires_in": result.expires_in,
    }


def register_local_auth_routers(target_router: APIRouter) -> bool:
    """Include local auth & bootstrap routers on ``target_router`` ONLY in ``local`` mode."""
    if get_dalton_auth_mode() != "local":
        return False
    target_router.include_router(local_auth_router)
    target_router.include_router(bootstrap_router)
    return True


__all__ = [
    "BootstrapAdminRequest",
    "ChangePasswordRequest",
    "LocalLoginRequest",
    "LocalLogoutRequest",
    "bootstrap_router",
    "local_auth_router",
    "register_local_auth_routers",
]
