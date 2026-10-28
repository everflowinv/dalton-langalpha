"""
Antigravity OAuth service — loopback authorization-code + PKCE flow (plus
manual callback paste fallback for Docker deployments), token exchange, and
refresh with Redis lock.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from src.server.database.oauth_tokens import (
    get_oauth_tokens,
    upsert_oauth_tokens,
)

logger = logging.getLogger(__name__)

# --- Constants (matching the official Antigravity CLI `agy` 1.4.2) ---

ANTIGRAVITY_PROVIDER = "antigravity-oauth"
ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"

ANTIGRAVITY_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/auth"
ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"

ANTIGRAVITY_SCOPES = " ".join(
    [
        "https://www.googleapis.com/auth/aicode",
        "https://www.googleapis.com/auth/cclog",
        "https://www.googleapis.com/auth/cloud-platform",
        "openid",
        "https://www.googleapis.com/auth/userinfo.profile",
        "https://www.googleapis.com/auth/experimentsandconfigs",
        "https://www.googleapis.com/auth/userinfo.email",
    ]
)

_CALLBACK_TIMEOUT_S = 600  # listener lives 10 minutes, matching Redis TTL


async def _record_provider_event_safe(user_id: str | None, kind: str, detail: dict) -> None:
    try:
        from src.dalton.database.provider_events import record_provider_event

        await record_provider_event(
            provider=ANTIGRAVITY_PROVIDER,
            kind=kind,
            user_id=user_id,
            detail=detail,
            best_effort=True,
        )
    except Exception as exc:
        logger.debug("[antigravity_oauth] record_provider_event skipped: %s", type(exc).__name__)


async def _get_latest_oauth_login_start(user_id: str) -> datetime | None:
    """Read the most recent `oauth_login_start` timestamp for `user_id` from `dalton_provider_events` (P0I §3 P3)."""
    try:
        from src.dalton.database.provider_events import list_recent_provider_events

        rows = await list_recent_provider_events(
            ANTIGRAVITY_PROVIDER,
            user_id=user_id,
            kinds=("oauth_login_start",),
            limit=5,
        )
        for row in rows:
            if row.get("user_id") not in (user_id, None):
                continue
            detail = row.get("detail")
            if isinstance(detail, dict) and detail.get("initiated_at"):
                raw_ts = str(detail["initiated_at"]).strip()
                if raw_ts.endswith("Z"):
                    raw_ts = raw_ts[:-1] + "+00:00"
                dt = datetime.fromisoformat(raw_ts)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            created_at = row.get("created_at")
            if isinstance(created_at, datetime):
                return (
                    created_at
                    if created_at.tzinfo
                    else created_at.replace(tzinfo=timezone.utc)
                )
    except Exception as exc:
        logger.debug(
            "[antigravity_oauth] _get_latest_oauth_login_start skipped: %s",
            type(exc).__name__,
        )
    return None


def _generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
    params = {
        "client_id": ANTIGRAVITY_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ANTIGRAVITY_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return f"{ANTIGRAVITY_AUTHORIZE_URL}?{urlencode(params)}"


def parse_id_token_claims(id_token: str) -> dict:
    """Decode a Google id_token payload without verification (we trust the
    token endpoint, same trust model as the codex provider)."""
    empty = {"email": None, "name": None, "sub": None}
    if not id_token:
        return empty
    parts = id_token.split(".")
    if len(parts) < 2:
        return empty
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        return empty
    return {
        "email": payload.get("email"),
        "name": payload.get("name"),
        "sub": payload.get("sub"),
    }


async def exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict:
    """Exchange the loopback authorization code for tokens."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            ANTIGRAVITY_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": ANTIGRAVITY_CLIENT_ID,
                "client_secret": ANTIGRAVITY_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code >= 400:
            logger.error(
                f"[antigravity_oauth] Token exchange failed: status={resp.status_code}"
            )
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "id_token": data.get("id_token", ""),
            "expires_in": data.get("expires_in", 3600),
        }


async def refresh_tokens(refresh_token: str) -> dict:
    """Use the refresh token to get a new access token."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            ANTIGRAVITY_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": ANTIGRAVITY_CLIENT_ID,
                "client_secret": ANTIGRAVITY_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "id_token": data.get("id_token", ""),
            "expires_in": data.get("expires_in", 3600),
        }


def _flow_keys(user_id: str) -> tuple[str, str]:
    """(flow_key, done_key) of the pending Antigravity login.

    DALTON-HOOK(team-credentials): keyed by the credential holder, so any
    member's disconnect clears the same pending flow a member's sign-in waits
    on — one shared credential, one pending flow."""
    from src.dalton.credentials import credential_user_id

    holder = credential_user_id(user_id)
    return f"oauth:antigravity:{holder}", f"oauth:antigravity:done:{holder}"


async def _finalize_token_save(user_id: str, tokens: dict, expected_flow_raw: str) -> dict:
    """Store a finished sign-in, only while THE SAME login is still pending.

    ``expected_flow_raw`` is the flow-key value the caller read before it
    spent the authorization. The claim (compare-and-delete on exactly that
    value) runs inside the connection lock and the tokens are written over
    that connection, so: a disconnect that landed while Google was answering
    leaves the key gone and this save aborts; a newer login that replaced the
    flow keeps its own key, and this old authorization cannot claim it.
    With no cache there is no verifiable flow, so nothing is stored — an
    unverifiable save is exactly the revive-on-disconnect race
    (DALTON-HOOK(team-credentials)).
    """
    from src.server.database.oauth_tokens import connection_change
    from src.utils.cache.redis_cache import get_cache_client

    claims = parse_id_token_claims(tokens.get("id_token", ""))
    cache = get_cache_client()
    flow_key, done_key = _flow_keys(user_id)
    if not cache.enabled or not cache.client:
        raise ValueError(
            "The Antigravity sign-in cannot be verified without its pending-flow "
            "store; start again with the cache available."
        )

    async def _save(conn) -> bool:
        # Claim on the value THIS login read: gone (disconnected) or replaced
        # (a newer login) both refuse, and the newer flow's key survives.
        return await cache.claim(flow_key, expected_flow_raw)

    async with connection_change(user_id, ANTIGRAVITY_PROVIDER) as conn:
        claimed = await _save(conn)
        if not claimed:
            raise ValueError("The Antigravity sign-in was cancelled or replaced; start again.")
        await upsert_oauth_tokens(
            user_id=user_id,
            provider=ANTIGRAVITY_PROVIDER,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
            account_id=claims.get("sub") or "",
            email=claims.get("email"),
            plan_type="antigravity",
            expires_at=datetime.now(timezone.utc)
            + timedelta(seconds=tokens.get("expires_in", 3600)),
            conn=conn,
        )
    await cache.client.set(done_key, "1", ex=600)
    return claims


async def start_login(user_id: str) -> dict:
    """Begin a login: PKCE pair + one-shot loopback callback listener."""
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    now_dt = datetime.now(timezone.utc)
    initiated_at = now_dt.isoformat()
    await _record_provider_event_safe(
        user_id,
        "oauth_login_start",
        {"initiated_at": initiated_at},
    )

    server_future = asyncio.get_running_loop().create_future()

    async def handle_connection(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await _handle_google_callback(reader, writer, user_id, verifier, state, server_future)
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass

    server = await asyncio.start_server(handle_connection, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    redirect_uri = f"http://localhost:{port}/callback"
    server_future.set_result(server)

    async def _serve_with_timeout() -> None:
        try:
            await asyncio.sleep(_CALLBACK_TIMEOUT_S)
        finally:
            server.close()
            await server.wait_closed()

    asyncio.create_task(_serve_with_timeout())

    from src.server.database.oauth_tokens import connection_change
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if cache.enabled and cache.client:
        flow_key, done_key = _flow_keys(user_id)
        payload = json.dumps(
            {
                "verifier": verifier,
                "state": state,
                "port": port,
                "redirect_uri": redirect_uri,
                "initiated_at": initiated_at,
            }
        )
        # A new flow replaces the old one inside the connection lock, so it can
        # never land between a save's claim and its write, and a disconnect
        # queued behind this login sees the new flow, not a half-cleared one
        # (DALTON-HOOK(team-credentials)). R6-9: the stale completion flag
        # goes with the same step.
        async with connection_change(user_id, ANTIGRAVITY_PROVIDER):
            await cache.client.delete(done_key)
            await cache.client.set(flow_key, payload, ex=_CALLBACK_TIMEOUT_S)

    return {
        "authorize_url": build_authorize_url(redirect_uri, state, challenge),
        "port": port,
    }


def parse_callback_input(raw_input: str) -> tuple[str, str, str | None]:
    """Parse pasted callback URL or query string into (code, state, redirect_uri).

    Supports:
    - Full callback URL: http://localhost:54321/callback?code=...&state=...
    - Query string: code=...&state=...
    - Code#state: code#state
    """
    text = (raw_input or "").strip()
    if not text:
        raise ValueError("Callback input is empty.")

    redirect_uri: str | None = None
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        qs = parse_qs(parsed.query)
        if parsed.scheme and parsed.netloc:
            redirect_uri = f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/callback'}"
    elif "?" in text or "&" in text or "code=" in text:
        qpart = text.split("?", 1)[1] if "?" in text else text
        qs = parse_qs(qpart)
    elif "#" in text:
        code_part, state_part = text.split("#", 1)
        return code_part.strip(), state_part.strip(), None
    else:
        raise ValueError("Expected full callback URL (http://localhost:<port>/callback?code=...&state=...).")

    if "error" in qs:
        err_desc = qs.get("error_description", qs.get("error", ["unknown"]))[0]
        raise ValueError(f"Authorization failed: {err_desc}")
    code = (qs.get("code") or [""])[0].strip()
    state = (qs.get("state") or [""])[0].strip()
    if not code:
        raise ValueError("Callback URL does not contain an authorization 'code' parameter.")
    if not state:
        raise ValueError("Callback URL does not contain a 'state' parameter.")
    return code, state, redirect_uri


async def submit_callback_url(user_id: str, callback_input: str) -> dict:
    """Complete Antigravity OAuth from a manually pasted callback URL (R6-5 Docker fallback)."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise RuntimeError("Cache unavailable for OAuth callback verification.")

    flow_key, _done_key = _flow_keys(user_id)
    raw = await cache.client.get(flow_key)
    if not raw:
        raise ValueError("No pending Antigravity authorization. Please initiate login again.")
    if isinstance(raw, bytes):
        raw = raw.decode()
    pending = json.loads(raw)

    code, returned_state, parsed_redirect = parse_callback_input(callback_input)
    expected_state = pending.get("state") or ""
    # R6-7: require non-empty matching state
    if not returned_state or returned_state != expected_state:
        raise ValueError("State mismatch — possible CSRF or stale login tab. Please initiate again.")

    redirect_uri = (
        pending.get("redirect_uri")
        or parsed_redirect
        or f"http://localhost:{pending.get('port')}/callback"
    )
    tokens = await exchange_code(code, pending["verifier"], redirect_uri)
    # The save must land only on this very flow: a disconnect or a newer login
    # that replaced it while the exchange ran refuses below
    # (DALTON-HOOK(team-credentials)).
    claims = await _finalize_token_save(user_id, tokens, expected_flow_raw=raw)
    logger.info(f"[antigravity_oauth] Connected via callback paste for user_id={user_id}")
    return {
        "success": True,
        "email": claims.get("email"),
        "plan_type": "antigravity",
    }


async def _handle_google_callback(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    user_id: str,
    verifier: str,
    state: str,
    server_future: "asyncio.Future[asyncio.AbstractServer]",
) -> None:
    """Parse one loopback HTTP request from Google, finish the login, respond."""
    reasons = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}

    def respond(status: int, title: str, detail: str) -> None:
        body = (
            f"<html><head><title>{title}</title></head>"
            f'<body style="font-family:sans-serif;text-align:center;padding-top:4rem">'
            f"<h2>{title}</h2><p>{detail}</p>"
            f"<p>You can close this tab and return to LangAlpha.</p></body></html>"
        ).encode()
        writer.write(
            f"HTTP/1.1 {status} {reasons.get(status, 'OK')}\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )

    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=10)
        request_target = (
            request_line.decode("latin-1").split(" ")[1] if b" " in request_line else ""
        )
        while True:  # drain headers
            header = await asyncio.wait_for(reader.readline(), timeout=10)
            if header in (b"\r\n", b"\n", b""):
                break

        qs = parse_qs(request_target.split("?", 1)[1] if "?" in request_target else "")

        if "error" in qs:
            respond(
                400,
                "Authorization failed",
                str(qs.get("error_description", qs.get("error", ["unknown"]))[0]),
            )
            return
        if "code" not in qs:
            respond(404, "Not found", "Expected Google's OAuth callback at /callback.")
            return

        code = qs["code"][0]
        returned_state = qs.get("state", [""])[0]
        # R6-7: require non-empty state matching expected state
        if not returned_state or returned_state != state:
            respond(400, "State mismatch", "Please restart the connection from LangAlpha.")
            return

        # Bind this callback to the login that opened the listener, by the
        # pending flow's own record, before spending the authorization: the
        # key must still stand and carry THIS verifier/state, or this is an
        # old listener answering after a disconnect / newer login
        # (DALTON-HOOK(team-credentials)).
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        flow_key, _done_key = _flow_keys(user_id)
        flow_raw = await cache.client.get(flow_key) if cache.enabled and cache.client else None
        if isinstance(flow_raw, bytes):
            flow_raw = flow_raw.decode("utf-8", "ignore")
        if not flow_raw:
            respond(400, "Sign-in cancelled", "This sign-in was cancelled or replaced. Start again from LangAlpha.")
            return
        pending_flow = json.loads(flow_raw)
        if pending_flow.get("verifier") != verifier or pending_flow.get("state") != state:
            respond(400, "Sign-in replaced", "A newer sign-in is in progress. Start again from LangAlpha.")
            return

        port = writer.get_extra_info("sockname")[1]
        tokens = await exchange_code(code, verifier, f"http://localhost:{port}/callback")
        claims = await _finalize_token_save(user_id, tokens, expected_flow_raw=flow_raw)

        respond(
            200,
            "Google Antigravity connected",
            f"Signed in as {claims.get('email') or 'your Google account'}.",
        )
        logger.info(f"[antigravity_oauth] Connected for user_id={user_id}")

        if server_future.done():
            server = server_future.result()
            server.close()
            await server.wait_closed()
    except Exception as e:
        logger.error(f"[antigravity_oauth] Callback handling failed: {type(e).__name__}")
        try:
            respond(500, "Connection failed", "Please try again from LangAlpha.")
        except Exception:
            pass


async def check_login_complete(user_id: str) -> bool:
    """Whether the loopback callback or manual paste finished a successful login."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if cache.enabled and cache.client:
        _flow_key, done_key = _flow_keys(user_id)
        return bool(await cache.client.get(done_key))
    # Redis unavailable: fall back to a DB lookup, comparing updated_at against initiated_at (R2-N5N6 #7).
    tokens = await get_oauth_tokens(user_id, ANTIGRAVITY_PROVIDER)
    if not tokens or not tokens.get("access_token"):
        return False
    initiated_at = await _get_latest_oauth_login_start(user_id)
    updated_at = tokens.get("updated_at")
    if initiated_at is not None and isinstance(updated_at, datetime):
        upd = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=timezone.utc)
        if upd < initiated_at:
            return False
    return True
