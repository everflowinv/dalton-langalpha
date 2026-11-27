"""Personal integration disconnect Phase 1 registration, account-switch replacement, and Phase 2 finalization.

Follows unidirectional lock ordering (§4.2(2)(3)):
  1. Lifecycle lock: ``advisory_key('dalton_integration_user_lifecycle', user_id, provider)``
  2. Refresh lock:   ``advisory_key('dalton_integration_refresh', ...)``
  3. Revoke lock:    ``advisory_key('dalton_integration_revoke', user_id, google_project, sub)``
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any
from uuid import uuid4

import anyio
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationStatus,
    LocalPurgeStatus,
    RemoteRevokeStatus,
    TriggerReason,
    lifecycle_lock_key,
    refresh_lock_key,
)
from src.dalton.services.integration_revoke import RemoteRevoker, process_revoke
from src.dalton.services.purge_fence import merge_turn_range
from src.server.database.encryption import get_encryption_key
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


class IntegrationNotFoundError(LookupError):
    """Target integration connection does not exist."""


class MissingRefreshTokenOnAccountSwitchError(ValueError):
    """Account switch callback omitted a new refresh_token (original row kept intact)."""

    code = "missing_refresh_token_on_account_switch"


class DuplicateWebhookEventError(RuntimeError):
    """Webhook event_id was already processed."""


@dataclass(frozen=True, slots=True)
class AccountSwitchResult:
    purge_id: str
    old_auth_epoch: int
    new_auth_epoch: int
    remote_revoke_status: RemoteRevokeStatus
    affected_threads: dict[str, dict[str, int]]


@dataclass(frozen=True, slots=True)
class FinishDisconnectResult:
    purge_id: str
    deleted_connections: int
    local_purge_status: LocalPurgeStatus
    remote_revoke_status: RemoteRevokeStatus


async def _lock_and_register_phase1_tx(
    conn: Any,
    *,
    user_id: str,
    provider: str,
    service: str | None,
    oauth_client_id: str,
    provider_account_id: str,
    auth_epoch_upto: int,
    trigger_reason: str,
    remote_revoke_status: RemoteRevokeStatus,
    encrypted_revoke_token: bytes | None,
) -> tuple[str, dict[str, dict[str, int]]]:
    """Shared Phase 1 registration inside the caller's open transaction.

    1. Locks every affected private thread (having unpurged touch rows for
       ``(user_id, provider[, service])`` with ``auth_epoch <= auth_epoch_upto``)
       with ``SELECT ... FOR UPDATE`` in deterministic ``conversation_thread_id`` order.
    2. Computes ``[first_touched_turn_index, purge_start_max_turn_index]`` per thread,
       sets ``personal_purge_in_progress = TRUE``, increments ``personal_purge_generation``
       (first bump), and merges the closed interval into ``personal_purged_turn_ranges``.
    3. Inserts the ``dalton_integration_purge_ledger`` row (``local_purge_status = 'running'``).
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        service_filter = "AND service = %(service)s" if service is not None else ""
        await cur.execute(
            f"""
            SELECT thread_id::text AS thread_id,
                   MIN(turn_index) AS first_touched_turn_index,
                   MAX(turn_index) AS max_touched_turn_index
            FROM dalton_thread_integration_touches
            WHERE user_id = %(user_id)s
              AND provider = %(provider)s
              {service_filter}
              AND auth_epoch <= %(auth_epoch_upto)s
              AND purged_at IS NULL
            GROUP BY thread_id
            ORDER BY thread_id ASC
            """,
            {
                "user_id": user_id,
                "provider": provider,
                "service": service,
                "auth_epoch_upto": int(auth_epoch_upto),
            },
        )
        touch_groups = [dict(r) for r in await cur.fetchall()]

        purged_ranges_by_thread: dict[str, dict[str, int]] = {}
        for grp in touch_groups:
            tid = str(grp["thread_id"])
            first_turn = int(grp["first_touched_turn_index"])
            max_touch_turn = int(grp["max_touched_turn_index"])

            await cur.execute(
                """
                SELECT personal_purged_turn_ranges
                FROM conversation_threads
                WHERE conversation_thread_id = %s::uuid
                FOR UPDATE
                """,
                (tid,),
            )
            t_row = await cur.fetchone()
            if t_row is None:
                continue

            await cur.execute(
                """
                SELECT GREATEST(
                    COALESCE((SELECT MAX(turn_index) FROM conversation_queries WHERE conversation_thread_id = %s::uuid), -1),
                    COALESCE((SELECT MAX(turn_index) FROM conversation_responses WHERE conversation_thread_id = %s::uuid), -1),
                    %s
                ) AS max_turn
                """,
                (tid, tid, max_touch_turn),
            )
            max_row = await cur.fetchone()
            purge_start_max = max(first_turn, int(max_row["max_turn"]) if max_row else first_turn)

            merged = merge_turn_range(
                t_row.get("personal_purged_turn_ranges"),
                first_turn,
                purge_start_max,
            )
            await cur.execute(
                """
                UPDATE conversation_threads
                SET personal_purge_in_progress = TRUE,
                    personal_purge_generation = personal_purge_generation + 1,
                    personal_purged_turn_ranges = %s,
                    updated_at = NOW()
                WHERE conversation_thread_id = %s::uuid
                """,
                (Jsonb(merged), tid),
            )
            purged_ranges_by_thread[tid] = {
                "min_turn": first_turn,
                "max_turn": purge_start_max,
            }

    purge_id = str(uuid4())
    await db.insert_purge_ledger(
        purge_id=purge_id,
        user_id=user_id,
        provider=provider,
        service=service,
        oauth_client_id=oauth_client_id,
        provider_account_id=provider_account_id,
        auth_epoch_upto=auth_epoch_upto,
        trigger_reason=trigger_reason,
        local_purge_status=LocalPurgeStatus.RUNNING,
        purged_max_turn_by_thread=purged_ranges_by_thread,
        remote_revoke_status=remote_revoke_status,
        encrypted_revoke_token=encrypted_revoke_token,
        conn=conn,
    )
    return purge_id, purged_ranges_by_thread


def _determine_remote_revoke(
    *,
    provider: str,
    trigger_reason: str,
    remote_mode: str,
    has_remaining_active_sibling: bool,
    raw_refresh_token: bytes | None,
    raw_access_token: bytes | None,
) -> tuple[RemoteRevokeStatus, bytes | None]:
    """Decide initial ``remote_revoke_status`` and ``encrypted_revoke_token`` (§4.2(3))."""
    if provider != "google":
        return RemoteRevokeStatus.NOT_APPLICABLE, None
    if trigger_reason in (TriggerReason.WEBHOOK_REVOKED, TriggerReason.ERROR_CODE_REVOKED):
        return RemoteRevokeStatus.NOT_APPLICABLE, None
    if remote_mode == "local_only":
        return RemoteRevokeStatus.NOT_APPLICABLE, None
    if has_remaining_active_sibling:
        return RemoteRevokeStatus.NOT_APPLICABLE, None
    token_bytes = raw_refresh_token if raw_refresh_token is not None else raw_access_token
    if token_bytes is None:
        return RemoteRevokeStatus.NOT_APPLICABLE, None
    return RemoteRevokeStatus.PENDING, bytes(token_bytes)


async def begin_disconnect(
    user_id: str,
    provider: str,
    service: str | None,
    *,
    trigger_reason: str = TriggerReason.USER_DISCONNECT,
    remote_mode: str = "remote_if_sole",
    event_id: str | None = None,
) -> str:
    """Phase 1 atomic registration transaction (§4.2(3) item 1, §4.5(2) item 1).

    Holds ``advisory_key('dalton_integration_user_lifecycle', user_id, provider)``
    while executing a single short transaction that:
      1. Optionally atomically deduplicates ``(provider, event_id)`` in ``dalton_integration_webhook_events``.
      2. Transitions target connection row(s) to ``disconnect_pending`` (or ``revoked_purging``).
      3. Locks all affected private threads ``FOR UPDATE``, records closed turn ranges,
         sets ``personal_purge_in_progress = TRUE``, and increments ``personal_purge_generation``.
      4. Inserts the ``dalton_integration_purge_ledger`` row (``local_purge_status = 'running'``).
    Commits, releases the lifecycle lock, and returns ``purge_id``.
    """
    is_revocation = trigger_reason in (
        TriggerReason.WEBHOOK_REVOKED,
        TriggerReason.ERROR_CODE_REVOKED,
    )
    target_status = (
        IntegrationStatus.REVOKED_PURGING
        if is_revocation
        else IntegrationStatus.DISCONNECT_PENDING
    )

    lock_key = lifecycle_lock_key(user_id, provider)
    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        try:
            async with get_db_connection() as tx_conn, tx_conn.transaction():
                if event_id is not None:
                    first_seen = await db.record_webhook_event(
                        provider, event_id, conn=tx_conn
                    )
                    if not first_seen:
                        raise DuplicateWebhookEventError(
                            f"Duplicate webhook event {provider}:{event_id}"
                        )

                async with tx_conn.cursor(row_factory=dict_row) as cur:
                    if service is not None:
                        await cur.execute(
                            """
                            SELECT user_id, provider, service, oauth_client_id,
                                   provider_account_id, auth_epoch,
                                   access_token, refresh_token, status
                            FROM dalton_user_integrations
                            WHERE user_id = %s
                              AND provider = %s
                              AND service = %s
                            ORDER BY service ASC
                            FOR UPDATE
                            """,
                            (user_id, provider, service),
                        )
                    else:
                        await cur.execute(
                            """
                            SELECT user_id, provider, service, oauth_client_id,
                                   provider_account_id, auth_epoch,
                                   access_token, refresh_token, status
                            FROM dalton_user_integrations
                            WHERE user_id = %s
                              AND provider = %s
                            ORDER BY service ASC
                            FOR UPDATE
                            """,
                            (user_id, provider),
                        )
                    targets = [dict(r) for r in await cur.fetchall()]
                    if not targets:
                        raise IntegrationNotFoundError(
                            f"No connection found for user={user_id} provider={provider} service={service}"
                        )

                    target_services = [str(r["service"]) for r in targets]
                    await cur.execute(
                        """
                        UPDATE dalton_user_integrations
                        SET status = %s,
                            refresh_inflight_gen = NULL,
                            refresh_started_at = NULL,
                            updated_at = NOW()
                        WHERE user_id = %s
                          AND provider = %s
                          AND service = ANY(%s)
                        """,
                        (str(target_status), user_id, provider, target_services),
                    )

                    primary = targets[0]
                    oauth_client_id = str(primary["oauth_client_id"])
                    provider_account_id = str(primary["provider_account_id"])
                    auth_epoch_upto = max(int(r["auth_epoch"]) for r in targets)

                    # Check remaining active siblings under same (oauth_client_id, provider_account_id)
                    siblings = await db.get_sibling_connections(
                        user_id,
                        provider,
                        oauth_client_id,
                        provider_account_id,
                        active_only=True,
                        conn=tx_conn,
                    )
                    remaining_siblings = [
                        s for s in siblings if s.service not in target_services
                    ]
                    raw_refresh = next(
                        (r["refresh_token"] for r in targets if r.get("refresh_token") is not None),
                        None,
                    )
                    raw_access = next(
                        (r["access_token"] for r in targets if r.get("access_token") is not None),
                        None,
                    )
                    remote_status, enc_revoke_token = _determine_remote_revoke(
                        provider=provider,
                        trigger_reason=trigger_reason,
                        remote_mode=remote_mode,
                        has_remaining_active_sibling=bool(remaining_siblings),
                        raw_refresh_token=raw_refresh,
                        raw_access_token=raw_access,
                    )

                purge_id, _ = await _lock_and_register_phase1_tx(
                    tx_conn,
                    user_id=user_id,
                    provider=provider,
                    service=service,
                    oauth_client_id=oauth_client_id,
                    provider_account_id=provider_account_id,
                    auth_epoch_upto=auth_epoch_upto,
                    trigger_reason=trigger_reason,
                    remote_revoke_status=remote_status,
                    encrypted_revoke_token=enc_revoke_token,
                )
            return purge_id
        finally:
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def replace_connection_for_account_switch(
    user_id: str,
    provider: str,
    service: str,
    *,
    new_oauth_client_id: str,
    new_provider_account_id: str,
    new_granted_scopes: str,
    new_access_token: str,
    new_refresh_token: str | None,
    new_provider_email: str | None = None,
    new_provider_union_id: str | None = None,
    new_expires_at: datetime | None = None,
    new_preferences: dict[str, Any] | None = None,
    remote_mode: str = "remote_if_sole",
) -> AccountSwitchResult:
    """Explicit account switch: block & replace in ONE transaction (§4.2(1), §4.2(3)).

    Reuses Phase 1 to lock affected threads, merge closed ranges, bump generation,
    and register the purge ledger (``trigger_reason='account_switch'``), then
    replaces the connection row IN PLACE with the new account (``status='connected'``,
    ``token_generation=1``, ``authorized_at=NOW()``, fresh ``auth_epoch``, reset
    account-specific preferences). Never deletes the new connection row.

    Raises ``MissingRefreshTokenOnAccountSwitchError`` if ``new_refresh_token`` is absent,
    leaving the existing connection completely untouched.
    """
    if not new_refresh_token:
        raise MissingRefreshTokenOnAccountSwitchError(
            "missing_refresh_token_on_account_switch"
        )

    enc_key = get_encryption_key()
    lock_key = lifecycle_lock_key(user_id, provider)
    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        try:
            async with get_db_connection() as tx_conn, tx_conn.transaction():
                async with tx_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT user_id, provider, service, oauth_client_id,
                               provider_account_id, auth_epoch,
                               access_token, refresh_token, status
                        FROM dalton_user_integrations
                        WHERE user_id = %s
                          AND provider = %s
                          AND service = %s
                        FOR UPDATE
                        """,
                        (user_id, provider, service),
                    )
                    existing = await cur.fetchone()
                    if existing is None:
                        raise IntegrationNotFoundError(
                            f"No connection found for account switch: user={user_id} provider={provider} service={service}"
                        )
                    old_client_id = str(existing["oauth_client_id"])
                    old_sub = str(existing["provider_account_id"])
                    old_auth_epoch = int(existing["auth_epoch"])
                    if old_sub == new_provider_account_id:
                        raise ValueError(
                            "replace_connection_for_account_switch requires a different provider_account_id"
                        )

                    siblings = await db.get_sibling_connections(
                        user_id,
                        provider,
                        old_client_id,
                        old_sub,
                        exclude_service=service,
                        active_only=True,
                        conn=tx_conn,
                    )
                    remote_status, enc_revoke_token = _determine_remote_revoke(
                        provider=provider,
                        trigger_reason=TriggerReason.ACCOUNT_SWITCH,
                        remote_mode=remote_mode,
                        has_remaining_active_sibling=bool(siblings),
                        raw_refresh_token=existing.get("refresh_token"),
                        raw_access_token=existing.get("access_token"),
                    )

                purge_id, affected = await _lock_and_register_phase1_tx(
                    tx_conn,
                    user_id=user_id,
                    provider=provider,
                    service=service,
                    oauth_client_id=old_client_id,
                    provider_account_id=old_sub,
                    auth_epoch_upto=old_auth_epoch,
                    trigger_reason=TriggerReason.ACCOUNT_SWITCH,
                    remote_revoke_status=remote_status,
                    encrypted_revoke_token=enc_revoke_token,
                )

                # In-place replacement of the connection row in the SAME transaction
                async with tx_conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        UPDATE dalton_user_integrations
                        SET oauth_client_id = %(oauth_client_id)s,
                            provider_account_id = %(provider_account_id)s,
                            provider_union_id = %(provider_union_id)s,
                            provider_email = %(provider_email)s,
                            granted_scopes = %(granted_scopes)s,
                            access_token = pgp_sym_encrypt(%(access_token)s, %(enc_key)s),
                            refresh_token = pgp_sym_encrypt(%(refresh_token)s, %(enc_key)s),
                            auth_epoch = nextval('dalton_integration_auth_epoch_seq'),
                            authorized_at = NOW(),
                            token_generation = 1,
                            status = 'connected',
                            refresh_inflight_gen = NULL,
                            refresh_started_at = NULL,
                            expires_at = %(expires_at)s,
                            needs_reauth_at = NULL,
                            last_error_code = NULL,
                            preferences = %(preferences)s,
                            updated_at = NOW()
                        WHERE user_id = %(user_id)s
                          AND provider = %(provider)s
                          AND service = %(service)s
                        RETURNING auth_epoch
                        """,
                        {
                            "oauth_client_id": new_oauth_client_id,
                            "provider_account_id": new_provider_account_id,
                            "provider_union_id": new_provider_union_id,
                            "provider_email": new_provider_email,
                            "granted_scopes": new_granted_scopes,
                            "access_token": new_access_token,
                            "refresh_token": new_refresh_token,
                            "enc_key": enc_key,
                            "expires_at": new_expires_at,
                            "preferences": Jsonb(new_preferences or {}),
                            "user_id": user_id,
                            "provider": provider,
                            "service": service,
                        },
                    )
                    updated_row = await cur.fetchone()
                    assert updated_row is not None
                    new_auth_epoch = int(updated_row["auth_epoch"])

            return AccountSwitchResult(
                purge_id=purge_id,
                old_auth_epoch=old_auth_epoch,
                new_auth_epoch=new_auth_epoch,
                remote_revoke_status=remote_status,
                affected_threads=affected,
            )
        finally:
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def _drain_refresh_locks(
    *,
    user_id: str,
    provider: str,
    service: str | None,
    oauth_client_id: str,
    provider_account_id: str,
) -> None:
    """Acquire and release refresh advisory lock(s) in ascending key order to drain in-flight HTTP refreshes."""
    if provider == "google":
        keys = [
            refresh_lock_key(user_id, "google", oauth_client_id, provider_account_id)
        ]
    else:
        services = [service] if service is not None else ["calendar", "mail"]
        keys = sorted(
            refresh_lock_key(
                user_id, "feishu", oauth_client_id, provider_account_id, svc
            )
            for svc in services
        )

    async with get_db_connection() as conn:
        for key in keys:
            await conn.execute("SELECT pg_advisory_lock(%s)", (key,))
            with anyio.CancelScope(shield=True):
                await conn.execute("SELECT pg_advisory_unlock(%s)", (key,))


async def finish_disconnect(
    purge_id: str,
    *,
    revoker: RemoteRevoker | None = None,
    cancel_drain_timeout_s: float = 15.0,
) -> FinishDisconnectResult:
    """Phase 2 finalization (§4.2(3) item 1):

    1. Drain any in-flight HTTP refresh via ``dalton_integration_refresh`` lock(s).
    2. For active disconnect or revocation only (NOT ``account_switch``), delete
       connection row(s) bounded by ``auth_epoch <= ledger.auth_epoch_upto``.
    3. Run ``run_local_purge(purge_id)`` (which unblocks threads on commit).
    4. If ``remote_revoke_status == 'pending'`` and ``revoker`` is provided,
       hand off to ``process_revoke(purge_id, revoker)``.
    """
    from src.dalton.services.integration_data_purge import run_local_purge

    ledger = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
    if ledger is None:
        raise LookupError(f"Purge ledger {purge_id} not found")

    await _drain_refresh_locks(
        user_id=ledger.user_id,
        provider=ledger.provider,
        service=ledger.service,
        oauth_client_id=ledger.oauth_client_id,
        provider_account_id=ledger.provider_account_id,
    )

    deleted_connections = 0
    if ledger.trigger_reason != TriggerReason.ACCOUNT_SWITCH:
        deleted_connections = await db.delete_connection_upto_epoch(
            ledger.user_id,
            ledger.provider,
            ledger.service,
            ledger.auth_epoch_upto,
        )

    if ledger.local_purge_status == LocalPurgeStatus.RUNNING:
        await run_local_purge(purge_id, cancel_drain_timeout_s=cancel_drain_timeout_s)

    refreshed = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
    assert refreshed is not None
    final_remote_status = refreshed.remote_revoke_status
    if (
        final_remote_status == RemoteRevokeStatus.PENDING
        and revoker is not None
    ):
        final_remote_status = await process_revoke(purge_id, revoker)

    return FinishDisconnectResult(
        purge_id=purge_id,
        deleted_connections=deleted_connections,
        local_purge_status=refreshed.local_purge_status,
        remote_revoke_status=final_remote_status,
    )
