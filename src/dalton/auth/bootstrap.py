"""First-administrator bootstrap with permanent lock, startup-seeded 24h TTL, and 5-attempt lockout.

Design invariants (Standalone Core §2.4 #1):
- Acquires a PostgreSQL transaction-level advisory lock (``pg_advisory_xact_lock``)
  so concurrent ``POST /api/v1/dalton/bootstrap/admin`` requests serialize strictly
  and at most one can ever succeed.
- Once ``dalton_bootstrap_state`` contains its singleton row (``singleton_id = 1``),
  bootstrap is permanently closed (HTTP 409) regardless of subsequent user/role edits.
- Bootstrap token TTL is 24 hours measured from the database clock ``NOW()`` when
  the token was written into ``dalton_bootstrap_tokens`` at server startup
  (``run_auth_db_startup_hook`` -> ``seed_bootstrap_token_from_env_if_needed``) or
  when reissued via ``python -m src.dalton.auth.cli reissue-bootstrap-token``.
  Restarts never overwrite an existing token row, preserving expiry and lockout state.
- Expiry is evaluated against PostgreSQL's database clock AFTER acquiring the
  advisory lock (never against a pre-lock application timestamp).
- After 5 failed attempts, bootstrap locks until an operator runs
  ``reissue-bootstrap-token`` on the server.
- Token verification uses constant-time ``hmac.compare_digest`` on SHA-256 digests.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.auth.passwords import hash_password_async, validate_password_policy
from src.dalton.auth.sessions import create_local_session
from src.dalton.company_identity import COMPANY_SYSTEM_EMAIL
from src.dalton.profile import DaltonStartupConfigError
from src.dalton.settings import (
    get_dalton_auth_mode,
    is_dalton_local_auth_enabled,
    validate_dalton_auth_config,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

BOOTSTRAP_TOKEN_TTL_HOURS = 24
BOOTSTRAP_MAX_FAILED_ATTEMPTS = 5
BOOTSTRAP_ADVISORY_LOCK_KEY: int = int.from_bytes(
    hashlib.sha256(b"dalton:bootstrap:admin:v1").digest()[:8],
    byteorder="big",
    signed=True,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USERNAME_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
_DEFAULT_OTHER_PREFERENCE = {"auto_approve_handoffs": True}


@dataclass(frozen=True)
class BootstrapAdminResult:
    """Result returned by :func:`bootstrap_first_admin`."""

    user_id: str
    email: str
    username: str | None
    name: str
    tier: str
    session_id: str
    access_token: str
    expires_in: int
    refresh_token: str


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def get_env_bootstrap_setup_token() -> str:
    """Return ``BOOTSTRAP_SETUP_TOKEN`` from environment (stripped, empty if unset)."""
    return os.getenv("BOOTSTRAP_SETUP_TOKEN", "").strip()


async def get_bootstrap_status(*, conn: Any = None) -> dict[str, bool]:
    """Return ``{"needs_bootstrap": bool}`` without exposing any sensitive state."""
    sql = "SELECT 1 AS done FROM dalton_bootstrap_state WHERE singleton_id = 1"
    if conn is not None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
    else:
        async with get_db_connection() as db_conn:
            async with db_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql)
                row = await cur.fetchone()
    return {"needs_bootstrap": row is None}


async def seed_bootstrap_token_from_env_if_needed() -> bool:
    """Seed ``dalton_bootstrap_tokens`` from ``BOOTSTRAP_SETUP_TOKEN`` at startup using DB ``NOW()``.

    Idempotent: never overwrites an existing token row (preserves 24h expiry and
    failure lockout across restarts) and does nothing once ``dalton_bootstrap_state``
    is populated. Returns True if a new row was inserted.
    """
    env_token = get_env_bootstrap_setup_token()
    if not env_token:
        return False

    token_hash = _sha256_hex(env_token)

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (BOOTSTRAP_ADVISORY_LOCK_KEY,),
                )
                await cur.execute(
                    "SELECT 1 FROM dalton_bootstrap_state WHERE singleton_id = 1"
                )
                if await cur.fetchone() is not None:
                    return False
                await cur.execute(
                    """
                    INSERT INTO dalton_bootstrap_tokens (
                        singleton_id, token_sha256, source, issued_at,
                        expires_at, failed_attempts, locked_at, updated_at
                    )
                    VALUES (
                        1, %s, 'env', clock_timestamp(),
                        clock_timestamp() + (%s * INTERVAL '1 hour'), 0, NULL, clock_timestamp()
                    )
                    ON CONFLICT (singleton_id) DO NOTHING
                    RETURNING singleton_id
                    """,
                    (token_hash, BOOTSTRAP_TOKEN_TTL_HOURS),
                )
                return (await cur.fetchone()) is not None


async def run_auth_db_startup_hook(
    host_mode: str,
    target_logger: logging.Logger | None = None,
) -> bool:
    """Single post-DB-open startup hook called from ``src/server/app/setup.py``.

    Also runs main object-storage startup checks:
    - Standalone Core S3-only provider validation (``validate_core_storage_provider``)
    - Warning-only main-bucket reference advisory check when main storage is disabled
      (never blocks startup; strict verification runs manually via
      ``python -m src.dalton.storage_refs check --strict`` before shutting down the main bucket).

    1. Validates main-storage provider constraints (Core profile) and logs a warning-only
       advisory if main storage is disabled while main-bucket references exist.
    2. Validates ``DALTON_AUTH_MODE`` across ALL ``HOST_MODE`` values (including rejecting
       ``HOST_MODE=platform`` + ``DALTON_AUTH_MODE=local`` and missing local transcription secret).
    3. When ``DALTON_AUTH_MODE == "local"``, seeds ``dalton_bootstrap_tokens`` from
       ``BOOTSTRAP_SETUP_TOKEN`` at server startup so the 24h TTL begins on boot.
       Any DB failure during startup seeding fails server startup loudly instead of
       allowing a later request to lazily restart the 24h window.
    4. Returns True iff ``local-dev-user`` should be auto-provisioned (False in ``local`` mode
       or non-``oss`` ``HOST_MODE``).
    """
    log = target_logger or logger
    from src.dalton.storage_config import validate_core_storage_provider
    from src.dalton.storage_refs import warn_main_bucket_references_on_startup

    validate_core_storage_provider()
    validate_dalton_auth_config()
    try:
        await warn_main_bucket_references_on_startup(target_logger=log)
    except Exception as exc:
        log.warning(
            "Main-storage startup advisory check raised %s; continuing startup.",
            type(exc).__name__,
        )

    if is_dalton_local_auth_enabled():
        try:
            seeded = await seed_bootstrap_token_from_env_if_needed()
            if seeded:
                log.info(
                    "DALTON_AUTH_MODE=local: seeded BOOTSTRAP_SETUP_TOKEN into "
                    "dalton_bootstrap_tokens (24h TTL started at server boot)."
                )
        except Exception as exc:
            err_type = type(exc).__name__
            log.error(
                "DALTON_AUTH_MODE=local: failed to seed bootstrap token at startup (%s).",
                err_type,
            )
            raise DaltonStartupConfigError(
                "DALTON_AUTH_MODE=local startup failed: unable to initialize "
                f"bootstrap token ({err_type}). Ensure d047 migrations are applied "
                "and PostgreSQL is reachable."
            ) from None
        return False

    return host_mode == "oss" and get_dalton_auth_mode() != "local"


async def _read_post_lock_clock(cur: Any) -> datetime:
    await cur.execute("SELECT clock_timestamp() AS db_now")
    clock_row = await cur.fetchone()
    return _as_utc(
        clock_row["db_now"] if clock_row and clock_row.get("db_now") else None
    ) or datetime.now(timezone.utc)


async def bootstrap_first_admin(
    *,
    bootstrap_token: str,
    email: str,
    password: str,
    username: str | None = None,
    name: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> BootstrapAdminResult:
    """Create the first administrator atomically under a PostgreSQL transaction advisory lock.

    Verification runs BEFORE ``scrypt`` password hashing so already-bootstrapped, locked,
    missing-token, expired, or wrong-token requests never burn CPU/memory in the scrypt
    pool. All expiry checks use PostgreSQL's ``clock_timestamp()`` read AFTER acquiring
    ``pg_advisory_xact_lock``, and ``POST /bootstrap/admin`` never lazily seeds a fresh
    24h token from ``BOOTSTRAP_SETUP_TOKEN``.
    """
    clean_email = str(email or "").strip().lower()
    if not clean_email or not _EMAIL_RE.match(clean_email):
        raise HTTPException(status_code=400, detail="Valid email address is required")
    if clean_email == COMPANY_SYSTEM_EMAIL:
        raise HTTPException(
            status_code=400,
            detail="Company system email cannot be used for an administrator account",
        )

    clean_username: str | None = None
    if username is not None and str(username).strip():
        clean_username = str(username).strip().lower()
        if not _USERNAME_RE.match(clean_username):
            raise HTTPException(
                status_code=400,
                detail="Username must be 3-32 lowercase alphanumeric, '.', '_', or '-' characters",
            )

    try:
        validate_password_policy(password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    display_name = (
        str(name).strip()
        if isinstance(name, str) and name.strip()
        else (clean_username or clean_email.split("@", 1)[0])
    )
    default_tz = os.getenv("DALTON_TIMEZONE", "UTC").strip() or "UTC"

    candidate_token = str(bootstrap_token or "").strip()
    candidate_hash = _sha256_hex(candidate_token) if candidate_token else ""

    failed_now_locked = False
    invalid_token = False
    verified_stored_hash = ""

    # Phase 1: Under advisory lock, check completion, lockout, token presence, post-lock
    # clock_timestamp() expiry, and constant-time token match BEFORE scheduling scrypt.
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (BOOTSTRAP_ADVISORY_LOCK_KEY,),
                )
                await cur.execute(
                    "SELECT completed_at, completed_by_user_id "
                    "FROM dalton_bootstrap_state WHERE singleton_id = 1"
                )
                if await cur.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Initial admin bootstrap has already been completed",
                    )

                db_now = await _read_post_lock_clock(cur)

                await cur.execute(
                    """
                    SELECT token_sha256, source, issued_at, expires_at,
                           failed_attempts, locked_at
                    FROM dalton_bootstrap_tokens
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                token_row = await cur.fetchone()
                if token_row is None:
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "Bootstrap setup token has not been initialized. Restart "
                            "the server with BOOTSTRAP_SETUP_TOKEN set or run "
                            "`python -m src.dalton.auth.cli reissue-bootstrap-token`."
                        ),
                    )

                failed_attempts = int(token_row.get("failed_attempts") or 0)
                if (
                    token_row.get("locked_at") is not None
                    or failed_attempts >= BOOTSTRAP_MAX_FAILED_ATTEMPTS
                ):
                    raise HTTPException(
                        status_code=423,
                        detail=(
                            "Bootstrap is locked after 5 failed attempts. Run "
                            "`python -m src.dalton.auth.cli reissue-bootstrap-token` "
                            "on the server to issue a new token."
                        ),
                    )

                expires_at_dt = _as_utc(token_row.get("expires_at"))
                if expires_at_dt is None or expires_at_dt <= db_now:
                    raise HTTPException(
                        status_code=410,
                        detail=(
                            "Bootstrap setup token has expired (24h TTL). Run "
                            "`python -m src.dalton.auth.cli reissue-bootstrap-token` "
                            "on the server to issue a new token."
                        ),
                    )

                stored_hash = str(token_row["token_sha256"])
                token_ok = bool(candidate_hash) and hmac.compare_digest(
                    candidate_hash.encode("ascii"),
                    stored_hash.encode("ascii"),
                )
                if not token_ok:
                    new_attempts = failed_attempts + 1
                    locked_at = (
                        db_now if new_attempts >= BOOTSTRAP_MAX_FAILED_ATTEMPTS else None
                    )
                    await cur.execute(
                        """
                        UPDATE dalton_bootstrap_tokens
                        SET failed_attempts = %s,
                            locked_at = COALESCE(locked_at, %s),
                            updated_at = %s
                        WHERE singleton_id = 1
                        """,
                        (new_attempts, locked_at, db_now),
                    )
                    invalid_token = True
                    failed_now_locked = locked_at is not None
                else:
                    verified_stored_hash = stored_hash

    if invalid_token:
        if failed_now_locked:
            raise HTTPException(
                status_code=423,
                detail=(
                    "Invalid bootstrap setup token; bootstrap is now locked after 5 "
                    "failed attempts. Run `python -m src.dalton.auth.cli "
                    "reissue-bootstrap-token` on the server."
                ),
            )
        raise HTTPException(status_code=401, detail="Invalid bootstrap setup token")

    # Phase 2: All bootstrap gates passed — compute scrypt hash off the event loop
    # without holding the DB advisory lock.
    password_hash = await hash_password_async(password)

    # Phase 3: Re-acquire advisory lock, re-check state & post-lock clock_timestamp(),
    # and atomically create the initial admin + permanently close bootstrap.
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (BOOTSTRAP_ADVISORY_LOCK_KEY,),
                )
                await cur.execute(
                    "SELECT completed_at, completed_by_user_id "
                    "FROM dalton_bootstrap_state WHERE singleton_id = 1"
                )
                if await cur.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Initial admin bootstrap has already been completed",
                    )

                db_now = await _read_post_lock_clock(cur)

                await cur.execute(
                    """
                    SELECT token_sha256, source, issued_at, expires_at,
                           failed_attempts, locked_at
                    FROM dalton_bootstrap_tokens
                    WHERE singleton_id = 1
                    FOR UPDATE
                    """
                )
                token_row = await cur.fetchone()
                if token_row is None:
                    raise HTTPException(
                        status_code=503,
                        detail="Bootstrap setup token is no longer present",
                    )
                if (
                    token_row.get("locked_at") is not None
                    or int(token_row.get("failed_attempts") or 0)
                    >= BOOTSTRAP_MAX_FAILED_ATTEMPTS
                ):
                    raise HTTPException(
                        status_code=423,
                        detail=(
                            "Bootstrap is locked after 5 failed attempts. Run "
                            "`python -m src.dalton.auth.cli reissue-bootstrap-token` "
                            "on the server to issue a new token."
                        ),
                    )
                expires_at_dt = _as_utc(token_row.get("expires_at"))
                if expires_at_dt is None or expires_at_dt <= db_now:
                    raise HTTPException(
                        status_code=410,
                        detail=(
                            "Bootstrap setup token has expired (24h TTL). Run "
                            "`python -m src.dalton.auth.cli reissue-bootstrap-token` "
                            "on the server to issue a new token."
                        ),
                    )
                current_stored_hash = str(token_row["token_sha256"])
                if current_stored_hash != verified_stored_hash and not hmac.compare_digest(
                    candidate_hash.encode("ascii"),
                    current_stored_hash.encode("ascii"),
                ):
                    raise HTTPException(
                        status_code=401,
                        detail="Bootstrap setup token was rotated concurrently",
                    )

                user_id = str(uuid4())

                # 1. users
                await cur.execute(
                    """
                    INSERT INTO users (
                        user_id, email, name, auth_provider, timezone,
                        onboarding_completed, created_at, updated_at, last_login_at
                    )
                    VALUES (%s, %s, %s, 'local', %s, FALSE, %s, %s, %s)
                    """,
                    (user_id, clean_email, display_name, default_tz, db_now, db_now, db_now),
                )
                # 1b. default user_preferences row
                await cur.execute(
                    """
                    INSERT INTO user_preferences (
                        user_preference_id, user_id, other_preference,
                        created_at, updated_at
                    )
                    VALUES (gen_random_uuid(), %s, %s::jsonb, %s, %s)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    (user_id, Json(_DEFAULT_OTHER_PREFERENCE), db_now, db_now),
                )
                # 2. dalton_local_credentials
                await cur.execute(
                    """
                    INSERT INTO dalton_local_credentials (
                        user_id, email, username, password_hash,
                        password_changed_at, failed_attempts, locked_until,
                        created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, 0, NULL, %s, %s)
                    """,
                    (
                        user_id,
                        clean_email,
                        clean_username,
                        password_hash,
                        db_now,
                        db_now,
                        db_now,
                    ),
                )
                # 3. dalton_user_roles (admin)
                await cur.execute(
                    """
                    INSERT INTO dalton_user_roles (
                        user_id, tier, granted_by, created_at, updated_at
                    )
                    VALUES (%s, 'admin', 'bootstrap', %s, %s)
                    """,
                    (user_id, db_now, db_now),
                )
                # 4. dalton_credential_holder
                await cur.execute(
                    """
                    INSERT INTO dalton_credential_holder (
                        singleton_id, holder_user_id, updated_by, updated_at
                    )
                    VALUES (1, %s, %s, %s)
                    ON CONFLICT (singleton_id) DO UPDATE
                    SET holder_user_id = EXCLUDED.holder_user_id,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (user_id, user_id, db_now),
                )
                # 5. dalton_bootstrap_state (permanent lock)
                await cur.execute(
                    """
                    INSERT INTO dalton_bootstrap_state (
                        singleton_id, completed_at, completed_by_user_id
                    )
                    VALUES (1, %s, %s)
                    """,
                    (db_now, user_id),
                )
                # 6. Clear active bootstrap token permanently
                await cur.execute(
                    "DELETE FROM dalton_bootstrap_tokens WHERE singleton_id = 1"
                )
                # 7. Mint initial session for the new admin
                (
                    session_id,
                    access_token,
                    raw_refresh,
                    expires_in,
                ) = await create_local_session(
                    user_id=user_id,
                    password_changed_at=db_now,
                    client_ip=client_ip,
                    user_agent=user_agent,
                    conn=conn,
                    now=db_now,
                )

    return BootstrapAdminResult(
        user_id=user_id,
        email=clean_email,
        username=clean_username,
        name=display_name,
        tier="admin",
        session_id=session_id,
        access_token=access_token,
        expires_in=expires_in,
        refresh_token=raw_refresh,
    )


async def reissue_bootstrap_token() -> tuple[str, datetime]:
    """Generate and persist a fresh 24h bootstrap token using DB ``clock_timestamp()``, clearing failure count.

    Refuses with :class:`HTTPException` (409) if bootstrap has already completed.
    Returns ``(plaintext_token, expires_at)``.
    """
    raw_token = secrets.token_hex(32)
    token_hash = _sha256_hex(raw_token)

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (BOOTSTRAP_ADVISORY_LOCK_KEY,),
                )
                await cur.execute(
                    "SELECT 1 FROM dalton_bootstrap_state WHERE singleton_id = 1"
                )
                if await cur.fetchone() is not None:
                    raise HTTPException(
                        status_code=409,
                        detail="Cannot reissue bootstrap token: bootstrap is already completed",
                    )
                await cur.execute(
                    """
                    INSERT INTO dalton_bootstrap_tokens (
                        singleton_id, token_sha256, source, issued_at,
                        expires_at, failed_attempts, locked_at, updated_at
                    )
                    VALUES (
                        1, %s, 'cli', clock_timestamp(),
                        clock_timestamp() + (%s * INTERVAL '1 hour'), 0, NULL, clock_timestamp()
                    )
                    ON CONFLICT (singleton_id) DO UPDATE
                    SET token_sha256 = EXCLUDED.token_sha256,
                        source = 'cli',
                        issued_at = EXCLUDED.issued_at,
                        expires_at = EXCLUDED.expires_at,
                        failed_attempts = 0,
                        locked_at = NULL,
                        updated_at = EXCLUDED.updated_at
                    RETURNING expires_at
                    """,
                    (token_hash, BOOTSTRAP_TOKEN_TTL_HOURS),
                )
                row = await cur.fetchone()
                expires_at = _as_utc(row["expires_at"]) if row and row.get("expires_at") else (
                    datetime.now(timezone.utc) + timedelta(hours=BOOTSTRAP_TOKEN_TTL_HOURS)
                )
    return raw_token, expires_at
