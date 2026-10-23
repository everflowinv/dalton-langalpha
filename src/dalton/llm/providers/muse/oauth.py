"""
Muse Code OAuth service — device code flow, API key minting (`/muse-code/key`),
Redis metadata caching, and token validation (P0N.2, P0N.8).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.server.database.oauth_tokens import (
    get_oauth_tokens,
    invalidate_oauth_active_cache,
    upsert_oauth_tokens,
)
from src.server.services.oauth_refresh_wait import token_expiry

logger = logging.getLogger(__name__)

# --- Constants (matching Muse Code CLI 1.4.12) ---

MUSE_PROVIDER = "muse-oauth"
MUSE_CLIENT_ID = "1031625952748946"
MUSE_USER_AGENT = "muse-code/launcher-1.4.12"
MUSE_MINT_USER_AGENT = "muse-build/0.1.0"
MUSE_MINT_API_VERSION = "1.0.0"

MUSE_DEVICE_AUTH_URL = "https://auth.meta.com/oidc/device/authorization/"
MUSE_TOKEN_URL = "https://auth.meta.com/oidc/device/token/"
MUSE_MINT_KEY_URL = "https://api.meta.ai/muse-code/key"
MUSE_DEFAULT_BASE_URL = "https://api.meta.ai/v1"
MUSE_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

MUSE_API_KEY_ACCOUNT_ID = "muse-api-key:v1"
MUSE_INVALID_ACCOUNT_ID = "muse-api-key:invalid"
MUSE_META_CACHE_TTL_S = 86400  # 1 day
MUSE_KEY_EXPIRES_DAYS = 365  # 1 year (minted API key carries no explicit expiry)

_SAFE_ERROR_CODE_RE = re.compile(r"^[a-z_]{1,40}$")

_current_muse_context: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "current_muse_context",
    default=None,
)


def get_active_muse_context() -> dict[str, str]:
    """Return the active Muse context (`user_id`, `cred_user_id`, `base_url`) for the current async task."""
    ctx = _current_muse_context.get()
    return dict(ctx) if isinstance(ctx, dict) else {}


def set_active_muse_context(
    *,
    user_id: str,
    cred_user_id: str,
    base_url: str,
) -> None:
    """Bind the active Muse context for downstream client construction."""
    _current_muse_context.set(
        {
            "user_id": str(user_id or ""),
            "cred_user_id": str(cred_user_id or ""),
            "base_url": str(base_url or MUSE_DEFAULT_BASE_URL),
        }
    )


def _muse_key_fp(api_key: str | None) -> str:
    """Return the first 16 hex chars of SHA-256 over `api_key`."""
    raw = str(api_key or "").strip()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _json_object(resp: httpx.Response) -> dict[str, Any]:
    """Parse a JSON object from `resp.json()`."""
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Muse response is not a JSON object.")
    return data


async def _record_provider_event_safe(user_id: str | None, kind: str, detail: dict) -> None:
    try:
        from src.dalton.database.provider_events import record_provider_event

        await record_provider_event(
            provider=MUSE_PROVIDER,
            kind=kind,
            user_id=user_id,
            detail=detail,
            best_effort=True,
        )
    except Exception as exc:
        logger.debug("[muse_oauth] record_provider_event skipped: %s", type(exc).__name__)


def _muse_meta_cache_key(cred_user_id: str) -> str:
    return f"oauth:muse:meta:{cred_user_id}"


def is_minted_muse_token(row_or_status: dict[str, Any] | None) -> bool:
    """Return True only if the stored credential row/status represents a minted Muse API key.

    Legacy OAuth rows saved before `mint_api_key` existed have `account_id == ""` (or a non-empty
    `refresh_token`), whereas invalidated rows have `account_id == "muse-api-key:invalid"`.
    """
    if not isinstance(row_or_status, dict):
        return False
    account_id = str(row_or_status.get("account_id") or "").strip()
    if account_id == MUSE_INVALID_ACCOUNT_ID:
        return False
    if not account_id.startswith(MUSE_API_KEY_ACCOUNT_ID):
        return False
    if row_or_status.get("refresh_token"):
        return False
    return True


def is_invalidated_muse_token(row_or_status: dict[str, Any] | None) -> bool:
    """Return True if the database credential row has `account_id == 'muse-api-key:invalid'`."""
    if not isinstance(row_or_status, dict):
        return False
    account_id = str(row_or_status.get("account_id") or "").strip()
    return account_id == MUSE_INVALID_ACCOUNT_ID


async def get_muse_cached_meta(user_id: str) -> dict[str, Any] | None:
    """Fetch per-user cached Muse mint metadata (`base_url`, `subs_usage`, subscription flags) from Redis."""
    from src.dalton.credentials import credential_user_id
    from src.utils.cache.redis_cache import get_cache_client

    cred_user_id = credential_user_id(user_id)
    cache = get_cache_client()
    if not getattr(cache, "enabled", False) or not getattr(cache, "client", None):
        return None
    try:
        raw = await cache.client.get(_muse_meta_cache_key(cred_user_id))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.debug("[muse_oauth] get_muse_cached_meta failed: %s", type(exc).__name__)
        return None


async def cache_muse_meta(user_id: str, meta_payload: dict[str, Any]) -> None:
    """Store Muse mint metadata (`base_url`, `subs_usage`, subscription info) in Redis with 1-day TTL."""
    from src.dalton.credentials import credential_user_id
    from src.utils.cache.redis_cache import get_cache_client

    cred_user_id = credential_user_id(user_id)
    cache = get_cache_client()
    if not getattr(cache, "enabled", False) or not getattr(cache, "client", None):
        return
    try:
        await cache.client.set(
            _muse_meta_cache_key(cred_user_id),
            json.dumps(meta_payload, ensure_ascii=False),
            ex=MUSE_META_CACHE_TTL_S,
        )
    except Exception as exc:
        logger.debug("[muse_oauth] cache_muse_meta skipped: %s", type(exc).__name__)


async def mark_muse_token_invalid(
    user_id: str | None,
    api_key: str,
    *,
    reason: str = "invalid_api_key",
) -> bool:
    """Atomically mark a user's Muse credential invalid if `api_key` matches the current DB row.

    `api_key` is required; if empty, returns `False` immediately without falling back to DB or context.
    """
    from src.dalton.credentials import credential_user_id
    from src.dalton.database.muse_tokens import mark_muse_key_invalid_if_current

    key_clean = str(api_key or "").strip()
    if not key_clean:
        logger.debug("[muse_oauth] mark_muse_token_invalid skipped: empty api_key")
        return False

    ctx = get_active_muse_context()
    eff_user_id = str(user_id or ctx.get("user_id") or "").strip()
    if not eff_user_id:
        logger.debug("[muse_oauth] mark_muse_token_invalid skipped: empty user_id")
        return False

    cred_user_id = credential_user_id(eff_user_id)
    try:
        updated = await mark_muse_key_invalid_if_current(cred_user_id, key_clean)
    except Exception as exc:
        logger.debug("[muse_oauth] mark_muse_key_invalid_if_current failed: %s", type(exc).__name__)
        return False

    if not updated:
        logger.debug(
            "[muse_oauth] mark_muse_token_invalid skipped: stored key does not match failed key (user_id=%s)",
            eff_user_id,
        )
        return False

    try:
        await invalidate_oauth_active_cache(cred_user_id)
    except Exception as exc:
        logger.debug("[muse_oauth] invalidate_oauth_active_cache skipped: %s", type(exc).__name__)

    await _record_provider_event_safe(
        eff_user_id,
        "auth_expired",
        {
            "error_kind": "auth_expired",
            "reason": reason,
            "message": "Muse API key 已失效（401 invalid_api_key），请重新连接 Muse。",
        },
    )
    return True


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
        data = _json_object(resp)
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


async def mint_api_key(access_token: str) -> dict[str, Any]:
    """Exchange a device-flow OAuth `access_token` for a Muse model `api_key`.

    Calls `POST https://api.meta.ai/muse-code/key` with body `{"onboard": false}`
    and headers matching Muse Code CLI 1.4.12 (`x-api-version: 1.0.0`,
    `user-agent: muse-build/0.1.0`). Never logs `access_token` or `api_key`, and
    never echoes upstream `message` or `error_description`.
    """
    token_clean = str(access_token or "").strip()
    if not token_clean:
        raise ValueError("Muse OAuth access_token is empty; cannot mint API key.")

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            MUSE_MINT_KEY_URL,
            json={"onboard": False},
            headers={
                "Authorization": f"Bearer {token_clean}",
                "x-api-version": MUSE_MINT_API_VERSION,
                "content-type": "application/json",
                "accept": "application/json",
                "user-agent": MUSE_MINT_USER_AGENT,
            },
        )
        if resp.status_code >= 400:
            err_code = ""
            try:
                err_json = _json_object(resp)
                raw_err = err_json.get("error")
                if isinstance(raw_err, str) and _SAFE_ERROR_CODE_RE.fullmatch(raw_err):
                    err_code = raw_err
            except Exception:
                err_code = ""
            logger.error("[muse_oauth] mint_api_key failed: status=%s", resp.status_code)
            suffix = f": {err_code}" if err_code else ""
            raise ValueError(f"Muse API key 换发失败 (HTTP {resp.status_code}{suffix})")

        data = _json_object(resp)

    api_key = str(data.get("api_key") or "").strip()
    if not api_key:
        logger.error("[muse_oauth] mint_api_key response missing api_key")
        raise ValueError("Muse API key 换发失败：响应中未包含 api_key")

    base_url = str(data.get("base_url") or MUSE_DEFAULT_BASE_URL).strip() or MUSE_DEFAULT_BASE_URL
    return {
        "api_key": api_key,
        "base_url": base_url,
        "has_payment_method": data.get("has_payment_method"),
        "require_payment": data.get("require_payment"),
        "is_subs_active": data.get("is_subs_active"),
        "can_subscribe": data.get("can_subscribe"),
        "show_subs_upsell": data.get("show_subs_upsell"),
        "user_full_name": data.get("user_full_name"),
        "user_email": data.get("user_email"),
        "user_avatar_url": data.get("user_avatar_url"),
        "payment_method": data.get("payment_method"),
        "action_url": data.get("action_url"),
        "subs_tier_id": data.get("subs_tier_id"),
        "subs_tier_name": data.get("subs_tier_name"),
        "is_subs_upgrade_available": data.get("is_subs_upgrade_available"),
        "subs_usage": data.get("subs_usage") if isinstance(data.get("subs_usage"), dict) else {},
    }


async def save_minted_credentials(user_id: str, minted: dict[str, Any]) -> dict[str, Any]:
    """Persist minted Muse `api_key` into `user_oauth_tokens` and cache `base_url` + `subs_usage` in Redis."""
    from src.dalton.credentials import credential_user_id

    api_key = str(minted.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Muse API key 换发失败：缺少 api_key")

    base_url = str(minted.get("base_url") or MUSE_DEFAULT_BASE_URL).strip() or MUSE_DEFAULT_BASE_URL
    subs_tier_name = str(minted.get("subs_tier_name") or "").strip()
    plan_type = subs_tier_name or str(minted.get("plan_type") or "muse").strip() or "muse"
    email = minted.get("user_email") if "user_email" in minted else minted.get("email")
    expires_at = datetime.now(timezone.utc) + timedelta(days=MUSE_KEY_EXPIRES_DAYS)

    await upsert_oauth_tokens(
        user_id=user_id,
        provider=MUSE_PROVIDER,
        access_token=api_key,
        refresh_token="",
        account_id=MUSE_API_KEY_ACCOUNT_ID,
        email=email,
        plan_type=plan_type,
        expires_at=expires_at,
    )

    cred_user_id = credential_user_id(user_id) if user_id else ""
    if cred_user_id:
        try:
            await invalidate_oauth_active_cache(cred_user_id)
        except Exception as exc:
            logger.debug("[muse_oauth] invalidate_oauth_active_cache skipped: %s", type(exc).__name__)

    meta_payload = {
        "base_url": base_url,
        "has_payment_method": minted.get("has_payment_method"),
        "require_payment": minted.get("require_payment"),
        "is_subs_active": minted.get("is_subs_active"),
        "can_subscribe": minted.get("can_subscribe"),
        "show_subs_upsell": minted.get("show_subs_upsell"),
        "user_full_name": minted.get("user_full_name"),
        "user_email": email,
        "subs_tier_id": minted.get("subs_tier_id"),
        "subs_tier_name": subs_tier_name or plan_type,
        "is_subs_upgrade_available": minted.get("is_subs_upgrade_available"),
        "subs_usage": minted.get("subs_usage") if isinstance(minted.get("subs_usage"), dict) else {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if user_id:
        await cache_muse_meta(user_id, meta_payload)
        await _record_provider_event_safe(
            user_id,
            "refresh_ok",
            {"plan_type": plan_type, "is_subs_active": bool(minted.get("is_subs_active"))},
        )

    return {
        "api_key": api_key,
        "base_url": base_url,
        "plan_type": plan_type,
        "email": email,
        "expires_at": expires_at,
        "is_subs_active": minted.get("is_subs_active"),
        "subs_tier_id": minted.get("subs_tier_id"),
        "subs_tier_name": subs_tier_name or plan_type,
        "subs_usage": meta_payload["subs_usage"],
        "_saved": True,
    }


async def poll_device_authorization(
    device_code: str,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Single poll attempt for device authorization; on approval, immediately mints a model API key.

    The transient OAuth `access_token` is used once to call `mint_api_key` and discarded without
    ever touching disk or database. If `user_id` is supplied, the minted `api_key` is persisted
    via `upsert_oauth_tokens` and `base_url` / `subs_usage` are cached in Redis.
    """
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
            data = _json_object(resp)
            oauth_access_token = str(data.get("access_token") or "").strip()
            if not oauth_access_token:
                raise ValueError("Muse device token response did not include an access_token.")
            minted = await mint_api_key(oauth_access_token)
            del oauth_access_token
            if user_id is not None:
                return await save_minted_credentials(user_id, minted)
            return minted

        error = ""
        try:
            raw_error = _json_object(resp).get("error")
            if isinstance(raw_error, str) and _SAFE_ERROR_CODE_RE.fullmatch(raw_error):
                error = raw_error
        except Exception:
            pass
        if error in ("authorization_pending", "slow_down"):
            return None
        if error == "access_denied":
            raise ValueError("The sign-in request was denied.")
        if error == "expired_token":
            raise ValueError("The sign-in request expired before it was approved.")
        raise ValueError(f"Muse sign-in failed (HTTP {resp.status_code}: {error or 'unknown'})")


async def get_valid_token(user_id: str) -> dict[str, Any] | None:
    """Return the stored minted Muse API key for `user_id`, or `None` if missing, legacy, or invalidated.

    Muse device OAuth does not issue a `refresh_token`, so tokens are never refreshed.
    Invalidation is determined solely by the database row (`account_id == 'muse-api-key:invalid'`),
    and legacy un-minted OAuth tokens (lacking `muse-api-key:v1`) are rejected.
    """
    from src.dalton.credentials import credential_user_id

    cred_user_id = credential_user_id(user_id)
    tokens = await get_oauth_tokens(cred_user_id, MUSE_PROVIDER)
    if not tokens:
        return None

    if is_invalidated_muse_token(tokens):
        logger.warning(
            "[muse_oauth] Muse API key for user_id=%s is marked invalid; please reconnect Muse.",
            user_id,
        )
        return None

    if not is_minted_muse_token(tokens):
        logger.warning(
            "[muse_oauth] Legacy un-minted Muse OAuth token detected for user_id=%s; require reconnect.",
            user_id,
        )
        await _record_provider_event_safe(
            user_id,
            "auth_expired",
            {
                "error_kind": "auth_expired",
                "reason": "legacy_unminted_token",
                "message": "检测到未换发 API key 的旧版 Muse token，请重新连接 Muse。",
            },
        )
        return None

    now = datetime.now(timezone.utc)
    if token_expiry(tokens) <= now:
        logger.warning("[muse_oauth] Muse API key expired for user_id=%s; please reconnect Muse.", user_id)
        return None

    meta = await get_muse_cached_meta(user_id)
    base_url = str((meta or {}).get("base_url") or MUSE_DEFAULT_BASE_URL).strip() or MUSE_DEFAULT_BASE_URL
    set_active_muse_context(
        user_id=user_id,
        cred_user_id=cred_user_id,
        base_url=base_url,
    )
    return {
        "access_token": tokens["access_token"],
        "plan_type": tokens.get("plan_type") or "muse",
        "base_url": base_url,
    }
