"""Shared machinery for synthetic notification turns.

Two dispatch relationships post system-authored turns into a watching
thread when background work finishes: Chief of Staff←PTC report-backs and
PTC←background-subagent task report-backs. Both need the same transport
discipline: a park while the receiving thread is busy, an admission-aware
POST defer loop bounded by the outbox lease, and a terminal wait that holds
the job (and thus the per-thread ordering chain) open until the
notification turn settles. The pending-registries deliberately stay
separate (Redis reserve machinery for hand-offs, open outbox rows for tasks);
only this turn machinery is shared.
"""

import asyncio
import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Admission statuses worth deferring on (vs terminal 4xx drops): capacity,
# credit, and rate-limit gates clear on their own.
DEFER_STATUSES = {402, 403, 429}

# 429s that are this server's capacity rather than the user's usage limit, as
# the automation executor splits them: only these clear within a wait here.
_CAPACITY_429_TYPES = frozenset({"burst_limit", "service_unavailable"})

_TERMINAL_POLL = 5.0

# A thread whose latest attempt is one of these cannot take a notification
# turn: a live turn takes the report in as a steer, and a POST into an
# interrupted one would collide with its pending HITL checkpoint.
_BUSY_STATUSES = ("interrupted", "in_progress")


@dataclass(frozen=True)
class NotOwed:
    """A report nothing is owed for any more: its executor drops it, and a
    live turn may close it without a word."""

    reason: str
    # Whether watchers riding the report's pending chip are told to reconcile
    # now rather than at the drainer's close.
    wake: bool = True


# DALTON-HOOK(handoff-keep-on-resumable): shared resumable-interruption
# predicate (same check as workflow-resume in subagent.py).
def is_resumable_failure(failure_error: object) -> bool:
    """Whether an error value marks a resumable worker-lost interruption."""
    if not failure_error:
        return False
    if isinstance(failure_error, (list, tuple, set)):
        return any(is_resumable_failure(item) for item in failure_error)
    if isinstance(failure_error, dict):
        return any(is_resumable_failure(v) for v in failure_error.values())
    text = str(failure_error)
    return "worker_lost" in text or "RESUMABLE" in text


async def park_while_thread_busy(
    job_id: str,
    attempts: int,
    thread_id: str,
    *,
    log_prefix: str,
    subject: str,
    parks_at_ceiling: bool = False,
) -> bool:
    """Park a claimed report-back while the thread it reports to is live or
    interrupted, until that thread's next dead-turn finalize releases it;
    True when the caller must stop.

    The re-read closes the race where that finalize landed between the
    status read and the park (its release pass saw no deferred row yet),
    releasing the job itself. It waits out a finalize still in flight: one
    whose release pass ran before the park reads live to a plain read until
    it commits, and the job would then wait on a turn already over, holding
    up every later notification on its chain. ``parks_at_ceiling`` is for a
    type the defer still parks at the attempts ceiling (an alert: a price
    target's or a wakeup's notice, held for the thread's next turn), so the
    re-read covers it there too; a steer settled there instead is not
    parked, and has nothing to release.
    """
    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.hook_outbox import MAX_ATTEMPTS

    latest = await tl_db.get_latest_attempt(thread_id)
    if not latest or latest.get("status") not in _BUSY_STATUSES:
        return False
    parked = await outbox_db.defer_claimed_job(
        job_id, attempts=attempts, max_attempts=MAX_ATTEMPTS
    )
    logger.info(
        f"{log_prefix} Parked job {job_id} for {subject} while thread "
        f"{thread_id} is {latest.get('status')} (status={parked})"
    )
    if parked == "pending" or (parked == "exhausted" and parks_at_ceiling):
        latest = await tl_db.get_latest_attempt(thread_id, wait_for_finalize=True)
        if not latest or latest.get("status") not in _BUSY_STATUSES:
            await release_parked_notifications(thread_id)
    return True


async def release_parked_notifications(thread_id: str) -> None:
    """Release the thread's parked notifications and wake the drainer, as the
    finalize of a turn that ended would have, for a caller that held one
    while that finalize ran and so was skipped by its release."""
    from src.server.database.pool import get_db_connection
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.hook_outbox import HookOutboxDrainer

    async with get_db_connection() as conn:
        await outbox_db.release_deferred_notifications(conn, thread_id)
    try:
        HookOutboxDrainer.get_instance().nudge()
    except Exception:
        pass


def run_marker(latest: dict | None) -> tuple[str, str] | None:
    """The thread's latest run as id and status, for a later read to tell
    whether a run ended in between."""
    if not latest:
        return None
    return str(latest.get("conversation_response_id")), str(latest.get("status"))


async def release_if_a_turn_ended(thread_id: str, since: tuple[str, str] | None) -> bool:
    """Release the thread's parked notifications when its latest run has
    ended since ``since`` (``run_marker`` read when the caller's claim
    began) and none is live or awaiting the user; True when it released.

    That run's finalize found the caller's job claimed and released nothing
    for it, so a job the caller then parked would miss the look the ending
    buys. Released only on a change: a claim that met no new ending leaves
    its job parked, so a refusal on every look cannot loop.
    """
    from src.server.database.runs import lifecycle as tl_db

    # Behind a finalize in flight, as the park's re-read is.
    latest = await tl_db.get_latest_attempt(thread_id, wait_for_finalize=True)
    if not latest or latest.get("status") in _BUSY_STATUSES:
        return False
    if run_marker(latest) == since:
        return False
    await release_parked_notifications(thread_id)
    return True


async def earlier_wake(key: str, thread_id: str, *, log_prefix: str) -> dict | None:
    """The run an earlier claim's POST started under ``key``, or None.

    Request keys are unique across every thread, so a run holding the key on
    another thread is not this notice's wake, and stamping the notice to it
    would mark delivered a notice this thread never saw.
    """
    from src.server.database.runs import lifecycle as tl_db

    earlier = await tl_db.find_run_by_request_key(key)
    if earlier is None:
        return None
    if str(earlier.get("conversation_thread_id")) != thread_id:
        logger.warning(
            f"{log_prefix} Request key {key} names run "
            f"{earlier.get('conversation_response_id')} on another thread "
            f"than {thread_id}; not adopting it"
        )
        return None
    return earlier


async def post_notification_turn(
    *,
    thread_id: str,
    body: dict,
    user_id: str,
    wait_cap: float,
    heartbeat=None,
    log_prefix: str,
    subject: str = "",
    still_due=None,
    busy_fails_fast: bool = False,
    refusal_fails_fast: bool = False,
    on_unanswered=None,
) -> tuple[str, str | None]:
    """POST a synthetic turn to ``thread_id``, deferring through admission.

    Returns ``(outcome, run_id)`` where outcome is ``"dispatched"`` (run_id
    set), ``"drop"`` (terminal 4xx), ``"cap"`` (exhausted defer-wait — the
    target may simply be busy; the caller picks drop vs park), ``"deleted"``
    (target thread 404), ``"lost"`` (the ``heartbeat`` fence failed —
    caller must stop with no teardown), ``"superseded"`` (``still_due``
    said no; no turn was started), or ``"taken"`` (START refused the steer
    the body carries: a live turn took it, or it already ended). Defers with backoff on 409, >=500,
    and ``DEFER_STATUSES``, bounded by ``wait_cap`` — except a 409 whose
    detail is ``duplicate_request``: that's a prior POST of this same
    ``request_key`` that already started a run, adopted as ``"dispatched"``.
    ``heartbeat`` (async -> bool) runs every defer iteration so a long
    busy-wait can't outlive the caller's outbox lease. ``still_due`` (async
    -> bool) is asked before each POST until one gets no definite answer (a
    transport error or a 5xx): that POST may still be admitted, and only a
    retry's request_key dedup can tell. ``on_unanswered`` (async) runs after
    each such POST, for a caller that must record it outlasting this call.
    ``busy_fails_fast`` returns ``"busy"`` on the first 409 that is not our
    own duplicate, for a caller with a better move than waiting out the
    thread's run (handing its message to that run).
    ``refusal_fails_fast`` returns ``"refused"`` on the first refusal no wait
    here outlives (``_lasting_refusal``), for a caller that can hand its
    message to the thread's next turn instead of retrying it.
    ``subject`` is a short label for log lines (e.g. the source PTC thread
    or task id).
    """
    import aiohttp

    self_base_url = os.environ.get("GINLIXFLOW_BASE_URL", "http://localhost:8000")
    service_token = os.environ.get("INTERNAL_SERVICE_TOKEN", "")

    # With auth enabled, the endpoint rejects an unauthenticated background
    # dispatch (403) — a defer status for this loop, so firing it anyway
    # would busy-wait the whole cap. Drop immediately instead.
    from src.config.settings import background_dispatch_requires_token

    if background_dispatch_requires_token():
        logger.error(
            "%s INTERNAL_SERVICE_TOKEN is not set; notification turn for %s "
            "cannot be dispatched as background. Set it on the backend "
            "service.",
            log_prefix,
            subject or thread_id,
        )
        return "drop", None

    headers = {
        "X-Service-Token": service_token,
        "X-User-Id": user_id,
        "X-Dispatch": "background",
    }
    url = f"{self_base_url}/api/v1/threads/{thread_id}/messages"

    deadline = asyncio.get_running_loop().time() + wait_cap
    backoff = 1.0
    answered = True
    async with aiohttp.ClientSession() as session:
        while True:
            if still_due is not None and answered and not await still_due():
                logger.info(
                    f"{log_prefix} Notification for {subject or thread_id} on "
                    f"thread {thread_id} is superseded; not posting"
                )
                return "superseded", None
            answered = False
            try:
                async with session.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(connect=10, sock_read=30),
                ) as resp:
                    answered = resp.status < 500
                    if resp.status < 400:
                        try:
                            run_id = (await resp.json()).get("run_id")
                        except Exception:
                            run_id = None
                        logger.info(
                            f"{log_prefix} Dispatched notification turn to "
                            f"thread {thread_id} for {subject or 'background work'}"
                        )
                        return "dispatched", run_id
                    if resp.status == 404:
                        logger.warning(
                            f"{log_prefix} Thread {thread_id} gone (404); "
                            f"dropping notification for {subject or 'background work'}"
                        )
                        return "deleted", None
                    if resp.status == 409:
                        # A duplicate_request 409 is OUR earlier POST of this
                        # same request_key (crash before the dispatched run id
                        # was persisted): adopt its run instead of deferring
                        # behind it forever.
                        try:
                            detail = (await resp.json()).get("detail")
                        except Exception:
                            detail = None
                        if (
                            isinstance(detail, dict)
                            and detail.get("code") == "duplicate_request"
                            and detail.get("run_id")
                        ):
                            logger.info(
                                f"{log_prefix} Adopting existing notification "
                                f"run {detail['run_id']} for {subject} on "
                                f"thread {thread_id} (request_key dedup)"
                            )
                            return "dispatched", detail["run_id"]
                        if (
                            isinstance(detail, dict)
                            and detail.get("code") == "steer_taken"
                        ):
                            logger.info(
                                f"{log_prefix} {subject or 'Steer'} on thread "
                                f"{thread_id} was taken in elsewhere; not posting"
                            )
                            return "taken", None
                        if busy_fails_fast:
                            return "busy", None
                    if refusal_fails_fast and await _lasting_refusal(resp):
                        logger.info(
                            f"{log_prefix} Thread {thread_id} refused the "
                            f"notification for {subject or 'background work'} "
                            f"({resp.status}) on a limit that does not clear "
                            f"by itself; not waiting"
                        )
                        return "refused", None
                    if (
                        resp.status == 409
                        or resp.status >= 500
                        or resp.status in DEFER_STATUSES
                    ):
                        # Don't log the body here: a 429 (credit/burst)
                        # response can carry the user's balance/limit figures.
                        logger.info(
                            f"{log_prefix} Thread {thread_id} cannot admit yet "
                            f"({resp.status}); deferring notification for "
                            f"{subject or 'background work'}"
                        )
                    else:
                        text = await resp.text()
                        logger.warning(
                            f"{log_prefix} Terminal {resp.status} POSTing to "
                            f"thread {thread_id}: {text[:200]}; dropping"
                        )
                        return "drop", None
            except Exception as e:
                logger.warning(
                    f"{log_prefix} HTTP error POSTing to thread {thread_id}: {e}"
                )
            if not answered and on_unanswered is not None:
                await on_unanswered()

            if heartbeat is not None and not await heartbeat():
                logger.warning(
                    f"{log_prefix} Outbox lease lost while deferring the "
                    f"notification POST for {subject or thread_id}; standing down"
                )
                return "lost", None
            await asyncio.sleep(min(backoff, 5.0))
            backoff = min(backoff * 2, 5.0)
            # The deadline gates the NEXT launch (checked after the sleep,
            # immediately before the loop re-POSTs): an attempt must never
            # start past the deadline — a post-deadline takeover could still
            # be holding admission when the drop tears down, orphaning the
            # turn it then starts.
            if asyncio.get_running_loop().time() >= deadline:
                logger.warning(
                    f"{log_prefix} Busy-wait cap hit for {subject or thread_id} "
                    f"on thread {thread_id}"
                )
                return "cap", None


async def _lasting_refusal(resp) -> bool:
    """A refusal no wait under one claim outlives: payment required, or a
    429 for the user's usage limit rather than this server's capacity.

    A 403 is not one. A missing provider is the user's to set up and a wrong
    dispatch token the operator's to fix, either of which can happen while this
    waits; both still reach the caller once the wait runs out. An access tier
    the platform cannot be asked for answers 503, which defers like any 5xx.
    """
    if resp.status == 402:
        return True
    if resp.status != 429:
        return False
    try:
        detail = (await resp.json()).get("detail")
    except Exception:
        return False
    return isinstance(detail, dict) and detail.get("type") not in _CAPACITY_429_TYPES


async def await_run_terminal(
    job_id: str,
    attempts: int,
    run_id: str,
    *,
    wait_cap: float,
    log_prefix: str,
) -> str:
    """Hold an outbox job open until ``run_id`` reaches terminal.

    Polls the durable run row; each iteration first heartbeats the fenced
    lease. Returns ``"terminal"`` (run settled), ``"lease_lost"`` (another
    drainer owns the job — caller must stand down with NO teardown), or
    ``"timeout"`` (never reached terminal, or the row vanished for good —
    caller applies its own fenced disposition). A missing run row is NOT
    treated as deletion: dispatched admission returns the run id before the
    START transaction commits, so an early poll legitimately sees nothing.
    """
    from src.server.database.runs import outbox as outbox_db
    from src.server.database.runs import lifecycle as tl_db
    from src.server.services.hook_outbox import LEASE_SECONDS

    deadline = asyncio.get_running_loop().time() + wait_cap
    while True:
        if not await outbox_db.extend_job_lease(
            job_id, LEASE_SECONDS, attempts=attempts
        ):
            logger.info(
                f"{log_prefix} Lease lost on job {job_id} while awaiting "
                f"run {run_id}; standing down"
            )
            return "lease_lost"
        run = await tl_db.get_run(run_id)
        if run is not None and run.get("status") != "in_progress":
            return "terminal"
        if asyncio.get_running_loop().time() >= deadline:
            logger.warning(
                f"{log_prefix} Terminal wait cap hit awaiting run {run_id} "
                f"(row {'missing' if run is None else 'in_progress'})"
            )
            return "timeout"
        await asyncio.sleep(_TERMINAL_POLL)
