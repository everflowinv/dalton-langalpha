"""Unified backend authentication dispatch for ``HOST_MODE == "oss"``.

Eliminates the passwordless ``local-dev-user`` bypass when ``DALTON_AUTH_MODE`` is
``local`` (or an unknown/invalid value):
- ``DALTON_AUTH_MODE == "oss"`` (explicitly): returns ``local_dev_user_id``.
- ``DALTON_AUTH_MODE == "local"``: strictly verifies local HS256 JWT tokens AND verifies
  that the ``sid`` session in ``dalton_local_sessions`` is active and unrevoked.
- ``DALTON_AUTH_MODE == "supabase"``: handled by the existing Supabase path upstream
  (kept untouched); if reached here unexpectedly, fails closed with HTTP 503.
- Any other value: fails closed with HTTP 503 (or WS 1008).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request, WebSocket, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials

from src.dalton.auth.sessions import verify_local_session_active
from src.dalton.auth.tokens import LocalAccessClaims, decode_local_access_token
from src.dalton.company_identity import (
    is_company_system_user,
    reject_company_system_auth,
)
from src.dalton.settings import get_dalton_auth_mode, is_dalton_local_auth_enabled

logger = logging.getLogger(__name__)

MAX_SESSION_RECHECK_SECONDS = 30.0
DEFAULT_WS_SESSION_RECHECK_SECONDS = 30.0
DEFAULT_SSE_SESSION_RECHECK_SECONDS = 30.0


def _refuse_holder(user_id: str | None) -> None:
    from src.server.utils.api import refuse_holder_credential

    refuse_holder_credential(user_id)


def _note_caller(request: Request, user_id: str, auth: str) -> None:
    from src.server.utils.api import note_caller

    note_caller(request, user_id, auth)


def _record_local_session_on_request(request: Request | None, session_id: str) -> None:
    if request is None or not session_id:
        return
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["dalton_local_session_id"] = session_id
            return
    req_state = getattr(request, "state", None)
    if req_state is not None:
        try:
            setattr(req_state, "dalton_local_session_id", session_id)
        except Exception:
            pass


def _read_local_session_from_request(request: Request | None) -> str | None:
    if request is None:
        return None
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        state = scope.get("state")
        if isinstance(state, dict) and state.get("dalton_local_session_id"):
            return str(state["dalton_local_session_id"]).strip() or None
    req_state = getattr(request, "state", None)
    sid = getattr(req_state, "dalton_local_session_id", None) if req_state is not None else None
    return str(sid).strip() if isinstance(sid, str) and sid.strip() else None


def _allow_ws_query_token() -> bool:
    """Return True ONLY when ``DALTON_WS_ALLOW_QUERY_TOKEN`` is explicitly enabled (default False)."""
    raw = os.getenv("DALTON_WS_ALLOW_QUERY_TOKEN", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def get_ws_session_recheck_seconds() -> float:
    raw = os.getenv("DALTON_WS_SESSION_RECHECK_SECONDS", "").strip()
    if raw:
        try:
            return min(MAX_SESSION_RECHECK_SECONDS, max(1.0, float(raw)))
        except ValueError:
            pass
    return DEFAULT_WS_SESSION_RECHECK_SECONDS


def get_sse_session_recheck_seconds() -> float:
    raw = os.getenv("DALTON_SSE_SESSION_RECHECK_SECONDS", "").strip()
    if raw:
        try:
            return min(MAX_SESSION_RECHECK_SECONDS, max(0.05, float(raw)))
        except ValueError:
            pass
    return DEFAULT_SSE_SESSION_RECHECK_SECONDS


def is_cli_auth_required(*, host_mode: str) -> bool:
    """Return ``auth_required`` for ``GET /api/v1/auth/cli`` (True in ``local`` mode)."""
    if is_dalton_local_auth_enabled():
        return True
    return host_mode != "oss"


class LocalSSESessionWatch:
    """Session liveness verifier for long-lived SSE streams in ``local`` auth mode."""

    def __init__(
        self,
        *,
        user_id: str,
        session_id: str,
        interval_seconds: float | None = None,
    ) -> None:
        self.user_id = user_id
        self.session_id = session_id
        raw_interval = (
            get_sse_session_recheck_seconds()
            if interval_seconds is None
            else max(0.05, float(interval_seconds))
        )
        self._interval = min(MAX_SESSION_RECHECK_SECONDS, raw_interval)
        self._last_checked = time.monotonic()

    def seconds_until_next_check(self) -> float:
        elapsed = time.monotonic() - self._last_checked
        return min(self._interval, max(0.05, self._interval - elapsed))

    async def ok(self, *, force: bool = False) -> bool:
        """Return True while the local session remains active; False once revoked/expired."""
        now = time.monotonic()
        if not force and (now - self._last_checked) < self._interval:
            return True
        try:
            await verify_local_session_active(
                user_id=self.user_id, session_id=self.session_id
            )
            self._last_checked = time.monotonic()
            return True
        except HTTPException:
            return False


def create_local_sse_session_watch(
    request: Request | None,
    user_id: str,
    *,
    interval_seconds: float | None = None,
) -> LocalSSESessionWatch | None:
    """Create a session watch for an established SSE stream in ``local`` mode (None otherwise)."""
    if not is_dalton_local_auth_enabled():
        return None
    sid = _read_local_session_from_request(request)
    uid = str(user_id or "").strip()
    if not sid or not uid:
        return None
    return LocalSSESessionWatch(
        user_id=uid, session_id=sid, interval_seconds=interval_seconds
    )


def make_prestart_chat_cleanup(
    *,
    user_id: str,
    upload_claim_key: str | None,
    request_key: str | None,
    additional_context: Any,
    release_slot: Callable[[], Awaitable[Any]] | None = None,
) -> Callable[[], Awaitable[None]] | None:
    """Build an ``on_initial_reject`` callback that releases provisional upload claims and burst slot."""
    if not upload_claim_key and release_slot is None:
        return None

    async def _cleanup() -> None:
        if upload_claim_key:
            from src.dalton.services.chat_uploads import release_request_chat_uploads

            try:
                await asyncio.shield(
                    release_request_chat_uploads(
                        user_id=user_id,
                        claim_key=upload_claim_key,
                        request_key=request_key,
                        additional_context=additional_context,
                    )
                )
            except Exception:
                logger.warning(
                    "Releasing provisional chat upload claim %s on revoked local SSE failed",
                    upload_claim_key,
                    exc_info=True,
                )
        if release_slot is not None:
            try:
                await release_slot()
            except Exception:
                logger.warning(
                    "Releasing burst slot on revoked local SSE failed",
                    exc_info=True,
                )

    return _cleanup


def wrap_local_sse_stream(
    stream: AsyncIterator[Any],
    request: Request | None,
    user_id: str,
    *,
    interval_seconds: float | None = None,
    timeout_frame: Any = None,
    on_initial_reject: Callable[[], Awaitable[Any]] | None = None,
) -> AsyncIterator[Any]:
    """Wrap a long-lived SSE stream in ``local`` mode to recheck the session every <=30s.

    Iterates ``stream`` directly inside the caller's task/context (never spawning per-read
    child tasks) so OpenTelemetry contexts attached by wrappers like ``traced_body`` stay
    intact across every chunk and detach cleanly on exit. A lightweight side monitor wakes
    every <=30s while idle and cancels the in-flight ``__anext__()`` wait on revocation.
    If the initial pre-stream check fails before ``stream`` starts, invokes
    ``on_initial_reject`` (e.g. releasing provisional attachment upload claims).
    Outside ``local`` mode (or without a local session), returns ``stream`` untouched.
    """
    watch = create_local_sse_session_watch(
        request, user_id, interval_seconds=interval_seconds
    )
    if watch is None:
        return stream

    async def _guarded() -> AsyncIterator[Any]:
        if not await watch.ok(force=True):
            if on_initial_reject is not None:
                await on_initial_reject()
            if timeout_frame is not None:
                yield timeout_frame
            return

        owner_task = asyncio.current_task()
        revoked = False
        awaiting_next = False

        async def _monitor() -> None:
            nonlocal revoked
            try:
                while True:
                    await asyncio.sleep(watch.seconds_until_next_check())
                    if not await watch.ok(force=True):
                        revoked = True
                        if (
                            awaiting_next
                            and owner_task is not None
                            and not owner_task.done()
                        ):
                            owner_task.cancel()
                        return
            except asyncio.CancelledError:
                return

        monitor_task = asyncio.create_task(_monitor())
        ait = stream.__aiter__()
        try:
            while True:
                if revoked:
                    if timeout_frame is not None:
                        yield timeout_frame
                    return
                awaiting_next = True
                try:
                    frame = await ait.__anext__()
                except StopAsyncIteration:
                    return
                finally:
                    awaiting_next = False
                # Read the flag after the check returns: the monitor may confirm a
                # revocation while this check is still waiting on its query.
                still_ok = await watch.ok()
                if revoked or not still_ok:
                    revoked = True
                    if timeout_frame is not None:
                        yield timeout_frame
                    return
                yield frame
        except asyncio.CancelledError:
            if revoked:
                if owner_task is not None:
                    owner_task.uncancel()
                if timeout_frame is not None:
                    yield timeout_frame
                return
            raise
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except BaseException:
                pass
            aclose = getattr(ait, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:
                    pass

    return _guarded()


async def _verify_local_token_and_session(token: str) -> LocalAccessClaims:
    """Decode local HS256 JWT and verify its session row is active & unrevoked in Postgres."""
    claims = decode_local_access_token(token)
    reject_company_system_auth(claims.user_id)
    _refuse_holder(claims.user_id)
    await verify_local_session_active(
        user_id=claims.user_id, session_id=claims.session_id
    )
    return claims


def is_service_token_rejected_by_dalton_auth(configured_token: str | None) -> bool:
    """Unified service-token policy for ``src/server/utils/api.py:_service_token_user_id``.

    When either Dalton Supabase auth or Dalton Local auth is enabled, requires a
    configured ``INTERNAL_SERVICE_TOKEN`` of at least 32 characters.
    """
    from src.dalton import auth as dalton_auth

    if (
        dalton_auth.is_dalton_supabase_auth_enabled() or is_dalton_local_auth_enabled()
    ) and not dalton_auth.is_dalton_service_token_valid(configured_token):
        return True
    return False


def background_dispatch_requires_token_override() -> bool | None:
    """Override for ``src/config/settings.py:background_dispatch_requires_token``.

    Enforces >=32-char ``INTERNAL_SERVICE_TOKEN`` in both ``supabase`` and ``local`` modes;
    returns ``None`` otherwise so upstream logic runs unchanged.
    """
    from src.dalton import auth as dalton_auth

    if dalton_auth.is_dalton_supabase_auth_enabled() or is_dalton_local_auth_enabled():
        return not dalton_auth.is_dalton_service_token_valid(
            os.environ.get("INTERNAL_SERVICE_TOKEN", "")
        )
    return None


def resolve_messaging_service_flags(
    *,
    is_internal: bool,
    is_dispatch: bool,
    sent_token: str,
    configured_token: str,
) -> tuple[bool, bool]:
    """Unified ``(from_service, is_server_retry)`` flags for ``messaging.py``.

    In both ``supabase`` and ``local`` auth modes, both flags reuse the strict
    ``is_internal`` result from ``_is_service_request`` instead of falling back to
    raw ``service_token_matches`` (which ignores the 32-char minimum).
    """
    from src.dalton import auth as dalton_auth
    from src.server.utils.api import service_token_matches

    if dalton_auth.is_dalton_supabase_auth_enabled() or is_dalton_local_auth_enabled():
        return (is_internal or is_dispatch), is_internal

    matched = service_token_matches(sent_token, configured_token)
    return (matched or is_dispatch), (matched or is_dispatch)


async def dispatch_oss_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    local_dev_user_id: str,
) -> str:
    """Resolve user_id in ``HOST_MODE == 'oss'`` for required-auth HTTP dependencies."""
    mode = get_dalton_auth_mode()
    if mode == "oss":
        reject_company_system_auth(local_dev_user_id)
        _note_caller(request, local_dev_user_id, "session")
        return local_dev_user_id
    if mode == "local":
        if credentials is None or not credentials.credentials:
            raise HTTPException(status_code=401, detail="Missing authentication")
        claims = await _verify_local_token_and_session(credentials.credentials)
        _record_local_session_on_request(request, claims.session_id)
        _note_caller(request, claims.user_id, "session")
        return claims.user_id
    raise HTTPException(status_code=503, detail="Invalid DALTON_AUTH_MODE configuration")


async def dispatch_oss_optional_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    local_dev_user_id: str,
) -> str | None:
    """Resolve optional user_id in ``HOST_MODE == 'oss'``.

    Under ``local`` mode:
    - Absent ``Authorization`` header -> ``None`` (anonymous).
    - Present ``Authorization`` header with empty/invalid/non-Bearer format or
      invalid/revoked JWT -> ``401`` (never silently downgraded to anonymous).
    Under explicit ``oss`` mode: returns ``local_dev_user_id``.
    """
    mode = get_dalton_auth_mode()
    if mode == "oss":
        reject_company_system_auth(local_dev_user_id)
        return local_dev_user_id
    if mode == "local":
        if credentials is None or not credentials.credentials:
            headers = getattr(request, "headers", None)
            if headers is not None and "authorization" in headers:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid Authorization header",
                )
            return None
        claims = await _verify_local_token_and_session(credentials.credentials)
        return claims.user_id
    raise HTTPException(status_code=503, detail="Invalid DALTON_AUTH_MODE configuration")


async def dispatch_oss_jwt_user_id(
    credentials: HTTPAuthorizationCredentials | None,
    *,
    local_dev_user_id: str,
) -> str:
    """Resolve user_id for ``jwt_bearer.verify_jwt_token`` in ``HOST_MODE == 'oss'``."""
    info = await dispatch_oss_auth_info(
        credentials, local_dev_user_id=local_dev_user_id
    )
    return info.user_id


async def dispatch_oss_auth_info(
    credentials: HTTPAuthorizationCredentials | None,
    *,
    local_dev_user_id: str,
) -> Any:
    """Resolve :class:`AuthInfo` for ``jwt_bearer.get_current_auth_info`` in ``HOST_MODE == 'oss'``."""
    from src.server.auth.jwt_bearer import AuthInfo

    mode = get_dalton_auth_mode()
    if mode == "oss":
        reject_company_system_auth(local_dev_user_id)
        return AuthInfo(user_id=local_dev_user_id)
    if mode == "local":
        if credentials is None or not credentials.credentials:
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        claims = await _verify_local_token_and_session(credentials.credentials)
        return AuthInfo(user_id=claims.user_id, auth_provider="local")
    raise HTTPException(status_code=503, detail="Invalid DALTON_AUTH_MODE configuration")


async def dispatch_oss_websocket(
    websocket: WebSocket,
    *,
    local_dev_user_id: str,
) -> str:
    """Authenticate a WebSocket handshake in ``HOST_MODE == 'oss'``.

    Under ``local`` mode, validates the HS256 JWT AND active DB session; missing,
    invalid, or revoked credentials close the socket with code ``1008``.
    On success, records ``local_user_id`` and ``local_session_id`` on ``websocket.state``
    so active connections can close promptly if the session is later revoked.
    """
    mode = get_dalton_auth_mode()
    if mode == "oss":
        if is_company_system_user(local_dev_user_id):
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token"
            )
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token"
            )
        return local_dev_user_id

    if mode != "local":
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Invalid auth configuration",
        )
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Invalid auth configuration",
        )

    token: str | None = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    if not token and _allow_ws_query_token():
        token = websocket.query_params.get("token")

    if not token:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Missing auth token"
        )
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="Missing auth token"
        )

    try:
        claims = await _verify_local_token_and_session(token)
        if is_company_system_user(claims.user_id):
            raise HTTPException(status_code=401, detail="Invalid token")
        ws_state = getattr(websocket, "state", None)
        if ws_state is not None:
            ws_state.local_user_id = claims.user_id
            ws_state.local_session_id = claims.session_id
        return claims.user_id
    except Exception as exc:
        logger.warning("Local WS auth failed: %s", exc)
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token"
        )
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token"
        ) from exc


async def check_ws_local_session_or_close(websocket: WebSocket) -> bool:
    """Re-verify an active WebSocket's local session; close with 1008 if revoked/expired.

    Returns True if the connection remains authorized (or if not in local auth mode),
    False after closing the socket with code ``1008``.
    """
    if not is_dalton_local_auth_enabled():
        return True
    ws_state = getattr(websocket, "state", None)
    uid = getattr(ws_state, "local_user_id", None) if ws_state is not None else None
    sid = getattr(ws_state, "local_session_id", None) if ws_state is not None else None
    if not uid or not sid:
        return True
    try:
        await verify_local_session_active(user_id=str(uid), session_id=str(sid))
        return True
    except HTTPException as exc:
        reason = "Session revoked" if exc.status_code == 401 else "Auth unavailable"
        try:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=reason)
        except Exception:
            pass
        return False


def start_ws_session_revocation_watch(
    websocket: WebSocket,
    disconnected: asyncio.Event,
    *,
    interval_seconds: float | None = None,
) -> asyncio.Task[None] | None:
    """Start a lightweight background watcher that closes an active local-auth WS on session revocation.

    Rechecks ``dalton_local_sessions`` every ``DALTON_WS_SESSION_RECHECK_SECONDS`` (default 30s).
    When the session is revoked or expired, closes the socket with code ``1008`` and sets
    ``disconnected`` so the WS handler loop exits cleanly. No-op outside ``local`` mode.
    """
    if not is_dalton_local_auth_enabled():
        return None
    ws_state = getattr(websocket, "state", None)
    uid = getattr(ws_state, "local_user_id", None) if ws_state is not None else None
    sid = getattr(ws_state, "local_session_id", None) if ws_state is not None else None
    if not uid or not sid:
        return None

    poll_s = min(
        MAX_SESSION_RECHECK_SECONDS,
        get_ws_session_recheck_seconds()
        if interval_seconds is None
        else max(0.05, float(interval_seconds)),
    )

    async def _watch() -> None:
        while not disconnected.is_set():
            try:
                await asyncio.wait_for(disconnected.wait(), timeout=poll_s)
                return
            except asyncio.TimeoutError:
                pass
            if not await check_ws_local_session_or_close(websocket):
                disconnected.set()
                return

    return asyncio.create_task(_watch(), name=f"dalton-ws-session-watch-{str(sid)[:8]}")


def dispatch_oss_service_request(
    sent_token: str,
    configured_token: str,
    *,
    host_mode: str,
) -> bool:
    """Verify server self-dispatch in ``messaging._is_service_request``.

    Only explicit ``DALTON_AUTH_MODE == "oss"`` trusts an empty service token.
    In ``local`` mode, requires a valid >=32-char matching ``X-Service-Token``.
    """
    from src.dalton.auth import is_dalton_service_request
    from src.server.utils.api import service_token_matches

    if host_mode != "oss":
        return service_token_matches(sent_token, configured_token)

    mode = get_dalton_auth_mode()
    if mode == "oss":
        return service_token_matches(sent_token, configured_token) or (
            not configured_token
        )
    if mode == "local":
        return is_dalton_service_request(sent_token, configured_token)
    return False
