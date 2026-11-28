"""DB-backed capacity backoff retry and pre-START thread status reset for automations.

When many automations fire in the same minute and hit transient server-side
capacity exhaustion (``WriterGuardUnavailable``, quota burst 429, or credit-gate
503), parking the execution in Postgres with ``status='waiting'`` and
``wait_reason='capacity_limit'`` lets every worker's scheduler sweep re-dispatch
the *same* execution row once its exponential backoff window elapses, up to a
15-minute hard cap (reusing upstream's ``parked_status`` and ``awaits_until``
columns with zero schema migrations).

Multi-worker safety:
- All retry state (attempt count, ``next_retry_at``, ``last_error``,
  ``awaits_until``) lives on the ``automation_executions`` row in Postgres — no
  in-process dicts or timers.
- Releasing a due parked row uses one guarded ``UPDATE ... WHERE status='waiting'
  AND wait_reason='capacity_limit' ...`` so concurrent sweeps across workers
  dispatch the execution at most once.
- Expiring a row past ``awaits_until`` uses ``settle(..., from_statuses=('waiting',))``
  under ``FOR UPDATE``, and ``release_capacity_parked_execution`` refuses any
  row with ``awaits_until <= NOW()``.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.settings import is_automation_capacity_retry_enabled
from src.server.database import automation as auto_db
from src.server.database.pool import get_db_connection
from src.server.services.automation_computer_wait import stopped_meanwhile
from src.server.services.automation_outcomes import Outcome, clean_error_text
from src.server.services.automation_settlement import settle
from src.server.services.writer_guard import WriterGuardUnavailable

logger = logging.getLogger(__name__)

CAPACITY_WAIT_REASON = "capacity_limit"
CAPACITY_RETRY_KEY = "capacity_retry"

DEFAULT_INITIAL_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_BACKOFF_SECONDS = 240.0
DEFAULT_MAX_WAIT_SECONDS = 15 * 60  # 15-minute total wait cap

_CAPACITY_429_TYPES = frozenset({"burst_limit", "service_unavailable"})

_ERROR_OUTCOMES = frozenset(
    {
        Outcome.FAILED,
        Outcome.FAILED_OURS,
        Outcome.KEY_REJECTED,
        Outcome.LIMITED,
        Outcome.PLAN_LIMITED,
        Outcome.SUBSCRIPTION_LIMITED,
        Outcome.TIMEOUT,
    }
)


def is_capacity_retryable_error(exc: BaseException) -> bool:
    """Return True for transient server-capacity errors eligible for backoff retry.

    Covers:
    - ``WriterGuardUnavailable`` (pinned-session writer pool exhausted / lock contended)
    - Quota-service burst / unavailable HTTP 429 (``burst_limit``, ``service_unavailable``)
    - Credit-gate / service HTTP 503

    User usage limits (429 ``credit_limit`` / ``weekly_limit``), workspace moves,
    SSH unreachable errors, and normal automation failures return False.
    """
    if isinstance(exc, WriterGuardUnavailable):
        return True
    if not isinstance(exc, HTTPException):
        return False
    if exc.status_code == 503:
        return True
    return (
        exc.status_code == 429
        and isinstance(exc.detail, dict)
        and exc.detail.get("type") in _CAPACITY_429_TYPES
    )


def compute_backoff_seconds(
    attempt: int,
    *,
    base_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> float:
    """Exponential backoff (30s, 60s, 120s, 240s...) with random jitter."""
    exp = max(0, int(attempt) - 1)
    raw = min(base_seconds * (2**exp), max_backoff_seconds)
    factor = jitter_fn(0.85, 1.25)
    return max(1.0, round(raw * factor, 3))


def format_capacity_error(exc: BaseException) -> str:
    """Format a capacity exception identically to ``_failure_outcome``."""
    return f"{type(exc).__name__}: {clean_error_text(str(exc))}"


def _fallback_thread_status(outcome: Optional[Outcome]) -> str:
    """Target ``conversation_threads.current_status`` when a thread has no prior runs."""
    if outcome in _ERROR_OUTCOMES:
        return "error"
    if outcome is Outcome.INTERRUPTED:
        return "cancelled"
    return "completed"


async def reset_thread_status_after_prestart_exit(
    thread_id: Optional[str],
    *,
    outcome: Optional[Outcome] = None,
) -> bool:
    """Reset a thread stuck in ``in_progress``/``active`` after a pre-START exit.

    ``ensure_thread`` flips ``conversation_threads.current_status`` to
    ``'in_progress'`` ahead of ``begin_run`` (START). When START never commits
    (e.g. ``WriterGuardUnavailable`` or any pre-START exception), no run row
    exists in ``conversation_responses`` and ``finalize_run`` never runs, leaving
    ``current_status='in_progress'`` indefinitely.

    Guarded by ``NOT EXISTS (... status = 'in_progress')`` so a concurrent live
    turn on a ``continue`` thread is never clobbered. Restores the latest
    committed run's terminal status when one exists, else ``'error'`` /
    ``'cancelled'`` / ``'completed'``.
    """
    if not thread_id:
        return False
    fallback = _fallback_thread_status(outcome)
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE conversation_threads ct
                    SET current_status = COALESCE(
                            (
                                SELECT cr.status
                                FROM conversation_responses cr
                                WHERE cr.conversation_thread_id = ct.conversation_thread_id
                                ORDER BY cr.run_seq DESC
                                LIMIT 1
                            ),
                            %s
                        ),
                        updated_at = NOW()
                    WHERE ct.conversation_thread_id = %s
                      AND ct.current_status IN ('in_progress', 'active')
                      AND NOT EXISTS (
                          SELECT 1
                          FROM conversation_responses cr2
                          WHERE cr2.conversation_thread_id = ct.conversation_thread_id
                            AND cr2.status = 'in_progress'
                      )
                    """,
                    (fallback, str(thread_id)),
                )
                return bool(getattr(cur, "rowcount", 0))
    except RuntimeError as exc:
        if "pool" in str(exc).lower():
            logger.debug(
                "[CAPACITY_RETRY] Skipping thread status reset (DB pool unavailable): "
                "thread_id=%s",
                thread_id,
            )
            return False
        logger.warning(
            "[CAPACITY_RETRY] Failed resetting thread status: thread_id=%s error=%s",
            thread_id,
            exc,
        )
        return False
    except Exception as exc:
        logger.warning(
            "[CAPACITY_RETRY] Failed resetting thread status: thread_id=%s error=%s",
            thread_id,
            exc,
        )
        return False


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def park_execution_for_capacity(
    execution_id: str,
    *,
    parked_status: str,
    error_message: str,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
    base_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> tuple[bool, Optional[str]]:
    """Park a pending/running execution in Postgres for capacity backoff retry.

    Returns ``(parked, recorded_thread_id)``. Returns ``(False, thread_id)``
    when the execution is no longer pending/running, or when the 15-minute wait
    cap (``awaits_until``) has already been reached — in which case the caller
    should settle as ``Outcome.FAILED_OURS``.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT status, awaits_until, parked_status, delivery,
                           conversation_thread_id, NOW() AS db_now
                    FROM automation_executions
                    WHERE automation_execution_id = %s
                      AND status IN ('pending', 'running')
                    FOR UPDATE
                    """,
                    (execution_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return False, None

                recorded_thread = (
                    str(row["conversation_thread_id"])
                    if row.get("conversation_thread_id")
                    else None
                )
                db_now = _ensure_utc(row.get("db_now") or datetime.now(timezone.utc))
                existing_deadline = row.get("awaits_until")
                if existing_deadline is not None:
                    deadline = _ensure_utc(existing_deadline)
                    if db_now >= deadline:
                        return False, recorded_thread
                else:
                    deadline = db_now + timedelta(seconds=max_wait_seconds)

                delivery_obj = row.get("delivery")
                prev_retry = (
                    delivery_obj.get(CAPACITY_RETRY_KEY)
                    if isinstance(delivery_obj, Mapping)
                    else None
                )
                prev_attempt = (
                    int(prev_retry.get("attempt") or 0)
                    if isinstance(prev_retry, Mapping)
                    else 0
                )
                attempt = prev_attempt + 1
                delay_s = compute_backoff_seconds(
                    attempt,
                    base_seconds=base_backoff_seconds,
                    max_backoff_seconds=max_backoff_seconds,
                    jitter_fn=jitter_fn,
                )
                next_retry_at = db_now + timedelta(seconds=delay_s)
                first_parked_at = (
                    str(prev_retry.get("first_parked_at"))
                    if isinstance(prev_retry, Mapping)
                    and prev_retry.get("first_parked_at")
                    else db_now.isoformat()
                )
                retry_payload = {
                    "attempt": attempt,
                    "next_retry_at": next_retry_at.isoformat(),
                    "last_error": error_message,
                    "first_parked_at": first_parked_at,
                }
                await cur.execute(
                    """
                    UPDATE automation_executions
                    SET status = 'waiting',
                        wait_reason = %(wait_reason)s,
                        parked_status = COALESCE(parked_status, %(parked_status)s),
                        awaits_until = COALESCE(awaits_until, %(deadline)s),
                        heartbeat_at = NOW(),
                        delivery = jsonb_set(
                            COALESCE(delivery, '{}'::jsonb),
                            '{capacity_retry}',
                            %(retry_payload)s::jsonb,
                            true
                        )
                    WHERE automation_execution_id = %(execution_id)s
                      AND status IN ('pending', 'running')
                    """,
                    {
                        "execution_id": execution_id,
                        "wait_reason": CAPACITY_WAIT_REASON,
                        "parked_status": parked_status,
                        "deadline": deadline,
                        "retry_payload": Json(retry_payload),
                    },
                )
                if cur.rowcount != 1:
                    return False, recorded_thread
                logger.info(
                    "[CAPACITY_RETRY] Parked execution for retry: "
                    "execution_id=%s attempt=%d delay=%.1fs deadline=%s error=%s",
                    execution_id,
                    attempt,
                    delay_s,
                    deadline.isoformat(),
                    error_message,
                )
                return True, recorded_thread


async def try_park_for_capacity(
    exc: BaseException,
    *,
    automation: Mapping[str, Any],
    execution_id: str,
    thread_id: Optional[str] = None,
) -> bool:
    """Park ``execution_id`` on a capacity failure when the feature switch is enabled.

    Returns True when parked (caller should return without settling).
    Returns False when disabled, not a capacity error, budget exhausted, or
    parking failed (caller falls through to normal settlement).
    """
    if not is_automation_capacity_retry_enabled():
        return False
    if not is_capacity_retryable_error(exc):
        return False
    error_message = format_capacity_error(exc)
    parked_status = str(automation.get("status") or "active")
    try:
        parked, recorded_thread = await park_execution_for_capacity(
            execution_id,
            parked_status=parked_status,
            error_message=error_message,
        )
    except RuntimeError as db_exc:
        if "pool" in str(db_exc).lower():
            logger.debug(
                "[CAPACITY_RETRY] Skipping capacity park (DB pool unavailable): "
                "execution_id=%s",
                execution_id,
            )
            return False
        logger.warning(
            "[CAPACITY_RETRY] Parking failed, falling back to settlement: "
            "execution_id=%s error=%s",
            execution_id,
            db_exc,
        )
        return False
    except Exception as db_exc:
        logger.warning(
            "[CAPACITY_RETRY] Parking failed, falling back to settlement: "
            "execution_id=%s error=%s",
            execution_id,
            db_exc,
        )
        return False
    if not parked:
        return False
    # Release any pre-START 'in_progress' status on the thread while queued.
    await reset_thread_status_after_prestart_exit(
        thread_id or recorded_thread,
        outcome=None,
    )
    return True


async def touch_capacity_parked_executions() -> int:
    """Keep ``heartbeat_at`` fresh for non-expired capacity-parked executions."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions
                SET heartbeat_at = NOW()
                WHERE status = 'waiting'
                  AND wait_reason = %s
                  AND (awaits_until IS NULL OR awaits_until > NOW())
                """,
                (CAPACITY_WAIT_REASON,),
            )
            return int(getattr(cur, "rowcount", 0) or 0)


async def list_capacity_parked_executions(limit: int = 50) -> list[dict[str, Any]]:
    """List capacity-parked executions that are either expired or due for retry."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT e.automation_execution_id,
                       e.automation_id,
                       e.parked_status,
                       e.conversation_thread_id,
                       e.delivery,
                       e.awaits_until,
                       a.user_id,
                       a.workspace_id,
                       COALESCE(e.awaits_until <= NOW(), FALSE) AS expired,
                       COALESCE(
                           (e.delivery->'capacity_retry'->>'next_retry_at')::timestamptz <= NOW(),
                           TRUE
                       ) AS due
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE e.status = 'waiting'
                  AND e.wait_reason = %(wait_reason)s
                  AND a.deleted_at IS NULL
                  AND (
                      COALESCE(e.awaits_until <= NOW(), FALSE)
                      OR COALESCE(
                          (e.delivery->'capacity_retry'->>'next_retry_at')::timestamptz <= NOW(),
                          TRUE
                      )
                  )
                ORDER BY COALESCE(
                    (e.delivery->'capacity_retry'->>'next_retry_at')::timestamptz,
                    e.awaits_until,
                    e.created_at
                ) ASC
                LIMIT %(limit)s
                """,
                {"wait_reason": CAPACITY_WAIT_REASON, "limit": limit},
            )
            return [dict(r) for r in await cur.fetchall()]


async def release_capacity_parked_execution(execution_id: str) -> bool:
    """Atomically claim a due capacity-parked execution back to ``pending``.

    Guarded by ``status='waiting'``, ``wait_reason='capacity_limit'``, not
    expired, due, and live parent automation so at most one worker dispatches
    the retry.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE automation_executions e
                SET status = 'pending',
                    heartbeat_at = NOW()
                WHERE e.automation_execution_id = %(execution_id)s
                  AND e.status = 'waiting'
                  AND e.wait_reason = %(wait_reason)s
                  AND NOT COALESCE(e.awaits_until <= NOW(), FALSE)
                  AND COALESCE(
                      (e.delivery->'capacity_retry'->>'next_retry_at')::timestamptz <= NOW(),
                      TRUE
                  )
                  AND EXISTS (
                      SELECT 1 FROM automations a
                      WHERE a.automation_id = e.automation_id
                        AND a.deleted_at IS NULL
                  )
                """,
                {"execution_id": execution_id, "wait_reason": CAPACITY_WAIT_REASON},
            )
            return cur.rowcount == 1


Dispatch = Callable[..., None]


async def sweep_one_capacity_parked(
    row: Mapping[str, Any],
    dispatch: Dispatch,
    *,
    stopping: Callable[[], bool],
) -> None:
    """Process one actionable capacity-parked execution row."""
    execution_id = str(row["automation_execution_id"])
    automation = await auto_db.get_automation(
        str(row["automation_id"]), row["user_id"]
    )
    if automation is None:
        return
    thread_id = (
        str(row["conversation_thread_id"])
        if row.get("conversation_thread_id")
        else None
    )
    workspace_id = row.get("workspace_id") or automation.get("workspace_id")
    delivery_obj = row.get("delivery")
    retry_meta = (
        delivery_obj.get(CAPACITY_RETRY_KEY)
        if isinstance(delivery_obj, Mapping)
        else None
    )
    last_error = (
        str(retry_meta.get("last_error"))
        if isinstance(retry_meta, Mapping) and retry_meta.get("last_error")
        else "WriterGuardUnavailable: capacity retry budget exhausted"
    )

    if row.get("expired"):
        settled = await settle(
            automation,
            execution_id,
            Outcome.FAILED_OURS,
            thread_id=thread_id,
            workspace_id=workspace_id,
            error=last_error,
            from_statuses=("waiting",),
            release_pre_start_signals=True,
        )
        if settled:
            await reset_thread_status_after_prestart_exit(
                thread_id, outcome=Outcome.FAILED_OURS
            )
        return

    if stopped_meanwhile(automation, row.get("parked_status") or automation["status"]):
        settled = await settle(
            automation,
            execution_id,
            Outcome.SKIPPED,
            thread_id=thread_id,
            workspace_id=workspace_id,
            skip_reason="user",
            from_statuses=("waiting",),
        )
        if settled:
            await reset_thread_status_after_prestart_exit(
                thread_id, outcome=Outcome.SKIPPED
            )
        return

    if not row.get("due") or stopping():
        return

    if not await release_capacity_parked_execution(execution_id):
        return

    logger.info(
        "[CAPACITY_RETRY] Re-dispatching execution after capacity backoff: "
        "execution_id=%s",
        execution_id,
    )
    dispatch(
        automation,
        execution_id,
        name=f"automation_exec_{str(automation['automation_id'])[:8]}",
    )


async def sweep_capacity_parked(
    dispatch: Dispatch,
    *,
    stopping: Callable[[], bool],
) -> None:
    """Refresh heartbeats and release/expire capacity-parked executions."""
    if not is_automation_capacity_retry_enabled():
        return
    try:
        await touch_capacity_parked_executions()
        rows = await list_capacity_parked_executions()
    except RuntimeError as exc:
        if "pool" in str(exc).lower():
            logger.debug(
                "[CAPACITY_RETRY] Skipping capacity sweep (DB pool unavailable)"
            )
            return
        logger.error("[CAPACITY_RETRY] Capacity-parked sweep failed: %s", exc)
        return
    except Exception as exc:
        logger.error("[CAPACITY_RETRY] Capacity-parked sweep failed: %s", exc)
        return

    for row in rows:
        execution_id = str(row["automation_execution_id"])
        try:
            await sweep_one_capacity_parked(row, dispatch, stopping=stopping)
        except Exception as exc:
            logger.error(
                "[CAPACITY_RETRY] Sweeping capacity-parked execution failed: "
                "execution_id=%s error=%s",
                execution_id,
                exc,
            )
