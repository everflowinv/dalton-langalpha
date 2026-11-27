"""PostgreSQL persistence for personal external integrations (Google / Feishu calendar & mail).

Token columns (``access_token``, ``refresh_token``, ``code_verifier``,
``encrypted_revoke_token``, ``encrypted_elicitation_state``) are encrypted at rest
using ``pgcrypto`` ``pgp_sym_encrypt`` / ``pgp_sym_decrypt`` keyed by
``src.server.database.encryption.get_encryption_key()``.

Secret fields on returned dataclasses are marked ``repr=False`` so logs, tracebacks,
and ``repr()`` never print plaintext or raw ciphertext tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import logging
import re
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.server.database.encryption import get_encryption_key
from src.server.database.pool import get_db_connection
from src.server.database.sql_fences import advisory_key

logger = logging.getLogger(__name__)

VALID_PROVIDERS: frozenset[str] = frozenset({"google", "feishu"})
VALID_SERVICES: frozenset[str] = frozenset({"calendar", "mail"})

_GOOGLE_PROJECT_PREFIX_RE = re.compile(r"^(\d+)-")


class IntegrationStatus(StrEnum):
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    REFRESH_AMBIGUOUS = "refresh_ambiguous"
    DISCONNECT_PENDING = "disconnect_pending"
    REVOKED_PURGING = "revoked_purging"


class LocalPurgeStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    NOT_REQUIRED = "not_required"


class RemoteRevokeStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED_RETRYABLE = "failed_retryable"
    SUPERSEDED = "superseded"
    NOT_APPLICABLE = "not_applicable"


class TriggerReason(StrEnum):
    USER_DISCONNECT = "user_disconnect"
    ACCOUNT_SWITCH = "account_switch"
    CLIENT_ROTATION = "client_rotation"
    WEBHOOK_REVOKED = "webhook_revoked"
    ERROR_CODE_REVOKED = "error_code_revoked"


class OAuthIntent(StrEnum):
    CONNECT = "connect"
    SWITCH_ACCOUNT = "switch_account"
    REAUTH = "reauth"


# ---------------------------------------------------------------------------
# Advisory lock keys (unidirectional lock ordering: lifecycle -> refresh -> revoke)
# ---------------------------------------------------------------------------


def extract_google_project(oauth_client_id: str) -> str:
    """Extract the numeric Google Cloud project prefix from an OAuth client ID.

    Google Web OAuth client IDs have the form ``<project_number>-<random>.apps.googleusercontent.com``.
    Falls back to the trimmed client ID when no numeric prefix is present.
    """
    raw = (oauth_client_id or "").strip()
    if not raw:
        raise ValueError("oauth_client_id must be non-empty")
    match = _GOOGLE_PROJECT_PREFIX_RE.match(raw)
    return match.group(1) if match else raw


def lifecycle_lock_key(user_id: str, provider: str) -> int:
    """Per-(user, provider) lifecycle advisory lock key (§4.2(3))."""
    return advisory_key("dalton_integration_user_lifecycle", str(user_id), str(provider))


def refresh_lock_key(
    user_id: str,
    provider: str,
    oauth_client_id: str,
    provider_account_id: str,
    service: str | None = None,
) -> int:
    """Credential refresh advisory lock key (§4.2(2)).

    Google shares one refresh token across services under ``(oauth_client_id, sub)``,
    so its refresh lock is account-scoped. Feishu issues independent single-use
    refresh tokens per service, so its refresh lock includes ``service``.
    """
    if provider == "google":
        return advisory_key(
            "dalton_integration_refresh",
            str(user_id),
            "google",
            str(oauth_client_id),
            str(provider_account_id),
        )
    if provider == "feishu":
        if not service:
            raise ValueError("service is required for feishu refresh_lock_key")
        return advisory_key(
            "dalton_integration_refresh",
            str(user_id),
            "feishu",
            str(oauth_client_id),
            str(provider_account_id),
            str(service),
        )
    raise ValueError(f"Unsupported provider for refresh_lock_key: {provider!r}")


def revoke_lock_key(user_id: str, google_project: str, sub: str) -> int:
    """Per-(user, Google project, sub) remote revocation advisory lock key (§4.2(3))."""
    return advisory_key(
        "dalton_integration_revoke",
        str(user_id),
        str(google_project),
        str(sub),
    )


def purge_lock_key(purge_id: str | UUID) -> int:
    """Per-ledger execution advisory lock key (mutual exclusion across workers)."""
    return advisory_key("dalton_integration_purge", str(purge_id))


def touch_user_lock_key(user_id: str) -> int:
    """Per-user transaction advisory lock key serializing Fork touch copy with Phase 1 touch enumeration."""
    return advisory_key("dalton_integration_touch_user", str(user_id))


def purge_thread_lock_key(thread_id: str | UUID) -> int:
    """Per-thread session advisory lock key serializing concurrent ledgers rewriting the same thread."""
    return advisory_key("dalton_integration_purge_thread", str(thread_id))


def health_sweep_lock_key() -> int:
    """Cluster-wide advisory lock key ensuring only one worker runs the 12-hour health patrol (§4.5(3))."""
    return advisory_key("dalton_integration_health_sweep", "global")


# ---------------------------------------------------------------------------
# Safe row dataclasses (token fields excluded from repr)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntegrationRow:
    user_id: str
    provider: str
    service: str
    oauth_client_id: str
    provider_account_id: str
    provider_union_id: str | None
    provider_email: str | None
    granted_scopes: str
    auth_epoch: int
    authorized_at: datetime
    token_generation: int
    status: IntegrationStatus
    refresh_inflight_gen: int | None
    refresh_started_at: datetime | None
    expires_at: datetime | None
    last_refresh_at: datetime | None
    needs_reauth_at: datetime | None
    last_error_code: str | None
    reauth_notified_at: datetime | None
    preferences: dict[str, Any]
    has_access_token: bool
    has_refresh_token: bool
    created_at: datetime
    updated_at: datetime
    access_token: str | None = field(default=None, repr=False, compare=False)
    refresh_token: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PurgeLedgerRow:
    purge_id: str
    user_id: str
    provider: str
    service: str | None
    oauth_client_id: str
    provider_account_id: str
    auth_epoch_upto: int
    trigger_reason: str
    local_purge_status: LocalPurgeStatus
    purged_max_turn_by_thread: dict[str, Any]
    tracker_reconcile_needed: bool
    local_completed_at: datetime | None
    remote_revoke_status: RemoteRevokeStatus
    has_revoke_token: bool
    retry_count: int
    next_retry_at: datetime | None
    last_remote_error: str | None
    remote_completed_at: datetime | None
    disconnected_at: datetime
    completed_at: datetime | None
    revoke_token: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class OAuthStateRow:
    state_hash: str
    user_id: str
    auth_mode: str
    session_id: str | None
    user_email: str | None
    provider: str
    service: str
    oauth_client_id: str
    intent: str
    requested_scopes: str
    redirect_uri: str
    return_to: str | None
    expires_at: datetime
    created_at: datetime
    code_verifier: str | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PendingConfirmationRow:
    confirmation_hash: str
    user_id: str
    provider: str
    service: str
    oauth_client_id: str
    provider_account_id: str
    provider_union_id: str | None
    provider_email: str | None
    granted_scopes: str
    expires_at: datetime | None
    intent: str
    preferences: dict[str, Any]
    confirm_expires_at: datetime
    created_at: datetime
    access_token: str | None = field(default=None, repr=False, compare=False)
    refresh_token: str | None = field(default=None, repr=False, compare=False)


# ---------------------------------------------------------------------------
# Row mappers
# ---------------------------------------------------------------------------


def _row_to_integration(r: dict[str, Any]) -> IntegrationRow:
    return IntegrationRow(
        user_id=str(r["user_id"]),
        provider=str(r["provider"]),
        service=str(r["service"]),
        oauth_client_id=str(r["oauth_client_id"]),
        provider_account_id=str(r["provider_account_id"]),
        provider_union_id=r.get("provider_union_id"),
        provider_email=r.get("provider_email"),
        granted_scopes=str(r["granted_scopes"]),
        auth_epoch=int(r["auth_epoch"]),
        authorized_at=r["authorized_at"],
        token_generation=int(r["token_generation"]),
        status=IntegrationStatus(str(r["status"])),
        refresh_inflight_gen=(
            int(r["refresh_inflight_gen"])
            if r.get("refresh_inflight_gen") is not None
            else None
        ),
        refresh_started_at=r.get("refresh_started_at"),
        expires_at=r.get("expires_at"),
        last_refresh_at=r.get("last_refresh_at"),
        needs_reauth_at=r.get("needs_reauth_at"),
        last_error_code=r.get("last_error_code"),
        reauth_notified_at=r.get("reauth_notified_at"),
        preferences=dict(r.get("preferences") or {}),
        has_access_token=bool(r.get("has_access_token")),
        has_refresh_token=bool(r.get("has_refresh_token")),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
        access_token=r.get("decrypted_access_token"),
        refresh_token=r.get("decrypted_refresh_token"),
    )


def _row_to_purge_ledger(r: dict[str, Any]) -> PurgeLedgerRow:
    return PurgeLedgerRow(
        purge_id=str(r["purge_id"]),
        user_id=str(r["user_id"]),
        provider=str(r["provider"]),
        service=r.get("service"),
        oauth_client_id=str(r["oauth_client_id"]),
        provider_account_id=str(r["provider_account_id"]),
        auth_epoch_upto=int(r["auth_epoch_upto"]),
        trigger_reason=str(r["trigger_reason"]),
        local_purge_status=LocalPurgeStatus(str(r["local_purge_status"])),
        purged_max_turn_by_thread=dict(r.get("purged_max_turn_by_thread") or {}),
        tracker_reconcile_needed=bool(r.get("tracker_reconcile_needed")),
        local_completed_at=r.get("local_completed_at"),
        remote_revoke_status=RemoteRevokeStatus(str(r["remote_revoke_status"])),
        has_revoke_token=bool(r.get("has_revoke_token")),
        retry_count=int(r.get("retry_count") or 0),
        next_retry_at=r.get("next_retry_at"),
        last_remote_error=r.get("last_remote_error"),
        remote_completed_at=r.get("remote_completed_at"),
        disconnected_at=r["disconnected_at"],
        completed_at=r.get("completed_at"),
        revoke_token=r.get("decrypted_revoke_token"),
    )


_INTEGRATION_META_COLS = """
    user_id, provider, service, oauth_client_id, provider_account_id,
    provider_union_id, provider_email, granted_scopes,
    auth_epoch, authorized_at, token_generation, status,
    refresh_inflight_gen, refresh_started_at, expires_at,
    last_refresh_at, needs_reauth_at, last_error_code, reauth_notified_at,
    preferences,
    (access_token IS NOT NULL) AS has_access_token,
    (refresh_token IS NOT NULL) AS has_refresh_token,
    created_at, updated_at
"""

_PURGE_LEDGER_META_COLS = """
    purge_id, user_id, provider, service, oauth_client_id, provider_account_id,
    auth_epoch_upto, trigger_reason, local_purge_status,
    purged_max_turn_by_thread, tracker_reconcile_needed, local_completed_at,
    remote_revoke_status,
    (encrypted_revoke_token IS NOT NULL) AS has_revoke_token,
    retry_count, next_retry_at, last_remote_error, remote_completed_at,
    disconnected_at, completed_at
"""


# ---------------------------------------------------------------------------
# dalton_user_integrations CRUD
# ---------------------------------------------------------------------------


async def upsert_connection(
    *,
    user_id: str,
    provider: str,
    service: str,
    oauth_client_id: str,
    provider_account_id: str,
    granted_scopes: str,
    access_token: str | None,
    refresh_token: str | None,
    provider_union_id: str | None = None,
    provider_email: str | None = None,
    expires_at: datetime | None = None,
    preferences: dict[str, Any] | None = None,
    status: IntegrationStatus | str = IntegrationStatus.CONNECTED,
    bump_auth_epoch: bool = True,
    reset_authorized_at: bool = True,
    reset_token_generation: bool = False,
    conn: Any = None,
) -> IntegrationRow:
    """Insert or update a personal integration row with pgcrypto-encrypted tokens."""
    enc_key = get_encryption_key()
    status_val = str(IntegrationStatus(status))
    auth_epoch_expr = (
        "nextval('dalton_integration_auth_epoch_seq')"
        if bump_auth_epoch
        else "dalton_user_integrations.auth_epoch"
    )
    authorized_at_expr = (
        "NOW()" if reset_authorized_at else "dalton_user_integrations.authorized_at"
    )
    token_gen_expr = (
        "1"
        if reset_token_generation
        else "dalton_user_integrations.token_generation + 1"
    )

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_user_integrations (
                    user_id, provider, service, oauth_client_id,
                    provider_account_id, provider_union_id, provider_email,
                    granted_scopes, access_token, refresh_token,
                    auth_epoch, authorized_at, token_generation, status,
                    refresh_inflight_gen, refresh_started_at, expires_at,
                    needs_reauth_at, last_error_code, preferences,
                    created_at, updated_at
                )
                VALUES (
                    %(user_id)s, %(provider)s, %(service)s, %(oauth_client_id)s,
                    %(provider_account_id)s, %(provider_union_id)s, %(provider_email)s,
                    %(granted_scopes)s,
                    CASE WHEN %(access_token)s::text IS NULL THEN NULL
                         ELSE pgp_sym_encrypt(%(access_token)s, %(enc_key)s) END,
                    CASE WHEN %(refresh_token)s::text IS NULL THEN NULL
                         ELSE pgp_sym_encrypt(%(refresh_token)s, %(enc_key)s) END,
                    nextval('dalton_integration_auth_epoch_seq'),
                    NOW(), 1, %(status)s,
                    NULL, NULL, %(expires_at)s,
                    NULL, NULL, %(preferences)s,
                    NOW(), NOW()
                )
                ON CONFLICT (user_id, provider, service) DO UPDATE SET
                    oauth_client_id = EXCLUDED.oauth_client_id,
                    provider_account_id = EXCLUDED.provider_account_id,
                    provider_union_id = EXCLUDED.provider_union_id,
                    provider_email = EXCLUDED.provider_email,
                    granted_scopes = EXCLUDED.granted_scopes,
                    access_token = EXCLUDED.access_token,
                    refresh_token = COALESCE(
                        EXCLUDED.refresh_token,
                        dalton_user_integrations.refresh_token
                    ),
                    auth_epoch = {auth_epoch_expr},
                    authorized_at = {authorized_at_expr},
                    token_generation = {token_gen_expr},
                    status = EXCLUDED.status,
                    refresh_inflight_gen = NULL,
                    refresh_started_at = NULL,
                    expires_at = EXCLUDED.expires_at,
                    needs_reauth_at = CASE
                        WHEN EXCLUDED.status = 'connected' THEN NULL
                        ELSE COALESCE(dalton_user_integrations.needs_reauth_at, NOW())
                    END,
                    last_error_code = CASE
                        WHEN EXCLUDED.status = 'connected' THEN NULL
                        ELSE dalton_user_integrations.last_error_code
                    END,
                    preferences = COALESCE(EXCLUDED.preferences, dalton_user_integrations.preferences),
                    updated_at = NOW()
                RETURNING {_INTEGRATION_META_COLS}
                """,
                {
                    "user_id": user_id,
                    "provider": provider,
                    "service": service,
                    "oauth_client_id": oauth_client_id,
                    "provider_account_id": provider_account_id,
                    "provider_union_id": provider_union_id,
                    "provider_email": provider_email,
                    "granted_scopes": granted_scopes,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "enc_key": enc_key,
                    "status": status_val,
                    "expires_at": expires_at,
                    "preferences": Jsonb(preferences if preferences is not None else {}),
                },
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_integration(dict(row))


async def get_connection(
    user_id: str,
    provider: str,
    service: str,
    *,
    decrypt_tokens: bool = False,
    for_update: bool = False,
    conn: Any = None,
) -> IntegrationRow | None:
    """Fetch a single integration connection row."""
    lock_clause = " FOR UPDATE" if for_update else ""
    if decrypt_tokens:
        enc_key = get_encryption_key()
        select_cols = (
            f"{_INTEGRATION_META_COLS}, "
            "CASE WHEN access_token IS NULL THEN NULL "
            "     ELSE pgp_sym_decrypt(access_token, %(enc_key)s) END AS decrypted_access_token, "
            "CASE WHEN refresh_token IS NULL THEN NULL "
            "     ELSE pgp_sym_decrypt(refresh_token, %(enc_key)s) END AS decrypted_refresh_token"
        )
        params: dict[str, Any] = {
            "user_id": user_id,
            "provider": provider,
            "service": service,
            "enc_key": enc_key,
        }
    else:
        select_cols = _INTEGRATION_META_COLS
        params = {"user_id": user_id, "provider": provider, "service": service}

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {select_cols}
                FROM dalton_user_integrations
                WHERE user_id = %(user_id)s
                  AND provider = %(provider)s
                  AND service = %(service)s
                {lock_clause}
                """,
                params,
            )
            row = await cur.fetchone()
    return _row_to_integration(dict(row)) if row is not None else None


async def list_user_connections(
    user_id: str,
    *,
    provider: str | None = None,
    conn: Any = None,
) -> list[IntegrationRow]:
    """List a user's integration connections without decrypting tokens."""
    where = "WHERE user_id = %(user_id)s"
    params: dict[str, Any] = {"user_id": user_id}
    if provider is not None:
        where += " AND provider = %(provider)s"
        params["provider"] = provider
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_INTEGRATION_META_COLS}
                FROM dalton_user_integrations
                {where}
                ORDER BY provider ASC, service ASC
                """,
                params,
            )
            rows = await cur.fetchall()
    return [_row_to_integration(dict(r)) for r in rows]


async def get_sibling_connections(
    user_id: str,
    provider: str,
    oauth_client_id: str,
    provider_account_id: str,
    *,
    exclude_service: str | None = None,
    active_only: bool = True,
    conn: Any = None,
) -> list[IntegrationRow]:
    """List sibling service rows sharing the same ``(user_id, provider, oauth_client_id, provider_account_id)``."""
    clauses = [
        "user_id = %(user_id)s",
        "provider = %(provider)s",
        "oauth_client_id = %(oauth_client_id)s",
        "provider_account_id = %(provider_account_id)s",
    ]
    params: dict[str, Any] = {
        "user_id": user_id,
        "provider": provider,
        "oauth_client_id": oauth_client_id,
        "provider_account_id": provider_account_id,
    }
    if exclude_service is not None:
        clauses.append("service <> %(exclude_service)s")
        params["exclude_service"] = exclude_service
    if active_only:
        clauses.append("status NOT IN ('disconnect_pending', 'revoked_purging')")

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_INTEGRATION_META_COLS}
                FROM dalton_user_integrations
                WHERE {' AND '.join(clauses)}
                ORDER BY service ASC
                """,
                params,
            )
            rows = await cur.fetchall()
    return [_row_to_integration(dict(r)) for r in rows]


async def delete_connection_upto_epoch(
    user_id: str,
    provider: str,
    service: str | None,
    auth_epoch_upto: int,
    *,
    conn: Any = None,
) -> int:
    """Delete connection row(s) bounded by ``auth_epoch <= auth_epoch_upto``.

    Used exclusively by active disconnect and revocation so a reconnection
    assigned a larger ``auth_epoch`` during purge is never deleted.
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            if service is not None:
                await cur.execute(
                    """
                    DELETE FROM dalton_user_integrations
                    WHERE user_id = %s
                      AND provider = %s
                      AND service = %s
                      AND auth_epoch <= %s
                    """,
                    (user_id, provider, service, auth_epoch_upto),
                )
            else:
                await cur.execute(
                    """
                    DELETE FROM dalton_user_integrations
                    WHERE user_id = %s
                      AND provider = %s
                      AND auth_epoch <= %s
                    """,
                    (user_id, provider, auth_epoch_upto),
                )
            return int(cur.rowcount or 0)


async def update_connection_status(
    user_id: str,
    provider: str,
    service: str,
    *,
    status: IntegrationStatus | str,
    last_error_code: str | None = None,
    clear_tokens: bool = False,
    expected_auth_epoch: int | None = None,
    conn: Any = None,
) -> bool:
    """Update a connection's status (optionally bounded by ``expected_auth_epoch``)."""
    status_val = str(IntegrationStatus(status))
    token_sets = (
        "access_token = NULL, refresh_token = NULL, "
        if clear_tokens
        else ""
    )
    needs_reauth_expr = (
        "COALESCE(needs_reauth_at, NOW())"
        if status_val == IntegrationStatus.NEEDS_REAUTH
        else "needs_reauth_at"
    )
    epoch_filter = " AND auth_epoch = %(expected_auth_epoch)s" if expected_auth_epoch is not None else ""
    params: dict[str, Any] = {
        "user_id": user_id,
        "provider": provider,
        "service": service,
        "status": status_val,
        "last_error_code": last_error_code,
        "expected_auth_epoch": expected_auth_epoch,
    }
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE dalton_user_integrations
                SET {token_sets}
                    status = %(status)s,
                    refresh_inflight_gen = NULL,
                    refresh_started_at = NULL,
                    needs_reauth_at = {needs_reauth_expr},
                    last_error_code = COALESCE(%(last_error_code)s, last_error_code),
                    updated_at = NOW()
                WHERE user_id = %(user_id)s
                  AND provider = %(provider)s
                  AND service = %(service)s
                  {epoch_filter}
                """,
                params,
            )
            return (cur.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# Thread touch tracking
# ---------------------------------------------------------------------------


async def record_touch(
    *,
    thread_id: str | UUID,
    response_id: str | UUID,
    provider: str,
    service: str,
    auth_epoch: int,
    turn_index: int,
    user_id: str,
    provider_account_id: str,
    conn: Any = None,
) -> bool:
    """Record a personal integration touch and mark the thread private (idempotent).

    Returns ``True`` if this call transitioned
    ``conversation_threads.has_personal_integration_data`` from ``FALSE`` to ``TRUE``
    (the thread's first touch), ``False`` otherwise.
    """
    tid = str(thread_id)
    rid = str(response_id)

    async def _run(db_conn: Any) -> bool:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT has_personal_integration_data
                FROM conversation_threads
                WHERE conversation_thread_id = %s::uuid
                FOR UPDATE
                """,
                (tid,),
            )
            row = await cur.fetchone()
            if row is None:
                raise LookupError(f"conversation_thread {tid} not found")
            was_private = bool(row["has_personal_integration_data"])

            await cur.execute(
                """
                INSERT INTO dalton_thread_integration_touches (
                    thread_id, response_id, provider, service, auth_epoch,
                    turn_index, user_id, provider_account_id, touched_at
                )
                VALUES (
                    %s::uuid, %s::uuid, %s, %s, %s,
                    %s, %s, %s, NOW()
                )
                ON CONFLICT (thread_id, response_id, provider, service, auth_epoch)
                DO NOTHING
                """,
                (
                    tid,
                    rid,
                    provider,
                    service,
                    int(auth_epoch),
                    int(turn_index),
                    user_id,
                    provider_account_id,
                ),
            )
            if not was_private:
                await cur.execute(
                    """
                    UPDATE conversation_threads
                    SET has_personal_integration_data = TRUE,
                        updated_at = NOW()
                    WHERE conversation_thread_id = %s::uuid
                    """,
                    (tid,),
                )
                return True
            return False

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)


# ---------------------------------------------------------------------------
# Purge ledger CRUD
# ---------------------------------------------------------------------------


async def insert_purge_ledger(
    *,
    purge_id: str | UUID | None = None,
    user_id: str,
    provider: str,
    service: str | None,
    oauth_client_id: str,
    provider_account_id: str,
    auth_epoch_upto: int,
    trigger_reason: str,
    local_purge_status: LocalPurgeStatus | str,
    purged_max_turn_by_thread: dict[str, Any],
    remote_revoke_status: RemoteRevokeStatus | str,
    encrypted_revoke_token: bytes | None = None,
    revoke_token_plaintext: str | None = None,
    tracker_reconcile_needed: bool = False,
    conn: Any = None,
) -> PurgeLedgerRow:
    """Insert a purge ledger row (optionally encrypting plaintext revoke token or copying ciphertext)."""
    pid = str(purge_id or uuid4())
    local_val = str(LocalPurgeStatus(local_purge_status))
    remote_val = str(RemoteRevokeStatus(remote_revoke_status))

    token_sql = "%(encrypted_revoke_token)s"
    params: dict[str, Any] = {
        "purge_id": pid,
        "user_id": user_id,
        "provider": provider,
        "service": service,
        "oauth_client_id": oauth_client_id,
        "provider_account_id": provider_account_id,
        "auth_epoch_upto": int(auth_epoch_upto),
        "trigger_reason": trigger_reason,
        "local_purge_status": local_val,
        "purged_max_turn_by_thread": Jsonb(purged_max_turn_by_thread),
        "tracker_reconcile_needed": bool(tracker_reconcile_needed),
        "remote_revoke_status": remote_val,
        "encrypted_revoke_token": encrypted_revoke_token,
    }
    if encrypted_revoke_token is None and revoke_token_plaintext is not None:
        token_sql = "pgp_sym_encrypt(%(revoke_token_plaintext)s, %(enc_key)s)"
        params["revoke_token_plaintext"] = revoke_token_plaintext
        params["enc_key"] = get_encryption_key()

    local_completed_sql = (
        "NOW()" if local_val in (LocalPurgeStatus.COMPLETED, LocalPurgeStatus.NOT_REQUIRED) else "NULL"
    )
    remote_completed_sql = (
        "NOW()"
        if remote_val in (
            RemoteRevokeStatus.SUCCEEDED,
            RemoteRevokeStatus.SUPERSEDED,
            RemoteRevokeStatus.NOT_APPLICABLE,
        )
        else "NULL"
    )
    completed_sql = (
        "NOW()"
        if local_completed_sql == "NOW()" and remote_completed_sql == "NOW()"
        else "NULL"
    )
    next_retry_sql = "NOW()" if remote_val == RemoteRevokeStatus.PENDING else "NULL"

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_integration_purge_ledger (
                    purge_id, user_id, provider, service,
                    oauth_client_id, provider_account_id, auth_epoch_upto,
                    trigger_reason, local_purge_status, purged_max_turn_by_thread,
                    tracker_reconcile_needed, local_completed_at,
                    remote_revoke_status, encrypted_revoke_token,
                    retry_count, next_retry_at, last_remote_error,
                    remote_completed_at, disconnected_at, completed_at
                )
                VALUES (
                    %(purge_id)s::uuid, %(user_id)s, %(provider)s, %(service)s,
                    %(oauth_client_id)s, %(provider_account_id)s, %(auth_epoch_upto)s,
                    %(trigger_reason)s, %(local_purge_status)s, %(purged_max_turn_by_thread)s,
                    %(tracker_reconcile_needed)s, {local_completed_sql},
                    %(remote_revoke_status)s, {token_sql},
                    0, {next_retry_sql}, NULL,
                    {remote_completed_sql}, NOW(), {completed_sql}
                )
                RETURNING {_PURGE_LEDGER_META_COLS}
                """,
                params,
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_purge_ledger(dict(row))


async def get_purge_ledger(
    purge_id: str | UUID,
    *,
    decrypt_revoke_token: bool = False,
    for_update: bool = False,
    conn: Any = None,
) -> PurgeLedgerRow | None:
    """Fetch a purge ledger row by ID."""
    lock_clause = " FOR UPDATE" if for_update else ""
    if decrypt_revoke_token:
        enc_key = get_encryption_key()
        select_cols = (
            f"{_PURGE_LEDGER_META_COLS}, "
            "CASE WHEN encrypted_revoke_token IS NULL THEN NULL "
            "     ELSE pgp_sym_decrypt(encrypted_revoke_token, %(enc_key)s) "
            "END AS decrypted_revoke_token"
        )
        params: dict[str, Any] = {"purge_id": str(purge_id), "enc_key": enc_key}
    else:
        select_cols = _PURGE_LEDGER_META_COLS
        params = {"purge_id": str(purge_id)}

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {select_cols}
                FROM dalton_integration_purge_ledger
                WHERE purge_id = %(purge_id)s::uuid
                {lock_clause}
                """,
                params,
            )
            row = await cur.fetchone()
    return _row_to_purge_ledger(dict(row)) if row is not None else None


async def list_running_local_purges(*, conn: Any = None) -> list[PurgeLedgerRow]:
    """List all purge ledger entries whose local 2A purge is still running (startup resume)."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_PURGE_LEDGER_META_COLS}
                FROM dalton_integration_purge_ledger
                WHERE local_purge_status = 'running'
                ORDER BY disconnected_at ASC
                """
            )
            rows = await cur.fetchall()
    return [_row_to_purge_ledger(dict(r)) for r in rows]


async def list_due_remote_revokes(*, limit: int = 50, conn: Any = None) -> list[PurgeLedgerRow]:
    """List purge ledger entries due for remote revocation retry (including initial pending rows)."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_PURGE_LEDGER_META_COLS}
                FROM dalton_integration_purge_ledger
                WHERE (
                    remote_revoke_status = 'failed_retryable'
                    AND next_retry_at IS NOT NULL
                    AND next_retry_at <= NOW()
                ) OR (
                    remote_revoke_status = 'pending'
                    AND (next_retry_at IS NULL OR next_retry_at <= NOW())
                )
                ORDER BY COALESCE(next_retry_at, disconnected_at) ASC, disconnected_at ASC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
    return [_row_to_purge_ledger(dict(r)) for r in rows]


async def list_active_revokes_for_google_project_sub(
    user_id: str,
    google_project: str,
    sub: str,
    *,
    decrypt_revoke_token: bool = False,
    conn: Any = None,
) -> list[PurgeLedgerRow]:
    """List all ``pending | failed_retryable`` Google revoke tasks for ``(user_id, google_project, sub)``."""
    if decrypt_revoke_token:
        enc_key = get_encryption_key()
        select_cols = (
            f"{_PURGE_LEDGER_META_COLS}, "
            "CASE WHEN encrypted_revoke_token IS NULL THEN NULL "
            "     ELSE pgp_sym_decrypt(encrypted_revoke_token, %(enc_key)s) "
            "END AS decrypted_revoke_token"
        )
        params: dict[str, Any] = {
            "user_id": user_id,
            "sub": sub,
            "enc_key": enc_key,
        }
    else:
        select_cols = _PURGE_LEDGER_META_COLS
        params = {"user_id": user_id, "sub": sub}

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {select_cols}
                FROM dalton_integration_purge_ledger
                WHERE user_id = %(user_id)s
                  AND provider = 'google'
                  AND provider_account_id = %(sub)s
                  AND remote_revoke_status IN ('pending', 'failed_retryable')
                ORDER BY disconnected_at ASC
                """,
                params,
            )
            rows = await cur.fetchall()
    return [
        _row_to_purge_ledger(dict(r))
        for r in rows
        if extract_google_project(str(r["oauth_client_id"])) == google_project
    ]


async def mark_tracker_reconcile_needed(
    purge_id: str | UUID, *, needed: bool = True, conn: Any = None
) -> None:
    """Set ``tracker_reconcile_needed`` on a purge ledger row."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_integration_purge_ledger
                SET tracker_reconcile_needed = %s
                WHERE purge_id = %s::uuid
                """,
                (bool(needed), str(purge_id)),
            )


async def update_purge_ledger_remote_status(
    purge_id: str | UUID,
    *,
    remote_revoke_status: RemoteRevokeStatus | str,
    clear_revoke_token: bool = False,
    increment_retry: bool = False,
    next_retry_at: datetime | None = None,
    last_remote_error: str | None = None,
    mark_remote_completed: bool = False,
    conn: Any = None,
) -> PurgeLedgerRow | None:
    """Update remote revocation state on a purge ledger row."""
    status_val = str(RemoteRevokeStatus(remote_revoke_status))
    token_sql = "encrypted_revoke_token = NULL," if clear_revoke_token else ""
    retry_sql = "retry_count = retry_count + 1," if increment_retry else ""
    remote_done_sql = (
        "remote_completed_at = COALESCE(remote_completed_at, NOW()),"
        if mark_remote_completed
        else ""
    )
    completed_sql = (
        """completed_at = CASE
               WHEN local_purge_status IN ('completed', 'not_required')
               THEN COALESCE(completed_at, NOW())
               ELSE completed_at
           END"""
        if mark_remote_completed
        else "completed_at = completed_at"
    )
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_integration_purge_ledger
                SET remote_revoke_status = %(status)s,
                    {token_sql}
                    {retry_sql}
                    next_retry_at = %(next_retry_at)s,
                    last_remote_error = %(last_remote_error)s,
                    {remote_done_sql}
                    {completed_sql}
                WHERE purge_id = %(purge_id)s::uuid
                RETURNING {_PURGE_LEDGER_META_COLS}
                """,
                {
                    "purge_id": str(purge_id),
                    "status": status_val,
                    "next_retry_at": next_retry_at,
                    "last_remote_error": last_remote_error,
                },
            )
            row = await cur.fetchone()
    return _row_to_purge_ledger(dict(row)) if row is not None else None


# ---------------------------------------------------------------------------
# Webhook event dedup
# ---------------------------------------------------------------------------


async def record_webhook_event(
    provider: str,
    event_id: str,
    *,
    conn: Any = None,
) -> bool:
    """Atomically record ``(provider, event_id)`` for idempotency.

    Returns ``True`` if this is the first time the event is seen, ``False`` if duplicate.
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_integration_webhook_events (provider, event_id, received_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (provider, event_id) DO NOTHING
                RETURNING 1
                """,
                (provider, event_id),
            )
            return (await cur.fetchone()) is not None


# ---------------------------------------------------------------------------
# OAuth state & Supabase pending confirmation primitives (for Step 2)
# ---------------------------------------------------------------------------


async def insert_oauth_state(
    *,
    state_hash: str,
    user_id: str,
    auth_mode: str,
    session_id: str | None,
    user_email: str | None,
    provider: str,
    service: str,
    oauth_client_id: str,
    intent: str,
    code_verifier: str,
    requested_scopes: str,
    redirect_uri: str,
    return_to: str | None,
    expires_at: datetime,
    conn: Any = None,
) -> None:
    """Persist a 10-minute one-time OAuth state with encrypted PKCE ``code_verifier``."""
    enc_key = get_encryption_key()
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_user_integration_oauth_states (
                    state_hash, user_id, auth_mode, session_id, user_email,
                    provider, service, oauth_client_id, intent,
                    code_verifier, requested_scopes, redirect_uri, return_to,
                    expires_at, created_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    pgp_sym_encrypt(%s, %s), %s, %s, %s,
                    %s, NOW()
                )
                """,
                (
                    state_hash,
                    user_id,
                    auth_mode,
                    session_id,
                    user_email,
                    provider,
                    service,
                    oauth_client_id,
                    intent,
                    code_verifier,
                    enc_key,
                    requested_scopes,
                    redirect_uri,
                    return_to,
                    expires_at,
                ),
            )


async def consume_oauth_state(
    state_hash: str,
    provider: str,
    *,
    conn: Any = None,
) -> OAuthStateRow | None:
    """Atomically consume a valid, unexpired OAuth state (single-use)."""
    enc_key = get_encryption_key()
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                DELETE FROM dalton_user_integration_oauth_states
                WHERE state_hash = %(state_hash)s
                  AND provider = %(provider)s
                  AND expires_at > clock_timestamp()
                RETURNING
                    state_hash, user_id, auth_mode, session_id, user_email,
                    provider, service, oauth_client_id, intent,
                    pgp_sym_decrypt(code_verifier, %(enc_key)s) AS decrypted_code_verifier,
                    requested_scopes, redirect_uri, return_to,
                    expires_at, created_at
                """,
                {
                    "state_hash": state_hash,
                    "provider": provider,
                    "enc_key": enc_key,
                },
            )
            row = await cur.fetchone()
    if row is None:
        return None
    r = dict(row)
    return OAuthStateRow(
        state_hash=str(r["state_hash"]),
        user_id=str(r["user_id"]),
        auth_mode=str(r["auth_mode"]),
        session_id=r.get("session_id"),
        user_email=r.get("user_email"),
        provider=str(r["provider"]),
        service=str(r["service"]),
        oauth_client_id=str(r["oauth_client_id"]),
        intent=str(r["intent"]),
        requested_scopes=str(r["requested_scopes"]),
        redirect_uri=str(r["redirect_uri"]),
        return_to=r.get("return_to"),
        expires_at=r["expires_at"],
        created_at=r["created_at"],
        code_verifier=r.get("decrypted_code_verifier"),
    )


async def insert_pending_confirmation(
    *,
    confirmation_hash: str,
    user_id: str,
    provider: str,
    service: str,
    oauth_client_id: str,
    provider_account_id: str,
    provider_union_id: str | None,
    provider_email: str | None,
    granted_scopes: str,
    access_token: str | None,
    refresh_token: str | None,
    expires_at: datetime | None,
    intent: str,
    preferences: dict[str, Any] | None,
    confirm_expires_at: datetime,
    conn: Any = None,
) -> None:
    """Stage a 5-minute Supabase post-callback confirmation payload (encrypted at rest)."""
    enc_key = get_encryption_key()
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_user_integration_pending_confirmations (
                    confirmation_hash, user_id, provider, service,
                    oauth_client_id, provider_account_id, provider_union_id,
                    provider_email, granted_scopes,
                    access_token, refresh_token, expires_at,
                    intent, preferences, confirm_expires_at, created_at
                )
                VALUES (
                    %(confirmation_hash)s, %(user_id)s, %(provider)s, %(service)s,
                    %(oauth_client_id)s, %(provider_account_id)s, %(provider_union_id)s,
                    %(provider_email)s, %(granted_scopes)s,
                    CASE WHEN %(access_token)s::text IS NULL THEN NULL
                         ELSE pgp_sym_encrypt(%(access_token)s, %(enc_key)s) END,
                    CASE WHEN %(refresh_token)s::text IS NULL THEN NULL
                         ELSE pgp_sym_encrypt(%(refresh_token)s, %(enc_key)s) END,
                    %(expires_at)s,
                    %(intent)s, %(preferences)s, %(confirm_expires_at)s, NOW()
                )
                """,
                {
                    "confirmation_hash": confirmation_hash,
                    "user_id": user_id,
                    "provider": provider,
                    "service": service,
                    "oauth_client_id": oauth_client_id,
                    "provider_account_id": provider_account_id,
                    "provider_union_id": provider_union_id,
                    "provider_email": provider_email,
                    "granted_scopes": granted_scopes,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "enc_key": enc_key,
                    "expires_at": expires_at,
                    "intent": intent,
                    "preferences": Jsonb(preferences or {}),
                    "confirm_expires_at": confirm_expires_at,
                },
            )


async def consume_pending_confirmation(
    confirmation_hash: str,
    user_id: str,
    *,
    conn: Any = None,
) -> PendingConfirmationRow | None:
    """Atomically consume a Supabase pending confirmation owned by ``user_id``."""
    enc_key = get_encryption_key()
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                DELETE FROM dalton_user_integration_pending_confirmations
                WHERE confirmation_hash = %(confirmation_hash)s
                  AND user_id = %(user_id)s
                  AND confirm_expires_at > clock_timestamp()
                RETURNING
                    confirmation_hash, user_id, provider, service,
                    oauth_client_id, provider_account_id, provider_union_id,
                    provider_email, granted_scopes,
                    CASE WHEN access_token IS NULL THEN NULL
                         ELSE pgp_sym_decrypt(access_token, %(enc_key)s) END AS decrypted_access_token,
                    CASE WHEN refresh_token IS NULL THEN NULL
                         ELSE pgp_sym_decrypt(refresh_token, %(enc_key)s) END AS decrypted_refresh_token,
                    expires_at, intent, preferences, confirm_expires_at, created_at
                """,
                {
                    "confirmation_hash": confirmation_hash,
                    "user_id": user_id,
                    "enc_key": enc_key,
                },
            )
            row = await cur.fetchone()
    if row is None:
        return None
    r = dict(row)
    return PendingConfirmationRow(
        confirmation_hash=str(r["confirmation_hash"]),
        user_id=str(r["user_id"]),
        provider=str(r["provider"]),
        service=str(r["service"]),
        oauth_client_id=str(r["oauth_client_id"]),
        provider_account_id=str(r["provider_account_id"]),
        provider_union_id=r.get("provider_union_id"),
        provider_email=r.get("provider_email"),
        granted_scopes=str(r["granted_scopes"]),
        expires_at=r.get("expires_at"),
        intent=str(r["intent"]),
        preferences=dict(r.get("preferences") or {}),
        confirm_expires_at=r["confirm_expires_at"],
        created_at=r["created_at"],
        access_token=r.get("decrypted_access_token"),
        refresh_token=r.get("decrypted_refresh_token"),
    )


# ---------------------------------------------------------------------------
# Approval cleanup helpers (Layer 6)
# ---------------------------------------------------------------------------


async def delete_approvals_for_disconnect(
    *,
    user_id: str,
    provider: str,
    service: str | None,
    thread_run_ids: list[str],
    conn: Any = None,
) -> int:
    """Delete all approvals for ``(user_id, provider[, service])`` AND all approvals
    for ANY service whose ``run_id`` falls in ``thread_run_ids`` (§4.5(2) Layer 6).
    """
    deleted = 0
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            if service is not None:
                await cur.execute(
                    """
                    DELETE FROM dalton_integration_action_approvals
                    WHERE caller_user_id = %s
                      AND provider = %s
                      AND service = %s
                    """,
                    (user_id, provider, service),
                )
            else:
                await cur.execute(
                    """
                    DELETE FROM dalton_integration_action_approvals
                    WHERE caller_user_id = %s
                      AND provider = %s
                    """,
                    (user_id, provider),
                )
            deleted += int(cur.rowcount or 0)

            if thread_run_ids:
                await cur.execute(
                    """
                    DELETE FROM dalton_integration_action_approvals
                    WHERE run_id = ANY(%s::uuid[])
                    """,
                    (thread_run_ids,),
                )
                deleted += int(cur.rowcount or 0)
    return deleted


# ---------------------------------------------------------------------------
# Step 2 additive query & CAS helpers (OAuth, refresh, webhook, 12h sweep)
# ---------------------------------------------------------------------------


async def list_connections_by_account(
    user_id: str,
    provider: str,
    oauth_client_id: str,
    provider_account_id: str,
    *,
    decrypt_tokens: bool = False,
    for_update: bool = False,
    conn: Any = None,
) -> list[IntegrationRow]:
    """List all connection rows for ``(user_id, provider, oauth_client_id, provider_account_id)``
    in ascending ``service`` order (deterministic PK lock ordering for Google shared tokens).
    """
    lock_clause = " FOR UPDATE" if for_update else ""
    if decrypt_tokens:
        enc_key = get_encryption_key()
        select_cols = (
            f"{_INTEGRATION_META_COLS}, "
            "CASE WHEN access_token IS NULL THEN NULL "
            "     ELSE pgp_sym_decrypt(access_token, %(enc_key)s) END AS decrypted_access_token, "
            "CASE WHEN refresh_token IS NULL THEN NULL "
            "     ELSE pgp_sym_decrypt(refresh_token, %(enc_key)s) END AS decrypted_refresh_token"
        )
        params: dict[str, Any] = {
            "user_id": user_id,
            "provider": provider,
            "oauth_client_id": oauth_client_id,
            "provider_account_id": provider_account_id,
            "enc_key": enc_key,
        }
    else:
        select_cols = _INTEGRATION_META_COLS
        params = {
            "user_id": user_id,
            "provider": provider,
            "oauth_client_id": oauth_client_id,
            "provider_account_id": provider_account_id,
        }

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {select_cols}
                FROM dalton_user_integrations
                WHERE user_id = %(user_id)s
                  AND provider = %(provider)s
                  AND oauth_client_id = %(oauth_client_id)s
                  AND provider_account_id = %(provider_account_id)s
                ORDER BY service ASC
                {lock_clause}
                """,
                params,
            )
            rows = await cur.fetchall()
    return [_row_to_integration(dict(r)) for r in rows]


async def list_connections_by_provider_account(
    provider: str,
    oauth_client_id: str,
    *,
    provider_account_id: str | None = None,
    provider_union_id: str | None = None,
    conn: Any = None,
) -> list[IntegrationRow]:
    """Find connections matching ``(provider, oauth_client_id)`` and ``open_id`` / ``union_id``."""
    match_clauses: list[str] = []
    params: dict[str, Any] = {
        "provider": provider,
        "oauth_client_id": oauth_client_id,
    }
    if provider_account_id:
        match_clauses.append("provider_account_id = %(provider_account_id)s")
        params["provider_account_id"] = provider_account_id
    if provider_union_id:
        match_clauses.append("provider_union_id = %(provider_union_id)s")
        params["provider_union_id"] = provider_union_id
    if not match_clauses:
        return []

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_INTEGRATION_META_COLS}
                FROM dalton_user_integrations
                WHERE provider = %(provider)s
                  AND oauth_client_id = %(oauth_client_id)s
                  AND ({' OR '.join(match_clauses)})
                ORDER BY user_id ASC, service ASC
                """,
                params,
            )
            rows = await cur.fetchall()
    return [_row_to_integration(dict(r)) for r in rows]


async def list_connections_for_health_sweep(
    *,
    limit: int = 500,
    conn: Any = None,
) -> list[IntegrationRow]:
    """List ``connected`` and ``refresh_ambiguous`` connections for the 12-hour health patrol."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_INTEGRATION_META_COLS}
                FROM dalton_user_integrations
                WHERE status IN ('connected', 'refresh_ambiguous')
                ORDER BY COALESCE(last_refresh_at, created_at) ASC, user_id ASC, provider ASC, service ASC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
    return [_row_to_integration(dict(r)) for r in rows]


async def list_user_purge_ledgers(
    user_id: str,
    *,
    active_only: bool = False,
    limit: int = 50,
    conn: Any = None,
) -> list[PurgeLedgerRow]:
    """List purge/revoke ledger entries for ``user_id`` without decrypting revoke tokens."""
    extra = (
        " AND (local_purge_status = 'running' OR remote_revoke_status IN ('pending', 'failed_retryable'))"
        if active_only
        else ""
    )
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_PURGE_LEDGER_META_COLS}
                FROM dalton_integration_purge_ledger
                WHERE user_id = %s
                  {extra}
                ORDER BY disconnected_at DESC
                LIMIT %s
                """,
                (user_id, int(limit)),
            )
            rows = await cur.fetchall()
    return [_row_to_purge_ledger(dict(r)) for r in rows]


async def has_user_any_integration_records(
    user_id: str,
    *,
    conn: Any = None,
) -> bool:
    """Return True if ``user_id`` has any stored integration row or active purge/revoke ledger."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM dalton_user_integrations WHERE user_id = %s
                    UNION ALL
                    SELECT 1 FROM dalton_integration_purge_ledger
                    WHERE user_id = %s
                      AND (
                          local_purge_status = 'running'
                          OR remote_revoke_status IN ('pending', 'failed_retryable')
                      )
                )
                """,
                (user_id, user_id),
            )
            row = await cur.fetchone()
    return bool(row and row[0])


async def has_running_local_purge_for_service(
    user_id: str,
    provider: str,
    service: str,
    *,
    conn: Any = None,
) -> list[str]:
    """Return ``purge_id`` values with ``local_purge_status = 'running'`` for ``(user_id, provider, service)``."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                SELECT purge_id::text
                FROM dalton_integration_purge_ledger
                WHERE user_id = %s
                  AND provider = %s
                  AND (service = %s OR service IS NULL)
                  AND local_purge_status = 'running'
                ORDER BY disconnected_at ASC
                """,
                (user_id, provider, service),
            )
            rows = await cur.fetchall()
    return [str(r[0]) for r in rows]


async def peek_pending_confirmation(
    confirmation_hash: str,
    user_id: str,
    *,
    conn: Any = None,
) -> PendingConfirmationRow | None:
    """Non-destructively read a pending confirmation owned by ``user_id`` (no token decryption)."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    confirmation_hash, user_id, provider, service,
                    oauth_client_id, provider_account_id, provider_union_id,
                    provider_email, granted_scopes,
                    expires_at, intent, preferences, confirm_expires_at, created_at
                FROM dalton_user_integration_pending_confirmations
                WHERE confirmation_hash = %(confirmation_hash)s
                  AND user_id = %(user_id)s
                  AND confirm_expires_at > clock_timestamp()
                """,
                {"confirmation_hash": confirmation_hash, "user_id": user_id},
            )
            row = await cur.fetchone()
    if row is None:
        return None
    r = dict(row)
    return PendingConfirmationRow(
        confirmation_hash=str(r["confirmation_hash"]),
        user_id=str(r["user_id"]),
        provider=str(r["provider"]),
        service=str(r["service"]),
        oauth_client_id=str(r["oauth_client_id"]),
        provider_account_id=str(r["provider_account_id"]),
        provider_union_id=r.get("provider_union_id"),
        provider_email=r.get("provider_email"),
        granted_scopes=str(r["granted_scopes"]),
        expires_at=r.get("expires_at"),
        intent=str(r["intent"]),
        preferences=dict(r.get("preferences") or {}),
        confirm_expires_at=r["confirm_expires_at"],
        created_at=r["created_at"],
        access_token=None,
        refresh_token=None,
    )


async def update_connection_preferences(
    user_id: str,
    provider: str,
    service: str,
    *,
    preferences: dict[str, Any],
    conn: Any = None,
) -> IntegrationRow | None:
    """Replace ``preferences`` on an existing integration row."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE dalton_user_integrations
                SET preferences = %s,
                    updated_at = NOW()
                WHERE user_id = %s
                  AND provider = %s
                  AND service = %s
                RETURNING {_INTEGRATION_META_COLS}
                """,
                (Jsonb(preferences), user_id, provider, service),
            )
            row = await cur.fetchone()
    return _row_to_integration(dict(row)) if row is not None else None


async def delete_expired_oauth_states_and_confirmations(
    *,
    conn: Any = None,
) -> tuple[int, int]:
    """Delete expired OAuth state and pending confirmation rows; returns ``(states_deleted, confirms_deleted)``."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM dalton_user_integration_oauth_states WHERE expires_at <= clock_timestamp()"
            )
            states_deleted = int(cur.rowcount or 0)
            await cur.execute(
                "DELETE FROM dalton_user_integration_pending_confirmations WHERE confirm_expires_at <= clock_timestamp()"
            )
            confirms_deleted = int(cur.rowcount or 0)
    return states_deleted, confirms_deleted


async def get_channel_feishu_open_id(
    user_id: str,
    *,
    conn: Any = None,
) -> str | None:
    """Look up ``user_id``'s bound Feishu ``open_id`` in ``dalton_channel_identities``, if any (§4.3 item 2)."""
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT platform_user_id
                    FROM dalton_channel_identities
                    WHERE user_id = %s
                      AND platform = 'feishu'
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
        if row and row[0]:
            return str(row[0]).strip() or None
        return None
    except Exception:
        # Standalone core or stripped schemas without channel_gateway tables bind independently.
        return None


async def read_health_sweep_state(
    *,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read the singleton 12-hour health sweep coordination row."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT last_started_at, last_completed_at, last_worker_id, clock_timestamp() AS db_now
                FROM dalton_integration_health_sweep_state
                WHERE singleton = TRUE
                """
            )
            row = await cur.fetchone()
    return dict(row) if row is not None else None


async def update_health_sweep_state(
    *,
    started: bool = False,
    completed: bool = False,
    worker_id: str | None = None,
    conn: Any = None,
) -> None:
    """Update ``last_started_at`` / ``last_completed_at`` on the singleton sweep coordination row."""
    sets = ["updated_at = NOW()"]
    params: dict[str, Any] = {}
    if started:
        sets.append("last_started_at = NOW()")
    if completed:
        sets.append("last_completed_at = NOW()")
    if worker_id is not None:
        sets.append("last_worker_id = %(worker_id)s")
        params["worker_id"] = worker_id
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO dalton_integration_health_sweep_state (singleton)
                VALUES (TRUE)
                ON CONFLICT DO NOTHING
                """
            )
            await cur.execute(
                f"""
                UPDATE dalton_integration_health_sweep_state
                SET {', '.join(sets)}
                WHERE singleton = TRUE
                """,
                params,
            )
