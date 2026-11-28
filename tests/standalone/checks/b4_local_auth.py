# auth: local
"""Standalone Core Batch 4b live acceptance check (`DALTON_AUTH_MODE=local`).

Uses ONLY Python stdlib and `_lib.py` helpers (no httpx, no psycopg).
Verifies:
1. Alembic heads match database versions (no hasattr bypass).
2. Edge strips/rejects service headers (requires INTERNAL_SERVICE_TOKEN) and
   unauthenticated `/users/me` returns 401; bad Bearer returns 401 on protected
   and optional-auth routes while anonymous optional-auth succeeds.
3. Anonymous WebSocket handshake is rejected (asserting actual HTTP/WS behavior
   without assuming 101), and a short-lived HMAC WS ticket connects (HTTP 101).
4. First-admin bootstrap succeeds once, returns access token + refresh cookie,
   rotates refresh cookie (new cookie value differs and continues working while
   replaying rotated cookie is rejected), and permanently returns 409 afterward.
5. Logout invalidates the old access token and refresh cookie (401).
6. Tiered invites (`admin` / `internal` / `external` + internal->admin 403) create
   local accounts with expected roles and reject replay.
7. Password change invalidates the old access token and refresh cookie (401).
8. Dual-user workspace isolation prevents cross-user listing or read.
9. Private/loopback/metadata custom model URLs are refused (400) for both member
   and admin, while the operator-allowlisted fake-llm endpoint (required, never
   skipped) returns HTTP 200 with expected fake model output.
10. External member permissions and unauthenticated credentials status (401):
    unauthenticated `/api/v1/dalton/credentials/status` returns 401; invited
    `external` member is gated across file audience, thread read/status/share,
    user settings, workspace settings, and monitor publish with expected
    403/404 statuses and structured `detail.code`.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import ssl
import struct
import sys
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _lib  # noqa: E402


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _extract_cookie(headers: dict[str, str], cookie_name: str) -> str:
    raw = headers.get("set-cookie", "")
    for part in raw.split(","):
        segment = part.strip().split(";", 1)[0].strip()
        if segment.startswith(f"{cookie_name}="):
            return segment
    # Handle cookies whose Expires attribute contains a comma (e.g. "Mon, 01 Jan ...")
    marker = f"{cookie_name}="
    idx = raw.find(marker)
    if idx != -1:
        tail = raw[idx:]
        return tail.split(";", 1)[0].strip()
    return ""


def _cookie_value(cookie_pair: str) -> str:
    return cookie_pair.split("=", 1)[1].strip() if "=" in cookie_pair else cookie_pair.strip()


def _ws_handshake(
    base_url: str,
    path: str,
    *,
    timeout: float = 10.0,
    expect_close: bool = True,
) -> tuple[int, int | None]:
    """Perform a minimal stdlib RFC 6455 handshake and return `(http_status, ws_close_code)`.

    When ``expect_close=False`` and the server upgrades with HTTP 101, waits briefly
    for any immediate policy close frame and returns ``(101, None)`` if the connection
    stays open cleanly.
    """
    parsed = urlparse(base_url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    host_header = f"{host}:{port}" if parsed.port else host

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    expected_accept = base64.b64encode(
        hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
        ).digest()
    ).decode("ascii")

    raw_sock = socket.create_connection((host, port), timeout=timeout)
    sock: socket.socket = raw_sock
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(raw_sock, server_hostname=host)
        sock.settimeout(timeout)

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"Origin: {base_url}\r\n"
            f"\r\n"
        ).encode("ascii")
        sock.sendall(request)

        header_buf = bytearray()
        while b"\r\n\r\n" not in header_buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            header_buf.extend(chunk)

        sep = header_buf.find(b"\r\n\r\n")
        _assert(sep != -1, f"Incomplete HTTP response during WS handshake for {path}")
        head = header_buf[:sep].decode("iso-8859-1")
        leftover = bytes(header_buf[sep + 4 :])

        status_line = head.split("\r\n", 1)[0]
        parts = status_line.split(" ", 2)
        http_status = int(parts[1])
        if http_status != 101:
            return http_status, None

        headers: dict[str, str] = {}
        for line in head.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        _assert(
            headers.get("sec-websocket-accept") == expected_accept,
            "Invalid Sec-WebSocket-Accept from server",
        )

        # Read frames to detect an immediate CLOSE frame (opcode 0x8).
        buf = bytearray(leftover)
        if not expect_close:
            sock.settimeout(1.0)

        def _take(count: int) -> bytes:
            while len(buf) < count:
                chunk = sock.recv(4096)
                if not chunk:
                    raise EOFError("WebSocket closed before CLOSE frame")
                buf.extend(chunk)
            out = bytes(buf[:count])
            del buf[:count]
            return out

        try:
            for _ in range(8):
                first2 = _take(2)
                opcode = first2[0] & 0x0F
                masked = bool(first2[1] & 0x80)
                length = first2[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", _take(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", _take(8))[0]
                mask_key = _take(4) if masked else b""
                payload = _take(length)
                if masked:
                    payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:
                    close_code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                    return 101, close_code
        except (TimeoutError, socket.timeout):
            if not expect_close:
                return 101, None
            raise
        except EOFError:
            if not expect_close:
                return 101, None
            raise
        return 101, None
    finally:
        sock.close()


def _assert_ws_rejected(http_status: int, ws_code: int | None, context: str) -> None:
    """Assert an unauthorized WS handshake was actually rejected (not 404, not left open)."""
    _assert(
        http_status != 404,
        f"{context}: WS route returned 404 (market-data WS endpoint is not mounted)",
    )
    rejected_at_handshake = http_status in (401, 403)
    rejected_after_upgrade = http_status == 101 and ws_code == 1008
    _assert(
        rejected_at_handshake or rejected_after_upgrade,
        f"{context}: expected HTTP 401/403 rejection or WS 1008 close, "
        f"got http={http_status} ws_close={ws_code}",
    )


def _check_unauthenticated_surface(ctx: _lib.ComposeContext) -> None:
    base = ctx.edge_url.rstrip("/")

    # 1. Edge with valid service headers must still return 401 (never skip missing token)
    service_token = ctx.internal_service_token
    _assert(bool(service_token), "INTERNAL_SERVICE_TOKEN is required for edge header-strip check")
    svc_headers: dict[str, str] = {
        "X-User-Id": "forged-user-id",
        "X-Service-Token": service_token,
    }
    resp_svc = _lib.http_request("GET", f"{base}/api/v1/users/me", headers=svc_headers)
    _assert(
        resp_svc.status == 401,
        f"Expected 401 on edge /users/me with service headers, got {resp_svc.status}",
    )

    # 2. No token -> 401 (including /dalton/credentials/status for missing VITE_DALTON_AUTH check)
    resp_anon = _lib.http_request("GET", f"{base}/api/v1/users/me")
    _assert(
        resp_anon.status == 401,
        f"Expected 401 on unauthenticated /users/me, got {resp_anon.status}",
    )
    resp_creds_anon = _lib.http_request("GET", f"{base}/api/v1/dalton/credentials/status")
    _assert(
        resp_creds_anon.status == 401,
        f"Expected 401 on unauthenticated /api/v1/dalton/credentials/status, got {resp_creds_anon.status}",
    )

    # 3. Malformed / invalid Bearer token -> 401
    resp_bad = _lib.http_request(
        "GET",
        f"{base}/api/v1/users/me",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    _assert(
        resp_bad.status == 401,
        f"Expected 401 on bad Bearer token /users/me, got {resp_bad.status}",
    )

    # 4. Optional-auth endpoint: anonymous passes, bad token fails 401
    resp_opt_anon = _lib.http_request("GET", f"{base}/api/v1/skills")
    _assert(
        resp_opt_anon.status == 200,
        f"Expected 200 on anonymous /api/v1/skills, got {resp_opt_anon.status}",
    )
    resp_opt_bad = _lib.http_request(
        "GET",
        f"{base}/api/v1/skills",
        headers={"Authorization": "Bearer invalid.jwt.token"},
    )
    _assert(
        resp_opt_bad.status == 401,
        f"Expected 401 on bad token /api/v1/skills, got {resp_opt_bad.status}",
    )

    # 5. Unauthenticated WebSocket handshake is rejected (Starlette rejects before accept -> 403, or 101+1008)
    http_status, ws_code = _ws_handshake(
        base, "/ws/v1/market-data/aggregates/stock", expect_close=True
    )
    _assert_ws_rejected(http_status, ws_code, "Anonymous WS handshake")


def _check_bootstrap_and_sessions(ctx: _lib.ComposeContext) -> tuple[str, str, str]:
    """Bootstrap first admin, lock permanently (409), verify cookie rotation, WS ticket, and logout.

    Returns `(admin_access_token, admin_user_id, admin_email)`.
    """
    base = ctx.edge_url.rstrip("/")
    bootstrap_token = ctx.env.get("BOOTSTRAP_SETUP_TOKEN") or os.environ.get(
        "BOOTSTRAP_SETUP_TOKEN", ""
    )
    _assert(bool(bootstrap_token), "BOOTSTRAP_SETUP_TOKEN must be present in test env")

    status_before = _lib.http_request("GET", f"{base}/api/v1/dalton/bootstrap/status")
    _assert(
        status_before.status == 200 and status_before.json().get("needs_bootstrap") is True,
        f"Expected needs_bootstrap=true before setup, got status={status_before.status}, "
        f"fields={_lib.response_fields(status_before)}",
    )

    admin_email = f"admin-{secrets.token_hex(4)}@core.local"
    admin_password = f"AdminPass!{secrets.token_hex(6)}"
    _lib.record_runtime_secret("LOCAL_ADMIN_PASSWORD", admin_password)

    # Wrong bootstrap token is rejected
    wrong_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/bootstrap/admin",
        json_body={
            "bootstrap_token": "wrong-bootstrap-token",
            "email": admin_email,
            "password": admin_password,
        },
    )
    _assert(
        wrong_resp.status == 401,
        f"Expected 401 for wrong bootstrap token (bootstrap.py rejects it as unauthenticated), got {wrong_resp.status}",
    )

    # Valid bootstrap creates first admin
    boot_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/bootstrap/admin",
        json_body={
            "bootstrap_token": bootstrap_token,
            "email": admin_email,
            "username": "first_admin",
            "name": "First Admin",
            "password": admin_password,
        },
    )
    _assert(
        boot_resp.status == 201,
        f"Expected 201 on first admin bootstrap, got status={boot_resp.status}, "
        f"fields={_lib.response_fields(boot_resp)}",
    )
    boot_data = boot_resp.json()
    admin_token = str(boot_data.get("access_token") or "")
    _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", admin_token)
    admin_user = boot_data.get("user") or {}
    admin_uid = str(admin_user.get("user_id") or "")
    _assert(
        bool(admin_token) and bool(admin_uid),
        f"Bootstrap response missing access_token/user_id "
        f"(status={boot_resp.status}, fields={_lib.response_field_names(boot_data)})",
    )
    _assert(
        admin_user.get("tier") == "admin",
        f"Expected admin tier (status={boot_resp.status}, "
        f"user_fields={_lib.response_field_names(admin_user)})",
    )

    refresh_cookie_1 = _extract_cookie(boot_resp.headers, "dalton_refresh_token")
    _lib.record_runtime_secret("LOCAL_REFRESH_COOKIE", _cookie_value(refresh_cookie_1))
    _assert(bool(refresh_cookie_1), "Bootstrap response missing dalton_refresh_token Set-Cookie")

    # Status now permanently false, repeat POST permanently 409
    status_after = _lib.http_request("GET", f"{base}/api/v1/dalton/bootstrap/status")
    _assert(
        status_after.status == 200 and status_after.json().get("needs_bootstrap") is False,
        f"Expected needs_bootstrap=false after setup, got status={status_after.status}, "
        f"fields={_lib.response_fields(status_after)}",
    )
    repeat_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/bootstrap/admin",
        json_body={
            "bootstrap_token": bootstrap_token,
            "email": "second-admin@core.local",
            "password": admin_password,
        },
    )
    _assert(
        repeat_resp.status == 409,
        f"Expected permanent 409 on repeat bootstrap, got {repeat_resp.status}",
    )

    # Access token works on /users/me
    me_resp = _lib.http_request(
        "GET",
        f"{base}/api/v1/users/me",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    _assert(
        me_resp.status == 200
        and (me_resp.json().get("user") or {}).get("user_id") == admin_uid,
        f"Expected 200 with the bootstrapped admin on /users/me, got status={me_resp.status}, "
        f"fields={_lib.response_fields(me_resp)}",
    )

    # Refresh cookie rotation: cookie 1 -> cookie 2 (distinct) -> cookie 3 (distinct)
    refresh_resp_1 = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/refresh",
        headers={"Cookie": refresh_cookie_1, "Origin": base},
    )
    refresh_data_1 = refresh_resp_1.json() if refresh_resp_1.status == 200 else {}
    if isinstance(refresh_data_1, dict):
        _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", str(refresh_data_1.get("access_token") or ""))
    _assert(
        refresh_resp_1.status == 200 and bool(refresh_data_1.get("access_token")),
        f"Expected 200 on first cookie refresh, got status={refresh_resp_1.status}, "
        f"fields={_lib.response_fields(refresh_resp_1)}",
    )
    refresh_cookie_2 = _extract_cookie(refresh_resp_1.headers, "dalton_refresh_token")
    _lib.record_runtime_secret("LOCAL_REFRESH_COOKIE", _cookie_value(refresh_cookie_2))
    _assert(
        bool(refresh_cookie_2) and refresh_cookie_2 != refresh_cookie_1,
        "Expected rotated dalton_refresh_token cookie distinct from initial cookie",
    )

    refresh_resp_2 = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/refresh",
        headers={"Cookie": refresh_cookie_2, "Origin": base},
    )
    refresh_data_2 = refresh_resp_2.json() if refresh_resp_2.status == 200 else {}
    refreshed_token = (
        str(refresh_data_2.get("access_token") or "") if isinstance(refresh_data_2, dict) else ""
    )
    _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", refreshed_token)
    _assert(
        refresh_resp_2.status == 200 and bool(refreshed_token),
        f"Expected 200 on second cookie refresh, got status={refresh_resp_2.status}, "
        f"fields={_lib.response_fields(refresh_resp_2)}",
    )
    refresh_cookie_3 = _extract_cookie(refresh_resp_2.headers, "dalton_refresh_token")
    _lib.record_runtime_secret("LOCAL_REFRESH_COOKIE", _cookie_value(refresh_cookie_3))
    _assert(
        bool(refresh_cookie_3) and refresh_cookie_3 != refresh_cookie_2,
        "Expected second rotated dalton_refresh_token cookie distinct from previous cookie",
    )

    # Short-lived HMAC WebSocket ticket succeeds (HTTP 101, stays open); raw JWT in ?ticket= fails
    ws_ticket_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/ws-ticket",
        headers={"Authorization": f"Bearer {refreshed_token}"},
    )
    _assert(
        ws_ticket_resp.status == 200,
        f"Expected 200 minting WS ticket, got status={ws_ticket_resp.status}, "
        f"fields={_lib.response_fields(ws_ticket_resp)}",
    )
    ws_ticket_data = ws_ticket_resp.json()
    ws_ticket = str(ws_ticket_data.get("ticket") or "")
    _lib.record_runtime_secret("LOCAL_WS_TICKET", ws_ticket)
    _assert(
        bool(ws_ticket) and ws_ticket_data.get("expires_in") == 60,
        f"Unexpected WS ticket payload (status={ws_ticket_resp.status}, "
        f"fields={_lib.response_field_names(ws_ticket_data)})",
    )
    ws_ok_status, ws_ok_close = _ws_handshake(
        base,
        f"/ws/v1/market-data/aggregates/stock?ticket={quote(ws_ticket, safe='')}",
        expect_close=False,
    )
    _assert(
        ws_ok_status == 101 and ws_ok_close is None,
        f"Expected valid WS ticket to connect with HTTP 101 and stay open, "
        f"got http={ws_ok_status} ws_close={ws_ok_close}",
    )
    ws_jwt_as_ticket_status, ws_jwt_as_ticket_close = _ws_handshake(
        base,
        f"/ws/v1/market-data/aggregates/stock?ticket={quote(refreshed_token, safe='')}",
        expect_close=True,
    )
    _assert_ws_rejected(
        ws_jwt_as_ticket_status,
        ws_jwt_as_ticket_close,
        "Raw access JWT passed as ?ticket=",
    )

    # Separate login session -> logout -> verify old access token & refresh cookie are both invalidated (401)
    login_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/login",
        json_body={"identifier": admin_email, "password": admin_password},
    )
    login_data = login_resp.json() if login_resp.status == 200 else {}
    logout_session_token = (
        str(login_data.get("access_token") or "") if isinstance(login_data, dict) else ""
    )
    _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", logout_session_token)
    _assert(
        login_resp.status == 200
        and bool(logout_session_token)
        and (login_data.get("user") or {}).get("tier") == "admin",
        f"Expected 200 on admin login, got status={login_resp.status}, "
        f"fields={_lib.response_fields(login_resp)}",
    )
    logout_session_cookie = _extract_cookie(login_resp.headers, "dalton_refresh_token")
    _lib.record_runtime_secret("LOCAL_REFRESH_COOKIE", _cookie_value(logout_session_cookie))
    _assert(bool(logout_session_cookie), "Admin login response missing refresh cookie")

    pre_logout_me = _lib.http_request(
        "GET",
        f"{base}/api/v1/users/me",
        headers={"Authorization": f"Bearer {logout_session_token}"},
    )
    _assert(pre_logout_me.status == 200, f"Expected 200 before logout, got {pre_logout_me.status}")

    logout_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/logout",
        headers={"Cookie": logout_session_cookie, "Origin": base},
    )
    _assert(
        logout_resp.status == 200,
        f"Expected 200 on logout, got status={logout_resp.status}, "
        f"fields={_lib.response_fields(logout_resp)}",
    )

    post_logout_me = _lib.http_request(
        "GET",
        f"{base}/api/v1/users/me",
        headers={"Authorization": f"Bearer {logout_session_token}"},
    )
    _assert(
        post_logout_me.status == 401,
        f"Expected 401 for access token after logout, got {post_logout_me.status}",
    )

    post_logout_refresh = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/refresh",
        headers={"Cookie": logout_session_cookie, "Origin": base},
    )
    _assert(
        post_logout_refresh.status == 401,
        f"Expected 401 for refresh cookie after logout, got {post_logout_refresh.status}",
    )

    # First session remains valid for subsequent checks
    return refreshed_token, admin_uid, admin_email


def _check_tiered_invites_and_password_change(
    ctx: _lib.ComposeContext, admin_token: str
) -> tuple[str, str, str, str]:
    """Exercise admin/internal/external invites, rotated-cookie replay, and password-change invalidation.

    Returns `(internal_token, internal_uid, ext_token, ext_uid)`.
    """
    base = ctx.edge_url.rstrip("/")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    created: dict[str, tuple[str, str, str, str, str]] = {}
    for tier in ("admin", "internal", "external"):
        inv_resp = _lib.http_request(
            "POST",
            f"{base}/api/v1/dalton/invites",
            headers=admin_headers,
            json_body={"tier": tier},
        )
        _assert(
            inv_resp.status == 200,
            f"Expected 200 creating {tier} invite, got status={inv_resp.status}, "
            f"fields={_lib.response_fields(inv_resp)}",
        )
        inv_data = inv_resp.json()
        raw_token = str(inv_data.get("token") or "")
        _lib.record_runtime_secret("LOCAL_INVITE_TOKEN", raw_token)
        _assert(
            inv_data.get("tier") == tier,
            f"Expected invite tier={tier} (status={inv_resp.status}, "
            f"fields={_lib.response_field_names(inv_data)})",
        )
        _assert(
            bool(raw_token),
            f"Invite response missing token for tier={tier} "
            f"(status={inv_resp.status}, fields={_lib.response_field_names(inv_data)})",
        )

        check_resp = _lib.http_request(
            "POST",
            f"{base}/api/v1/dalton/invites/check",
            json_body={"token": raw_token},
        )
        _assert(
            check_resp.status == 200 and check_resp.json().get("valid") is True,
            f"Expected valid invite check for {tier}, got status={check_resp.status}, "
            f"fields={_lib.response_fields(check_resp)}",
        )

        username = f"u_{tier}_{secrets.token_hex(3)}"
        password = f"MemberPass!{secrets.token_hex(6)}"
        _lib.record_runtime_secret("LOCAL_MEMBER_PASSWORD", password)
        redeem_resp = _lib.http_request(
            "POST",
            f"{base}/api/v1/dalton/invites/redeem",
            json_body={"token": raw_token, "username": username, "password": password},
        )
        _assert(
            redeem_resp.status == 200,
            f"Expected 200 redeeming {tier} invite, got status={redeem_resp.status}, "
            f"fields={_lib.response_fields(redeem_resp)}",
        )
        redeem_data = redeem_resp.json()
        _assert(
            redeem_data.get("tier") == tier,
            f"Expected redeemed tier={tier} (status={redeem_resp.status}, "
            f"fields={_lib.response_field_names(redeem_data)})",
        )

        # One-time use enforcement
        replay_resp = _lib.http_request(
            "POST",
            f"{base}/api/v1/dalton/invites/redeem",
            json_body={"token": raw_token, "username": f"{username}_2", "password": password},
        )
        _assert(
            replay_resp.status == 400,
            f"Expected 400 on invite replay for {tier}, got {replay_resp.status}",
        )

        # Log in with the newly created local account
        login_resp = _lib.http_request(
            "POST",
            f"{base}/api/v1/dalton/auth/local/login",
            json_body={"identifier": username, "password": password},
        )
        _assert(
            login_resp.status == 200,
            f"Expected 200 logging in {tier} invitee, got status={login_resp.status}, "
            f"fields={_lib.response_fields(login_resp)}",
        )
        login_data = login_resp.json()
        member_access_token = str(login_data.get("access_token") or "")
        _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", member_access_token)
        _assert(
            bool(member_access_token) and login_data.get("user", {}).get("tier") == tier,
            f"Expected logged-in user tier={tier} (status={login_resp.status}, "
            f"fields={_lib.response_field_names(login_data)})",
        )
        login_cookie = _extract_cookie(login_resp.headers, "dalton_refresh_token")
        _lib.record_runtime_secret("LOCAL_REFRESH_COOKIE", _cookie_value(login_cookie))
        _assert(bool(login_cookie), f"Missing refresh cookie on login for {tier}")
        created[tier] = (
            member_access_token,
            str(login_data["user"]["user_id"]),
            username,
            password,
            login_cookie,
        )

    # Invited admin can perform admin-only invite creation (tier="internal")
    invited_admin_token, _inv_admin_uid, _inv_admin_u, _inv_admin_p, inv_admin_cookie = created["admin"]
    inv_admin_create = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/invites",
        headers={"Authorization": f"Bearer {invited_admin_token}"},
        json_body={"tier": "internal"},
    )
    inv_admin_data = inv_admin_create.json() if inv_admin_create.status == 200 else {}
    if isinstance(inv_admin_data, dict):
        _lib.record_runtime_secret("LOCAL_INVITE_TOKEN", str(inv_admin_data.get("token") or ""))
    _assert(
        inv_admin_create.status == 200 and inv_admin_data.get("tier") == "internal",
        f"Invited admin should be able to create internal invite, got status={inv_admin_create.status}, "
        f"fields={_lib.response_fields(inv_admin_create)}",
    )

    # Verify rotated cookie replay is refused (401 or 409 concurrent-grace conflict)
    inv_admin_rot = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/refresh",
        headers={"Cookie": inv_admin_cookie, "Origin": base},
    )
    if inv_admin_rot.status == 200:
        rot_data = inv_admin_rot.json()
        if isinstance(rot_data, dict):
            _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", str(rot_data.get("access_token") or ""))
    _lib.record_runtime_secret(
        "LOCAL_REFRESH_COOKIE",
        _cookie_value(_extract_cookie(inv_admin_rot.headers, "dalton_refresh_token")),
    )
    _assert(
        inv_admin_rot.status == 200,
        f"Expected 200 rotating invited admin cookie, got {inv_admin_rot.status}",
    )
    inv_admin_replay = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/refresh",
        headers={"Cookie": inv_admin_cookie, "Origin": base},
    )
    _assert(
        inv_admin_replay.status in (401, 409),
        f"Expected 401/409 replaying rotated refresh cookie, got {inv_admin_replay.status}",
    )

    # Non-admin (`internal`) must NOT be able to mint an `admin` invite
    internal_token, internal_uid, internal_user, internal_pass, internal_cookie = created["internal"]
    ext_token, ext_uid, _ext_user, _ext_pass, _ext_cookie = created["external"]
    forbid_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/invites",
        headers={"Authorization": f"Bearer {internal_token}"},
        json_body={"tier": "admin"},
    )
    _assert(
        forbid_resp.status == 403,
        f"Expected 403 when internal user attempts to create admin invite, got {forbid_resp.status}",
    )

    # Password change revokes all existing sessions (old access token & old refresh cookie -> 401)
    new_internal_pass = f"ChangedPass!{secrets.token_hex(6)}"
    _lib.record_runtime_secret("LOCAL_MEMBER_PASSWORD", new_internal_pass)
    chpwd_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/change-password",
        headers={"Authorization": f"Bearer {internal_token}"},
        json_body={
            "current_password": internal_pass,
            "new_password": new_internal_pass,
        },
    )
    _assert(
        chpwd_resp.status == 200 and int(chpwd_resp.json().get("sessions_revoked") or 0) >= 1,
        f"Expected 200 with sessions_revoked>=1 on change-password, got status={chpwd_resp.status}, "
        f"fields={_lib.response_fields(chpwd_resp)}",
    )
    old_token_me = _lib.http_request(
        "GET",
        f"{base}/api/v1/users/me",
        headers={"Authorization": f"Bearer {internal_token}"},
    )
    _assert(
        old_token_me.status == 401,
        f"Expected 401 for old access token after password change, got {old_token_me.status}",
    )
    old_cookie_refresh = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/refresh",
        headers={"Cookie": internal_cookie, "Origin": base},
    )
    _assert(
        old_cookie_refresh.status == 401,
        f"Expected 401 for old refresh cookie after password change, got {old_cookie_refresh.status}",
    )
    old_pass_login = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/login",
        json_body={"identifier": internal_user, "password": internal_pass},
    )
    _assert(
        old_pass_login.status == 401,
        f"Expected 401 logging in with old password, got {old_pass_login.status}",
    )
    new_pass_login = _lib.http_request(
        "POST",
        f"{base}/api/v1/dalton/auth/local/login",
        json_body={"identifier": internal_user, "password": new_internal_pass},
    )
    _assert(
        new_pass_login.status == 200,
        f"Expected 200 logging in with new password, got status={new_pass_login.status}, "
        f"fields={_lib.response_fields(new_pass_login)}",
    )
    new_pass_data = new_pass_login.json()
    new_internal_token = str(new_pass_data.get("access_token") or "")
    _lib.record_runtime_secret("LOCAL_ACCESS_TOKEN", new_internal_token)
    _lib.record_runtime_secret(
        "LOCAL_REFRESH_COOKIE",
        _cookie_value(_extract_cookie(new_pass_login.headers, "dalton_refresh_token")),
    )
    _assert(
        bool(new_internal_token),
        f"New-password login missing access_token (status={new_pass_login.status}, "
        f"fields={_lib.response_field_names(new_pass_data)})",
    )

    return new_internal_token, internal_uid, ext_token, ext_uid


def _check_dual_user_isolation(
    ctx: _lib.ComposeContext, admin_token: str, member_token: str
) -> str:
    base = ctx.edge_url.rstrip("/")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}

    ws_create = _lib.http_request(
        "POST",
        f"{base}/api/v1/workspaces",
        headers=admin_headers,
        json_body={"name": f"Admin Private WS {secrets.token_hex(3)}"},
    )
    _assert(
        ws_create.status in (200, 201),
        f"Expected 200/201 creating admin workspace, got {ws_create.status}: {ws_create.text}",
    )
    admin_ws_id = str(ws_create.json().get("workspace_id") or "")
    _assert(bool(admin_ws_id), f"Missing workspace_id in create response: {ws_create.text}")

    # Member cannot see admin's private workspace in list
    member_list = _lib.http_request("GET", f"{base}/api/v1/workspaces", headers=member_headers)
    _assert(
        member_list.status == 200,
        f"Expected 200 listing member workspaces, got {member_list.status}",
    )
    member_ws_ids = {
        str(row.get("workspace_id") or "")
        for row in (member_list.json().get("workspaces") or [])
    }
    _assert(
        admin_ws_id not in member_ws_ids,
        "External member list leaked admin private workspace",
    )

    # Member cannot fetch admin's private workspace directly
    member_get = _lib.http_request(
        "GET", f"{base}/api/v1/workspaces/{admin_ws_id}", headers=member_headers
    )
    _assert(
        member_get.status in (403, 404),
        f"Expected 403/404 when member fetches admin workspace, got {member_get.status}",
    )
    return admin_ws_id


def _assert_forbidden_code(resp: _lib.HttpResponse, expected_code: str, context: str) -> None:
    _assert(
        resp.status == 403,
        f"{context}: expected HTTP 403, got {resp.status}: {resp.text}",
    )
    detail = resp.json().get("detail")
    _assert(
        isinstance(detail, dict) and detail.get("code") == expected_code,
        f"{context}: expected detail.code={expected_code!r}, got {detail!r}",
    )


def _check_external_member_permissions(
    ctx: _lib.ComposeContext,
    admin_uid: str,
    admin_ws_id: str,
    ext_token: str,
    ext_uid: str,
) -> None:
    """Verify invited `external` member gates across audience, threads, settings, and monitors."""
    base = ctx.edge_url.rstrip("/")
    ext_headers = {"Authorization": f"Bearer {ext_token}"}

    # 1. Authenticated /credentials/status reports local mode + external tier + can_invite=False
    ext_creds = _lib.http_request(
        "GET", f"{base}/api/v1/dalton/credentials/status", headers=ext_headers
    )
    _assert(
        ext_creds.status == 200,
        f"Expected 200 on external /credentials/status, got {ext_creds.status}: {ext_creds.text}",
    )
    creds_data = ext_creds.json()
    _assert(
        creds_data.get("caller_tier") == "external"
        and creds_data.get("auth_mode") == "local"
        and creds_data.get("can_invite") is False,
        f"Unexpected external /credentials/status payload: {creds_data}",
    )

    # 2. File audience: external member cannot change artifact audience (public or organization)
    fake_artifact_id = str(uuid.uuid4())
    for audience in ("public", "organization"):
        aud_resp = _lib.http_request(
            "PUT",
            f"{base}/api/v1/artifacts/{fake_artifact_id}/audience",
            headers=ext_headers,
            json_body={"audience": audience},
        )
        _assert_forbidden_code(
            aud_resp,
            "external_member_forbidden",
            f"External PUT /artifacts/{{id}}/audience ({audience})",
        )

    # 3. Create external workspace + seed threads in admin and external workspaces
    ext_ws_resp = _lib.http_request(
        "POST",
        f"{base}/api/v1/workspaces",
        headers=ext_headers,
        json_body={"name": f"External Own WS {secrets.token_hex(3)}"},
    )
    _assert(
        ext_ws_resp.status in (200, 201),
        f"Expected 200/201 creating external workspace, got {ext_ws_resp.status}: {ext_ws_resp.text}",
    )
    ext_ws_id = str(ext_ws_resp.json().get("workspace_id") or "")
    _assert(bool(ext_ws_id), f"Missing workspace_id in external create response: {ext_ws_resp.text}")

    def _seed_thread(ws_id: str, owner_uid: str, title: str) -> str:
        tid = ctx.psql_query(
            f"""
            INSERT INTO conversation_threads
                (workspace_id, user_id, title, current_status, thread_index)
            SELECT '{ws_id}', '{owner_uid}', '{title}', 'completed',
                   COALESCE(MAX(thread_index), -1) + 1
            FROM conversation_threads
            WHERE workspace_id = '{ws_id}'
            RETURNING conversation_thread_id;
            """
        )
        _assert(bool(tid), f"Failed to seed thread in workspace {ws_id}")
        return tid

    admin_tid = _seed_thread(admin_ws_id, admin_uid, "b4-admin-thread")
    ext_tid = _seed_thread(ext_ws_id, ext_uid, "b4-ext-thread")

    # 4. Thread read, status, and share isolation (admin thread -> 404; own thread -> read 200, share on -> 403, share off -> 200)
    for label, method, path, body in (
        ("External GET admin thread", "GET", f"/api/v1/threads/{admin_tid}", None),
        ("External GET admin thread status", "GET", f"/api/v1/threads/{admin_tid}/status", None),
        ("External GET admin thread share", "GET", f"/api/v1/threads/{admin_tid}/share", None),
        (
            "External POST admin thread share",
            "POST",
            f"/api/v1/threads/{admin_tid}/share",
            {"is_shared": True},
        ),
    ):
        resp = _lib.http_request(method, f"{base}{path}", headers=ext_headers, json_body=body)
        _assert(
            resp.status == 404,
            f"{label}: expected 404, got {resp.status}: {resp.text}",
        )

    own_read = _lib.http_request("GET", f"{base}/api/v1/threads/{ext_tid}", headers=ext_headers)
    _assert(own_read.status == 200, f"Expected 200 reading own thread, got {own_read.status}: {own_read.text}")
    own_status = _lib.http_request(
        "GET", f"{base}/api/v1/threads/{ext_tid}/status", headers=ext_headers
    )
    _assert(
        own_status.status == 200,
        f"Expected 200 reading own thread status, got {own_status.status}: {own_status.text}",
    )

    own_share_on = _lib.http_request(
        "POST",
        f"{base}/api/v1/threads/{ext_tid}/share",
        headers=ext_headers,
        json_body={"is_shared": True},
    )
    _assert_forbidden_code(
        own_share_on,
        "external_member_forbidden",
        "External POST own thread share (is_shared=True)",
    )

    # Prepare an already-shared external own thread (simulating pre-demotion historical share),
    # confirm the public share link works before stopping, then send the legacy stop-share payload
    # carrying permissions={"audience": "organization"} and verify:
    # (a) is_shared becomes False, (b) share link returns 404, (c) original audience="public" is kept.
    ext_share_token = f"b4ext{secrets.token_hex(8)}"
    ctx.psql_exec(
        f"""
        UPDATE conversation_threads
           SET is_shared = TRUE,
               share_token = '{ext_share_token}',
               shared_at = NOW(),
               share_permissions = '{{"allow_files": false, "allow_download": false, "audience": "public"}}'::jsonb
         WHERE conversation_thread_id = '{ext_tid}'::uuid;
        """
    )
    public_before = _lib.http_request("GET", f"{base}/api/v1/public/shared/{ext_share_token}")
    _assert(
        public_before.status == 200 and public_before.json().get("thread_id") == ext_tid,
        f"Expected seeded external shared link to be accessible (200) before stop, "
        f"got {public_before.status}: {public_before.text}",
    )

    own_share_off = _lib.http_request(
        "POST",
        f"{base}/api/v1/threads/{ext_tid}/share",
        headers=ext_headers,
        json_body={"is_shared": False, "permissions": {"audience": "organization"}},
    )
    _assert(
        own_share_off.status == 200,
        f"Expected 200 stopping own thread share, got {own_share_off.status}: {own_share_off.text}",
    )
    share_off_data = own_share_off.json()
    _assert(
        share_off_data.get("is_shared") is False
        and share_off_data.get("share_token") is None
        and (share_off_data.get("permissions") or {}).get("audience") == "public",
        f"Unexpected stop-share response payload (expected is_shared=False & audience='public'): "
        f"{share_off_data}",
    )

    share_after_get = _lib.http_request(
        "GET", f"{base}/api/v1/threads/{ext_tid}/share", headers=ext_headers
    )
    _assert(
        share_after_get.status == 200
        and share_after_get.json().get("is_shared") is False
        and (share_after_get.json().get("permissions") or {}).get("audience") == "public",
        f"Expected persisted is_shared=False & audience='public' after stop, "
        f"got {share_after_get.status}: {share_after_get.text}",
    )
    stored_audience = ctx.psql_query(
        f"SELECT COALESCE(share_permissions->>'audience', '<missing>') FROM conversation_threads "
        f"WHERE conversation_thread_id = '{ext_tid}';"
    ).strip()
    _assert(
        stored_audience == "public",
        f"Expected stored share_permissions.audience to stay 'public' after stop, got {stored_audience!r}",
    )
    public_after = _lib.http_request("GET", f"{base}/api/v1/public/shared/{ext_share_token}")
    _assert(
        public_after.status == 404,
        f"Expected stopped external share link to return 404, got {public_after.status}: {public_after.text}",
    )

    # 5. User settings: neutral 200 on /sharing/user-settings (colleague_sharing off); 404 on disabled writes & /user-settings
    sharing_user_get = _lib.http_request(
        "GET", f"{base}/api/v1/dalton/sharing/user-settings", headers=ext_headers
    )
    _assert(
        sharing_user_get.status == 200,
        f"Expected 200 neutral on GET /sharing/user-settings, got {sharing_user_get.status}: {sharing_user_get.text}",
    )
    su_data = sharing_user_get.json()
    _assert(
        su_data.get("caller_tier") == "external"
        and su_data.get("can_edit") is False
        and su_data.get("colleague_dispatch_enabled") is False,
        f"Unexpected external GET /sharing/user-settings payload: {su_data}",
    )
    sharing_user_put = _lib.http_request(
        "PUT",
        f"{base}/api/v1/dalton/sharing/user-settings",
        headers=ext_headers,
        json_body={"cos_rw_enabled": True},
    )
    _assert(
        sharing_user_put.status == 404,
        f"Expected 404 on PUT /sharing/user-settings in Core, got {sharing_user_put.status}: {sharing_user_put.text}",
    )
    user_settings_get = _lib.http_request(
        "GET", f"{base}/api/v1/dalton/user-settings", headers=ext_headers
    )
    _assert(
        user_settings_get.status == 404,
        f"Expected 404 on GET /user-settings when conversation_archive is off, got {user_settings_get.status}",
    )
    user_settings_put = _lib.http_request(
        "PUT",
        f"{base}/api/v1/dalton/user-settings",
        headers=ext_headers,
        json_body={"home_team_memory_enabled": True},
    )
    _assert(
        user_settings_put.status == 404,
        f"Expected 404 on PUT /user-settings when conversation_archive is off, got {user_settings_put.status}",
    )

    # 6. Workspace settings & sharing: own PUT /settings -> 403 external_member_forbidden; admin workspace -> 404
    ext_ws_settings_get = _lib.http_request(
        "GET", f"{base}/api/v1/dalton/workspaces/{ext_ws_id}/settings", headers=ext_headers
    )
    _assert(
        ext_ws_settings_get.status == 200,
        f"Expected 200 on external own GET /workspaces/{{id}}/settings, got {ext_ws_settings_get.status}: {ext_ws_settings_get.text}",
    )
    ws_st = ext_ws_settings_get.json()
    _assert(
        ws_st.get("owner_external") is True
        and ws_st.get("can_edit") is False
        and ws_st.get("can_edit_web_access") is True
        and ws_st.get("can_edit_cos_access") is False
        and ws_st.get("can_edit_team_memory") is False,
        f"Unexpected external own GET /workspaces/{{id}}/settings payload: {ws_st}",
    )
    ext_ws_settings_put = _lib.http_request(
        "PUT",
        f"{base}/api/v1/dalton/workspaces/{ext_ws_id}/settings",
        headers=ext_headers,
        json_body={"team_memory_enabled": True},
    )
    _assert_forbidden_code(
        ext_ws_settings_put,
        "external_member_forbidden",
        "External PUT own /workspaces/{id}/settings",
    )
    ext_ws_sharing_get = _lib.http_request(
        "GET", f"{base}/api/v1/dalton/workspaces/{ext_ws_id}/sharing", headers=ext_headers
    )
    _assert(
        ext_ws_sharing_get.status == 200 and ext_ws_sharing_get.json().get("can_edit") is False,
        f"Expected 200 neutral on external own GET /workspaces/{{id}}/sharing, got {ext_ws_sharing_get.status}: {ext_ws_sharing_get.text}",
    )
    ext_ws_sharing_put = _lib.http_request(
        "PUT",
        f"{base}/api/v1/dalton/workspaces/{ext_ws_id}/sharing",
        headers=ext_headers,
        json_body={"cos_access": "ro"},
    )
    _assert(
        ext_ws_sharing_put.status == 404,
        f"Expected 404 on PUT /workspaces/{{id}}/sharing in Core, got {ext_ws_sharing_put.status}",
    )

    for label, method, path, body in (
        (
            "External GET admin workspace settings",
            "GET",
            f"/api/v1/dalton/workspaces/{admin_ws_id}/settings",
            None,
        ),
        (
            "External PUT admin workspace settings",
            "PUT",
            f"/api/v1/dalton/workspaces/{admin_ws_id}/settings",
            {"team_memory_enabled": True},
        ),
        (
            "External GET admin workspace sharing",
            "GET",
            f"/api/v1/dalton/workspaces/{admin_ws_id}/sharing",
            None,
        ),
    ):
        resp = _lib.http_request(method, f"{base}{path}", headers=ext_headers, json_body=body)
        _assert(
            resp.status == 404,
            f"{label}: expected 404, got {resp.status}: {resp.text}",
        )

    # 7. Monitor publish: external member cannot publish non-own visibilities (public / company)
    for visibility in ("public", "company"):
        mon_resp = _lib.http_request(
            "POST",
            f"{base}/api/v1/dalton/monitors",
            headers=ext_headers,
            json_body={
                "workspace_id": ext_ws_id,
                "path": "report.html",
                "visibility": visibility,
            },
        )
        _assert_forbidden_code(
            mon_resp,
            "external_member_forbidden",
            f"External POST /dalton/monitors (visibility={visibility})",
        )


def _check_member_llm_egress(
    ctx: _lib.ComposeContext, admin_token: str, member_token: str
) -> None:
    base = ctx.edge_url.rstrip("/")
    admin_headers = {"Authorization": f"Bearer {admin_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}

    blocked_urls = [
        "http://127.0.0.1:8000/v1",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1:8080/v1",
    ]
    for role_label, headers in (("member", member_headers), ("admin", admin_headers)):
        for url in blocked_urls:
            resp = _lib.http_request(
                "POST",
                f"{base}/api/v1/providers/custom/models",
                headers=headers,
                json_body={
                    "api_format": "openai-completions",
                    "base_url": url,
                    "api_key": "sk-test-dummy",
                },
            )
            _assert(
                resp.status == 400,
                f"Expected 400 refusing {role_label} private/metadata URL {url}, "
                f"got {resp.status}: {resp.text}",
            )

    # Allowlist check MUST NOT skip when DALTON_OPERATOR_LLM_ENDPOINTS is unset; fail directly.
    raw_allowlist = (
        ctx.env.get("DALTON_OPERATOR_LLM_ENDPOINTS")
        or os.environ.get("DALTON_OPERATOR_LLM_ENDPOINTS")
        or ""
    )
    allowed_urls = [item.strip() for item in raw_allowlist.split(",") if item.strip()]
    _assert(
        bool(allowed_urls),
        "DALTON_OPERATOR_LLM_ENDPOINTS must be configured in CI (cannot skip allowlist check)",
    )

    allowed_url = allowed_urls[0]
    expected_model = (
        os.environ.get("FAKE_LLM_MODEL")
        or ctx.env.get("FAKE_LLM_MODEL")
        or "gpt-6-luna"
    )
    resp_ok = _lib.http_request(
        "POST",
        f"{base}/api/v1/providers/custom/models",
        headers=member_headers,
        json_body={
            "api_format": "openai-completions",
            "base_url": allowed_url,
            "api_key": "sk-test-dummy",
        },
    )
    _assert(
        resp_ok.status == 200,
        f"Allowlisted operator endpoint {allowed_url} expected HTTP 200, "
        f"got {resp_ok.status}: {resp_ok.text}",
    )
    payload = resp_ok.json()
    _assert(
        payload.get("status") == "ok",
        f"Allowlisted operator endpoint {allowed_url} did not return status='ok': {payload}",
    )
    discovered_ids: set[str] = set()
    for bucket in ("recognized", "others"):
        for item in payload.get(bucket) or []:
            if isinstance(item, str):
                discovered_ids.add(item)
            elif isinstance(item, dict):
                for key in ("id", "model_id", "name"):
                    val = item.get(key)
                    if isinstance(val, str) and val:
                        discovered_ids.add(val)
    _assert(
        expected_model in discovered_ids,
        f"Allowlisted fake-llm endpoint {allowed_url} did not return expected model "
        f"{expected_model!r}; discovered={sorted(discovered_ids)} payload={payload}",
    )


def main() -> int:
    ctx = _lib.ComposeContext.from_environment()
    _lib.wait_for_http_ready(
        f"{ctx.edge_url.rstrip('/')}/api/v1/dalton/bootstrap/status", timeout=60.0
    )

    heads = set(ctx.get_alembic_heads())
    db_versions = set(ctx.get_db_alembic_versions())
    _assert(
        heads == db_versions,
        f"Alembic heads mismatch: expected={sorted(heads)} actual={sorted(db_versions)}",
    )

    _check_unauthenticated_surface(ctx)
    admin_token, admin_uid, _admin_email = _check_bootstrap_and_sessions(ctx)
    _internal_token, _internal_uid, ext_token, ext_uid = _check_tiered_invites_and_password_change(
        ctx, admin_token
    )
    admin_ws_id = _check_dual_user_isolation(ctx, admin_token, ext_token)
    _check_external_member_permissions(ctx, admin_uid, admin_ws_id, ext_token, ext_uid)
    _check_member_llm_egress(ctx, admin_token, ext_token)
    print("b4_local_auth: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
