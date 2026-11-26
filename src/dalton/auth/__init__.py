"""Dalton authentication package (Supabase + standalone local account auth).

Preserves the full ``src.dalton.auth`` module surface for Everflow Supabase auth
and exports unified startup validation, local auth, roles, sessions, bootstrap,
and backend entry-point dispatch.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import time
import uuid
from typing import Any, NamedTuple

import httpx
import jwt
from fastapi import HTTPException, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials

from src.dalton.invites import is_team_member
from src.dalton.settings import (
    get_dalton_allowed_emails,
    get_dalton_auth_mode,
    get_supabase_publishable_key,
    is_dalton_local_auth_enabled,
    is_dalton_supabase_auth_enabled,
    validate_dalton_auth_config,
)
from src.server.auth import jwt_bearer
from src.server.auth.jwt_bearer import AuthInfo

logger = logging.getLogger(__name__)

_AUTH_SETTINGS_SUCCESS_TTL_S = 300.0
_AUTH_SETTINGS_STALE_MAX_AGE_S = 1800.0
_AUTH_SETTINGS_FAILURE_TTL_S = 30.0
_AUTH_SETTINGS_RETRY_TTL_S = 5.0
_AUTH_SETTINGS_RETRY_MAX_TTL_S = 30.0
_AUTH_SETTINGS_FETCH_TIMEOUT_S = 3.0
MIN_SERVICE_TOKEN_LEN = 32


class _AuthSettingsCache(NamedTuple):
    supabase_url: str
    publishable_key: str
    is_ready: bool
    expires_at: float
    last_success_at: float | None = None


class _AuthSettingsRetryState(NamedTuple):
    supabase_url: str
    publishable_key: str
    retry_at: float


def _monotonic() -> float:
    return time.monotonic()


async def _sleep(delay_s: float) -> None:
    await asyncio.sleep(delay_s)


# Per-worker auth decision cache (success or explicit policy refusal only).
_auth_settings_cache: _AuthSettingsCache | None = None
# Independent transient-failure retry deadline (kept separate from auth decision).
_auth_settings_retry: _AuthSettingsRetryState | None = None
_auth_settings_lock = asyncio.Lock()
_auth_settings_refresh_task: asyncio.Task[bool] | None = None
_auth_settings_refresh_key: tuple[str, str] | None = None
_auth_settings_retry_task: asyncio.Task[bool] | None = None
_auth_settings_retry_key: tuple[str, str] | None = None
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
    async with httpx.AsyncClient(timeout=_AUTH_SETTINGS_FETCH_TIMEOUT_S) as client:
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


def _cache_last_success_at(
    cached: _AuthSettingsCache | tuple[Any, ...] | None,
    supabase_url: str,
    publishable_key: str,
) -> float | None:
    if cached is None or len(cached) < 5:
        return None
    if cached[0] != supabase_url or cached[1] != publishable_key:
        return None
    last_ok = cached[4]
    return float(last_ok) if isinstance(last_ok, (int, float)) else None


def _cancel_background_auth_retry() -> None:
    global _auth_settings_retry_task, _auth_settings_retry_key
    task = _auth_settings_retry_task
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
    _auth_settings_retry_task = None
    _auth_settings_retry_key = None


async def _run_background_auth_retry(
    supabase_url: str, publishable_key: str, delay_s: float
) -> bool:
    global _auth_settings_retry, _auth_settings_retry_task, _auth_settings_retry_key
    attempt = 0
    next_delay = max(0.0, delay_s)
    try:
        while True:
            if next_delay > 0:
                await _sleep(next_delay)
            if get_supabase_publishable_key() != publishable_key:
                return False
            now = _monotonic()
            cached = _auth_settings_cache
            if (
                cached is not None
                and cached[0] == supabase_url
                and cached[1] == publishable_key
                and now < cached[3]
            ):
                return bool(cached[2])

            # Share the single-flight refresh task so incoming requests reuse this in-flight check.
            task, _created = _schedule_background_auth_refresh(
                supabase_url,
                publishable_key,
                schedule_retry_on_failure=False,
            )
            ok = await task
            if ok:
                return True

            # Explicit policy failure (e.g. disable_signup=False) caches a non-ready decision;
            # stop transient retries until that decision expires or config changes.
            cached_after = _auth_settings_cache
            if (
                cached_after is not None
                and cached_after[0] == supabase_url
                and cached_after[1] == publishable_key
                and not cached_after[2]
            ):
                return False
            if get_supabase_publishable_key() != publishable_key:
                return False

            attempt += 1
            next_delay = min(
                _AUTH_SETTINGS_RETRY_TTL_S * (2**attempt),
                _AUTH_SETTINGS_RETRY_MAX_TTL_S,
            )
            _auth_settings_retry = _AuthSettingsRetryState(
                supabase_url,
                publishable_key,
                _monotonic() + next_delay,
            )
    except asyncio.CancelledError:
        cached = _auth_settings_cache
        if (
            cached is not None
            and cached[0] == supabase_url
            and cached[1] == publishable_key
            and cached[2]
            and _monotonic() < cached[3]
        ):
            return True
        return False
    finally:
        if _auth_settings_retry_task is asyncio.current_task():
            _auth_settings_retry_task = None
            _auth_settings_retry_key = None


def _schedule_background_auth_retry(
    supabase_url: str, publishable_key: str, delay_s: float
) -> asyncio.Task[bool] | None:
    """Schedule one single-flight background retry after ``delay_s`` for transient failures."""
    global _auth_settings_retry_task, _auth_settings_retry_key
    key = (supabase_url, publishable_key)
    if (
        _auth_settings_retry_task is not None
        and not _auth_settings_retry_task.done()
        and _auth_settings_retry_key == key
    ):
        return _auth_settings_retry_task
    if _auth_settings_retry_task is not None and not _auth_settings_retry_task.done():
        _auth_settings_retry_task.cancel()
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    _auth_settings_retry_key = key
    _auth_settings_retry_task = asyncio.create_task(
        _run_background_auth_retry(supabase_url, publishable_key, max(0.0, delay_s))
    )
    return _auth_settings_retry_task


async def _check_supabase_signup_disabled(
    supabase_url: str,
    target_logger: logging.Logger | None = None,
    *,
    schedule_retry_on_failure: bool = True,
) -> bool:
    """Verify via ``${SUPABASE_URL}/auth/v1/settings`` that signup, autoconfirm, and anonymous users are disabled."""
    global _auth_settings_cache, _auth_settings_retry
    log = target_logger or logger
    clean_url = supabase_url.strip().rstrip("/")
    publishable_key = get_supabase_publishable_key()
    if not publishable_key:
        _auth_settings_retry = None
        if schedule_retry_on_failure:
            _cancel_background_auth_retry()
        _auth_settings_cache = _AuthSettingsCache(
            clean_url,
            "",
            False,
            _monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
            None,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: SUPABASE_PUBLISHABLE_KEY 为空，"
            "无法请求 %s/auth/v1/settings 核验认证设置，所有请求将被拒绝 (fail-closed)。",
            clean_url,
        )
        return False

    prev_success_at = _cache_last_success_at(
        _auth_settings_cache, clean_url, publishable_key
    )
    started = _monotonic()
    try:
        settings_data = await asyncio.wait_for(
            _fetch_supabase_auth_settings(clean_url, publishable_key),
            timeout=_AUTH_SETTINGS_FETCH_TIMEOUT_S,
        )
    except Exception as exc:
        finished = _monotonic()
        elapsed_ms = max(0.0, (finished - started) * 1000.0)
        if (
            prev_success_at is not None
            and (finished - prev_success_at) <= _AUTH_SETTINGS_STALE_MAX_AGE_S
        ):
            stale_age_s = max(0.0, finished - prev_success_at)
            next_expires_at = min(
                finished + _AUTH_SETTINGS_FAILURE_TTL_S,
                prev_success_at + _AUTH_SETTINGS_STALE_MAX_AGE_S,
            )
            _auth_settings_retry = None
            _auth_settings_cache = _AuthSettingsCache(
                clean_url,
                publishable_key,
                True,
                next_expires_at,
                prev_success_at,
            )
            log.warning(
                "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: 刷新 %s/auth/v1/settings 失败 "
                "(%s: %s, elapsed=%.1fms)，继续沿用上次成功结果 (age=%.1fs/%.0fs)。",
                clean_url,
                type(exc).__name__,
                exc,
                elapsed_ms,
                stale_age_s,
                _AUTH_SETTINGS_STALE_MAX_AGE_S,
            )
            return True

        # No valid cached success: keep auth decision empty (fail-closed) and record an
        # independent 5s retry deadline + single-flight background retry task.
        _auth_settings_cache = None
        _auth_settings_retry = _AuthSettingsRetryState(
            clean_url,
            publishable_key,
            finished + _AUTH_SETTINGS_RETRY_TTL_S,
        )
        if schedule_retry_on_failure:
            _schedule_background_auth_retry(
                clean_url, publishable_key, _AUTH_SETTINGS_RETRY_TTL_S
            )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: 请求 %s/auth/v1/settings 失败 "
            "(%s: %s, elapsed=%.1fms)，将在 %.0fs 后后台重试，当前请求拒绝 (fail-closed)。",
            clean_url,
            type(exc).__name__,
            exc,
            elapsed_ms,
            _AUTH_SETTINGS_RETRY_TTL_S,
        )
        return False

    if settings_data.get("disable_signup") is not True:
        _auth_settings_retry = None
        if schedule_retry_on_failure:
            _cancel_background_auth_retry()
        _auth_settings_cache = _AuthSettingsCache(
            clean_url,
            publishable_key,
            False,
            _monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
            None,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: %s/auth/v1/settings 中 disable_signup=%r "
            "(必须为 true)，所有请求将被拒绝 (fail-closed)。",
            clean_url,
            settings_data.get("disable_signup"),
        )
        return False

    if settings_data.get("mailer_autoconfirm") is not False:
        _auth_settings_retry = None
        if schedule_retry_on_failure:
            _cancel_background_auth_retry()
        _auth_settings_cache = _AuthSettingsCache(
            clean_url,
            publishable_key,
            False,
            _monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
            None,
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
        _auth_settings_retry = None
        if schedule_retry_on_failure:
            _cancel_background_auth_retry()
        _auth_settings_cache = _AuthSettingsCache(
            clean_url,
            publishable_key,
            False,
            _monotonic() + _AUTH_SETTINGS_FAILURE_TTL_S,
            None,
        )
        log.error(
            "HOST_MODE=oss, DALTON_AUTH_MODE=supabase: %s/auth/v1/settings 中 external.anonymous_users=%r "
            "(不能为真)，所有请求将被拒绝 (fail-closed)。",
            clean_url,
            anon_users,
        )
        return False

    ok_at = _monotonic()
    _auth_settings_retry = None
    if schedule_retry_on_failure:
        _cancel_background_auth_retry()
    _auth_settings_cache = _AuthSettingsCache(
        clean_url,
        publishable_key,
        True,
        ok_at + _AUTH_SETTINGS_SUCCESS_TTL_S,
        ok_at,
    )
    return True


async def _run_background_auth_refresh(
    supabase_url: str,
    publishable_key: str,
    *,
    schedule_retry_on_failure: bool = True,
) -> bool:
    global _auth_settings_refresh_task, _auth_settings_refresh_key
    try:
        async with _auth_settings_lock:
            now = _monotonic()
            cached = _auth_settings_cache
            if (
                cached is not None
                and cached[0] == supabase_url
                and cached[1] == publishable_key
                and now < cached[3]
            ):
                return bool(cached[2])
            if get_supabase_publishable_key() != publishable_key:
                return False
            return await _check_supabase_signup_disabled(
                supabase_url,
                logger,
                schedule_retry_on_failure=schedule_retry_on_failure,
            )
    finally:
        if _auth_settings_refresh_task is asyncio.current_task():
            _auth_settings_refresh_task = None
            _auth_settings_refresh_key = None


def _schedule_background_auth_refresh(
    supabase_url: str,
    publishable_key: str,
    *,
    schedule_retry_on_failure: bool = True,
) -> tuple[asyncio.Task[bool], bool]:
    """Start at most one background refresh for ``(supabase_url, publishable_key)``.

    Returns ``(task, created_new)`` so callers share one single-flight refresh.
    """
    global _auth_settings_refresh_task, _auth_settings_refresh_key
    key = (supabase_url, publishable_key)
    if (
        _auth_settings_refresh_task is not None
        and not _auth_settings_refresh_task.done()
        and _auth_settings_refresh_key == key
    ):
        return _auth_settings_refresh_task, False
    _auth_settings_refresh_key = key
    _auth_settings_refresh_task = asyncio.create_task(
        _run_background_auth_refresh(
            supabase_url,
            publishable_key,
            schedule_retry_on_failure=schedule_retry_on_failure,
        )
    )
    return _auth_settings_refresh_task, True


async def _ensure_dalton_auth_ready() -> None:
    """Fail-closed check for ``SUPABASE_URL`` and Supabase ``/auth/v1/settings`` with per-worker TTL cache."""
    from src.config import settings as app_settings

    supabase_url = (app_settings.SUPABASE_URL or "").strip().rstrip("/")
    if not supabase_url:
        raise HTTPException(status_code=503, detail="认证配置错误")

    publishable_key = get_supabase_publishable_key()
    now = _monotonic()
    cached = _auth_settings_cache
    if (
        cached is not None
        and cached[0] == supabase_url
        and cached[1] == publishable_key
    ):
        last_success_at = _cache_last_success_at(cached, supabase_url, publishable_key)
        within_stale_window = (
            last_success_at is not None
            and (now - last_success_at) <= _AUTH_SETTINGS_STALE_MAX_AGE_S
        )
        if now < cached[3]:
            if not cached[2]:
                raise HTTPException(status_code=503, detail="认证配置错误")
            if last_success_at is None or within_stale_window:
                return
        elif cached[2] and within_stale_window:
            _task, started_new = _schedule_background_auth_refresh(
                supabase_url, publishable_key
            )
            if started_new:
                # Let the newly created background task start immediately without blocking on I/O.
                await asyncio.sleep(0)
            return

    # If a single-flight refresh (including one launched by the background retry loop) is
    # already in flight for this (url, key), reuse it instead of returning a stale 503.
    key = (supabase_url, publishable_key)
    in_flight = (
        _auth_settings_refresh_task
        if (
            _auth_settings_refresh_task is not None
            and not _auth_settings_refresh_task.done()
            and _auth_settings_refresh_key == key
        )
        else None
    )
    if in_flight is not None:
        try:
            ok = await asyncio.wait_for(
                asyncio.shield(in_flight), timeout=_AUTH_SETTINGS_FETCH_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=503, detail="认证配置错误") from None
        if not ok:
            raise HTTPException(status_code=503, detail="认证配置错误")
        return

    # Independent transient-failure retry window (5s+ backoff): fail-closed without hammering
    # upstream, while ensuring the single-flight background retry loop is running.
    retry_state = _auth_settings_retry
    if (
        retry_state is not None
        and retry_state.supabase_url == supabase_url
        and retry_state.publishable_key == publishable_key
        and now < retry_state.retry_at
    ):
        _schedule_background_auth_retry(
            supabase_url, publishable_key, retry_state.retry_at - now
        )
        raise HTTPException(status_code=503, detail="认证配置错误")

    # Cold start, URL/key change, expired grace (>30m), or expired retry cooldown:
    # run a single-flight refresh task bounded by the short timeout (3s).
    task, _created = _schedule_background_auth_refresh(supabase_url, publishable_key)
    try:
        ok = await asyncio.wait_for(
            asyncio.shield(task), timeout=_AUTH_SETTINGS_FETCH_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=503, detail="认证配置错误") from None
    if not ok:
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

    from src.dalton.company_identity import (
        COMPANY_SYSTEM_EMAIL,
        reject_company_system_auth,
    )

    reject_company_system_auth(auth_info.user_id)

    is_anonymous = claims.get("is_anonymous")
    if is_anonymous is True or (
        isinstance(is_anonymous, str) and is_anonymous.strip().lower() == "true"
    ):
        raise HTTPException(status_code=403, detail="Anonymous accounts are not allowed")

    raw_email = claims.get("email")
    email = raw_email.strip().lower() if isinstance(raw_email, str) else ""
    if email == COMPANY_SYSTEM_EMAIL:
        raise HTTPException(
            status_code=401,
            detail="Company system identity cannot authenticate externally",
        )

    if not email or not await is_team_member(email):
        allowed_emails = get_dalton_allowed_emails()
        if not allowed_emails:
            logger.warning(
                "Dalton Supabase auth rejected request (allowlist is empty, fail-closed): email=%s",
                email or "<missing>",
            )
        else:
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


async def verify_workspace_access(
    workspace_id: str,
    user_id: str,
    need: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Verify that ``workspace_id`` exists and ``user_id`` has access fail-closed via ``get_workspace``.

    - Default (``need=None, channel=None``): strict owner check (raises 403 when ``workspace.user_id != user_id``).
    - When ``need`` ('read' | 'write') or ``channel`` ('cos' | 'web') is passed and Dalton Supabase auth is enabled,
      non-owner team members are checked against ``dalton_workspace_sharing`` via ``require_workspace_access``.
    - Raises HTTP 404 when ``workspace_id`` is empty/``home`` or the workspace row does not exist (or is ``hidden``).
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
    if owner and owner == cleaned_user:
        return ws

    from src.server.database.home_workspace import (
        is_home_stand_in_id,
        is_home_stand_in_row,
    )

    if is_home_stand_in_row(ws) or is_home_stand_in_id(owner, cleaned_ws):
        raise HTTPException(
            status_code=404,
            detail=f"Workspace '{workspace_id}' not found",
        )

    if (need is not None or channel is not None) and (
        is_dalton_supabase_auth_enabled() or is_dalton_local_auth_enabled()
    ):
        from src.dalton.sharing import require_workspace_access

        return await require_workspace_access(
            ws,
            cleaned_user,
            channel=channel or "cos",
            need=need or "read",
        )

    raise HTTPException(
        status_code=403,
        detail="Workspace does not belong to current user",
    )


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
    """Validate ``DALTON_AUTH_MODE`` at startup and log auth status.

    Returns True when Dalton auth (``supabase`` or ``local``) is active, False for ``oss``.
    Raises :class:`DaltonStartupConfigError` on invalid auth mode configuration.
    """
    global _auth_settings_cache, _auth_settings_retry, _auth_settings_refresh_task, _auth_settings_refresh_key
    validate_dalton_auth_config()

    log = target_logger or logger
    if is_dalton_local_auth_enabled():
        svc_token = os.getenv("INTERNAL_SERVICE_TOKEN", "").strip()
        if not is_dalton_service_token_valid(svc_token):
            log.warning(
                "HOST_MODE=oss, DALTON_AUTH_MODE=local: INTERNAL_SERVICE_TOKEN is empty "
                "or shorter than %d characters (len=%d); service-token requests will be rejected.",
                MIN_SERVICE_TOKEN_LEN,
                len(svc_token),
            )
        log.info(
            "HOST_MODE=oss, DALTON_AUTH_MODE=local: Dalton local account authentication is enabled."
        )
        return True

    if not is_dalton_supabase_auth_enabled():
        return False

    from src.config import settings as app_settings

    _auth_settings_cache = None
    _auth_settings_retry = None
    _cancel_background_auth_retry()
    if _auth_settings_refresh_task is not None and not _auth_settings_refresh_task.done():
        _auth_settings_refresh_task.cancel()
    _auth_settings_refresh_task = None
    _auth_settings_refresh_key = None
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
    "get_dalton_auth_mode",
    "is_dalton_local_auth_enabled",
    "is_dalton_service_request",
    "is_dalton_service_token_valid",
    "is_dalton_supabase_auth_enabled",
    "log_dalton_auth_startup_if_enabled",
    "resolve_caller_home_workspace_id",
    "resolve_dalton_create_user_email",
    "resolve_dalton_sync_email",
    "validate_dalton_auth_config",
    "verify_dalton_bearer",
    "verify_dalton_optional_bearer",
    "verify_dalton_token",
    "verify_workspace_access",
]
