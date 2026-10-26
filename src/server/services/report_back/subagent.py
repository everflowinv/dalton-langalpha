"""PTC←background-subagent report-backs, mirroring Chief of Staff←PTC hand-offs.

A subagent run that reaches a reportable outcome (``REPORT_BACK_STATUSES``)
bears a ``task_report_back`` outbox job in the same transaction as its
terminal CAS (``subagent_runs.finalize_task_run``) — the enqueue commits
with the CAS or not at all. Whether a notification is actually *owed* is
the EXECUTOR's decision at claim time, against the ledger: a run whose
``result_delivered_at`` is stamped (TaskOutput delivered it) is dropped
with a cleared wake; a live or interrupted parent parks the job until the
thread's next dead-turn finalize (completed, error or cancelled) releases
it, and a live turn may close it first by taking the report in as a steer
(``report_back.steer``). A run that had already settled when the user
stopped the turn that launched it drops too (``_stop_already_covered``),
since the whole point of a stop is that no new turn starts. Both paths
decide through ``task_report_verdict``. The POST is a synthetic
notification turn via the shared ``notify_turn`` machinery: it announces
the outcome and leaves TaskOutput to fetch the result from the durable
archive, so nothing volatile rides the payload.

Unlike the hand-off pipeline there is no Redis reserve state: the open outbox
row IS the pending-registry, owed from enqueue until the notification run
it dispatched ends (/status reads it via ``read_task_report_back_status``).

Delivery semantics are AT-LEAST-ONCE, never exactly-once: completed and
error runs enqueue (cancelled/interrupted never notify — cancellation is
intentful and carries its own UX). TaskOutput deliveries stamp
``result_delivered_at`` (stamp failures are swallowed in favor of a
duplicate over a lost notification); this executor never stamps — its
dedup is the claim-time drop against the stamp plus the per-run
idempotent job identity. Any change to this pipeline must preserve that
posture — the ledger stamp is the dedup, process memory never is.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

# DALTON-HOOK(handoff-keep-on-resumable): reuse shared resumable-interruption predicate
from src.server.services.report_back.notify_turn import NotOwed, is_resumable_failure

if TYPE_CHECKING:
    from src.server.models.chat import TaskReport

logger = logging.getLogger(__name__)

# One deterministic request-key namespace per job id (separate from the
# hand-off RB namespace so a job id can never mint the same key for both).
TASK_RB_REQUEST_NS = uuid.UUID("3d9c4e8a-1b6f-4a72-9e05-7f8a2c31d4b6")

# POST defer-loop cap per lease chain. Short on purpose: a busy thread's
# cap exhaustion doesn't drop the notification — the executor re-parks the
# job as deferred and the thread's next dead-turn finalize releases it.
_TASK_RB_BUSY_WAIT_CAP = 120.0
# The notification turn is a full PTC turn (sandbox, tools); hold the
# chain open up to this long before acking anyway.
_TASK_RB_TERMINAL_WAIT_CAP = 45 * 60.0


def task_request_key(job_id: str) -> str:
    """The request key a job's notification turn is admitted under."""
    return str(uuid.uuid5(TASK_RB_REQUEST_NS, job_id))


async def publish_cleared_wake(thread_id: str) -> None:
    """Tell a thread's watchers to re-read its report-back status now.
    Best-effort: a dropped wake degrades to the watch's own backstop."""
    try:
        from src.server.services.report_back.handoff import wake
        from src.utils.cache.redis_cache import get_cache_client

        await wake.publish_wake(get_cache_client(), str(thread_id), cleared=True)
    except Exception:
        logger.debug(
            f"[TASK_REPORT_BACK] Cleared wake for {thread_id} failed", exc_info=True
        )


async def publish_cleared_wake_if_no_open_job(thread_id: str) -> None:
    """Settled-watch reconciliation for the subagent collectors.

    Report-back jobs are born on the run ledger's terminal CAS
    (``subagent_runs.finalize_task_run``), not here — when a collector's
    batch has fully settled and no report-back job is open for the thread,
    watchers are told to reconcile now instead of riding the status
    backstop. An open job means the executor's own outcome (run_id wake or
    cleared) is the signal to wait for. Never raises.
    """
    try:
        from src.server.database.runs import outbox as outbox_db
        from src.server.services.report_back.handoff import wake
        from src.utils.cache.redis_cache import get_cache_client

        open_jobs = await outbox_db.list_open_notification_jobs(
            thread_id, "task_report_back", limit=1
        )
        if not open_jobs:
            await wake.publish_wake(get_cache_client(), thread_id, cleared=True)
    except Exception:
        logger.warning(
            f"[TASK_REPORT_BACK] Settled-wake reconciliation failed for "
            f"thread {thread_id}",
            exc_info=True,
        )


async def read_task_report_back_status(thread_id: str) -> dict:
    """Report-back status slice for a PTC thread (same contract as hand-offs).

    A non-deferred outbox job is owed until the notification run it
    dispatched ends (see ``ended_run_ids``). The first owed job's
    ``dispatched_run_id`` (present once the notification turn is POSTed) is
    the run to attach to. On a read failure ``pending_report_back`` is
    ``None`` (unknown — the frontend keeps watching). Drained notification
    runs are derived from recently DONE outbox rows — the only recovery for
    a wake published while the client held no subscription, and durable by
    construction (the ack that closes the job IS the ledger write) — plus
    the runs of open jobs that already ended, which are persisted and
    replayable before the ack lands.
    """
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.report_back.handoff.status import ended_run_ids

    pending: bool | None = False
    run_id = None
    ended: list[str] = []
    try:
        jobs = await outbox_db.list_open_notification_jobs(
            thread_id, "task_report_back"
        )
        runs = [(job.get("payload") or {}).get("dispatched_run_id") for job in jobs]
        ended = await ended_run_ids(runs)
        owed = [r for r in runs if r not in ended]
        pending = bool(owed)
        run_id = owed[0] if owed else None
    except Exception:
        logger.warning(
            f"Task report-back status read failed for {thread_id}; "
            f"reporting unknown",
            exc_info=True,
        )
        pending = None
    # Producer-undecided signal: while a tail subagent still runs there is no
    # open outbox row yet, so ``pending=False`` alone would read as drained.
    # Listing the live writers lets the client keep its watch armed.
    active_tasks: list[str] = []
    try:
        from src.server.services import subagent_liveness

        active_tasks = await subagent_liveness.get_active_task_ids(thread_id)
    except Exception:
        logger.warning(
            f"Active-task read failed for {thread_id} report-back slice",
            exc_info=True,
        )
    recent_run_ids: list[str] = []
    try:
        recent_run_ids = await outbox_db.get_recent_notification_run_ids(
            thread_id, "task_report_back"
        )
    except Exception:
        logger.warning(
            f"Recents read failed for {thread_id} report-back slice",
            exc_info=True,
        )
        # Recents are the wake-miss recovery channel; without them an idle
        # answer would authorize teardown while a drained run may still be
        # unrendered. Degrade to unknown so the client stays armed.
        if pending is False:
            pending = None
    # Ended-but-unacked runs are newer than every acked one, since the jobs
    # drain in order on the thread's chain.
    recent_run_ids = list(dict.fromkeys([*ended, *recent_run_ids]))
    return {
        "thread_id": thread_id,
        "pending_report_back": pending,
        "report_back_run_id": run_id,
        "recent_report_back_run_ids": recent_run_ids,
        "active_tasks": active_tasks,
    }


def task_report(payload: dict) -> "TaskReport":
    """The task a notification turn announces, read off its payload once: the
    turn's message names it from this, and history labels the turn with it."""
    from src.server.models.chat import TaskReport

    task_id = payload.get("task_id")
    return TaskReport(
        kind="task",
        task_id=str(task_id),
        display_id=payload.get("display_id") or f"Task-{task_id}",
        description=payload.get("description") or None,
        # Pre-final_status jobs (enqueued before the field existed) carry no
        # status; only completed runs were enqueued then.
        failed=payload.get("final_status") == "error",
    )


def task_report_message(
    report: "TaskReport",
    subagent_type: str | None,
    *,
    steered: bool = False,
    failure_error: str | None = None,
) -> str:
    """The report's content, as its own turn or as a steer into a live one.

    Only the event. Reading the result is the Task description's to say and
    never passing a failure off as finished work the system prompt's, so the
    report does not restate either. It need not say the result is unseen
    either: once TaskOutput serves a result, its report is dropped."""
    # Posted, it names no time: a report is posted whenever no live turn took
    # it in, which includes a task that settled after the turn's last model
    # call, so "after your previous turn ended" can be false.
    when = " while you were working" if steered else ""
    outcome = "FAILED" if report.failed else "finished"
    # DALTON-HOOK(workflow-resume): a lost-worker workflow run is resumable —
    # say so, with the task id the resume call needs, or the agent reads the
    # failure as lost work and starts over from scratch.
    # DALTON-HOOK(handoff-keep-on-resumable): shared resumable-interruption check
    resume_hint = ""
    lost_worker = is_resumable_failure(failure_error)
    if report.failed and subagent_type == "workflow" and lost_worker:
        resume_hint = (
            f"\nTask id: {report.task_id}. This workflow run was interrupted "
            "by a lost worker and can be resumed: "
            "RunWorkflow(workflow=<name>, params=<same as before>, "
            f"resume='{report.task_id}') replays completed dispatches from "
            "its journal. When continuing (or when the user asks to continue "
            f"/ '继续'), you MUST call RunWorkflow(workflow=<same name>, "
            f"params=<same as before>, resume='{report.task_id}') — do NOT "
            "manually re-dispatch Task subagents (such as data-prep or "
            "dalton-evidence)."
        )
    # DALTON-HOOK(i18n): Prevent language drift after English subagent completion notices
    return (
        "<system>\n"
        f"Background subagent {report.display_id} ({subagent_type or 'subagent'}) "
        f"{outcome}{when}.\n"
        f"Task description: {report.description or ''}{resume_hint}\n"
        "This notice is from the system, not the user. Keep writing to the user in the language of their request.\n"
        "</system>"
    )


def _stop_already_covered(parent: dict | None, run_row: dict) -> bool:
    """Whether the user stopped the task's parent turn after the task settled.

    A cancel finalize releases every parked report-back on the thread, so
    without this the stop itself opens a synthetic turn — billable model work
    a second after the user asked for none. Narrow on purpose: only a result
    that had already settled when they stopped. A run that terminalized
    *after* the cancel is news the stop cannot have been about, and
    announcing it is the entire job of this pipeline — a blanket "the thread
    was cancelled once" test would silence every later completion instead.

    Read off the task's own parent attempt, never the thread's newest one: a
    task launched by turn A can settle while turn B runs, and cancelling B
    must not read A's earlier settle as covered; and once the user starts
    another turn after the stop, the drop still holds, whichever of the
    drainer or that turn reaches the report first.

    Undecidable without both stamps, and silence is the worse failure, so a
    missing one notifies.
    """
    if not parent or parent.get("status") != "cancelled":
        return False
    stopped_at = parent.get("cancel_requested_at")
    settled_at = run_row.get("finalized_at")
    if stopped_at is None or settled_at is None:
        return False
    return settled_at <= stopped_at


@dataclass(frozen=True)
class OwedTaskReport:
    """A task report still owed: what the reader is shown, the subagent
    type the model is told about, and the run's failure text."""

    report: "TaskReport"
    subagent_type: str | None
    failure_error: str | None = None

    def message(self, *, steered: bool) -> str:
        # DALTON-HOOK(workflow-resume): the resume hint is reserved for the
        # worker_lost wording a recovered workflow run carries — a script
        # error, a cap or a usage error is not resumable work.
        return task_report_message(
            self.report,
            self.subagent_type,
            steered=steered,
            failure_error=self.failure_error,
        )


async def task_report_verdict(job: dict) -> OwedTaskReport | NotOwed:
    """Whether a ``task_report_back`` job is still owed, against the ledger.

    The one eligibility both its executor (at claim time) and a live turn
    taking it in as a steer decide by. TaskOutput deliveries stamp
    ``result_delivered_at``, so a stamped run owes nothing.
    """
    from src.server.database.runs import lifecycle as tl_db
    from src.server.database.runs import subagent_runs as sr_db

    payload = job.get("payload") or {}
    task_run_id = payload.get("task_run_id")
    if task_run_id:
        run_row = await sr_db.get_task_run(str(task_run_id))
        if run_row is None:
            return NotOwed("its run row is gone", wake=False)
        if run_row.get("result_delivered_at"):
            return NotOwed("its result was already delivered")
        parent_run_id = run_row.get("parent_run_id")
        if parent_run_id and _stop_already_covered(
            await tl_db.get_run(str(parent_run_id)), run_row
        ):
            return NotOwed("its turn was stopped after it settled")
    # DALTON-HOOK(workflow-resume): failure_error rides the owed report so
    # the message can tell a resumable worker_lost workflow apart from an
    # ordinary failure.
    return OwedTaskReport(
        task_report(payload),
        payload.get("subagent_type"),
        payload.get("failure_error"),
    )


async def execute_task_report_back(job: dict) -> None:
    """Execute one ``task_report_back`` outbox job.

    Sole caller is the hook-outbox drainer — raising nacks, returning acks.
    The job stays open (lease-heartbeated) until the notification run
    reaches terminal, so per-thread FIFO holds. Crash-safe resume via
    ``dispatched_run_id`` (merged atomically with the result-text scrub) and
    the job-deterministic ``request_key``. A thread found interrupted, or a
    busy-wait cap, re-parks the job as deferred instead of dropping — the
    thread's next dead-turn finalize releases it.
    """
    from src.server.database.runs import outbox as outbox_db
    from src.server.services.report_back.notify_turn import (
        await_run_terminal,
        park_while_thread_busy,
        post_notification_turn,
    )
    from src.server.services.report_back.handoff import wake
    from src.server.services.hook_outbox import LEASE_SECONDS, MAX_ATTEMPTS
    from src.utils.cache.redis_cache import get_cache_client

    payload = job.get("payload") or {}
    thread_id = job.get("conversation_thread_id") or payload.get("thread_id")
    user_id = payload.get("user_id")
    job_id = str(job["hook_outbox_id"])
    attempts = job["attempts"]
    subject = f"task {payload.get('display_id') or payload.get('task_id')}"

    if not thread_id:
        logger.warning(
            f"[TASK_REPORT_BACK] Job {job_id} missing thread; dropping"
        )
        return
    # psycopg returns uuid columns as UUID objects; downstream wants str
    # (wake payloads are json.dumps'd — UUID is not serializable).
    thread_id = str(thread_id)

    # Ledger-enqueued jobs carry task identity only — owner and workspace
    # resolve from the thread, the durable source (legacy collector jobs
    # carried both in the payload).
    workspace_id = payload.get("workspace_id")
    if not user_id or not workspace_id:
        from src.server.database import conversation as conv_db

        if not user_id:
            meta = await conv_db.get_thread_auth_meta(thread_id)
            user_id = str(meta["user_id"]) if meta and meta.get("user_id") else None
        if not workspace_id:
            row = await conv_db.get_thread_by_id(thread_id)
            workspace_id = (
                str(row["workspace_id"]) if row and row.get("workspace_id") else None
            )
    if not user_id:
        logger.warning(
            f"[TASK_REPORT_BACK] Job {job_id} on thread {thread_id}: owner "
            f"unresolvable (thread deleted?); dropping"
        )
        return

    async def _fence() -> bool:
        return await outbox_db.extend_job_lease(
            job_id, LEASE_SECONDS, attempts=attempts
        )

    async def _repark() -> str | None:
        status = await outbox_db.defer_claimed_job(
            job_id, attempts=attempts, max_attempts=MAX_ATTEMPTS
        )
        logger.info(
            f"[TASK_REPORT_BACK] Re-parked job {job_id} for {subject} on "
            f"thread {thread_id} (status={status})"
        )
        return status

    rb_run_id = payload.get("dispatched_run_id")
    if not rb_run_id:
        # Ledger arbitration: the job was enqueued unconditionally at the
        # run's terminal CAS; whether a notification is still owed is
        # decided HERE, at claim time, against the durable rows.
        verdict = await task_report_verdict(job)
        if isinstance(verdict, NotOwed):
            logger.info(
                f"[TASK_REPORT_BACK] Dropping {subject} on thread {thread_id}: "
                f"{verdict.reason}"
            )
            if verdict.wake:
                # Watchers may be riding the pending chip on this job —
                # tell them to reconcile now instead of via the backstop.
                await publish_cleared_wake(thread_id)
            return

        # A live turn takes the report in as a steer before its next model
        # call (``report_back.steer``; jobs are born at the run's CAS,
        # usually mid-turn), or fetches the result itself.
        if await park_while_thread_busy(
            job_id,
            attempts,
            thread_id,
            log_prefix="[TASK_REPORT_BACK]",
            subject=subject,
        ):
            return

        report = verdict.report
        message = verdict.message(steered=False)
        body = {
            "messages": [{"role": "user", "content": message}],
            "workspace_id": workspace_id,
            "query_type": "system",
            "request_key": task_request_key(job_id),
        }
        # The message is hidden, so history labels the turn from this. Every
        # enqueue names its task; a payload that does not still announces,
        # unlabelled, rather than have a label name `Task-None`.
        if payload.get("task_id"):
            body["report_source"] = report.model_dump(exclude_none=True)
        outcome, rb_run_id = await post_notification_turn(
            thread_id=thread_id,
            body=body,
            user_id=user_id,
            wait_cap=_TASK_RB_BUSY_WAIT_CAP,
            heartbeat=_fence,
            log_prefix="[TASK_REPORT_BACK]",
            subject=subject,
        )
        if outcome == "lost":
            return  # reclaimed: the live owner resumes; no teardown
        if outcome == "cap":
            # Busy thread (long user turn, backlog). Not a failure: park
            # until the next dead-turn finalize re-releases the job.
            await _repark()
            return
        if outcome in ("deleted", "drop"):
            return  # thread gone or terminal rejection: ack, nothing owed
        if rb_run_id is None:
            raise RuntimeError(
                f"task report-back for {subject} dispatched without a run_id; "
                f"nacking to recover it via request_key dedup"
            )
        # Durable resume pointer: after this a reclaim resumes the terminal
        # wait (before it, request_key dedup makes the re-POST safe). Merge
        # failure must NACK — recents are derived from dispatched_run_id on
        # DONE rows, so acking without the pointer erases the notification
        # from wake-miss recovery.
        try:
            await outbox_db.merge_job_payload(
                job_id, {"dispatched_run_id": rb_run_id}
            )
        except Exception:
            logger.warning(
                f"[TASK_REPORT_BACK] Failed persisting dispatched_run_id "
                f"{rb_run_id} on job {job_id}; nacking (request_key dedup "
                f"makes the retry's re-POST safe)",
                exc_info=True,
            )
            raise
        # Nudge watching clients toward the notification run. Best-effort;
        # unfenced — a stale owner's wake still points at the real run.
        try:
            await wake.publish_wake(get_cache_client(), thread_id, run_id=rb_run_id)
        except Exception:
            pass
    else:
        logger.info(
            f"[TASK_REPORT_BACK] Resuming in-flight notification run "
            f"{rb_run_id} for {subject} on thread {thread_id} (no re-dispatch)"
        )

    outcome = await await_run_terminal(
        job_id,
        attempts,
        rb_run_id,
        wait_cap=_TASK_RB_TERMINAL_WAIT_CAP,
        log_prefix="[TASK_REPORT_BACK]",
    )
    if outcome == "timeout":
        # No pair state to tear down (unlike hand-offs): ack and release the
        # chain — the run row itself is the durable record of what happened.
        logger.warning(
            f"[TASK_REPORT_BACK] Terminal wait cap hit for {subject} on "
            f"thread {thread_id}; acking anyway"
        )
    # No recovery-ledger write here: the drainer's ack (status='done' +
    # completed_at) IS the ledger — the slice derives its recents from
    # recently DONE rows, so wake-miss recovery cannot be lost to a Redis
    # failure in this window.


# ---------------------------------------------------------------------------
# Outbox executor — registered with the drainer at startup
# (``register_outbox_executors``, called from app setup before the drainer
# starts) so the outbox service never imports this module.
# ---------------------------------------------------------------------------


async def _exec_subagent_report_back(job: dict) -> None:
    await execute_task_report_back(job)


async def _on_subagent_report_back_closed(job: dict) -> None:
    # Task report-back pendingness IS the open outbox row, and the frontend
    # watch is push-driven — a /status read that raced ahead of the ack, or
    # of a job parked dead, leaves the client armed with no later signal
    # until the ~30-min watch recycle. `cleared` only means "reconcile now":
    # the client re-reads /status, so a wake with another job still queued
    # is harmless. A dropped wake degrades to the recycle snapshot.
    thread_id = job.get("conversation_thread_id") or (
        job.get("payload") or {}
    ).get("thread_id")
    if thread_id:
        await publish_cleared_wake(thread_id)


def register_outbox_executors() -> None:
    from src.server.services.hook_outbox import register_hook_executor

    register_hook_executor(
        "task_report_back",
        _exec_subagent_report_back,
        on_closed=_on_subagent_report_back_closed,
    )
