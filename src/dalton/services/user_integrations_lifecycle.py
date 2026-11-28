"""Personal integration disconnect Phase 1 registration, account-switch replacement, and Phase 2 finalization.

Follows unidirectional lock ordering (§4.2(2)(3)):
  1. Lifecycle lock: ``advisory_key('dalton_integration_user_lifecycle', user_id, provider)``
  2. Refresh lock:   ``advisory_key('dalton_integration_refresh', ...)``
  3. Revoke lock:    ``advisory_key('dalton_integration_revoke', user_id, google_project, sub)``
"""

from __future__ import annotations

from contextlib import ExitStack
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationRow,
    IntegrationStatus,
    LocalPurgeStatus,
    RemoteRevokeStatus,
    TriggerReason,
    extract_google_project,
    lifecycle_lock_key,
    refresh_lock_key,
    revoke_lock_key,
    touch_user_lock_key,
)
from src.dalton.integrations.constants import (
    GOOGLE_GMAIL_DRAFT_SCOPES,
    get_connector_spec,
    scope_satisfied,
)
from src.dalton.integrations.lock_guard import track_advisory_lock
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


class AccountSwitchTargetChangedError(RuntimeError):
    """Account-switch confirmation target (client, account, or auth_epoch) changed before Phase 1."""

    code = "account_switch_state_changed"


class DisconnectTargetMismatchError(RuntimeError):
    """Connection identity, epoch, or generation changed before revocation disconnect registration."""

    code = "integration_epoch_revoked"


class GoogleAccountAlreadyConnectedError(RuntimeError):
    """This Google (project, sub) is already connected or pending confirmation for another Dalton member."""

    code = "google_account_already_connected"

    def __init__(
        self,
        message: str = "这个 Google 账号已被团队里另一位成员连接，请换一个账号或请对方先断开",
    ) -> None:
        super().__init__(message)


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
        await cur.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (touch_user_lock_key(user_id),),
        )
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


def _parse_scope_tokens(scope_str: str | None) -> list[str]:
    if not scope_str:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tok in str(scope_str).replace(",", " ").split():
        s = tok.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _format_scope_tokens(scopes: Sequence[str]) -> str:
    return " ".join(sorted({s.strip() for s in scopes if s and s.strip()}))


async def sync_google_sibling_rows_tx(
    tx_conn: Any,
    *,
    user_id: str,
    oauth_client_id: str,
    provider_account_id: str,
    access_token: str,
    refresh_token: str,
    expires_at: datetime | None,
    granted_scopes: str,
    active_siblings: Sequence[IntegrationRow],
) -> None:
    """Update active Google sibling rows on the same ``(oauth_client_id, sub)`` with the new token's ACTUAL scopes.

    Recomputes each sibling's ``status`` (``connected`` vs ``needs_reauth`` with
    ``missing_required_scopes``) and optional preferences against the newly issued
    token's actual granted scopes (never unioning stale sibling scopes), while
    preserving each sibling's own ``auth_epoch`` and ``authorized_at``.
    """
    if not active_siblings:
        return
    enc_key = get_encryption_key()
    actual_granted_list = _parse_scope_tokens(granted_scopes)
    actual_granted_set = set(actual_granted_list)
    actual_granted_str = _format_scope_tokens(actual_granted_list)

    async with tx_conn.cursor() as cur:
        for sib in active_siblings:
            sib_prefs = dict(sib.preferences or {})
            if sib.service == "mail":
                has_draft = any(s in actual_granted_set for s in GOOGLE_GMAIL_DRAFT_SCOPES)
                if not has_draft:
                    sib_prefs["allow_create_draft"] = False

            spec = get_connector_spec("google", sib.service)
            sib_missing = (
                [
                    s
                    for s in spec.min_required_scopes
                    if not scope_satisfied(s, actual_granted_set)
                ]
                if spec is not None
                else ["unsupported_service"]
            )
            if sib_missing:
                sib_status = IntegrationStatus.NEEDS_REAUTH.value
                sib_err_code: str | None = "missing_required_scopes"
            else:
                sib_status = IntegrationStatus.CONNECTED.value
                sib_err_code = None

            await cur.execute(
                """
                UPDATE dalton_user_integrations
                SET access_token = pgp_sym_encrypt(%(access_token)s, %(enc_key)s),
                    refresh_token = pgp_sym_encrypt(%(refresh_token)s, %(enc_key)s),
                    expires_at = %(expires_at)s,
                    granted_scopes = %(granted_scopes)s,
                    preferences = %(preferences)s,
                    token_generation = token_generation + 1,
                    refresh_inflight_gen = NULL,
                    refresh_attempt_gen = refresh_attempt_gen + 1,
                    refresh_started_at = NULL,
                    refresh_inflight_until = NULL,
                    status = %(status)s,
                    needs_reauth_at = CASE WHEN %(status)s = 'needs_reauth'
                                           THEN COALESCE(needs_reauth_at, NOW())
                                           ELSE NULL END,
                    last_error_code = %(err_code)s,
                    updated_at = NOW()
                WHERE user_id = %(user_id)s
                  AND provider = 'google'
                  AND service = %(service)s
                  AND oauth_client_id = %(oauth_client_id)s
                  AND provider_account_id = %(provider_account_id)s
                """,
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "enc_key": enc_key,
                    "expires_at": expires_at,
                    "granted_scopes": actual_granted_str,
                    "preferences": Jsonb(sib_prefs),
                    "status": sib_status,
                    "err_code": sib_err_code,
                    "user_id": user_id,
                    "service": sib.service,
                    "oauth_client_id": oauth_client_id,
                    "provider_account_id": provider_account_id,
                },
            )


def _extract_pending_raw_token(row: dict[str, Any]) -> bytes | None:
    if row.get("refresh_token") is not None:
        return bytes(row["refresh_token"])
    if row.get("access_token") is not None:
        return bytes(row["access_token"])
    return None


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
    service: str,
    *,
    trigger_reason: str = TriggerReason.USER_DISCONNECT,
    remote_mode: str = "remote_if_sole",
    event_id: str | None = None,
    expected_oauth_client_id: str | None = None,
    expected_provider_account_id: str | None = None,
    expected_auth_epoch: int | None = None,
    expected_token_generation: int | None = None,
    last_error_code: str | None = None,
    clear_tokens: bool = False,
    skip_if_running_ledger: bool = False,
) -> str:
    """Phase 1 atomic registration transaction for a SINGLE service (§4.2(3) item 1, §4.5(2) item 1).

    For disconnecting all services of a provider at once, call
    ``begin_disconnect_all_services(user_id, provider, ...)``.

    Holds ``advisory_key('dalton_integration_user_lifecycle', user_id, provider)``
    while executing a single short transaction that:
      1. Optionally atomically deduplicates ``(provider, event_id)`` in ``dalton_integration_webhook_events``.
      2. Transitions the target connection row to ``disconnect_pending`` (or ``revoked_purging``),
         optionally verifying expected identity/epoch/token_generation and clearing tokens.
      3. Locks all affected private threads ``FOR UPDATE``, records closed turn ranges,
         sets ``personal_purge_in_progress = TRUE``, and increments ``personal_purge_generation``.
      4. Inserts the ``dalton_integration_purge_ledger`` row (``local_purge_status = 'running'``).
    Commits, releases the lifecycle lock, and returns ``purge_id``.
    """
    if service is None:
        raise ValueError(
            "begin_disconnect only handles a single service; call "
            "begin_disconnect_all_services(user_id, provider, ...) to disconnect all services"
        )

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
    async with get_db_connection() as tx_conn, tx_conn.transaction():
        with ExitStack() as lock_stack:
            await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            lock_stack.enter_context(track_advisory_lock("lifecycle", lock_key))
            # Canonical order: lifecycle -> revoke (sorted ascending), all on the SAME tx_conn.
            if provider == "google":
                revoke_keys_set: set[int] = set()
                pre_row = await db.get_connection(
                    user_id, "google", service, decrypt_tokens=False, conn=tx_conn
                )
                if pre_row is not None:
                    revoke_keys_set.add(
                        revoke_lock_key(
                            extract_google_project(pre_row.oauth_client_id),
                            pre_row.provider_account_id,
                        )
                    )
                pre_pendings = await db.list_pending_confirmations_for_user_provider(
                    user_id, "google", service=service, conn=tx_conn
                )
                for prow in pre_pendings:
                    revoke_keys_set.add(
                        revoke_lock_key(
                            extract_google_project(str(prow["oauth_client_id"])),
                            str(prow["provider_account_id"]),
                        )
                    )
                for rk in sorted(revoke_keys_set):
                    await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (rk,))
                    lock_stack.enter_context(track_advisory_lock("revoke", rk))

            if event_id is not None:
                first_seen = await db.record_webhook_event(
                    provider, event_id, conn=tx_conn
                )
                if not first_seen:
                    raise DuplicateWebhookEventError(
                        f"Duplicate webhook event {provider}:{event_id}"
                    )

            async with tx_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT user_id, provider, service, oauth_client_id,
                           provider_account_id, auth_epoch, token_generation,
                           access_token, refresh_token, status
                    FROM dalton_user_integrations
                    WHERE user_id = %s
                      AND provider = %s
                      AND service = %s
                    FOR UPDATE
                    """,
                    (user_id, provider, service),
                )
                target = await cur.fetchone()
                if target is None:
                    raise IntegrationNotFoundError(
                        f"No connection found for user={user_id} provider={provider} service={service}"
                    )
                target_row = dict(target)

                if (
                    (
                        expected_oauth_client_id is not None
                        and str(target_row["oauth_client_id"]) != expected_oauth_client_id
                    )
                    or (
                        expected_provider_account_id is not None
                        and str(target_row["provider_account_id"])
                        != expected_provider_account_id
                    )
                    or (
                        expected_auth_epoch is not None
                        and int(target_row["auth_epoch"]) != int(expected_auth_epoch)
                    )
                    or (
                        expected_token_generation is not None
                        and int(target_row["token_generation"])
                        != int(expected_token_generation)
                    )
                ):
                    raise DisconnectTargetMismatchError(
                        "Connection identity, epoch, or generation changed before disconnect registration."
                    )

                if skip_if_running_ledger:
                    existing_running = await db.has_running_local_purge_for_service(
                        user_id,
                        provider,
                        service,
                        oauth_client_id=str(target_row["oauth_client_id"]),
                        provider_account_id=str(target_row["provider_account_id"]),
                        min_auth_epoch_upto=int(target_row["auth_epoch"]),
                        conn=tx_conn,
                    )
                    if existing_running:
                        return existing_running[0]

                token_clear_sql = (
                    "access_token = NULL, refresh_token = NULL, "
                    if clear_tokens
                    else ""
                )
                err_code_sql = (
                    "last_error_code = %(last_error_code)s, "
                    if last_error_code is not None
                    else ""
                )
                await cur.execute(
                    f"""
                    UPDATE dalton_user_integrations
                    SET {token_clear_sql}
                        {err_code_sql}
                        status = %(status)s,
                        refresh_inflight_gen = NULL,
                        refresh_attempt_gen = refresh_attempt_gen + 1,
                        refresh_started_at = NULL,
                        refresh_inflight_until = NULL,
                        updated_at = NOW()
                    WHERE user_id = %(user_id)s
                      AND provider = %(provider)s
                      AND service = %(service)s
                    """,
                    {
                        "status": str(target_status),
                        "last_error_code": last_error_code,
                        "user_id": user_id,
                        "provider": provider,
                        "service": service,
                    },
                )

                oauth_client_id = str(target_row["oauth_client_id"])
                provider_account_id = str(target_row["provider_account_id"])
                auth_epoch_upto = int(target_row["auth_epoch"])

                # Void any pending or in-flight OAuth connect intents AND staged pending
                # confirmations for (user_id, provider, service) so an in-flight callback
                # cannot resurrect this connection even if the row is deleted before HTTP ends.
                await db.void_oauth_states_for_user_provider(
                    user_id, provider, service=service, conn=tx_conn
                )
                voided_pending = await db.consume_pending_confirmations_for_user_provider(
                    user_id, provider, service=service, conn=tx_conn
                )
                target_proj = (
                    extract_google_project(oauth_client_id)
                    if provider == "google"
                    else None
                )
                override_raw_token: bytes | None = None
                extra_pending_revokes: list[tuple[str, str, bytes]] = []
                if provider == "google" and not is_revocation and remote_mode != "local_only":
                    seen_extra: set[tuple[str, str]] = set()
                    for prow in voided_pending:
                        ptok = _extract_pending_raw_token(prow)
                        if ptok is None:
                            continue
                        pcid = str(prow["oauth_client_id"])
                        psub = str(prow["provider_account_id"])
                        pproj = extract_google_project(pcid)
                        if pproj == target_proj and psub == provider_account_id:
                            override_raw_token = ptok
                        elif (pproj, psub) not in seen_extra:
                            seen_extra.add((pproj, psub))
                            extra_pending_revokes.append((pcid, psub, ptok))

                siblings = await db.get_sibling_connections(
                    user_id,
                    provider,
                    oauth_client_id,
                    provider_account_id,
                    exclude_service=service,
                    active_only=True,
                    conn=tx_conn,
                )
                has_other_active_grant = bool(siblings)
                if provider == "google" and not has_other_active_grant:
                    has_other_active_grant = await db.has_active_or_pending_google_grant(
                        target_proj or extract_google_project(oauth_client_id),
                        provider_account_id,
                        user_id=user_id,
                        exclude_service=service,
                        conn=tx_conn,
                    )
                remote_status, enc_revoke_token = _determine_remote_revoke(
                    provider=provider,
                    trigger_reason=trigger_reason,
                    remote_mode=remote_mode,
                    has_remaining_active_sibling=has_other_active_grant,
                    raw_refresh_token=(
                        override_raw_token
                        if override_raw_token is not None
                        else target_row.get("refresh_token")
                    ),
                    raw_access_token=target_row.get("access_token"),
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
            for pcid, psub, ptok in extra_pending_revokes:
                pproj = extract_google_project(pcid)
                if not await db.has_active_or_pending_google_grant(
                    pproj, psub, user_id=user_id, conn=tx_conn
                ):
                    await db.insert_purge_ledger(
                        purge_id=str(uuid4()),
                        user_id=user_id,
                        provider="google",
                        service=service,
                        oauth_client_id=pcid,
                        provider_account_id=psub,
                        auth_epoch_upto=0,
                        trigger_reason=trigger_reason,
                        local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                        purged_max_turn_by_thread={},
                        remote_revoke_status=RemoteRevokeStatus.PENDING,
                        encrypted_revoke_token=ptok,
                        conn=tx_conn,
                    )
            return purge_id


async def begin_disconnect_all_services(
    user_id: str,
    provider: str,
    *,
    trigger_reason: str = TriggerReason.USER_DISCONNECT,
    remote_mode: str = "remote_if_sole",
    event_id: str | None = None,
) -> list[str]:
    """Phase 1 atomic registration transaction for ALL services of ``(user_id, provider)`` (§4.2(3)).

    In ONE transaction under ``advisory_key('dalton_integration_user_lifecycle', user_id, provider)``:
      1. Locks and transitions all target connection rows to ``disconnect_pending`` / ``revoked_purging``.
      2. Groups target rows by ``(oauth_client_id, provider_account_id)``: assigns
         ``remote_revoke_status = 'pending'`` (with that group's token) to AT MOST ONE
         ledger per group, and ``'not_applicable'`` to sibling ledgers in the same group.
      3. Locks each affected private thread ONCE ``FOR UPDATE``, merges all closed turn
         ranges, sets ``personal_purge_in_progress = TRUE``, and bumps ``personal_purge_generation`` ONCE.
      4. Inserts ONE ``dalton_integration_purge_ledger`` row per target connection row
         (recording that row's own ``service``, ``oauth_client_id``, ``provider_account_id``,
         and ``auth_epoch_upto``).
    Returns the list of created ``purge_id`` values; callers invoke ``finish_disconnect``
    for each returned ``purge_id``.
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
    async with get_db_connection() as tx_conn, tx_conn.transaction():
        with ExitStack() as lock_stack:
            await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            lock_stack.enter_context(track_advisory_lock("lifecycle", lock_key))
            # Canonical order: lifecycle -> revoke (sorted ascending), all on the SAME tx_conn.
            if provider == "google":
                revoke_keys_set: set[int] = set()
                pre_rows = await db.list_user_connections(
                    user_id, provider="google", conn=tx_conn
                )
                for r in pre_rows:
                    revoke_keys_set.add(
                        revoke_lock_key(
                            extract_google_project(r.oauth_client_id),
                            r.provider_account_id,
                        )
                    )
                pre_pendings = await db.list_pending_confirmations_for_user_provider(
                    user_id, "google", conn=tx_conn
                )
                for prow in pre_pendings:
                    revoke_keys_set.add(
                        revoke_lock_key(
                            extract_google_project(str(prow["oauth_client_id"])),
                            str(prow["provider_account_id"]),
                        )
                    )
                for rk in sorted(revoke_keys_set):
                    await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (rk,))
                    lock_stack.enter_context(track_advisory_lock("revoke", rk))

            if event_id is not None:
                first_seen = await db.record_webhook_event(
                    provider, event_id, conn=tx_conn
                )
                if not first_seen:
                    raise DuplicateWebhookEventError(
                        f"Duplicate webhook event {provider}:{event_id}"
                    )

            async with tx_conn.cursor(row_factory=dict_row) as cur:
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
                        f"No connection found for user={user_id} provider={provider}"
                    )

                target_services = [str(r["service"]) for r in targets]
                await cur.execute(
                    """
                    UPDATE dalton_user_integrations
                    SET status = %s,
                        refresh_inflight_gen = NULL,
                        refresh_attempt_gen = refresh_attempt_gen + 1,
                        refresh_started_at = NULL,
                        refresh_inflight_until = NULL,
                        updated_at = NOW()
                    WHERE user_id = %s
                      AND provider = %s
                      AND service = ANY(%s)
                    """,
                    (str(target_status), user_id, provider, target_services),
                )

                # Serialize with concurrent Fork touch copy before enumerating touches
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    (touch_user_lock_key(user_id),),
                )

                # Collect unpurged touches per target connection row
                per_service_touches: dict[str, dict[str, tuple[int, int]]] = {}
                thread_touch_bounds: dict[str, tuple[int, int]] = {}
                for r in targets:
                    svc = str(r["service"])
                    epoch = int(r["auth_epoch"])
                    await cur.execute(
                        """
                        SELECT thread_id::text AS thread_id,
                               MIN(turn_index) AS first_touched_turn_index,
                               MAX(turn_index) AS max_touched_turn_index
                        FROM dalton_thread_integration_touches
                        WHERE user_id = %s
                          AND provider = %s
                          AND service = %s
                          AND auth_epoch <= %s
                          AND purged_at IS NULL
                        GROUP BY thread_id
                        ORDER BY thread_id ASC
                        """,
                        (user_id, provider, svc, epoch),
                    )
                    svc_map: dict[str, tuple[int, int]] = {}
                    for grp in await cur.fetchall():
                        tid = str(grp["thread_id"])
                        lo = int(grp["first_touched_turn_index"])
                        hi = int(grp["max_touched_turn_index"])
                        svc_map[tid] = (lo, hi)
                        if tid in thread_touch_bounds:
                            prev_lo, prev_hi = thread_touch_bounds[tid]
                            thread_touch_bounds[tid] = (min(prev_lo, lo), max(prev_hi, hi))
                        else:
                            thread_touch_bounds[tid] = (lo, hi)
                    per_service_touches[svc] = svc_map

                # Lock each affected thread ONCE, bump generation ONCE, merge closed ranges ONCE
                thread_purge_max: dict[str, int] = {}
                for tid in sorted(thread_touch_bounds.keys()):
                    overall_lo, overall_max_touch = thread_touch_bounds[tid]
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
                        (tid, tid, overall_max_touch),
                    )
                    max_row = await cur.fetchone()
                    purge_start_max = max(
                        overall_lo,
                        int(max_row["max_turn"]) if max_row else overall_lo,
                    )

                    merged = merge_turn_range(
                        t_row.get("personal_purged_turn_ranges"),
                        overall_lo,
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
                    thread_purge_max[tid] = purge_start_max

            # Void all pending/in-flight OAuth intents AND staged pending confirmations for
            # (user_id, provider) so in-flight callbacks cannot resurrect disconnected rows.
            await db.void_oauth_states_for_user_provider(
                user_id, provider, conn=tx_conn
            )
            voided_all_pending = await db.consume_pending_confirmations_for_user_provider(
                user_id, provider, conn=tx_conn
            )
            pending_token_by_proj_sub: dict[tuple[str, str], tuple[str, str | None, bytes]] = {}
            if provider == "google" and not is_revocation and remote_mode != "local_only":
                for prow in voided_all_pending:
                    ptok = _extract_pending_raw_token(prow)
                    if ptok is None:
                        continue
                    pcid = str(prow["oauth_client_id"])
                    psub = str(prow["provider_account_id"])
                    pproj = extract_google_project(pcid)
                    pending_token_by_proj_sub[(pproj, psub)] = (
                        pcid,
                        str(prow["service"]) if prow.get("service") else None,
                        ptok,
                    )

            # Group by (google_project, sub) for Google or (oauth_client_id, sub) otherwise
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for r in targets:
                cid_r = str(r["oauth_client_id"])
                sub_r = str(r["provider_account_id"])
                gkey = (
                    (extract_google_project(cid_r), sub_r)
                    if provider == "google"
                    else (cid_r, sub_r)
                )
                groups.setdefault(gkey, []).append(r)

            covered_proj_subs: set[tuple[str, str]] = set()
            group_revoke_assignment: dict[tuple[str, str], tuple[str, RemoteRevokeStatus, bytes | None]] = {}
            for gkey, grows in groups.items():
                proj_or_cid, sub_g = gkey
                raw_refresh = next(
                    (r["refresh_token"] for r in grows if r.get("refresh_token") is not None),
                    None,
                )
                raw_access = next(
                    (r["access_token"] for r in grows if r.get("access_token") is not None),
                    None,
                )
                if provider == "google":
                    covered_proj_subs.add((proj_or_cid, sub_g))
                    if (proj_or_cid, sub_g) in pending_token_by_proj_sub:
                        raw_refresh = pending_token_by_proj_sub[(proj_or_cid, sub_g)][2]
                g_status, g_token = _determine_remote_revoke(
                    provider=provider,
                    trigger_reason=trigger_reason,
                    remote_mode=remote_mode,
                    has_remaining_active_sibling=False,
                    raw_refresh_token=raw_refresh,
                    raw_access_token=raw_access,
                )
                owner_service = str(grows[0]["service"])
                group_revoke_assignment[gkey] = (owner_service, g_status, g_token)

            purge_ids: list[str] = []
            for r in targets:
                svc = str(r["service"])
                cid = str(r["oauth_client_id"])
                sub = str(r["provider_account_id"])
                epoch = int(r["auth_epoch"])
                gkey = (
                    (extract_google_project(cid), sub)
                    if provider == "google"
                    else (cid, sub)
                )
                owner_svc, g_status, g_token = group_revoke_assignment[gkey]
                if svc == owner_svc:
                    row_remote_status = g_status
                    row_enc_token = g_token
                else:
                    row_remote_status = RemoteRevokeStatus.NOT_APPLICABLE
                    row_enc_token = None

                svc_ranges: dict[str, dict[str, int]] = {}
                for tid, (svc_lo, _svc_hi) in per_service_touches.get(svc, {}).items():
                    if tid in thread_purge_max:
                        svc_ranges[tid] = {
                            "min_turn": svc_lo,
                            "max_turn": thread_purge_max[tid],
                        }

                pid = str(uuid4())
                await db.insert_purge_ledger(
                    purge_id=pid,
                    user_id=user_id,
                    provider=provider,
                    service=svc,
                    oauth_client_id=cid,
                    provider_account_id=sub,
                    auth_epoch_upto=epoch,
                    trigger_reason=trigger_reason,
                    local_purge_status=LocalPurgeStatus.RUNNING,
                    purged_max_turn_by_thread=svc_ranges,
                    remote_revoke_status=row_remote_status,
                    encrypted_revoke_token=row_enc_token,
                    conn=tx_conn,
                )
                purge_ids.append(pid)

            # Enqueue revocations for any voided Google pending confirmations targeting other (project, sub)
            for (pproj, psub), (pcid, psvc, ptok) in pending_token_by_proj_sub.items():
                if (pproj, psub) in covered_proj_subs:
                    continue
                extra_pid = str(uuid4())
                await db.insert_purge_ledger(
                    purge_id=extra_pid,
                    user_id=user_id,
                    provider="google",
                    service=psvc,
                    oauth_client_id=pcid,
                    provider_account_id=psub,
                    auth_epoch_upto=0,
                    trigger_reason=trigger_reason,
                    local_purge_status=LocalPurgeStatus.NOT_REQUIRED,
                    purged_max_turn_by_thread={},
                    remote_revoke_status=RemoteRevokeStatus.PENDING,
                    encrypted_revoke_token=ptok,
                    conn=tx_conn,
                )
                purge_ids.append(extra_pid)

            return purge_ids


async def begin_feishu_webhook_revocation_batch(
    *,
    app_id: str,
    open_id: str | None,
    union_id: str | None,
    event_id: str | None,
    event_time: datetime,
    candidate_user_ids: Sequence[str],
) -> tuple[str, list[str]]:
    """Atomically register Phase 1 + webhook dedup across ALL matching Dalton users in ONE transaction (§4.5(3)).

    Acquires ``lifecycle_lock_key(uid, 'feishu')`` in ascending ``uid`` order, then in a
    single transaction:
      1. Locks all matching ``dalton_user_integrations`` rows ``FOR UPDATE``.
      2. Re-checks ``event_time >= row.authorized_at`` inside the transaction so a
         concurrent re-authorization is never purged by an older revocation event.
      3. Skips any ``(user_id, service)`` that already has a ``running`` purge ledger
         (allowing safe replay to complete registration for any missing users).
      4. Records ``('feishu', event_id)`` in ``dalton_integration_webhook_events`` together
         with all eligible users' Phase 1 status updates, thread locks, and purge ledgers.

    Returns ``(outcome, purge_ids)`` where ``outcome`` is one of:
    ``"purged"``, ``"duplicate"``, ``"stale_before_authorized_at"``, ``"no_matching_connection"``.
    """
    unique_uids = sorted({str(u) for u in candidate_user_ids if u})
    if not unique_uids:
        return "no_matching_connection", []

    event_time_utc = (
        event_time if event_time.tzinfo is not None else event_time.replace(tzinfo=UTC)
    )
    lock_keys = [lifecycle_lock_key(uid, "feishu") for uid in unique_uids]

    async with get_db_connection() as tx_conn, tx_conn.transaction():
        with ExitStack() as lock_stack:
            for lk in lock_keys:
                await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (lk,))
                lock_stack.enter_context(track_advisory_lock("lifecycle", lk))

            already_seen = (
                await db.has_webhook_event("feishu", event_id, conn=tx_conn)
                if event_id is not None
                else False
            )

            match_clauses: list[str] = []
            params: dict[str, Any] = {
                "app_id": app_id,
                "user_ids": unique_uids,
            }
            if open_id:
                match_clauses.append("provider_account_id = %(open_id)s")
                params["open_id"] = open_id
            if union_id:
                match_clauses.append("provider_union_id = %(union_id)s")
                params["union_id"] = union_id
            if not match_clauses:
                return ("duplicate" if already_seen else "no_matching_connection"), []

            async with tx_conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT user_id, provider, service, oauth_client_id,
                           provider_account_id, auth_epoch, authorized_at, status
                    FROM dalton_user_integrations
                    WHERE provider = 'feishu'
                      AND oauth_client_id = %(app_id)s
                      AND user_id = ANY(%(user_ids)s)
                      AND ({' OR '.join(match_clauses)})
                    ORDER BY user_id ASC, service ASC
                    FOR UPDATE
                    """,
                    params,
                )
                locked_rows = [dict(r) for r in await cur.fetchall()]

            if not locked_rows:
                return ("duplicate" if already_seen else "no_matching_connection"), []

            eligible_rows: list[dict[str, Any]] = []
            for r in locked_rows:
                auth_at = r.get("authorized_at")
                if auth_at is not None:
                    auth_at_utc = (
                        auth_at if auth_at.tzinfo is not None else auth_at.replace(tzinfo=UTC)
                    )
                    if event_time_utc < auth_at_utc:
                        logger.info(
                            "[feishu-events] skipping stale revocation in-tx event_id=%s user=%s service=%s (event_time < authorized_at)",
                            event_id,
                            r["user_id"],
                            r["service"],
                        )
                        continue
                eligible_rows.append(r)

            if not eligible_rows:
                return ("duplicate" if already_seen else "stale_before_authorized_at"), []

            rows_needing_ledger: list[dict[str, Any]] = []
            for r in eligible_rows:
                running_ids = await db.has_running_local_purge_for_service(
                    str(r["user_id"]),
                    "feishu",
                    str(r["service"]),
                    oauth_client_id=str(r["oauth_client_id"]),
                    provider_account_id=str(r["provider_account_id"]),
                    min_auth_epoch_upto=int(r["auth_epoch"]),
                    conn=tx_conn,
                )
                if running_ids:
                    continue
                rows_needing_ledger.append(r)

            if not rows_needing_ledger:
                return ("duplicate" if already_seen else "no_matching_connection"), []

            if event_id is not None and not already_seen:
                await db.record_webhook_event("feishu", event_id, conn=tx_conn)

            affected_uids = sorted({str(r["user_id"]) for r in rows_needing_ledger})
            async with tx_conn.cursor(row_factory=dict_row) as cur:
                for uid in affected_uids:
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (touch_user_lock_key(uid),),
                    )

                for r in rows_needing_ledger:
                    await cur.execute(
                        """
                        UPDATE dalton_user_integrations
                        SET status = %s,
                            access_token = NULL,
                            refresh_token = NULL,
                            refresh_inflight_gen = NULL,
                            refresh_attempt_gen = refresh_attempt_gen + 1,
                            refresh_started_at = NULL,
                            refresh_inflight_until = NULL,
                            last_error_code = 'webhook_revoked',
                            updated_at = NOW()
                        WHERE user_id = %s
                          AND provider = 'feishu'
                          AND service = %s
                        """,
                        (
                            str(IntegrationStatus.REVOKED_PURGING),
                            str(r["user_id"]),
                            str(r["service"]),
                        ),
                    )
                    await db.void_oauth_states_for_user_provider(
                        str(r["user_id"]), "feishu", service=str(r["service"]), conn=tx_conn
                    )

                per_row_touches: dict[tuple[str, str], dict[str, tuple[int, int]]] = {}
                thread_touch_bounds: dict[str, tuple[int, int]] = {}
                for r in rows_needing_ledger:
                    uid = str(r["user_id"])
                    svc = str(r["service"])
                    epoch = int(r["auth_epoch"])
                    await cur.execute(
                        """
                        SELECT thread_id::text AS thread_id,
                               MIN(turn_index) AS first_touched_turn_index,
                               MAX(turn_index) AS max_touched_turn_index
                        FROM dalton_thread_integration_touches
                        WHERE user_id = %s
                          AND provider = 'feishu'
                          AND service = %s
                          AND auth_epoch <= %s
                          AND purged_at IS NULL
                        GROUP BY thread_id
                        ORDER BY thread_id ASC
                        """,
                        (uid, svc, epoch),
                    )
                    row_map: dict[str, tuple[int, int]] = {}
                    for grp in await cur.fetchall():
                        tid = str(grp["thread_id"])
                        lo = int(grp["first_touched_turn_index"])
                        hi = int(grp["max_touched_turn_index"])
                        row_map[tid] = (lo, hi)
                        if tid in thread_touch_bounds:
                            prev_lo, prev_hi = thread_touch_bounds[tid]
                            thread_touch_bounds[tid] = (min(prev_lo, lo), max(prev_hi, hi))
                        else:
                            thread_touch_bounds[tid] = (lo, hi)
                    per_row_touches[(uid, svc)] = row_map

                thread_purge_max: dict[str, int] = {}
                for tid in sorted(thread_touch_bounds.keys()):
                    overall_lo, overall_max_touch = thread_touch_bounds[tid]
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
                        (tid, tid, overall_max_touch),
                    )
                    max_row = await cur.fetchone()
                    purge_start_max = max(
                        overall_lo,
                        int(max_row["max_turn"]) if max_row else overall_lo,
                    )
                    merged = merge_turn_range(
                        t_row.get("personal_purged_turn_ranges"),
                        overall_lo,
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
                    thread_purge_max[tid] = purge_start_max

            purge_ids: list[str] = []
            for r in rows_needing_ledger:
                uid = str(r["user_id"])
                svc = str(r["service"])
                cid = str(r["oauth_client_id"])
                sub = str(r["provider_account_id"])
                epoch = int(r["auth_epoch"])
                svc_ranges: dict[str, dict[str, int]] = {}
                for tid, (svc_lo, _svc_hi) in per_row_touches.get((uid, svc), {}).items():
                    if tid in thread_purge_max:
                        svc_ranges[tid] = {
                            "min_turn": svc_lo,
                            "max_turn": thread_purge_max[tid],
                        }
                pid = str(uuid4())
                await db.insert_purge_ledger(
                    purge_id=pid,
                    user_id=uid,
                    provider="feishu",
                    service=svc,
                    oauth_client_id=cid,
                    provider_account_id=sub,
                    auth_epoch_upto=epoch,
                    trigger_reason=TriggerReason.WEBHOOK_REVOKED,
                    local_purge_status=LocalPurgeStatus.RUNNING,
                    purged_max_turn_by_thread=svc_ranges,
                    remote_revoke_status=RemoteRevokeStatus.NOT_APPLICABLE,
                    encrypted_revoke_token=None,
                    conn=tx_conn,
                )
                purge_ids.append(pid)

            return "purged", purge_ids


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
    expected_old_oauth_client_id: str | None = None,
    expected_old_provider_account_id: str | None = None,
    expected_old_auth_epoch: int | None = None,
    consume_confirmation_hash: str | None = None,
) -> AccountSwitchResult:
    """Explicit account switch: block & replace in ONE transaction (§4.2(1), §4.2(3)).

    Reuses Phase 1 to lock affected threads, merge closed ranges, bump generation,
    and register the purge ledger (``trigger_reason='account_switch'``), then
    replaces the connection row IN PLACE with the new account (``status='connected'``,
    ``token_generation=1``, ``authorized_at=NOW()``, fresh ``auth_epoch``, reset
    account-specific preferences). Never deletes the new connection row.

    When ``expected_old_*`` are supplied, re-verifies them under the ``FOR UPDATE`` row
    lock inside the Phase 1 transaction and raises ``AccountSwitchTargetChangedError``
    on any mismatch so interleaved confirmation tickets cannot overwrite a newer account.
    When ``consume_confirmation_hash`` is supplied, atomically consumes that pending
    confirmation row INSIDE the activation transaction (never before).

    Raises ``MissingRefreshTokenOnAccountSwitchError`` if ``new_refresh_token`` is absent,
    leaving the existing connection completely untouched.
    """
    if not new_refresh_token:
        raise MissingRefreshTokenOnAccountSwitchError(
            "missing_refresh_token_on_account_switch"
        )

    enc_key = get_encryption_key()
    lock_key = lifecycle_lock_key(user_id, provider)
    target_refresh_key = refresh_lock_key(
        user_id,
        provider,
        new_oauth_client_id,
        new_provider_account_id,
        service if provider == "feishu" else None,
    )
    async with get_db_connection() as tx_conn, tx_conn.transaction():
        with ExitStack() as lock_stack:
            await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            lock_stack.enter_context(track_advisory_lock("lifecycle", lock_key))
            # Canonical unidirectional lock order: lifecycle -> refresh -> revoke (sorted ascending)
            # All acquired on the SAME tx_conn (zero second pooled connection).
            await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (target_refresh_key,))
            lock_stack.enter_context(track_advisory_lock("refresh", target_refresh_key))

            if provider == "google":
                new_proj = extract_google_project(new_oauth_client_id)
                revoke_keys_set: set[int] = {
                    revoke_lock_key(new_proj, new_provider_account_id)
                }
                pre_existing = await db.get_connection(
                    user_id, "google", service, decrypt_tokens=False, conn=tx_conn
                )
                if pre_existing is not None:
                    revoke_keys_set.add(
                        revoke_lock_key(
                            extract_google_project(pre_existing.oauth_client_id),
                            pre_existing.provider_account_id,
                        )
                    )
                for rk in sorted(revoke_keys_set):
                    await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (rk,))
                    lock_stack.enter_context(track_advisory_lock("revoke", rk))

                if await db.has_other_user_google_account_claim(
                    new_proj,
                    new_provider_account_id,
                    exclude_user_id=user_id,
                    conn=tx_conn,
                ):
                    raise GoogleAccountAlreadyConnectedError()

            if consume_confirmation_hash is not None:
                consumed_conf = await db.consume_pending_confirmation(
                    consume_confirmation_hash,
                    user_id,
                    conn=tx_conn,
                )
                if consumed_conf is None:
                    raise AccountSwitchTargetChangedError(
                        "Confirmation ticket is invalid, expired, or was invalidated before activation."
                    )

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
                old_status = str(existing["status"])
                if (
                    old_status in (
                        IntegrationStatus.DISCONNECT_PENDING.value,
                        IntegrationStatus.REVOKED_PURGING.value,
                    )
                    or (
                        expected_old_oauth_client_id is not None
                        and old_client_id != expected_old_oauth_client_id
                    )
                    or (
                        expected_old_provider_account_id is not None
                        and old_sub != expected_old_provider_account_id
                    )
                    or (
                        expected_old_auth_epoch is not None
                        and old_auth_epoch != int(expected_old_auth_epoch)
                    )
                ):
                    raise AccountSwitchTargetChangedError(
                        "Existing account changed before account-switch confirmation committed."
                    )
                if old_sub == new_provider_account_id:
                    raise ValueError(
                        "replace_connection_for_account_switch requires a different provider_account_id"
                    )

                await db.void_oauth_states_for_user_provider(
                    user_id, provider, service=service, conn=tx_conn
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
                has_other_old_grant = bool(siblings)
                if provider == "google" and not has_other_old_grant:
                    has_other_old_grant = await db.has_active_or_pending_google_grant(
                        extract_google_project(old_client_id),
                        old_sub,
                        user_id=user_id,
                        exclude_service=service,
                        conn=tx_conn,
                    )
                remote_status, enc_revoke_token = _determine_remote_revoke(
                    provider=provider,
                    trigger_reason=TriggerReason.ACCOUNT_SWITCH,
                    remote_mode=remote_mode,
                    has_remaining_active_sibling=has_other_old_grant,
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
                        refresh_attempt_gen = refresh_attempt_gen + 1,
                        refresh_started_at = NULL,
                        refresh_inflight_until = NULL,
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

            # For Google: synchronize active sibling services already connected to the target
            # (new_oauth_client_id, new_provider_account_id) under the target refresh lock,
            # updating tokens, actual granted scopes, preferences, and status in the same tx.
            if provider == "google":
                target_siblings = await db.list_connections_by_account(
                    user_id,
                    "google",
                    new_oauth_client_id,
                    new_provider_account_id,
                    decrypt_tokens=False,
                    for_update=True,
                    conn=tx_conn,
                )
                active_target_siblings = [
                    r
                    for r in target_siblings
                    if r.service != service
                    and r.status
                    not in (
                        IntegrationStatus.DISCONNECT_PENDING,
                        IntegrationStatus.REVOKED_PURGING,
                    )
                ]
                if active_target_siblings:
                    await sync_google_sibling_rows_tx(
                        tx_conn,
                        user_id=user_id,
                        oauth_client_id=new_oauth_client_id,
                        provider_account_id=new_provider_account_id,
                        access_token=new_access_token,
                        refresh_token=new_refresh_token,
                        expires_at=new_expires_at,
                        granted_scopes=new_granted_scopes,
                        active_siblings=active_target_siblings,
                    )

                await db.supersede_active_revokes_for_google_project_sub_tx(
                    extract_google_project(new_oauth_client_id),
                    new_provider_account_id,
                    conn=tx_conn,
                )

            return AccountSwitchResult(
                purge_id=purge_id,
                old_auth_epoch=old_auth_epoch,
                new_auth_epoch=new_auth_epoch,
                remote_revoke_status=remote_status,
                affected_threads=affected,
            )


async def _drain_refresh_locks(
    *,
    user_id: str,
    provider: str,
    service: str | None,
    oauth_client_id: str,
    provider_account_id: str,
) -> None:
    """Acquire and release refresh advisory lock(s) in ascending key order to drain in-flight short lock sections."""
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

    async with get_db_connection() as conn, conn.transaction():
        for key in keys:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


async def finish_disconnect_locked(
    purge_id: str,
    *,
    revoker: RemoteRevoker | None = None,
    cancel_drain_timeout_s: float = 120.0,
    run_remote_revoke: bool = True,
) -> FinishDisconnectResult | None:
    """Shared mutex-protected Phase 2 entry point for ``finish_disconnect`` and resume loops.

    Acquires non-blocking ``pg_try_advisory_lock(advisory_key('dalton_integration_purge', purge_id))``.
    Returns ``None`` immediately if another worker/coroutine already holds the lock.
    Once acquired, re-reads the ledger inside the lock and skips local purge if
    ``local_purge_status != 'running'``.
    """
    from src.dalton.services.integration_data_purge import (
        run_local_purge,
        try_purge_ledger_lock,
    )

    deleted_connections = 0
    local_status = LocalPurgeStatus.RUNNING
    final_remote_status = RemoteRevokeStatus.NOT_APPLICABLE
    claimed_attempt_gen: int | None = None

    # Hold try_purge_ledger_lock ONLY for the short PG claim & connection-delete phase;
    # release the dedicated PG session and advisory lock BEFORE running 2A purge (which
    # performs Redis cancel nudges & stream deletions) or remote revocation HTTP (§4.2(3)).
    async with try_purge_ledger_lock(purge_id) as acquired:
        if not acquired:
            logger.debug(
                "[integration-disconnect] purge_id=%s already locked by another worker; skipping",
                purge_id,
            )
            return None
        ledger = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
        if ledger is None:
            raise LookupError(f"Purge ledger {purge_id} not found")

        if ledger.local_purge_status == LocalPurgeStatus.RUNNING:
            claimed_attempt_gen = await db.claim_local_purge_attempt(purge_id)
            if claimed_attempt_gen is None:
                logger.debug(
                    "[integration-disconnect] purge_id=%s already claimed via active durable lease; skipping",
                    purge_id,
                )
                return None

            try:
                await _drain_refresh_locks(
                    user_id=ledger.user_id,
                    provider=ledger.provider,
                    service=ledger.service,
                    oauth_client_id=ledger.oauth_client_id,
                    provider_account_id=ledger.provider_account_id,
                )

                if ledger.trigger_reason != TriggerReason.ACCOUNT_SWITCH:
                    deleted_connections = await db.delete_connection_upto_epoch(
                        ledger.user_id,
                        ledger.provider,
                        ledger.service,
                        ledger.auth_epoch_upto,
                    )
            except BaseException:
                try:
                    await db.release_local_purge_attempt(
                        purge_id, expected_attempt_gen=claimed_attempt_gen
                    )
                except Exception:
                    pass
                raise
        else:
            local_status = ledger.local_purge_status
            final_remote_status = ledger.remote_revoke_status

    # Dedicated PG session and ledger advisory lock are now RELEASED.
    if claimed_attempt_gen is not None:
        await run_local_purge(
            purge_id,
            cancel_drain_timeout_s=cancel_drain_timeout_s,
            _lock_held=True,
            _claimed_attempt_gen=claimed_attempt_gen,
        )
        refreshed = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
        assert refreshed is not None
        local_status = refreshed.local_purge_status
        final_remote_status = refreshed.remote_revoke_status

    # Remote revocation runs strictly OUTSIDE try_purge_ledger_lock so no PG advisory lock
    # or dedicated session connection is ever held across external HTTP (§4.2(3)).
    if (
        run_remote_revoke
        and final_remote_status == RemoteRevokeStatus.PENDING
        and revoker is not None
    ):
        final_remote_status = await process_revoke(purge_id, revoker)

    return FinishDisconnectResult(
        purge_id=purge_id,
        deleted_connections=deleted_connections,
        local_purge_status=local_status,
        remote_revoke_status=final_remote_status,
    )


async def finish_disconnect(
    purge_id: str,
    *,
    revoker: RemoteRevoker | None = None,
    cancel_drain_timeout_s: float = 120.0,
    run_remote_revoke: bool = True,
) -> FinishDisconnectResult | None:
    """Phase 2 finalization (§4.2(3) item 1), guarded by per-ledger advisory lock.

    Delegates to ``finish_disconnect_locked`` so concurrent callers and the
    background resume loop share identical mutual exclusion.
    """
    return await finish_disconnect_locked(
        purge_id,
        revoker=revoker,
        cancel_drain_timeout_s=cancel_drain_timeout_s,
        run_remote_revoke=run_remote_revoke,
    )
