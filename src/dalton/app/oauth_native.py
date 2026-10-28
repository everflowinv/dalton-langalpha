"""
Dalton-owned OAuth routes for Google Antigravity and Meta Muse (P0N.2, G5.2).

Mounted at ``/api/v1/oauth`` via ``dalton_router`` so paths remain identical:
- POST   /api/v1/oauth/antigravity/initiate
- POST   /api/v1/oauth/antigravity/poll
- POST   /api/v1/oauth/antigravity/callback  (R6-5 manual URL paste fallback for Docker)
- GET    /api/v1/oauth/antigravity/status
- DELETE /api/v1/oauth/antigravity
- POST   /api/v1/oauth/muse/device/initiate
- POST   /api/v1/oauth/muse/device/poll
- GET    /api/v1/oauth/muse/status
- DELETE /api/v1/oauth/muse
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.dalton.llm.providers.antigravity.oauth import (
    ANTIGRAVITY_PROVIDER,
    check_login_complete as antigravity_check_login_complete,
    start_login as antigravity_start_login,
    submit_callback_url as antigravity_submit_callback_url,
)
from src.dalton.llm.providers.muse.oauth import (
    MUSE_PROVIDER,
    get_muse_cached_meta,
    is_invalidated_muse_token,
    is_minted_muse_token,
    poll_device_authorization as muse_poll_device_authorization,
    request_device_code as muse_request_device_code,
    save_minted_credentials as muse_save_minted_credentials,
)
from src.dalton.llm.providers.xai_grok.oauth import (
    MAX_POLL_WAIT_SECONDS as XAI_MAX_POLL_WAIT_SECONDS,
    XAI_PLAN_TYPE,
    XAI_PROVIDER,
    poll_device_authorization as xai_poll_device_authorization,
    request_device_code as xai_request_device_code,
    save_xai_tokens,
)
from src.server.database.oauth_tokens import (
    connection_change,
    delete_oauth_tokens,
    get_oauth_connection,
)
from src.server.services.oauth_health import connection_changed
from src.server.models.oauth import OAuthStatus
from src.server.utils.api import CurrentUserId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/oauth", tags=["Dalton OAuth"])


async def _dalton_oauth_status(user_id: str, provider: str) -> dict:
    """The connection's status in the unified projection: ``state`` says
    connected / needs_reauth / disconnected, so availability is judged by
    state, never by the presence of a token row."""
    return OAuthStatus.from_row(await get_oauth_connection(user_id, provider)).model_dump()


class AntigravityCallbackRequest(BaseModel):
    callback_input: str


def _pending_key(user_id: str, kind: str) -> str:
    """A pending sign-in's Redis key, by the credential holder.

    DALTON-HOOK(team-credentials): one shared credential has one pending flow,
    so a disconnect by any member clears the flow a member's poll is waiting
    on, and a completed sign-in can never revive a disconnected connection.
    """
    from src.dalton.credentials import credential_user_id

    return f"oauth:{kind}:device:{credential_user_id(user_id)}"


async def _claim_pending(cache, key: str, raw) -> bool:
    """Take the pending flow (compare-and-delete) so only its poller saves."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    return await cache.claim(key, raw)


# ═══════════════════════════════════════════════════════════════════════════════
# Muse Code OAuth — Device Code Flow
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/muse/device/initiate")
async def muse_device_initiate(user_id: CurrentUserId):
    """Start device code flow. Returns user_code + verification URL."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")

    try:
        device = await muse_request_device_code()
    except Exception as e:
        logger.error(f"[oauth] Muse device code request failed: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to request device code from Meta")

    await cache.client.set(
        _pending_key(user_id, "muse"),
        json.dumps({"device_code": device["device_code"]}),
        ex=max(60, device.get("expires_in", 600)),
    )

    logger.info(f"[oauth] Muse device code initiated for user_id={user_id}")
    return {
        "user_code": device["user_code"],
        "verification_url": device["verification_url"],
        "interval": device["interval"],
    }


@router.post("/muse/device/poll")
async def muse_device_poll(user_id: CurrentUserId):
    """Poll for device authorization."""
    from src.server.database.oauth_tokens import connection_change
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    key = _pending_key(user_id, "muse")
    raw = await cache.client.get(key)
    if not raw:
        raise HTTPException(status_code=400, detail="No pending Muse authorization. Please initiate again.")

    device = json.loads(raw)

    try:
        # The low-level poll only mints and returns the key; every write happens
        # below, inside the connection lock, so an approval that arrives while a
        # disconnect is landing cannot revive the connection.
        result = await muse_poll_device_authorization(device["device_code"])
    except ValueError as e:
        # Take only our own flow's key: an unconditional delete here could drop
        # a newer sign-in's pending flow (DALTON-HOOK(team-credentials)).
        await _claim_pending(cache, key, raw)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] Muse device poll error for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to poll Meta or mint Muse API key")

    if result is None:
        return {"pending": True}

    try:
        # Claim the flow and store under the connection lock: a disconnect that
        # landed while Meta was answering has cleared the key, and this save
        # aborts instead of reviving the connection (DALTON-HOOK(team-credentials)).
        async with connection_change(user_id, MUSE_PROVIDER) as conn:
            if not await _claim_pending(cache, key, raw):
                raise HTTPException(
                    status_code=400,
                    detail="The Muse sign-in was cancelled or replaced. Start again.",
                )
            result = await muse_save_minted_credentials(user_id, result, conn=conn)

        logger.info(f"[oauth] Muse connected for user_id={user_id}")
        return {
            "success": True,
            "email": result.get("email"),
            "plan_type": result.get("plan_type"),
        }

    except ValueError as e:
        logger.error(f"[oauth] Muse API key mint/save failed for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[oauth] Muse token storage failed for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=f"Token storage failed: {type(e).__name__}")


@router.get("/muse/status")
async def muse_status(user_id: CurrentUserId):
    """Return connection status in the unified projection (no token decryption).

    A stored row the Muse line itself refuses — an invalidated key or a legacy
    un-minted OAuth token — reports ``needs_reauth`` with ``reconnect_required``,
    not a working connection."""
    row = await get_oauth_connection(user_id, MUSE_PROVIDER)
    st = OAuthStatus.from_row(row).model_dump()
    if st.get("state") == "connected":
        if row and (is_invalidated_muse_token(row) or not is_minted_muse_token(row)):
            return {
                **st,
                "connected": False,
                "state": "needs_reauth",
                "reconnect_required": True,
                "message": "请重新连接 Muse",
            }
        meta = await get_muse_cached_meta(user_id)
        if isinstance(meta, dict):
            st = {
                **st,
                "is_subs_active": meta.get("is_subs_active"),
                "subs_tier_name": meta.get("subs_tier_name") or st.get("plan_type"),
                "subs_usage": meta.get("subs_usage") or {},
            }
    return st


@router.delete("/muse")
async def muse_disconnect(user_id: CurrentUserId):
    """Delete the stored Muse connection and any sign-in still pending.

    The write rides the unified ``connection_change`` lock and first clears the
    pending flow — keyed by the credential holder, so this cancels every
    member's pending sign-in too, and an approval arriving after the
    disconnect cannot bring the connection back
    (DALTON-HOOK(team-credentials))."""
    from src.dalton.credentials import credential_user_id, require_credential_admin
    from src.utils.cache.redis_cache import get_cache_client

    require_credential_admin(user_id)
    cache = get_cache_client()
    async with connection_change(user_id, MUSE_PROVIDER) as conn:
        if getattr(cache, "enabled", False) and getattr(cache, "client", None):
            await cache.client.delete(_pending_key(user_id, "muse"))
        await delete_oauth_tokens(user_id, MUSE_PROVIDER, conn=conn)
    await connection_changed(user_id)
    try:
        if getattr(cache, "enabled", False) and getattr(cache, "client", None):
            await cache.client.delete(f"oauth:muse:meta:{credential_user_id(user_id)}")
    except Exception:
        pass
    logger.info(f"[oauth] Muse disconnected for user_id={user_id}")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# Google Antigravity OAuth — browser + loopback redirect + manual paste fallback
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/antigravity/initiate")
async def antigravity_initiate(user_id: CurrentUserId):
    """Start the loopback login: PKCE + local callback listener + authorize URL."""
    try:
        login = await antigravity_start_login(user_id)
    except Exception as e:
        logger.error(f"[oauth] Antigravity login start failed for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to start Google login")

    logger.info(f"[oauth] Antigravity login initiated for user_id={user_id} (port {login['port']})")
    return {
        "authorize_url": login["authorize_url"],
        "port": login["port"],
    }


@router.post("/antigravity/poll")
async def antigravity_poll(user_id: CurrentUserId):
    """Poll for loopback-callback completion."""
    try:
        if await antigravity_check_login_complete(user_id):
            status = await get_oauth_connection(user_id, ANTIGRAVITY_PROVIDER)
            logger.info(f"[oauth] Antigravity connected for user_id={user_id}")
            return {
                "success": True,
                "email": (status or {}).get("email"),
                "plan_type": (status or {}).get("plan_type"),
            }
        return {"pending": True}
    except Exception as e:
        logger.error(f"[oauth] Antigravity poll error for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to poll login status")


@router.post("/antigravity/callback")
async def antigravity_callback(user_id: CurrentUserId, body: AntigravityCallbackRequest):
    """Exchange a manually pasted callback URL for Antigravity tokens (Docker fallback, R6-5)."""
    try:
        return await antigravity_submit_callback_url(user_id, body.callback_input)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] Antigravity callback exchange failed for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {type(e).__name__}")


@router.get("/antigravity/status")
async def antigravity_status(user_id: CurrentUserId):
    """Return connection status (no token decryption — fast check)."""
    return await _dalton_oauth_status(user_id, ANTIGRAVITY_PROVIDER)


@router.delete("/antigravity")
async def antigravity_disconnect(user_id: CurrentUserId):
    """Delete the stored Antigravity connection and any login still pending.

    Same unified door as the others: the holder-keyed flow and completion
    markers go first, inside the ``connection_change`` lock, so a loopback
    callback or pasted URL landing after the disconnect cannot revive the
    connection (DALTON-HOOK(team-credentials))."""
    from src.dalton.credentials import require_credential_admin
    from src.dalton.llm.providers.antigravity.oauth import _flow_keys
    from src.utils.cache.redis_cache import get_cache_client

    require_credential_admin(user_id)
    cache = get_cache_client()
    async with connection_change(user_id, ANTIGRAVITY_PROVIDER) as conn:
        if getattr(cache, "enabled", False) and getattr(cache, "client", None):
            flow_key, done_key = _flow_keys(user_id)
            from src.dalton.llm.providers.antigravity.oauth import _current_flow_key

            await cache.client.delete(flow_key, done_key, _current_flow_key(user_id))
        await delete_oauth_tokens(user_id, ANTIGRAVITY_PROVIDER, conn=conn)
    await connection_changed(user_id)
    logger.info(f"[oauth] Antigravity disconnected for user_id={user_id}")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════════
# xAI Grok OAuth — RFC 8628 Device Code Flow (SuperGrok subscription)
# ═══════════════════════════════════════════════════════════════════════════════


@router.post("/xai/device/initiate")
async def xai_device_initiate(user_id: CurrentUserId):
    """Start xAI device code flow. Returns user_code + verification URL."""
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable for OAuth")

    try:
        device = await xai_request_device_code(user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] xAI device code request failed: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to request device code from xAI")

    ttl = min(XAI_MAX_POLL_WAIT_SECONDS, max(60, int(device.get("expires_in") or XAI_MAX_POLL_WAIT_SECONDS)))
    expires_at_ts = int(datetime.now(timezone.utc).timestamp()) + ttl
    await cache.client.set(
        _pending_key(user_id, "xai"),
        json.dumps(
            {
                "device_code": device["device_code"],
                "token_endpoint": device.get("token_endpoint"),
                "interval": int(device.get("interval") or 5),
                "ttl": ttl,
                "expires_at": expires_at_ts,
            }
        ),
        ex=ttl,
    )

    logger.info(f"[oauth] xAI device code initiated for user_id={user_id}")
    return {
        "user_code": device["user_code"],
        "verification_url": device["verification_url"],
        "interval": device["interval"],
    }


@router.post("/xai/device/poll")
async def xai_device_poll(user_id: CurrentUserId):
    """Poll for xAI device authorization."""
    from src.server.database.oauth_tokens import connection_change
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    device_key = _pending_key(user_id, "xai")
    raw = await cache.client.get(device_key)
    if not raw:
        raise HTTPException(status_code=400, detail="No pending xAI authorization. Please initiate again.")

    device = json.loads(raw)
    cur_interval = int(device.get("interval") or 5)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    raw_expires_at = device.get("expires_at")

    def _cancelled() -> HTTPException:
        return HTTPException(
            status_code=400,
            detail="The xAI sign-in was cancelled or replaced. Start again.",
        )

    # Every cleanup below takes only OUR flow (compare-and-delete on the raw
    # value this poll read): a disconnect may have cleared the key, or a newer
    # sign-in replaced it, and an unconditional delete would drop the newer
    # flow (DALTON-HOOK(team-credentials)).
    if isinstance(raw_expires_at, (int, float)) and int(raw_expires_at) <= now_ts:
        await _claim_pending(cache, device_key, raw)
        raise HTTPException(status_code=400, detail="The xAI sign-in request expired before it was approved (expired_token).")

    try:
        result = await xai_poll_device_authorization(
            device["device_code"],
            token_endpoint=device.get("token_endpoint"),
            interval=cur_interval,
            user_id=user_id,
        )
    except ValueError as e:
        await _claim_pending(cache, device_key, raw)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] xAI device poll error for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to poll xAI")

    if result.get("pending"):
        new_interval = int(result.get("interval") or cur_interval)
        if new_interval != cur_interval:
            device["interval"] = new_interval
            initial_ttl = int(device.get("ttl") or XAI_MAX_POLL_WAIT_SECONDS)
            remaining_ttl: int | None = None
            if isinstance(raw_expires_at, (int, float)):
                remaining_ttl = int(raw_expires_at) - int(datetime.now(timezone.utc).timestamp())
            elif hasattr(cache.client, "ttl"):
                try:
                    redis_ttl = await cache.client.ttl(device_key)
                    if isinstance(redis_ttl, int) and redis_ttl > 0:
                        remaining_ttl = redis_ttl
                except Exception:
                    pass
            if remaining_ttl is None:
                remaining_ttl = initial_ttl
            if remaining_ttl <= 0:
                await _claim_pending(cache, device_key, raw)
                raise HTTPException(status_code=400, detail="The xAI sign-in request expired before it was approved (expired_token).")
            # Conditional write on the raw value this poll read: a disconnect
            # that cleared the key while xAI was answering, or a newer sign-in
            # that replaced it, leaves the swap refused — the slow_down backoff
            # cannot resurrect a dead flow or clobber a live newer one. The
            # swap is atomic against deletion, the same protection the
            # connection lock gives the save below, without a DB transaction
            # per poll (DALTON-HOOK(team-credentials)).
            if not await cache.swap(device_key, raw, json.dumps(device)):
                raise _cancelled()
        return {"pending": True, "interval": new_interval}

    try:
        # Claim the flow and store under the connection lock: a disconnect that
        # landed while xAI was answering has cleared the key, and this save
        # aborts instead of reviving the connection (DALTON-HOOK(team-credentials)).
        async with connection_change(user_id, XAI_PROVIDER) as conn:
            if not await _claim_pending(cache, device_key, raw):
                raise HTTPException(
                    status_code=400,
                    detail="The xAI sign-in was cancelled or replaced. Start again.",
                )
            await save_xai_tokens(user_id, result, conn=conn)
        logger.info(f"[oauth] xAI connected for user_id={user_id}")
        return {
            "success": True,
            "account_id": result.get("account_id") or result.get("accountId") or "",
            "email": result.get("email"),
            "display_name": result.get("displayName") or result.get("display_name"),
            "plan_type": XAI_PLAN_TYPE,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[oauth] xAI token storage failed for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=f"Token storage failed: {type(e).__name__}")


@router.get("/xai/status")
async def xai_status(user_id: CurrentUserId):
    """Return xAI connection status (no token decryption — fast check)."""
    return await _dalton_oauth_status(user_id, XAI_PROVIDER)


@router.delete("/xai")
async def xai_disconnect(user_id: CurrentUserId):
    """Delete the stored xAI connection and any sign-in still pending.

    Same unified door as the others: the holder-keyed pending device flow goes
    first, inside the ``connection_change`` lock, so an approval arriving
    after the disconnect cannot revive the connection
    (DALTON-HOOK(team-credentials))."""
    from src.dalton.credentials import require_credential_admin
    from src.utils.cache.redis_cache import get_cache_client

    require_credential_admin(user_id)
    cache = get_cache_client()
    async with connection_change(user_id, XAI_PROVIDER) as conn:
        if getattr(cache, "enabled", False) and getattr(cache, "client", None):
            await cache.client.delete(_pending_key(user_id, "xai"))
        await delete_oauth_tokens(user_id, XAI_PROVIDER, conn=conn)
    await connection_changed(user_id)
    logger.info(f"[oauth] xAI disconnected for user_id={user_id}")
    return {"success": True}
