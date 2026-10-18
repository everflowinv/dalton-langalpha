"""
Antigravity OAuth service — loopback authorization-code + PKCE flow, token
exchange, refresh with Redis lock.

Connects Google Antigravity subscription models via users' existing Antigravity
/ Google One AI plans, speaking the same endpoints the official `agy` CLI
uses (protocol details reverse-engineered from the CLI 1.4.2 binary and
verified against live traffic):

- Client ID/secret: the `agy` installed-app client (Google "installed" clients
  embed a non-secret client secret; it is not treated as confidential).
- This client is NOT registered for the device-code flow (`invalid_client`),
  so login uses the browser authorization-code flow with an RFC 8252 loopback
  redirect: Google redirects to http://localhost:<port>/callback served by
  this process. This requires the browser and the LangAlpha server to share
  a machine (the normal self-hosted deployment).
- Model calls: POST {server}/v1internal:streamGenerateContent — see
  src/llms/extension/antigravity.py.
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode

import httpx

from src.server.database.oauth_tokens import (
    get_oauth_tokens,
    invalidate_oauth_active_cache,
    upsert_oauth_tokens,
)
from src.server.services.oauth_refresh_wait import (
    REFRESH_BUFFER,
    REFRESH_LOCK_TTL_MS,
    await_refreshed_tokens,
    token_expiry,
)

logger = logging.getLogger(__name__)

# --- Constants (matching the official Antigravity CLI `agy` 1.4.2) ---

ANTIGRAVITY_PROVIDER = "antigravity-oauth"
ANTIGRAVITY_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
ANTIGRAVITY_CLIENT_SECRET = "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"

ANTIGRAVITY_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/auth"
ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scope set observed on a live `agy`-issued refresh token. `aicode` is the
# Code Assist/Antigravity scope the model endpoint authorizes on.
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


# --- PKCE + authorize URL ---


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


# --- JWT claims (Google id_token) ---


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


# --- Token exchange / refresh ---


async def exchange_code(code: str, code_verifier: str, redirect_uri: str) -> dict:
    """Exchange the loopback authorization code for tokens.

    Returns:
        {access_token, refresh_token, id_token, expires_in}
    """
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
                f"[antigravity_oauth] Token exchange failed: status={resp.status_code} body={resp.text[:300]}"
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
    """Use the refresh token to get a new access token.

    Google does not rotate refresh tokens; the response may omit
    refresh_token, in which case the caller keeps the existing one.

    Returns:
        {access_token, refresh_token, id_token, expires_in}
    """
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


# --- Loopback login flow ---


async def start_login(user_id: str) -> dict:
    """Begin a login: PKCE pair + one-shot loopback callback listener.

    The listener serves Google's redirect on 127.0.0.1:<ephemeral port>,
    completes the token exchange itself, and shuts down. The frontend polls
    ``check_login_complete`` until it flips.

    Returns:
        {authorize_url, port}
    """
    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(24)

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

    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if cache.enabled and cache.client:
        await cache.client.set(
            f"oauth:antigravity:{user_id}",
            json.dumps({"verifier": verifier, "state": state, "port": port}),
            ex=_CALLBACK_TIMEOUT_S,
        )

    return {
        "authorize_url": build_authorize_url(redirect_uri, state, challenge),
        "port": port,
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
        if returned_state and returned_state != state:
            respond(400, "State mismatch", "Please restart the connection from LangAlpha.")
            return

        port = writer.get_extra_info("sockname")[1]
        tokens = await exchange_code(code, verifier, f"http://localhost:{port}/callback")
        claims = parse_id_token_claims(tokens.get("id_token", ""))

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
        )
        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass

        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        if cache.enabled and cache.client:
            await cache.client.set(f"oauth:antigravity:done:{user_id}", "1", ex=600)

        respond(
            200,
            "Google Antigravity connected",
            f"Signed in as {claims.get('email') or 'your Google account'}.",
        )
        logger.info(f"[antigravity_oauth] Connected for user_id={user_id}")

        # Login complete — retire the listener.
        if server_future.done():
            server = server_future.result()
            server.close()
            await server.wait_closed()
    except Exception as e:
        logger.error(f"[antigravity_oauth] Callback handling failed: {e}")
        try:
            respond(500, "Connection failed", "Please try again from LangAlpha.")
        except Exception:
            pass


async def check_login_complete(user_id: str) -> bool:
    """Whether the loopback callback finished a successful login."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if cache.enabled and cache.client:
        return bool(await cache.client.get(f"oauth:antigravity:done:{user_id}"))
    # Redis unavailable: fall back to a DB lookup (decrypts, slower but rare).
    tokens = await get_oauth_tokens(user_id, ANTIGRAVITY_PROVIDER)
    return bool(tokens and tokens.get("access_token"))


# --- Orchestrator with refresh-lock (mirrors codex/claude) ---


async def get_valid_token(user_id: str) -> dict | None:
    """
    Get a valid access token, refreshing if needed.

    Uses Redis SETNX lock to prevent concurrent refresh.
    Returns None if user hasn't connected.
    """
    tokens = await get_oauth_tokens(user_id, ANTIGRAVITY_PROVIDER)
    if not tokens:
        return None

    now = datetime.now(timezone.utc)

    if token_expiry(tokens) > now + REFRESH_BUFFER:
        return {"access_token": tokens["access_token"], "plan_type": tokens.get("plan_type")}

    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    lock_key = f"oauth:refresh:{user_id}:{ANTIGRAVITY_PROVIDER}"
    # Released only by its owner, so a refresh that outlives the lock cannot
    # free the next holder's. None (Redis unavailable) refreshes uncoordinated.
    lock_token = secrets.token_hex(16)
    acquired = await cache.acquire_lock(lock_key, lock_token, REFRESH_LOCK_TTL_MS)
    if acquired is False:
        tokens = await await_refreshed_tokens(user_id, ANTIGRAVITY_PROVIDER)
        if tokens:
            return {"access_token": tokens["access_token"], "plan_type": tokens.get("plan_type")}
        return None

    try:
        new = await refresh_tokens(tokens["refresh_token"])
        new_expires = now + timedelta(seconds=new.get("expires_in", 3600))
        claims = parse_id_token_claims(new.get("id_token", ""))

        await upsert_oauth_tokens(
            user_id=user_id,
            provider=ANTIGRAVITY_PROVIDER,
            access_token=new["access_token"],
            refresh_token=new["refresh_token"] or tokens["refresh_token"],
            account_id=tokens.get("account_id", ""),
            email=claims.get("email") or tokens.get("email"),
            plan_type=tokens.get("plan_type"),
            expires_at=new_expires,
        )

        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass

        logger.debug(f"[antigravity_oauth] Refreshed tokens for user_id={user_id}")
        return {"access_token": new["access_token"], "plan_type": tokens.get("plan_type")}
    except Exception as e:
        logger.error(f"[antigravity_oauth] Token refresh failed for user_id={user_id}: {e}")
        return None
    finally:
        if acquired:
            await cache.release_lock(lock_key, lock_token)
