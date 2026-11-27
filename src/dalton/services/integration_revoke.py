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

from src.dalton.database import user_integrations as db
from src.dalton.database.user_integrations import (
    LocalPurgeStatus,
    RemoteRevokeStatus,
    extract_google_project,
    google_project_coord_lock_key,
    revoke_lock_key,
)
from src.dalton.integrations.lock_guard import (
    assert_no_advisory_lock_held,
    track_advisory_lock,
)
from src.server.database.pool import get_db_connection

logger = logging.getLogger(__name__)

_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 3600
_REVOKE_INFLIGHT_LEASE_SECONDS = 30
_BACKGROUND_REVOKE_TASKS: set[asyncio.Task[object]] = set()


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
    google_project: str,
    sub: str,
    conn: object = None,
) -> bool:
    """Return True if ``(google_project, sub)`` holds any active or pending grant across ANY Dalton user."""
    if conn is not None:
        return await db.has_active_or_pending_google_grant(
            google_project,
            sub,
            conn=conn,
        )
    async with get_db_connection() as db_conn:
        return await db.has_active_or_pending_google_grant(
            google_project,
            sub,
            conn=db_conn,
        )


async def process_revoke(
    purge_id: str,
    revoker: RemoteRevoker,
) -> RemoteRevokeStatus:
    """Execute one remote revocation attempt coordinated by ``(google_project, sub)`` without holding DB locks across HTTP.

    Phase 1 (short ``revoke_lock_key(google_project, sub)``):
      - Verifies local 2A purges for ``(user_id, provider)`` have completed (local-first).
      - Checks cross-user active/pending Google grants for ``(google_project, sub)`` -> ``superseded``.
      - Checks active Google OAuth states / in-flight code exchanges in ``google_project`` -> defer.
      - Claims a monotonic ``revoke_attempt_gen`` + short ``revoke_inflight_until`` lease, then RELEASES the lock.
    Phase 2 (outside any DB lock or pool connection):
      - Invokes ``revoker.revoke(...)``.
    Phase 3 (short ``revoke_lock_key(google_project, sub)``):
      - Verifies ``revoke_attempt_gen`` matches and commits ``succeeded`` / ``failed_retryable`` / ``superseded``.
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
    lock_key = revoke_lock_key(google_project, initial.provider_account_id)
    coord_key = (
        google_project_coord_lock_key(google_project)
        if initial.provider == "google"
        else None
    )

    attempt_gen: int | None = None
    token_to_revoke: str | None = None
    oauth_client_id = initial.oauth_client_id
    provider = initial.provider
    retry_count_before = initial.retry_count

    # Phase 1: short lock — check guards & claim in-flight revoke attempt generation.
    # Lock order: revoke_lock_key(project, sub) -> google_project_coord_lock_key(project).
    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        coord_locked = False
        try:
            with track_advisory_lock("revoke", lock_key):
                if coord_key is not None:
                    await lock_conn.execute("SELECT pg_advisory_lock(%s)", (coord_key,))
                    coord_locked = True
                row = await db.get_purge_ledger(
                    purge_id, decrypt_revoke_token=True, conn=lock_conn
                )
                if row is None:
                    raise LookupError(f"Purge ledger {purge_id} not found")
                if row.remote_revoke_status not in (
                    RemoteRevokeStatus.PENDING,
                    RemoteRevokeStatus.FAILED_RETRYABLE,
                ):
                    return row.remote_revoke_status

                # Local-first invariant: never call upstream revoke while any local 2A purge
                # for (user_id, provider) is still running.
                if (
                    row.local_purge_status == LocalPurgeStatus.RUNNING
                    or await db.has_running_local_purge_for_user_provider(
                        row.user_id, row.provider, conn=lock_conn
                    )
                ):
                    defer_until = datetime.now(UTC) + timedelta(seconds=5)
                    await db.update_purge_ledger_remote_status(
                        purge_id,
                        remote_revoke_status=row.remote_revoke_status,
                        clear_revoke_token=False,
                        increment_retry=False,
                        next_retry_at=defer_until,
                        last_remote_error="deferred_local_purge_running",
                        mark_remote_completed=False,
                        conn=lock_conn,
                    )
                    return row.remote_revoke_status

                if row.provider == "google" and await _has_newer_active_google_connection(
                    google_project=google_project,
                    sub=row.provider_account_id,
                    conn=lock_conn,
                ):
                    await db.update_purge_ledger_remote_status(
                        purge_id,
                        remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                        clear_revoke_token=True,
                        next_retry_at=None,
                        last_remote_error="superseded_by_newer_authorization",
                        mark_remote_completed=True,
                        conn=lock_conn,
                    )
                    return RemoteRevokeStatus.SUPERSEDED

                if row.provider == "google":
                    # At most ONE in-flight revoke HTTP attempt per (google_project, sub)
                    if await db.has_inflight_google_revoke_for_project_sub(
                        google_project,
                        row.provider_account_id,
                        conn=lock_conn,
                    ):
                        return row.remote_revoke_status

                    active_oauth_exp = await db.get_active_google_oauth_state_expiry(
                        google_project,
                        sub=row.provider_account_id,
                        conn=lock_conn,
                    )
                    if active_oauth_exp is not None:
                        exp_utc = (
                            active_oauth_exp
                            if active_oauth_exp.tzinfo is not None
                            else active_oauth_exp.replace(tzinfo=UTC)
                        )
                        defer_until = max(
                            datetime.now(UTC) + timedelta(seconds=5),
                            exp_utc + timedelta(seconds=5),
                        )
                        await db.update_purge_ledger_remote_status(
                            purge_id,
                            remote_revoke_status=RemoteRevokeStatus.FAILED_RETRYABLE,
                            clear_revoke_token=False,
                            increment_retry=False,
                            next_retry_at=defer_until,
                            last_remote_error="deferred_for_active_oauth_flow",
                            mark_remote_completed=False,
                            conn=lock_conn,
                        )
                        return RemoteRevokeStatus.FAILED_RETRYABLE

                token_to_revoke = row.revoke_token
                if not token_to_revoke:
                    await db.update_purge_ledger_remote_status(
                        purge_id,
                        remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                        clear_revoke_token=True,
                        next_retry_at=None,
                        last_remote_error="missing_revoke_token",
                        mark_remote_completed=True,
                        conn=lock_conn,
                    )
                    return RemoteRevokeStatus.SUPERSEDED

                attempt_gen = await db.claim_revoke_attempt_tx(
                    purge_id,
                    lease_seconds=_REVOKE_INFLIGHT_LEASE_SECONDS,
                    conn=lock_conn,
                )
                if attempt_gen is None:
                    return row.remote_revoke_status
                oauth_client_id = row.oauth_client_id
                provider = row.provider
                retry_count_before = row.retry_count
        finally:
            with anyio.CancelScope(shield=True):
                if coord_locked and coord_key is not None:
                    await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (coord_key,))
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))

    # Phase 2: external HTTP revoke OUTSIDE any PostgreSQL lock or pool connection
    assert attempt_gen is not None and token_to_revoke is not None
    committed = False
    try:
        try:
            assert_no_advisory_lock_held(f"{provider}.revoke")
            outcome = await revoker.revoke(
                provider=provider,
                oauth_client_id=oauth_client_id,
                token=token_to_revoke,
            )
        except Exception as exc:
            logger.warning(
                "[integration-revoke] revoker raised unexpected error for purge_id=%s: %s",
                purge_id,
                type(exc).__name__,
            )
            outcome = RevokeOutcome.RETRYABLE

        # Phase 3: short lock — verify attempt_gen and commit outcome
        async with get_db_connection() as commit_conn:
            await commit_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
            try:
                with track_advisory_lock("revoke", lock_key):
                    if outcome == RevokeOutcome.SUCCEEDED:
                        updated = await db.finalize_revoke_attempt_tx(
                            purge_id,
                            expected_attempt_gen=attempt_gen,
                            remote_revoke_status=RemoteRevokeStatus.SUCCEEDED,
                            clear_revoke_token=True,
                            next_retry_at=None,
                            last_remote_error=None,
                            mark_remote_completed=True,
                            conn=commit_conn,
                        )
                    elif outcome == RevokeOutcome.RETRYABLE:
                        delay_s = compute_revoke_backoff_seconds(retry_count_before)
                        next_retry = datetime.now(UTC) + timedelta(seconds=delay_s)
                        updated = await db.finalize_revoke_attempt_tx(
                            purge_id,
                            expected_attempt_gen=attempt_gen,
                            remote_revoke_status=RemoteRevokeStatus.FAILED_RETRYABLE,
                            clear_revoke_token=False,
                            increment_retry=True,
                            next_retry_at=next_retry,
                            last_remote_error="retryable_remote_revoke_failure",
                            mark_remote_completed=False,
                            conn=commit_conn,
                        )
                    else:
                        updated = await db.finalize_revoke_attempt_tx(
                            purge_id,
                            expected_attempt_gen=attempt_gen,
                            remote_revoke_status=RemoteRevokeStatus.SUPERSEDED,
                            clear_revoke_token=True,
                            next_retry_at=None,
                            last_remote_error="permanent_remote_revoke_failure",
                            mark_remote_completed=True,
                            conn=commit_conn,
                        )
                    committed = True
                    if updated is not None:
                        return updated.remote_revoke_status
                    latest = await db.get_purge_ledger(
                        purge_id, decrypt_revoke_token=False, conn=commit_conn
                    )
                    return (
                        latest.remote_revoke_status
                        if latest is not None
                        else RemoteRevokeStatus.SUPERSEDED
                    )
            finally:
                with anyio.CancelScope(shield=True):
                    await commit_conn.execute(
                        "SELECT pg_advisory_unlock(%s)", (lock_key,)
                    )
    finally:
        if not committed:
            with anyio.CancelScope(shield=True):
                await db.release_revoke_inflight_lease(
                    purge_id, expected_attempt_gen=attempt_gen
                )


async def supersede_pending_revokes(
    google_project: str,
    sub: str,
) -> int:
    """Supersede all pending/retryable tasks for ``(google_project, sub)`` across all Dalton users.

    Called after Google issues a new token so stale revocation tasks cannot revoke
    the newly issued grant in the same Google Cloud project.
    """
    lock_key = revoke_lock_key(google_project, sub)
    async with get_db_connection() as lock_conn:
        await lock_conn.execute("SELECT pg_advisory_lock(%s)", (lock_key,))
        try:
            with track_advisory_lock("revoke", lock_key):
                active = await db.list_active_revokes_for_google_project_sub(
                    google_project, sub, decrypt_revoke_token=False, conn=lock_conn
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
                        conn=lock_conn,
                    )
                    if updated is not None:
                        count += 1
                return count
        finally:
            with anyio.CancelScope(shield=True):
                await lock_conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))


async def revoke_before_new_authorization(
    google_project: str,
    sub: str,
    revoker: RemoteRevoker,
    *,
    user_id: str | None = None,
) -> dict[str, RemoteRevokeStatus]:
    """Attempt once to revoke pending old grants for ``(google_project, sub)`` before starting a new Google OAuth flow.

    Delegates each active ledger to ``process_revoke`` so:
    - No PostgreSQL advisory lock or pool connection is held across upstream HTTP.
    - Retryable failures preserve the encrypted revoke token and remain in the retry queue
      (``failed_retryable``), instead of being prematurely marked ``superseded`` before a
      new authorization actually exists.
    - Permanent failures clear ciphertext and mark ``superseded``.
    """
    active = await db.list_active_revokes_for_google_project_sub(
        google_project, sub, user_id=user_id, decrypt_revoke_token=False
    )
    results: dict[str, RemoteRevokeStatus] = {}
    for row in active:
        results[row.purge_id] = await process_revoke(row.purge_id, revoker)
    return results


async def _run_background_revoke_safe(purge_id: str, revoker: RemoteRevoker) -> None:
    try:
        await process_revoke(purge_id, revoker)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "[integration-revoke] background revoke failed for purge_id=%s (%s); retry loop will pick it up",
            purge_id,
            type(exc).__name__,
        )


def schedule_background_remote_revokes(
    purge_ids: list[str],
    revoker: RemoteRevoker,
) -> list[asyncio.Task[object]]:
    """Schedule independent background tasks for remote Google revocations (§4.2(3)).

    Allows full-provider disconnects to complete all local 2A purges and unblock
    shared threads before any slow/failing upstream HTTP revoke runs.
    """
    tasks: list[asyncio.Task[object]] = []
    for pid in purge_ids:
        task: asyncio.Task[object] = asyncio.create_task(
            _run_background_revoke_safe(pid, revoker),
            name=f"dalton-integration-revoke-{pid[:8]}",
        )
        _BACKGROUND_REVOKE_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_REVOKE_TASKS.discard)
        tasks.append(task)
    return tasks


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
