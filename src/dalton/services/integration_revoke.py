"""Remote revocation task state machine for personal integrations (§4.2(3)).

Step 1 defines the ``RemoteRevoker`` protocol and the full cross-worker
PostgreSQL-backed state machine without performing real upstream HTTP calls.
Step 2 injects the concrete Google OAuth revocation client.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import logging
from typing import Protocol, runtime_checkable

import anyio
from psycopg.rows import dict_row

from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    RemoteRevokeStatus,
    extract_google_project,
    revoke_lock_key,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 3600


class RevokeOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    RETRYABLE = "retryable"
    PERMANENT_FAILURE = "permanent_failure"


@runtime_checkable
class RemoteRevoker(Protocol):
    async def revoke(
        self, *, provider: str, oauth_client_id: str, token: str
    ) -> RevokeOutcome: ...


def compute_revoke_backoff_seconds(retry_count: int) -> int:
    """Exponential backoff in seconds (30s, 60s, 120s, ..., capped at 3600s)."""
    exp = max(0, int(retry_count))
    return min(_BACKOFF_BASE_SECONDS * (2**exp), _BACKOFF_MAX_SECONDS)


async def _has_newer_active_google_connection(
    *,
    user_id: str,
    google_project: str,
    sub: str,
    auth_epoch_upto: int,
) -> bool:
    """Return True if ``(user_id, google_project, sub)`` already holds a newer active token."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT oauth_client_id
                FROM dalton_user_integrations
                WHERE user_id = %s
                  AND provider = 'google'
                  AND provider_account_id = %s
                  AND status IN ('connected', 'refresh_ambiguous')
                  AND auth_epoch > %s
                """,
                (user_id, sub, int(auth_epoch_upto)),
            )
            rows = await cur.fetchall()
    return any(
        extract_google_project(str(r["oauth_client_id"])) == google_project
        for r in rows
    )


async def process_revoke(
    purge_id: str,
    revoker: RemoteRevoker,
) -> RemoteRevokeStatus:
    """Execute one remote revocation attempt under the ``(user_id, google_project, sub)`` lock.

    - ``succeeded``: wipes ``encrypted_revoke_token``, sets ``remote_completed_at``.
    - ``retryable``: sets ``failed_retryable``, increments ``retry_count``, schedules exponential backoff ``next_retry_at``.
    - ``permanent_failure``: wipes ``encrypted_revoke_token``, marks ``superseded``.
    - If a concurrent worker already transitioned the task or a newer token has been
      issued for the same ``(google_project, sub)``, skips calling ``revoker`` and converges cleanly.
    """
    initial = await db.get_purge_ledger(purge_id, decrypt_revoke_token=False)
    if initial is None:
        raise LookupError(f"Purge ledger {purge_id} not found")
    if initial.remote_revoke_status not in (
        RemoteRevokeStatus.PENDING,
        RemoteRevokeStatus.FAILED_RETRYABLE,
    ):
        return initial.remote_revoke_status

    google_project = extract_google_project(initial.oauth_client_id)
    lock_key = revoke_lock_key(
        initial.user_id, google_project, initial.provider_account_id
    )

    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        try:
            row = await db.get_purge_ledger(purge_id, decrypt_revoke_token=True)
            if row is None:
                raise LookupError(f"Purge ledger {purge_id} not found")
            if row.remote_revoke_status not in (
                RemoteRevokeStatus.PENDING,
                RemoteRevokeStatus.FAILED_RETRYABLE,
            ):
                return row.remote_revoke_status

            if row.provider == "google" and await _has_newer_active_google_connection(
                user_id=row.user_id,
                google_project=google_project,
                sub=row.provider_account_id,
                auth_epoch_upto=row.auth_epoch_upto,
            ):
                await db.update_purge_ledger_remote_status(
                    purge_id,
                    remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                    clear_revoke_token=True,
                    next_retry_at=None,
                    last_remote_error="superseded_by_newer_authorization",
                    mark_remote_completed=True,
                )
                return RemoteRevokeStatus.SUPERSEDED

            token = row.revoke_token
            if not token:
                await db.update_purge_ledger_remote_status(
                    purge_id,
                    remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                    clear_revoke_token=True,
                    next_retry_at=None,
                    last_remote_error="missing_revoke_token",
                    mark_remote_completed=True,
                )
                return RemoteRevokeStatus.SUPERSEDED

            try:
                outcome = await revoker.revoke(
                    provider=row.provider,
                    oauth_client_id=row.oauth_client_id,
                    token=token,
                )
            except Exception as exc:
                logger.warning(
                    "[integration-revoke] revoker raised unexpected error for purge_id=%s: %s",
                    purge_id,
                    type(exc).__name__,
                )
                outcome = RevokeOutcome.RETRYABLE

            if outcome == RevokeOutcome.SUCCEEDED:
                await db.update_purge_ledger_remote_status(
                    purge_id,
                    remote_revoke_status=RemoteRevokeStatus.SUCCEEDED,
                    clear_revoke_token=True,
                    next_retry_at=None,
                    last_remote_error=None,
                    mark_remote_completed=True,
                )
                return RemoteRevokeStatus.SUCCEEDED

            if outcome == RevokeOutcome.RETRYABLE:
                delay_s = compute_revoke_backoff_seconds(row.retry_count)
                next_retry = datetime.now(UTC) + timedelta(seconds=delay_s)
                await db.update_purge_ledger_remote_status(
                    purge_id,
                    remote_revoke_status=RemoteRevokeStatus.FAILED_RETRYABLE,
                    clear_revoke_token=False,
                    increment_retry=True,
                    next_retry_at=next_retry,
                    last_remote_error="retryable_remote_revoke_failure",
                    mark_remote_completed=False,
                )
                return RemoteRevokeStatus.FAILED_RETRYABLE

            # PERMANENT_FAILURE -> clear ciphertext & mark superseded
            await db.update_purge_ledger_remote_status(
                purge_id,
                remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                clear_revoke_token=True,
                next_retry_at=None,
                last_remote_error="permanent_remote_revoke_failure",
                mark_remote_completed=True,
            )
            return RemoteRevokeStatus.SUPERSEDED
        finally:
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def supersede_pending_revokes(
    user_id: str,
    google_project: str,
    sub: str,
) -> int:
    """Drain any in-flight revoke and supersede all pending/retryable tasks for ``(user_id, google_project, sub)``.

    Called by Step 2 after Google issues a new token so stale revocation tasks
    cannot revoke the newly issued grant in the same Google Cloud project.
    """
    lock_key = revoke_lock_key(user_id, google_project, sub)
    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        try:
            active = await db.list_active_revokes_for_google_project_sub(
                user_id, google_project, sub, decrypt_revoke_token=False
            )
            count = 0
            for row in active:
                updated = await db.update_purge_ledger_remote_status(
                    row.purge_id,
                    remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                    clear_revoke_token=True,
                    next_retry_at=None,
                    last_remote_error="superseded_by_new_authorization",
                    mark_remote_completed=True,
                )
                if updated is not None:
                    count += 1
            return count
        finally:
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def revoke_before_new_authorization(
    user_id: str,
    google_project: str,
    sub: str,
    revoker: RemoteRevoker,
) -> dict[str, RemoteRevokeStatus]:
    """Synchronously attempt once to revoke pending old grants before starting a new Google OAuth flow.

    Acquires the shared ``(user_id, google_project, sub)`` lock to drain any in-flight
    revocation. Any failure (retryable or permanent) clears ciphertext and immediately
    marks ``superseded`` so no background retry can later revoke the newly authorized grant.
    """
    lock_key = revoke_lock_key(user_id, google_project, sub)
    results: dict[str, RemoteRevokeStatus] = {}
    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        try:
            active = await db.list_active_revokes_for_google_project_sub(
                user_id, google_project, sub, decrypt_revoke_token=True
            )
            for row in active:
                if not row.revoke_token:
                    await db.update_purge_ledger_remote_status(
                        row.purge_id,
                        remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                        clear_revoke_token=True,
                        next_retry_at=None,
                        last_remote_error="missing_revoke_token",
                        mark_remote_completed=True,
                    )
                    results[row.purge_id] = RemoteRevokeStatus.SUPERSEDED
                    continue
                try:
                    outcome = await revoker.revoke(
                        provider=row.provider,
                        oauth_client_id=row.oauth_client_id,
                        token=row.revoke_token,
                    )
                except Exception as exc:
                    logger.warning(
                        "[integration-revoke] pre-auth revoke raised %s for purge_id=%s; marking superseded",
                        type(exc).__name__,
                        row.purge_id,
                    )
                    outcome = RevokeOutcome.PERMANENT_FAILURE

                if outcome == RevokeOutcome.SUCCEEDED:
                    await db.update_purge_ledger_remote_status(
                        row.purge_id,
                        remote_revoke_status=RemoteRevokeStatus.SUCCEEDED,
                        clear_revoke_token=True,
                        next_retry_at=None,
                        last_remote_error=None,
                        mark_remote_completed=True,
                    )
                    results[row.purge_id] = RemoteRevokeStatus.SUCCEEDED
                else:
                    await db.update_purge_ledger_remote_status(
                        row.purge_id,
                        remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                        clear_revoke_token=True,
                        next_retry_at=None,
                        last_remote_error=f"pre_auth_revoke_{outcome.value}",
                        mark_remote_completed=True,
                    )
                    results[row.purge_id] = RemoteRevokeStatus.SUPERSEDED
            return results
        finally:
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def run_due_remote_revokes_once(
    revoker: RemoteRevoker,
    *,
    limit: int = 50,
) -> dict[str, RemoteRevokeStatus]:
    """Process one batch of due remote revocation retries (``next_retry_at <= NOW()``)."""
    due = await db.list_due_remote_revokes(limit=limit)
    outcomes: dict[str, RemoteRevokeStatus] = {}
    for row in due:
        outcomes[row.purge_id] = await process_revoke(row.purge_id, revoker)
    return outcomes


async def run_integration_revoke_retry_loop(
    revoker: RemoteRevoker | None = None,
    *,
    poll_interval_s: float = 60.0,
) -> None:
    """Background loop for due remote revocation retries.

    Does not start when no ``RemoteRevoker`` is injected (Step 1 has no concrete HTTP revoker).
    """
    if revoker is None:
        logger.info(
            "[integration-revoke] retry loop not started: no RemoteRevoker configured"
        )
        return
    logger.info(
        "[integration-revoke] retry loop started (poll_interval_s=%.1f)",
        poll_interval_s,
    )
    while True:
        try:
            await run_due_remote_revokes_once(revoker)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[integration-revoke] retry loop iteration failed", exc_info=True
            )
        await asyncio.sleep(poll_interval_s)
