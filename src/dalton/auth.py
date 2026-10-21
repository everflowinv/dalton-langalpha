"""Dalton Supabase authentication and email allowlist enforcement for OSS mode.

Keeps ``HOST_MODE=oss`` intact (preserving LocalBudgetPort, ``.env`` provider keys,
and non-commercial behavior) while verifying Supabase Bearer JWTs via upstream
``_decode_token`` and enforcing ``DALTON_ALLOWED_EMAILS`` (fail-closed when empty).
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
import uuid
from typing import Any

import httpx
import jwt
from fastapi import HTTPException, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials

from src.dalton.settings import (
    get_dalton_allowed_emails,
    get_supabase_publishable_key,
    is_dalton_supabase_auth_enabled,
)
from src.server.auth import jwt_bearer
from src.server.auth.jwt_bearer import AuthInfo

logger = logging.getLogger(__name__)

_AUTH_SETTINGS_SUCCESS_TTL_S = 300.0
_AUTH_SETTINGS_FAILURE_TTL_S = 30.0
MIN_SERVICE_TOKEN_LEN = 32

# Per-worker cache: (supabase_url, publishable_key, is_ready, expires_at_monotonic)
_auth_settings_cache: tuple[str, str, bool, float] | None = None
_auth_settings_lock = asyncio.Lock()
_verified_token_identity: contextvars.ContextVar[tuple[str, str] | None] = (
    contextvars.ContextVar("_verified_token_identity", default=None)
)


def _is_valid_uuid(value: str) -> bool:
    """Return True when ``value`` is a canonical UUID string."""
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


async def _fetch_supabase_auth_settings(
    supabase_url: str, publishable_key: str
) -> dict[str, Any]:
    """Fetch ``${SUPABASE_URL}/auth/v1/settings`` asynchronously with ``SUPABASE_PUBLISHABLE_KEY``."""
    url = f"{supabase_url.rstrip('/')}/auth/v1/settings"
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            url,
            headers={
                "apikey": publishable_key,
                "Authorization": f"Bearer {publishable_key}",
            },
        )
        resp.raise_for_status()
        data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Invalid Supabase auth settings payload")
    return data


async def _check_supabase_signup_disabled(
    supabase_url: str,
    target_logger: logging.Logger | None = None,
) -> bool:
    """Verify via ``${SUPABASE_URL}/auth/v1/settings`` that signup, autoconfirm, and anonymous users are disabled."""
    global _auth_settings_cache
    log = target_logger or logger
    clean_url = supabase_url.strip().rstrip("/")
    publishable_key = get_supabase_publishable_key()
    if not publishable_key:
        _auth_settings_cache = (
            clean_url,
            "",
            False,
            time.monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: SUPABASE_PUBLISHABLE_KEY 为空，"
            "无法请求 %s/auth/v1/settings 核验认证设置，所有请求将被拒绝 (fail-closed)。",
            clean_url,
        )
        return False

    try:
        settings_data = await _fetch_supabase_auth_settings(clean_url, publishable_key)
    except Exception as exc:
        _auth_settings_cache = (
            clean_url,
            publishable_key,
            False,
            time.monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: 请求 %s/auth/v1/settings 失败 (%s)，"
            "所有请求将被拒绝 (fail-closed)。",
            clean_url,
            exc,
        )
        return False

    if settings_data.get("disable_signup") is not True:
        _auth_settings_cache = (
            clean_url,
            publishable_key,
            False,
            time.monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: %s/auth/v1/settings 中 disable_signup=%r "
            "(必须为 true)，所有请求将被拒绝 (fail-closed)。",
            clean_url,
            settings_data.get("disable_signup"),
        )
        return False

    if settings_data.get("mailer_autoconfirm") is not False:
        _auth_settings_cache = (
            clean_url,
            publishable_key,
            False,
            time.monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: %s/auth/v1/settings 中 mailer_autoconfirm=%r "
            "(必须为 false)，所有请求将被拒绝 (fail-closed)。",
            clean_url,
            settings_data.get("mailer_autoconfirm"),
        )
        return False

    external = settings_data.get("external")
    anon_users = external.get("anonymous_users") if isinstance(external, dict) else None
    anon_enabled = (
        not isinstance(external, dict)
        or anon_users is True
        or (
            isinstance(anon_users, str)
            and anon_users.strip().lower() not in ("", "false", "0")
        )
        or (not isinstance(anon_users, (bool, str, type(None))) and bool(anon_users))
    )
    if anon_enabled:
        _auth_settings_cache = (
            clean_url,
            publishable_key,
            False,
            time.monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: %s/auth/v1/settings 中 external.anonymous_users=%r "
            "(不能为真)，所有请求将被拒绝 (fail-closed)。",
            clean_url,
            anon_users,
        )
        return False

    _auth_settings_cache = (
        clean_url,
        publishable_key,
        True,
        time.monotonic() + _AUTH_SETTINGS_SUCCESS_TTL_S,
    )
    return True


async def _ensure_dalton_auth_ready() -> None:
    """Fail-closed check for ``SUPABASE_URL`` and Supabase ``/auth/v1/settings`` with per-worker TTL cache."""
    from src.config import settings as app_settings

    supabase_url = (app_settings.SUPABASE_URL or "").strip().rstrip("/")
    if not supabase_url:
        raise HTTPException(status_code=503, detail="认证配置错误")

    publishable_key = get_supabase_publishable_key()
    now = time.monotonic()
    cached = _auth_settings_cache
    if (
        cached is not None
        and cached[0] == supabase_url
        and cached[1] == publishable_key
        and now < cached[3]
    ):
        if not cached[2]:
            raise HTTPException(status_code=503, detail="认证配置错误")
        return

    async with _auth_settings_lock:
        now = time.monotonic()
        cached = _auth_settings_cache
        if (
            cached is not None
            and cached[0] == supabase_url
            and cached[1] == publishable_key
            and now < cached[3]
        ):
            if not cached[2]:
                raise HTTPException(status_code=503, detail="认证配置错误")
            return

        if not await _check_supabase_signup_disabled(supabase_url, logger):
            raise HTTPException(status_code=503, detail="认证配置错误")


def _extract_token_claims(token: str) -> dict[str, Any]:
    """Decode payload claims from a JWT already verified by ``_decode_token``."""
    try:
        claims = jwt.decode(
            token,
            options={
                "verify_signature": False,
                "verify_aud": False,
                "verify_exp": False,
            },
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    if not isinstance(claims, dict):
        raise HTTPException(status_code=401, detail="Invalid token")
    return claims


async def verify_dalton_token(token: str) -> AuthInfo:
    """Verify a Supabase JWT via upstream ``_decode_token`` and check ``DALTON_ALLOWED_EMAILS``.

    - Raises 503 when auth configuration is invalid or ``/auth/v1/settings`` check fails.
    - Raises 401 when the token is missing, invalid, or unverified ``sub`` != ``auth_info.user_id``.
    - Raises 403 when ``is_anonymous == true``, allowlist is empty, or ``email`` is not allowlisted.
    - Never uses ``user_metadata.email_verified`` (user-mutable).
    """
    _verified_token_identity.set(None)
    await _ensure_dalton_auth_ready()

    if not token or not token.strip():
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    auth_info = await jwt_bearer._decode_token(token)
    claims = _extract_token_claims(token)

    claim_sub = claims.get("sub")
    if not isinstance(claim_sub, str) or not claim_sub.strip() or claim_sub != auth_info.user_id:
        raise HTTPException(status_code=401, detail="Token sub claim mismatch")

    is_anonymous = claims.get("is_anonymous")
    if is_anonymous is True or (
        isinstance(is_anonymous, str) and is_anonymous.strip().lower() == "true"
    ):
        raise HTTPException(status_code=403, detail="Anonymous accounts are not allowed")

    raw_email = claims.get("email")
    email = raw_email.strip().lower() if isinstance(raw_email, str) else ""
    allowed_emails = get_dalton_allowed_emails()

    if not allowed_emails:
        logger.warning(
            "Dalton Supabase auth rejected request (allowlist is empty, fail-closed): email=%s",
            email or "<missing>",
        )
        raise HTTPException(status_code=403, detail="Email is not on the Dalton allowlist")

    if not email or email not in allowed_emails:
        logger.warning(
            "Dalton Supabase auth rejected email not in allowlist: email=%s",
            email or "<missing>",
        )
        raise HTTPException(status_code=403, detail="Email is not on the Dalton allowlist")

    object.__setattr__(auth_info, "email", email)
    _verified_token_identity.set((auth_info.user_id, email))
    return auth_info


async def verify_dalton_bearer(
    credentials: HTTPAuthorizationCredentials | None,
    *,
    missing_detail: str = "Missing Bearer token",
) -> AuthInfo:
    """Verify HTTP Bearer credentials under Dalton Supabase auth mode."""
    _verified_token_identity.set(None)
    await _ensure_dalton_auth_ready()
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail=missing_detail)
    return await verify_dalton_token(credentials.credentials)


async def verify_dalton_optional_bearer(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Verify optional HTTP Bearer credentials; returns ``None`` when absent without remote readiness check."""
    if credentials is None or not credentials.credentials:
        return None
    return (await verify_dalton_token(credentials.credentials)).user_id


async def authenticate_dalton_websocket(websocket: WebSocket) -> str:
    """Verify WebSocket handshake token + allowlist under Dalton Supabase auth mode."""
    try:
        await _ensure_dalton_auth_ready()
    except HTTPException as exc:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason=str(exc.detail)
        )
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason=str(exc.detail)
        ) from exc

    token: str | None = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    if not token:
        token = websocket.query_params.get("token")

    if not token:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Missing auth token"
        )
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="Missing auth token"
        )

    try:
        auth_info = await verify_dalton_token(token)
        return auth_info.user_id
    except HTTPException as exc:
        reason = (
            "Email is not on the Dalton allowlist"
            if exc.status_code == 403
            else "Invalid or expired token"
        )
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason)
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason=reason
        ) from exc
    except Exception as exc:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token"
        )
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token"
        ) from exc


def resolve_dalton_sync_email(
    auth_info: AuthInfo,
    body_email: str | None,
) -> tuple[str | None, bool]:
    """Resolve ``(effective_email, allow_legacy_email_migration)`` for ``POST /auth/sync``.

    When Dalton Supabase auth is disabled, preserves upstream ``(body_email, True)``.
    When enabled:
    - Uses only the verified token email attached to ``auth_info``.
    - Rejects a non-empty ``body_email`` that does not match the token email with HTTP 400.
    - Disables legacy email-based PK migration (``allow_legacy_email_migration=False``).
    """
    if not is_dalton_supabase_auth_enabled():
        return body_email, True

    raw_token_email = getattr(auth_info, "email", None)
    token_email = (
        raw_token_email.strip().lower()
        if isinstance(raw_token_email, str) and raw_token_email.strip()
        else ""
    )
    if not token_email:
        raise HTTPException(status_code=401, detail="Token missing verified email claim")

    if isinstance(body_email, str) and body_email.strip():
        if body_email.strip().lower() != token_email:
            raise HTTPException(
                status_code=400,
                detail="Request email does not match authenticated token email",
            )

    return token_email, False


def resolve_dalton_create_user_email(
    user_id: str,
    body_email: str | None,
) -> str | None:
    """Resolve effective email for ``POST /users``.

    When Dalton Supabase auth is disabled, preserves upstream ``body_email``.
    When enabled, ignores ``body_email`` and uses only the verified token email
    from the current request context when its subject matches ``user_id``.
    """
    if not is_dalton_supabase_auth_enabled():
        return body_email

    ident = _verified_token_identity.get()
    if ident is not None and ident[0] == user_id and ident[1]:
        return ident[1]
    return None


def is_dalton_service_token_valid(configured_token: str | None) -> bool:
    """Whether ``INTERNAL_SERVICE_TOKEN`` is non-empty and at least 32 characters."""
    return bool(
        isinstance(configured_token, str)
        and len(configured_token.strip()) >= MIN_SERVICE_TOKEN_LEN
    )


def is_dalton_service_request(sent_token: str, configured_token: str) -> bool:
    """Verify server self-dispatch under Dalton auth mode: requires >=32-char matching service token."""
    from src.server.utils.api import service_token_matches

    if not is_dalton_service_token_valid(configured_token):
        return False
    return service_token_matches(sent_token, configured_token)


async def verify_workspace_access(workspace_id: str, user_id: str) -> dict[str, Any]:
    """Verify that ``workspace_id`` exists and belongs to ``user_id`` fail-closed via ``get_workspace``.

    - Raises HTTP 404 when ``workspace_id`` is empty/``home`` or the workspace row does not exist.
    - Raises HTTP 403 when ``user_id`` is empty or ``workspace.user_id != user_id``.
    - Raises HTTP 503 (fail-closed) when querying the workspace table raises an exception.
    """
    cleaned_ws = str(workspace_id or "").strip()
    cleaned_user = str(user_id or "").strip()
    if not cleaned_ws or cleaned_ws.lower() == "home":
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{workspace_id}' not found",
        )
    if not cleaned_user:
        raise HTTPException(
            status_code=403,
            detail="Workspace does not belong to current user",
        )
    try:
        from src.server.database.workspace import get_workspace

        ws = await get_workspace(cleaned_ws)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Database error verifying workspace access for workspace_id=%s: %s",
            workspace_id,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="Database unavailable while verifying workspace access",
        ) from exc
    if ws is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{workspace_id}' not found",
        )
    owner = str(ws.get("user_id") or "").strip()
    if not owner or owner != cleaned_user:
        raise HTTPException(
            status_code=403,
            detail="Workspace does not belong to current user",
        )
    return ws


async def resolve_caller_home_workspace_id(user_id: str) -> str:
    """Resolve the caller's own Home workspace ID fail-closed via ``get_or_create_home_workspace``."""
    cleaned_user = str(user_id or "").strip()
    if not cleaned_user:
        raise HTTPException(
            status_code=403,
            detail="user_id is required to resolve Home workspace",
        )
    try:
        from src.server.database.workspace import get_or_create_home_workspace

        ws = await get_or_create_home_workspace(cleaned_user)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Database error resolving Home workspace for user_id=%s: %s",
            cleaned_user,
            exc,
        )
        raise HTTPException(
            status_code=503,
            detail="Database unavailable while resolving Home workspace",
        ) from exc
    ws_id = str((ws or {}).get("workspace_id") or "").strip()
    owner = str((ws or {}).get("user_id") or "").strip()
    if not ws_id or not owner or owner != cleaned_user:
        raise HTTPException(
            status_code=403,
            detail="Resolved Home workspace does not belong to current user",
        )
    return ws_id


async def log_dalton_auth_startup_if_enabled(
    target_logger: logging.Logger | None = None,
) -> bool:
    """Log Dalton Supabase auth startup status; returns True when Dalton auth is active."""
    global _auth_settings_cache
    if not is_dalton_supabase_auth_enabled():
        return False

    from src.config import settings as app_settings

    log = target_logger or logger
    _auth_settings_cache = None
    has_error = False

    supabase_url = (app_settings.SUPABASE_URL or "").strip()
    if not supabase_url:
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: SUPABASE_URL 为空，"
            "认证配置错误，所有请求将返回 503 (fail-closed)。"
        )
        has_error = True
    else:
        if not await _check_supabase_signup_disabled(supabase_url, log):
            has_error = True

    svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
    if not is_dalton_service_token_valid(svc_token):
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: INTERNAL_SERVICE_TOKEN 为空或少于 %d 个字符 "
            "(len=%d)，所有服务令牌路径将一律不认。",
            MIN_SERVICE_TOKEN_LEN,
            len(svc_token),
        )
        has_error = True

    allowed = get_dalton_allowed_emails()
    if not allowed:
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: Dalton Supabase 认证已启用，"
            "但 DALTON_ALLOWED_EMAILS 为空，所有请求将被拒绝 (fail-closed)。"
        )
        has_error = True

    raw_pm = os.getenv("DALTON_PM_USER_IDS", "")
    pm_ids = [u.strip() for u in raw_pm.split(",") if u.strip()]
    if not pm_ids or any(not _is_valid_uuid(u) for u in pm_ids):
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: DALTON_PM_USER_IDS 为空、仅含默认开发账号 "
            "或含非 UUID 的值 (%r)，PM 专属接口将无法访问。",
            raw_pm,
        )
        has_error = True

    if not has_error:
        log.info(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: Dalton Supabase 认证已启用 "
            "(allowed_emails=%d, disable_signup=True)。",
            len(allowed),
        )
    return True


__all__ = [
    "MIN_SERVICE_TOKEN_LEN",
    "authenticate_dalton_websocket",
    "is_dalton_service_request",
    "is_dalton_service_token_valid",
    "is_dalton_supabase_auth_enabled",
    "log_dalton_auth_startup_if_enabled",
    "resolve_caller_home_workspace_id",
    "resolve_dalton_create_user_email",
    "resolve_dalton_sync_email",
    "verify_dalton_bearer",
    "verify_dalton_optional_bearer",
    "verify_dalton_token",
    "verify_workspace_access",
]
