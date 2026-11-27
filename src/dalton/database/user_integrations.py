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


def revoke_lock_key(google_project: str, sub: str) -> int:
    """Per-(Google project, sub) remote revocation advisory lock key across all Dalton users (§4.2(3))."""
    return advisory_key(
        "dalton_integration_revoke",
        str(google_project),
        str(sub),
    )


def google_project_coord_lock_key(google_project: str) -> int:
    """Per-Google-Cloud-project leaf coordination lock serializing OAuth exchange registration with revoke claiming."""
    return advisory_key(
        "dalton_integration_google_project_coord",
        str(google_project),
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
    refresh_attempt_gen: int = 0
    refresh_inflight_until: datetime | None = None
    db_now: datetime | None = None


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
    revoke_attempt_gen: int = 0
    revoke_inflight_until: datetime | None = None
    local_purge_attempt_gen: int = 0
    local_purge_inflight_until: datetime | None = None


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
    browser_binding_hash: str | None = None
    expected_provider_account_id: str | None = None
    exchange_expires_at: datetime | None = None
    baseline_existed: bool | None = None
    baseline_oauth_client_id: str | None = None
    baseline_provider_account_id: str | None = None
    baseline_auth_epoch: int | None = None
    voided_at: datetime | None = None


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
    expected_old_oauth_client_id: str | None = None
    expected_old_provider_account_id: str | None = None
    expected_old_auth_epoch: int | None = None


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
        refresh_attempt_gen=int(r.get("refresh_attempt_gen") or 0),
        refresh_inflight_until=r.get("refresh_inflight_until"),
        db_now=r.get("db_now"),
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
        revoke_attempt_gen=int(r.get("revoke_attempt_gen") or 0),
        revoke_inflight_until=r.get("revoke_inflight_until"),
        local_purge_attempt_gen=int(r.get("local_purge_attempt_gen") or 0),
        local_purge_inflight_until=r.get("local_purge_inflight_until"),
    )


_INTEGRATION_META_COLS = """
    user_id, provider, service, oauth_client_id, provider_account_id,
    provider_union_id, provider_email, granted_scopes,
    auth_epoch, authorized_at, token_generation, status,
    refresh_inflight_gen, refresh_attempt_gen, refresh_started_at, refresh_inflight_until,
    expires_at, last_refresh_at, needs_reauth_at, last_error_code, reauth_notified_at,
    preferences,
    (access_token IS NOT NULL) AS has_access_token,
    (refresh_token IS NOT NULL) AS has_refresh_token,
    created_at, updated_at,
    clock_timestamp() AS db_now
"""

_PURGE_LEDGER_META_COLS = """
    purge_id, user_id, provider, service, oauth_client_id, provider_account_id,
    auth_epoch_upto, trigger_reason, local_purge_status,
    local_purge_attempt_gen, local_purge_inflight_until,
    purged_max_turn_by_thread, tracker_reconcile_needed, local_completed_at,
    remote_revoke_status,
    (encrypted_revoke_token IS NOT NULL) AS has_revoke_token,
    revoke_attempt_gen, revoke_inflight_until,
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
                    refresh_attempt_gen = dalton_user_integrations.refresh_attempt_gen + 1,
                    refresh_started_at = NULL,
                    refresh_inflight_until = NULL,
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
        clauses.append("status IN ('connected', 'refresh_ambiguous')")

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
    clear_refresh_token_only: bool = False,
    expected_auth_epoch: int | None = None,
    conn: Any = None,
) -> bool:
    """Update a connection's status (optionally bounded by ``expected_auth_epoch``)."""
    status_val = str(IntegrationStatus(status))
    if clear_tokens:
        token_sets = "access_token = NULL, refresh_token = NULL, "
    elif clear_refresh_token_only:
        token_sets = "refresh_token = NULL, "
    else:
        token_sets = ""
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
                    refresh_attempt_gen = refresh_attempt_gen + 1,
                    refresh_started_at = NULL,
                    refresh_inflight_until = NULL,
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


async def release_refresh_inflight_lease(
    user_id: str,
    provider: str,
    service: str,
    *,
    oauth_client_id: str,
    provider_account_id: str,
    expected_attempt_gen: int,
    preserve_crash_marker: bool = False,
    conn: Any = None,
) -> None:
    """Clear ``refresh_inflight_until`` (and optionally ``refresh_inflight_gen``) for matching ``refresh_attempt_gen``."""
    clear_gen_sql = "" if preserve_crash_marker else "refresh_inflight_gen = NULL, refresh_started_at = NULL, "
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            if provider == "google":
                await cur.execute(
                    f"""
                    UPDATE dalton_user_integrations
                    SET {clear_gen_sql}
                        refresh_inflight_until = NULL
                    WHERE user_id = %s
                      AND provider = 'google'
                      AND oauth_client_id = %s
                      AND provider_account_id = %s
                      AND refresh_attempt_gen = %s
                    """,
                    (user_id, oauth_client_id, provider_account_id, int(expected_attempt_gen)),
                )
            else:
                await cur.execute(
                    f"""
                    UPDATE dalton_user_integrations
                    SET {clear_gen_sql}
                        refresh_inflight_until = NULL
                    WHERE user_id = %s
                      AND provider = %s
                      AND service = %s
                      AND refresh_attempt_gen = %s
                    """,
                    (user_id, provider, service, int(expected_attempt_gen)),
                )


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


async def claim_local_purge_attempt(
    purge_id: str | UUID,
    *,
    lease_seconds: int = 300,
    conn: Any = None,
) -> int | None:
    """Atomically claim a durable Scheme 2A local purge execution generation and lease.

    Allows releasing any PostgreSQL advisory lock / dedicated session before Redis I/O
    while preventing duplicate concurrent execution across workers. Returns the claimed
    ``local_purge_attempt_gen``, or ``None`` if the ledger is not ``running`` or another
    worker holds an unexpired lease.
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_integration_purge_ledger
                SET local_purge_attempt_gen = local_purge_attempt_gen + 1,
                    local_purge_inflight_until = clock_timestamp() + make_interval(secs => %s)
                WHERE purge_id = %s::uuid
                  AND local_purge_status = 'running'
                  AND (
                      local_purge_inflight_until IS NULL
                      OR local_purge_inflight_until <= clock_timestamp()
                  )
                RETURNING local_purge_attempt_gen
                """,
                (max(1, int(lease_seconds)), str(purge_id)),
            )
            row = await cur.fetchone()
    return int(row[0]) if row is not None else None


async def renew_local_purge_attempt(
    purge_id: str | UUID,
    *,
    expected_attempt_gen: int,
    lease_seconds: int = 300,
    conn: Any = None,
) -> bool:
    """Renew an active Scheme 2A local purge lease iff ``local_purge_attempt_gen`` still matches and has not expired."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_integration_purge_ledger
                SET local_purge_inflight_until = clock_timestamp() + make_interval(secs => %s)
                WHERE purge_id = %s::uuid
                  AND local_purge_status = 'running'
                  AND local_purge_attempt_gen = %s
                  AND local_purge_inflight_until IS NOT NULL
                  AND local_purge_inflight_until > clock_timestamp()
                RETURNING local_purge_attempt_gen
                """,
                (max(1, int(lease_seconds)), str(purge_id), int(expected_attempt_gen)),
            )
            row = await cur.fetchone()
    return row is not None


async def release_local_purge_attempt(
    purge_id: str | UUID,
    *,
    expected_attempt_gen: int,
    conn: Any = None,
) -> None:
    """Clear ``local_purge_inflight_until`` on error/abort so retries can resume immediately."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_integration_purge_ledger
                SET local_purge_inflight_until = NULL
                WHERE purge_id = %s::uuid
                  AND local_purge_attempt_gen = %s
                """,
                (str(purge_id), int(expected_attempt_gen)),
            )


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
    """List purge ledger entries due for remote revocation retry once all local 2A purges for that (user, provider) finish."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_PURGE_LEDGER_META_COLS}
                FROM dalton_integration_purge_ledger l
                WHERE l.local_purge_status IN ('completed', 'not_required')
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dalton_integration_purge_ledger l2
                      WHERE l2.user_id = l.user_id
                        AND l2.provider = l.provider
                        AND l2.local_purge_status = 'running'
                  )
                  AND (
                      (
                          l.remote_revoke_status = 'failed_retryable'
                          AND l.next_retry_at IS NOT NULL
                          AND l.next_retry_at <= NOW()
                      ) OR (
                          l.remote_revoke_status = 'pending'
                          AND (l.next_retry_at IS NULL OR l.next_retry_at <= NOW())
                      )
                  )
                ORDER BY COALESCE(l.next_retry_at, l.disconnected_at) ASC, l.disconnected_at ASC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
    return [_row_to_purge_ledger(dict(r)) for r in rows]


async def list_active_revokes_for_google_project_sub(
    google_project: str,
    sub: str,
    *,
    user_id: str | None = None,
    decrypt_revoke_token: bool = False,
    conn: Any = None,
) -> list[PurgeLedgerRow]:
    """List all ``pending | failed_retryable`` Google revoke tasks for ``(google_project, sub)`` across all users (optionally filtered by ``user_id``)."""
    clauses = [
        "provider = 'google'",
        "provider_account_id = %(sub)s",
        "remote_revoke_status IN ('pending', 'failed_retryable')",
    ]
    params: dict[str, Any] = {"sub": sub}
    if user_id is not None:
        clauses.append("user_id = %(user_id)s")
        params["user_id"] = user_id

    if decrypt_revoke_token:
        enc_key = get_encryption_key()
        select_cols = (
            f"{_PURGE_LEDGER_META_COLS}, "
            "CASE WHEN encrypted_revoke_token IS NULL THEN NULL "
            "     ELSE pgp_sym_decrypt(encrypted_revoke_token, %(enc_key)s) "
            "END AS decrypted_revoke_token"
        )
        params["enc_key"] = enc_key
    else:
        select_cols = _PURGE_LEDGER_META_COLS

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {select_cols}
                FROM dalton_integration_purge_ledger
                WHERE {' AND '.join(clauses)}
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


async def has_running_local_purge_for_user_provider(
    user_id: str,
    provider: str,
    *,
    conn: Any = None,
) -> bool:
    """Return True if any purge ledger for ``(user_id, provider)`` still has ``local_purge_status = 'running'``."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                FROM dalton_integration_purge_ledger
                WHERE user_id = %s
                  AND provider = %s
                  AND local_purge_status = 'running'
                LIMIT 1
                """,
                (user_id, provider),
            )
            return (await cur.fetchone()) is not None


async def claim_revoke_attempt_tx(
    purge_id: str | UUID,
    *,
    lease_seconds: int = 30,
    conn: Any = None,
) -> int | None:
    """Atomically increment ``revoke_attempt_gen`` and set ``revoke_inflight_until`` lease for one external HTTP revoke attempt.

    Returns the new ``revoke_attempt_gen`` if claimed, or ``None`` if another worker
    currently holds an active lease or the ledger is no longer ``pending | failed_retryable``.
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_integration_purge_ledger
                SET revoke_attempt_gen = revoke_attempt_gen + 1,
                    revoke_inflight_until = clock_timestamp() + make_interval(secs => %(lease_seconds)s)
                WHERE purge_id = %(purge_id)s::uuid
                  AND remote_revoke_status IN ('pending', 'failed_retryable')
                  AND (
                      revoke_inflight_until IS NULL
                      OR revoke_inflight_until <= clock_timestamp()
                  )
                RETURNING revoke_attempt_gen
                """,
                {
                    "purge_id": str(purge_id),
                    "lease_seconds": int(lease_seconds),
                },
            )
            row = await cur.fetchone()
    return int(row[0]) if row is not None else None


async def release_revoke_inflight_lease(
    purge_id: str | UUID,
    *,
    expected_attempt_gen: int,
    conn: Any = None,
) -> None:
    """Clear ``revoke_inflight_until`` if ``revoke_attempt_gen`` still matches."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE dalton_integration_purge_ledger
                SET revoke_inflight_until = NULL
                WHERE purge_id = %s::uuid
                  AND revoke_attempt_gen = %s
                """,
                (str(purge_id), int(expected_attempt_gen)),
            )


async def has_inflight_google_revoke_in_project(
    google_project: str,
    *,
    conn: Any = None,
) -> bool:
    """Return True if any Google revoke HTTP call in ``google_project`` currently holds an unexpired inflight lease."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT oauth_client_id
                FROM dalton_integration_purge_ledger
                WHERE provider = 'google'
                  AND remote_revoke_status IN ('pending', 'failed_retryable')
                  AND revoke_inflight_until IS NOT NULL
                  AND revoke_inflight_until > clock_timestamp()
                """
            )
            rows = await cur.fetchall()
    return any(
        extract_google_project(str(r["oauth_client_id"])) == google_project
        for r in rows
    )


async def has_inflight_google_revoke_for_project_sub(
    google_project: str,
    sub: str,
    *,
    exclude_purge_id: str | UUID | None = None,
    conn: Any = None,
) -> bool:
    """Return True if any Google revoke ledger for ``(google_project, sub)`` holds an unexpired inflight lease."""
    clauses = [
        "provider = 'google'",
        "provider_account_id = %(sub)s",
        "remote_revoke_status IN ('pending', 'failed_retryable')",
        "revoke_inflight_until IS NOT NULL",
        "revoke_inflight_until > clock_timestamp()",
    ]
    params: dict[str, Any] = {"sub": sub}
    if exclude_purge_id is not None:
        clauses.append("purge_id <> %(exclude_purge_id)s::uuid")
        params["exclude_purge_id"] = str(exclude_purge_id)
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT oauth_client_id
                FROM dalton_integration_purge_ledger
                WHERE {' AND '.join(clauses)}
                """,
                params,
            )
            rows = await cur.fetchall()
    return any(
        extract_google_project(str(r["oauth_client_id"])) == google_project
        for r in rows
    )


async def finalize_revoke_attempt_tx(
    purge_id: str | UUID,
    *,
    expected_attempt_gen: int,
    remote_revoke_status: RemoteRevokeStatus | str,
    clear_revoke_token: bool = False,
    increment_retry: bool = False,
    next_retry_at: datetime | None = None,
    last_remote_error: str | None = None,
    mark_remote_completed: bool = False,
    conn: Any = None,
) -> PurgeLedgerRow | None:
    """Commit the result of an external revoke attempt iff ``revoke_attempt_gen`` matches and status is still active."""
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
                    revoke_inflight_until = NULL,
                    {token_sql}
                    {retry_sql}
                    next_retry_at = %(next_retry_at)s,
                    last_remote_error = %(last_remote_error)s,
                    {remote_done_sql}
                    {completed_sql}
                WHERE purge_id = %(purge_id)s::uuid
                  AND revoke_attempt_gen = %(expected_attempt_gen)s
                  AND remote_revoke_status IN ('pending', 'failed_retryable')
                RETURNING {_PURGE_LEDGER_META_COLS}
                """,
                {
                    "purge_id": str(purge_id),
                    "expected_attempt_gen": int(expected_attempt_gen),
                    "status": status_val,
                    "next_retry_at": next_retry_at,
                    "last_remote_error": last_remote_error,
                },
            )
            row = await cur.fetchone()
    return _row_to_purge_ledger(dict(row)) if row is not None else None


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
                    revoke_attempt_gen = revoke_attempt_gen + 1,
                    revoke_inflight_until = NULL,
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


async def has_webhook_event(
    provider: str,
    event_id: str,
    *,
    conn: Any = None,
) -> bool:
    """Return True if ``(provider, event_id)`` has already been recorded."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                FROM dalton_integration_webhook_events
                WHERE provider = %s AND event_id = %s
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
    browser_binding_hash: str | None = None,
    expected_provider_account_id: str | None = None,
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
                    provider, service, oauth_client_id, expected_provider_account_id, intent,
                    code_verifier, requested_scopes, redirect_uri, return_to,
                    browser_binding_hash, expires_at, created_at
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    pgp_sym_encrypt(%s, %s), %s, %s, %s,
                    %s, %s, NOW()
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
                    expected_provider_account_id,
                    intent,
                    code_verifier,
                    enc_key,
                    requested_scopes,
                    redirect_uri,
                    return_to,
                    browser_binding_hash,
                    expires_at,
                ),
            )


async def peek_oauth_state(
    state_hash: str,
    provider: str,
    *,
    conn: Any = None,
) -> tuple[str, str] | None:
    """Non-destructively read ``(user_id, oauth_client_id)`` for an unexpired, unconsumed, non-voided OAuth state."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                SELECT user_id, oauth_client_id
                FROM dalton_user_integration_oauth_states
                WHERE state_hash = %s
                  AND provider = %s
                  AND consumed_at IS NULL
                  AND voided_at IS NULL
                  AND expires_at > clock_timestamp()
                """,
                (state_hash, provider),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return str(row[0]), str(row[1])


async def peek_oauth_state_voided(
    state_hash: str,
    provider: str,
    *,
    conn: Any = None,
) -> bool:
    """Return True if an OAuth state row exists and has been voided by a concurrent disconnect."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                FROM dalton_user_integration_oauth_states
                WHERE state_hash = %s
                  AND provider = %s
                  AND voided_at IS NOT NULL
                """,
                (state_hash, provider),
            )
            return (await cur.fetchone()) is not None


async def void_oauth_states_for_user_provider(
    user_id: str,
    provider: str,
    *,
    service: str | None = None,
    conn: Any = None,
) -> int:
    """Void all pending or in-flight OAuth state intents for ``(user_id, provider[, service])``.

    Called inside disconnect / account-switch Phase 1 transactions so an in-flight OAuth
    callback cannot resurrect a disconnected connection even if the old row is deleted
    before the callback's HTTP exchange completes.
    """
    clauses = [
        "user_id = %(user_id)s",
        "provider = %(provider)s",
        "voided_at IS NULL",
    ]
    params: dict[str, Any] = {"user_id": user_id, "provider": provider}
    if service is not None:
        clauses.append("service = %(service)s")
        params["service"] = service
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE dalton_user_integration_oauth_states
                SET voided_at = clock_timestamp()
                WHERE {' AND '.join(clauses)}
                """,
                params,
            )
            return int(cur.rowcount or 0)


async def get_active_google_oauth_state_expiry(
    google_project: str,
    *,
    sub: str | None = None,
    conn: Any = None,
) -> datetime | None:
    """Return the furthest expiry blocking revocation for ``(google_project, sub)``.

    - Consumed in-flight code exchanges (``consumed_at IS NOT NULL AND voided_at IS NULL AND exchange_expires_at > clock_timestamp()``)
      block revocations in ``google_project`` for their short exchange window.
    - Unconsumed OAuth states (``consumed_at IS NULL AND voided_at IS NULL AND expires_at > clock_timestamp()``)
      match strictly by ``(google_project, expected_provider_account_id == sub)`` so opening
      a consent screen for another account (or unknown first-connect account) never defers
      unrelated Google accounts' revocations in the same project.
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT oauth_client_id,
                       consumed_at,
                       expected_provider_account_id,
                       CASE WHEN consumed_at IS NOT NULL
                            THEN exchange_expires_at
                            ELSE expires_at
                       END AS effective_expires_at
                FROM dalton_user_integration_oauth_states
                WHERE provider = 'google'
                  AND voided_at IS NULL
                  AND (
                      (
                          consumed_at IS NOT NULL
                          AND exchange_expires_at IS NOT NULL
                          AND exchange_expires_at > clock_timestamp()
                      )
                      OR (
                          consumed_at IS NULL
                          AND expires_at > clock_timestamp()
                          AND %(sub)s::text IS NOT NULL
                          AND expected_provider_account_id = %(sub)s::text
                      )
                  )
                """,
                {"sub": sub},
            )
            rows = await cur.fetchall()
    max_exp: datetime | None = None
    for r in rows:
        if extract_google_project(str(r["oauth_client_id"])) == google_project:
            exp = r["effective_expires_at"]
            if exp is not None and (max_exp is None or exp > max_exp):
                max_exp = exp
    return max_exp


async def delete_oauth_state(
    state_hash: str,
    provider: str,
    *,
    conn: Any = None,
) -> None:
    """Delete an OAuth state / in-flight exchange record once callback staging or cleanup completes."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_user_integration_oauth_states
                WHERE state_hash = %s
                  AND provider = %s
                """,
                (state_hash, provider),
            )


async def delete_pending_confirmation_by_hash(
    confirmation_hash: str,
    user_id: str,
    *,
    conn: Any = None,
) -> None:
    """Delete a pending confirmation (or internal commit-guard row) owned by ``user_id``."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_user_integration_pending_confirmations
                WHERE confirmation_hash = %s
                  AND user_id = %s
                """,
                (confirmation_hash, user_id),
            )


async def list_pending_confirmations_for_user_provider(
    user_id: str,
    provider: str,
    *,
    service: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Read metadata ``(oauth_client_id, provider_account_id, service)`` for pending confirmations owned by ``(user_id, provider[, service])``."""
    clauses = ["user_id = %(user_id)s", "provider = %(provider)s"]
    params: dict[str, Any] = {"user_id": user_id, "provider": provider}
    if service is not None:
        clauses.append("service = %(service)s")
        params["service"] = service
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT service, oauth_client_id, provider_account_id
                FROM dalton_user_integration_pending_confirmations
                WHERE {' AND '.join(clauses)}
                """,
                params,
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def consume_pending_confirmations_for_user_provider(
    user_id: str,
    provider: str,
    *,
    service: str | None = None,
    conn: Any = None,
) -> list[dict[str, Any]]:
    """Atomically void and return raw encrypted tokens for all pending confirmations owned by ``(user_id, provider[, service])``."""
    clauses = ["user_id = %(user_id)s", "provider = %(provider)s"]
    params: dict[str, Any] = {"user_id": user_id, "provider": provider}
    if service is not None:
        clauses.append("service = %(service)s")
        params["service"] = service
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                DELETE FROM dalton_user_integration_pending_confirmations
                WHERE {' AND '.join(clauses)}
                RETURNING
                    confirmation_hash, user_id, provider, service,
                    oauth_client_id, provider_account_id,
                    access_token, refresh_token, intent, preferences, confirm_expires_at
                """,
                params,
            )
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def consume_oauth_state(
    state_hash: str,
    provider: str,
    *,
    exchange_ttl_seconds: int = 30,
    conn: Any = None,
) -> OAuthStateRow | None:
    """Atomically consume a valid, unexpired, non-voided OAuth state (single-use) and snapshot baseline connection epoch.

    Transitions the row in-place (``consumed_at``, ``exchange_expires_at``, ``baseline_*``)
    so:
      - Concurrent replay sees ``consumed_at IS NOT NULL`` and fails single-use atomically.
      - Concurrent disconnect sets ``voided_at`` on this intent row so Step 3 activation
        cannot resurrect a disconnected connection even if the old row was deleted during HTTP.
      - For Google, ``exchange_expires_at`` protects the in-flight code exchange against
        concurrent revocations without holding a PostgreSQL advisory lock across HTTP.
    """
    enc_key = get_encryption_key()
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                WITH target AS (
                    SELECT state_hash, user_id, provider, service
                    FROM dalton_user_integration_oauth_states
                    WHERE state_hash = %(state_hash)s
                      AND provider = %(provider)s
                      AND consumed_at IS NULL
                      AND voided_at IS NULL
                      AND expires_at > clock_timestamp()
                    FOR UPDATE
                ),
                baseline AS (
                    SELECT t.state_hash,
                           (i.user_id IS NOT NULL
                            AND i.status NOT IN ('disconnect_pending', 'revoked_purging')) AS existed,
                           CASE WHEN i.user_id IS NOT NULL
                                 AND i.status NOT IN ('disconnect_pending', 'revoked_purging')
                                THEN i.oauth_client_id ELSE NULL END AS oauth_client_id,
                           CASE WHEN i.user_id IS NOT NULL
                                 AND i.status NOT IN ('disconnect_pending', 'revoked_purging')
                                THEN i.provider_account_id ELSE NULL END AS provider_account_id,
                           CASE WHEN i.user_id IS NOT NULL
                                 AND i.status NOT IN ('disconnect_pending', 'revoked_purging')
                                THEN i.auth_epoch ELSE NULL END AS auth_epoch
                    FROM target t
                    LEFT JOIN dalton_user_integrations i
                      ON i.user_id = t.user_id
                     AND i.provider = t.provider
                     AND i.service = t.service
                )
                UPDATE dalton_user_integration_oauth_states s
                SET consumed_at = clock_timestamp(),
                    exchange_expires_at = clock_timestamp() + make_interval(secs => %(exchange_ttl_seconds)s),
                    baseline_existed = b.existed,
                    baseline_oauth_client_id = b.oauth_client_id,
                    baseline_provider_account_id = b.provider_account_id,
                    baseline_auth_epoch = b.auth_epoch
                FROM baseline b
                WHERE s.state_hash = b.state_hash
                  AND s.consumed_at IS NULL
                  AND s.voided_at IS NULL
                RETURNING
                    s.state_hash, s.user_id, s.auth_mode, s.session_id, s.user_email,
                    s.provider, s.service, s.oauth_client_id, s.expected_provider_account_id, s.intent,
                    pgp_sym_decrypt(s.code_verifier, %(enc_key)s) AS decrypted_code_verifier,
                    s.requested_scopes, s.redirect_uri, s.return_to,
                    s.browser_binding_hash, s.expires_at, s.created_at,
                    s.exchange_expires_at, s.baseline_existed,
                    s.baseline_oauth_client_id, s.baseline_provider_account_id,
                    s.baseline_auth_epoch, s.voided_at
                """,
                {
                    "state_hash": state_hash,
                    "provider": provider,
                    "exchange_ttl_seconds": int(exchange_ttl_seconds),
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
        browser_binding_hash=r.get("browser_binding_hash"),
        expected_provider_account_id=r.get("expected_provider_account_id"),
        exchange_expires_at=r.get("exchange_expires_at"),
        baseline_existed=(
            bool(r["baseline_existed"]) if r.get("baseline_existed") is not None else None
        ),
        baseline_oauth_client_id=r.get("baseline_oauth_client_id"),
        baseline_provider_account_id=r.get("baseline_provider_account_id"),
        baseline_auth_epoch=(
            int(r["baseline_auth_epoch"])
            if r.get("baseline_auth_epoch") is not None
            else None
        ),
        voided_at=r.get("voided_at"),
    )


async def consume_active_oauth_intent_tx(
    state_hash: str,
    provider: str,
    *,
    require_valid_exchange_lease: bool = False,
    conn: Any,
) -> tuple[str, OAuthStateRow | None]:
    """Atomically verify and consume an in-flight OAuth connect intent inside a Step 3 transaction.

    Returns ``(outcome, state_row)`` where ``outcome`` is one of:
      - ``"ok"``: intent was active (``voided_at IS NULL`` and lease unexpired if required)
        and has been deleted inside ``conn``'s open transaction.
      - ``"voided"``: a concurrent disconnect/account-switch voided this intent (deleted).
      - ``"lease_expired"``: ``exchange_expires_at`` already elapsed (deleted).
      - ``"missing"``: intent row was already consumed or cleaned up.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT
                state_hash, user_id, auth_mode, session_id, user_email,
                provider, service, oauth_client_id, expected_provider_account_id, intent,
                requested_scopes, redirect_uri, return_to,
                browser_binding_hash, expires_at, created_at,
                exchange_expires_at, baseline_existed,
                baseline_oauth_client_id, baseline_provider_account_id,
                baseline_auth_epoch, voided_at,
                (exchange_expires_at IS NOT NULL AND exchange_expires_at > clock_timestamp()) AS lease_valid
            FROM dalton_user_integration_oauth_states
            WHERE state_hash = %s
              AND provider = %s
            FOR UPDATE
            """,
            (state_hash, provider),
        )
        row = await cur.fetchone()
        if row is None:
            return "missing", None
        r = dict(row)
        await cur.execute(
            """
            DELETE FROM dalton_user_integration_oauth_states
            WHERE state_hash = %s
              AND provider = %s
            """,
            (state_hash, provider),
        )

    if r.get("voided_at") is not None:
        return "voided", None
    if require_valid_exchange_lease and not bool(r.get("lease_valid")):
        return "lease_expired", None

    return (
        "ok",
        OAuthStateRow(
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
            browser_binding_hash=r.get("browser_binding_hash"),
            expected_provider_account_id=r.get("expected_provider_account_id"),
            exchange_expires_at=r.get("exchange_expires_at"),
            baseline_existed=(
                bool(r["baseline_existed"])
                if r.get("baseline_existed") is not None
                else None
            ),
            baseline_oauth_client_id=r.get("baseline_oauth_client_id"),
            baseline_provider_account_id=r.get("baseline_provider_account_id"),
            baseline_auth_epoch=(
                int(r["baseline_auth_epoch"])
                if r.get("baseline_auth_epoch") is not None
                else None
            ),
            voided_at=r.get("voided_at"),
        ),
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
    expected_old_oauth_client_id: str | None = None,
    expected_old_provider_account_id: str | None = None,
    expected_old_auth_epoch: int | None = None,
    conn: Any = None,
) -> None:
    """Stage a 5-minute post-callback confirmation payload (encrypted at rest)."""
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
                    intent,
                    expected_old_oauth_client_id,
                    expected_old_provider_account_id,
                    expected_old_auth_epoch,
                    preferences, confirm_expires_at, created_at
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
                    %(intent)s,
                    %(expected_old_oauth_client_id)s,
                    %(expected_old_provider_account_id)s,
                    %(expected_old_auth_epoch)s,
                    %(preferences)s, %(confirm_expires_at)s, NOW()
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
                    "expected_old_oauth_client_id": expected_old_oauth_client_id,
                    "expected_old_provider_account_id": expected_old_provider_account_id,
                    "expected_old_auth_epoch": expected_old_auth_epoch,
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
    """Atomically consume a pending confirmation owned by ``user_id``."""
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
                    expires_at, intent,
                    expected_old_oauth_client_id,
                    expected_old_provider_account_id,
                    expected_old_auth_epoch,
                    preferences, confirm_expires_at, created_at
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
        expected_old_oauth_client_id=r.get("expected_old_oauth_client_id"),
        expected_old_provider_account_id=r.get("expected_old_provider_account_id"),
        expected_old_auth_epoch=(
            int(r["expected_old_auth_epoch"])
            if r.get("expected_old_auth_epoch") is not None
            else None
        ),
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
    oauth_client_id: str | None = None,
    provider_account_id: str | None = None,
    min_auth_epoch_upto: int | None = None,
    conn: Any = None,
) -> list[str]:
    """Return ``purge_id`` values with ``local_purge_status = 'running'`` for ``(user_id, provider, service)``.

    When ``oauth_client_id``, ``provider_account_id``, or ``min_auth_epoch_upto`` are supplied,
    only returns ledgers covering that specific app/account/epoch so an in-flight purge for an
    older account never blocks revocation of a newly bound account on the same service.
    """
    clauses = [
        "user_id = %(user_id)s",
        "provider = %(provider)s",
        "(service = %(service)s OR service IS NULL)",
        "local_purge_status = 'running'",
    ]
    params: dict[str, Any] = {
        "user_id": user_id,
        "provider": provider,
        "service": service,
    }
    if oauth_client_id is not None:
        clauses.append("oauth_client_id = %(oauth_client_id)s")
        params["oauth_client_id"] = oauth_client_id
    if provider_account_id is not None:
        clauses.append("provider_account_id = %(provider_account_id)s")
        params["provider_account_id"] = provider_account_id
    if min_auth_epoch_upto is not None:
        clauses.append("auth_epoch_upto >= %(min_auth_epoch_upto)s")
        params["min_auth_epoch_upto"] = int(min_auth_epoch_upto)

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT purge_id::text
                FROM dalton_integration_purge_ledger
                WHERE {' AND '.join(clauses)}
                ORDER BY disconnected_at ASC
                """,
                params,
            )
            rows = await cur.fetchall()
    return [str(r[0]) for r in rows]


async def peek_pending_confirmation(
    confirmation_hash: str,
    user_id: str,
    *,
    decrypt_tokens: bool = False,
    conn: Any = None,
) -> PendingConfirmationRow | None:
    """Non-destructively read a pending confirmation owned by ``user_id``."""
    params: dict[str, Any] = {"confirmation_hash": confirmation_hash, "user_id": user_id}
    if decrypt_tokens:
        params["enc_key"] = get_encryption_key()
        token_cols = (
            ", CASE WHEN access_token IS NULL THEN NULL "
            "       ELSE pgp_sym_decrypt(access_token, %(enc_key)s) END AS decrypted_access_token"
            ", CASE WHEN refresh_token IS NULL THEN NULL "
            "       ELSE pgp_sym_decrypt(refresh_token, %(enc_key)s) END AS decrypted_refresh_token"
        )
    else:
        token_cols = ""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT
                    confirmation_hash, user_id, provider, service,
                    oauth_client_id, provider_account_id, provider_union_id,
                    provider_email, granted_scopes,
                    expires_at, intent,
                    expected_old_oauth_client_id,
                    expected_old_provider_account_id,
                    expected_old_auth_epoch,
                    preferences, confirm_expires_at, created_at
                    {token_cols}
                FROM dalton_user_integration_pending_confirmations
                WHERE confirmation_hash = %(confirmation_hash)s
                  AND user_id = %(user_id)s
                  AND confirm_expires_at > clock_timestamp()
                """,
                params,
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
        access_token=r.get("decrypted_access_token") if decrypt_tokens else None,
        refresh_token=r.get("decrypted_refresh_token") if decrypt_tokens else None,
        expected_old_oauth_client_id=r.get("expected_old_oauth_client_id"),
        expected_old_provider_account_id=r.get("expected_old_provider_account_id"),
        expected_old_auth_epoch=(
            int(r["expected_old_auth_epoch"])
            if r.get("expected_old_auth_epoch") is not None
            else None
        ),
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
    """Delete expired OAuth state and pending confirmation rows, queueing unactivated Google tokens for revocation.

    Serializes Google confirmation expiry with disconnect/activation using
    ``pg_advisory_xact_lock(revoke_lock_key(google_project, sub))`` on the same connection
    before deleting and checking whether the owner still holds an active/pending grant.

    Returns ``(states_deleted, confirms_deleted)``.
    """
    async def _run(db_conn: Any) -> tuple[int, int]:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                DELETE FROM dalton_user_integration_oauth_states
                WHERE (consumed_at IS NULL AND expires_at <= clock_timestamp())
                   OR (
                       consumed_at IS NOT NULL
                       AND COALESCE(exchange_expires_at, expires_at) <= clock_timestamp()
                   )
                   OR (voided_at IS NOT NULL AND voided_at <= clock_timestamp() - INTERVAL '5 minutes')
                """
            )
            states_deleted = int(cur.rowcount or 0)

            # Snapshot ONE cutoff timestamp shared by pre-scan and DELETE so no Google
            # confirmation expiring between pre-scan and DELETE can ever be deleted
            # without first holding its per-(project, sub) revoke_lock_key.
            await cur.execute("SELECT clock_timestamp() AS cutoff")
            cutoff_row = await cur.fetchone()
            cutoff = cutoff_row["cutoff"] if cutoff_row else None

            await cur.execute(
                """
                SELECT confirmation_hash, provider, oauth_client_id, provider_account_id
                FROM dalton_user_integration_pending_confirmations
                WHERE confirm_expires_at <= %s
                """,
                (cutoff,),
            )
            pre_expired = await cur.fetchall()
            target_hashes = [str(r["confirmation_hash"]) for r in pre_expired]
            expired_confirms: list[dict[str, Any]] = []
            if target_hashes:
                lock_keys = sorted(
                    {
                        revoke_lock_key(
                            extract_google_project(str(r["oauth_client_id"])),
                            str(r["provider_account_id"]),
                        )
                        for r in pre_expired
                        if str(r.get("provider") or "") == "google"
                    }
                )
                for rk in lock_keys:
                    await cur.execute("SELECT pg_advisory_xact_lock(%s)", (rk,))

                await cur.execute(
                    """
                    DELETE FROM dalton_user_integration_pending_confirmations
                    WHERE confirmation_hash = ANY(%s::text[])
                      AND confirm_expires_at <= %s
                    RETURNING
                        user_id, provider, service, oauth_client_id, provider_account_id,
                        access_token, refresh_token, preferences
                    """,
                    (target_hashes, cutoff),
                )
                expired_confirms = [dict(r) for r in await cur.fetchall()]
        confirms_deleted = len(expired_confirms)

        # Enqueue unactivated expired Google tokens into the remote revoke ledger
        # (deduplicated per (google_project, sub)) when the owner holds no other active/pending grant.
        seen_proj_sub: set[tuple[str, str]] = set()
        for r in expired_confirms:
            if str(r.get("provider") or "") != "google":
                continue
            raw_tok = (
                bytes(r["refresh_token"])
                if r.get("refresh_token") is not None
                else (
                    bytes(r["access_token"])
                    if r.get("access_token") is not None
                    else None
                )
            )
            if raw_tok is None:
                continue
            cid = str(r["oauth_client_id"])
            sub = str(r["provider_account_id"])
            uid = str(r["user_id"])
            proj = extract_google_project(cid)
            if (proj, sub) in seen_proj_sub:
                continue
            if await has_active_or_pending_google_grant(
                proj, sub, user_id=uid, conn=db_conn
            ):
                continue
            seen_proj_sub.add((proj, sub))
            await insert_purge_ledger(
                purge_id=str(uuid4()),
                user_id=uid,
                provider="google",
                service=str(r["service"]) if r.get("service") else None,
                oauth_client_id=cid,
                provider_account_id=sub,
                auth_epoch_upto=0,
                trigger_reason="pending_confirmation_expired",
                local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                purged_max_turn_by_thread={},
                remote_revoke_status=RemoteRevokeStatus.PENDING,
                encrypted_revoke_token=raw_tok,
                conn=db_conn,
            )
        return states_deleted, confirms_deleted

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)


async def get_channel_feishu_binding(
    user_id: str,
    *,
    conn: Any = None,
) -> tuple[str | None, str | None]:
    """Look up ``(open_id, feishu_app_id)`` for ``user_id``'s bound Feishu channel identity (§4.3 item 2).

    Returns ``(None, None)`` when no binding or channel table exists, and ``(open_id, None)``
    for legacy bindings created before ``feishu_app_id`` was tracked.
    """
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT platform_user_id, feishu_app_id
                    FROM dalton_channel_identities
                    WHERE user_id = %s
                      AND platform = 'feishu'
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
        if not row or not row[0]:
            return None, None
        open_id = str(row[0]).strip() or None
        app_id = str(row[1]).strip() if len(row) > 1 and row[1] else None
        return open_id, app_id or None
    except Exception:
        # Fallback for schemas without feishu_app_id column or without channel_gateway tables
        try:
            open_id = await get_channel_feishu_open_id(user_id, conn=conn)
            return open_id, None
        except Exception:
            return None, None


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
    for_update: bool = False,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Read the singleton 12-hour health sweep coordination row."""
    lock_clause = " FOR UPDATE" if for_update else ""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT sweep_gen, inflight_until,
                       last_started_at, last_completed_at, last_worker_id,
                       clock_timestamp() AS db_now
                FROM dalton_integration_health_sweep_state
                WHERE singleton = TRUE
                {lock_clause}
                """
            )
            row = await cur.fetchone()
    return dict(row) if row is not None else None


async def update_health_sweep_state(
    *,
    started: bool = False,
    completed: bool = False,
    worker_id: str | None = None,
    claim_lease_seconds: int | None = None,
    renew_lease_seconds: int | None = None,
    expected_sweep_gen: int | None = None,
    clear_inflight: bool = False,
    conn: Any = None,
) -> int | None:
    """Update health sweep coordination state, optionally claiming, renewing, or finalizing a monotonic ``sweep_gen`` lease."""
    sets = ["updated_at = NOW()"]
    params: dict[str, Any] = {}
    if started:
        sets.append("last_started_at = NOW()")
    if completed:
        sets.append("last_completed_at = NOW()")
        sets.append("inflight_until = NULL")
    if clear_inflight and not completed:
        sets.append("inflight_until = NULL")
    if claim_lease_seconds is not None:
        sets.append("sweep_gen = sweep_gen + 1")
        sets.append("inflight_until = clock_timestamp() + make_interval(secs => %(lease_s)s)")
        params["lease_s"] = int(claim_lease_seconds)
    elif renew_lease_seconds is not None:
        sets.append("inflight_until = clock_timestamp() + make_interval(secs => %(renew_s)s)")
        params["renew_s"] = int(renew_lease_seconds)
    if worker_id is not None:
        sets.append("last_worker_id = %(worker_id)s")
        params["worker_id"] = worker_id

    where_parts: list[str] = ["singleton = TRUE"]
    if expected_sweep_gen is not None:
        where_parts.append("sweep_gen = %(expected_sweep_gen)s")
        params["expected_sweep_gen"] = int(expected_sweep_gen)
    if renew_lease_seconds is not None or (completed and expected_sweep_gen is not None):
        where_parts.append("inflight_until IS NOT NULL")
        where_parts.append("inflight_until > clock_timestamp()")

    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_integration_health_sweep_state (singleton)
                VALUES (TRUE)
                ON CONFLICT DO NOTHING
                """
            )
            await cur.execute(
                f"""
                UPDATE dalton_integration_health_sweep_state
                SET {', '.join(sets)}
                WHERE {' AND '.join(where_parts)}
                RETURNING sweep_gen
                """,
                params,
            )
            row = await cur.fetchone()
    return int(row[0]) if row is not None else None


async def renew_health_sweep_lease(
    *,
    expected_sweep_gen: int,
    worker_id: str,
    lease_seconds: int,
    conn: Any = None,
) -> bool:
    """Extend ``inflight_until`` iff ``sweep_gen`` still matches and the lease has not expired."""
    renewed = await update_health_sweep_state(
        worker_id=worker_id,
        renew_lease_seconds=lease_seconds,
        expected_sweep_gen=expected_sweep_gen,
        conn=conn,
    )
    return renewed is not None and renewed == int(expected_sweep_gen)


async def list_orphaned_revoked_purging_connections(
    *,
    limit: int = 500,
    conn: Any = None,
) -> list[IntegrationRow]:
    """List ``revoked_purging`` rows that lack any ``running`` local purge ledger (§4.2(2))."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_INTEGRATION_META_COLS}
                FROM dalton_user_integrations i
                WHERE i.status = 'revoked_purging'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM dalton_integration_purge_ledger l
                      WHERE l.user_id = i.user_id
                        AND l.provider = i.provider
                        AND (l.service = i.service OR l.service IS NULL)
                        AND l.oauth_client_id = i.oauth_client_id
                        AND l.provider_account_id = i.provider_account_id
                        AND l.auth_epoch_upto >= i.auth_epoch
                        AND l.local_purge_status = 'running'
                  )
                ORDER BY i.updated_at ASC, i.user_id ASC, i.provider ASC, i.service ASC
                LIMIT %s
                """,
                (int(limit),),
            )
            rows = await cur.fetchall()
    return [_row_to_integration(dict(r)) for r in rows]


async def has_other_user_google_account_claim(
    google_project: str,
    sub: str,
    *,
    exclude_user_id: str,
    conn: Any = None,
) -> bool:
    """Return True if another Dalton member already owns ``(google_project, sub)`` via any connection or unexpired pending confirmation.

    Enforces the 1-Google-account-per-Dalton-member invariant at callback entry and
    inside staging/activation transactions under ``revoke_lock_key(google_project, sub)``.
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT oauth_client_id
                FROM dalton_user_integrations
                WHERE provider = 'google'
                  AND provider_account_id = %(sub)s
                  AND user_id <> %(exclude_user_id)s
                """,
                {"sub": sub, "exclude_user_id": exclude_user_id},
            )
            conn_rows = await cur.fetchall()
            if any(
                extract_google_project(str(r["oauth_client_id"])) == google_project
                for r in conn_rows
            ):
                return True

            await cur.execute(
                """
                SELECT oauth_client_id
                FROM dalton_user_integration_pending_confirmations
                WHERE provider = 'google'
                  AND provider_account_id = %(sub)s
                  AND user_id <> %(exclude_user_id)s
                  AND confirm_expires_at > clock_timestamp()
                """,
                {"sub": sub, "exclude_user_id": exclude_user_id},
            )
            pending_rows = await cur.fetchall()
            return any(
                extract_google_project(str(r["oauth_client_id"])) == google_project
                for r in pending_rows
            )


async def has_any_active_google_grant_in_project(
    google_project: str,
    *,
    conn: Any = None,
) -> bool:
    """Return True if any active connection or unexpired pending confirmation exists in ``google_project``."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT oauth_client_id
                FROM dalton_user_integrations
                WHERE provider = 'google'
                  AND status IN ('connected', 'refresh_ambiguous')
                """
            )
            if any(
                extract_google_project(str(r["oauth_client_id"])) == google_project
                for r in await cur.fetchall()
            ):
                return True
            await cur.execute(
                """
                SELECT oauth_client_id
                FROM dalton_user_integration_pending_confirmations
                WHERE provider = 'google'
                  AND confirm_expires_at > clock_timestamp()
                """
            )
            return any(
                extract_google_project(str(r["oauth_client_id"])) == google_project
                for r in await cur.fetchall()
            )


async def has_active_or_pending_google_grant(
    google_project: str,
    sub: str,
    *,
    user_id: str | None = None,
    exclude_user_id: str | None = None,
    exclude_client_id: str | None = None,
    exclude_service: str | None = None,
    exclude_confirmation_hash: str | None = None,
    min_auth_epoch: int | None = None,
    conn: Any = None,
) -> bool:
    """Return True if ``(google_project, sub)`` holds an active connection or unexpired pending confirmation.

    Because one Google ``(project, sub)`` connects to at most one Dalton member, callers
    scope checks to ``user_id`` for same-member multi-service (calendar + mail) shared
    credentials.
    """
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            conn_clauses = [
                "provider = 'google'",
                "provider_account_id = %(sub)s",
                "status IN ('connected', 'refresh_ambiguous')",
            ]
            conn_params: dict[str, Any] = {"sub": sub}
            if user_id is not None:
                conn_clauses.append("user_id = %(user_id)s")
                conn_params["user_id"] = user_id
                if exclude_service is not None and exclude_client_id is not None:
                    conn_clauses.append(
                        "NOT (service = %(ex_svc)s AND oauth_client_id = %(ex_cid)s)"
                    )
                    conn_params["ex_svc"] = exclude_service
                    conn_params["ex_cid"] = exclude_client_id
                elif exclude_service is not None:
                    conn_clauses.append("service <> %(ex_svc)s")
                    conn_params["ex_svc"] = exclude_service
                elif exclude_client_id is not None:
                    conn_clauses.append("oauth_client_id <> %(ex_cid)s")
                    conn_params["ex_cid"] = exclude_client_id
            elif exclude_user_id is not None and exclude_service is not None:
                if exclude_client_id is not None:
                    conn_clauses.append(
                        "NOT (user_id = %(ex_uid)s AND service = %(ex_svc)s AND oauth_client_id = %(ex_cid)s)"
                    )
                    conn_params["ex_cid"] = exclude_client_id
                else:
                    conn_clauses.append(
                        "NOT (user_id = %(ex_uid)s AND service = %(ex_svc)s)"
                    )
                conn_params["ex_uid"] = exclude_user_id
                conn_params["ex_svc"] = exclude_service
            elif exclude_user_id is not None and exclude_client_id is not None:
                conn_clauses.append(
                    "NOT (user_id = %(ex_uid)s AND oauth_client_id = %(ex_cid)s)"
                )
                conn_params["ex_uid"] = exclude_user_id
                conn_params["ex_cid"] = exclude_client_id
            elif exclude_user_id is not None:
                conn_clauses.append("user_id <> %(ex_uid)s")
                conn_params["ex_uid"] = exclude_user_id
            elif exclude_client_id is not None:
                conn_clauses.append("oauth_client_id <> %(ex_cid)s")
                conn_params["ex_cid"] = exclude_client_id

            if min_auth_epoch is not None:
                conn_clauses.append("auth_epoch > %(min_auth_epoch)s")
                conn_params["min_auth_epoch"] = int(min_auth_epoch)

            await cur.execute(
                f"""
                SELECT oauth_client_id
                FROM dalton_user_integrations
                WHERE {' AND '.join(conn_clauses)}
                """,
                conn_params,
            )
            conn_rows = await cur.fetchall()
            if any(
                extract_google_project(str(r["oauth_client_id"])) == google_project
                for r in conn_rows
            ):
                return True

            pend_clauses = [
                "provider = 'google'",
                "provider_account_id = %(sub)s",
                "confirm_expires_at > clock_timestamp()",
            ]
            pend_params: dict[str, Any] = {"sub": sub}
            if exclude_confirmation_hash is not None:
                pend_clauses.append("confirmation_hash <> %(ex_chash)s")
                pend_params["ex_chash"] = exclude_confirmation_hash
            if user_id is not None:
                pend_clauses.append("user_id = %(user_id)s")
                pend_params["user_id"] = user_id
                if exclude_service is not None and exclude_client_id is not None:
                    pend_clauses.append(
                        "NOT (service = %(ex_svc)s AND oauth_client_id = %(ex_cid)s)"
                    )
                    pend_params["ex_svc"] = exclude_service
                    pend_params["ex_cid"] = exclude_client_id
                elif exclude_service is not None:
                    pend_clauses.append("service <> %(ex_svc)s")
                    pend_params["ex_svc"] = exclude_service
                elif exclude_client_id is not None:
                    pend_clauses.append("oauth_client_id <> %(ex_cid)s")
                    pend_params["ex_cid"] = exclude_client_id
            elif exclude_user_id is not None and exclude_service is not None:
                if exclude_client_id is not None:
                    pend_clauses.append(
                        "NOT (user_id = %(ex_uid)s AND service = %(ex_svc)s AND oauth_client_id = %(ex_cid)s)"
                    )
                    pend_params["ex_cid"] = exclude_client_id
                else:
                    pend_clauses.append(
                        "NOT (user_id = %(ex_uid)s AND service = %(ex_svc)s)"
                    )
                pend_params["ex_uid"] = exclude_user_id
                pend_params["ex_svc"] = exclude_service
            elif exclude_user_id is not None and exclude_client_id is not None:
                pend_clauses.append(
                    "NOT (user_id = %(ex_uid)s AND oauth_client_id = %(ex_cid)s)"
                )
                pend_params["ex_uid"] = exclude_user_id
                pend_params["ex_cid"] = exclude_client_id
            elif exclude_user_id is not None:
                pend_clauses.append("user_id <> %(ex_uid)s")
                pend_params["ex_uid"] = exclude_user_id
            elif exclude_client_id is not None:
                pend_clauses.append("oauth_client_id <> %(ex_cid)s")
                pend_params["ex_cid"] = exclude_client_id

            await cur.execute(
                f"""
                SELECT oauth_client_id
                FROM dalton_user_integration_pending_confirmations
                WHERE {' AND '.join(pend_clauses)}
                """,
                pend_params,
            )
            pending_rows = await cur.fetchall()
            return any(
                extract_google_project(str(r["oauth_client_id"])) == google_project
                for r in pending_rows
            )


async def supersede_active_revokes_for_google_project_sub_tx(
    google_project: str,
    sub: str,
    *,
    reason: str = "superseded_by_new_authorization",
    conn: Any,
) -> int:
    """Supersede all ``pending | failed_retryable`` Google revoke ledgers for ``(google_project, sub)`` across all users inside an open transaction.

    Caller MUST hold ``revoke_lock_key(google_project, sub)``.
    """
    active = await list_active_revokes_for_google_project_sub(
        google_project, sub, decrypt_revoke_token=False, conn=conn
    )
    count = 0
    for row in active:
        updated = await update_purge_ledger_remote_status(
            row.purge_id,
            remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
            clear_revoke_token=True,
            next_retry_at=None,
            last_remote_error=reason,
            mark_remote_completed=True,
            conn=conn,
        )
        if updated is not None:
            count += 1
    return count


async def upsert_channel_gateway_runtime(
    *,
    instance_id: str,
    lease_epoch: int,
    feishu_app_id: str | None,
    expires_at: datetime | str,
    conn: Any = None,
) -> None:
    """Record the running channel gateway's active Feishu App ID while it holds the deployment lease."""
    cleaned_app_id = str(feishu_app_id).strip() if isinstance(feishu_app_id, str) and feishu_app_id.strip() else None
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO dalton_integration_gateway_runtime (
                    singleton, instance_id, lease_epoch, feishu_app_id, expires_at, updated_at
                )
                VALUES (TRUE, %s, %s, %s, %s::timestamptz, NOW())
                ON CONFLICT (singleton) DO UPDATE
                SET instance_id = EXCLUDED.instance_id,
                    lease_epoch = EXCLUDED.lease_epoch,
                    feishu_app_id = EXCLUDED.feishu_app_id,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                WHERE EXCLUDED.lease_epoch >= dalton_integration_gateway_runtime.lease_epoch
                """,
                (instance_id, int(lease_epoch), cleaned_app_id, expires_at),
            )


async def clear_channel_gateway_runtime(
    *,
    instance_id: str,
    lease_epoch: int,
    conn: Any = None,
) -> None:
    """Clear the reported channel gateway runtime row on voluntary lease release."""
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM dalton_integration_gateway_runtime
                WHERE singleton = TRUE
                  AND instance_id = %s
                  AND lease_epoch = %s
                """,
                (instance_id, int(lease_epoch)),
            )


async def get_active_channel_gateway_feishu_app_id(
    *,
    conn: Any = None,
) -> tuple[bool, str | None]:
    """Return ``(has_live_gateway, feishu_app_id)`` from the active unexpired gateway runtime row.

    Returns ``(False, None)`` when no live gateway runtime record exists or the table is absent.
    """
    try:
        async with get_db_connection(conn) as db_conn:
            async with db_conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT feishu_app_id
                    FROM dalton_integration_gateway_runtime
                    WHERE singleton = TRUE
                      AND expires_at > clock_timestamp()
                    """
                )
                row = await cur.fetchone()
        if row is None:
            return False, None
        val = str(row[0]).strip() if row[0] else None
        return True, val or None
    except Exception:
        return False, None


# ---------------------------------------------------------------------------
# Step 3 additive CRUD helpers for dalton_integration_action_approvals (§5.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionApprovalRow:
    approval_id: str
    caller_user_id: str
    thread_id: str
    run_id: str | None
    interrupt_id: str | None
    action_type: str
    provider: str
    service: str
    provider_account_id: str
    auth_epoch: int
    token_generation: int
    payload_sha256: str
    input_requests_map: dict[str, Any] | None
    idempotent_message_id: str | None
    status: str
    expires_at: datetime
    consumed_at: datetime | None
    created_at: datetime
    has_elicitation_state: bool = False
    elicitation_state: dict[str, Any] | None = field(
        default=None, repr=False, compare=False
    )


def _row_to_action_approval(r: dict[str, Any]) -> ActionApprovalRow:
    import json as _json

    raw_state = r.get("decrypted_elicitation_state")
    parsed_state: dict[str, Any] | None = None
    if isinstance(raw_state, str) and raw_state:
        try:
            loaded = _json.loads(raw_state)
            if isinstance(loaded, dict):
                parsed_state = loaded
        except ValueError:
            parsed_state = None
    elif isinstance(raw_state, dict):
        parsed_state = dict(raw_state)

    raw_map = r.get("input_requests_map")
    input_map = dict(raw_map) if isinstance(raw_map, dict) else None
    return ActionApprovalRow(
        approval_id=str(r["approval_id"]),
        caller_user_id=str(r["caller_user_id"]),
        thread_id=str(r["thread_id"]),
        run_id=str(r["run_id"]) if r.get("run_id") is not None else None,
        interrupt_id=str(r["interrupt_id"]) if r.get("interrupt_id") is not None else None,
        action_type=str(r["action_type"]),
        provider=str(r["provider"]),
        service=str(r["service"]),
        provider_account_id=str(r["provider_account_id"]),
        auth_epoch=int(r["auth_epoch"]),
        token_generation=int(r["token_generation"]),
        payload_sha256=str(r["payload_sha256"]),
        input_requests_map=input_map,
        idempotent_message_id=(
            str(r["idempotent_message_id"])
            if r.get("idempotent_message_id") is not None
            else None
        ),
        status=str(r["status"]),
        expires_at=r["expires_at"],
        consumed_at=r.get("consumed_at"),
        created_at=r["created_at"],
        has_elicitation_state=bool(r.get("has_elicitation_state", parsed_state is not None)),
        elicitation_state=parsed_state,
    )


async def create_action_approval(
    *,
    caller_user_id: str,
    thread_id: str | UUID,
    run_id: str | UUID | None,
    action_type: str,
    provider: str,
    service: str,
    provider_account_id: str,
    auth_epoch: int,
    token_generation: int,
    payload_sha256: str,
    expires_at: datetime,
    approval_id: str | UUID | None = None,
    interrupt_id: str | None = None,
    input_requests_map: dict[str, Any] | None = None,
    elicitation_state: dict[str, Any] | None = None,
    idempotent_message_id: str | None = None,
    conn: Any = None,
) -> ActionApprovalRow:
    """Persist a one-time write or Google MCP elicitation approval row (§5.2)."""
    import json as _json

    aid = str(approval_id or uuid4())
    tid = str(thread_id)
    rid = str(run_id) if run_id else None
    enc_key = get_encryption_key() if elicitation_state is not None else None
    state_json = (
        _json.dumps(elicitation_state, ensure_ascii=False, separators=(",", ":"))
        if elicitation_state is not None
        else None
    )

    async def _run(db_conn: Any) -> ActionApprovalRow:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            if state_json is not None and enc_key is not None:
                await cur.execute(
                    """
                    INSERT INTO dalton_integration_action_approvals (
                        approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                        action_type, provider, service, provider_account_id,
                        auth_epoch, token_generation, payload_sha256,
                        input_requests_map, encrypted_elicitation_state,
                        idempotent_message_id, status, expires_at
                    )
                    VALUES (
                        %s::uuid, %s, %s::uuid, %s::uuid, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, pgp_sym_encrypt(%s, %s),
                        %s, 'pending', %s
                    )
                    RETURNING approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                              action_type, provider, service, provider_account_id,
                              auth_epoch, token_generation, payload_sha256,
                              input_requests_map, idempotent_message_id, status,
                              expires_at, consumed_at, created_at,
                              (encrypted_elicitation_state IS NOT NULL) AS has_elicitation_state
                    """,
                    (
                        aid,
                        caller_user_id,
                        tid,
                        rid,
                        interrupt_id,
                        action_type,
                        provider,
                        service,
                        provider_account_id,
                        int(auth_epoch),
                        int(token_generation),
                        payload_sha256,
                        Jsonb(input_requests_map) if input_requests_map is not None else None,
                        state_json,
                        enc_key,
                        idempotent_message_id,
                        expires_at,
                    ),
                )
            else:
                await cur.execute(
                    """
                    INSERT INTO dalton_integration_action_approvals (
                        approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                        action_type, provider, service, provider_account_id,
                        auth_epoch, token_generation, payload_sha256,
                        input_requests_map, encrypted_elicitation_state,
                        idempotent_message_id, status, expires_at
                    )
                    VALUES (
                        %s::uuid, %s, %s::uuid, %s::uuid, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, NULL,
                        %s, 'pending', %s
                    )
                    RETURNING approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                              action_type, provider, service, provider_account_id,
                              auth_epoch, token_generation, payload_sha256,
                              input_requests_map, idempotent_message_id, status,
                              expires_at, consumed_at, created_at,
                              (encrypted_elicitation_state IS NOT NULL) AS has_elicitation_state
                    """,
                    (
                        aid,
                        caller_user_id,
                        tid,
                        rid,
                        interrupt_id,
                        action_type,
                        provider,
                        service,
                        provider_account_id,
                        int(auth_epoch),
                        int(token_generation),
                        payload_sha256,
                        Jsonb(input_requests_map) if input_requests_map is not None else None,
                        idempotent_message_id,
                        expires_at,
                    ),
                )
            row = await cur.fetchone()
            assert row is not None
            return _row_to_action_approval(dict(row))

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)


async def get_action_approval(
    approval_id: str | UUID,
    *,
    decrypt_state: bool = False,
    conn: Any = None,
) -> ActionApprovalRow | None:
    """Fetch one approval row by ``approval_id`` (optionally decrypting ``elicitation_state``)."""
    aid = str(approval_id)
    enc_key = get_encryption_key() if decrypt_state else None
    async with get_db_connection(conn) as db_conn:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            if decrypt_state and enc_key is not None:
                await cur.execute(
                    """
                    SELECT approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                           action_type, provider, service, provider_account_id,
                           auth_epoch, token_generation, payload_sha256,
                           input_requests_map, idempotent_message_id, status,
                           expires_at, consumed_at, created_at,
                           (encrypted_elicitation_state IS NOT NULL) AS has_elicitation_state,
                           CASE WHEN encrypted_elicitation_state IS NOT NULL
                                THEN pgp_sym_decrypt(encrypted_elicitation_state, %s)
                                ELSE NULL END AS decrypted_elicitation_state
                    FROM dalton_integration_action_approvals
                    WHERE approval_id = %s::uuid
                    """,
                    (enc_key, aid),
                )
            else:
                await cur.execute(
                    """
                    SELECT approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                           action_type, provider, service, provider_account_id,
                           auth_epoch, token_generation, payload_sha256,
                           input_requests_map, idempotent_message_id, status,
                           expires_at, consumed_at, created_at,
                           (encrypted_elicitation_state IS NOT NULL) AS has_elicitation_state
                    FROM dalton_integration_action_approvals
                    WHERE approval_id = %s::uuid
                    """,
                    (aid,),
                )
            row = await cur.fetchone()
    return _row_to_action_approval(dict(row)) if row is not None else None


async def consume_action_approval(
    approval_id: str | UUID,
    caller_user_id: str,
    *,
    decrypt_state: bool = False,
    conn: Any = None,
) -> ActionApprovalRow | None:
    """Atomically transition a pending unexpired approval to ``executing`` (one-time use)."""
    aid = str(approval_id)
    enc_key = get_encryption_key() if decrypt_state else None

    async def _run(db_conn: Any) -> ActionApprovalRow | None:
        async with db_conn.cursor(row_factory=dict_row) as cur:
            if decrypt_state and enc_key is not None:
                await cur.execute(
                    """
                    UPDATE dalton_integration_action_approvals
                    SET status = 'executing',
                        consumed_at = NOW()
                    WHERE approval_id = %s::uuid
                      AND caller_user_id = %s
                      AND status = 'pending'
                      AND expires_at > clock_timestamp()
                    RETURNING approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                              action_type, provider, service, provider_account_id,
                              auth_epoch, token_generation, payload_sha256,
                              input_requests_map, idempotent_message_id, status,
                              expires_at, consumed_at, created_at,
                              (encrypted_elicitation_state IS NOT NULL) AS has_elicitation_state,
                              CASE WHEN encrypted_elicitation_state IS NOT NULL
                                   THEN pgp_sym_decrypt(encrypted_elicitation_state, %s)
                                   ELSE NULL END AS decrypted_elicitation_state
                    """,
                    (aid, caller_user_id, enc_key),
                )
            else:
                await cur.execute(
                    """
                    UPDATE dalton_integration_action_approvals
                    SET status = 'executing',
                        consumed_at = NOW()
                    WHERE approval_id = %s::uuid
                      AND caller_user_id = %s
                      AND status = 'pending'
                      AND expires_at > clock_timestamp()
                    RETURNING approval_id, caller_user_id, thread_id, run_id, interrupt_id,
                              action_type, provider, service, provider_account_id,
                              auth_epoch, token_generation, payload_sha256,
                              input_requests_map, idempotent_message_id, status,
                              expires_at, consumed_at, created_at,
                              (encrypted_elicitation_state IS NOT NULL) AS has_elicitation_state
                    """,
                    (aid, caller_user_id),
                )
            row = await cur.fetchone()
        return _row_to_action_approval(dict(row)) if row is not None else None

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)


async def mark_action_approval_status(
    approval_id: str | UUID,
    status: str,
    *,
    caller_user_id: str | None = None,
    conn: Any = None,
) -> bool:
    """Update status on an approval row (e.g., ``succeeded``, ``rejected``, ``failed``, ``expired``)."""
    aid = str(approval_id)

    async def _run(db_conn: Any) -> bool:
        async with db_conn.cursor() as cur:
            if caller_user_id is not None:
                await cur.execute(
                    """
                    UPDATE dalton_integration_action_approvals
                    SET status = %s
                    WHERE approval_id = %s::uuid
                      AND caller_user_id = %s
                    """,
                    (status, aid, caller_user_id),
                )
            else:
                await cur.execute(
                    """
                    UPDATE dalton_integration_action_approvals
                    SET status = %s
                    WHERE approval_id = %s::uuid
                    """,
                    (status, aid),
                )
            return bool(cur.rowcount)

    if conn is not None:
        return await _run(conn)
    async with get_db_connection() as db_conn, db_conn.transaction():
        return await _run(db_conn)
