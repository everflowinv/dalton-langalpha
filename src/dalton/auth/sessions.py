"""Local account login, atomic rate-limiting, stable-sid rotating sessions, and password updates.

Concurrency & security invariants:
- Login, refresh, change-password, CLI reset-password, and account-level revocation all
  serialize on the same per-account PostgreSQL transaction advisory lock
  (``account_advisory_lock_key(user_id)``) and re-verify ``password_changed_at`` /
  session revocation state under that lock.
- Rate-limit attempts are atomically registered BEFORE scheduling off-loop ``scrypt``
  verification; identified accounts are keyed uniformly by ``user_id`` (merging email
  and username aliases), and client IP honors ``X-Forwarded-For`` ONLY via
  ``DALTON_TRUSTED_PROXY_HOPS`` from trusted proxies (default 0 = direct peer IP only).
- ``session_id`` (``sid``) remains stable across refresh rotations; rotated refresh token
  hashes are tracked in ``dalton_local_refresh_tokens`` for bounded, once-only replay
  detection within their original ``expires_at`` window.
- Every access-token request verifies the session row is active, unrevoked, unexpired,
  and matches ``dalton_local_credentials.password_changed_at``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import HTTPException, Request
from psycopg.rows import dict_row

from src.dalton.auth.passwords import (
    DUMMY_PASSWORD_HASH,
    acquire_scrypt_slot,
    hash_password_async,
    validate_password_policy,
    verify_password_async,
)
from src.dalton.auth.roles import get_user_role
from src.dalton.auth.tokens import (
    generate_refresh_token,
    get_refresh_session_ttl_seconds,
    hash_refresh_token,
    issue_local_access_token,
)
from src.dalton.company_identity import (
    COMPANY_SYSTEM_EMAIL,
    reject_company_system_auth,
)
from src.dalton.settings import (
    get_dalton_trusted_proxies,
    get_dalton_trusted_proxy_hops,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

ATTEMPT_WINDOW_SECONDS = 15 * 60  # 15 minutes
DEFAULT_REFRESH_CONCURRENT_GRACE_SECONDS = 10


def get_refresh_concurrent_grace_seconds() -> int:
    """Return grace window (seconds) where a just-rotated refresh token is treated as concurrent refresh (HTTP 409)."""
    raw = os.getenv("DALTON_REFRESH_GRACE_SECONDS", "").strip()
    if raw:
        try:
            return max(0, min(60, int(raw)))
        except ValueError:
            pass
    return DEFAULT_REFRESH_CONCURRENT_GRACE_SECONDS


async def _read_post_lock_clock(cur: Any, override_now: datetime | None = None) -> datetime:
    """Read PostgreSQL ``clock_timestamp()`` AFTER lock acquisition (not transaction-start ``NOW()``)."""
    await cur.execute("SELECT clock_timestamp() AS db_now")
    row = await cur.fetchone()
    if override_now is not None:
        return _as_utc(override_now) or _utcnow()
    db_now = _as_utc(row["db_now"]) if row and row.get("db_now") is not None else None
    return db_now or _utcnow()

_ACCOUNT_BACKOFF_SCHEDULE: tuple[tuple[int, int], ...] = (
    (9, 900),
    (8, 300),
    (7, 120),
    (6, 60),
    (5, 30),
)

_IP_BACKOFF_SCHEDULE: tuple[tuple[int, int], ...] = (
    (25, 900),
    (20, 300),
    (15, 60),
)


@dataclass(frozen=True)
class LocalLoginResult:
    """Authenticated local login payload returned by :func:`authenticate_local_login`."""

    user_id: str
    email: str
    username: str | None
    tier: str
    session_id: str
    access_token: str
    expires_in: int
    refresh_token: str


@dataclass(frozen=True)
class LocalRefreshResult:
    """Rotated refresh session payload returned by :func:`refresh_local_session`."""

    user_id: str
    session_id: str
    access_token: str
    expires_in: int
    refresh_token: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def account_advisory_lock_key(user_id: str) -> int:
    """Deterministic signed 64-bit PostgreSQL advisory lock key per user account."""
    return int.from_bytes(
        hashlib.sha256(f"dalton:auth:account:{str(user_id).strip()}".encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )


def hash_attempt_key(value: str) -> str:
    """Hash a rate-limit dimension value (IP, ``uid:<user_id>``, or ``ident:<identifier>``)."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def account_attempt_key_hash(
    *, user_id: str | None = None, identifier: str | None = None
) -> str:
    """Canonical account rate-limit key hash: ``uid:<user_id>`` when identified, else ``ident:<raw>``."""
    if user_id and str(user_id).strip():
        return hash_attempt_key(f"uid:{str(user_id).strip()}")
    return hash_attempt_key(f"ident:{str(identifier or '').strip().lower()}")


def compute_backoff_seconds(dimension: str, failed_attempts: int) -> int:
    """Return lockout backoff seconds for ``(dimension, failed_attempts)``."""
    count = int(failed_attempts)
    schedule = (
        _IP_BACKOFF_SCHEDULE if dimension == "ip" else _ACCOUNT_BACKOFF_SCHEDULE
    )
    for threshold, delay in schedule:
        if count >= threshold:
            return delay
    return 0


def _ip_in_networks(raw_ip: str, trusted_cidrs: tuple[str, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(raw_ip.strip())
    except ValueError:
        return False
    for cidr in trusted_cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def extract_client_ip(request: Request | None) -> str:
    """Extract client IP using ``DALTON_TRUSTED_PROXY_HOPS`` from the right of ``X-Forwarded-For``.

    - Default (``DALTON_TRUSTED_PROXY_HOPS=0``): ALWAYS returns direct ``request.client.host``.
    - When ``hops > 0`` AND ``request.client.host`` is inside ``DALTON_TRUSTED_PROXIES``:
      if ``hops > len(xff_parts)`` (fewer forwarded hops than configured), returns direct
      ``peer_host`` rather than trusting a client-supplied leftmost value; otherwise builds
      ``chain = [*xff_parts, peer_host]`` and selects ``chain[-(hops + 1)]``.
    """
    if request is None:
        return "unknown"
    client = getattr(request, "client", None)
    peer_host = str(getattr(client, "host", "") or "").strip() if client else ""
    if not peer_host:
        return "unknown"

    hops = get_dalton_trusted_proxy_hops()
    if hops <= 0:
        return peer_host

    trusted = tuple(get_dalton_trusted_proxies() or ())
    if not trusted or not _ip_in_networks(peer_host, trusted):
        return peer_host

    headers = getattr(request, "headers", None)
    xff = str(headers.get("x-forwarded-for", "") or "").strip() if headers else ""
    if not xff:
        return peer_host

    xff_parts = [part.strip() for part in xff.split(",") if part.strip()]
    if not xff_parts or hops > len(xff_parts):
        return peer_host

    chain = [*xff_parts, peer_host]
    idx = len(chain) - (hops + 1)
    return chain[idx] or peer_host


def _retry_after_seconds(locked_until: datetime, now: datetime) -> int:
    delta = (_as_utc(locked_until) - now).total_seconds()  # type: ignore[operator]
    return max(1, int(math.ceil(delta)))


def _raise_rate_limited(retry_after: int) -> None:
    raise HTTPException(
        status_code=429,
        detail=f"Too many failed authentication attempts. Try again in {retry_after} seconds.",
        headers={"Retry-After": str(retry_after)},
    )


async def _atomic_reserve_attempt_dimension(
    cur: Any,
    *,
    dimension: str,
    key_hash: str,
    now: datetime,
) -> tuple[bool, int, datetime | None]:
    """Atomically check lock & increment attempt counter in a single upsert statement.

    Returns ``(already_locked, new_count, locked_until)``.
    If already locked (``locked_until > now``), leaves counters intact and signals 429.
    """
    window_cutoff = now - timedelta(seconds=ATTEMPT_WINDOW_SECONDS)
    await cur.execute(
        """
        INSERT INTO dalton_auth_attempts (
            dimension, key_hash, failed_attempts, window_started_at,
            last_failed_at, locked_until, updated_at
        )
        VALUES (%s, %s, 0, %s, NULL, NULL, %s)
        ON CONFLICT (dimension, key_hash) DO NOTHING
        """,
        (dimension, key_hash, now, now),
    )
    await cur.execute(
        """
        SELECT failed_attempts, window_started_at, locked_until
        FROM dalton_auth_attempts
        WHERE dimension = %s AND key_hash = %s
        FOR UPDATE
        """,
        (dimension, key_hash),
    )
    row = await cur.fetchone()
    existing_lock = _as_utc(row.get("locked_until")) if row else None
    if existing_lock is not None and existing_lock > now:
        return True, int(row.get("failed_attempts") or 0), existing_lock

    existing_window = _as_utc(row.get("window_started_at")) if row else None
    if existing_window is None or existing_window < window_cutoff:
        new_count = 1
        window_started_at = now
    else:
        new_count = int(row.get("failed_attempts") or 0) + 1
        window_started_at = existing_window

    backoff_s = compute_backoff_seconds(dimension, new_count)
    locked_until = now + timedelta(seconds=backoff_s) if backoff_s > 0 else None
    await cur.execute(
        """
        UPDATE dalton_auth_attempts
        SET failed_attempts = %s,
            window_started_at = %s,
            last_failed_at = %s,
            locked_until = %s,
            updated_at = %s
        WHERE dimension = %s AND key_hash = %s
        """,
        (new_count, window_started_at, now, locked_until, now, dimension, key_hash),
    )
    return False, new_count, locked_until


async def reserve_auth_attempt_or_429(
    *,
    account_key_hash: str,
    ip_key_hash: str | None = None,
    matched_user_id: str | None = None,
    now: datetime | None = None,
) -> tuple[int, datetime | None]:
    """Atomically reserve an authentication attempt BEFORE scheduling scrypt.

    Ordering & isolation invariants:
    1. Checks/reserves IP rate limit FIRST in its own short IP-only transaction. If the IP
       is already locked, immediately raises HTTP 429 WITHOUT incrementing any account counter.
    2. Unknown accounts (``matched_user_id is None``) NEVER insert an ``account`` row into
       ``dalton_auth_attempts`` (counted on IP dimension only).
    3. For identified accounts, acquires locks strictly in the unified order:
       ``pg_advisory_xact_lock(account_advisory_lock_key(user_id))`` ->
       ``dalton_local_credentials FOR UPDATE`` ->
       ``dalton_auth_attempts('account') FOR UPDATE``.
    """
    ts = now or _utcnow()

    # 1. Check & increment IP rate limit first; if already locked -> 429 immediately, no account touch.
    if ip_key_hash is not None:
        ip_retry_after = 0
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    ip_locked, _ip_count, ip_lock = (
                        await _atomic_reserve_attempt_dimension(
                            cur, dimension="ip", key_hash=ip_key_hash, now=ts
                        )
                    )
                    if ip_locked and ip_lock is not None:
                        ip_retry_after = _retry_after_seconds(ip_lock, ts)
        if ip_retry_after > 0:
            _raise_rate_limited(ip_retry_after)

    # 2. Unknown username/email: IP-only tracking (never create account rows in dalton_auth_attempts).
    if not matched_user_id:
        return 0, None

    # 3. Identified account: unified lock order (account advisory lock -> credentials row -> attempts row).
    acct_retry_after = 0
    acct_count = 0
    acct_lock: datetime | None = None
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (account_advisory_lock_key(matched_user_id),),
                )
                await cur.execute(
                    "SELECT user_id FROM dalton_local_credentials WHERE user_id = %s FOR UPDATE",
                    (matched_user_id,),
                )
                cred_row = await cur.fetchone()
                if cred_row is None:
                    return 0, None

                acct_locked, acct_count, acct_lock = (
                    await _atomic_reserve_attempt_dimension(
                        cur, dimension="account", key_hash=account_key_hash, now=ts
                    )
                )
                if acct_locked and acct_lock is not None:
                    acct_retry_after = _retry_after_seconds(acct_lock, ts)
                else:
                    await cur.execute(
                        """
                        UPDATE dalton_local_credentials
                        SET failed_attempts = %s,
                            locked_until = %s,
                            updated_at = %s
                        WHERE user_id = %s
                        """,
                        (acct_count, acct_lock, ts, matched_user_id),
                    )

    if acct_retry_after > 0:
        _raise_rate_limited(acct_retry_after)
    return acct_count, acct_lock


async def _release_ip_attempt_on_success(
    *, ip_key_hash: str, now: datetime
) -> None:
    """Decrement IP counter by 1 on a verified login in a standalone IP-only transaction.

    Best-effort: runs AFTER the auth/password-change transaction has committed, so any
    DB failure is logged and never turns a committed login or password change into a 500.
    """
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT failed_attempts
                        FROM dalton_auth_attempts
                        WHERE dimension = 'ip' AND key_hash = %s
                        FOR UPDATE
                        """,
                        (ip_key_hash,),
                    )
                    row = await cur.fetchone()
                    if row is None:
                        return
                    new_count = max(0, int(row.get("failed_attempts") or 0) - 1)
                    new_backoff = compute_backoff_seconds("ip", new_count)
                    new_lock = now + timedelta(seconds=new_backoff) if new_backoff > 0 else None
                    await cur.execute(
                        """
                        UPDATE dalton_auth_attempts
                        SET failed_attempts = %s,
                            locked_until = %s,
                            updated_at = %s
                        WHERE dimension = 'ip' AND key_hash = %s
                        """,
                        (new_count, new_lock, now, ip_key_hash),
                    )
    except Exception as exc:
        logger.warning(
            "Failed to release IP auth attempt after committed auth success (%s); "
            "preserving committed result.",
            type(exc).__name__,
        )


async def verify_local_session_active(
    *,
    user_id: str,
    session_id: str,
    now: datetime | None = None,
) -> bool:
    """Verify in PostgreSQL that ``session_id`` belongs to ``user_id``, is active, and matches password version.

    Raises:
        HTTPException(401): if session does not exist, was revoked, expired, or predates
            the user's current ``password_changed_at``.
        HTTPException(503): if the database cannot be queried (fail closed).
    """
    uid = str(user_id or "").strip()
    sid = str(session_id or "").strip()
    if not uid or not sid:
        raise HTTPException(status_code=401, detail="Invalid session claims")

    try:
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT s.session_id, s.user_id, s.revoked_at, s.expires_at,
                           s.password_changed_at,
                           c.password_changed_at AS current_password_changed_at,
                           clock_timestamp() AS db_now
                    FROM dalton_local_sessions s
                    JOIN dalton_local_credentials c ON c.user_id = s.user_id
                    WHERE s.session_id = %s AND s.user_id = %s
                    """,
                    (sid, uid),
                )
                row = await cur.fetchone()
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "Failed to verify local session active state (fail-closed 503): %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Authentication service temporarily unavailable",
        ) from exc

    if row is None:
        raise HTTPException(status_code=401, detail="Session not found")
    ts = _as_utc(now) if now is not None else (_as_utc(row.get("db_now")) or _utcnow())
    if row.get("revoked_at") is not None:
        raise HTTPException(status_code=401, detail="Session has been revoked")
    expires_at = _as_utc(row.get("expires_at"))
    if expires_at is None or expires_at <= ts:
        raise HTTPException(status_code=401, detail="Session has expired")

    sess_pw_ts = _as_utc(row.get("password_changed_at"))
    curr_pw_ts = _as_utc(row.get("current_password_changed_at"))
    if sess_pw_ts is not None and curr_pw_ts is not None and sess_pw_ts < curr_pw_ts:
        raise HTTPException(status_code=401, detail="Session invalidated by password change")

    return True


async def create_local_session(
    *,
    user_id: str,
    password_changed_at: datetime | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    conn: Any = None,
    now: datetime | None = None,
) -> tuple[str, str, str, int]:
    """Create a ``dalton_local_sessions`` row and mint an access token.

    Returns ``(session_id, access_token, raw_refresh_token, expires_in)``.
    """
    ttl_seconds = get_refresh_session_ttl_seconds()
    raw_refresh = generate_refresh_token()
    refresh_hash = hash_refresh_token(raw_refresh)

    async def _insert(db_conn: Any) -> str:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            # The session's clock is the database's, as refresh and logout read it
            # (``_read_post_lock_clock``); an app-server clock here would let a
            # skewed host shift every expiry those paths later compare against.
            ts = await _read_post_lock_clock(cur, override_now=now)
            pw_ts = _as_utc(password_changed_at) or ts
            expires_at = ts + timedelta(seconds=ttl_seconds)
            await cur.execute(
                """
                INSERT INTO dalton_local_sessions (
                    user_id, refresh_token_hash, password_changed_at,
                    created_at, last_used_at, expires_at, client_ip, user_agent
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING session_id
                """,
                (
                    user_id,
                    refresh_hash,
                    pw_ts,
                    ts,
                    ts,
                    expires_at,
                    client_ip,
                    user_agent,
                ),
            )
            row = await cur.fetchone()
            return str(row["session_id"])

    if conn is not None:
        session_id = await _insert(conn)
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.transaction():
                session_id = await _insert(db_conn)

    access_token, expires_in = issue_local_access_token(
        user_id=user_id, session_id=session_id
    )
    return session_id, access_token, raw_refresh, expires_in


async def authenticate_local_login(
    *,
    identifier: str,
    password: str,
    client_ip: str = "unknown",
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LocalLoginResult:
    """Authenticate email or username + password with atomic pre-scrypt rate limiting and account locking."""
    ts = now or _utcnow()
    clean_ident = str(identifier or "").strip().lower()
    if not clean_ident or not password:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if clean_ident == COMPANY_SYSTEM_EMAIL:
        raise HTTPException(
            status_code=401,
            detail="Company system identity cannot authenticate externally",
        )

    # 1. Identify account first so email and username share one canonical uid:<user_id> rate-limit bucket.
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT user_id, email, username, password_hash, password_changed_at,
                       failed_attempts, locked_until
                FROM dalton_local_credentials
                WHERE email = %s OR username = %s
                LIMIT 1
                """,
                (clean_ident, clean_ident),
            )
            cred = await cur.fetchone()

    matched_user_id: str | None = None
    if cred is not None:
        matched_user_id = str(cred["user_id"]).strip()
        reject_company_system_auth(matched_user_id)

    ip_hash = hash_attempt_key(client_ip)
    acct_hash = account_attempt_key_hash(user_id=matched_user_id, identifier=clean_ident)

    # 2. Reserve a scrypt executor slot FIRST (if full -> 429 immediately with zero rate-limit
    #    writes), then atomically register the attempt and verify the password under that slot.
    with acquire_scrypt_slot():
        await reserve_auth_attempt_or_429(
            account_key_hash=acct_hash,
            ip_key_hash=ip_hash,
            matched_user_id=matched_user_id,
            now=ts,
        )

        if cred is None:
            await verify_password_async(password, DUMMY_PASSWORD_HASH, slot_held=True)
            raise HTTPException(status_code=401, detail="Invalid email/username or password")

        # 3. Verify password off the event loop using the already-reserved executor slot.
        initial_hash = str(cred["password_hash"])
        initial_pw_changed_at = _as_utc(cred.get("password_changed_at"))
        password_ok = await verify_password_async(password, initial_hash, slot_held=True)
    if not password_ok:
        raise HTTPException(status_code=401, detail="Invalid email/username or password")

    # 4. Acquire per-account advisory lock and re-verify password_hash / password_changed_at
    #    before creating the session (prevents racing with concurrent change-password / CLI reset).
    user_id = matched_user_id or ""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (account_advisory_lock_key(user_id),),
                )
                await cur.execute(
                    """
                    SELECT password_hash, password_changed_at
                    FROM dalton_local_credentials
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                )
                locked_cred = await cur.fetchone()
                if locked_cred is None:
                    raise HTTPException(
                        status_code=401, detail="Invalid email/username or password"
                    )
                locked_pw_changed_at = _as_utc(locked_cred.get("password_changed_at"))
                if (
                    str(locked_cred["password_hash"]) != initial_hash
                    or locked_pw_changed_at != initial_pw_changed_at
                ):
                    raise HTTPException(
                        status_code=401,
                        detail="Credentials changed during login; please sign in again",
                    )

                await cur.execute(
                    """
                    UPDATE dalton_local_credentials
                    SET failed_attempts = 0, locked_until = NULL, updated_at = %s
                    WHERE user_id = %s
                    """,
                    (ts, user_id),
                )
                await cur.execute(
                    """
                    DELETE FROM dalton_auth_attempts
                    WHERE dimension = 'account' AND key_hash = %s
                    """,
                    (acct_hash,),
                )
                await cur.execute(
                    "UPDATE users SET last_login_at = %s, updated_at = %s WHERE user_id = %s",
                    (ts, ts, user_id),
                )

            session_id, access_token, raw_refresh, expires_in = (
                await create_local_session(
                    user_id=user_id,
                    password_changed_at=locked_pw_changed_at or ts,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    conn=conn,
                    # Only an explicit override reaches the session; otherwise it reads
                    # the database clock, as refresh and logout do.
                    now=now,
                )
            )
            tier = await get_user_role(user_id, conn=conn) or "external"

    await _release_ip_attempt_on_success(ip_key_hash=ip_hash, now=ts)

    return LocalLoginResult(
        user_id=user_id,
        email=str(cred["email"]),
        username=cred.get("username"),
        tier=tier,
        session_id=session_id,
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=raw_refresh,
    )


async def refresh_local_session(
    *,
    raw_refresh_token: str,
    client_ip: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> LocalRefreshResult:
    """Rotate refresh token IN PLACE keeping ``session_id`` stable, under per-account advisory lock.

    Replay of a rotated token triggers account-wide revocation AT MOST ONCE and ONLY while
    the rotated token is still within its original ``expires_at`` window (bounded replay window).
    """
    token = str(raw_refresh_token or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing refresh token")

    ts = now or _utcnow()
    token_hash = hash_refresh_token(token)

    # First locate which user owns either the active session or the rotated token history
    # so we can acquire the unified per-account advisory lock before mutating.
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT user_id FROM dalton_local_sessions WHERE refresh_token_hash = %s",
                (token_hash,),
            )
            owner_row = await cur.fetchone()
            if owner_row is None:
                await cur.execute(
                    "SELECT user_id FROM dalton_local_refresh_tokens WHERE token_hash = %s",
                    (token_hash,),
                )
                owner_row = await cur.fetchone()

    if owner_row is None or not owner_row.get("user_id"):
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user_id = str(owner_row["user_id"]).strip()
    reject_company_system_auth(user_id)

    reuse_revoked_now = False
    concurrent_refresh = False
    expired = False
    revoked_reason: str | None = None
    grace_seconds = get_refresh_concurrent_grace_seconds()

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (account_advisory_lock_key(user_id),),
                )
                ts = await _read_post_lock_clock(cur, override_now=now)
                await cur.execute(
                    """
                    SELECT s.session_id, s.user_id, s.expires_at, s.revoked_at,
                           s.revoked_reason, s.password_changed_at,
                           c.password_changed_at AS current_password_changed_at
                    FROM dalton_local_sessions s
                    JOIN dalton_local_credentials c ON c.user_id = s.user_id
                    WHERE s.refresh_token_hash = %s
                    FOR UPDATE OF s
                    """,
                    (token_hash,),
                )
                row = await cur.fetchone()

                if row is not None:
                    session_id = str(row["session_id"])
                    sess_pw_ts = _as_utc(row.get("password_changed_at"))
                    curr_pw_ts = _as_utc(row.get("current_password_changed_at"))
                    if row.get("revoked_at") is not None:
                        revoked_reason = str(row.get("revoked_reason") or "revoked")
                    elif (
                        sess_pw_ts is not None
                        and curr_pw_ts is not None
                        and sess_pw_ts < curr_pw_ts
                    ):
                        revoked_reason = "password_changed"
                        await cur.execute(
                            """
                            UPDATE dalton_local_sessions
                            SET revoked_at = %s, revoked_reason = 'password_changed'
                            WHERE session_id = %s AND revoked_at IS NULL
                            """,
                            (ts, session_id),
                        )
                    else:
                        old_expires_at = _as_utc(row.get("expires_at"))
                        if old_expires_at is None or old_expires_at <= ts:
                            expired = True
                            await cur.execute(
                                """
                                UPDATE dalton_local_sessions
                                SET revoked_at = %s, revoked_reason = 'expired'
                                WHERE session_id = %s
                                """,
                                (ts, session_id),
                            )
                        else:
                            new_raw_refresh = generate_refresh_token()
                            new_refresh_hash = hash_refresh_token(new_raw_refresh)
                            new_expires_at = ts + timedelta(
                                seconds=get_refresh_session_ttl_seconds()
                            )
                            # Archive old refresh hash with its original expires_at for bounded replay detection.
                            await cur.execute(
                                """
                                INSERT INTO dalton_local_refresh_tokens (
                                    token_hash, session_id, user_id, rotated_at, expires_at, reuse_detected_at
                                )
                                VALUES (%s, %s, %s, %s, %s, NULL)
                                ON CONFLICT (token_hash) DO NOTHING
                                """,
                                (token_hash, session_id, user_id, ts, old_expires_at),
                            )
                            # Rotate IN PLACE so session_id (sid) stays stable.
                            await cur.execute(
                                """
                                UPDATE dalton_local_sessions
                                SET refresh_token_hash = %s,
                                    last_used_at = %s,
                                    expires_at = %s,
                                    client_ip = COALESCE(%s, client_ip),
                                    user_agent = COALESCE(%s, user_agent)
                                WHERE session_id = %s
                                """,
                                (
                                    new_refresh_hash,
                                    ts,
                                    new_expires_at,
                                    client_ip,
                                    user_agent,
                                    session_id,
                                ),
                            )
                            access_token, expires_in = issue_local_access_token(
                                user_id=user_id, session_id=session_id
                            )
                            return LocalRefreshResult(
                                user_id=user_id,
                                session_id=session_id,
                                access_token=access_token,
                                expires_in=expires_in,
                                refresh_token=new_raw_refresh,
                            )
                else:
                    # Check rotated-out refresh token history under account lock using post-lock clock_timestamp().
                    await cur.execute(
                        """
                        SELECT h.token_hash, h.session_id, h.user_id, h.rotated_at,
                               h.expires_at, h.reuse_detected_at,
                               s.revoked_at AS session_revoked_at,
                               s.expires_at AS session_expires_at,
                               s.password_changed_at AS session_password_changed_at,
                               c.password_changed_at AS current_password_changed_at
                        FROM dalton_local_refresh_tokens h
                        JOIN dalton_local_sessions s ON s.session_id = h.session_id
                        JOIN dalton_local_credentials c ON c.user_id = h.user_id
                        WHERE h.token_hash = %s
                        FOR UPDATE OF h, s
                        """,
                        (token_hash,),
                    )
                    hist = await cur.fetchone()
                    if hist is not None:
                        hist_reuse_at = hist.get("reuse_detected_at")
                        hist_expires_at = _as_utc(hist.get("expires_at"))
                        rotated_at = _as_utc(hist.get("rotated_at"))
                        sess_expires_at = _as_utc(hist.get("session_expires_at"))
                        sess_pw_ts = _as_utc(hist.get("session_password_changed_at"))
                        curr_pw_ts = _as_utc(hist.get("current_password_changed_at"))
                        session_active = (
                            hist.get("session_revoked_at") is None
                            and sess_expires_at is not None
                            and sess_expires_at > ts
                            and not (
                                sess_pw_ts is not None
                                and curr_pw_ts is not None
                                and sess_pw_ts < curr_pw_ts
                            )
                        )
                        # Every row in dalton_local_refresh_tokens was valid when rotated
                        # (rotated_at < original expires_at). Within the short concurrent
                        # grace window after rotation, while the renewed session is still
                        # active, always return retryable 409 (preserving the winning tab's
                        # new cookie) even if the old token's original expires_at just elapsed.
                        within_grace = (
                            grace_seconds > 0
                            and rotated_at is not None
                            and (ts - rotated_at).total_seconds() <= grace_seconds
                        )
                        if hist_reuse_at is None and session_active and within_grace:
                            # Concurrent refresh (e.g. two tabs racing): signal retryable 409
                            # without revoking any session or stamping reuse_detected_at.
                            concurrent_refresh = True
                        elif (
                            hist_reuse_at is None
                            and hist_expires_at is not None
                            and hist_expires_at > ts
                        ):
                            await cur.execute(
                                """
                                UPDATE dalton_local_refresh_tokens
                                SET reuse_detected_at = %s
                                WHERE token_hash = %s AND reuse_detected_at IS NULL
                                RETURNING session_id, user_id
                                """,
                                (ts, token_hash),
                            )
                            reused = await cur.fetchone()
                            if reused is not None:
                                reuse_revoked_now = True
                                await cur.execute(
                                    """
                                    UPDATE dalton_local_sessions
                                    SET revoked_at = %s, revoked_reason = 'reuse_detected'
                                    WHERE user_id = %s AND revoked_at IS NULL
                                    """,
                                    (ts, user_id),
                                )
                            else:
                                revoked_reason = "rotated_or_expired"
                        else:
                            revoked_reason = "rotated_or_expired"
                    else:
                        revoked_reason = "rotated_or_expired"

    if concurrent_refresh:
        raise HTTPException(
            status_code=409,
            detail="Concurrent refresh in progress; please retry",
            headers={"Retry-After": "1"},
        )
    if reuse_revoked_now:
        raise HTTPException(
            status_code=401,
            detail="Refresh token reuse detected; all sessions have been revoked",
        )
    if expired:
        raise HTTPException(status_code=401, detail="Refresh session has expired")
    raise HTTPException(
        status_code=401,
        detail=f"Refresh session is no longer active ({revoked_reason or 'revoked'})",
    )


LogoutRevocationOutcome = Literal["revoked", "expired_noop", "not_found", "mismatch"]


async def revoke_session_for_logout(
    raw_refresh_token: str | None,
    *,
    expected_user_id: str | None = None,
    expected_session_id: str | None = None,
    reason: str = "logout",
    now: datetime | None = None,
) -> LogoutRevocationOutcome:
    """Revoke the session identified by ``raw_refresh_token`` using its stable ``session_id`` (``sid``).

    - If ``expected_user_id`` (or ``expected_session_id``) is supplied and does NOT match the
      cookie session's owner (or ``session_id``), returns ``"mismatch"`` WITHOUT revoking any
      session so a stale tab from account A cannot revoke account B's active session.
    - Current refresh token in ``dalton_local_sessions``: revokes by ``session_id`` under the
      per-account advisory lock (``"revoked"`` or ``"not_found"`` if already revoked).
    - Rotated historical token in ``dalton_local_refresh_tokens``: ONLY revokes if the historical
      token is still within its original ``expires_at`` window (checked against post-lock
      ``clock_timestamp()``). If the historical token has already expired, leaves the renewed
      session untouched and returns ``"expired_noop"`` so logout completes cleanly without
      revoking any active session.
    """
    token = str(raw_refresh_token or "").strip()
    if not token:
        return "not_found"
    exp_uid = str(expected_user_id or "").strip() or None
    exp_sid = str(expected_session_id or "").strip() or None
    token_hash = hash_refresh_token(token)
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                from_history = False
                await cur.execute(
                    "SELECT session_id, user_id FROM dalton_local_sessions WHERE refresh_token_hash = %s",
                    (token_hash,),
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        "SELECT session_id, user_id, expires_at FROM dalton_local_refresh_tokens WHERE token_hash = %s",
                        (token_hash,),
                    )
                    row = await cur.fetchone()
                    from_history = row is not None
                if row is None or not row.get("session_id") or not row.get("user_id"):
                    return "not_found"

                session_id = str(row["session_id"])
                user_id = str(row["user_id"]).strip()
                if (exp_uid is not None and user_id != exp_uid) or (
                    exp_sid is not None and session_id != exp_sid
                ):
                    return "mismatch"
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (account_advisory_lock_key(user_id),),
                )
                ts = await _read_post_lock_clock(cur, override_now=now)
                # Re-verify token state AFTER acquiring the account lock in case a concurrent
                # refresh rotated it into dalton_local_refresh_tokens while we waited for the lock.
                if not from_history:
                    await cur.execute(
                        "SELECT session_id FROM dalton_local_sessions WHERE refresh_token_hash = %s",
                        (token_hash,),
                    )
                    if await cur.fetchone() is None:
                        await cur.execute(
                            "SELECT session_id, user_id, expires_at FROM dalton_local_refresh_tokens WHERE token_hash = %s",
                            (token_hash,),
                        )
                        hist_row = await cur.fetchone()
                        if hist_row is None:
                            return "not_found"
                        row = hist_row
                        from_history = True
                        session_id = str(row["session_id"])
                        user_id = str(row["user_id"]).strip()
                        if (exp_uid is not None and user_id != exp_uid) or (
                            exp_sid is not None and session_id != exp_sid
                        ):
                            return "mismatch"
                if from_history:
                    hist_expires_at = _as_utc(row.get("expires_at"))
                    if hist_expires_at is None or hist_expires_at <= ts:
                        return "expired_noop"
                await cur.execute(
                    """
                    UPDATE dalton_local_sessions
                    SET revoked_at = %s, revoked_reason = %s
                    WHERE session_id = %s AND user_id = %s AND revoked_at IS NULL
                    RETURNING session_id
                    """,
                    (ts, reason, session_id, user_id),
                )
                updated = await cur.fetchone()
                return "revoked" if updated is not None else "not_found"


async def revoke_session_by_refresh_token(
    raw_refresh_token: str | None,
    *,
    reason: str = "logout",
    now: datetime | None = None,
) -> bool:
    """Return True iff ``raw_refresh_token`` revoked an active session."""
    outcome = await revoke_session_for_logout(
        raw_refresh_token, reason=reason, now=now
    )
    return outcome == "revoked"


async def revoke_all_user_sessions(
    user_id: str,
    *,
    reason: str = "admin_revoked",
    conn: Any = None,
    now: datetime | None = None,
) -> int:
    """Revoke every active local session belonging to ``user_id`` under the per-account advisory lock."""
    uid = str(user_id or "").strip()
    if not uid:
        return 0
    ts = now or _utcnow()

    async def _run(db_conn: Any) -> int:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (account_advisory_lock_key(uid),),
            )
            await cur.execute(
                """
                UPDATE dalton_local_sessions
                SET revoked_at = %s, revoked_reason = %s
                WHERE user_id = %s AND revoked_at IS NULL
                RETURNING session_id
                """,
                (ts, reason, uid),
            )
            rows = await cur.fetchall()
            return len(rows)

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn:
        async with db_conn.transaction():
            return await _run(db_conn)


async def change_local_password(
    *,
    user_id: str,
    current_password: str,
    new_password: str,
    client_ip: str = "unknown",
    now: datetime | None = None,
) -> int:
    """Rate-limited password change serialized with login/refresh under the per-account advisory lock."""
    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="Authentication required")
    reject_company_system_auth(uid)

    try:
        validate_password_policy(new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if current_password == new_password:
        raise HTTPException(
            status_code=400, detail="New password must differ from current password"
        )

    ts = now or _utcnow()
    acct_hash = account_attempt_key_hash(user_id=uid)
    ip_hash = hash_attempt_key(client_ip) if client_ip and client_ip != "unknown" else None

    # Reserve a scrypt executor slot FIRST (if full -> 429 immediately with zero rate-limit
    # writes), then rate-limit current_password guessing and run scrypt under that slot.
    with acquire_scrypt_slot():
        await reserve_auth_attempt_or_429(
            account_key_hash=acct_hash,
            ip_key_hash=ip_hash,
            matched_user_id=uid,
            now=ts,
        )

        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT user_id, password_hash, password_changed_at
                    FROM dalton_local_credentials
                    WHERE user_id = %s
                    """,
                    (uid,),
                )
                cred = await cur.fetchone()
        if cred is None:
            raise HTTPException(status_code=404, detail="Local account credentials not found")

        initial_hash = str(cred["password_hash"])
        if not await verify_password_async(current_password, initial_hash, slot_held=True):
            raise HTTPException(status_code=401, detail="Current password is incorrect")

        new_hash = await hash_password_async(new_password, slot_held=True)

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (account_advisory_lock_key(uid),),
                )
                await cur.execute(
                    """
                    SELECT password_hash, password_changed_at
                    FROM dalton_local_credentials
                    WHERE user_id = %s
                    FOR UPDATE
                    """,
                    (uid,),
                )
                locked = await cur.fetchone()
                if locked is None:
                    raise HTTPException(
                        status_code=404, detail="Local account credentials not found"
                    )
                if str(locked["password_hash"]) != initial_hash:
                    raise HTTPException(
                        status_code=409,
                        detail="Password was changed concurrently; please retry",
                    )

                await cur.execute(
                    """
                    UPDATE dalton_local_credentials
                    SET password_hash = %s,
                        password_changed_at = %s,
                        failed_attempts = 0,
                        locked_until = NULL,
                        updated_at = %s
                    WHERE user_id = %s
                    """,
                    (new_hash, ts, ts, uid),
                )
                await cur.execute(
                    """
                    DELETE FROM dalton_auth_attempts
                    WHERE dimension = 'account' AND key_hash = %s
                    """,
                    (acct_hash,),
                )

            revoked = await revoke_all_user_sessions(
                uid, reason="password_changed", conn=conn, now=ts
            )

    if ip_hash is not None:
        await _release_ip_attempt_on_success(ip_key_hash=ip_hash, now=ts)
    return revoked
