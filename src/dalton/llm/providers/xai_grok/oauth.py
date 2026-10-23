"""xAI Grok OAuth service — OpenID discovery, RFC 8628 device code flow,
token refresh with Cloudflare retry + Redis lock, model list, and billing quota.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from src.dalton.settings import get_xai_grok_client_version, get_xai_oauth_client_id
from src.server.database.oauth_tokens import (
    get_oauth_tokens,
    invalidate_oauth_active_cache,
    upsert_oauth_tokens,
)
from src.server.services.oauth_refresh_wait import (
    REFRESH_BUFFER,
    token_expiry,
)

logger = logging.getLogger(__name__)

XAI_PROVIDER = "xai-oauth"
XAI_PLAN_TYPE = "supergrok"
XAI_ISSUER = "https://auth.x.ai"
XAI_OPENID_CONFIG_URL = f"{XAI_ISSUER}/.well-known/openid-configuration"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
XAI_PROXY_BASE_URL = "https://cli-chat-proxy.grok.com/v1"

# Read from DALTON_XAI_OAUTH_CLIENT_ID with no default value in repository.
XAI_OAUTH_CLIENT_ID: str = os.getenv("DALTON_XAI_OAUTH_CLIENT_ID", "")
_IMPORT_CLIENT_ID = XAI_OAUTH_CLIENT_ID

DEFAULT_POLL_INTERVAL_SECONDS = 5
SLOW_DOWN_INCREMENT_SECONDS = 5
MAX_POLL_WAIT_SECONDS = 300
CF_MAX_RETRIES = 3
CF_RETRY_DELAY_SECONDS = 0.25
DISCOVERY_CACHE_TTL_SECONDS = 3600

# Lock TTL (180s) covers worst-case OpenID discovery (30s) + up to 4 refresh
# requests (4 * 30s + Cloudflare backoff) + DB save.
# The lock holder bounds its entire flow with `asyncio.timeout(XAI_REFRESH_HARD_TIMEOUT_S)`
# (120s, strictly less than the 180s lock TTL) so a slow refresh aborts before
# the lock can expire; waiters poll for up to 130s (> 120s hard timeout) at >= 0.5s intervals.
XAI_REFRESH_LOCK_TTL_MS = 180_000
XAI_REFRESH_HARD_TIMEOUT_S = 120.0
_REFRESH_WAIT_SECONDS = 130.0
_REFRESH_POLL_SECONDS = 0.5

_RELEASE_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_RENEW_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""

_VALID_XAI_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def _resolve_client_id() -> str:
    """Resolve `XAI_OAUTH_CLIENT_ID` from module override or `DALTON_XAI_OAUTH_CLIENT_ID` env."""
    if XAI_OAUTH_CLIENT_ID != _IMPORT_CLIENT_ID and XAI_OAUTH_CLIENT_ID.strip():
        return XAI_OAUTH_CLIENT_ID.strip()
    val = get_xai_oauth_client_id() or XAI_OAUTH_CLIENT_ID.strip()
    if not val:
        raise ValueError("DALTON_XAI_OAUTH_CLIENT_ID environment variable is not set")
    return val


def validate_xai_endpoint(url: str, field_name: str = "endpoint") -> str:
    """Ensure `url` is strict `https://` with hostname `x.ai` or `*.x.ai` (no userinfo, port, backslash, null byte, or uppercase host/scheme)."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"Missing xAI OAuth {field_name}")
    cleaned = url.strip()
    lowered = cleaned.lower()
    if (
        not cleaned.startswith("https://")
        or "\\" in cleaned
        or "\x00" in cleaned
        or "%00" in lowered
        or "%5c" in lowered
    ):
        raise ValueError(
            f"Invalid xAI OAuth {field_name}: {cleaned!r} (must use https and host x.ai or *.x.ai)"
        )
    parsed = urlparse(cleaned)
    netloc = parsed.netloc
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not netloc
        or netloc != netloc.lower()
        or "@" in netloc
        or ":" in netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or netloc != host
        or not (host == "x.ai" or (host.endswith(".x.ai") and len(host) > len(".x.ai")))
        or any(not _VALID_XAI_HOST_LABEL_RE.match(label) for label in host.split("."))
    ):
        raise ValueError(
            f"Invalid xAI OAuth {field_name}: {cleaned!r} (must use https and host x.ai or *.x.ai)"
        )
    return cleaned


def parse_jwt_claims(token: str) -> dict[str, Any]:
    """Decode JWT payload claims without logging or exposing the token."""
    if not isinstance(token, str) or "." not in token:
        return {}
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    payload_b64 = parts[1]
    padding = "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64 + padding)
        claims = json.loads(raw.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def extract_token_record(
    data: dict[str, Any],
    *,
    token_endpoint: str,
    fallback_refresh: str = "",
) -> dict[str, Any]:
    """Normalize OAuth token response into stored record fields.

    Preserves `access`, `refresh`, `expires` (from `expires_in`, falling back to
    JWT `exp`), `email`, `displayName`, `accountId` (JWT `sub`), and `token_endpoint`.
    """
    validated_endpoint = validate_xai_endpoint(token_endpoint, "token_endpoint")
    access_token = str(data.get("access_token") or data.get("access") or "")
    if not access_token:
        raise ValueError("xAI token response missing access_token")

    refresh_token = str(
        data.get("refresh_token") or data.get("refresh") or fallback_refresh or ""
    )
    access_claims = parse_jwt_claims(access_token)
    id_token = data.get("id_token")
    id_claims = parse_jwt_claims(id_token) if isinstance(id_token, str) and id_token else {}

    now = datetime.now(timezone.utc)
    raw_expires_in = data.get("expires_in")
    if isinstance(raw_expires_in, (int, float)) and raw_expires_in > 0:
        expires_in = int(raw_expires_in)
        expires_at = now + timedelta(seconds=expires_in)
        expires_ts = int(expires_at.timestamp())
    elif isinstance(access_claims.get("exp"), (int, float)):
        expires_ts = int(access_claims["exp"])
        expires_at = datetime.fromtimestamp(expires_ts, tz=timezone.utc)
        expires_in = max(1, int(expires_ts - now.timestamp()))
    else:
        expires_in = 3600
        expires_at = now + timedelta(seconds=expires_in)
        expires_ts = int(expires_at.timestamp())

    account_id = str(
        data.get("accountId")
        or data.get("account_id")
        or access_claims.get("sub")
        or id_claims.get("sub")
        or ""
    )
    email = (
        data.get("email")
        or access_claims.get("email")
        or id_claims.get("email")
        or None
    )
    display_name = (
        data.get("displayName")
        or data.get("display_name")
        or access_claims.get("name")
        or access_claims.get("preferred_username")
        or id_claims.get("name")
        or id_claims.get("preferred_username")
        or None
    )

    return {
        "access": access_token,
        "access_token": access_token,
        "refresh": refresh_token,
        "refresh_token": refresh_token,
        "expires": expires_ts,
        "expires_in": expires_in,
        "expires_at": expires_at,
        "email": email,
        "displayName": display_name,
        "display_name": display_name,
        "accountId": account_id,
        "account_id": account_id,
        "token_endpoint": validated_endpoint,
    }


def is_cloudflare_challenge(resp: httpx.Response) -> bool:
    """Return True if the response is a Cloudflare challenge (`cf-mitigated` header or HTML body)."""
    headers = getattr(resp, "headers", None) or {}
    if headers.get("cf-mitigated"):
        return True
    content_type = str(headers.get("content-type") or "").lower()
    if "text/html" in content_type:
        return True
    text_head = str(getattr(resp, "text", "") or "").lstrip()[:64].lower()
    return text_head.startswith("<!doctype html") or text_head.startswith("<html")


async def _record_provider_event_safe(user_id: str | None, kind: str, detail: dict) -> None:
    try:
        from src.dalton.database.provider_events import record_provider_event

        await record_provider_event(
            provider=XAI_PROVIDER,
            kind=kind,
            user_id=user_id,
            detail=detail,
            best_effort=True,
        )
    except Exception as exc:
        logger.debug("[xai_oauth] record_provider_event skipped: %s", type(exc).__name__)


def _discovery_cache_key(user_id: str) -> str:
    from src.dalton.credentials import credential_user_id

    return f"oauth:xai:discovery:{credential_user_id(user_id)}"


async def _read_cached_discovery(user_id: str) -> dict[str, str] | None:
    try:
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        key = _discovery_cache_key(user_id)
        data: Any = None
        client = getattr(cache, "client", None)
        if getattr(cache, "enabled", False) is True and client is not None:
            raw = await client.get(key)
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="ignore")
                data = json.loads(raw) if isinstance(raw, str) else raw
        elif hasattr(cache, "get") and getattr(cache, "enabled", False) is True:
            data = await cache.get(key)
        if not isinstance(data, dict):
            return None
        dev_ep = validate_xai_endpoint(
            str(data.get("device_authorization_endpoint") or ""),
            "device_authorization_endpoint",
        )
        tok_ep = validate_xai_endpoint(
            str(data.get("token_endpoint") or ""),
            "token_endpoint",
        )
        return {
            "device_authorization_endpoint": dev_ep,
            "token_endpoint": tok_ep,
        }
    except Exception as exc:
        logger.debug("[xai_oauth] discovery cache read skipped: %s", type(exc).__name__)
        return None


async def _write_cached_discovery(user_id: str, endpoints: dict[str, str]) -> None:
    try:
        from src.utils.cache.redis_cache import get_cache_client

        cache = get_cache_client()
        key = _discovery_cache_key(user_id)
        client = getattr(cache, "client", None)
        if getattr(cache, "enabled", False) is True and client is not None:
            await client.set(
                key,
                json.dumps(endpoints),
                ex=DISCOVERY_CACHE_TTL_SECONDS,
            )
        elif hasattr(cache, "set") and getattr(cache, "enabled", False) is True:
            await cache.set(key, endpoints, ttl=DISCOVERY_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.debug("[xai_oauth] discovery cache write skipped: %s", type(exc).__name__)


async def discover_openid_endpoints(
    *,
    issuer: str = XAI_ISSUER,
    user_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    """Fetch `/.well-known/openid-configuration` and validate xAI endpoints.

    When `user_id` is provided, caches validated discovery endpoints in Redis
    under `oauth:xai:discovery:<cred_user_id>` with a 1-hour TTL.
    """
    if user_id:
        cached = await _read_cached_discovery(user_id)
        if cached is not None:
            return cached

    config_url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
    validate_xai_endpoint(config_url, "openid_configuration_url")

    async def _fetch(c: httpx.AsyncClient) -> dict[str, str]:
        resp = await c.get(
            config_url,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("Invalid OpenID configuration payload from xAI")
        device_ep = validate_xai_endpoint(
            str(data.get("device_authorization_endpoint") or ""),
            "device_authorization_endpoint",
        )
        token_ep = validate_xai_endpoint(
            str(data.get("token_endpoint") or ""),
            "token_endpoint",
        )
        result = {
            "device_authorization_endpoint": device_ep,
            "token_endpoint": token_ep,
        }
        if user_id:
            await _write_cached_discovery(user_id, result)
        return result

    if client is not None:
        return await _fetch(client)
    async with httpx.AsyncClient(timeout=30.0) as new_client:
        return await _fetch(new_client)


async def request_device_code(
    *,
    device_authorization_endpoint: str | None = None,
    token_endpoint: str | None = None,
    user_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Request an RFC 8628 device code from xAI."""
    client_id = _resolve_client_id()

    async def _do_request(c: httpx.AsyncClient) -> dict[str, Any]:
        dev_ep = device_authorization_endpoint
        tok_ep = token_endpoint
        if not dev_ep or not tok_ep:
            discovered = await discover_openid_endpoints(user_id=user_id, client=c)
            dev_ep = dev_ep or discovered["device_authorization_endpoint"]
            tok_ep = tok_ep or discovered["token_endpoint"]
        dev_ep = validate_xai_endpoint(dev_ep, "device_authorization_endpoint")
        tok_ep = validate_xai_endpoint(tok_ep, "token_endpoint")

        resp = await c.post(
            dev_ep,
            data={"client_id": client_id, "scope": XAI_SCOPE},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code >= 400:
            logger.error("[xai_oauth] Device code request failed: status=%s", resp.status_code)
        resp.raise_for_status()
        data = resp.json()
        verification_uri = str(data.get("verification_uri") or "")
        verification_uri_complete = str(data.get("verification_uri_complete") or "")
        verification_url = verification_uri_complete or verification_uri
        return {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_uri": verification_uri,
            "verification_uri_complete": verification_uri_complete,
            "verification_url": verification_url,
            "interval": int(data.get("interval") or DEFAULT_POLL_INTERVAL_SECONDS),
            "expires_in": int(data.get("expires_in") or MAX_POLL_WAIT_SECONDS),
            "token_endpoint": tok_ep,
        }

    if client is not None:
        return await _do_request(client)
    async with httpx.AsyncClient(timeout=30.0) as new_client:
        return await _do_request(new_client)


async def poll_device_authorization(
    device_code: str,
    *,
    token_endpoint: str | None = None,
    interval: int = DEFAULT_POLL_INTERVAL_SECONDS,
    user_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Single poll attempt for RFC 8628 device authorization.

    Handles the 4 RFC 8628 error states:
    - `authorization_pending`: returns `{'status': 'authorization_pending', 'pending': True, 'interval': interval}`
    - `slow_down`: returns `{'status': 'slow_down', 'pending': True, 'interval': interval + 5}`
    - `access_denied`: raises `ValueError`
    - `expired_token`: raises `ValueError`
    """
    client_id = _resolve_client_id()

    async def _do_poll(c: httpx.AsyncClient) -> dict[str, Any]:
        tok_ep = token_endpoint
        if not tok_ep:
            discovered = await discover_openid_endpoints(user_id=user_id, client=c)
            tok_ep = discovered["token_endpoint"]
        tok_ep = validate_xai_endpoint(tok_ep, "token_endpoint")

        resp = await c.post(
            tok_ep,
            data={
                "grant_type": XAI_DEVICE_GRANT,
                "client_id": client_id,
                "device_code": device_code,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            record = extract_token_record(data, token_endpoint=tok_ep)
            return {
                "status": "approved",
                "pending": False,
                "interval": interval,
                **record,
            }

        error = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                error = str(body.get("error") or "")
        except Exception:
            pass

        if error == "authorization_pending":
            return {
                "status": "authorization_pending",
                "pending": True,
                "interval": int(interval),
            }
        if error == "slow_down":
            return {
                "status": "slow_down",
                "pending": True,
                "interval": int(interval) + SLOW_DOWN_INCREMENT_SECONDS,
            }
        if error == "access_denied":
            raise ValueError("The xAI sign-in request was denied (access_denied).")
        if error == "expired_token":
            raise ValueError("The xAI sign-in request expired before it was approved (expired_token).")
        raise ValueError(f"xAI sign-in failed (HTTP {resp.status_code}: {error or 'unknown'})")

    if client is not None:
        return await _do_poll(client)
    async with httpx.AsyncClient(timeout=30.0) as new_client:
        return await _do_poll(new_client)


async def poll_device_token_loop(
    device_code: str,
    *,
    token_endpoint: str | None = None,
    interval: float = float(DEFAULT_POLL_INTERVAL_SECONDS),
    max_wait_seconds: float = float(MAX_POLL_WAIT_SECONDS),
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Poll device authorization until approved, denied, or `max_wait_seconds` (default 300s)."""
    current_interval = float(interval if interval and interval > 0 else DEFAULT_POLL_INTERVAL_SECONDS)
    elapsed = 0.0

    while elapsed <= max_wait_seconds:
        res = await poll_device_authorization(
            device_code,
            token_endpoint=token_endpoint,
            interval=int(current_interval),
            client=client,
        )
        if not res.get("pending"):
            return res
        current_interval = float(res.get("interval") or current_interval)
        if elapsed + current_interval > max_wait_seconds:
            break
        await asyncio.sleep(current_interval)
        elapsed += current_interval

    raise TimeoutError("The xAI sign-in request timed out after 300 seconds.")


async def refresh_tokens(
    refresh_token: str,
    *,
    token_endpoint: str | None = None,
    user_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Refresh an xAI OAuth token.

    - Keeps the existing `refresh_token` when the response omits a new one.
    - Retries ONLY on Cloudflare challenge (`cf-mitigated` header or HTML body),
      up to 3 retries with 250ms delay between attempts.
    - When `token_endpoint` is omitted, discovers endpoints (with host validation
      and per-user 1-hour Redis cache when `user_id` is given).
    """
    client_id = _resolve_client_id()

    async def _do_refresh(c: httpx.AsyncClient) -> dict[str, Any]:
        tok_ep = token_endpoint
        if not tok_ep:
            discovered = await discover_openid_endpoints(user_id=user_id, client=c)
            tok_ep = discovered["token_endpoint"]
        tok_ep = validate_xai_endpoint(tok_ep, "token_endpoint")

        last_resp: httpx.Response | None = None
        for attempt in range(CF_MAX_RETRIES + 1):
            resp = await c.post(
                tok_ep,
                data={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            last_resp = resp
            if is_cloudflare_challenge(resp):
                if attempt < CF_MAX_RETRIES:
                    await asyncio.sleep(CF_RETRY_DELAY_SECONDS)
                    continue
                raise ValueError(
                    f"xAI token refresh blocked by Cloudflare challenge (HTTP {resp.status_code})"
                )
            resp.raise_for_status()
            data = resp.json()
            return extract_token_record(
                data,
                token_endpoint=tok_ep,
                fallback_refresh=refresh_token,
            )

        raise ValueError(
            f"xAI token refresh failed after Cloudflare retries (HTTP {getattr(last_resp, 'status_code', 'unknown')})"
        )

    if client is not None:
        return await _do_refresh(client)
    async with httpx.AsyncClient(timeout=30.0) as new_client:
        return await _do_refresh(new_client)


async def save_xai_tokens(user_id: str, token_record: dict[str, Any]) -> None:
    """Persist xAI OAuth token record via `upsert_oauth_tokens(provider='xai-oauth', plan_type='supergrok')`."""
    now = datetime.now(timezone.utc)
    expires_at = token_record.get("expires_at")
    if not isinstance(expires_at, datetime):
        expires_in = int(token_record.get("expires_in") or 3600)
        expires_at = now + timedelta(seconds=expires_in)

    email_or_name = (
        token_record.get("email")
        or token_record.get("displayName")
        or token_record.get("display_name")
    )

    await upsert_oauth_tokens(
        user_id=user_id,
        provider=XAI_PROVIDER,
        access_token=str(token_record.get("access_token") or token_record.get("access") or ""),
        refresh_token=str(token_record.get("refresh_token") or token_record.get("refresh") or ""),
        account_id=str(token_record.get("account_id") or token_record.get("accountId") or ""),
        email=email_or_name,
        plan_type=XAI_PLAN_TYPE,
        expires_at=expires_at,
    )


async def _renew_refresh_lock(cache: Any, lock_key: str, lock_token: str, ttl_ms: int) -> bool:
    """Extend lock TTL only if `lock_token` still owns `lock_key`."""
    client = getattr(cache, "client", None)
    if getattr(cache, "enabled", False) is True and client is not None:
        try:
            if hasattr(client, "eval"):
                res = await client.eval(_RENEW_LOCK_LUA, 1, lock_key, lock_token, str(ttl_ms))
                return bool(res)
            cur = await client.get(lock_key)
            if isinstance(cur, bytes):
                cur = cur.decode("utf-8", errors="ignore")
            if cur == lock_token:
                if hasattr(client, "pexpire"):
                    await client.pexpire(lock_key, ttl_ms)
                return True
            return False
        except Exception as exc:
            logger.debug("[xai_oauth] Lock renew skipped for %s: %s", lock_key, type(exc).__name__)
            return False
    return True


async def _release_refresh_lock(cache: Any, lock_key: str, lock_token: str) -> bool:
    """Release `lock_key` only if `lock_token` still owns it (compare-and-delete)."""
    client = getattr(cache, "client", None)
    if getattr(cache, "enabled", False) is True and client is not None:
        try:
            if hasattr(client, "eval"):
                res = await client.eval(_RELEASE_LOCK_LUA, 1, lock_key, lock_token)
                return bool(res)
            cur = await client.get(lock_key)
            if isinstance(cur, bytes):
                cur = cur.decode("utf-8", errors="ignore")
            if cur == lock_token:
                await client.delete(lock_key)
                return True
            return False
        except Exception as exc:
            logger.debug("[xai_oauth] Lock release failed for %s: %s", lock_key, type(exc).__name__)
            return False
    await cache.release_lock(lock_key, lock_token)
    return True


async def _await_xai_refreshed_tokens(cred_user_id: str) -> dict[str, Any] | None:
    """Wait for another holder of the refresh lock to write fresh tokens to DB."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _REFRESH_WAIT_SECONDS
    poll_interval = max(0.5, float(_REFRESH_POLL_SECONDS))
    while True:
        await asyncio.sleep(poll_interval)
        tokens = await get_oauth_tokens(cred_user_id, XAI_PROVIDER)
        if not tokens:
            return None
        now = datetime.now(timezone.utc)
        if token_expiry(tokens) > now + REFRESH_BUFFER:
            return tokens
        if loop.time() >= deadline:
            return tokens if token_expiry(tokens) > now else None


async def get_valid_token(user_id: str) -> dict[str, Any] | None:
    """Get a valid xAI access token, refreshing with Redis lock and `REFRESH_BUFFER` if needed."""
    from src.dalton.credentials import credential_user_id

    cred_user_id = credential_user_id(user_id)
    tokens = await get_oauth_tokens(cred_user_id, XAI_PROVIDER)
    if not tokens:
        return None

    now = datetime.now(timezone.utc)
    if token_expiry(tokens) > now + REFRESH_BUFFER:
        return {
            "access_token": tokens["access_token"],
            "plan_type": tokens.get("plan_type") or XAI_PLAN_TYPE,
            "account_id": tokens.get("account_id", ""),
        }

    if not tokens.get("refresh_token"):
        return {
            "access_token": tokens["access_token"],
            "plan_type": tokens.get("plan_type") or XAI_PLAN_TYPE,
            "account_id": tokens.get("account_id", ""),
        }

    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    lock_key = f"oauth:refresh:{cred_user_id}:{XAI_PROVIDER}"
    lock_token = secrets.token_hex(16)
    acquired = await cache.acquire_lock(lock_key, lock_token, XAI_REFRESH_LOCK_TTL_MS)
    if acquired is False:
        tokens = await _await_xai_refreshed_tokens(cred_user_id)
        if tokens:
            return {
                "access_token": tokens["access_token"],
                "plan_type": tokens.get("plan_type") or XAI_PLAN_TYPE,
                "account_id": tokens.get("account_id", ""),
            }
        return None

    saved_token_result: dict[str, Any] | None = None
    saved_expires_in: int = 3600
    try:
        # Bound the entire lock-held section (DB re-read, discovery, refresh_tokens,
        # lock renewal, DB save) by XAI_REFRESH_HARD_TIMEOUT_S (120s), strictly less
        # than XAI_REFRESH_LOCK_TTL_MS (180s) so timeout aborts before the lock expires.
        async with asyncio.timeout(XAI_REFRESH_HARD_TIMEOUT_S):
            # Re-read credentials from DB after acquiring the lock: if another worker
            # already refreshed them before we acquired the lock, use the new token directly.
            tokens = await get_oauth_tokens(cred_user_id, XAI_PROVIDER)
            if not tokens:
                return None
            now = datetime.now(timezone.utc)
            if token_expiry(tokens) > now + REFRESH_BUFFER:
                return {
                    "access_token": tokens["access_token"],
                    "plan_type": tokens.get("plan_type") or XAI_PLAN_TYPE,
                    "account_id": tokens.get("account_id", ""),
                }
            if not tokens.get("refresh_token"):
                return {
                    "access_token": tokens["access_token"],
                    "plan_type": tokens.get("plan_type") or XAI_PLAN_TYPE,
                    "account_id": tokens.get("account_id", ""),
                }

            used_refresh_token = tokens["refresh_token"]
            new = await refresh_tokens(
                used_refresh_token,
                user_id=cred_user_id,
            )
            renewed = await _renew_refresh_lock(
                cache, lock_key, lock_token, XAI_REFRESH_LOCK_TTL_MS
            )
            if not renewed:
                latest = await get_oauth_tokens(cred_user_id, XAI_PROVIDER)
                if latest and latest.get("refresh_token") == used_refresh_token:
                    logger.warning(
                        "[xai_oauth] lock lost but DB unchanged; saving for user_id=%s",
                        user_id,
                    )
                    tokens = latest
                else:
                    logger.warning(
                        "[xai_oauth] Refresh lock lost before DB save for user_id=%s; skipping upsert",
                        user_id,
                    )
                    now = datetime.now(timezone.utc)
                    if latest and (
                        token_expiry(latest) > now + REFRESH_BUFFER
                        or (
                            latest.get("access_token") != tokens.get("access_token")
                            and token_expiry(latest) > now
                        )
                    ):
                        return {
                            "access_token": latest["access_token"],
                            "plan_type": latest.get("plan_type") or XAI_PLAN_TYPE,
                            "account_id": latest.get("account_id", ""),
                        }
                    return None

            now = datetime.now(timezone.utc)
            new_expires = new.get("expires_at")
            if not isinstance(new_expires, datetime):
                new_expires = now + timedelta(seconds=int(new.get("expires_in") or 3600))

            new_refresh = new.get("refresh_token") or new.get("refresh") or tokens["refresh_token"]
            new_account_id = (
                new.get("account_id")
                or new.get("accountId")
                or tokens.get("account_id", "")
            )
            new_email = (
                new.get("email")
                or tokens.get("email")
                or new.get("displayName")
                or new.get("display_name")
            )

            await upsert_oauth_tokens(
                user_id=user_id,
                provider=XAI_PROVIDER,
                access_token=new["access_token"],
                refresh_token=new_refresh,
                account_id=new_account_id,
                email=new_email,
                plan_type=XAI_PLAN_TYPE,
                expires_at=new_expires,
            )
            saved_expires_in = int(new.get("expires_in") or 3600)
            saved_token_result = {
                "access_token": new["access_token"],
                "plan_type": XAI_PLAN_TYPE,
                "account_id": new_account_id,
            }
    except Exception as e:
        if saved_token_result is None:
            await _record_provider_event_safe(
                user_id,
                "refresh_fail",
                {"error_type": type(e).__name__, "message": str(e)[:160]},
            )
            logger.error(
                "[xai_oauth] Token refresh failed for user_id=%s: %s",
                user_id,
                type(e).__name__,
            )
            return None
    finally:
        if acquired:
            try:
                await _release_refresh_lock(cache, lock_key, lock_token)
            except Exception as exc:
                logger.debug("[xai_oauth] Lock release skipped: %s", type(exc).__name__)

    try:
        async with asyncio.timeout(5):
            await invalidate_oauth_active_cache(cred_user_id)
    except Exception as exc:
        logger.debug(
            "[xai_oauth] invalidate_oauth_active_cache skipped: %s",
            type(exc).__name__,
        )

    try:
        async with asyncio.timeout(5):
            await _record_provider_event_safe(
                user_id,
                "refresh_ok",
                {"expires_in": saved_expires_in},
            )
    except Exception as exc:
        logger.debug("[xai_oauth] refresh_ok event skipped: %s", type(exc).__name__)

    logger.debug("[xai_oauth] Refreshed tokens for user_id=%s", user_id)
    return saved_token_result


async def fetch_xai_models(
    access_token: str,
    *,
    base_url: str = XAI_PROXY_BASE_URL,
    client_version: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Fetch `/v1/models` from `cli-chat-proxy.grok.com` with bearer token."""
    ver = client_version or get_xai_grok_client_version()
    url = f"{base_url.rstrip('/')}/models"

    async def _do_get(c: httpx.AsyncClient) -> list[dict[str, Any]]:
        resp = await c.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-XAI-Token-Auth": "xai-grok-cli",
                "x-grok-client-version": ver,
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        normalized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            item = {
                "id": row.get("id"),
                "name": row.get("name"),
                "context_window": row.get("context_window"),
                "supports_reasoning_effort": bool(row.get("supports_reasoning_effort")),
                "reasoning_efforts": list(row.get("reasoning_efforts") or []),
            }
            normalized.append(item)
        return normalized

    if client is not None:
        return await _do_get(client)
    async with httpx.AsyncClient(timeout=15.0) as new_client:
        return await _do_get(new_client)


async def fetch_xai_billing(
    access_token: str,
    *,
    base_url: str = XAI_PROXY_BASE_URL,
    client_version: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch `/v1/billing?format=credits` for quota display.

    Treats HTTP 401/403 as `token_expired=True`.
    """
    ver = client_version or get_xai_grok_client_version()
    url = f"{base_url.rstrip('/')}/billing?format=credits"

    async def _do_billing(c: httpx.AsyncClient) -> dict[str, Any]:
        resp = await c.get(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "X-XAI-Token-Auth": "xai-grok-cli",
                "x-grok-client-mode": "cli",
                "x-grok-client-version": ver,
                "Accept": "application/json",
            },
        )
        if resp.status_code in (401, 403):
            return {
                "ok": False,
                "token_expired": True,
                "status_code": resp.status_code,
                "error": "token_expired",
            }
        if resp.status_code >= 400:
            return {
                "ok": False,
                "token_expired": False,
                "status_code": resp.status_code,
                "error": f"http_{resp.status_code}",
            }

        data = resp.json()
        if not isinstance(data, dict):
            return {"ok": False, "token_expired": False, "error": "invalid_payload"}

        cfg = data.get("config") if isinstance(data.get("config"), dict) else {}
        raw_pct = cfg.get("creditUsagePercent")
        if raw_pct is None:
            raw_pct = data.get("creditUsagePercent")

        used = cfg.get("used") if cfg.get("used") is not None else data.get("used")
        monthly_limit = (
            cfg.get("monthlyLimit")
            if cfg.get("monthlyLimit") is not None
            else data.get("monthlyLimit")
        )

        credit_usage_percent: float | None = None
        if isinstance(raw_pct, (int, float)):
            credit_usage_percent = round(float(raw_pct), 2)
        elif (
            isinstance(used, (int, float))
            and isinstance(monthly_limit, (int, float))
            and float(monthly_limit) > 0
        ):
            credit_usage_percent = round((float(used) / float(monthly_limit)) * 100.0, 2)

        current_period = data.get("currentPeriod") or cfg.get("currentPeriod")
        subscription_tier = (
            data.get("subscription_tier")
            or data.get("subscriptionTier")
            or cfg.get("subscription_tier")
            or cfg.get("subscriptionTier")
        )

        return {
            "ok": True,
            "token_expired": False,
            "credit_usage_percent": credit_usage_percent,
            "used": used,
            "monthly_limit": monthly_limit,
            "current_period": current_period,
            "subscription_tier": subscription_tier,
        }

    if client is not None:
        return await _do_billing(client)
    async with httpx.AsyncClient(timeout=10.0) as new_client:
        return await _do_billing(new_client)
