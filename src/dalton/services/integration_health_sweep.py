"""12-hour multi-worker health patrol for personal integrations (§4.5(3)).

Coordinates exclusively via PostgreSQL (``pg_try_advisory_lock(health_sweep_lock_key())``
and ``dalton_integration_health_sweep_state``) so only one worker executes each 12-hour
sweep. Strictly distinguishes:
- Missing/unready deployment credentials (``get_credential_group(...).ready == False``) -> SKIP
  (never misclassified as revocation or needs_reauth).
- Transient network/5xx errors -> keep connection active, retry next sweep.
- Routine invalidation (Google ``invalid_grant``, Feishu ``20024/20063/20037/20005/20064``,
  expired ``refresh_ambiguous``) -> ``needs_reauth`` (preserves online conversation history).
- Official confirmed user revocation -> ``revoked_purging`` + Scheme 2A purge.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
import os
import socket

import anyio

from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    IntegrationStatus,
    TriggerReason,
    health_sweep_lock_key,
)
from src.dalton.deployment_credentials.snapshot import get_credential_group
from src.dalton.integrations.constants import HEALTH_SWEEP_INTERVAL_SECONDS
from src.dalton.integrations.lock_guard import track_advisory_lock
from src.dalton.integrations.oauth_clients import (
    FeishuOAuthClientProtocol,
    GoogleOAuthClientProtocol,
)
from src.dalton.services.integration_revoke import RemoteRevoker
from src.dalton.services.user_integrations_lifecycle import (
    DisconnectTargetMismatchError,
    IntegrationNotFoundError,
    begin_disconnect,
    finish_disconnect,
)
from src.dalton.services.user_integrations_oauth import (
    IntegrationCredentialsNotReadyError,
    IntegrationEpochRevokedError,
    IntegrationNeedsReauthError,
    IntegrationNotConnectedError,
    IntegrationRefreshBusyError,
    IntegrationRevokedError,
    IntegrationTransientError,
    ensure_fresh_access_token,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HealthSweepReport:
    acquired: bool
    skipped_reason: str | None = None
    checked: int = 0
    healthy: int = 0
    marked_needs_reauth: int = 0
    triggered_revocation_purge: int = 0
    transient_errors: int = 0
    skipped_credentials_not_ready: int = 0
    skipped_unexpired_ambiguous: int = 0
    skipped_concurrent: int = 0
    expired_states_deleted: int = 0
    expired_confirmations_deleted: int = 0


def _worker_identity() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _is_provider_credentials_ready(provider: str) -> bool:
    if provider == "google":
        return get_credential_group("google_oauth").ready
    if provider == "feishu":
        return get_credential_group("feishu_app").ready
    return False


_SWEEP_INFLIGHT_LEASE_SECONDS = 600


async def _renew_sweep_lease_or_false(
    *,
    lock_key: int,
    expected_sweep_gen: int,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """Renew ``inflight_until`` under a short ``pg_advisory_xact_lock`` on ONE pooled connection.

    Returns ``False`` (so the caller stops immediately) if the lease expired, was taken over
    by another worker (``sweep_gen`` changed), or the DB renewal fails.
    """
    try:
        async with get_db_connection() as tx_conn, tx_conn.transaction():
            await tx_conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
            with track_advisory_lock("health_sweep", lock_key):
                return await db.renew_health_sweep_lease(
                    expected_sweep_gen=expected_sweep_gen,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    conn=tx_conn,
                )
    except Exception as exc:
        logger.warning(
            "[integration-sweep] lease renewal failed for sweep_gen=%s (%s); stopping sweep",
            expected_sweep_gen,
            type(exc).__name__,
        )
        return False


async def run_integration_health_sweep_once(
    *,
    force: bool = False,
    min_interval_seconds: int = HEALTH_SWEEP_INTERVAL_SECONDS,
    limit: int = 500,
    lease_seconds: int = _SWEEP_INFLIGHT_LEASE_SECONDS,
    google_client: GoogleOAuthClientProtocol | None = None,
    feishu_client: FeishuOAuthClientProtocol | None = None,
    revoker: RemoteRevoker | None = None,
) -> HealthSweepReport:
    """Run one cluster-wide mutually-exclusive personal integration health sweep (§4.5(3)).

    Guarantees at most ONE active sweep at any time using ONE pooled connection per short DB step:
      1. Short ``pg_try_advisory_xact_lock(health_sweep_lock_key())`` on ONE connection:
         verify no active sweep lease and 12-hour interval elapsed, claim a monotonic
         ``sweep_gen`` + ``inflight_until`` lease, and snapshot candidates in the SAME
         transaction (any claim or listing failure rolls back automatically and exits
         immediately without leaving a stale lease or continuing execution).
      2. Outside any global lock or pool connection: before each orphan repair and each
         shared-credential group, renew and verify ``sweep_gen`` + unexpired ``inflight_until``;
         if the lease was lost or taken over by another worker, stop immediately.
      3. Short ``pg_advisory_xact_lock`` on ONE connection: verify ``sweep_gen`` still matches
         and commit ``last_completed_at`` + clear ``inflight_until``.
    """
    lock_key = health_sweep_lock_key()
    worker_id = _worker_identity()
    eff_lease_seconds = max(1, int(lease_seconds))
    claimed_sweep_gen: int | None = None
    expired_states = 0
    expired_confirms = 0
    connections: list[db.IntegrationRow] = []
    orphaned_revoked: list[db.IntegrationRow] = []

    # Step 1: ONE pooled connection + pg_try_advisory_xact_lock.
    # Claiming sweep_gen and snapshotting candidates share one atomic transaction so a failure
    # rolls back the lease automatically and exits immediately.
    try:
        async with get_db_connection() as tx_conn, tx_conn.transaction():
            async with tx_conn.cursor() as cur:
                await cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (lock_key,))
                row = await cur.fetchone()
                acquired = bool(row and row[0])
            if not acquired:
                return HealthSweepReport(acquired=False, skipped_reason="lock_busy")

            with track_advisory_lock("health_sweep", lock_key):
                state = await db.read_health_sweep_state(for_update=True, conn=tx_conn)
                if state:
                    db_now = state.get("db_now") or datetime.now(UTC)
                    if db_now.tzinfo is None:
                        db_now = db_now.replace(tzinfo=UTC)
                    inflight_until = state.get("inflight_until")
                    if inflight_until is not None:
                        inflight_utc = (
                            inflight_until
                            if inflight_until.tzinfo is not None
                            else inflight_until.replace(tzinfo=UTC)
                        )
                        if inflight_utc > db_now:
                            return HealthSweepReport(
                                acquired=False,
                                skipped_reason="lock_busy",
                            )
                    if (
                        not force
                        and min_interval_seconds > 0
                        and state.get("last_completed_at") is not None
                    ):
                        last_done = state["last_completed_at"]
                        if last_done.tzinfo is None:
                            last_done = last_done.replace(tzinfo=UTC)
                        elapsed = (db_now - last_done).total_seconds()
                        if elapsed < min_interval_seconds:
                            return HealthSweepReport(
                                acquired=True,
                                skipped_reason="interval_not_elapsed",
                            )

                expected_gen = int(state["sweep_gen"]) if state and state.get("sweep_gen") is not None else None
                claimed_sweep_gen = await db.update_health_sweep_state(
                    started=True,
                    worker_id=worker_id,
                    claim_lease_seconds=eff_lease_seconds,
                    expected_sweep_gen=expected_gen,
                    conn=tx_conn,
                )
                if claimed_sweep_gen is None:
                    logger.warning(
                        "[integration-sweep] failed to claim sweep_gen; exiting immediately"
                    )
                    return HealthSweepReport(
                        acquired=False,
                        skipped_reason="claim_failed",
                    )

                connections = await db.list_connections_for_health_sweep(
                    limit=limit, conn=tx_conn
                )
                orphaned_revoked = await db.list_orphaned_revoked_purging_connections(
                    limit=limit, conn=tx_conn
                )
    except Exception as exc:
        logger.warning(
            "[integration-sweep] sweep claim/snapshot failed (%s); exiting immediately",
            type(exc).__name__,
        )
        return HealthSweepReport(
            acquired=False,
            skipped_reason="claim_failed",
        )

    assert claimed_sweep_gen is not None

    # Step 2: OUTSIDE global lock and DB pool connection — renew lease before each unit of work
    checked = 0
    healthy = 0
    marked_needs_reauth = 0
    triggered_revocation_purge = 0
    transient_errors = 0
    skipped_credentials_not_ready = 0
    skipped_unexpired_ambiguous = 0
    skipped_concurrent = 0
    completed_ok = False
    lease_lost = False

    try:
        try:
            (
                expired_states,
                expired_confirms,
            ) = await db.delete_expired_oauth_states_and_confirmations()
        except Exception as exc:
            logger.warning(
                "[integration-sweep] expired state cleanup failed (%s)",
                type(exc).__name__,
            )

        # Repair any orphaned revoked_purging rows lacking a running 2A purge ledger (§4.2(2))
        for orphan in orphaned_revoked:
            if not await _renew_sweep_lease_or_false(
                lock_key=lock_key,
                expected_sweep_gen=claimed_sweep_gen,
                worker_id=worker_id,
                lease_seconds=eff_lease_seconds,
            ):
                lease_lost = True
                break
            checked += 1
            try:
                pid = await begin_disconnect(
                    orphan.user_id,
                    orphan.provider,
                    orphan.service,
                    trigger_reason=TriggerReason.ERROR_CODE_REVOKED,
                    remote_mode="local_only",
                    expected_auth_epoch=orphan.auth_epoch,
                    clear_tokens=True,
                    skip_if_running_ledger=True,
                )
                await finish_disconnect(pid, revoker=None)
                triggered_revocation_purge += 1
            except (IntegrationNotFoundError, DisconnectTargetMismatchError):
                skipped_concurrent += 1
            except Exception as exc:
                transient_errors += 1
                logger.warning(
                    "[integration-sweep] orphaned revoked_purging repair failed for %s/%s (%s)",
                    orphan.provider,
                    orphan.service,
                    type(exc).__name__,
                )

        # Group by shared credential so Google calendar + mail on the same (client, sub)
        # issue at most ONE upstream refresh request per sweep (§4.2(2), §4.5(3)).
        now = datetime.now(UTC)
        seen_cred_groups: set[tuple[str, ...]] = set()
        if not lease_lost:
            for conn_row in connections:
                if not _is_provider_credentials_ready(conn_row.provider):
                    skipped_credentials_not_ready += 1
                    continue

                if conn_row.status == IntegrationStatus.REFRESH_AMBIGUOUS:
                    exp = conn_row.expires_at
                    if exp is not None:
                        exp_utc = exp if exp.tzinfo is not None else exp.replace(tzinfo=UTC)
                        if exp_utc > now:
                            skipped_unexpired_ambiguous += 1
                            continue

                if conn_row.provider == "google":
                    cred_group_key: tuple[str, ...] = (
                        conn_row.user_id,
                        "google",
                        conn_row.oauth_client_id,
                        conn_row.provider_account_id,
                    )
                else:
                    cred_group_key = (
                        conn_row.user_id,
                        conn_row.provider,
                        conn_row.oauth_client_id,
                        conn_row.provider_account_id,
                        conn_row.service,
                    )
                if cred_group_key in seen_cred_groups:
                    continue
                seen_cred_groups.add(cred_group_key)

                # Renew and verify sweep_gen BEFORE starting each credential group!
                if not await _renew_sweep_lease_or_false(
                    lock_key=lock_key,
                    expected_sweep_gen=claimed_sweep_gen,
                    worker_id=worker_id,
                    lease_seconds=eff_lease_seconds,
                ):
                    lease_lost = True
                    break

                checked += 1
                try:
                    await ensure_fresh_access_token(
                        conn_row.user_id,
                        conn_row.provider,
                        conn_row.service,
                        min_remaining_seconds=600,
                        force_refresh=True,
                        google_client=google_client,
                        feishu_client=feishu_client,
                        revoker=revoker,
                    )
                    healthy += 1
                except IntegrationCredentialsNotReadyError:
                    skipped_credentials_not_ready += 1
                except IntegrationNeedsReauthError:
                    marked_needs_reauth += 1
                except IntegrationRevokedError:
                    triggered_revocation_purge += 1
                except IntegrationTransientError:
                    transient_errors += 1
                except (
                    IntegrationRefreshBusyError,
                    IntegrationEpochRevokedError,
                    IntegrationNotConnectedError,
                ):
                    skipped_concurrent += 1
                except Exception as exc:
                    transient_errors += 1
                    logger.warning(
                        "[integration-sweep] unexpected error checking %s/%s (%s)",
                        conn_row.provider,
                        conn_row.service,
                        type(exc).__name__,
                    )

        if lease_lost:
            logger.warning(
                "[integration-sweep] lost sweep lease (sweep_gen=%s); stopping early after checked=%d",
                claimed_sweep_gen,
                checked,
            )
            return HealthSweepReport(
                acquired=True,
                skipped_reason="lease_lost",
                checked=checked,
                healthy=healthy,
                marked_needs_reauth=marked_needs_reauth,
                triggered_revocation_purge=triggered_revocation_purge,
                transient_errors=transient_errors,
                skipped_credentials_not_ready=skipped_credentials_not_ready,
                skipped_unexpired_ambiguous=skipped_unexpired_ambiguous,
                skipped_concurrent=skipped_concurrent,
                expired_states_deleted=expired_states,
                expired_confirmations_deleted=expired_confirms,
            )

        # Step 3: short pg_advisory_xact_lock on ONE connection — verify sweep_gen & stamp last_completed_at
        try:
            async with get_db_connection() as commit_conn, commit_conn.transaction():
                await commit_conn.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (lock_key,)
                )
                with track_advisory_lock("health_sweep", lock_key):
                    final_gen = await db.update_health_sweep_state(
                        completed=True,
                        worker_id=worker_id,
                        expected_sweep_gen=claimed_sweep_gen,
                        conn=commit_conn,
                    )
                    if final_gen is None:
                        lease_lost = True
                    else:
                        completed_ok = True
        except Exception as exc:
            logger.warning(
                "[integration-sweep] could not stamp last_completed_at (%s)",
                type(exc).__name__,
            )

        return HealthSweepReport(
            acquired=True,
            skipped_reason="lease_lost" if lease_lost else None,
            checked=checked,
            healthy=healthy,
            marked_needs_reauth=marked_needs_reauth,
            triggered_revocation_purge=triggered_revocation_purge,
            transient_errors=transient_errors,
            skipped_credentials_not_ready=skipped_credentials_not_ready,
            skipped_unexpired_ambiguous=skipped_unexpired_ambiguous,
            skipped_concurrent=skipped_concurrent,
            expired_states_deleted=expired_states,
            expired_confirmations_deleted=expired_confirms,
        )
    finally:
        if not completed_ok and not lease_lost and claimed_sweep_gen is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await db.update_health_sweep_state(
                        clear_inflight=True,
                        expected_sweep_gen=claimed_sweep_gen,
                    )
                except Exception:
                    pass


async def run_integration_health_sweep_loop(
    *,
    poll_interval_s: float = float(HEALTH_SWEEP_INTERVAL_SECONDS),
    initial_delay_s: float = 60.0,
) -> None:
    """Background loop invoking ``run_integration_health_sweep_once`` every 12 hours."""
    logger.info(
        "[integration-sweep] health patrol loop started (interval_s=%.0f)",
        poll_interval_s,
    )
    if initial_delay_s > 0:
        await asyncio.sleep(initial_delay_s)
    while True:
        try:
            report = await run_integration_health_sweep_once()
            if report.acquired and report.skipped_reason is None:
                logger.info(
                    "[integration-sweep] completed checked=%d healthy=%d needs_reauth=%d revoked=%d transient=%d skipped_creds=%d",
                    report.checked,
                    report.healthy,
                    report.marked_needs_reauth,
                    report.triggered_revocation_purge,
                    report.transient_errors,
                    report.skipped_credentials_not_ready,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("[integration-sweep] loop iteration failed", exc_info=True)
        await asyncio.sleep(poll_interval_s)


__all__ = [
    "HealthSweepReport",
    "run_integration_health_sweep_loop",
    "run_integration_health_sweep_once",
]
