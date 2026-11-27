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


async def run_integration_health_sweep_once(
    *,
    force: bool = False,
    min_interval_seconds: int = HEALTH_SWEEP_INTERVAL_SECONDS,
    limit: int = 500,
    google_client: GoogleOAuthClientProtocol | None = None,
    feishu_client: FeishuOAuthClientProtocol | None = None,
    revoker: RemoteRevoker | None = None,
) -> HealthSweepReport:
    """Run one cluster-wide mutually-exclusive personal integration health sweep (§4.5(3))."""
    lock_key = health_sweep_lock_key()
    async with get_db_connection() as lock_conn:
        async with lock_conn.cursor() as cur:
            await cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
            row = await cur.fetchone()
            acquired = bool(row and row[0])
        if not acquired:
            return HealthSweepReport(acquired=False, skipped_reason="lock_busy")

        try:
            # Check persistent 12-hour interval across worker restarts / staggered boots
            if not force and min_interval_seconds > 0:
                try:
                    state = await db.read_health_sweep_state()
                except Exception:
                    state = None
                if state and state.get("last_completed_at") is not None:
                    last_done = state["last_completed_at"]
                    db_now = state.get("db_now") or datetime.now(UTC)
                    if last_done.tzinfo is None:
                        last_done = last_done.replace(tzinfo=UTC)
                    if db_now.tzinfo is None:
                        db_now = db_now.replace(tzinfo=UTC)
                    elapsed = (db_now - last_done).total_seconds()
                    if elapsed < min_interval_seconds:
                        return HealthSweepReport(
                            acquired=True,
                            skipped_reason="interval_not_elapsed",
                        )

            worker_id = _worker_identity()
            try:
                await db.update_health_sweep_state(started=True, worker_id=worker_id)
            except Exception as exc:
                logger.warning(
                    "[integration-sweep] could not stamp last_started_at (%s)",
                    type(exc).__name__,
                )

            expired_states = 0
            expired_confirms = 0
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

            connections = await db.list_connections_for_health_sweep(limit=limit)
            checked = 0
            healthy = 0
            marked_needs_reauth = 0
            triggered_revocation_purge = 0
            transient_errors = 0
            skipped_credentials_not_ready = 0
            skipped_unexpired_ambiguous = 0
            skipped_concurrent = 0

            # Repair any orphaned revoked_purging rows lacking a running 2A purge ledger (§4.2(2))
            orphaned_revoked: list[db.IntegrationRow] = []
            try:
                orphaned_revoked = await db.list_orphaned_revoked_purging_connections(
                    limit=limit
                )
            except Exception as exc:
                logger.warning(
                    "[integration-sweep] orphaned revoked_purging scan failed (%s)",
                    type(exc).__name__,
                )
            for orphan in orphaned_revoked:
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
            for conn_row in connections:
                # Never misclassify existing connections when deployment credentials are not ready
                if not _is_provider_credentials_ready(conn_row.provider):
                    skipped_credentials_not_ready += 1
                    continue

                # For refresh_ambiguous rows whose access token has NOT expired yet, wait until expiry
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

            try:
                await db.update_health_sweep_state(completed=True, worker_id=worker_id)
            except Exception as exc:
                logger.warning(
                    "[integration-sweep] could not stamp last_completed_at (%s)",
                    type(exc).__name__,
                )

            return HealthSweepReport(
                acquired=True,
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
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


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
