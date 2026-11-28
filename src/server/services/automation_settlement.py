"""
Settlement: the one place an automation firing ends.

A firing ends once. ``settle`` ends the row in one transaction with what the
firing leaves on its automation (the strike or its reset, and the schedule),
and only the writer whose transaction lands goes on to the delivery (the
webhook, or the messaging service's finish), the wait notice and the metric.
The executor, a user's skip, the run's finalize hook and the scheduler's
sweep can race to end the same firing; the loser finds it settled and leaves
it as it is.

Once a firing's turn reached the run ledger, the run's finalize settles it
through the hook outbox, on whichever worker drains the job, as the run's
ledger row says it ended: ``settle_by_run``. The sweep reads the same row for
a job that never ran. Such a firing's finish at the messaging service is an
outbox job of its own, written in the settle's own transaction, so the
settle and the job commit together (``_leave_finish``). A firing whose
process died before its turn reached the ledger has no run row to say how it
delivers; the stamp its start kept in the execution's ``delivery`` column
says it instead, so a run the service holds still ends there.
"""

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable, Mapping  # DALTON-HOOK(automation-continuation-identity)
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

from src.observability import automation_executions, safe_add
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.pool import get_db_connection
from src.server.database.runs import outbox as outbox_db
from src.server.models.automation import ExecutionStatus, FailureReason, SkipReason
from src.server.services import automation_delivery
from src.server.services.account import read_standing
from src.server.services.automation_excerpt import RunAnswer, plain_excerpt, read_run_answer
from src.server.services.automation_outcomes import (
    LIMITS,
    POLICIES,
    RUN_FAILURES,
    SUSPENSION_ENDS,
    TIMEOUT_ERROR,
    Outcome,
    OutcomePolicy,
    clean_error_text,
    ledger_outcome,
    run_failure_message,
    schedule_action,
)
from src.server.services.thread_lifecycle_feed import publish_automation_wait
from src.server.services.webhook_client import WebhookClient

logger = logging.getLogger(__name__)

# DALTON-HOOK(precheck-outcome): re-export settlement outcome tables/actions moved to automation_outcomes
_POLICIES = POLICIES
_schedule_action = schedule_action

# --- DALTON-HOOK(automation-continuation-identity): single-source timeout check and guarded takeover ---
execution_timeout_seconds = exec_db.execution_timeout_seconds


def is_execution_timed_out(
    exec_row: Mapping[str, Any],
    automation: Optional[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> bool:
    """True once elapsed time since execution start reaches its timeout cap."""
    anchor = exec_row.get("started_at") or exec_row.get("created_at")
    if not isinstance(anchor, datetime):
        return False
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    ref_now = now or datetime.now(timezone.utc)
    if ref_now.tzinfo is None:
        ref_now = ref_now.replace(tzinfo=timezone.utc)
    return (ref_now - anchor).total_seconds() >= execution_timeout_seconds(automation)


async def take_over_firing(
    execution_id: str, run_id: str, *, conn: Any = None
) -> None:
    """Point a running firing at the continuation/resumed turn ``run_id`` atomically."""
    taken = await exec_db.take_over_execution_run(execution_id, run_id, conn=conn)
    if not taken:
        logger.warning(
            f"[AUTOMATION_SETTLE] Failed to take over firing: "
            f"execution_id={execution_id} run_id={run_id}"
        )
# --- DALTON-HOOK(automation-continuation-identity): end ---


async def settle_unresumed(run_id: str) -> None:
    """Settle as interrupted the firing a restart stopped that still points
    at the stopped run, once a user's newer turn took the resume's place. A
    firing a newer turn took over (``carry.carried_firing``) is that turn's
    to settle, even before it relinked the firing: its START stamp says so."""
    from src.server.database.runs import lifecycle as tl_db

    row = await exec_db.get_running_execution_for_run(run_id)
    if row is None:
        return
    latest = await tl_db.get_latest_attempt(str(row["conversation_thread_id"]))
    if latest is not None and str(latest["conversation_response_id"]) != run_id and (
        (latest.get("metadata") or {}).get("automation_execution_id")
        == str(row["automation_execution_id"])
    ):
        return
    automation = await auto_db.get_automation(str(row["automation_id"]), row["user_id"])
    if automation is None:
        return
    execution_id = str(row["automation_execution_id"])
    thread_id = str(row["conversation_thread_id"])
    workspace_id = row.get("workspace_id") or automation.get("workspace_id")
    settling_run = await exec_db.get_settling_run(run_id)
    # DALTON-HOOK(automation-subscription-fallback): carry fallback notices into unresumed interrupted settlement excerpt
    from src.dalton.services.automation_subscription_fallback import apply_execution_fallback_notice

    excerpt, _ = await apply_execution_fallback_notice(
        execution_id, thread_id=thread_id, run=settling_run, exec_row=row,
    )
    await settle(
        automation, execution_id, Outcome.INTERRUPTED,
        thread_id=thread_id, run_id=run_id, workspace_id=workspace_id,
        excerpt=excerpt,
        delivery=automation_delivery.delivery_of_run(settling_run, execution_id),
    )




async def plan_message(
    detail: Dict[str, Any], user_id: str, reword: Optional[Callable[[Dict[str, Any], Optional[str]], str]] = None,
) -> Optional[str]:
    """A plan's used-up limit in words, with its reset on the automation
    owner's clock, since its report and delivery are theirs to read; None
    where the stamp can't be worded. ``reword`` words ``detail``: the Claude
    plan's refusal (``reword_plan``) unless given another subscription's."""
    from src.server.database.automation import get_user_timezone
    from src.server.services.llm.claude_code import reword_plan

    try:
        zone = await get_user_timezone(user_id) if user_id else None
    except Exception:
        zone = None
    try:
        return clean_error_text((reword or reword_plan)(detail, zone))
    except Exception:
        logger.warning("[AUTOMATION_SETTLE] Unreadable plan stamp", exc_info=True)
        return None


async def settle(
    automation: Dict[str, Any],
    execution_id: str,
    outcome: Outcome,
    *,
    thread_id: Optional[str] = None,
    run_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    error: Optional[str] = None,
    skip_reason: Optional[SkipReason] = None,
    excerpt: Optional[str] = None,
    quiet_for: Optional[int] = None,
    parked: bool = False,
    delivery: Optional[automation_delivery.Delivery] = None,
    final_text: Optional[str] = None,
    expected_run_id: Any = exec_db.UNSET_RUN_ID,
    check_run_state_in_lock: Optional[Literal["terminal", "completed_idle", "timeout"]] = None,
    # DALTON-HOOK(automation-subscription-fallback): restrict pre-START signal release to executor's synchronous failure settle
    release_pre_start_signals: bool = False,
    # DALTON-HOOK(coverage-units): allow caller to narrow/specify source statuses without relaxing policy defaults
    from_statuses: Optional[tuple[ExecutionStatus, ...]] = None,
    # DALTON-HOOK(dalton-notify): honor persisted one-shot schedule action during rollback recovery
    schedule_override: Optional[exec_db.ScheduleAction] = None,
) -> bool:
    """End a firing as ``outcome``; False when it was no longer in a state
    that outcome can end, because someone else settled it first.

    The execution must belong to ``automation``, which keeps the skip
    endpoint to its own automation's firings. ``quiet_for`` is the sweep's:
    settle only while the heartbeat is still that many seconds quiet, and
    ``parked`` the parked sweep's: only while still parked for its computer.
    ``delivery`` is how the firing's start left its delivery: one the
    messaging service holds ends there instead of firing the webhook, and
    ``final_text`` is the result a completed one hands over; one the service
    didn't take fires the webhook, and records why each entry went
    undelivered when the webhook has nowhere to post. Without one, the stamp
    the start kept in the execution's ``delivery`` column stands in: the
    sweep's only word on a firing whose process died after the start, before
    its turn's run row carried the stamp. What follows the settled row never
    raises.
    """
    policy = POLICIES[outcome]
    error = error or policy.error
    finish_left = False

    async def leave_finish(conn: Any, settled: Dict[str, Any]) -> None:
        nonlocal finish_left
        settled_tid, settled_rid, settled_delivery = _settled_as(
            settled, execution_id, thread_id, run_id, delivery
        )
        finish_left = await _leave_finish(
            conn, policy, automation, execution_id,
            settled_tid, settled_rid, settled_delivery,
            settled, final_text,
        )
        # DALTON-HOOK(dalton-notify): enqueue durable failure notice or bump recovery epoch inside settle tx
        from src.dalton.notify.failure_receipts import on_automation_settle_in_tx

        await on_automation_settle_in_tx(
            conn, automation=automation, execution_id=execution_id, outcome_status=policy.status,
            failure_reason=policy.failure_reason, error=error, delivery=settled_delivery,
            thread_id=settled_tid, workspace_id=workspace_id,
        )
        # DALTON-HOOK(automation-subscription-fallback): release signals only when executor's synchronous pre-START path settles 'failed'
        if release_pre_start_signals and policy.status == "failed":
            from src.dalton.services.automation_subscription_fallback import (
                release_claimed_signals_on_executor_failed_settle,
            )

            await release_claimed_signals_on_executor_failed_settle(
                conn,
                automation=automation,
                execution_id=execution_id,
                thread_id=thread_id
                or (settled.get("conversation_thread_id") if isinstance(settled, dict) else None),
                run_id=run_id,
                settled_row=settled,
            )

    extra_settle_kwargs: Dict[str, Any] = {}
    if expected_run_id is not exec_db.UNSET_RUN_ID:
        extra_settle_kwargs["expected_run_id"] = expected_run_id
    if check_run_state_in_lock is not None:
        extra_settle_kwargs["check_run_state_in_lock"] = check_run_state_in_lock

    row = await exec_db.settle_execution(
        execution_id,
        automation_id=str(automation["automation_id"]),
        from_statuses=from_statuses or policy.from_statuses,
        to=policy.status,
        strike=policy.strike,
        schedule=(
            schedule_override
            if schedule_override is not None
            else schedule_action(policy, automation, outcome=outcome, excerpt=excerpt)
        ),  # DALTON-HOOK(precheck-outcome) DALTON-HOOK(dalton-notify)
        quiet_for=quiet_for,
        parked=parked,
        in_transaction=leave_finish,
        conversation_thread_id=thread_id,
        conversation_response_id=run_id,
        result_excerpt=excerpt,
        error_message=error,
        skip_reason=skip_reason or policy.skip_reason,
        failure_reason=policy.failure_reason,
        completed_at=datetime.now(timezone.utc),
        **extra_settle_kwargs,
    )
    if row is None:
        logger.info(
            f"[AUTOMATION_SETTLE] Not settling as {outcome}: "
            f"execution_id={execution_id} was already settled or moved on"
        )
        return False
    logger.info(
        f"[AUTOMATION_SETTLE] Settled as {outcome}: execution_id={execution_id}"
        + (f" reason={skip_reason}" if skip_reason else "")
    )
    thread_id, run_id, delivery = _settled_as(row, execution_id, thread_id, run_id, delivery)

    # Shielded: a cancel landing after the commit, such as shutdown stopping
    # the sweep, must not drop the notice the commit made this writer's to
    # send, since no later sweep finds the row to send it again.
    tail = _hold(asyncio.create_task(
        _after_settling(outcome, automation, execution_id, row,
                        thread_id, run_id, workspace_id, error,
                        delivery, final_text, finish_left),
        name=f"settle_tail_{execution_id}",
    ))
    await asyncio.shield(tail)
    return True


def _settled_as(
    row: Dict[str, Any],
    execution_id: str,
    thread_id: Optional[str],
    run_id: Optional[str],
    delivery: Optional[automation_delivery.Delivery],
) -> tuple[Optional[str], Optional[str], Optional[automation_delivery.Delivery]]:
    """The thread, run and delivery a settled firing goes on with: the
    caller's, else what its row recorded."""
    recorded_thread = row["conversation_thread_id"]
    recorded_run = row["conversation_response_id"]
    thread_id = thread_id or (str(recorded_thread) if recorded_thread else None)
    run_id = run_id or (str(recorded_run) if recorded_run else None)
    if delivery is None:
        delivery = automation_delivery.read_stamp(row.get("delivery"), execution_id)
    return thread_id, run_id, delivery


# Held so a tail a cancel left running is not collected mid-webhook, and so
# shutdown can wait for it (``drain_tails``). A finish this process asks
# again itself, having no outbox job to leave it to, is held the same way.
_tails: set[asyncio.Task] = set()


def _hold(task: asyncio.Task) -> asyncio.Task:
    _tails.add(task)
    task.add_done_callback(_tails.discard)
    return task

# One webhook's timeout (``WebhookClient.fire``) plus the writes after it.
TAIL_DRAIN_SECONDS = 20.0


async def drain_tails(timeout: float = TAIL_DRAIN_SECONDS) -> None:
    """Wait, up to ``timeout``, for this process's tails still running.

    A tail cut off by shutdown is lost: its row is already settled, so no
    sweep finds it to send the webhook or the notice again. A held run that
    reached the run ledger is past that, since its settle wrote the finish
    job, which outlives the process (``_leave_finish``). Only a firing that
    never reached the ledger asks here, and its retries cut off leave the
    record its first ask made.
    """
    if not _tails:
        return
    _, pending = await asyncio.wait(set(_tails), timeout=timeout)
    if pending:
        logger.warning(
            f"[AUTOMATION_SETTLE] {len(pending)} settlement tail(s) still "
            f"running after {timeout:g}s at shutdown: "
            f"{sorted(t.get_name() for t in pending)}"
        )


async def _after_settling(
    outcome: Outcome,
    automation: Dict[str, Any],
    execution_id: str,
    row: Dict[str, Any],
    thread_id: Optional[str],
    run_id: Optional[str],
    workspace_id: Optional[str],
    error: Optional[str],
    delivery: Optional[automation_delivery.Delivery],
    final_text: Optional[str],
    finish_left: bool,
) -> None:
    """The delivery, the chat's wait line and the metric a settle sets off.

    A held run whose settle left its finish to the hook outbox (``finish_left``)
    is asked by whichever worker claims the job, this one's drainer woken for
    it. Any other held run is finished from here.
    """
    policy = POLICIES[outcome]
    # DALTON-HOOK(company-automations): a soft-deleted automation does not notify
    from src.dalton.database.automation_recycle import automation_is_soft_deleted

    parent_id = str(automation.get("automation_id") or "")
    deleted = bool(parent_id) and await automation_is_soft_deleted(parent_id)
    if (
        not deleted
        and policy.webhook
        and not _repeats_a_refusal(policy.failure_reason, run_id, row)
    ):
        if delivery is not None and delivery.held and policy.finish is not None:
            if finish_left:
                _nudge_drainer()
            else:
                await _finish_here(
                    automation,
                    execution_id,
                    thread_id,
                    delivery,
                    policy.finish,
                    final_text,
                    run_id=run_id,  # DALTON-HOOK(channel-chart-snapshot)
                )
        else:
            # DALTON-HOOK(personal-integrations): never forward personal integration thread results to external webhooks
            from src.dalton.services.personal_integration_guard import (
                is_thread_personal_private,
            )

            is_personal_private = bool(
                thread_id and await is_thread_personal_private(str(thread_id))
            )
            if is_personal_private:
                delivery_result = (
                    automation_delivery.unsent(delivery)
                    if delivery is not None and not delivery.held
                    else None
                )
            else:
                delivery_result = await WebhookClient().fire_event(
                    policy.webhook, automation, execution_id, thread_id, workspace_id,
                    error=error, run_id=run_id, failure_reason=policy.failure_reason,
                )
                if delivery_result is None and delivery is not None and not delivery.held:
                    # The messaging service didn't take the run and the webhook
                    # has nowhere to post: each entry went undelivered, and why.
                    delivery_result = automation_delivery.unsent(delivery) or None
            if delivery_result is not None:
                await _record_delivery(execution_id, delivery_result)
    # DALTON-HOOK(dalton-notify): push completed Dalton automation results to the analyst via the channel gateway
    if not deleted and outcome is Outcome.COMPLETED:
        from src.dalton.notify.dispatch import on_automation_settled

        # DALTON-HOOK(coverage-units): forward execution-entry unit stamp and version to notify dispatch
        await on_automation_settled(
            automation=automation,
            execution_id=execution_id,
            thread_id=thread_id,
            run_id=run_id,
            workspace_id=workspace_id,
            final_text=final_text or (row.get("result_excerpt") if isinstance(row, dict) else None),
            coverage_unit_id=row.get("coverage_unit_id") if isinstance(row, dict) else None,
            coverage_unit_version=row.get("coverage_unit_version") if isinstance(row, dict) else None,
        )
    # DALTON-HOOK(dalton-notify): wake failure-notice dispatcher after failed settle commits
    if not deleted and policy.status == "failed":
        from src.dalton.notify.failure_receipts import wake_failure_notice_dispatcher

        wake_failure_notice_dispatcher()
    if row["settled_from"] == "waiting" and thread_id:
        await publish_automation_wait(
            user_id=automation["user_id"],
            thread_id=thread_id,
            automation_execution_id=execution_id,
            waiting=False,
        )
    safe_add(
        automation_executions,
        1,
        {"status": policy.metric, "trigger": automation.get("trigger_type") or "unknown"},
    )


async def _record_delivery(execution_id: str, delivery_result: list) -> None:
    try:
        await exec_db.record_delivery(execution_id, delivery_result)
    except Exception as e:
        logger.error(
            f"[AUTOMATION_SETTLE] Recording delivery failed: "
            f"execution_id={execution_id} error={e}"
        )


#: The hook outbox job that ends a held run on the run ledger at the
#: messaging service.
FINISH_HOOK = "automation_finish"


def _replaces_record(finish: automation_delivery.Finish, answered: bool) -> bool:
    """Whether a later ask's record replaces the run's: any answer does, and
    a refusal replaces one no answer made, which says less than any answer."""
    return finish.answered or not (answered or finish.retry)


async def _leave_finish(
    conn: Any,
    policy: OutcomePolicy,
    automation: Dict[str, Any],
    execution_id: str,
    thread_id: Optional[str],
    run_id: Optional[str],
    delivery: Optional[automation_delivery.Delivery],
    row: Dict[str, Any],
    final_text: Optional[str],
) -> bool:
    """Write the ``automation_finish`` job (``_finish_held_run``) a held run
    that reached the run ledger is owed, in its settle's transaction; False
    when none is owed or it can't be written, and the finish is asked in
    process instead.

    Committed with the settle, so a worker that dies once the firing reads
    settled has already left every ask, the first included, to the job: no
    retry of the settle, nor any sweep, finds a settled firing to finish. The
    job hangs on the run's ledger row, so a firing that never reached the
    ledger has none to leave it to, and a savepoint keeps a job that can't be
    written from undoing the settle. The payload is everything a worker that
    never saw the settle needs to ask the same finish.
    """
    if not (
        policy.webhook
        and policy.finish is not None
        and delivery is not None
        and delivery.held
        and run_id
        and thread_id
        and not _repeats_a_refusal(policy.failure_reason, run_id, row)
    ):
        return False
    try:
        async with conn.transaction():
            await outbox_db.enqueue_compensation_job(
                run_id=run_id,
                thread_id=thread_id,
                hook_type=FINISH_HOOK,
                payload={
                    "execution_id": execution_id,
                    "automation_id": str(automation["automation_id"]),
                    "user_id": str(automation["user_id"]),
                    "run_id": run_id,  # DALTON-HOOK(channel-chart-snapshot)
                    "status": policy.finish,
                    "final_text": final_text,
                    "delivery": automation_delivery.stamp(delivery),
                    "answered": False,
                },
                ordering_key=None,
                idempotency_key=f"{run_id}:{FINISH_HOOK}",
                conn=conn,
            )
    except Exception as e:
        logger.warning(
            f"[AUTOMATION_SETTLE] Leaving the finish to the outbox failed, "
            f"asking here: execution_id={execution_id} error={e}"
        )
        return False
    return True


def _nudge_drainer() -> None:
    """Wake this process's drainer for the job just written. Only a hint:
    the drainer's poll, on this worker or any other, finds the job anyway."""
    try:
        from src.server.services.hook_outbox import HookOutboxDrainer

        HookOutboxDrainer.get_instance().nudge()
    except Exception as e:
        logger.warning(f"[AUTOMATION_SETTLE] Nudging the hook outbox drainer failed: {e}")


# DALTON-HOOK(execution-delivery-status): persist sender retry eligibility on unconfirmed delivery_result items
def _stamp_delivery_retry(
    result: list[Dict[str, Any]], *, retrying: bool
) -> list[Dict[str, Any]]:
    """Mark unconfirmed failed attempts with whether the sender will retry them."""
    stamped: list[Dict[str, Any]] = []
    for item in result:
        if item.get("success") is True:
            stamped.append(item)
            continue
        if not str(item.get("error") or "").startswith(automation_delivery._UNCONFIRMED):
            stamped.append(item)
            continue
        stamped.append({**item, "retrying": bool(retrying)})
    return stamped


# DALTON-HOOK(execution-delivery-status): clear retrying on kept delivery_result when sender stops retrying
async def _clear_delivery_retrying(execution_id: str) -> None:
    try:
        await exec_db.clear_delivery_retrying(execution_id)
    except Exception as e:
        logger.error(
            f"[AUTOMATION_SETTLE] Clearing delivery retrying flag failed: "
            f"execution_id={execution_id} error={e}"
        )


async def _finish_here(
    automation: Dict[str, Any],
    execution_id: str,
    thread_id: Optional[str],
    delivery: automation_delivery.Delivery,
    status: automation_delivery.FinishStatus,
    final_text: Optional[str],
    *,
    # DALTON-HOOK(channel-chart-snapshot): forward settlement run_id to automation_delivery.finish_run
    run_id: Optional[str] = None,
) -> None:
    """Ask the messaging service to end a held run from this process, for a
    run with no outbox job to leave it to. The first ask's record stands
    until a retry says more."""
    # DALTON-HOOK(dalton-notify): pass final_attempt when failure-notice retries are exhausted so gateway seals the run
    from src.dalton.settings import is_failure_notice_enabled

    ask = functools.partial(
        automation_delivery.finish_run, automation, delivery.id, status,
        targets=delivery.targets, final_text=final_text, thread_id=thread_id,
        run_id=run_id,  # DALTON-HOOK(channel-chart-snapshot)
    )
    first_kw: Dict[str, Any] = {}
    if not automation_delivery.FINISH_RETRY_DELAYS and is_failure_notice_enabled():
        first_kw["final_attempt"] = True
    finish = await ask(**first_kw)
    # DALTON-HOOK(execution-delivery-status): persist whether finish will be retried
    will_retry = finish.retry and bool(automation_delivery.FINISH_RETRY_DELAYS)
    await _record_delivery(
        execution_id, _stamp_delivery_retry(finish.result, retrying=will_retry)
    )
    if finish.retry:
        # Off the settle's path, which the executor waits on.
        ask_final = (
            functools.partial(ask, final_attempt=True)
            if is_failure_notice_enabled()
            else ask
        )
        _hold(asyncio.create_task(
            _finish_again(ask, execution_id, answered=finish.answered, ask_final=ask_final),
            name=f"settle_finish_{execution_id}",
        ))


class _FinishPending(Exception):
    """A finish the messaging service still wants asked again."""


async def _finish_held_run(job: Dict[str, Any]) -> None:
    """The ``automation_finish`` outbox job: ask the messaging service to end
    a held run that reached the run ledger, from the first ask on
    (``_leave_finish``).

    Each run of the job asks once. The first ask whose record is written
    keeps it, whatever it says, and a later one replaces it only as
    ``_finish_again`` would. What the next attempt needs (``recorded``,
    ``answered``) rides in the payload, since it may run on another worker,
    and is written with the record (``_keep_record``), so a job run again
    after its effect, its acknowledgement lost, never takes an answer the
    service gave for one it didn't. One still worth asking again raises, so
    the drainer runs it again after its backoff on whichever worker claims
    it, until the job's last attempt, whose record stands. A record that
    can't be written raises too, and the next attempt asks the same finish
    again.
    """
    from src.server.services.hook_outbox import MAX_ATTEMPTS

    payload = job.get("payload") or {}
    execution_id = payload["execution_id"]
    automation = await auto_db.get_automation(
        payload["automation_id"], payload.get("user_id")
    )
    if automation is None:
        # Deleted, and its firings with it.
        return
    delivery = automation_delivery.read_stamp(payload["delivery"], execution_id)
    recorded = payload.get("recorded") is True
    answered = payload.get("answered") is True
    # DALTON-HOOK(channel-chart-snapshot): pass the outbox job's own run_id to finish_run
    job_run_id = payload.get("run_id") or job.get("run_id")
    # DALTON-HOOK(dalton-notify): tell gateway when this is the final outbox attempt so finishing runs always seal
    from src.dalton.settings import is_failure_notice_enabled

    finish_kw: Dict[str, Any] = {}
    if int(job["attempts"]) >= MAX_ATTEMPTS and is_failure_notice_enabled():
        finish_kw["final_attempt"] = True
    finish = await automation_delivery.finish_run(
        automation, delivery.id, payload["status"],
        targets=delivery.targets, final_text=payload.get("final_text"),
        thread_id=str(job["conversation_thread_id"]),
        run_id=str(job_run_id) if job_run_id else None,
        **finish_kw,
    )
    # DALTON-HOOK(execution-delivery-status): persist sender retry decision on delivery_result
    will_retry = finish.retry and job["attempts"] < MAX_ATTEMPTS
    job_id = str(job["hook_outbox_id"])
    attempts = int(job["attempts"])
    # DALTON-HOOK(dalton-notify): verify outbox claim ownership before writing delivery result
    if not recorded or _replaces_record(finish, answered):
        saved = await _keep_record(
            job_id,
            execution_id,
            _stamp_delivery_retry(finish.result, retrying=will_retry),
            answered=answered or finish.answered,
            attempts=attempts,
        )
        if not saved:
            return
    elif not will_retry:
        cleared = await _clear_delivery_retrying_for_job(
            job_id,
            execution_id,
            attempts=attempts,
        )
        if not cleared:
            return
    if not finish.retry:
        return
    if job["attempts"] >= MAX_ATTEMPTS:
        logger.warning(
            f"[AUTOMATION_SETTLE] Finish asks ran out, delivery left unconfirmed: "
            f"execution_id={execution_id}"
        )
        return
    raise _FinishPending(
        f"The messaging service wants the finish asked again: execution_id={execution_id}"
    )


async def _keep_record(
    job_id: str,
    execution_id: str,
    result: list,
    *,
    answered: bool,
    attempts: int | None = None,
) -> bool:
    """Write a finish job's record and, in the same transaction, what its
    next attempt needs to know of it: that one is written, and whether the
    service has answered.

    Returns False without writing if the outbox claim has already been taken
    over or closed by another attempt."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            # DALTON-HOOK(dalton-notify): fence write on active outbox claim (status='claimed', attempts)
            if attempts is not None and not await outbox_db.owns_claimed_job(
                job_id, attempts=attempts, conn=conn
            ):
                logger.warning(
                    f"[AUTOMATION_SETTLE] Discarding stale automation_finish result after claim takeover: "
                    f"job_id={job_id} execution_id={execution_id} attempts={attempts}"
                )
                return False
            await exec_db.record_delivery(execution_id, result, conn=conn)
            await outbox_db.merge_job_payload(
                job_id, {"recorded": True, "answered": answered}, conn=conn
            )
    return True


# DALTON-HOOK(dalton-notify): fence retrying-flag clear on active outbox claim ownership
async def _clear_delivery_retrying_for_job(
    job_id: str, execution_id: str, *, attempts: int
) -> bool:
    try:
        async with get_db_connection() as conn:
            async with conn.transaction():
                if not await outbox_db.owns_claimed_job(
                    job_id, attempts=attempts, conn=conn
                ):
                    logger.warning(
                        f"[AUTOMATION_SETTLE] Discarding stale retrying-flag clear after claim takeover: "
                        f"job_id={job_id} execution_id={execution_id} attempts={attempts}"
                    )
                    return False
                await exec_db.clear_delivery_retrying(execution_id, conn=conn)
        return True
    except Exception as e:
        logger.error(
            f"[AUTOMATION_SETTLE] Clearing delivery retrying flag failed: "
            f"execution_id={execution_id} error={e}"
        )
        return True


async def _finish_again(
    ask: Callable[[], Awaitable[automation_delivery.Finish]],
    execution_id: str,
    *,
    answered: bool,
    # DALTON-HOOK(dalton-notify): optional final-attempt callable to seal finishing run on last try
    ask_final: Optional[Callable[[], Awaitable[automation_delivery.Finish]]] = None,
) -> None:
    """Ask the messaging service again to end a run whose finish got no
    answer, found it briefly unavailable, or got rows it was still posting
    (``answered``). The service ends a run once and answers the same record
    to every ask, so asking again posts nothing twice.

    Each record the service answers replaces the run's, until a final one
    ends the asking. A refusal replaces only a record no answer made, which
    says less than any answer. When the asks run out, the last record
    stands, rows still being posted reading as unconfirmed."""
    delays = automation_delivery.FINISH_RETRY_DELAYS
    for idx, delay in enumerate(delays):
        await asyncio.sleep(delay)
        is_last = idx == len(delays) - 1
        finish = await (ask_final if (is_last and ask_final is not None) else ask)()
        # DALTON-HOOK(execution-delivery-status): persist sender retry decision on replacement records
        will_retry = finish.retry and idx < len(delays) - 1
        if _replaces_record(finish, answered):
            await _record_delivery(
                execution_id, _stamp_delivery_retry(finish.result, retrying=will_retry)
            )
        elif not will_retry:
            await _clear_delivery_retrying(execution_id)
        answered = answered or finish.answered
        if not finish.retry:
            return
    logger.warning(
        f"[AUTOMATION_SETTLE] Finish asks ran out, delivery left unconfirmed: "
        f"execution_id={execution_id}"
    )


# Reasons that refuse firing after firing until something outside the
# automation changes: a usage limit until it resets, our outage until it ends.
_STREAK_REASONS = frozenset(POLICIES[outcome].failure_reason for outcome in (*LIMITS, Outcome.FAILED_OURS))


def _repeats_a_refusal(
    reason: Optional[FailureReason], run_id: Optional[str], row: Dict[str, Any]
) -> bool:
    """This firing was refused for the same streak reason as the one before,
    which the channel already heard about, so a frequent schedule cannot
    flood it.

    Only a firing refused before its turn: one that was admitted sent a
    started notice, and only a terminal event clears it. A notice no channel
    took was not heard, so the next refusal sends it again.
    """
    delivered = row.get("previous_delivery_result")
    return (
        reason in _STREAK_REASONS
        and run_id is None
        and row.get("previous_failure_reason") == reason
        and not (delivered and not any(d.get("success") for d in delivered))
    )


async def _as_suspended(outcome: Outcome, owner: str) -> Outcome:
    """A suspended account's firing cut short tells no channel, takes no
    strike, and is put off rather than used up."""
    if (
        outcome in SUSPENSION_ENDS
        and owner
        and (await read_standing(owner, unknown_on_cache_error=True)).suspended
    ):
        return Outcome.SUSPENDED
    return outcome


async def settle_by_run(
    automation: Dict[str, Any],
    execution_id: str,
    run_id: Optional[str],
    run: Optional[Dict[str, Any]],
    *,
    thread_id: Optional[str],
    workspace_id: Optional[str],
    quiet_for: Optional[int] = None,
    retry_read: bool = False,
    # DALTON-HOOK(automation-continuation-identity): propagate lock/CAS settlement guards
    expected_run_id: Any = exec_db.UNSET_RUN_ID,
    check_run_state_in_lock: Optional[Literal["terminal", "completed_idle", "timeout"]] = None,
    # DALTON-HOOK(automation-subscription-fallback): carry loaded execution row to avoid redundant notice lookups
    exec_row: Optional[Mapping[str, Any]] = None,
) -> Optional[Outcome]:
    """Settle a firing as ``run``, its turn's ledger row, ended; the outcome,
    or None while the run is still going or once someone else settled it.

    The row decides, not the stream the executor drained (v4 2.4), and a run
    that failed says why in its own words. No row at all is a run the server
    lost. The row also says whether the messaging service holds the run's
    delivery, which a completed run hands its result to: the text of the
    send it ended on, else its final answer.

    Elsewhere the answer only heads the list, and a failed read settles
    without it. A held run's answer is what the chats its agent didn't reach
    get, and the service ends a run once, so with ``retry_read`` (the
    finalize job, while it has attempts left) a failed read raises before
    anything is settled, for the job to run again.
    """
    # DALTON-HOOK(dalton-notify): recover persisted pre-run failure settlement params after rollback
    from src.dalton.notify.failure_receipts import read_pending_settlement

    pending_settlement = read_pending_settlement(exec_row) if run is None else None
    outcome = ledger_outcome(run, pending_settlement=pending_settlement)
    if outcome is None:
        logger.debug(
            f"[AUTOMATION_SETTLE] Run still going, not settling: "
            f"execution_id={execution_id} run_id={run_id}"
        )
        return None
    owner = str(automation.get("user_id") or "")
    outcome = await _as_suspended(outcome, owner)
    delivery = automation_delivery.delivery_of_run(run, execution_id)
    error = excerpt = final_text = None
    extra_from_pending: Dict[str, Any] = {}
    if run is None and pending_settlement is not None:
        if delivery is None and isinstance(exec_row, Mapping):
            delivery = automation_delivery.read_stamp(exec_row.get("delivery"), execution_id)
        if pending_settlement.get("error") is not None:
            error = str(pending_settlement["error"])
        if pending_settlement.get("excerpt") is not None:
            excerpt = str(pending_settlement["excerpt"])
        if not workspace_id and pending_settlement.get("workspace_id"):
            workspace_id = str(pending_settlement["workspace_id"])
        if not thread_id and pending_settlement.get("thread_id"):
            thread_id = str(pending_settlement["thread_id"])
        if pending_settlement.get("release_pre_start_signals") is True:
            extra_from_pending["release_pre_start_signals"] = True
        saved_sched = pending_settlement.get("schedule_action")
        if (
            isinstance(saved_sched, str)
            and saved_sched in exec_db._SCHEDULE_SQL
            and outcome is not Outcome.SUSPENDED
        ):
            extra_from_pending["schedule_override"] = saved_sched
    elif outcome in RUN_FAILURES and run is not None:
        error = run_failure_message(run)
        if outcome is Outcome.PLAN_LIMITED:
            error = await plan_message(run["metadata"]["claude_plan"], owner) or error
        elif outcome is Outcome.SUBSCRIPTION_LIMITED and (
            limit := (run["metadata"].get("upstream") or {}).get("plan_limit")
        ):
            from src.server.services.llm.claude_code import reword_subscription_limit

            error = await plan_message(limit, owner, reword_subscription_limit) or error
        log = logger.warning if outcome in LIMITS else logger.error
        log(
            f"[AUTOMATION_SETTLE] Run ended as {outcome}: "
            f"execution_id={execution_id} run_id={run_id} error={error}"
        )
    elif outcome is Outcome.COMPLETED:
        answer = RunAnswer()
        answer_read_failed = False  # DALTON-HOOK(deliverable-text): do not fabricate empty-turn summary when checkpoint read failed
        if thread_id:
            # The run's own turn, however many the thread has taken since.
            try:
                answer = await read_run_answer(thread_id, run_id)
            except Exception as e:
                if retry_read and delivery is not None and delivery.held:
                    raise
                logger.warning(
                    f"[AUTOMATION_SETTLE] Answer read failed, settling without it: "
                    f"execution_id={execution_id} run_id={run_id} error={e}"
                )
                answer = RunAnswer()
                answer_read_failed = True
        excerpt = (plain_excerpt(answer.text) or None) if answer.text else None
        # DALTON-HOOK(deliverable-text): resolve empty-text automation excerpt from dalton_channel_deliveries truth and order extracted_text -> sent
        from src.dalton.channels.deliverable_text import resolve_automation_final_text, resolve_completed_automation_excerpt_from_db
        if excerpt is None and not answer_read_failed:
            excerpt = await resolve_completed_automation_excerpt_from_db(execution_id, answer=answer, excerpt=excerpt, thread_id=thread_id, run_id=run_id)
        final_text = resolve_automation_final_text(answer)
    # DALTON-HOOK(automation-subscription-fallback): surface subscription fallback notice in execution excerpt and delivery text
    from src.dalton.services.automation_subscription_fallback import apply_execution_fallback_notice

    excerpt, final_text = await apply_execution_fallback_notice(
        execution_id, thread_id=thread_id, run=run, exec_row=exec_row,
        excerpt=excerpt, text=final_text, include_text=(outcome is Outcome.COMPLETED),
    )
    settle_kwargs: Dict[str, Any] = dict(extra_from_pending)
    if expected_run_id is not exec_db.UNSET_RUN_ID:
        settle_kwargs["expected_run_id"] = expected_run_id
    if check_run_state_in_lock is not None:
        settle_kwargs["check_run_state_in_lock"] = check_run_state_in_lock
    settled = await settle(
        automation, execution_id, outcome,
        thread_id=thread_id, run_id=run_id, workspace_id=workspace_id,
        error=error, excerpt=excerpt, quiet_for=quiet_for,
        delivery=delivery, final_text=final_text,
        **settle_kwargs,
    )
    return outcome if settled else None


# --- DALTON-HOOK(automation-continuation-identity): unified background hold, timeout, and race-safe settlement ---
async def _settle_timeout_for_run(
    automation: Dict[str, Any],
    execution_id: str,
    run_id: Optional[str],
    *,
    thread_id: Optional[str],
    workspace_id: Optional[str],
    run: Optional[Dict[str, Any]] = None,
    quiet_for: Optional[int] = None,
    expected_run_id: Any = exec_db.UNSET_RUN_ID,
    # DALTON-HOOK(automation-subscription-fallback): carry loaded execution row to avoid redundant notice lookups
    exec_row: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Settle a stalled execution with no live turn that exceeded its timeout cap."""
    if run is None and run_id:
        run = await exec_db.get_settling_run(run_id)
    delivery = automation_delivery.delivery_of_run(run, execution_id) if run else None
    # DALTON-HOOK(automation-subscription-fallback): include fallback notice on timed-out executions
    from src.dalton.services.automation_subscription_fallback import apply_execution_fallback_notice

    excerpt, _ = await apply_execution_fallback_notice(
        execution_id, thread_id=thread_id, run=run, exec_row=exec_row,
    )
    settle_kwargs: Dict[str, Any] = {"check_run_state_in_lock": "timeout"}
    if expected_run_id is not exec_db.UNSET_RUN_ID:
        settle_kwargs["expected_run_id"] = expected_run_id
    return await settle(
        automation,
        execution_id,
        Outcome.TIMEOUT,
        thread_id=thread_id,
        run_id=run_id,
        workspace_id=workspace_id,
        error=TIMEOUT_ERROR,
        excerpt=excerpt,
        quiet_for=quiet_for,
        delivery=delivery,
        **settle_kwargs,
    )


async def _resolve_execution_settlement(
    automation: Dict[str, Any],
    execution_id: str,
    exec_row: Optional[Mapping[str, Any]],
    run_id: Optional[str],
    run: Optional[Dict[str, Any]],
    *,
    thread_id: Optional[str],
    workspace_id: Optional[str],
    quiet_for: Optional[int] = None,
    retry_read: bool = False,
    excluding_run_id: Optional[str] = None,
    expected_run_id: Any = exec_db.UNSET_RUN_ID,
) -> Optional[Outcome]:
    """Shared decision order for finished turns, idle callbacks, and sweep:

    1. A live (``in_progress`` or restart-resumable) linked run never settles or times out.
    2. A terminal run already ``completed`` with no open background work settles as COMPLETED,
       even if elapsed time crossed the timeout cap while settlement waited.
    3. A completed run with open background work stays open until work finishes or,
       with no live turn running, crosses the hard timeout cap.
    4. Any other terminal run settles with its own outcome.
    """
    timed_out = (
        exec_row is not None
        and exec_row.get("status") == "running"
        and is_execution_timed_out(exec_row, automation)
    )
    if run is not None:
        outcome = ledger_outcome(run)
        if outcome is None:
            return None
        if outcome is Outcome.COMPLETED:
            pending = bool(
                thread_id
                and await exec_db.has_pending_background_work(
                    execution_id,
                    thread_id=thread_id,
                    excluding_run_id=excluding_run_id,
                )
            )
            if pending:
                if timed_out:
                    settled = await _settle_timeout_for_run(
                        automation,
                        execution_id,
                        run_id,
                        thread_id=thread_id,
                        workspace_id=workspace_id,
                        run=run,
                        quiet_for=None,
                        expected_run_id=expected_run_id,
                        exec_row=exec_row,
                    )
                    return Outcome.TIMEOUT if settled else None
                logger.info(
                    f"[AUTOMATION_SETTLE] Keeping execution open while background work "
                    f"is running: execution_id={execution_id} run_id={run_id}"
                )
                return None
            return await settle_by_run(
                automation,
                execution_id,
                run_id,
                run,
                thread_id=thread_id,
                workspace_id=workspace_id,
                quiet_for=None if timed_out else quiet_for,
                retry_read=retry_read,
                expected_run_id=expected_run_id,
                check_run_state_in_lock="completed_idle",
                exec_row=exec_row,
            )
        return await settle_by_run(
            automation,
            execution_id,
            run_id,
            run,
            thread_id=thread_id,
            workspace_id=workspace_id,
            quiet_for=None if timed_out else quiet_for,
            retry_read=retry_read,
            expected_run_id=expected_run_id,
            check_run_state_in_lock="terminal",
            exec_row=exec_row,
        )
    # DALTON-HOOK(dalton-notify): persisted pre-run failure parameters take precedence over timeout
    from src.dalton.notify.failure_receipts import read_pending_settlement

    if read_pending_settlement(exec_row) is not None:
        return await settle_by_run(
            automation,
            execution_id,
            run_id,
            None,
            thread_id=thread_id,
            workspace_id=workspace_id,
            quiet_for=None if timed_out else quiet_for,
            retry_read=retry_read,
            expected_run_id=expected_run_id,
            exec_row=exec_row,
        )
    if timed_out:
        settled = await _settle_timeout_for_run(
            automation,
            execution_id,
            run_id,
            thread_id=thread_id,
            workspace_id=workspace_id,
            run=None,
            quiet_for=None,
            expected_run_id=expected_run_id,
            exec_row=exec_row,
        )
        return Outcome.TIMEOUT if settled else None
    return await settle_by_run(
        automation,
        execution_id,
        run_id,
        None,
        thread_id=thread_id,
        workspace_id=workspace_id,
        quiet_for=quiet_for,
        retry_read=retry_read,
        expected_run_id=expected_run_id,
        exec_row=exec_row,
    )


async def settle_abandoned(
    automation: Dict[str, Any], row: Dict[str, Any], quiet_for: int
) -> Optional[Outcome]:
    """Settle a firing whose process stopped holding it; the outcome, or
    None when it was left alone or someone else settled it."""
    # DALTON-HOOK(automation-continuation-identity): sweep routes through _resolve_execution_settlement
    execution_id = str(row["automation_execution_id"])
    thread_id = (
        str(row["conversation_thread_id"]) if row["conversation_thread_id"] else None
    )
    run_id = (
        str(row["conversation_response_id"])
        if row["conversation_response_id"]
        else None
    )
    workspace_id = row.get("workspace_id") or automation.get("workspace_id")
    if row["status"] == "waiting":
        settled = await settle(
            automation, execution_id, Outcome.SKIPPED,
            thread_id=thread_id, run_id=run_id, workspace_id=workspace_id,
            skip_reason="interrupted", quiet_for=quiet_for,
            expected_run_id=run_id,
        )
        return Outcome.SKIPPED if settled else None
    run = await exec_db.get_settling_run(run_id) if run_id else None
    return await _resolve_execution_settlement(
        automation,
        execution_id,
        row,
        run_id,
        run,
        thread_id=thread_id,
        workspace_id=workspace_id,
        quiet_for=quiet_for,
        expected_run_id=run_id,
    )


async def _settle_finished_run(job: Dict[str, Any]) -> None:
    """The ``automation_settle`` outbox job: settle the firing whose turn
    the finalized run was, as the run ended."""
    from src.server.services.hook_outbox import MAX_ATTEMPTS

    payload = job.get("payload") or {}
    # DALTON-HOOK(company-automations): a deleted parent still settles.
    # None means the row is physically gone.
    automation = await auto_db.get_automation_including_deleted(
        payload["automation_id"], payload.get("user_id")
    )
    if automation is None:
        return
    execution_id = str(payload["execution_id"])
    run_id = str(job["run_id"])
    thread_id = str(job["conversation_thread_id"])
    workspace_id = payload.get("workspace_id")
    exec_row = await exec_db.get_execution_for_settlement(execution_id)
    expected_run_id: Any = exec_db.UNSET_RUN_ID
    if exec_row is not None:
        if exec_row.get("status") not in ("pending", "waiting", "running"):
            return
        linked_run_id = exec_row.get("conversation_response_id")
        if linked_run_id and str(linked_run_id) != run_id:
            logger.debug(
                f"[AUTOMATION_SETTLE] Skipping stale finalize for superseded run: "
                f"execution_id={execution_id} job_run_id={run_id} "
                f"linked_run_id={linked_run_id}"
            )
            return
        expected_run_id = str(linked_run_id) if linked_run_id else None
    run = await exec_db.get_settling_run(run_id)
    await _resolve_execution_settlement(
        automation,
        execution_id,
        exec_row,
        run_id,
        run,
        thread_id=thread_id,
        workspace_id=workspace_id,
        retry_read=job["attempts"] < MAX_ATTEMPTS,
        # DALTON-HOOK(automation-continuation-identity): pass run CAS + exclude finished run from pending check
        excluding_run_id=run_id,
        expected_run_id=expected_run_id,
    )


async def settle_thread_execution_if_idle(thread_id: str) -> Optional[Outcome]:
    """Settle each running execution on ``thread_id`` whose linked turn reached a
    terminal state and has no remaining background work or live turns.

    Called when a background command or report-back closes without opening a
    continuation turn; checks every running execution on the thread (for shared
    threads or ``thread_strategy: continue``).
    """
    last_settled: Optional[Outcome] = None
    for exec_row in await exec_db.get_running_executions_for_thread(thread_id):
        execution_id = str(exec_row["automation_execution_id"])
        automation = await auto_db.get_automation_including_deleted(
            str(exec_row["automation_id"]), exec_row.get("user_id")
        )
        if automation is None:
            continue
        run_id = (
            str(exec_row["conversation_response_id"])
            if exec_row.get("conversation_response_id")
            else None
        )
        if not run_id and not is_execution_timed_out(exec_row, automation):
            continue
        workspace_id = exec_row.get("workspace_id") or automation.get("workspace_id")
        run = await exec_db.get_settling_run(run_id) if run_id else None
        settled = await _resolve_execution_settlement(
            automation,
            execution_id,
            exec_row,
            run_id,
            run,
            thread_id=thread_id,
            workspace_id=workspace_id,
            expected_run_id=run_id,
        )
        if settled is not None:
            last_settled = settled
    return last_settled
# --- DALTON-HOOK(automation-continuation-identity): end ---


def register_outbox_executors() -> None:
    """Bind the automation_settle and automation_finish executors. Must run
    before the drainer starts."""
    from src.server.services.hook_outbox import register_hook_executor

    register_hook_executor("automation_settle", _settle_finished_run)
    register_hook_executor(FINISH_HOOK, _finish_held_run)
