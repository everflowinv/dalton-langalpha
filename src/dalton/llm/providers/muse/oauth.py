"""
Muse Code OAuth service — device code flow, token exchange, refresh with
Redis lock (P0N.2, P0N.8).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

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

# --- Constants (matching the official Muse Code launcher) ---

MUSE_PROVIDER = "muse-oauth"
MUSE_CLIENT_ID = "1031625952748946"
MUSE_USER_AGENT = "muse-code/launcher-1.4.10"

MUSE_DEVICE_AUTH_URL = "https://auth.meta.com/oidc/device/authorization/"
MUSE_TOKEN_URL = "https://auth.meta.com/oidc/device/token/"
MUSE_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


def _record_provider_event_safe(user_id: str | None, kind: str, detail: dict) -> None:
    try:
        from src.dalton.database.provider_events import record_provider_event

        record_provider_event(
            provider=MUSE_PROVIDER,
            kind=kind,
            user_id=user_id,
            detail=detail,
        )
    except Exception:
        pass


async def request_device_code() -> dict:
    """Request a device code from Meta."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            MUSE_DEVICE_AUTH_URL,
            data={"client_id": MUSE_CLIENT_ID},
            headers={
                "Accept": "application/json",
                "User-Agent": MUSE_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code >= 400:
            logger.error(f"[muse_oauth] Device code request failed: status={resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        verification_url = (
            data.get("verification_uri_complete") or data.get("verification_uri") or ""
        )
        return {
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_url": verification_url,
            "interval": int(data.get("interval") or 5),
            "expires_in": int(data.get("expires_in") or 600),
        }


async def poll_device_authorization(device_code: str) -> dict | None:
    """Single poll attempt for device authorization."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            MUSE_TOKEN_URL,
            data={
                "grant_type": MUSE_DEVICE_GRANT,
                "device_code": device_code,
                "client_id": MUSE_CLIENT_ID,
            },
            headers={
                "Accept": "application/json",
                "User-Agent": MUSE_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "expires_in": data.get("expires_in", 3600),
            }
        error = ""
        try:
            error = resp.json().get("error", "")
        except Exception:
            pass
        if error in ("authorization_pending", "slow_down"):
            return None
        if error == "access_denied":
            raise ValueError("The sign-in request was denied.")
        if error == "expired_token":
            raise ValueError("The sign-in request expired before it was approved.")
        raise ValueError(f"Muse sign-in failed (HTTP {resp.status_code}: {error or 'unknown'})")


async def refresh_tokens(refresh_token: str) -> dict:
    """Use the refresh token to get a new access token."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            MUSE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": MUSE_CLIENT_ID,
            },
            headers={
                "Accept": "application/json",
                "User-Agent": MUSE_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", refresh_token),
            "expires_in": data.get("expires_in", 3600),
        }


async def get_valid_token(user_id: str) -> dict | None:
    """Get a valid access token, refreshing if needed."""
    tokens = await get_oauth_tokens(user_id, MUSE_PROVIDER)
    if not tokens:
        return None

    now = datetime.now(timezone.utc)

    if token_expiry(tokens) > now + REFRESH_BUFFER:
        return {"access_token": tokens["access_token"], "plan_type": tokens.get("plan_type")}

    if not tokens.get("refresh_token"):
        return {"access_token": tokens["access_token"], "plan_type": tokens.get("plan_type")}

    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    lock_key = f"oauth:refresh:{user_id}:{MUSE_PROVIDER}"
    lock_token = secrets.token_hex(16)
    acquired = await cache.acquire_lock(lock_key, lock_token, REFRESH_LOCK_TTL_MS)
    if acquired is False:
        tokens = await await_refreshed_tokens(user_id, MUSE_PROVIDER)
        if tokens:
            return {"access_token": tokens["access_token"], "plan_type": tokens.get("plan_type")}
        return None

    try:
        new = await refresh_tokens(tokens["refresh_token"])
        new_expires = now + timedelta(seconds=new.get("expires_in", 3600))

        await upsert_oauth_tokens(
            user_id=user_id,
            provider=MUSE_PROVIDER,
            access_token=new["access_token"],
            refresh_token=new["refresh_token"] or tokens["refresh_token"],
            account_id=tokens.get("account_id", ""),
            email=tokens.get("email"),
            plan_type=tokens.get("plan_type"),
            expires_at=new_expires,
        )

        try:
            await invalidate_oauth_active_cache(user_id)
        except Exception:
            pass

        _record_provider_event_safe(
            user_id,
            "refresh_ok",
            {"expires_in": int(new.get("expires_in", 3600))},
        )
        logger.debug(f"[muse_oauth] Refreshed tokens for user_id={user_id}")
        return {"access_token": new["access_token"], "plan_type": tokens.get("plan_type")}
    except Exception as e:
        _record_provider_event_safe(
            user_id,
            "refresh_fail",
            {"error_type": type(e).__name__, "message": str(e)[:160]},
        )
        logger.error(f"[muse_oauth] Token refresh failed for user_id={user_id}: {type(e).__name__}")
        return None
    finally:
        if acquired:
            await cache.release_lock(lock_key, lock_token)
