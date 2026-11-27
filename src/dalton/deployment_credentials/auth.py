"""Interactive web-session administrator guard for deployment settings routes (§3.1).

Accepts exclusively verifiable web login sessions (``DALTON_AUTH_MODE=local`` or
``supabase``) belonging to an administrator:
- Requests carrying ``X-Service-Token`` or non-web-session tokens (computer CLI
  ``lac1_*`` / ``lac1.*`` tokens, PATs, OAuth resource tokens, service callers,
  company system identities, or team credential holder accounts) receive
  ``403 session_auth_required``.
- Passwordless OSS loopback mode (``DALTON_AUTH_MODE=oss``) receives
  ``403 login_mode_required``.
- ``local`` mode reuses ``dispatch_oss_user_id`` (HS256 JWT + active
  ``dalton_local_sessions`` verification) followed by ``require_catalog_admin_async``.
- ``supabase`` mode reuses ``verify_dalton_bearer`` and verifies ``user_id`` is in
  ``DALTON_PM_USER_IDS``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config.settings import LOCAL_DEV_USER_ID
from src.dalton.auth import verify_dalton_bearer
from src.dalton.auth.dispatch import dispatch_oss_user_id
from src.dalton.company_identity import is_company_system_user
from src.dalton.credentials import require_catalog_admin_async
from src.dalton.settings import get_configured_pm_user_ids, get_dalton_auth_mode
from src.server.auth import oauth_resource, pats
from src.server.database.shared_workspaces import is_holder
from src.server.services import cli_tokens
from src.server.utils.api import caller_of, note_caller

_optional_bearer = HTTPBearer(auto_error=False)


def _forbid(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code, "message": message})


def _is_non_web_session_token(raw_token: str) -> bool:
    if not raw_token:
        return False
    if cli_tokens.is_cli_token(raw_token) or raw_token.startswith(("lac1_", "lac1.")):
        return True
    if pats.is_pat(raw_token):
        return True
    if oauth_resource.from_resource_issuer(raw_token):
        return True
    return False


async def require_deployment_admin_session(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> str:
    """FastAPI dependency enforcing an authenticated interactive web administrator session."""
    if "x-service-token" in request.headers:
        raise _forbid(
            "session_auth_required",
            "Interactive web session authentication is required.",
        )

    raw_token = credentials.credentials if credentials is not None else ""
    if _is_non_web_session_token(raw_token):
        raise _forbid(
            "session_auth_required",
            "Interactive web session authentication is required.",
        )

    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        prior_caller = caller_of(scope)
        if prior_caller is not None and prior_caller.auth != "session":
            raise _forbid(
                "session_auth_required",
                "Interactive web session authentication is required.",
            )

    mode = get_dalton_auth_mode()
    if mode == "oss":
        raise _forbid(
            "login_mode_required",
            "Administrator login mode (DALTON_AUTH_MODE=local or supabase) is required.",
        )

    if mode == "local":
        user_id = await dispatch_oss_user_id(
            request,
            credentials,
            local_dev_user_id=LOCAL_DEV_USER_ID,
        )
        if isinstance(scope, dict):
            caller = caller_of(scope)
            if caller is not None and caller.auth != "session":
                raise _forbid(
                    "session_auth_required",
                    "Interactive web session authentication is required.",
                )
        if is_company_system_user(user_id) or is_holder(user_id):
            raise _forbid(
                "session_auth_required",
                "Interactive web session authentication is required.",
            )
        await require_catalog_admin_async(user_id)
        return user_id

    if mode == "supabase":
        resolved = await verify_dalton_bearer(
            credentials,
            missing_detail="Missing authentication",
        )
        user_id = resolved.user_id
        if is_company_system_user(user_id) or is_holder(user_id):
            raise _forbid(
                "session_auth_required",
                "Interactive web session authentication is required.",
            )
        note_caller(request, user_id, "session")
        if user_id not in set(get_configured_pm_user_ids()):
            raise _forbid(
                "admin_required",
                "Administrator privileges are required.",
            )
        return user_id

    raise HTTPException(
        status_code=503,
        detail={"code": "invalid_auth_mode", "message": "Invalid DALTON_AUTH_MODE configuration."},
    )


AdminSessionUserId = Annotated[str, Depends(require_deployment_admin_session)]

__all__ = [
    "AdminSessionUserId",
    "require_deployment_admin_session",
]
