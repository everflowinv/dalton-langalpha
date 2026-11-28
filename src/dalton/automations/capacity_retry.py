"""DB-backed capacity backoff retry, delivery reconciliation, and atomic pre-START thread reset.

When many automations fire in the same minute and hit transient server-side
capacity exhaustion (``WriterGuardUnavailable``, quota burst 429, or credit-gate
503), parking the execution in Postgres with ``status='waiting'`` and
``wait_reason='capacity_limit'`` lets every worker's scheduler sweep re-dispatch
the *same* execution row once its exponential backoff window elapses, up to a
15-minute hard cap.

Key invariants:
1. **Isolated deadline**: the capacity wait deadline lives ONLY in
   ``delivery -> 'capacity_retry' ->> 'deadline_at'`` (set once on first
   capacity park, never overwritten). ``awaits_until`` is NEVER read or written
   by capacity retry and remains exclusively owned by computer-away waits.
2. **No lost dispatch on claim**: claiming a due row transitions
   ``waiting -> pending`` (or reclaims an orphaned ``pending`` row) with a
   bounded ``dispatch_lease_until`` in ``delivery.capacity_retry`` via a single
   guarded SQL ``UPDATE``. If the claiming worker crashes or fails to dispatch
   before ``begin_execution_if_not_deleted`` (``pending -> running``), any
   worker's sweep reclaims and dispatches it once the lease expires.
3. **Atomic settlement & thread reset**: resetting a stuck pre-START
   ``conversation_threads.current_status`` runs inside the enclosing park or
   settlement transaction, locking the thread row ``FOR UPDATE`` first and then
   checking ``conversation_responses`` on a fresh statement snapshot so a
   concurrent ``START`` is never clobbered.
4. **Clean switch-off draining**: when ``DALTON_AUTOMATION_CAPACITY_RETRY`` is
   disabled, new capacity failures are not parked and existing
   ``capacity_limit`` rows in ``waiting``/``pending`` are drained to
   ``Outcome.FAILED_OURS`` through the shared failure-delivery initialization
   path.
5. **Winner-only delivery reconciliation**: expiry and switch-off settlements
   initialize/reconcile delivery only on the single worker that wins
   ``settle_execution``; recipient edits or clears during capacity wait
   re-resolve or revoke unstarted registrations so removed targets receive zero
   deliveries.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.settings import is_automation_capacity_retry_enabled
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.pool import get_db_connection
from src.server.services import automation_delivery
from src.server.services.automation_computer_wait import stopped_meanwhile
from src.server.services.automation_outcomes import POLICIES, Outcome, clean_error_text
from src.server.services.automation_settlement import settle
from src.server.services.webhook_client import WebhookClient
from src.server.services.writer_guard import WriterGuardUnavailable

logger = logging.getLogger(__name__)

CAPACITY_WAIT_REASON = "capacity_limit"
CAPACITY_RETRY_KEY = "capacity_retry"
DELIVERY_META_KEY = "delivery_meta"
FAILURE_NOTIFY_KEY = "failure_notify"

DEFAULT_INITIAL_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_BACKOFF_SECONDS = 240.0
DEFAULT_MAX_WAIT_SECONDS = 15 * 60  # 15-minute total wait cap
DEFAULT_DISPATCH_LEASE_SECONDS = 30.0
FAILURE_NOTIFY_MAX_ATTEMPTS = 5
FAILURE_NOTIFY_LEASE_SECONDS = 60.0
FAILURE_NOTIFY_RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0, 60.0, 180.0)

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

# Effective capacity deadline expression purely from delivery.capacity_retry
# (never reads or writes awaits_until, which belongs to computer-away waits).
# Falls back to first_parked_at + max_wait_s (rolling-upgrade compatibility with r1)
# and finally created_at + max_wait_s so no capacity row can ever remain unbounded.
_CAPACITY_DEADLINE_EXPR = (
    "COALESCE("
    "(e.delivery->'capacity_retry'->>'deadline_at')::timestamptz, "
    "(e.delivery->'capacity_retry'->>'first_parked_at')::timestamptz "
    "+ make_interval(secs => %(max_wait_s)s::double precision), "
    "e.created_at + make_interval(secs => %(max_wait_s)s::double precision)"
    ")"
)

_CAPACITY_EXPIRED_SQL = f"({_CAPACITY_DEADLINE_EXPR} <= NOW())"


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


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return _ensure_utc(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return _ensure_utc(datetime.fromisoformat(raw.strip()))
    except ValueError:
        return None


def _resolve_existing_capacity_deadline(
    prev_retry: Any, *, max_wait_seconds: int
) -> Optional[datetime]:
    """Read the immutable capacity deadline solely from ``delivery.capacity_retry``."""
    if not isinstance(prev_retry, Mapping):
        return None
    explicit = _parse_iso_utc(prev_retry.get("deadline_at"))
    if explicit is not None:
        return explicit
    first_parked = _parse_iso_utc(prev_retry.get("first_parked_at"))
    if first_parked is not None:
        return first_parked + timedelta(seconds=max_wait_seconds)
    return None


async def _reset_thread_status_on_conn(
    conn: Any,
    thread_id: str,
    *,
    fallback: str,
) -> bool:
    """Lock ``conversation_threads`` first, then reset on a fresh statement snapshot.

    Two statements are mandatory under PostgreSQL READ COMMITTED:
    1. ``SELECT current_status ... FOR UPDATE`` serializes behind any concurrent
       ``START`` holding the thread row lock.
    2. Once the lock is acquired, the next statement takes a brand-new snapshot
       that is guaranteed to observe any ``conversation_responses`` row
       committed by that concurrent ``START`` before it released the lock, so a
       freshly started ``in_progress`` turn is never overwritten.
    """
    tid = str(thread_id)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT current_status
            FROM conversation_threads
            WHERE conversation_thread_id = %s::uuid
            FOR UPDATE
            """,
            (tid,),
        )
        locked = await cur.fetchone()
        if locked is None:
            return False
        cur_status = (
            locked["current_status"]
            if isinstance(locked, Mapping)
            else locked[0]
        )
        if cur_status not in ("in_progress", "active"):
            return False

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
            WHERE ct.conversation_thread_id = %s::uuid
              AND ct.current_status IN ('in_progress', 'active')
              AND NOT EXISTS (
                  SELECT 1
                  FROM conversation_responses cr2
                  WHERE cr2.conversation_thread_id = ct.conversation_thread_id
                    AND cr2.status = 'in_progress'
              )
            """,
            (fallback, tid),
        )
        return bool(getattr(cur, "rowcount", 0))


async def reset_thread_status_after_prestart_exit(
    thread_id: Optional[str],
    *,
    outcome: Optional[Outcome] = None,
    conn: Any = None,
) -> bool:
    """Reset a thread stuck in ``in_progress``/``active`` after a pre-START exit.

    When ``conn`` is supplied, runs inside the caller's open transaction (e.g.,
    ``settle_execution`` or ``park_execution_for_capacity``) and propagates any
    database error so the enclosing transaction commits or rolls back atomically.
    When ``conn`` is None (standalone best-effort, e.g., after ``hand_off``),
    opens a dedicated transaction and logs errors without raising.
    """
    if not thread_id:
        return False
    fallback = _fallback_thread_status(outcome)
    if conn is not None:
        if not callable(getattr(conn, "cursor", None)):
            return False
        return await _reset_thread_status_on_conn(
            conn, str(thread_id), fallback=fallback
        )
    try:
        async with get_db_connection() as own_conn:
            async with own_conn.transaction():
                return await _reset_thread_status_on_conn(
                    own_conn, str(thread_id), fallback=fallback
                )
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


# ---------------------------------------------------------------------------
# Delivery state persistence & reconciliation across retries / sweep failures
# ---------------------------------------------------------------------------


def _extract_delivery_meta(delivery_obj: Any) -> dict[str, Any]:
    if not isinstance(delivery_obj, Mapping):
        return {}
    meta = delivery_obj.get(DELIVERY_META_KEY)
    return dict(meta) if isinstance(meta, Mapping) else {}


def has_default_delivery_entry(entries: Sequence[str]) -> bool:
    """Return True when any delivery entry is an app-only default target (no ':')."""
    return any(isinstance(e, str) and bool(e.strip()) and ":" not in e for e in entries)


def delivery_id_for_start(execution_id: str, delivery_starts: int) -> str:
    """Resolve the delivery ID for start #``delivery_starts``.

    Uses upstream's ``execution_id`` on the first start and
    ``f"{execution_id}.{delivery_starts}"`` on subsequent starts after a
    recipient change. Dalton's channel gateway maps ``<uuid>.<n>`` back to the
    execution UUID and updates the unstarted registration in place, while
    external messaging services register a fresh run and let the old one lapse.
    """
    if delivery_starts <= 1:
        return execution_id
    return f"{execution_id}.{delivery_starts}"


async def _persist_delivery_meta(
    execution_id: str,
    *,
    entries: list[str],
    starts: int,
    registered: bool,
) -> None:
    """Persist delivery registration metadata into ``automation_executions.delivery``."""
    meta = {
        "entries": list(entries),
        "starts": int(starts),
        "registered": bool(registered),
    }
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE automation_executions
                    SET delivery = jsonb_set(
                        COALESCE(delivery, '{}'::jsonb),
                        '{delivery_meta}',
                        %s::jsonb,
                        true
                    )
                    WHERE automation_execution_id = %s::uuid
                      AND status IN ('pending', 'waiting', 'running')
                    """,
                    (Json(meta), execution_id),
                )
    except Exception as exc:
        logger.debug(
            "[CAPACITY_RETRY] Persisting delivery_meta failed: execution_id=%s error=%s",
            execution_id,
            exc,
        )


async def _revoke_prior_channel_registration(
    execution_id: str, *, user_id: Optional[str]
) -> None:
    """Revoke any unstarted ``dalton_channel_automation_runs`` registration."""
    if not has_capability(CapabilityId.CHANNEL_GATEWAY):
        return
    try:
        from src.dalton.database.channel_gateway import (
            revoke_unstarted_automation_run,
        )

        await revoke_unstarted_automation_run(
            execution_id, user_id=user_id
        )
    except Exception as exc:
        logger.warning(
            "[CAPACITY_RETRY] Revoking prior channel automation registration failed: "
            "execution_id=%s error=%s",
            execution_id,
            exc,
        )


async def hydrate_delivery_state_for_firing(
    execution_id: str, firing: Any
) -> None:
    """Hydrate ``_Firing`` delivery state from ``automation_executions.delivery`` once.

    Ensures a retried attempt (in a fresh ``_Firing`` or another worker) knows
    what delivery entries were previously registered so recipient edits trigger
    re-resolution and clearing all recipients clears the old stamp and revokes
    the old gateway registration.
    """
    if getattr(firing, "_delivery_hydrated", False):
        return
    setattr(firing, "_delivery_hydrated", True)
    if getattr(firing, "delivery_starts", 0) > 0:
        return
    try:
        row = await exec_db.get_execution_for_settlement(execution_id)
    except Exception as exc:
        logger.debug(
            "[CAPACITY_RETRY] Hydrating delivery state failed: execution_id=%s error=%s",
            execution_id,
            exc,
        )
        return
    if not isinstance(row, Mapping):
        return
    delivery_obj = row.get("delivery")
    if not isinstance(delivery_obj, Mapping):
        return
    prior_delivery = automation_delivery.read_stamp(delivery_obj, execution_id)
    meta = _extract_delivery_meta(delivery_obj)
    if prior_delivery is None and not meta:
        return
    firing.delivery = prior_delivery
    if isinstance(meta.get("entries"), list):
        firing.delivery_entries = [str(e) for e in meta["entries"] if isinstance(e, str)]
    elif prior_delivery is not None:
        firing.delivery_entries = [t.entry for t in prior_delivery.targets]
    starts = int(meta.get("starts") or (1 if prior_delivery is not None else 0))
    if starts > 0:
        firing.delivery_starts = starts


async def sync_delivery_state_after_start(
    execution_id: str,
    *,
    automation: Mapping[str, Any],
    entries: list[str],
    delivery: Optional[automation_delivery.Delivery],
    delivery_starts: int,
    had_prior_registration: bool,
) -> None:
    """Sync persisted delivery_meta and revoke stale registrations when cleared."""
    if had_prior_registration and (delivery is None or not delivery.held):
        await _revoke_prior_channel_registration(
            execution_id,
            user_id=str(automation.get("user_id") or "") or None,
        )
    if delivery is not None or had_prior_registration or entries:
        await _persist_delivery_meta(
            execution_id,
            entries=entries,
            starts=delivery_starts,
            registered=bool(delivery is not None and delivery.held),
        )


async def resolve_delivery_workspace_id(
    automation: Mapping[str, Any], execution_id: str
) -> Optional[str]:
    """Resolve the workspace ID for an unstarted firing's delivery, matching
    ``AutomationExecutor._start_delivery_unstarted``.
    """
    from src.server.services.automation_executor import _turn_place
    from src.server.services.turn_runtime import turn_route

    try:
        place = await _turn_place(dict(automation))
        route = turn_route(str(automation["user_id"]), place.workspace)
        return route.workspace_id if route.role == "analyst" else None
    except Exception as exc:
        logger.warning(
            "[CAPACITY_RETRY] No route for delivery of a firing that won't run, "
            "naming no workspace: execution_id=%s error=%s",
            execution_id,
            exc,
        )
        return None


async def reconcile_delivery_for_settled_failure(
    automation: Mapping[str, Any],
    execution_id: str,
    *,
    settled_row: Mapping[str, Any],
    thread_id: Optional[str],
    current_delivery: Optional[automation_delivery.Delivery],
) -> Optional[automation_delivery.Delivery]:
    """Initialize or reconcile delivery ONLY on the worker that won settlement.

    Shares the exact resolution semantics of
    ``AutomationExecutor._start_delivery_unstarted``:
    - Soft-deleted automations deliver nothing.
    - If delivery entries were cleared while parked, revokes any prior unstarted
      channel registration and returns None so old targets receive zero posts.
    - If entries are unchanged and already registered, reuses the kept delivery.
    - If never registered yet (e.g. credit-gate 503 before delivery start) or
      entries changed during the wait, resolves workspace route and calls
      ``automation_delivery.start_run`` with the current automation config.
    """
    from src.dalton.database.automation_recycle import automation_is_soft_deleted

    automation_id = str(automation.get("automation_id") or "")
    if automation_id and await automation_is_soft_deleted(automation_id):
        return None

    entries = automation_delivery.entries_of(dict(automation))
    raw_delivery = settled_row.get("delivery") if isinstance(settled_row, Mapping) else None
    meta = _extract_delivery_meta(raw_delivery)
    prior_entries: Optional[list[str]] = None
    if isinstance(meta.get("entries"), list):
        prior_entries = [str(e) for e in meta["entries"] if isinstance(e, str)]
    elif current_delivery is not None:
        prior_entries = [t.entry for t in current_delivery.targets]

    had_prior = current_delivery is not None or bool(meta.get("registered"))
    prior_starts = int(meta.get("starts") or (1 if had_prior else 0))

    if not entries:
        if had_prior:
            await _revoke_prior_channel_registration(
                execution_id,
                user_id=str(automation.get("user_id") or "") or None,
            )
        return None

    if (
        current_delivery is not None
        and prior_entries == entries
        and not has_default_delivery_entry(entries)
    ):
        return current_delivery

    next_starts = prior_starts + 1
    delivery_id = delivery_id_for_start(execution_id, next_starts)
    delivery_ws_id = await resolve_delivery_workspace_id(automation, execution_id)
    try:
        new_delivery = await automation_delivery.start_run(
            dict(automation),
            delivery_id,
            delivery_ws_id,
            thread_id=thread_id,
        )
    except Exception as exc:
        logger.error(
            "[CAPACITY_RETRY] Starting delivery for settled failure failed: "
            "execution_id=%s error=%s",
            execution_id,
            exc,
        )
        new_delivery = None

    if had_prior and (new_delivery is None or not new_delivery.held):
        await _revoke_prior_channel_registration(
            execution_id,
            user_id=str(automation.get("user_id") or "") or None,
        )
    return new_delivery


# ---------------------------------------------------------------------------
# Parking, heartbeating, listing, claiming, and sweeping
# ---------------------------------------------------------------------------


async def park_execution_for_capacity(
    execution_id: str,
    *,
    parked_status: str,
    error_message: str,
    thread_id: Optional[str] = None,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
    base_backoff_seconds: float = DEFAULT_INITIAL_BACKOFF_SECONDS,
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
    jitter_fn: Callable[[float, float], float] = random.uniform,
) -> tuple[bool, Optional[str]]:
    """Park a pending/running execution in Postgres for capacity backoff retry.

    Stores the capacity deadline exclusively in
    ``delivery -> 'capacity_retry' ->> 'deadline_at'`` (never reads or writes
    ``awaits_until``) and resets any stuck pre-START thread status inside the
    exact same database transaction.

    Returns ``(parked, recorded_thread_id)``. Returns ``(False, thread_id)``
    when the execution is no longer pending/running or when the 15-minute
    capacity deadline has already been reached — in which case the caller
    settles as ``Outcome.FAILED_OURS``.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT status, parked_status, delivery,
                           conversation_thread_id, NOW() AS db_now
                    FROM automation_executions
                    WHERE automation_execution_id = %s::uuid
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
                delivery_obj = row.get("delivery")
                prev_retry = (
                    delivery_obj.get(CAPACITY_RETRY_KEY)
                    if isinstance(delivery_obj, Mapping)
                    else None
                )

                existing_deadline = _resolve_existing_capacity_deadline(
                    prev_retry, max_wait_seconds=max_wait_seconds
                )
                if existing_deadline is not None:
                    deadline = existing_deadline
                    if db_now >= deadline:
                        return False, recorded_thread
                else:
                    deadline = db_now + timedelta(seconds=max_wait_seconds)

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
                    "deadline_at": deadline.isoformat(),
                    "last_error": error_message,
                    "first_parked_at": first_parked_at,
                }
                await cur.execute(
                    """
                    UPDATE automation_executions
                    SET status = 'waiting',
                        wait_reason = %(wait_reason)s,
                        parked_status = COALESCE(parked_status, %(parked_status)s),
                        heartbeat_at = NOW(),
                        delivery = jsonb_set(
                            COALESCE(delivery, '{}'::jsonb),
                            '{capacity_retry}',
                            %(retry_payload)s::jsonb,
                            true
                        )
                    WHERE automation_execution_id = %(execution_id)s::uuid
                      AND status IN ('pending', 'running')
                    """,
                    {
                        "execution_id": execution_id,
                        "wait_reason": CAPACITY_WAIT_REASON,
                        "parked_status": parked_status,
                        "retry_payload": Json(retry_payload),
                    },
                )
                if cur.rowcount != 1:
                    return False, recorded_thread

            effective_thread = thread_id or recorded_thread
            if effective_thread:
                await reset_thread_status_after_prestart_exit(
                    effective_thread, outcome=None, conn=conn
                )

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
        parked, _recorded_thread = await park_execution_for_capacity(
            execution_id,
            parked_status=parked_status,
            error_message=error_message,
            thread_id=thread_id,
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
    return bool(parked)


async def touch_capacity_parked_executions(
    *, max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS
) -> int:
    """Keep ``heartbeat_at`` fresh for non-expired capacity-parked executions."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE automation_executions e
                SET heartbeat_at = NOW()
                WHERE e.status = 'waiting'
                  AND e.wait_reason = %(wait_reason)s
                  AND NOT {_CAPACITY_EXPIRED_SQL}
                """,
                {
                    "wait_reason": CAPACITY_WAIT_REASON,
                    "max_wait_s": float(max_wait_seconds),
                },
            )
            return int(getattr(cur, "rowcount", 0) or 0)


async def list_capacity_parked_executions(
    limit: int = 50,
    *,
    drain_all: bool = False,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> list[dict[str, Any]]:
    """List actionable capacity-parked executions (waiting or orphaned pending).

    When ``drain_all=True`` (feature switch turned OFF), selects every remaining
    ``capacity_limit`` row in ``('waiting', 'pending')`` so none stays stranded.
    """
    due_expr = (
        "CASE "
        "WHEN e.status = 'waiting' THEN "
        "COALESCE((e.delivery->'capacity_retry'->>'next_retry_at')::timestamptz <= NOW(), TRUE) "
        "WHEN e.status = 'pending' THEN "
        "COALESCE((e.delivery->'capacity_retry'->>'dispatch_lease_until')::timestamptz <= NOW(), TRUE) "
        "ELSE FALSE END"
    )
    stopped_expr = (
        "(a.status NOT IN ('active', 'executing') "
        "AND a.status IS DISTINCT FROM COALESCE(e.parked_status, a.status))"
    )
    actionable_filter = (
        ""
        if drain_all
        else f"AND ({_CAPACITY_EXPIRED_SQL} OR ({due_expr}) OR {stopped_expr})"
    )
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT e.automation_execution_id,
                       e.automation_id,
                       e.status,
                       e.parked_status,
                       e.conversation_thread_id,
                       e.delivery,
                       a.user_id,
                       a.workspace_id,
                       {_CAPACITY_EXPIRED_SQL} AS expired,
                       ({due_expr}) AS due
                FROM automation_executions e
                JOIN automations a ON a.automation_id = e.automation_id
                WHERE e.status IN ('waiting', 'pending')
                  AND e.wait_reason = %(wait_reason)s
                  AND a.deleted_at IS NULL
                  {actionable_filter}
                ORDER BY COALESCE(
                    (e.delivery->'capacity_retry'->>'next_retry_at')::timestamptz,
                    (e.delivery->'capacity_retry'->>'dispatch_lease_until')::timestamptz,
                    (e.delivery->'capacity_retry'->>'deadline_at')::timestamptz,
                    e.created_at
                ) ASC
                LIMIT %(limit)s
                """,
                {
                    "wait_reason": CAPACITY_WAIT_REASON,
                    "max_wait_s": float(max_wait_seconds),
                    "limit": limit,
                },
            )
            return [dict(r) for r in await cur.fetchall()]


async def release_capacity_parked_execution(
    execution_id: str,
    *,
    lease_seconds: float = DEFAULT_DISPATCH_LEASE_SECONDS,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
) -> Optional[str]:
    """Atomically claim a due capacity-parked (or lease-expired pending) row.

    Sets ``status = 'pending'``, refreshes ``heartbeat_at``, and stamps a fresh
    ``claim_token`` + bounded ``dispatch_lease_until`` in
    ``delivery.capacity_retry`` in one guarded ``UPDATE``. Concurrent sweeps
    across workers observe at most one winner (returns ``claim_token``); if
    that winner stalls past the lease before ``begin_execution_if_not_deleted``
    (``pending -> running``), another worker reclaims with a new token and the
    stale worker's ``claim_token`` is rejected at start and lease-release time.
    """
    claim_token = str(uuid4())
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE automation_executions e
                SET status = 'pending',
                    heartbeat_at = NOW(),
                    delivery = jsonb_set(
                        COALESCE(e.delivery, '{{}}'::jsonb),
                        '{{capacity_retry}}',
                        COALESCE(e.delivery->'capacity_retry', '{{}}'::jsonb)
                        || jsonb_build_object(
                            'claimed_at', to_jsonb(NOW()),
                            'claim_token', to_jsonb(%(claim_token)s::text),
                            'dispatch_lease_until',
                            to_jsonb(NOW() + make_interval(secs => %(lease_s)s::double precision))
                        ),
                        true
                    )
                WHERE e.automation_execution_id = %(execution_id)s::uuid
                  AND e.wait_reason = %(wait_reason)s
                  AND NOT {_CAPACITY_EXPIRED_SQL}
                  AND (
                      (
                          e.status = 'waiting'
                          AND COALESCE(
                              (e.delivery->'capacity_retry'->>'next_retry_at')::timestamptz <= NOW(),
                              TRUE
                          )
                      )
                      OR (
                          e.status = 'pending'
                          AND COALESCE(
                              (e.delivery->'capacity_retry'->>'dispatch_lease_until')::timestamptz <= NOW(),
                              TRUE
                          )
                      )
                  )
                  AND EXISTS (
                      SELECT 1 FROM automations a
                      WHERE a.automation_id = e.automation_id
                        AND a.deleted_at IS NULL
                  )
                """,
                {
                    "execution_id": execution_id,
                    "wait_reason": CAPACITY_WAIT_REASON,
                    "claim_token": claim_token,
                    "lease_s": float(lease_seconds),
                    "max_wait_s": float(max_wait_seconds),
                },
            )
            return claim_token if cur.rowcount == 1 else None


async def release_dispatch_lease(
    execution_id: str,
    *,
    claim_token: Optional[str] = None,
) -> bool:
    """Expire an active dispatch lease immediately when local dispatch fails.

    Fenced on ``claim_token`` so a stale worker can never clear a newer worker's
    reclaimed lease.
    """
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE automation_executions
                    SET delivery = jsonb_set(
                        COALESCE(delivery, '{}'::jsonb),
                        '{capacity_retry}',
                        COALESCE(delivery->'capacity_retry', '{}'::jsonb)
                        || jsonb_build_object('dispatch_lease_until', to_jsonb(NOW())),
                        true
                    )
                    WHERE automation_execution_id = %s::uuid
                      AND status = 'pending'
                      AND wait_reason = %s
                      AND (%s::text IS NULL OR delivery->'capacity_retry'->>'claim_token' = %s::text)
                    """,
                    (execution_id, CAPACITY_WAIT_REASON, claim_token, claim_token),
                )
                return bool(getattr(cur, "rowcount", 0))
    except Exception as exc:
        logger.warning(
            "[CAPACITY_RETRY] Releasing dispatch lease failed: execution_id=%s error=%s",
            execution_id,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Durable, retryable failure notification task (keyed by execution_id;
# supports executions with no thread_id / no run_id)
# ---------------------------------------------------------------------------


async def enqueue_settled_failure_notification(
    conn: Any,
    *,
    execution_id: str,
    automation: Mapping[str, Any],
    outcome: Outcome,
    error: Optional[str],
    thread_id: Optional[str],
    workspace_id: Optional[str],
) -> bool:
    """Write a durable retryable failure-notification task inside the settlement
    transaction on ``automation_executions.delivery -> 'failure_notify'``.

    Keyed by ``execution_id`` (no foreign key to ``conversation_threads`` or
    ``conversation_responses``), so pre-START / threadless executions never lose
    their failure notice if the settling process crashes right after commit.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    effective_ws = workspace_id or (
        str(automation["workspace_id"]) if automation.get("workspace_id") else None
    )
    job_payload = {
        "status": "pending",
        "execution_id": execution_id,
        "automation_id": str(automation["automation_id"]),
        "user_id": str(automation["user_id"]),
        "thread_id": str(thread_id) if thread_id else None,
        "workspace_id": str(effective_ws) if effective_ws else None,
        "outcome": outcome.value,
        "error": error,
        "attempts": 0,
        "recorded": False,
        "answered": False,
        "next_retry_at": now_iso,
        "created_at": now_iso,
    }
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE automation_executions
            SET delivery = jsonb_set(
                COALESCE(delivery, '{}'::jsonb),
                '{failure_notify}',
                %s::jsonb,
                true
            )
            WHERE automation_execution_id = %s::uuid
            """,
            (Json(job_payload), execution_id),
        )
        return bool(getattr(cur, "rowcount", 0))


async def list_pending_failure_notifications(
    limit: int = 50,
    *,
    max_attempts: int = FAILURE_NOTIFY_MAX_ATTEMPTS,
) -> list[str]:
    """List execution IDs with due pending or lease-expired failure notifications."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT e.automation_execution_id
                FROM automation_executions e
                WHERE e.status IN ('failed', 'timeout')
                  AND e.delivery ? 'failure_notify'
                  AND (
                      (
                          (e.delivery->'failure_notify'->>'status') = 'pending'
                          AND COALESCE(
                              (e.delivery->'failure_notify'->>'next_retry_at')::timestamptz <= NOW(),
                              TRUE
                          )
                      )
                      OR (
                          (e.delivery->'failure_notify'->>'status') = 'claimed'
                          AND COALESCE(
                              (e.delivery->'failure_notify'->>'lease_until')::timestamptz <= NOW(),
                              TRUE
                          )
                          AND COALESCE((e.delivery->'failure_notify'->>'attempts')::int, 0) < %(max_attempts)s
                      )
                  )
                ORDER BY e.completed_at ASC NULLS LAST
                LIMIT %(limit)s
                """,
                {"max_attempts": int(max_attempts), "limit": int(limit)},
            )
            return [str(r["automation_execution_id"]) for r in await cur.fetchall()]


async def claim_failure_notification(
    execution_id: str,
    *,
    lease_seconds: float = FAILURE_NOTIFY_LEASE_SECONDS,
    max_attempts: int = FAILURE_NOTIFY_MAX_ATTEMPTS,
) -> Optional[dict[str, Any]]:
    """Atomically claim one due failure notification with a fenced claim token."""
    claim_token = str(uuid4())
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE automation_executions e
                SET delivery = jsonb_set(
                    e.delivery,
                    '{failure_notify}',
                    (e.delivery->'failure_notify')
                    || jsonb_build_object(
                        'status', 'claimed',
                        'claim_token', to_jsonb(%(claim_token)s::text),
                        'attempts', to_jsonb(COALESCE((e.delivery->'failure_notify'->>'attempts')::int, 0) + 1),
                        'lease_until', to_jsonb(NOW() + make_interval(secs => %(lease_s)s::double precision))
                    ),
                    true
                )
                WHERE e.automation_execution_id = %(execution_id)s::uuid
                  AND e.delivery ? 'failure_notify'
                  AND (
                      (
                          (e.delivery->'failure_notify'->>'status') = 'pending'
                          AND COALESCE(
                              (e.delivery->'failure_notify'->>'next_retry_at')::timestamptz <= NOW(),
                              TRUE
                          )
                      )
                      OR (
                          (e.delivery->'failure_notify'->>'status') = 'claimed'
                          AND COALESCE(
                              (e.delivery->'failure_notify'->>'lease_until')::timestamptz <= NOW(),
                              TRUE
                          )
                          AND COALESCE((e.delivery->'failure_notify'->>'attempts')::int, 0) < %(max_attempts)s
                      )
                  )
                RETURNING e.automation_execution_id,
                          e.automation_id,
                          e.conversation_thread_id,
                          e.conversation_response_id,
                          e.delivery,
                          e.error_message
                """,
                {
                    "execution_id": execution_id,
                    "claim_token": claim_token,
                    "lease_s": float(lease_seconds),
                    "max_attempts": int(max_attempts),
                },
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def _update_failure_notification_state(
    execution_id: str,
    *,
    claim_token: str,
    status: str,
    delay_seconds: Optional[float] = None,
    delivery_result: Optional[list[dict[str, Any]]] = None,
    clear_retrying: bool = False,
    recorded: Optional[bool] = None,
    answered: Optional[bool] = None,
) -> bool:
    """Update failure_notify status (and optionally delivery_result) fenced on claim_token."""
    patch_obj: dict[str, Any] = {"status": status}
    if recorded is not None:
        patch_obj["recorded"] = bool(recorded)
    if answered is not None:
        patch_obj["answered"] = bool(answered)
    if status in ("done", "dead"):
        patch_obj["completed_at"] = datetime.now(timezone.utc).isoformat()
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if delay_seconds is not None:
                    next_retry_expr = (
                        "jsonb_build_object('next_retry_at', "
                        "to_jsonb(NOW() + make_interval(secs => %(delay_s)s::double precision)))"
                    )
                else:
                    next_retry_expr = "'{}'::jsonb"
                await cur.execute(
                    f"""
                    UPDATE automation_executions
                    SET delivery = jsonb_set(
                        delivery,
                        '{{failure_notify}}',
                        ((delivery->'failure_notify') - 'lease_until' - 'claim_token')
                        || %(patch)s::jsonb
                        || {next_retry_expr},
                        true
                    )
                    WHERE automation_execution_id = %(execution_id)s::uuid
                      AND delivery->'failure_notify'->>'claim_token' = %(claim_token)s::text
                    """,
                    {
                        "execution_id": execution_id,
                        "claim_token": claim_token,
                        "patch": Json(patch_obj),
                        "delay_s": float(delay_seconds or 0.0),
                    },
                )
                if cur.rowcount != 1:
                    return False
                if delivery_result is not None:
                    await exec_db.record_delivery(
                        execution_id, delivery_result, conn=conn
                    )
                elif clear_retrying:
                    await exec_db.clear_delivery_retrying(execution_id, conn=conn)
    return True


def _failure_notify_backoff(attempts: int) -> float:
    idx = max(0, min(int(attempts) - 1, len(FAILURE_NOTIFY_RETRY_DELAYS) - 1))
    return FAILURE_NOTIFY_RETRY_DELAYS[idx]


async def deliver_settled_failure_notification(execution_id: str) -> bool:
    """Claim and deliver a durable failure-notification task for ``execution_id``.

    Re-resolves current delivery recipients (including Feishu/Discord workspace
    default groups), posts via the channel gateway or webhook, persists
    ``delivery_result``, and schedules bounded retries on transient failure.
    """
    from src.dalton.database.automation_recycle import automation_is_soft_deleted
    from src.server.services.automation_settlement import (
        _replaces_record,
        _stamp_delivery_retry,
    )

    claimed = await claim_failure_notification(execution_id)
    if claimed is None:
        return False

    delivery_col = claimed.get("delivery")
    job = (
        delivery_col.get(FAILURE_NOTIFY_KEY)
        if isinstance(delivery_col, Mapping)
        else None
    )
    if not isinstance(job, Mapping):
        return False
    claim_token = str(job.get("claim_token") or "")
    attempts = int(job.get("attempts") or 1)
    recorded = job.get("recorded") is True
    answered = job.get("answered") is True
    automation_id = str(job.get("automation_id") or claimed.get("automation_id") or "")
    user_id = str(job.get("user_id") or "")

    try:
        if not automation_id or await automation_is_soft_deleted(automation_id):
            await _update_failure_notification_state(
                execution_id, claim_token=claim_token, status="done"
            )
            return True
        automation = await auto_db.get_automation(automation_id, user_id)
        if automation is None:
            await _update_failure_notification_state(
                execution_id, claim_token=claim_token, status="done"
            )
            return True

        raw_outcome = str(job.get("outcome") or Outcome.FAILED_OURS.value)
        try:
            outcome = Outcome(raw_outcome)
        except ValueError:
            outcome = Outcome.FAILED_OURS
        policy = POLICIES[outcome]
        thread_id = (
            str(job["thread_id"])
            if job.get("thread_id")
            else (
                str(claimed["conversation_thread_id"])
                if claimed.get("conversation_thread_id")
                else None
            )
        )
        workspace_id = (
            str(job["workspace_id"])
            if job.get("workspace_id")
            else (
                str(automation["workspace_id"])
                if automation.get("workspace_id")
                else None
            )
        )
        error = job.get("error") or claimed.get("error_message") or policy.error
        current_delivery = automation_delivery.read_stamp(delivery_col, execution_id)
        delivery = await reconcile_delivery_for_settled_failure(
            automation,
            execution_id,
            settled_row=claimed,
            thread_id=thread_id,
            current_delivery=current_delivery,
        )

        if delivery is not None and delivery.held and policy.finish is not None:
            finish = await automation_delivery.finish_run(
                automation,
                delivery.id,
                policy.finish,
                targets=delivery.targets,
                final_text=None,
                thread_id=thread_id,
                run_id=None,
            )
            will_retry = finish.retry and attempts < FAILURE_NOTIFY_MAX_ATTEMPTS
            should_write = not recorded or _replaces_record(finish, answered)
            stamped_result = (
                _stamp_delivery_retry(finish.result, retrying=will_retry)
                if should_write
                else None
            )
            next_answered = answered or finish.answered
            next_recorded = recorded or should_write
            if will_retry:
                await _update_failure_notification_state(
                    execution_id,
                    claim_token=claim_token,
                    status="pending",
                    delay_seconds=_failure_notify_backoff(attempts),
                    delivery_result=stamped_result,
                    recorded=next_recorded,
                    answered=next_answered,
                )
            else:
                await _update_failure_notification_state(
                    execution_id,
                    claim_token=claim_token,
                    status="done" if not finish.retry else "dead",
                    delivery_result=stamped_result,
                    clear_retrying=not should_write,
                    recorded=next_recorded,
                    answered=next_answered,
                )
            return True

        delivery_result = await WebhookClient().fire_event(
            policy.webhook,
            automation,
            execution_id,
            thread_id,
            workspace_id,
            error=error,
            run_id=None,
            failure_reason=policy.failure_reason,
        )
        if delivery_result is None and delivery is not None and not delivery.held:
            delivery_result = automation_delivery.unsent(delivery) or None
        await _update_failure_notification_state(
            execution_id,
            claim_token=claim_token,
            status="done",
            delivery_result=delivery_result,
            recorded=bool(delivery_result is not None or recorded),
            answered=answered,
        )
        return True
    except Exception as exc:
        logger.warning(
            "[CAPACITY_RETRY] Delivering settled failure notification failed "
            "(attempt %d/%d): execution_id=%s error=%s",
            attempts,
            FAILURE_NOTIFY_MAX_ATTEMPTS,
            execution_id,
            exc,
        )
        if attempts < FAILURE_NOTIFY_MAX_ATTEMPTS:
            await _update_failure_notification_state(
                execution_id,
                claim_token=claim_token,
                status="pending",
                delay_seconds=_failure_notify_backoff(attempts),
            )
        else:
            await _update_failure_notification_state(
                execution_id,
                claim_token=claim_token,
                status="dead",
                clear_retrying=True,
            )
        return False


async def drain_pending_failure_notifications(limit: int = 50) -> int:
    """Drain due durable failure notifications across workers and restarts."""
    try:
        execution_ids = await list_pending_failure_notifications(limit=limit)
    except RuntimeError as exc:
        if "pool" in str(exc).lower():
            return 0
        logger.error(
            "[CAPACITY_RETRY] Listing pending failure notifications failed: %s", exc
        )
        return 0
    except Exception as exc:
        logger.error(
            "[CAPACITY_RETRY] Listing pending failure notifications failed: %s", exc
        )
        return 0

    delivered = 0
    for eid in execution_ids:
        try:
            if await deliver_settled_failure_notification(eid):
                delivered += 1
        except Exception as exc:
            logger.error(
                "[CAPACITY_RETRY] Draining failure notification failed: "
                "execution_id=%s error=%s",
                eid,
                exc,
            )
    return delivered


Dispatch = Callable[..., None]


async def _settle_capacity_failure(
    automation: dict[str, Any],
    execution_id: str,
    *,
    thread_id: Optional[str],
    workspace_id: Optional[str],
    error: str,
    expected_claim_token: Optional[str] = None,
    require_capacity_expired: bool = False,
) -> bool:
    """Settle an expired or switch-drained capacity-parked row as FAILED_OURS.

    Under FOR UPDATE, re-verifies ``wait_reason == 'capacity_limit'``, the
    observed ``claim_token``, and (for expiry) the capacity deadline. Thread
    reset, pre-START signal release, and durable retryable failure-notification
    enqueuing all commit in the exact same settlement transaction.
    """
    return await settle(
        automation,
        execution_id,
        Outcome.FAILED_OURS,
        thread_id=thread_id,
        workspace_id=workspace_id,
        error=error,
        from_statuses=("waiting", "pending"),
        expected_wait_reason=CAPACITY_WAIT_REASON,
        expected_claim_token=expected_claim_token,
        require_capacity_expired=require_capacity_expired,
        release_pre_start_signals=True,
        reset_prestart_thread_status=True,
        enqueue_failure_notify=True,
    )


async def sweep_one_capacity_parked(
    row: Mapping[str, Any],
    dispatch: Dispatch,
    *,
    stopping: Callable[[], bool],
    force_fail: bool = False,
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
    row_claim_token = (
        str(retry_meta.get("claim_token"))
        if isinstance(retry_meta, Mapping) and retry_meta.get("claim_token")
        else None
    )
    last_error = (
        str(retry_meta.get("last_error"))
        if isinstance(retry_meta, Mapping) and retry_meta.get("last_error")
        else "WriterGuardUnavailable: capacity retry budget exhausted"
    )

    if force_fail or row.get("expired"):
        await _settle_capacity_failure(
            automation,
            execution_id,
            thread_id=thread_id,
            workspace_id=workspace_id,
            error=last_error,
            expected_claim_token=row_claim_token,
            require_capacity_expired=not force_fail,
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
            from_statuses=("waiting", "pending"),
            expected_wait_reason=CAPACITY_WAIT_REASON,
            expected_claim_token=row_claim_token,
            release_pre_start_signals=True,
            reset_prestart_thread_status=True,
        )
        if settled:
            await _revoke_prior_channel_registration(
                execution_id,
                user_id=str(automation.get("user_id") or "") or None,
            )
        return

    if not row.get("due") or stopping():
        return

    claim_token = await release_capacity_parked_execution(execution_id)
    if not claim_token:
        return

    logger.info(
        "[CAPACITY_RETRY] Re-dispatching execution after capacity backoff: "
        "execution_id=%s",
        execution_id,
    )
    try:
        dispatch(
            automation,
            execution_id,
            name=f"automation_exec_{str(automation['automation_id'])[:8]}",
            claim_token=claim_token,
        )
    except Exception:
        await release_dispatch_lease(execution_id, claim_token=claim_token)
        raise


async def sweep_capacity_parked(
    dispatch: Dispatch,
    *,
    stopping: Callable[[], bool],
) -> None:
    """Refresh heartbeats, release/expire/drain capacity-parked executions, and
    drain pending durable failure notifications.

    When ``DALTON_AUTOMATION_CAPACITY_RETRY`` is disabled, stops dispatching
    retries and drains every already-parked ``capacity_limit`` execution to
    ``Outcome.FAILED_OURS`` through the durable failure-notification path so no
    execution stays permanently stuck in ``waiting``.
    """
    enabled = is_automation_capacity_retry_enabled()
    try:
        if enabled:
            await touch_capacity_parked_executions()
            rows = await list_capacity_parked_executions(drain_all=False)
        else:
            rows = await list_capacity_parked_executions(drain_all=True)
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
            await sweep_one_capacity_parked(
                row,
                dispatch,
                stopping=stopping,
                force_fail=not enabled,
            )
        except Exception as exc:
            logger.error(
                "[CAPACITY_RETRY] Sweeping capacity-parked execution failed: "
                "execution_id=%s error=%s",
                execution_id,
                exc,
            )

    await drain_pending_failure_notifications()
