"""
WebSocket authentication helper.

Extracts and verifies Supabase JWTs from WebSocket handshake parameters
(``?token=`` query param or ``Authorization`` header).  Reuses the same
``_decode_token`` logic as the HTTP Bearer flow in ``jwt_bearer.py``.

When auth is disabled (``SUPABASE_URL`` unset), returns the local-dev
user ID without requiring a token — same behaviour as HTTP endpoints.
"""

import logging

from fastapi import WebSocket, WebSocketException, status

from src.config.settings import HOST_MODE, LOCAL_DEV_USER_ID
from src.server.auth.jwt_bearer import _decode_token
from src.server.services.account import ACCOUNT_SUSPENDED, AccountSuspended, admit

logger = logging.getLogger(__name__)


async def authenticate_websocket(websocket: WebSocket) -> str:
    """Verify a Supabase JWT from a WebSocket handshake.

    Call **before** ``websocket.accept()``.  Returns the ``user_id``
    (Supabase ``sub`` claim).

    Token sources (checked in order):
    1. ``Authorization: Bearer <token>`` header
    2. ``?token=<token>`` query parameter

    On auth failure the socket is closed with code **1008** (Policy
    Violation) and a ``WebSocketException`` is raised so the caller's
    handler exits cleanly.
    """
    # DALTON-HOOK(dalton-auth): verify WebSocket JWT + allowlist when Dalton auth is enabled
    from src.dalton.auth import authenticate_dalton_websocket, is_dalton_supabase_auth_enabled
    # DALTON-HOOK(company-automations): refuse WebSocket login as system:dalton-company
    from src.dalton.company_identity import is_company_system_user

    if is_dalton_supabase_auth_enabled():
        ws_uid = await authenticate_dalton_websocket(websocket)
        if is_company_system_user(ws_uid):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
        return ws_uid
    if HOST_MODE == "oss":
        # DALTON-HOOK(standalone-core): dispatch local/invalid Dalton auth modes before upstream OSS owner check
        # DALTON-HOOK(share-redaction): enforce trusted loopback/owner gate on OSS WebSocket handshakes
        from src.dalton.auth.dispatch import dispatch_oss_websocket, verify_oss_websocket_owner
        from src.dalton.settings import get_dalton_auth_mode

        if get_dalton_auth_mode() != "oss":
            return await dispatch_oss_websocket(websocket, local_dev_user_id=LOCAL_DEV_USER_ID)
        await verify_oss_websocket_owner(websocket)
        if is_company_system_user(LOCAL_DEV_USER_ID):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
        return LOCAL_DEV_USER_ID

    # 1. Try Authorization header
    token: str | None = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()

    # 2. Fall back to ?token= query param
    if not token:
        token = websocket.query_params.get("token")

    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing auth token")
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION, reason="Missing auth token")

    try:
        auth_info = await _decode_token(token)
    except Exception as exc:
        logger.warning("WS auth failed: %s", exc)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token")
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason="Invalid or expired token"
        ) from exc
    try:
        await admit(auth_info.user_id)
    except AccountSuspended:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason=ACCOUNT_SUSPENDED)
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION, reason=ACCOUNT_SUSPENDED
        ) from None
    return auth_info.user_id
