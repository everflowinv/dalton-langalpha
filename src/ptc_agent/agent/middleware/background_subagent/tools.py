"""Background subagent management tools.

This module provides tools for the main agent to interact with background
subagents: reading results and checking progress.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog
from langchain_core.tools import StructuredTool

from ptc_agent.agent.limits import pending_read
from ptc_agent.agent.middleware.background_subagent.outcomes import (
    is_credit_stop,
    is_time_limit_stop,
)
from ptc_agent.agent.middleware.background_subagent.task import bare_task_id
from src.server.contracts.status import REPORT_BACK_STATUSES

if TYPE_CHECKING:
    from ptc_agent.agent.middleware.background_subagent.middleware import (
        BackgroundSubagentMiddleware,
    )
    from ptc_agent.agent.middleware.background_subagent.registry import (
        BackgroundTask,
    )

logger = structlog.get_logger(__name__)

TASK_OUTPUT_DESCRIPTION = """Get the output of a background subagent task, or an overview of all of them.

Args:
    task_id: A specific task's ID, e.g. "k7Xm2p". Omit to see every task.

Returns:
    The task's result once it has completed, or its progress so far."""


def _settled_label(task: BackgroundTask) -> str:
    """How a settled task is labeled in the aggregate views."""
    return {
        "cancelled": "stopped by the user",
        "timeout": "timed out",
        "error": "failed",
        "never_started": "never started",
    }.get(task.terminal_status or "", "completed")


def _timeout_notice(task: BackgroundTask) -> str:
    """What the agent is told about a task that outran its deadline.

    Distinct from the stop notice: a timeout is nobody's decision, so the
    "do not re-dispatch" instruction that belongs to a deliberate stop would
    be wrong here — a narrower retry is the right next move.
    """
    return (
        f"**{task.display_id}** ({task.subagent_type}) ran past its time "
        f"limit and was stopped; no result was produced. Narrow the task or "
        f"split it before re-dispatching."
    )


def _stop_notice(task: BackgroundTask) -> str:
    """What the agent is told about a task the user stopped."""
    return (
        f"**{task.display_id}** ({task.subagent_type}) was stopped "
        f"by the user before it finished; no result was produced. "
        f"Do not re-dispatch it unless the user asks — the stop "
        f"was deliberate. Report the stop and await instruction."
    )


def _credit_stop_notice(task: BackgroundTask) -> str:
    """A stop the budget caused, not the user — so the advice inverts.

    ``_stop_notice``'s "do not re-dispatch, the stop was deliberate" would
    steer the model off the one path this stop is designed to leave open.
    """
    return (
        f"**{task.display_id}** ({task.subagent_type}) stopped when the turn "
        f"ran out of credits; no result was produced. Its prior work is "
        f'preserved — resume it with Task(action="resume", task_id=...) if '
        f"the work is still needed."
    )


def _never_started_notice(task: BackgroundTask) -> str:
    """A task the infrastructure refused before it ever ran — the opposite
    advice from ``_stop_notice``, which is why they must not share a status."""
    reason = f" ({task.error})" if task.error else ""
    return (
        f"**{task.display_id}** ({task.subagent_type}) never started"
        f"{reason}; no work was done and nothing was stopped on purpose. "
        f"Re-dispatch it if the work is still needed."
    )


def _terminal_notice(task: BackgroundTask) -> str | None:
    """The fixed reply for a settled task that produced no result, or None
    when the task has a real result to render."""
    if task.terminal_status == "cancelled":
        if is_credit_stop(task.result):
            return _credit_stop_notice(task)
        return _stop_notice(task)
    if task.terminal_status == "timeout":
        return _timeout_notice(task)
    if task.terminal_status == "never_started":
        return _never_started_notice(task)
    return None


def _sync_task_completion(task: BackgroundTask) -> None:
    """Settle a task whose writer finished without a collector noticing."""
    if task.completed:
        return
    if task.asyncio_task is None:
        return
    if not task.asyncio_task.done():
        return
    task.adopt_writer_outcome(task.asyncio_task)


async def _delivered_result_text(registry, task) -> str:
    """Text handed to the model for a completed task.

    Durable-first: the subagent's answer checkpointed under ``task:{id}`` is
    the primary source (survives registry eviction/stop/restart/other-worker
    reads); the in-memory handler result is the fallback (CLI, failures, or
    a checkpoint that isn't readable yet).
    """
    result = task.result
    success, _ = extract_result_content(result)
    if success:
        resolved = await registry.resolve_result_text(
            task.task_id, task.task_run_id
        )
        if resolved:
            return resolved
    return _format_result(result)


async def _settled_text(registry, task: BackgroundTask, *, listed: bool) -> str:
    """A settled task as a single-task reply, or as its entry in the listing.

    A stopped or never-started task has no result to show, so its notice IS
    the reply; anything else renders its delivered result. Reading the notice
    first also skips a pointless durable read for a task that produced nothing.
    """
    notice = _terminal_notice(task)
    if notice is not None:
        return notice
    body = await _delivered_result_text(registry, task)
    # Named, not hardcoded "completed": a task that settled with terminal_status
    # 'error' reaches here (only stop/timeout/never-started have notices), and
    # announcing its error text as finished work is the one thing that must not
    # happen.
    label = _settled_label(task)
    # DALTON-HOOK(workflow-resume): resumable workflow failures must resume via RunWorkflow, never manual Task re-dispatch.
    if task.terminal_status == "error" and (
        "RESUMABLE" in body
        or (task.subagent_type == "workflow" and "worker_lost" in body)
    ):
        body = (
            f"{body}\n\n"
            f"When continuing (or when the user asks to continue / '继续'), you MUST "
            f"re-launch the workflow with RunWorkflow(workflow=<same workflow>, resume='{task.task_id}') "
            f"to replay completed dispatches from its journal — do NOT manually re-dispatch "
            f"Task subagents (such as data-prep or dalton-evidence)."
        )
    if listed:
        return f"### {task.display_id} ({task.subagent_type}) - {label}\n{body}"
    return f"**{task.display_id}** ({task.subagent_type}) {label}:\n\n{body}"


def _archived_reply(task_id: str, text: str) -> str:
    return (
        f"**Task-{task_id}** completed (result recovered from the "
        f"durable archive):\n\n{text}"
    )


async def _compose_missing_reply(registry, task_id: str, run: dict | None) -> str:
    """Compose the reply text for a run's fate — pure of delivery accounting
    (the ``_missing_task_reply`` funnel stamps).

    Honest failure semantics: a run reports its actual fate rather than a
    predecessor's stale text (or the legacy guess of "cancelled"). The one
    thing that outranks the ledger's verdict is an archived result for THIS
    run — only a run that produced a result archives one, so its presence
    proves work the ledger may simply never have recorded. A run live on
    another worker or turn says so. Pre-ledger tasks (no run row) keep the
    archive-first legacy behavior.
    """
    status = str(run["status"]) if run else None

    if status == "in_progress":
        # DALTON-HOOK(subagent-caps): check Redis queue status when task is owned by another worker.
        from src.dalton.subagent_slots import (
            format_queued_notice_from_status,
            query_queued_slot_status,
        )

        queued_status = await query_queued_slot_status(
            getattr(registry, "thread_id", ""),
            task_id,
            str(run.get("task_run_id") or "") if isinstance(run, dict) else None,
        )
        if queued_status is not None:
            return (
                f"Task-{task_id} is [QUEUED] (排队中, owned by another turn or "
                f"worker). {format_queued_notice_from_status(queued_status)}"
            )
        return (
            f"Task-{task_id} is still running (owned by another turn or "
            "worker). You will be notified when it finishes."
        )
    if status in (None, "completed"):
        # Run-scoped: an archived result belongs to the run that wrote it, and
        # only the ledger knows which run this task's latest one is.
        recovered = await registry.resolve_result_text(
            task_id, str(run["task_run_id"]) if run else None
        )
        if recovered:
            return _archived_reply(task_id, recovered)
        if status == "completed":
            return (
                f"Task-{task_id} completed but its result text is no longer "
                f"available; recover it from the workspace files it produced."
            )
        # The id arrives normalized, so nothing recorded means it names no
        # task. Read as a fate ("never recorded", "cannot be resumed"), a
        # ``Task-`` prefixed id that missed every lookup once sent the agent
        # away from a task that had completed with a stored result.
        return (
            f"No task Task-{task_id} is known on this thread: nothing is running "
            f"under that id, and no run or result was recorded for it. Check the "
            f"id against the launch result that named it."
        )
    if status == "cancelled":
        # The one *why* the ledger can prove, and the one that changes the
        # advice: a credit stop leaves the checkpoint intact and resumable, so
        # the generic text below would talk the model out of the single path
        # this stop exists to leave open. The local notice already branches
        # here; without the same branch the advice would be correct only when
        # the request happened to land on the worker still holding the task in
        # memory, which multi-worker makes a coin flip.
        failure = run.get("failure") if isinstance(run, dict) else None
        if is_credit_stop(failure):
            return (
                f"Task-{task_id} stopped when the turn ran out of credits; no "
                f"result was produced. Its prior work is preserved — resume it "
                f'with Task(action="resume", task_id="{task_id}") if the work '
                f"is still needed."
            )
        # Otherwise deliberately silent on *why*: the ledger spells a timeout
        # kill 'cancelled' too, so claiming the stop was intentional here would
        # tell the agent not to retry a task that merely ran long. The live
        # task carries its own terminal_status and says which it was.
        return (
            f"Task-{task_id} was stopped before finishing; no result was "
            f"delivered. It is not running and cannot be resumed — check "
            f"with the user before re-dispatching it."
        )
    if status == "interrupted":
        return (
            f"Task-{task_id} stopped at an interrupt and was never resumed; "
            f"no final result was produced."
        )
    # Archive-ONLY (never the permissive resolver, which would derive a failed
    # run's mid-work transcript into a plausible-looking answer). A result is
    # archived only by a run that got far enough to produce one, so its
    # presence outranks an error verdict the run never got to overwrite —
    # e.g. a worker lost between the archive write and the ledger CAS.
    archived = await registry.resolve_archived_result_text(
        task_id, str(run["task_run_id"])
    )
    if archived:
        return _archived_reply(task_id, archived)
    failure = run.get("failure") if isinstance(run, dict) else None
    if is_time_limit_stop(failure):
        # The local notice's advice, so it does not hang on which worker
        # answers: a run that outran its clock wants a narrower retry.
        return (
            f"Task-{task_id} ran past its time limit and was stopped; no "
            f"result was produced. Narrow the task or split it before "
            f"re-dispatching."
        )
    detail = ""
    if isinstance(failure, dict) and failure.get("error"):
        detail = f" Error: {str(failure['error'])[:450]}"
    # DALTON-HOOK(workflow-resume): resumable workflow failures must resume via RunWorkflow, never manual Task re-dispatch.
    if "RESUMABLE" in detail or (
        isinstance(run, dict)
        and run.get("subagent_type") == "workflow"
        and "worker_lost" in detail
    ):
        return (
            f"Task-{task_id} failed and produced no final result.{detail} "
            f"When continuing (or when the user asks to continue / '继续'), you MUST "
            f"re-launch the workflow with RunWorkflow(workflow=<same workflow>, resume='{task_id}') "
            f"to replay completed dispatches from its journal — do NOT manually re-dispatch "
            f"Task subagents (such as data-prep or dalton-evidence)."
        )
    return (
        f"Task-{task_id} failed and produced no result.{detail} "
        f"Re-dispatch it if the work is still needed."
    )


async def _missing_task_reply(
    registry, task_id: str, run: dict | None = None
) -> tuple[bool, str]:
    """Reply for a task_id with no authoritative live registry entry, answered
    from the run ledger, and whether that run is still going.

    ``run`` may be a pre-fetched latest-run row (from ``_resolve_task_reply``)
    to avoid a redundant ledger read; when None it is fetched here.
    """
    ledger = getattr(registry, "run_ledger", None)
    if run is None and ledger is not None:
        try:
            run = await ledger.get_latest_run(task_id)
        except Exception:
            logger.warning(
                "missing-task ledger read failed", task_id=task_id, exc_info=True
            )
    reply = await _compose_missing_reply(registry, task_id, run)
    # A reply that hands the agent a run's terminal fate IS the delivery:
    # unstamped, the report-back executor still believes a notification is
    # owed and announces a fate the agent already reported. Stamped at this
    # single exit for exactly REPORT_BACK_STATUSES — the set the outbox
    # enqueues on — so the composer's branches can never drift from the
    # stamp policy. Failures are swallowed: a duplicate notification is the
    # accepted cost of never losing one.
    if ledger is not None and run is not None and str(run["status"]) in REPORT_BACK_STATUSES:
        try:
            await ledger.mark_result_delivered(str(run["task_run_id"]))
        except Exception:
            pass
    return run is not None and str(run["status"]) == "in_progress", reply


async def _resolve_task_reply(
    registry, task: BackgroundTask
) -> tuple[bool, str] | None:
    """Ledger-authoritative resolution of one task's TaskOutput reply.

    The per-process ``BackgroundTask`` is a cross-worker shell on any worker that
    is not currently running the task (checkpoint-hydrated, `asyncio_task=None`,
    zero metrics). It is authoritative ONLY when it is provably this worker's
    LIVE handle for the ledger's CURRENT run; otherwise the durable ledger
    decides, so a stale shell can never shadow terminal (or newer-run) truth.

    Returns ``(is_terminal, reply)`` resolved from the ledger, or ``None`` to
    render locally — no ledger/row (CLI & pre-ledger tasks), or we hold the live
    writer for the current run (our own in-progress work).
    """
    ledger = getattr(registry, "run_ledger", None)
    if ledger is None:
        return None
    try:
        run = await ledger.get_latest_run(task.task_id)
    except Exception:
        return None
    if run is None:
        return None
    holds_live_writer = (
        task.asyncio_task is not None
        and not task.asyncio_task.done()
        and task.task_run_id == str(run["task_run_id"])
    )
    # Our own live run: local progress is the truth. A terminal ledger row wins
    # even over a live local handle (the run committed terminal inside
    # _settle_terminal_run before its outer asyncio handle returned).
    if str(run["status"]) == "in_progress" and holds_live_writer:
        return None
    is_terminal = str(run["status"]) != "in_progress"
    if is_terminal:
        # Execution-context cache, never truth: the reply below hands the
        # agent this run's fate, so local views (progress formatting, the
        # ledger-less sweep) must stop treating it as undelivered.
        task.result_seen = True
    _, reply = await _missing_task_reply(registry, task.task_id, run=run)
    return (is_terminal, reply)


async def _task_reply(registry, task: BackgroundTask, *, listed: bool) -> tuple[bool, str]:
    """One task's TaskOutput answer, and whether the task is still pending.

    A stale cross-worker shell (or a run settled or superseded on another
    worker) defers to the durable ledger; past that the local object is
    authoritative. A listed entry carries the listing's separator: a progress
    block ends in one newline, anything else in a blank line.
    """
    _sync_task_completion(task)
    resolved = await _resolve_task_reply(registry, task)
    if resolved is not None:
        is_terminal, text = resolved
        pending, end = not is_terminal, "\n\n"
    elif not task.completed:
        # DALTON-HOOK(subagent-caps): resolve Redis queue status when local lease is absent.
        from src.dalton.subagent_slots import resolve_task_queued_status

        queued_status = await resolve_task_queued_status(
            task, getattr(registry, "thread_id", "")
        )
        pending, text, end = (
            True,
            _format_task_progress(task, queued_status=queued_status),
            "\n",
        )
    else:
        # Marked seen even when the reply is a stop notice: that notice IS
        # this task's delivery, and without it the orchestrator's
        # settled-but-unseen sweep announces the task all over again and the
        # agent re-reports a stop it already reported.
        task.result_seen = True
        await registry.mark_result_delivered(task)
        pending, end = False, "\n\n"
        text = await _settled_text(registry, task, listed=listed)
    return pending, text + end if listed else text


def create_task_output_tool(middleware: BackgroundSubagentMiddleware) -> StructuredTool:
    """Create the TaskOutput tool.

    It answers with what is known now: a finished task's result reaches the
    agent on its own, mid-turn as a steer or as a new turn once the turn ends.
    """

    async def task_output(task_id: str | None = None) -> tuple[str, dict | None]:
        """Get background task output.

        Args:
            task_id: Task ID (e.g., 'k7Xm2p') or None for all

        Returns:
            Result if completed, progress if still running
        """
        # A read that finds work still running is marked on the artifact, which
        # is how the loop guard tells polling from progress: the progress text
        # itself changes on every read.
        pending, reply = await _read(task_id)
        return reply, pending_read() if pending else None

    async def _read(task_id: str | None) -> tuple[bool, str]:
        registry = middleware.registry
        task_id = bare_task_id(task_id)

        if task_id is not None:
            task = await registry.get_by_task_id(task_id)
            if not task:
                return await _missing_task_reply(registry, task_id)
            return await _task_reply(registry, task, listed=False)

        # Workflow-owned children are hidden from the aggregate view (their
        # results belong to the driver) but remain individually addressable
        # via TaskOutput(task_id=...) as the drill-in escape hatch.
        all_tasks = await registry.get_turn_visible_tasks()
        if not all_tasks:
            return False, "No background tasks have been assigned yet."

        # Each entry and its pending flag come from one reading of the task, so
        # the header's counts cannot disagree with the body.
        entries = [
            await _task_reply(registry, task, listed=True)
            for task in sorted(all_tasks, key=lambda t: t.task_id)
        ]
        pending_count = sum(1 for pending, _ in entries if pending)
        completed_count = len(entries) - pending_count
        # DALTON-HOOK(subagent-caps): surface queued count in TaskOutput overview (including cross-worker).
        from src.dalton.subagent_slots import resolve_task_queued_status

        queued_statuses = [
            await resolve_task_queued_status(task, getattr(registry, "thread_id", ""))
            for task in all_tasks
            if not task.completed and task.terminal_status is None
        ]
        queued_count = sum(1 for qs in queued_statuses if qs is not None)
        running_summary = (
            f"{max(0, pending_count - queued_count)} running, {queued_count} queued (排队中)"
            if queued_count > 0
            else f"{pending_count} running"
        )
        header = (
            f"**Background Tasks** ({len(all_tasks)} total: "
            f"{completed_count} completed, {running_summary})\n\n"
        )
        return pending_count > 0, header + "".join(entry for _, entry in entries)

    return StructuredTool.from_function(
        name="TaskOutput",
        description=TASK_OUTPUT_DESCRIPTION,
        coroutine=task_output,
        response_format="content_and_artifact",
    )


def extract_result_content(result: dict[str, Any] | Any) -> tuple[bool, str]:
    """Extract content from a task result.

    Handles various result types including raw values, dicts with success/error,
    objects with .content attribute, and Command types with .update.messages.

    Args:
        result: The task result (dict, Command, or raw value)

    Returns:
        Tuple of (success: bool, content: str)
    """
    if not isinstance(result, dict):
        return (True, str(result))

    if result.get("success"):
        inner = result.get("result")
        if inner is None:
            return (True, "Task completed successfully (no output)")
        if hasattr(inner, "content"):
            return (True, str(inner.content))
        # Handle Command type
        if hasattr(inner, "update"):
            update = inner.update
            if isinstance(update, dict) and "messages" in update:
                messages = update["messages"]
                if messages:
                    last_msg = messages[-1]
                    if hasattr(last_msg, "content"):
                        return (True, str(last_msg.content))
        return (True, str(inner))

    error = result.get("error", "Unknown error")
    status = result.get("status", "error")
    return (False, f"{status.upper()}: {error}")


def _format_result(result: dict[str, Any] | Any) -> str:
    """Format a single task result for display.

    Args:
        result: The task result dict

    Returns:
        Formatted string
    """
    success, content = extract_result_content(result)
    if success:
        return content
    return f"**{content}**"


def _fmt_ago(ts: float) -> str:
    """Format a past timestamp as a coarse 'Xs/m/h ago' string."""
    delta = max(0.0, time.time() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    return f"{int(delta / 3600)}h ago"


def _format_task_progress(task: BackgroundTask, queued_status: Any = None) -> str:
    """Format progress info for a single task.

    Args:
        task: The BackgroundTask to format
        queued_status: Optional resolved ``QueuedSlotStatus`` (local or Redis)

    Returns:
        Formatted progress string
    """
    elapsed = time.time() - task.created_at

    # Status indicator
    # DALTON-HOOK(subagent-caps): show queued status and queue notice in TaskOutput.
    from src.dalton.subagent_slots import (
        format_queued_notice,
        is_subagent_queued,
    )

    queued = task.terminal_status is None and (
        queued_status is not None or is_subagent_queued(task)
    )
    status = (
        "[QUEUED] (排队中)"
        if queued
        else {
            None: "[RUNNING]",
            "completed": "[DONE]",
            "cancelled": "[STOPPED]",
            "timeout": "[TIMED OUT]",
            "error": "[ERROR]",
            "never_started": "[NEVER STARTED]",
        }[task.terminal_status]
    )

    # Tool call summary (always show, even if 0)
    tool_summary = f" | {task.total_tool_calls} tool calls"
    if task.tool_call_counts:
        # Show top 3 tools
        top_tools = sorted(task.tool_call_counts.items(), key=lambda x: -x[1])[:3]
        tool_details = ", ".join(f"{t}: {c}" for t, c in top_tools)
        tool_summary += f" ({tool_details})"

    # Current activity (only for running tasks)
    # DALTON-HOOK(subagent-caps): include queue notice in progress block when queued.
    activity = ""
    if queued:
        activity = f"\n  Queue: {format_queued_notice(task, queued_status=queued_status)}"
    elif not task.completed and task.current_tool:
        activity = f"\n  Currently executing: `{task.current_tool}`"

    # Staleness summary: how long since anything meaningful changed (see
    # Part 5 in plan).
    staleness = f"\n  _last changed {_fmt_ago(task.last_updated_at)}_"

    return (
        f"### {task.display_id}: {task.subagent_type}\n"
        f"  Status: {status} | Elapsed: {elapsed:.1f}s{tool_summary}{activity}{staleness}\n"
        f"  Task: {task.description[:100]}{'...' if len(task.description) > 100 else ''}"
    )
