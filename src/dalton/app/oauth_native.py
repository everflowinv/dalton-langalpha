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
from src.server.database.oauth_tokens import (
    delete_oauth_tokens,
    get_oauth_status,
    invalidate_oauth_active_cache,
)
from src.server.utils.api import CurrentUserId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/oauth", tags=["Dalton OAuth"])


class AntigravityCallbackRequest(BaseModel):
    callback_input: str


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
        f"oauth:muse:device:{user_id}",
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
    from src.utils.cache.redis_cache import get_cache_client

    cache = get_cache_client()
    if not cache.enabled or not cache.client:
        raise HTTPException(status_code=503, detail="Cache unavailable")

    raw = await cache.client.get(f"oauth:muse:device:{user_id}")
    if not raw:
        raise HTTPException(status_code=400, detail="No pending Muse authorization. Please initiate again.")

    device = json.loads(raw)

    try:
        result = await muse_poll_device_authorization(device["device_code"], user_id=user_id)
    except ValueError as e:
        await cache.client.delete(f"oauth:muse:device:{user_id}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] Muse device poll error for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=502, detail="Failed to poll Meta or mint Muse API key")

    if result is None:
        return {"pending": True}

    try:
        if not result.get("_saved"):
            result = await muse_save_minted_credentials(user_id, result)

        await cache.client.delete(f"oauth:muse:device:{user_id}")

        logger.info(f"[oauth] Muse connected for user_id={user_id}")
        return {
            "success": True,
            "email": result.get("email"),
            "plan_type": result.get("plan_type"),
        }

    except ValueError as e:
        logger.error(f"[oauth] Muse API key mint/save failed for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[oauth] Muse token storage failed for user_id={user_id}: {type(e).__name__}")
        raise HTTPException(status_code=400, detail=f"Token storage failed: {type(e).__name__}")


@router.get("/muse/status")
async def muse_status(user_id: CurrentUserId):
    """Return connection status (no token decryption — fast check)."""
    st = await get_oauth_status(user_id, MUSE_PROVIDER) or {"connected": False}
    if st.get("connected"):
        if is_invalidated_muse_token(st) or not is_minted_muse_token(st):
            return {
                **st,
                "connected": False,
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
    """Delete stored Muse OAuth tokens."""
    from src.dalton.credentials import credential_user_id, require_credential_admin
    from src.utils.cache.redis_cache import get_cache_client

    require_credential_admin(user_id)
    await delete_oauth_tokens(user_id, MUSE_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    try:
        cache = get_cache_client()
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
            status = await get_oauth_status(user_id, ANTIGRAVITY_PROVIDER)
            logger.info(f"[oauth] Antigravity connected for user_id={user_id}")
            return {
                "success": True,
                "email": status.get("email"),
                "plan_type": status.get("plan_type"),
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
    return await get_oauth_status(user_id, ANTIGRAVITY_PROVIDER)


@router.delete("/antigravity")
async def antigravity_disconnect(user_id: CurrentUserId):
    """Delete stored Antigravity OAuth tokens."""
    from src.dalton.credentials import require_credential_admin
    require_credential_admin(user_id)
    await delete_oauth_tokens(user_id, ANTIGRAVITY_PROVIDER)
    try:
        await invalidate_oauth_active_cache(user_id)
    except Exception:
        pass
    logger.info(f"[oauth] Antigravity disconnected for user_id={user_id}")
    return {"success": True}
