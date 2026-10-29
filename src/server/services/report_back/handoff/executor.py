"""Hand-off report-back execution: the outbox job that delivers one summary turn.

``execute_report_back`` is the sole entry — run under the hook-outbox
drainer's lease, it POSTs the synthetic summary turn (job-deterministic
``request_key``), persists the dispatched run id for crash-resume, and holds
the job open until the summary run's row reaches terminal so the per-thread
ordering chain can't advance early.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import uuid

import redis.exceptions as redis_exceptions
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.server.services import automation_delivery
from src.server.services.report_back.handoff import leases, pointer, requested_from, wake
from src.server.services.report_back.handoff.keys import (
    ORIGIN_KIND,
    ORIGIN_THREAD_FIELD,
    ORIGIN_WORKSPACE_FIELD,
    decode,
    ptc_origin_key,
    rb_run_key,
    watch_key,
)
# DALTON-HOOK(handoff-keep-on-resumable): shared resumable-interruption check
from src.server.services.report_back.notify_turn import NotOwed, is_resumable_failure

if TYPE_CHECKING:
    from src.server.models.chat import AnalystReport

# Same hard-coded logger name request_prep uses — existing log routing keys off it.
logger = logging.getLogger("src.server.handlers.chat_handler")

# Namespace for the report-back POST's request_key: uuid5(NS, outbox job id).
# Deterministic per job, so a crash-and-reclaim re-POST dedups to the original
# summary run instead of starting a second one.
RB_REQUEST_NS = uuid.UUID("6f7a2b1c-9d3e-4f50-8a61-c2d4e5f60718")


def analyst_request_key(job_id: str) -> str:
    """One deterministic request identity per job: dedups the POST at the DB
    layer AND scopes the run-pointer claim/re-assert to this job."""
    return str(uuid.uuid5(RB_REQUEST_NS, job_id))


@dataclass(frozen=True)
class OwedAnalystReport:
    """An analyst's report still owed: the origin it reports under, the
    thread it reports to, and the run it reports on, as its finalize left
    it."""

    origin: dict
    dispatcher_thread_id: str
    ptc_thread_id: str
    final_status: str
    run_id: str | None = None
    # DALTON-HOOK(handoff-report-quiescence): set when the quiescence wait
    # hit its cap — the report the executor posts then names this still-open
    # work instead of presenting the run as a finished deliverable. The
    # LIST (empty included) is the independent "unfinished" state; whether
    # the listing could be read only words the message.
    unfinished: list[str] | None = None
    listing_failed: bool = False

    def steerable_into(self, thread_id: str) -> bool:
        """Whether a live turn on ``thread_id`` can take this report in. A
        steer carries no automation run's delivery stamp, and an outcome the
        report has no words for is its executor's to fail."""
        return (
            str(self.dispatcher_thread_id) == str(thread_id)
            and self.final_status in _OUTCOME_INSTRUCTIONS
            and not requested_from.deliveries(self.origin)
        )


# DALTON-HOOK(handoff-keep-on-resumable): log NotOwed exits and detect
# resumable worker-lost interruptions on the analyst thread.
_RESUMABLE_INTERRUPT_NOTICE = "运行被中断，系统会续跑，完成后会再回报。"


def _not_owed(
    reason: str,
    *,
    dispatcher_thread_id: str | None,
    ptc_thread_id: str | None,
    dispatch_gen: str | None,
) -> NotOwed:
    logger.info(
        "[HANDOFF_REPORT_BACK] NotOwed (%s): dispatcher_thread=%s "
        "ptc_thread=%s dispatch_gen=%s",
        reason,
        dispatcher_thread_id,
        ptc_thread_id,
        dispatch_gen,
    )
    return NotOwed(reason, wake=False)


async def is_resumable_analyst_failure(run_id: str | None) -> bool:
    """Whether the analyst run ``run_id`` ended with a resumable worker-lost
    interruption (and was not cancelled by the user)."""
    if not run_id:
        return False
    from src.server.database.runs import lifecycle as tl_db

    row = await tl_db.get_run(str(run_id))
    if not isinstance(row, dict) or row.get("status") != "error":
        return False
    metadata = row.get("metadata")
    if row.get("cancel_requested_at") or (
        isinstance(metadata, dict) and metadata.get("cancelled_by_user")
    ):
        return False
    replay_facts = row.get("replay_facts")
    error_frame = (
        replay_facts.get("error_frame") if isinstance(replay_facts, dict) else None
    )
    return any(
        is_resumable_failure(candidate)
        for candidate in (
            row.get("errors"),
            row.get("error"),
            row.get("failure_error"),
            error_frame,
        )
    )


async def analyst_report_verdict(cache, job: dict) -> OwedAnalystReport | NotOwed:
    """Whether the pair a ``report_back`` job names still reports back to a
    thread: the one eligibility its executor and a live turn decide by.

    Strict reads: raises on ANY unavailable state (blip, failed startup
    connect, config-off), so the drainer nacks instead of acking a dropped
    dispatch as "not report-back".
    """
    payload = job.get("payload") or {}
    ptc_thread_id = payload.get("ptc_thread_id")
    dispatch_gen = payload.get("dispatch_gen")
    # DALTON-HOOK(handoff-keep-on-resumable): log NotOwed exits at INFO
    if not ptc_thread_id:
        return _not_owed(
            "it names no analyst thread",
            dispatcher_thread_id=job.get("ordering_key"),
            ptc_thread_id=ptc_thread_id,
            dispatch_gen=dispatch_gen,
        )
    origin = await cache.get_strict(ptc_origin_key(ptc_thread_id))
    if (
        not isinstance(origin, dict)
        or origin.get("origin") != ORIGIN_KIND
        or not origin.get("report_back")
    ):
        return _not_owed(
            "its analyst reports to no thread",
            dispatcher_thread_id=(
                origin.get(ORIGIN_THREAD_FIELD)
                if isinstance(origin, dict)
                else job.get("ordering_key")
            ),
            ptc_thread_id=ptc_thread_id,
            dispatch_gen=dispatch_gen
            or (origin.get("dispatch_gen") if isinstance(origin, dict) else None),
        )
    dispatcher_thread_id = origin.get(ORIGIN_THREAD_FIELD)
    effective_gen = dispatch_gen or origin.get("dispatch_gen")
    # DALTON-HOOK(handoff-keep-on-resumable): log NotOwed exits at INFO
    if not dispatcher_thread_id or not origin.get("user_id"):
        return _not_owed(
            "its origin names no thread or owner",
            dispatcher_thread_id=dispatcher_thread_id or job.get("ordering_key"),
            ptc_thread_id=ptc_thread_id,
            dispatch_gen=effective_gen,
        )
    # Already terminal-cleared (duplicate completion event, or a reclaimed job
    # whose summary turn already finished and watch-cleared): nothing to do.
    # DALTON-HOOK(handoff-keep-on-resumable): log NotOwed exits at INFO
    if not await cache.client.sismember(watch_key(dispatcher_thread_id), ptc_thread_id):
        return _not_owed(
            "its pair was already cleared",
            dispatcher_thread_id=dispatcher_thread_id,
            ptc_thread_id=ptc_thread_id,
            dispatch_gen=effective_gen,
        )
    run_id = job.get("run_id")
    return OwedAnalystReport(
        origin=origin,
        dispatcher_thread_id=dispatcher_thread_id,
        ptc_thread_id=ptc_thread_id,
        # Rows enqueued before the field existed carry none, and only
        # completed runs were enqueued then.
        final_status=payload.get("final_status") or "completed",
        run_id=str(run_id) if run_id else None,
    )


async def execute_report_back(job: dict) -> None:
    """Execute one ``report_back`` outbox job: POST the summary turn, await terminal.

    Sole caller is the hook-outbox drainer — raising nacks the job (retry with
    backoff), returning acks it. Per-thread ordering is the outbox's ordering-key
    FIFO: this job stays open (lease-heartbeated, including through the POST's
    defer loop) until the summary run reaches terminal, so the next report-back
    on the same Chief of Staff thread can't POST early. Crash-safe resume: the dispatched
    run id is merged into the job payload right after the POST, and the POST
    itself carries a job-deterministic ``request_key`` so a re-POST before that
    merge lands adopts the original run via 409 duplicate_request. Every
    destructive exit re-verifies the lease fence first — a stale owner must
    not tear down state the reclaiming owner is executing against.
    """
    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.hook_outbox import LEASE_SECONDS
    from src.utils.cache.redis_cache import get_cache_client

    payload = job.get("payload") or {}
    ptc_thread_id = payload["ptc_thread_id"]
    dispatch_gen = payload.get("dispatch_gen")
    job_id = str(job["hook_outbox_id"])
    attempts = job["attempts"]
    # The failure budget the DB ceilings read: raw claim count minus the
    # reschedules a wait made (the SQL and this must agree — same helper).
    # DALTON-HOOK(handoff-report-quiescence)
    effective = outbox_db.effective_attempts(attempts, payload)
    job_request_key = analyst_request_key(job_id)

    async def _fence() -> bool:
        """Heartbeat + ownership check in one: extends our lease iff we still
        hold this claim generation."""
        return await outbox_db.extend_job_lease(
            job_id, LEASE_SECONDS, attempts=attempts
        )

    cache = get_cache_client()
    owed = await analyst_report_verdict(cache, job)
    if isinstance(owed, NotOwed):
        return
    origin, dispatcher_thread_id = owed.origin, owed.dispatcher_thread_id
    final_status = owed.final_status
    # A run that took in more than one hand-off from this Home carries one
    # generation on its row but owns them all; the pair's origin holds the
    # newest one reserved, and a report under any other is fenced out of the
    # teardown that frees Home's watch.
    origin_gen = origin.get("dispatch_gen")
    if (
        origin_gen
        and origin_gen != dispatch_gen
        and job.get("run_id")
        and await tl_db.run_has_dispatch_gen(str(job["run_id"]), origin_gen)
    ):
        dispatch_gen = origin_gen
    user_id = origin.get("user_id")
    # DALTON-HOOK(handoff-keep-on-resumable): stamp awaiting_resume on the
    # handoff pair's authoritative ptc_origin state BEFORE any requeue, park,
    # or POST when the analyst run ended in a resumable failure; revoke any
    # prior awaiting_resume marker before delivering a subsequent terminal run.
    analyst_run_id = str(job["run_id"]) if job.get("run_id") else None
    resumable = final_status == "error" and await is_resumable_analyst_failure(
        analyst_run_id
    )
    if resumable:
        if not await pointer.mark_awaiting_resume(
            cache,
            ptc_thread_id,
            analyst_run_id=str(analyst_run_id or ""),
            expected_gen=dispatch_gen,
        ):
            return
    # Claimed under a stale ordering key (pre-deploy row that finalized
    # unstamped, keyed on its own PTC thread): requeue onto the dispatching
    # thread's chain instead of executing here — N such jobs would busy-wait their
    # individual caps CONCURRENTLY against that one thread's admission gate
    # and can drop summaries permanently, where correctly-keyed rows just
    # wait their turn at the chain head. The fenced ack no-ops afterwards.
    if job.get("ordering_key") != dispatcher_thread_id:
        from src.server.services.hook_outbox import MAX_ATTEMPTS

        # DALTON-HOOK(handoff-keep-on-resumable): if this non-resumable terminal
        # job is about to exhaust attempts into dead_clear, revoke awaiting_resume first.
        if (
            not resumable
            and effective >= MAX_ATTEMPTS
            and isinstance(origin, dict)
            and origin.get(pointer.AWAITING_RESUME_FIELD)
        ):
            await pointer.clear_awaiting_resume(
                cache, ptc_thread_id, expected_gen=dispatch_gen
            )
            origin.pop(pointer.AWAITING_RESUME_FIELD, None)
        requeued = await outbox_db.requeue_job_with_key(
            job_id,
            attempts=attempts,
            ordering_key=dispatcher_thread_id,
            max_attempts=MAX_ATTEMPTS,
        )
        logger.info(
            f"[HANDOFF_REPORT_BACK] Requeued job {job_id} for {ptc_thread_id} "
            f"onto the chain of {dispatcher_thread_id} "
            f"(stale key {job.get('ordering_key')!r}, status={requeued})"
        )
        return

    rb_run_id = payload.get("dispatched_run_id")
    if rb_run_id:
        # DALTON-HOOK(handoff-keep-on-resumable): ensure awaiting_resume is
        # revoked when resuming terminal wait for a subsequent non-resumable run.
        if (
            not resumable
            and isinstance(origin, dict)
            and origin.get(pointer.AWAITING_RESUME_FIELD)
        ):
            await pointer.clear_awaiting_resume(
                cache, ptc_thread_id, expected_gen=dispatch_gen
            )
            origin.pop(pointer.AWAITING_RESUME_FIELD, None)
        logger.info(
            f"[HANDOFF_REPORT_BACK] Resuming in-flight report-back run {rb_run_id} "
            f"for {ptc_thread_id} on thread {dispatcher_thread_id} (no re-dispatch)"
        )
    else:
        # A live turn on the thread takes the report in as a steer before its
        # next model call (``report_back.steer``), as a task's report-back
        # does.
        from src.server.services.hook_outbox import MAX_ATTEMPTS
        from src.server.services.report_back.notify_turn import (
            park_while_thread_busy,
        )

        # DALTON-HOOK(handoff-keep-on-resumable): if parking at ceiling will
        # dead-letter this subsequent run, revoke awaiting_resume beforehand.
        if (
            not resumable
            and effective >= MAX_ATTEMPTS
            and isinstance(origin, dict)
            and origin.get(pointer.AWAITING_RESUME_FIELD)
        ):
            await pointer.clear_awaiting_resume(
                cache, ptc_thread_id, expected_gen=dispatch_gen
            )
            origin.pop(pointer.AWAITING_RESUME_FIELD, None)
        if await park_while_thread_busy(
            job_id,
            attempts,
            dispatcher_thread_id,
            log_prefix="[HANDOFF_REPORT_BACK]",
            subject=f"PTC thread {ptc_thread_id}",
        ):
            return

        async def _still_due() -> bool:
            return not await _superseded(
                cache, job.get("run_id"), ptc_thread_id, dispatcher_thread_id,
                job_request_key,
            )

        if not await _wait_for_hand_off(
            ptc_thread_id, dispatcher_thread_id, _fence, _still_due
        ):
            return  # lease lost: the reclaiming owner carries on

        # DALTON-HOOK(handoff-report-quiescence): a completed report goes out
        # only when the analyst thread is quiescent — no live turn, no open
        # background work, no notification turn still owed. The turn this job
        # names may have ended mid-work on purpose (ending the turn on a
        # subagent's completion notification is the designed waiting
        # posture). The wait RESCHEDULES the job rather than holding the
        # slot, and at its cap delivers an honest unfinished report.
        if final_status == "completed":
            hold = await _hold_report_for_quiescence(
                cache, job, ptc_thread_id, origin, attempts, _fence
            )
            if hold in ("rescheduled", "lost"):
                return  # slot freed: the job returns when due, reclaimed, or capped
            if hold == "deliver-unfinished":
                # Unfinished is its own state, decided by the cap alone: the
                # listing's readability only words the message, never whether
                # the report says completed.
                items, listing_failed = await _pending_work_listing(ptc_thread_id)
                owed = dataclasses.replace(
                    owed, unfinished=items, listing_failed=listing_failed
                )

        # DALTON-HOOK(handoff-keep-on-resumable): revoke awaiting_resume before
        # delivering a subsequent terminal report so its watch_clear takes effect.
        if (
            not resumable
            and isinstance(origin, dict)
            and origin.get(pointer.AWAITING_RESUME_FIELD)
        ):
            await pointer.clear_awaiting_resume(
                cache, ptc_thread_id, expected_gen=dispatch_gen
            )
            origin.pop(pointer.AWAITING_RESUME_FIELD, None)
        outcome, rb_run_id = await _post_report_back(
            cache,
            owed,
            request_key=job_request_key,
            heartbeat=_fence,
            dispatch_gen=dispatch_gen,
            still_due=_still_due,
            resumable=resumable,
        )

        if outcome == "lost":
            # Lease lost inside the POST defer loop: the reclaiming owner is
            # (or will be) executing this job; do nothing further. The
            # drainer's fenced ack no-ops.
            return
        if outcome == "superseded":
            # A newer run on the thread owes its own report-back under the
            # same pair, so this job releases nothing and wakes nothing: the
            # pair is still pending, and a clear here would drop that run's
            # report. Acking only advances the chain, which needs no fence.
            return
        if outcome in ("deleted", "drop", "cap"):
            # Key-lock + row-lock fence held ACROSS the teardown (not just
            # checked before it): while held, no sibling claim on this
            # ordering key can be gated and this row can't be reclaimed — a
            # paused stale owner can't clear state a newer incarnation has
            # re-established.
            async with outbox_db.fenced_job_guard(job_id, attempts) as owned:
                if not owned:
                    return  # reclaimed: the live owner decides the teardown
                if outcome == "deleted":
                    # The Chief of Staff thread is gone (404). Nothing will
                    # consume these report-backs; clear every watch member.
                    await _discard_dispatcher_thread(cache, dispatcher_thread_id)
                else:
                    # Terminal rejection or exhausted defer-wait. Clear this
                    # member so the chain advances; otherwise the next
                    # report-back on this thread would wait behind a
                    # summary turn that never starts. Pointer-gated: a POST's
                    # server route can outlive the client's socket timeout and
                    # still sit pre-START inside a lawful admission hold — the
                    # chain lease serializes THIS executor, not that in-flight
                    # route (round-5 F1). Its live claim fences the teardown
                    # atomically (and a route that has not claimed yet is
                    # refused by the claim script's membership gate once the
                    # clear lands); on refusal we nack, and the retry adopts
                    # the claim's run row, takes over its corpse, or finds
                    # the claim released.
                    # DALTON-HOOK(handoff-keep-on-resumable): clear_handoff_report_back
                    # skips teardown and sets awaiting_resume=True when the pair
                    # is awaiting resume of a resumable analyst interruption.
                    cleared = await pointer.clear_handoff_report_back(
                        cache, ptc_thread_id, dispatcher_thread_id, user_id=user_id,
                        expected_gen=dispatch_gen, refuse_if_pointer=True,
                    )
                    if (
                        not cleared
                        and not cleared.awaiting_resume
                        and cleared.fencer_gen is None
                    ):
                        raise RuntimeError(
                            f"report-back drop for {ptc_thread_id} refused: a "
                            f"live run pointer on thread {dispatcher_thread_id} "
                            f"may still be mid-admission; nacking to retry"
                        )
                    if cleared and final_status != "completed":
                        # No summary will tell the user about the stop or the
                        # failure, so the card has to: the wake a failed
                        # dispatch always sent before it got a summary.
                        await wake.publish_wake(
                            cache, dispatcher_thread_id,
                            error="background_workflow_failed",
                        )
                    elif cleared:
                        # Watchers still read this member as owed; without a
                        # wake they hold that until their watch recycles.
                        await wake.publish_wake(cache, dispatcher_thread_id, cleared=True)
            return

        # outcome == "dispatched"
        if rb_run_id is None:
            # POSTed but the response body didn't yield a run id — we can't
            # await its terminal, and acking would release the chain before
            # this summary finishes. Nack: the retry re-POSTs the SAME
            # request_key and recovers the id via 409 duplicate_request (or
            # the admission run-pointer claim).
            raise RuntimeError(
                f"report-back for {ptc_thread_id} dispatched without a run_id; "
                f"nacking to recover it via request_key dedup"
            )

        # Durable resume pointer FIRST: after this merge lands, a crash-and-
        # reclaim resumes the terminal wait instead of re-POSTing. (Before it
        # lands, the request_key dedup makes the re-POST safe.) A merge failure
        # is therefore tolerable — log, don't nack, the effect already ran.
        try:
            await outbox_db.merge_job_payload(job_id, {"dispatched_run_id": rb_run_id})
            # DALTON-HOOK(handoff-keep-on-resumable): mirror dispatched_run_id in-memory for retry tests
            if isinstance(job.get("payload"), dict):
                job["payload"]["dispatched_run_id"] = rb_run_id
        except Exception:
            logger.warning(
                f"[HANDOFF_REPORT_BACK] Failed persisting dispatched_run_id "
                f"{rb_run_id} on job {job_id}; request_key dedup covers a re-POST",
                exc_info=True,
            )

    # Both paths (fresh dispatch AND crash-resume) confirm the run pointer and
    # publish INSIDE one fence window. The pointer re-assert is belt-and-
    # suspenders for a degraded-cache admission and lets a reloading client
    # reattach via /status; atomically gated on membership + absent-or-same-run
    # (Lua) so a fast-terminal clear is never resurrected and a paused stale
    # owner can't repoint a re-dispatched pair at its dead run. The wake is
    # published only while the fence is held AND the pointer is confirmed
    # current — a stale resume must not wake clients toward a dead run.
    async with outbox_db.fenced_job_guard(job_id, attempts) as owned:
        if not owned:
            return  # reclaimed: the live owner publishes and awaits
        pointer_ok = True
        try:
            pointer_ok = await pointer.reassert_run_pointer(
                cache,
                dispatcher_thread_id,
                ptc_thread_id,
                rb_run_id,
                dispatch_gen=dispatch_gen,
                request_key=job_request_key,
            )
        except Exception:
            pass  # transient Redis failure: the wake is best-effort anyway
        if pointer_ok:
            await wake.publish_wake(cache, dispatcher_thread_id, run_id=rb_run_id)
        else:
            logger.warning(
                f"[HANDOFF_REPORT_BACK] Pointer for {ptc_thread_id} on "
                f"thread {dispatcher_thread_id} refused run {rb_run_id}; not waking"
            )

    await _await_run_terminal(
        job_id, attempts, rb_run_id, dispatcher_thread_id, ptc_thread_id, user_id,
        dispatch_gen,
    )


async def _await_run_terminal(
    job_id: str,
    attempts: int,
    rb_run_id: str,
    dispatcher_thread_id: str,
    ptc_thread_id: str,
    user_id: str | None,
    dispatch_gen: str | None = None,
) -> None:
    """Hold the report-back job open until its summary run reaches terminal.

    Polls the durable run row (NOT watch membership — the member is removed by
    the summary run's own watch_clear job, which queues BEHIND this one on the
    same ordering key; waiting on it would deadlock until the cap). On timeout
    (never terminal, or the row vanished for good — thread deleted) it
    force-clears the pair so the chain and dispatch caps can't stay wedged;
    on a lost lease it stands down with NO teardown — the reclaiming owner
    resumes via ``dispatched_run_id``.
    """
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.report_back.notify_turn import await_run_terminal
    from src.utils.cache.redis_cache import get_cache_client

    outcome = await await_run_terminal(
        job_id,
        attempts,
        rb_run_id,
        wait_cap=leases.RB_TERMINAL_WAIT_CAP,
        log_prefix="[HANDOFF_REPORT_BACK]",
    )
    if outcome != "timeout":
        return
    # Row-lock fence held across the clear (see fenced_job_guard) — a
    # heartbeat alone can't cover a pause between check and mutation.
    logger.warning(
        f"[HANDOFF_REPORT_BACK] Terminal wait cap hit for {ptc_thread_id} "
        f"on thread {dispatcher_thread_id}; clearing"
    )
    cache = get_cache_client()
    async with outbox_db.fenced_job_guard(job_id, attempts) as owned:
        if not owned:
            return
        # DALTON-HOOK(handoff-keep-on-resumable): the teardown Lua refuses a
        # pair still awaiting resume, and then the unfinished summary turn's
        # pointer stays too; an unmarked pair clears as upstream does.
        cleared = await pointer.clear_handoff_report_back(
            cache,
            ptc_thread_id,
            dispatcher_thread_id,
            user_id=user_id,
            expected_gen=dispatch_gen,
            release_pointer_when_kept=False,
        )
        if cleared:
            await wake.publish_wake(cache, dispatcher_thread_id, cleared=True)


async def _workspace_name(workspace_id: str | None) -> str | None:
    """The workspace's name, as the summary turn should name it to the user.

    The agent repeats it in its summary, and the user knows a workspace by its
    name, never its id; the thread id alone reads the result. A failed read
    names no workspace rather than hold the report-back.
    """
    if workspace_id:
        from src.server.database.workspace import (
            get_workspace_name_and_description,
        )

        try:
            row = await get_workspace_name_and_description(workspace_id)
        except Exception:
            logger.debug(
                f"[HANDOFF_REPORT_BACK] Could not read the name of workspace "
                f"{workspace_id}",
                exc_info=True,
            )
            return None
        if row and row.get("name"):
            return row["name"]
    return None


# DALTON-HOOK(colleague-dispatch): read colleague owner name for report-back
async def _workspace_owner_name(
    workspace_id: str | None, dispatcher_user_id: str | None = None
) -> str | None:
    if not workspace_id or not dispatcher_user_id:
        return None
    from src.server.database.workspace import get_workspace

    try:
        ws = await get_workspace(workspace_id)
        ws_owner_id = str((ws or {}).get("user_id") or "")
        if ws_owner_id and ws_owner_id != str(dispatcher_user_id):
            from src.server.database.user import get_user

            owner_user = await get_user(ws_owner_id)
            return (
                str((owner_user or {}).get("name") or "").strip()
                or str((owner_user or {}).get("email") or "").strip()
                or ws_owner_id
            )
    except Exception:
        logger.debug(
            f"[HANDOFF_REPORT_BACK] Could not read owner of workspace {workspace_id}",
            exc_info=True,
        )
    return None


# DALTON-HOOK(colleague-dispatch): helper combining workspace name and colleague owner name
async def _workspace_name_and_owner(
    workspace_id: str | None, dispatcher_user_id: str | None = None
) -> tuple[str | None, str | None]:
    ws_name = await _workspace_name(workspace_id)
    owner_name = await _workspace_owner_name(
        workspace_id, dispatcher_user_id=dispatcher_user_id
    )
    return ws_name, owner_name


# How the summary turn is told the run ended. A run that did not complete
# must never be summarized as finished work: the agent would hand the user a
# deliverable that does not exist. So an unknown status raises rather than
# reading as completed: the job nacks, and once dead its compensation tears
# the pair down with an error wake.
_OUTCOME_INSTRUCTIONS = {
    "completed": "has completed.",
    "cancelled": (
        "was stopped before it finished. Do not present partial work as finished."
    ),
    "error": "failed before it finished. Do not present its work as finished.",
}


async def _reported_runs(ptc_thread_id: str, run_id) -> list[str]:
    """The reported run and the interrupted runs it continued, oldest first.
    A read that fails or finds nothing names the reported run alone rather
    than hold the report."""
    if not run_id:
        return []
    # DALTON-HOOK(handoff-keep-on-resumable): extend continued-run chain across
    # prior resumable worker-lost runs on the same analyst thread.
    from src.dalton.database import runs as dalton_runs_db
    from src.server.database.runs import lifecycle as tl_db

    try:
        chain = await tl_db.get_continued_runs(ptc_thread_id, str(run_id))
    except Exception:
        logger.debug(
            f"[HANDOFF_REPORT_BACK] Could not read the runs {run_id} continued",
            exc_info=True,
        )
        chain = []
    base_chain = list(chain) if chain else [str(run_id)]
    limit = getattr(tl_db, "_CONTINUED_RUNS_LIMIT", 50)
    try:
        extended = list(base_chain)
        while len(extended) < limit:
            prev = await dalton_runs_db.get_previous_run_on_thread(
                ptc_thread_id, extended[0]
            )
            if not isinstance(prev, dict):
                break
            prev_id = prev.get("conversation_response_id")
            if not prev_id or str(prev_id) in extended:
                break
            prev_id = str(prev_id)
            prev_status = prev.get("status")
            if prev_status == "interrupted":
                extended.insert(0, prev_id)
            elif (
                prev_status == "error"
                and await _retries(extended[0], prev_id)
                and await is_resumable_analyst_failure(prev_id)
            ):
                # Only the /retry of that run continues it; a fresh turn after
                # a resumable failure answers its own hand-off, not this one.
                extended.insert(0, prev_id)
            else:
                break
        return extended
    except Exception:
        logger.debug(
            f"[HANDOFF_REPORT_BACK] Could not read resumable runs before {run_id}",
            exc_info=True,
        )
        return base_chain


async def _retries(run_id: str, failed_run_id: str) -> bool:
    """Whether ``run_id`` is the retry attempt of ``failed_run_id``."""
    from src.server.database.runs import lifecycle as tl_db

    row = await tl_db.get_run(str(run_id))
    return isinstance(row, dict) and str(row.get("retry_of_run_id") or "") == str(
        failed_run_id
    )


async def _taken_hand_offs(
    ptc_thread_id: str, run_ids: list[str]
) -> list[str] | None:
    """The hand-off steers ``run_ids`` took in, oldest first. None when that
    is unknown, with no run to ask about or a read that failed, since a view
    takes any list as final and could then never match a steered hand-off
    to this report; the report goes all the same."""
    if not run_ids:
        return None
    from src.server.database.runs import outbox as outbox_db

    try:
        return await outbox_db.list_taken_hand_offs(ptc_thread_id, run_ids)
    except Exception:
        logger.debug(
            f"[HANDOFF_REPORT_BACK] Could not read the hand-offs {run_ids} took in",
            exc_info=True,
        )
        return None


async def report_message(
    owed: OwedAnalystReport,
    *,
    steered: bool = False,
    # DALTON-HOOK(handoff-keep-on-resumable): a caller that already evaluated
    # the resumable verdict (the executor and steer both stamp awaiting_resume
    # off it) hands it in; None evaluates it here from the run row.
    resumable: bool | None = None,

) -> tuple[str, "AnalystReport"]:
    """What the report says to the agent, as its own turn or as a steer, and
    the source history labels it with: the message is model-facing and
    hidden, so the label is all a reader sees. A steer carries the request's
    reminder inline, where its own turn sends it as additional context."""
    from src.server.models.chat import AnalystReport

    ptc_thread_id, origin = owed.ptc_thread_id, owed.origin
    final_status = owed.final_status
    ptc_workspace_id = origin.get("ptc_workspace_id")
    # DALTON-HOOK(colleague-dispatch): include workspace_owner when reporting from a colleague's workspace
    ws_name = await _workspace_name(ptc_workspace_id)
    ws_owner_name = await _workspace_owner_name(
        ptc_workspace_id, dispatcher_user_id=origin.get("user_id")
    )
    if ws_name and ws_owner_name:
        ws_label = f'the "{ws_name}" workspace (workspace_owner="{ws_owner_name}")'
    elif ws_name:
        ws_label = f'the "{ws_name}" workspace'
    else:
        ws_label = "a workspace"
    # DALTON-HOOK(handoff-report-quiescence): the wait cap's honest report.
    # The run itself ended, but work it depended on is still open, so the one
    # thing the line must never say is "has completed" — whichever way the
    # listing read went.
    if owed.unfinished is not None:
        if owed.unfinished:
            pending = "、".join(owed.unfinished)
        elif owed.listing_failed:
            pending = "清单读取失败"
        else:
            pending = "未能列出具体条目"
        outcome_line = (
            f"The analysis you dispatched (thread {ptc_thread_id} in {ws_label}) "
            f"分析师回合已结束，但后台仍有工作未完成（{pending}），"
            "请勿当作已完成交付。"
        )
    else:
        outcome_line = (
            f"The analysis you dispatched (thread {ptc_thread_id} in {ws_label}) "
            f"{_OUTCOME_INSTRUCTIONS[final_status]}"
        )
    # DALTON-HOOK(handoff-keep-on-resumable): tell Home when a failed analyst
    # run was a resumable interruption that will report back again on finish.
    is_resumable = False
    if final_status == "error":
        is_resumable = bool(
            await is_resumable_analyst_failure(owed.run_id)
            if resumable is None
            else resumable
        )
        if is_resumable:
            outcome_line += f"\n{_RESUMABLE_INTERRUPT_NOTICE}"
    # DALTON-HOOK(i18n): Prevent language drift after English analyst report-back notices
    message = (
        "<system>\n"
        f"{outcome_line}\n"
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        "</system>"
    )
    reminder = requested_from.reminder(origin) if steered else None
    if reminder:
        from src.server.models.additional_context import DirectiveContext
        from src.server.utils.directive_context import build_directive_reminder

        message += build_directive_reminder([DirectiveContext(content=reminder)]) or ""
    run_ids = await _reported_runs(ptc_thread_id, owed.run_id)
    report_source = AnalystReport(
        kind="analyst",
        thread_id=ptc_thread_id,
        workspace_id=str(ptc_workspace_id) if ptc_workspace_id else None,
        workspace_name=ws_name,
        # DALTON-HOOK(colleague-dispatch): attach colleague workspace owner name to AnalystReport
        workspace_owner=ws_owner_name,
        # One of the three the message has words for, or it raised above.
        outcome=final_status,
        # DALTON-HOOK(handoff-report-quiescence): the wait cap's report names
        # the work it still sees open.
        unfinished=owed.unfinished,
        # Matches the report to its hand-off, whose result named the first.
        run_ids=run_ids or None,
        # And to a hand-off steered into a run that ended before reading it,
        # whose result named the steer. Empty when the runs took none in;
        # absent when that is unknown.
        steer_ids=await _taken_hand_offs(ptc_thread_id, run_ids),
        # DALTON-HOOK(handoff-keep-on-resumable): mark interim resumable report
        resumable=True if is_resumable else None,
    )
    return message, report_source


async def _post_report_back(
    cache,
    owed: OwedAnalystReport,
    *,
    request_key: str | None = None,
    heartbeat=None,
    dispatch_gen: str | None = None,
    still_due=None,
    resumable: bool | None = None,
) -> tuple[str, str | None]:
    """POST the synthetic report-back message to the Chief of Staff thread.

    Builds the hand-off body (summary prompt, watch-member identity,
    dispatch generation, where the work was asked for) and delegates the
    admission-aware defer loop to the shared ``post_notification_turn``.
    Returns its ``(outcome, run_id)``:
    ``"dispatched"`` / ``"drop"``/``"cap"`` (caller clears the member) /
    ``"deleted"`` (caller discards the watch) / ``"lost"`` (caller stops,
    no teardown) / ``"superseded"`` (no turn started, nothing to tear down).
    """
    from src.server.services.report_back.notify_turn import post_notification_turn

    ptc_thread_id, origin = owed.ptc_thread_id, owed.origin
    # DALTON-HOOK(handoff-keep-on-resumable): use the resumable verdict already
    # computed by execute_report_back, or evaluate it when called directly.
    if resumable is None:
        resumable = (
            owed.final_status == "error"
            and await is_resumable_analyst_failure(owed.run_id)
        )
    message, report_source = await report_message(owed, resumable=resumable)
    body = {
        "messages": [{"role": "user", "content": message}],
        # The dispatching thread's workspace, which is Home.
        "workspace_id": origin.get(ORIGIN_WORKSPACE_FIELD),
        "query_type": "system",
        # Lets the report-back run identify which watch member to clear
        # on its own completion.
        "report_back_ptc_thread_id": ptc_thread_id,
        # The pair's dispatch generation rides into the summary run's START
        # metadata so its consumption watch_clear is fenced to THIS incarnation.
        "origin_dispatch_gen": dispatch_gen,
        "report_source": report_source.model_dump(exclude_none=True),
    }
    # Where the work was asked for: the turn is reminded, and each automation
    # run a request came from rides into its config so a send to a run's
    # targets is that run's own. A record from before this was kept names
    # nothing.
    reminder = requested_from.reminder(origin)
    if reminder:
        body["additional_context"] = [{"type": "directive", "content": reminder}]
    runs = requested_from.deliveries(origin)
    if runs:
        body["automation_delivery"] = automation_delivery.stamp(runs[-1])
    if len(runs) > 1:
        body[automation_delivery.EARLIER_KEY] = [
            automation_delivery.stamp(run) for run in runs[:-1]
        ]
    if request_key:
        body["request_key"] = request_key
    # DALTON-HOOK(handoff-keep-on-resumable): dispatch summary turn after
    # awaiting_resume has been atomically stamped on ptc_origin.
    return await post_notification_turn(
        thread_id=owed.dispatcher_thread_id,
        body=body,
        user_id=origin.get("user_id"),
        wait_cap=leases.RB_BUSY_WAIT_CAP,
        heartbeat=heartbeat,
        log_prefix="[HANDOFF_REPORT_BACK]",
        subject=f"PTC thread {ptc_thread_id}",
        still_due=still_due,
    )


# How often a report waiting on a hand-off re-reads it.
_HAND_OFF_POLL_S = 2.0

# DALTON-HOOK(handoff-report-quiescence): the pending-work probe and the
# rescheduling hold that keeps a "completed" report from firing while the
# dispatched thread is still working — a turn that ends with background
# subagents live is a pause, not a finish, and the summary would hand the
# Chief of Staff a deliverable that does not exist yet.
# The report-backs on the dispatched thread's own chain that will run a turn
# of it when they land: a subagent's and a background command's. A
# ``command_notice`` is deliberately NOT here: by design it wakes nobody (it
# waits for the thread's next turn under its own prefixed ordering key, rows
# parked at ``next_retry_at='infinity'``), so it is invisible to the
# thread-keyed open-jobs read either way — running commands themselves are
# covered by the ``command_runs`` check below. Market alerts and steers need
# no row here either: they do not start a turn while the thread is idle, and
# once one does start, the latest-attempt branch of the probe reads it as
# work in motion.
_THREAD_WAKE_JOB_TYPES = ("task_report_back", "command_report_back")


async def analyst_thread_quiescent(ptc_thread_id: str) -> bool:
    """One look: whether the dispatched thread has stopped working for now.

    Not quiescent while a turn of its own is live or waiting on the user, a
    background subagent run is still open, a background command is still
    running, or a report-back job is still owed a notification turn (the
    wake that continues the analysis). A read that fails reads as not
    quiescent: a blind "completed" is exactly the false report this exists
    to stop.
    """
    from src.server.database.runs import command_runs as cr_db
    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import outbox as outbox_db
    from src.server.database.runs import subagent_runs as sr_db

    try:
        latest = await tl_db.get_latest_attempt(ptc_thread_id)
        if latest and latest.get("status") in ("in_progress", "interrupted"):
            return False
        if await sr_db.list_open_runs_for_thread(ptc_thread_id):
            return False
        if await cr_db.list_running(thread_id=ptc_thread_id):
            return False
        return not await outbox_db.list_open_notification_jobs(
            ptc_thread_id, _THREAD_WAKE_JOB_TYPES, limit=1
        )
    except Exception:
        logger.warning(
            f"[HANDOFF_REPORT_BACK] Quiescence read failed for {ptc_thread_id}; "
            f"holding its report",
            exc_info=True,
        )
        return False


# The reschedule cadence for a report waiting on the analyst's background
# work: start at 30s, double to a 5-minute ceiling. A wait costs no failure
# budget (``reschedule_claimed_job`` bumps the payload's ``reschedules``
# counter, which every attempts ceiling counts out), so the cadence is
# purely politeness toward the claim loop.
_QUIESCENCE_FIRST_POLL_S = 30.0
_QUIESCENCE_MAX_POLL_S = 300.0
# Payload keys the wait keeps on the job row: when the wait started (the
# cap's anchor — the row's created_at moves with each reschedule) and the
# reschedule count (the backoff ladder; maintained by the reschedule's own
# atomic payload bump).
_WAIT_SINCE_KEY = "quiescence_wait_since"
_RESCHEDULES_KEY = "reschedules"
def _quiescence_cap() -> float:
    """How long a completed report may wait before it reports the work
    unfinished instead (see ``get_report_back_quiescence_cap``)."""
    from src.config.settings import get_report_back_quiescence_cap

    return float(get_report_back_quiescence_cap())


async def _analyst_turn_end(ptc_thread_id: str) -> float | None:
    """When the analyst's latest turn ended, as the wait cap's anchor.

    ``usage_settled_at`` is stamped by the finalize CAS (the run's own
    "settled" convention, as liveness reads treat it); a row from before it
    existed falls back to its start plus measured duration. None when the
    row or its timestamps cannot be read."""
    from datetime import UTC, datetime

    from src.server.database.runs import lifecycle as tl_db

    try:
        latest = await tl_db.get_latest_attempt(ptc_thread_id)
    except Exception:
        logger.warning(
            f"[HANDOFF_REPORT_BACK] Latest-attempt read failed anchoring the "
            f"wait for {ptc_thread_id}",
            exc_info=True,
        )
        return None
    if not isinstance(latest, dict):
        return None
    settled = latest.get("usage_settled_at")
    if isinstance(settled, datetime):
        if settled.tzinfo is None:
            settled = settled.replace(tzinfo=UTC)
        return settled.timestamp()
    started = latest.get("created_at")
    execution = latest.get("execution_time")
    if isinstance(started, datetime) and isinstance(execution, (int, float)):
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return started.timestamp() + float(execution)
    return None


async def _renew_pair_ttls(
    cache, ptc_thread_id: str, origin: dict, delay_seconds: float
) -> None:
    """Extend the pair's Redis keys for the length of the wait — never
    shortening them.

    The wait can outlive the keys' 24h TTLs (the cap anchors on the turn's
    end, which may precede the first claim by hours); an expired origin or
    watch membership would take the cap report's own verdict away
    (``analyst_report_verdict`` reads the origin strictly and gates on
    membership). But the watch and user-pending keys are SHARED by every
    pair the same Home dispatched: an unconditional EXPIRE here once
    trampled a sibling pair's full 24h (set at reserve, keys.py) down to
    minutes, and its report died as NotOwed. So each key is only ever
    extended — to the reserve-time TTL constant, the same strength a fresh
    dispatch holds — and an extension that cannot be made safely (the
    read fails) leaves the key exactly as it was. Best-effort: a dropped
    renewal is healed by the next one."""
    from src.server.services.report_back.handoff.keys import (
        PTC_ORIGIN_TTL,
        ptc_origin_key,
        user_pending_key,
        watch_key,
    )

    ttl = int(max(delay_seconds * 3.0, float(PTC_ORIGIN_TTL)))
    pair_keys = [
        ptc_origin_key(ptc_thread_id),
        watch_key(str(origin.get("home_thread_id") or "")),
    ]
    user_id = origin.get("user_id")
    if user_id:
        pair_keys.append(user_pending_key(str(user_id)))
    for key in pair_keys:
        await _extend_key_ttl(cache.client, key, ttl)


async def _extend_key_ttl(client, key: str, seconds: int) -> None:
    """One key's TTL extended to ``seconds``; never shortened, never dropped.

    Prefers ``EXPIRE key seconds GT`` (Redis ≥ 7 and redis-py's matching
    option): the server itself refuses to shorten. A client without the
    option or a server that rejects it falls back to reading the live TTL
    and writing back only the larger value — a read that fails or names no
    TTL-bearing key (-2 gone, -1 immortal) leaves the key untouched. Any
    other failure is a transport problem: log and leave the key alone."""
    try:
        await client.expire(key, seconds, gt=True)
        return
    except TypeError:
        pass  # a client whose expire takes no gt — read-compare-write below
    except redis_exceptions.ResponseError:
        pass  # an older server rejecting the option — same fallback
    except Exception:
        logger.warning(
            f"[HANDOFF_REPORT_BACK] TTL extend failed for {key}", exc_info=True
        )
        return
    try:
        current = await client.ttl(key)
    except Exception:
        logger.warning(
            f"[HANDOFF_REPORT_BACK] TTL read failed extending {key}; "
            f"leaving it as is",
            exc_info=True,
        )
        return
    if not isinstance(current, int) or current < 0:
        return  # gone (-2) or immortal (-1): not ours to touch
    if seconds > current:
        try:
            await client.expire(key, seconds)
        except Exception:
            logger.warning(
                f"[HANDOFF_REPORT_BACK] TTL write-back failed for {key}",
                exc_info=True,
            )


async def _pending_work_listing(ptc_thread_id: str) -> tuple[list[str], bool]:
    """What the analyst thread still has running, for the honest report the
    wait cap sends: open background subagent runs and running commands, by
    the ids the agent knows them by. Returns the items and whether any read
    failed — the cap report is unfinished either way (its own state), and
    the reads only decide whether the listing can name the work."""
    from src.server.database.runs import command_runs as cr_db
    from src.server.database.runs import subagent_runs as sr_db

    items: list[str] = []
    reads_failed = False
    try:
        for row in await sr_db.list_open_runs_for_thread(ptc_thread_id):
            items.append(f"Task-{row.get('task_id')}")
    except Exception:
        logger.warning(
            f"[HANDOFF_REPORT_BACK] Open-run read failed for {ptc_thread_id} "
            f"at the wait cap",
            exc_info=True,
        )
        reads_failed = True
    try:
        for row in await cr_db.list_running(thread_id=ptc_thread_id):
            items.append(f"command {row.get('command_id')}")
    except Exception:
        logger.warning(
            f"[HANDOFF_REPORT_BACK] Running-command read failed for "
            f"{ptc_thread_id} at the wait cap",
            exc_info=True,
        )
        reads_failed = True
    return items, reads_failed


async def _hold_report_for_quiescence(
    cache, job: dict, ptc_thread_id: str, origin: dict, attempts: int, fence
) -> str:
    """Decide one claim of a completed report against the analyst's background
    work: ``"rescheduled"`` (not quiescent — the job re-appears due in
    30s..5min at the back of its chain, the slot is freed at once),
    ``"deliver"`` (quiescent — the report goes out as completed), or
    ``"deliver-unfinished"`` (the wait cap hit — an honest report goes out
    naming the work still pending, never "has completed"). ``"lost"`` when
    the lease went: the reclaiming owner decides the next claim.

    Never holds the slot: a waiting report-back must not sit on one of the
    drainer's bounded slots — enough of them would starve the very
    ``task_report_back`` jobs whose turns end the wait.
    """
    from src.server.database.runs import outbox as outbox_db

    if await analyst_thread_quiescent(ptc_thread_id):
        return "deliver"

    payload = job.get("payload") or {}
    wait_since = payload.get(_WAIT_SINCE_KEY)
    try:
        wait_since = float(wait_since)
    except (TypeError, ValueError):
        wait_since = None
    now = time.time()
    if wait_since is None:
        # Anchor on when the analyst's turn actually ENDED, so a report that
        # waits hours before its first claim cannot outlive the cap — and a
        # turn that ended long ago reaches the cap that much sooner. The
        # anchor is fixed in the payload at first sight and survives every
        # later claim; unreadable, it degrades to now.
        wait_since = await _analyst_turn_end(ptc_thread_id) or now
    if now - wait_since >= _quiescence_cap():
        logger.warning(
            f"[HANDOFF_REPORT_BACK] Wait cap hit for {ptc_thread_id}; "
            f"reporting the work unfinished rather than completed"
        )
        return "deliver-unfinished"

    try:
        polls = int(payload.get(_RESCHEDULES_KEY) or 0)
    except (TypeError, ValueError):
        polls = 0
    delay = min(_QUIESCENCE_FIRST_POLL_S * (2**polls), _QUIESCENCE_MAX_POLL_S)
    # The anchor is durable before the reschedule: a crash between them
    # re-enters with it intact. Unfenced by design (merge_job_payload); the
    # reschedule's own payload bump keeps the ladder count atomic.
    await outbox_db.merge_job_payload(
        str(job["hook_outbox_id"]),
        {_WAIT_SINCE_KEY: wait_since},
    )
    if not await fence():
        return "lost"  # lease lost: the reclaiming owner reschedules or delivers
    status = await outbox_db.reschedule_claimed_job(
        str(job["hook_outbox_id"]), attempts=attempts, delay_seconds=delay
    )
    if status is None:
        return "lost"
    await _renew_pair_ttls(cache, ptc_thread_id, origin, delay)
    logger.info(
        f"[HANDOFF_REPORT_BACK] Analyst thread {ptc_thread_id} still has "
        f"background work; report_back job {job['hook_outbox_id']} "
        f"rescheduled in {delay:.0f}s (waited {now - wait_since:.0f}s of the "
        f"cap)"
    )
    return "rescheduled"



async def _wait_for_hand_off(
    ptc_thread_id: str, dispatcher_thread_id: str, fence, still_due
) -> bool:
    """Hold the report while a hand-off from the same Home thread waits on the
    analyst's thread: the run that reads it reports back, and ``_superseded``
    then acks this one, so the Home thread hears once. A hand-off dropped
    unread leaves this report to go. The wait ends as soon as a newer run has
    taken this report over, as nothing is left to wait for, and past the cap
    it goes anyway: the later run's report then finds the pair cleared. False
    when the lease is lost."""
    from src.server.database.runs import outbox as outbox_db

    loop = asyncio.get_running_loop()
    deadline = loop.time() + leases.RB_BUSY_WAIT_CAP
    while await outbox_db.has_live_hand_off(ptc_thread_id, dispatcher_thread_id):
        if not await still_due():
            return True
        if loop.time() >= deadline:
            logger.warning(
                f"[HANDOFF_REPORT_BACK] Hand-off into {ptc_thread_id} from "
                f"{dispatcher_thread_id} still pending past the cap; reporting anyway"
            )
            return True
        if not await fence():
            return False
        await asyncio.sleep(_HAND_OFF_POLL_S)
    return True


async def summary_started(
    cache, dispatcher_thread_id: str, ptc_thread_id: str, request_key: str
) -> bool:
    """Whether this job's summary run already started: a run row under its
    request key, or the run pointer its executor claims before posting. A
    started summary is past admission, to be awaited, never abandoned or
    taken in again."""
    from src.server.database.runs import lifecycle as tl_db

    if await tl_db.find_run_by_request_key(request_key):
        return True
    claim = await cache.get_strict(rb_run_key(dispatcher_thread_id, ptc_thread_id))
    return isinstance(claim, dict) and claim.get("request_key") == request_key


async def overtaking_run(run_id, ptc_thread_id: str) -> str | None:
    """The newer run on the analyst's thread that took over ``run_id``'s
    report-back, if one started: the summary turn names only the thread, and
    ``agent_output`` reads its latest turns, so a summary posted after a
    newer run starts would describe that run's output as this one's. The
    newer run carries the same pair and reports back when it ends."""
    from src.server.database.runs import lifecycle as tl_db

    if not run_id:
        return None
    latest = await tl_db.get_latest_attempt(ptc_thread_id)
    if not latest or str(latest["conversation_response_id"]) == str(run_id):
        return None
    return str(latest["conversation_response_id"])


async def _superseded(
    cache,
    run_id,
    ptc_thread_id: str,
    dispatcher_thread_id: str,
    request_key: str,
) -> bool:
    """Whether a newer run on the thread has taken over this run's report-back
    and this job's own summary never started."""
    newer = await overtaking_run(run_id, ptc_thread_id)
    if newer is None or await summary_started(
        cache, dispatcher_thread_id, ptc_thread_id, request_key
    ):
        return False
    logger.info(
        f"[HANDOFF_REPORT_BACK] Run {run_id} on {ptc_thread_id} is superseded by "
        f"run {newer}; its report-back is owed there"
    )
    return True


async def _discard_dispatcher_thread(cache, dispatcher_thread_id: str) -> None:
    """Chief of Staff thread deleted (404): disposition every snapshotted watch member.

    Raises on any failure so the drainer nacks and retries — a swallowed
    member clear here would strand that member's origin/pointer/cap state
    forever; that is also why the origin reads are STRICT — a transient
    read degrading to a gen-less clear would refuse a generated origin.
    Each member is cleared against the generation its origin holds RIGHT
    NOW (observed-gen CAS); a member whose origin moved to a different
    thread loses only OUR stale watch reference — its live dispatch
    owns the rest. There is deliberately NO final DEL of the watch set: a
    member SADDed by a concurrent reserve after our snapshot must survive
    (its completion would otherwise ack as a non-member and drop the
    summary). Every snapshotted member is removed individually — the
    clear's teardown Lua SREMs it, the cross-thread branches SREM it, and
    an origin that vanished mid-walk means a rival teardown already did —
    and Redis drops an empty set automatically; any residue is an orphan
    the reapers collect.
    """
    watch_set = watch_key(dispatcher_thread_id)
    members = await cache.client.smembers(watch_set)
    for member in members or []:
        ptc_tid = decode(member)
        origin = await cache.get_strict(ptc_origin_key(ptc_tid))
        if isinstance(origin, dict) and origin.get(ORIGIN_THREAD_FIELD) not in (
            None,
            dispatcher_thread_id,
        ):
            await cache.client.srem(watch_set, ptc_tid)
            continue
        observed_gen = (
            origin.get("dispatch_gen") if isinstance(origin, dict) else None
        )
        # No drained-run record: the thread is gone, so nothing can
        # ever render these turns.
        # DALTON-HOOK(handoff-keep-on-resumable): deleted dispatcher thread
        # discards unconditionally (ignore_awaiting_resume=True).
        cleared = await pointer.clear_handoff_report_back(
            cache,
            ptc_tid,
            dispatcher_thread_id,
            record_drained=False,
            expected_gen=observed_gen,
            ignore_awaiting_resume=True,
        )
        if not cleared:
            # The origin's generation moved between our read and the CAS.
            # Re-read: moved cross-thread -> drop only our reference; gone ->
            # already torn down; still ours -> retry the whole job rather
            # than delete the watch set out from under live state.
            fresh = await cache.get_strict(ptc_origin_key(ptc_tid))
            if isinstance(fresh, dict) and fresh.get(ORIGIN_THREAD_FIELD) not in (
                None,
                dispatcher_thread_id,
            ):
                await cache.client.srem(watch_set, ptc_tid)
                continue
            if fresh is not None:
                raise RuntimeError(
                    f"discard of thread {dispatcher_thread_id}: clear of "
                    f"member {ptc_tid} refused (generation moved); nacking"
                )
