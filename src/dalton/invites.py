"""Dalton team member lookup (`DALTON_ALLOWED_EMAILS ∪ dalton_members`) and single-use invite signup."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import HTTPException, Request

from src.dalton.database import members as members_db
from src.dalton.settings import (
    get_configured_pm_user_ids,
    get_dalton_allowed_emails,
    get_dalton_frontend_url,
    get_dalton_invite_email_domain,
    get_dalton_trusted_proxies,
    get_supabase_secret_key,
    get_supabase_url,
)
from src.server.database.pool import get_db_connection
from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)

INVITE_TTL_DAYS = 7
MAX_ACTIVE_INVITES_PER_USER = 20
MEMBER_CACHE_TTL_S = 60.0
INVITE_RATE_LIMIT_WINDOW_S = 60
DEFAULT_INVITE_RATE_LIMIT_PER_MINUTE = 20
ADMIN_USERS_PER_PAGE = 100
MAX_ADMIN_USER_PAGES = 1000

USERNAME_PATTERN = re.compile(r"^[a-z0-9._-]{3,32}$")
MIN_PASSWORD_LENGTH = 10

INVITE_NOT_CONFIGURED_DETAIL = "邀请注册未配置"
INVITE_LIMIT_EXCEEDED_DETAIL = "你当前有效的邀请已达 20 个上限，请先撤销不用的邀请"

_STATUS_LABELS: dict[str, str] = {
    "pending": "待使用",
    "used": "已使用",
    "expired": "已过期",
    "revoked": "已撤销",
}

# Per-worker positive-only cache: maps lowercase email -> expires_at_monotonic.
# Negative results are NEVER cached, so when a user redeems an invite on Worker A
# and immediately logs in against Worker B, Worker B misses its positive cache and
# queries Postgres directly, seeing the new member immediately.
_positive_member_cache: dict[str, float] = {}


def invalidate_team_members_cache() -> None:
    """Clear the per-worker positive member email cache."""
    _positive_member_cache.clear()


def record_team_member_cached(
    email: str,
    *,
    cache: dict[str, float] | None = None,
) -> None:
    """Record a positive member email in the per-worker cache for `MEMBER_CACHE_TTL_S`."""
    normalized = str(email or "").strip().lower()
    if not normalized:
        return
    target = _positive_member_cache if cache is None else cache
    target[normalized] = time.monotonic() + MEMBER_CACHE_TTL_S


async def _fetch_db_member_emails_live(
    *,
    cache: dict[str, float] | None = None,
) -> set[str]:
    """Query `dalton_members` directly and populate positive cache entries for found emails."""
    try:
        emails = await members_db.list_member_emails()
    except RuntimeError as exc:
        if "pool is not open" in str(exc):
            return set()
        logger.debug("Failed to query dalton_members emails: %s", exc)
        return set()
    except Exception as exc:
        logger.debug("Failed to query dalton_members emails: %s", exc)
        return set()

    target = _positive_member_cache if cache is None else cache
    exp = time.monotonic() + MEMBER_CACHE_TTL_S
    normalized_set: set[str] = set()
    for raw in emails:
        cleaned = str(raw or "").strip().lower()
        if cleaned:
            normalized_set.add(cleaned)
            target[cleaned] = exp
    return normalized_set


async def get_team_member_emails(*, bypass_cache: bool = False) -> set[str]:
    """Return the union `DALTON_ALLOWED_EMAILS ∪ dalton_members` (lowercase)."""
    env_emails = set(get_dalton_allowed_emails())
    db_emails = await _fetch_db_member_emails_live()
    return env_emails | db_emails


async def is_team_member(
    email: str | None,
    *,
    bypass_cache: bool = False,
    cache: dict[str, float] | None = None,
) -> bool:
    """Return True if `email` belongs to `DALTON_ALLOWED_EMAILS` or `dalton_members`.

    Only positive memberships are cached (max 60s TTL). Cache misses always query
    `dalton_members` directly and never cache negative results.
    """
    normalized = str(email or "").strip().lower()
    if not normalized:
        return False
    if normalized in get_dalton_allowed_emails():
        return True

    target_cache = _positive_member_cache if cache is None else cache
    now = time.monotonic()
    if not bypass_cache:
        expires_at = target_cache.get(normalized)
        if expires_at is not None:
            if now < expires_at:
                return True
            target_cache.pop(normalized, None)

    db_emails = await _fetch_db_member_emails_live(cache=target_cache)
    return normalized in db_emails


def is_invite_pm(user_id: str | None) -> bool:
    """Return True if `user_id` is in explicitly configured `DALTON_PM_USER_IDS`."""
    uid = str(user_id or "").strip()
    if not uid:
        return False
    return uid in set(get_configured_pm_user_ids())


async def get_user_tier(user_id: str | None) -> str:
    """Return the caller's access tier (`admin`, `internal`, or `external`)."""
    from src.dalton.access_tiers import get_user_tier as _get_user_tier

    return await _get_user_tier(user_id)


async def require_pm_for_invites(user_id: str | None) -> tuple[str, str]:
    """Verify caller is an authenticated internal member (`admin` or `internal`)."""
    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    tier = await get_user_tier(uid)
    if tier not in ("admin", "internal"):
        raise HTTPException(status_code=403, detail="仅内部成员可管理邀请链接")
    return uid, tier


def _require_invite_supabase_config() -> tuple[str, str]:
    """Return `(supabase_url, supabase_secret_key)` or raise HTTP 503 `邀请注册未配置`."""
    supabase_url = get_supabase_url()
    secret_key = get_supabase_secret_key()
    if not supabase_url or not secret_key:
        raise HTTPException(status_code=503, detail=INVITE_NOT_CONFIGURED_DETAIL)
    return supabase_url, secret_key


def hash_invite_token(token: str) -> str:
    """Return lowercase SHA-256 hex digest of a raw invite token."""
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _verify_token_digest(row: dict[str, Any] | None, expected_sha256: str) -> bool:
    """Constant-time verification of `row['token_sha256']` against `expected_sha256`."""
    if not row:
        return False
    stored = str(row.get("token_sha256") or "").strip().lower()
    expected = str(expected_sha256 or "").strip().lower()
    if not stored or not expected:
        return False
    return hmac.compare_digest(stored, expected)


def _resolve_invite_frontend_base_url() -> str:
    """Return the frontend base URL used to build `{DALTON_FRONTEND_URL}/invite#{token}`."""
    url = get_dalton_frontend_url()
    if url:
        return url.rstrip("/")
    for key in ("DALTON_FRONTEND_URL", "PUBLIC_APP_URL", "FRONTEND_URL"):
        val = os.getenv(key, "").strip().rstrip("/")
        if val:
            return val
    return "http://localhost:5173"


def _ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return _ensure_utc(dt).isoformat()  # type: ignore[union-attr]
    return str(dt)


def compute_invite_status(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Compute `(status_code, status_label_zh)` for an invite row."""
    ref_now = _ensure_utc(now) or datetime.now(timezone.utc)
    if row.get("used_at") is not None:
        return "used", _STATUS_LABELS["used"]
    if row.get("revoked_at") is not None:
        return "revoked", _STATUS_LABELS["revoked"]
    expires_at = _ensure_utc(row.get("expires_at"))
    if expires_at is not None and expires_at <= ref_now:
        return "expired", _STATUS_LABELS["expired"]
    return "pending", _STATUS_LABELS["pending"]


def _format_invite_summary(
    row: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    status, status_label = compute_invite_status(row, now=now)
    used_by_username = row.get("used_by_username")
    if not used_by_username and row.get("used_by_email"):
        used_email = str(row["used_by_email"]).strip()
        used_by_username = used_email.split("@", 1)[0] if "@" in used_email else used_email
    return {
        "invite_id": str(row.get("invite_id") or ""),
        "status": status,
        "status_label": status_label,
        "created_at": _iso(row.get("created_at")),
        "expires_at": _iso(row.get("expires_at")),
        "used_at": _iso(row.get("used_at")),
        "revoked_at": _iso(row.get("revoked_at")),
        "used_by_username": used_by_username,
        "username": used_by_username,
    }


def validate_invite_credentials(username: str, password: str) -> tuple[str, str]:
    """Validate `username` and `password` rules and return `(username, email)`."""
    raw_username = str(username if username is not None else "")
    if not raw_username or not USERNAME_PATTERN.fullmatch(raw_username):
        raise HTTPException(
            status_code=400,
            detail="用户名须为 3–32 位小写字母、数字或 ._-",
        )
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="密码至少需要 10 位",
        )
    domain = get_dalton_invite_email_domain()
    email = f"{raw_username}@{domain}".lower()
    return raw_username, email


def _unwrap_ipv4_mapped(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Convert IPv4-mapped IPv6 addresses (`::ffff:a.b.c.d`) to `IPv4Address`."""
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped
    return addr


def _is_trusted_proxy_ip(ip_str: str, trusted_specs: tuple[str, ...]) -> bool:
    """Return True if `ip_str` matches any IP or CIDR in `trusted_specs`."""
    cleaned = str(ip_str or "").strip()
    if not cleaned:
        return False
    try:
        addr = _unwrap_ipv4_mapped(ipaddress.ip_address(cleaned))
    except ValueError:
        return cleaned in trusted_specs

    for spec in trusted_specs:
        s = spec.strip()
        if not s:
            continue
        try:
            if "/" in s:
                net = ipaddress.ip_network(s, strict=False)
                if addr in net:
                    return True
            else:
                if addr == _unwrap_ipv4_mapped(ipaddress.ip_address(s)):
                    return True
        except ValueError:
            if cleaned == s:
                return True
    return False


def _extract_client_ip(request: Request | None) -> str:
    """Resolve client IP from `request.client.host`, trusting `X-Forwarded-For` only from `DALTON_TRUSTED_PROXIES`."""
    if request is None:
        return "local"
    client = getattr(request, "client", None)
    peer_host = str(getattr(client, "host", "") or "").strip() if client is not None else ""
    if not peer_host:
        peer_host = "unknown"

    trusted_proxies = get_dalton_trusted_proxies()
    if not _is_trusted_proxy_ip(peer_host, trusted_proxies):
        return peer_host

    headers = getattr(request, "headers", None)
    xff = headers.get("x-forwarded-for") if headers else None
    if not xff:
        return peer_host

    chain = [part.strip() for part in str(xff).split(",") if part.strip()]
    if not chain:
        return peer_host

    # Walk right-to-left and pick the first address that is NOT a trusted proxy.
    for candidate in reversed(chain):
        if not _is_trusted_proxy_ip(candidate, trusted_proxies):
            return candidate
    return chain[0]


def _get_rate_limit_per_minute() -> int:
    raw = os.getenv("DALTON_INVITE_RATE_LIMIT_PER_MINUTE", "").strip()
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_INVITE_RATE_LIMIT_PER_MINUTE


async def enforce_invite_rate_limit(
    request: Request | None,
    *,
    action: str = "invite",
) -> None:
    """Enforce per-IP per-minute rate limit via Redis (`INCR` + `EXPIRE`).

    Fails closed with HTTP 503 when Redis is disabled or unreachable.
    """
    ip = _extract_client_ip(request)
    limit = _get_rate_limit_per_minute()
    bucket = int(time.time() // INVITE_RATE_LIMIT_WINDOW_S)
    redis_key = f"dalton:invite:ratelimit:{action}:{ip}:{bucket}"

    try:
        cache = get_cache_client()
    except Exception as exc:
        logger.error("Failed to obtain Redis cache client for invite rate limit: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="限流服务不可用，请稍后重试") from exc

    if cache is None or not getattr(cache, "enabled", False) or getattr(cache, "client", None) is None:
        raise HTTPException(status_code=503, detail="限流服务不可用，请稍后重试")

    try:
        count = int(await cache.client.incr(redis_key))
        if count == 1:
            await cache.client.expire(redis_key, INVITE_RATE_LIMIT_WINDOW_S)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Redis invite rate limit failed: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="限流服务不可用，请稍后重试") from exc

    if count > limit:
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


async def create_invite_for_pm(user_id: str | None) -> dict[str, Any]:
    """Create a 7-day single-use invite token for internal members (`admin` or `internal`).

    Internal members are capped at 20 active (unused, unrevoked, unexpired) invites,
    enforced under a per-creator transaction-scoped advisory lock; admins are exempt.
    Link uses URL fragment (`{DALTON_FRONTEND_URL}/invite#{token}`) so the token
    is never sent in HTTP request paths or server access logs.
    """
    uid, tier = await require_pm_for_invites(user_id)
    _require_invite_supabase_config()

    token = secrets.token_urlsafe(32)
    token_sha256 = hash_invite_token(token)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=INVITE_TTL_DAYS)

    if tier == "admin":
        row = await members_db.insert_invite(
            token_sha256=token_sha256,
            created_by_user_id=uid,
            expires_at=expires_at,
        )
    else:
        async with get_db_connection() as conn:
            async with conn.transaction():
                await members_db.lock_invite_creator(uid, conn=conn)
                active_count = await members_db.count_active_invites_by_creator(
                    uid, conn=conn
                )
                if active_count >= MAX_ACTIVE_INVITES_PER_USER:
                    raise HTTPException(
                        status_code=409,
                        detail=INVITE_LIMIT_EXCEEDED_DETAIL,
                    )
                row = await members_db.insert_invite(
                    token_sha256=token_sha256,
                    created_by_user_id=uid,
                    expires_at=expires_at,
                    conn=conn,
                )
    frontend_base = _resolve_invite_frontend_base_url()
    invite_url = f"{frontend_base}/invite#{token}"
    summary = _format_invite_summary(row, now=now)
    return {
        **summary,
        "token": token,
        "url": invite_url,
        "invite_url": invite_url,
    }


async def list_invites_for_pm(user_id: str | None) -> list[dict[str, Any]]:
    """Return invites for management view without exposing token or token hash.

    Internal members see only their own invites (`created_by_user_id == uid` filtered in SQL);
    admins see all invites.
    """
    uid, tier = await require_pm_for_invites(user_id)
    if tier == "admin":
        rows = await members_db.list_invites()
    else:
        rows = await members_db.list_invites(created_by_user_id=uid)
    now = datetime.now(timezone.utc)
    return [_format_invite_summary(r, now=now) for r in rows]


async def revoke_invite_for_pm(user_id: str | None, invite_id: str) -> dict[str, Any]:
    """Revoke an unused invite (`admin` can revoke any; `internal` only their own via single UPDATE)."""
    uid, tier = await require_pm_for_invites(user_id)
    if tier == "admin":
        existing = await members_db.get_invite_by_id(invite_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="邀请不存在")
        if existing.get("used_at") is not None:
            raise HTTPException(status_code=409, detail="邀请已被使用，无法撤销")

        updated = await members_db.revoke_invite(invite_id)
        if updated is None:
            raise HTTPException(status_code=409, detail="邀请已被使用，无法撤销")
        return _format_invite_summary(updated)

    updated = await members_db.revoke_invite(invite_id, created_by_user_id=uid)
    if updated is None:
        raise HTTPException(status_code=404, detail="邀请不存在")
    return _format_invite_summary(updated)


async def check_invite_token(token: str) -> dict[str, Any]:
    """Public check returning ONLY `valid: bool` and `reason: str | None`."""
    cleaned = str(token or "").strip()
    if not cleaned:
        return {"valid": False, "reason": "not_found"}

    token_sha256 = hash_invite_token(cleaned)
    row = await members_db.get_invite_by_token_hash(token_sha256)
    if not _verify_token_digest(row, token_sha256):
        return {"valid": False, "reason": "not_found"}

    status, _ = compute_invite_status(row)  # type: ignore[arg-type]
    if status != "pending":
        return {"valid": False, "reason": status}

    return {"valid": True, "reason": None}


def _response_indicates_existing_user(resp: httpx.Response) -> bool:
    """Return True if a Supabase Admin create-user response indicates the email already exists."""
    if resp.status_code not in (400, 409, 422):
        return False
    try:
        data = resp.json()
    except Exception:
        data = None
    if isinstance(data, dict):
        code = str(data.get("error_code") or data.get("code") or "").lower()
        msg = " ".join(
            str(data.get(k) or "")
            for k in ("msg", "message", "error_description", "error")
        ).lower()
        if "exists" in code or "already" in code:
            return True
        if any(tok in msg for tok in ("already", "exists", "registered", "duplicate")):
            return True
    text = (resp.text or "").lower()
    return any(tok in text for tok in ("already", "exists", "registered", "duplicate"))


async def _find_existing_supabase_user_by_email(
    *,
    client: httpx.AsyncClient,
    admin_users_url: str,
    headers: dict[str, str],
    email: str,
) -> dict[str, Any] | None:
    """Paginate `GET /auth/v1/admin/users` until the user matching `email` is found or a page returns fewer than `ADMIN_USERS_PER_PAGE` users."""
    target = email.strip().lower()
    for page in range(1, MAX_ADMIN_USER_PAGES + 1):
        sep = "&" if "?" in admin_users_url else "?"
        page_url = f"{admin_users_url}{sep}page={page}&per_page={ADMIN_USERS_PER_PAGE}"
        resp = await client.get(page_url, headers=headers)
        if resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except Exception:
            return None

        users: list[Any]
        if isinstance(payload, dict) and isinstance(payload.get("users"), list):
            users = payload["users"]
        elif isinstance(payload, list):
            users = payload
        elif isinstance(payload, dict) and payload.get("email"):
            users = [payload]
        else:
            users = []

        if not users:
            return None

        for user in users:
            if isinstance(user, dict) and str(user.get("email") or "").strip().lower() == target:
                return user

        if len(users) < ADMIN_USERS_PER_PAGE:
            return None

    logger.error(
        "Supabase Admin API user lookup exceeded safety limit (%s pages) for email=%s",
        MAX_ADMIN_USER_PAGES,
        target,
    )
    raise HTTPException(status_code=502, detail="创建账户失败，请稍后重试")


async def _update_supabase_user_password(
    *,
    client: httpx.AsyncClient,
    admin_users_url: str,
    headers: dict[str, str],
    supabase_user_id: str,
    password: str,
) -> dict[str, Any]:
    """Update an existing Supabase user's password via `PUT /auth/v1/admin/users/{id}`."""
    user_url = f"{admin_users_url.rstrip('/')}/{supabase_user_id}"
    resp = await client.put(
        user_url,
        json={"password": password, "email_confirm": True},
        headers=headers,
    )
    if resp.status_code not in (200, 201):
        logger.error(
            "Supabase Admin API failed to update password for user_id=%s (status=%s)",
            supabase_user_id,
            resp.status_code,
        )
        raise HTTPException(status_code=502, detail="创建账户失败，请稍后重试")
    try:
        data = resp.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {"id": supabase_user_id}


async def ensure_supabase_invite_user(
    *,
    supabase_url: str,
    secret_key: str,
    email: str,
    password: str,
    invite_id: str,
    allow_existing_retry: bool = False,
) -> dict[str, Any]:
    """Create user via Supabase Admin API, or recover & update password on bound invite retry.

    - Writes `app_metadata: {"dalton_invite_id": invite_id}` on creation so only
      accounts created by this invite carry its ownership tag.
    - If the email already exists in Supabase, only recovers and updates the
      password when `allow_existing_retry` is True AND the existing user's
      `app_metadata.dalton_invite_id == invite_id`; otherwise raises HTTP 409
      `用户名已被占用` without modifying the account.
    - Never logs `secret_key`.
    """
    admin_users_url = f"{supabase_url.rstrip('/')}/auth/v1/admin/users"
    headers = {
        "apikey": secret_key,
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }
    body = {
        "email": email,
        "password": password,
        "email_confirm": True,
        "app_metadata": {"dalton_invite_id": invite_id},
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(admin_users_url, json=body, headers=headers)
            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                return data if isinstance(data, dict) else {"email": email}

            if _response_indicates_existing_user(resp):
                if not allow_existing_retry:
                    raise HTTPException(status_code=409, detail="用户名已被占用")

                existing = await _find_existing_supabase_user_by_email(
                    client=client,
                    admin_users_url=admin_users_url,
                    headers=headers,
                    email=email,
                )
                if existing is not None:
                    app_metadata = existing.get("app_metadata")
                    existing_invite_id = (
                        str(app_metadata.get("dalton_invite_id") or "").strip()
                        if isinstance(app_metadata, dict)
                        else ""
                    )
                    if not invite_id or existing_invite_id != invite_id:
                        raise HTTPException(status_code=409, detail="用户名已被占用")

                    existing_id = str(existing.get("id") or "").strip()
                    if not existing_id:
                        raise HTTPException(status_code=502, detail="创建账户失败，请稍后重试")
                    await _update_supabase_user_password(
                        client=client,
                        admin_users_url=admin_users_url,
                        headers=headers,
                        supabase_user_id=existing_id,
                        password=password,
                    )
                    return existing
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Supabase Admin API request failed for email=%s: %s",
            email,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="创建账户失败，请稍后重试") from exc

    logger.error(
        "Supabase Admin API rejected user creation for email=%s (status=%s)",
        email,
        resp.status_code,
    )
    raise HTTPException(status_code=502, detail="创建账户失败，请稍后重试")


async def redeem_invite_token(
    *,
    token: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    """Two-phase invite redemption bound to `redeeming_username` and `supabase_user_id`.

    Phase 1 (tx 1): Lock invite row (`SELECT ... FOR UPDATE`), constant-time verify
    `token_sha256`, verify invite is pending and either unclaimed or already
    redeeming the same `username`, verify no conflict in `dalton_members` /
    `DALTON_ALLOWED_EMAILS`, and persist `redeeming_username` + `redeeming_at`.
    Phase 2: Create user in Supabase with `app_metadata.dalton_invite_id` (or, if
    retrying an already-redeeming invite for the same username whose Supabase user
    bears matching `app_metadata.dalton_invite_id`, update their password).
    Phase 3 (tx 2): Lock invite row, strictly verify `locked_redeeming == normalized_username`,
    insert `dalton_members`, and mark invite used with `supabase_user_id`.
    """
    cleaned_token = str(token or "").strip()
    if not cleaned_token:
        raise HTTPException(status_code=400, detail="邀请链接无效或已过期")

    normalized_username, email = validate_invite_credentials(username, password)
    supabase_url, secret_key = _require_invite_supabase_config()
    token_sha256 = hash_invite_token(cleaned_token)

    # Phase 1: Lock invite, verify state & conflicts, record `redeeming_username` + `redeeming_at`
    async with get_db_connection() as conn:
        async with conn.transaction():
            invite = await members_db.lock_invite_by_token_hash(token_sha256, conn=conn)
            if not _verify_token_digest(invite, token_sha256):
                raise HTTPException(status_code=400, detail="邀请链接无效或已过期")
            assert invite is not None

            status, _ = compute_invite_status(invite)
            if status != "pending":
                raise HTTPException(status_code=400, detail="邀请链接无效或已过期")

            prior_redeeming_username = (
                str(invite.get("redeeming_username") or "").strip().lower() or None
            )
            is_retry = prior_redeeming_username is not None
            if is_retry and prior_redeeming_username != normalized_username:
                raise HTTPException(status_code=409, detail="用户名已被占用")

            existing_by_username = await members_db.get_member_by_username(
                normalized_username,
                conn=conn,
            )
            if existing_by_username is not None:
                raise HTTPException(status_code=409, detail="用户名已被占用")

            existing_by_email = await members_db.get_member_by_email(
                email,
                conn=conn,
            )
            if existing_by_email is not None or email in get_dalton_allowed_emails():
                raise HTTPException(status_code=409, detail="用户名已被占用")

            invite_id = str(invite["invite_id"])
            invited_by_user_id = str(invite.get("created_by_user_id") or "") or None
            claimed = await members_db.mark_invite_redeeming(
                invite_id=invite_id,
                redeeming_username=normalized_username,
                conn=conn,
            )
            if claimed is None:
                raise HTTPException(status_code=400, detail="邀请链接无效或已过期")

    # Phase 2: Create or recover user in Supabase Admin API
    try:
        supabase_user = await ensure_supabase_invite_user(
            supabase_url=supabase_url,
            secret_key=secret_key,
            email=email,
            password=password,
            invite_id=invite_id,
            allow_existing_retry=is_retry,
        )
    except HTTPException as exc:
        if exc.status_code == 409:
            try:
                await members_db.clear_invite_redeeming(
                    invite_id=invite_id,
                    redeeming_username=normalized_username,
                )
            except Exception as clear_exc:
                logger.warning(
                    "Failed to clear redeeming state for invite_id=%s username=%s: %s",
                    invite_id,
                    normalized_username,
                    type(clear_exc).__name__,
                )
        raise

    supabase_user_id = str((supabase_user or {}).get("id") or "").strip() or None

    # Phase 3: Persist member row and mark invite used with `supabase_user_id`
    async with get_db_connection() as conn:
        async with conn.transaction():
            locked_invite = await members_db.lock_invite_by_token_hash(token_sha256, conn=conn)
            if not _verify_token_digest(locked_invite, token_sha256):
                raise HTTPException(status_code=400, detail="邀请链接无效或已过期")
            assert locked_invite is not None

            status, _ = compute_invite_status(locked_invite)
            if status != "pending":
                raise HTTPException(status_code=400, detail="邀请链接无效或已过期")

            locked_redeeming = (
                str(locked_invite.get("redeeming_username") or "").strip().lower() or None
            )
            if locked_redeeming != normalized_username:
                raise HTTPException(status_code=409, detail="用户名已被占用")

            await members_db.insert_member(
                email=email,
                username=normalized_username,
                source="invite",
                invited_by_user_id=invited_by_user_id,
                conn=conn,
            )
            marked = await members_db.mark_invite_used(
                invite_id=invite_id,
                used_by_email=email,
                supabase_user_id=supabase_user_id,
                conn=conn,
            )
            if marked is None:
                raise HTTPException(status_code=400, detail="邀请链接无效或已过期")

    record_team_member_cached(email)
    return {
        "ok": True,
        "username": normalized_username,
        "email": email,
        "supabase_user_id": supabase_user_id,
    }
