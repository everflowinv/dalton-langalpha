"""
AutomationExecutor — Executes a single automation run.

Shared by all trigger types (time-based now, event-based later).
Builds a ChatRequest, invokes the appropriate agent workflow,
and drains the async generator (no HTTP client to consume SSE).
Every way a firing ends goes through ``automation_settlement``: once its turn
reached the run ledger, by the run's own finalize.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import HTTPException

from src.server.contracts.status import is_restart_interrupted, restart_resume_expired
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.database.runs import lifecycle as tl_db
from src.server.dependencies.usage_limits import enforce_credit_limit
from src.server.models.chat import ChatMessage, ChatRequest, ThreadOrigin
from src.server.services import automation_delivery
from src.server.services.automation_computer_wait import (
    late_note,
    park_for_computer,
    stopped_meanwhile,
)
from src.server.services.automation_outcomes import LIMITS, Outcome, clean_error_text
from src.server.services.automation_settlement import plan_message, settle
from src.server.database.runs.hook_jobs import AUTOMATION_HANDOFF_HOOK, automation_handoff_job
from src.server.services.runs import restart_drain
from src.server.services.runs.admission import BUSY_STATES
from src.server.services.shared_workspaces.turn import TurnIdentity, carried_recorded_as
from src.server.services.thread_lifecycle_feed import publish_automation_wait
from src.server.services.turn_runtime import TurnRoute, turn_route
from src.server.services.webhook_client import WebhookClient
from src.observability.tracing import hash_id as _obs_hash_id, tracer as _otel_tracer

logger = logging.getLogger(__name__)

# How long a firing waits for its thread's running turn to end before it is
# skipped, and how often it looks.
_WAIT_CAP_SECONDS = 30 * 60
_WAIT_POLL_SECONDS = 5

# The process holding a firing touches its row this often, and the sweep
# settles one quiet for ABANDONED_AFTER_SECONDS. The margin is for event-loop
# stalls, which delay a beat without the process being gone.
_HEARTBEAT_SECONDS = 60
ABANDONED_AFTER_SECONDS = 5 * 60

# 429s that are our capacity rather than the user's usage limit.
_OUR_429_TYPES = frozenset({"burst_limit", "service_unavailable"})

# ``_look_again``'s answer while the thread is still busy.
_STILL_WAITING = object()

async def hand_off(
    automation: Dict[str, Any],
    execution_id: str,
    thread_id: str,
    *,
    from_status: str,
) -> None:
    """Leave a firing this draining process has not started a turn for to the
    next process: back to ``pending``, with a job that starts it there."""
    job = automation_handoff_job(
        execution_id=execution_id,
        automation_id=str(automation["automation_id"]),
        user_id=automation["user_id"],
    )
    handed = await exec_db.hand_off_execution(
        execution_id, from_statuses=(from_status,), thread_id=thread_id, job=job
    )
    logger.info(
        f"[AUTOMATION_EXEC] Draining, {'handed' if handed else 'could not hand'} "
        f"the firing to the next process: execution_id={execution_id}"
    )


async def start_handed_off(job: Dict[str, Any]) -> None:
    """The ``automation_handoff`` job: start the firing a draining process
    handed over, which runs on the thread its row records. A process that is
    itself draining hands the job back. One older than the sweep's margin is
    left to the sweep, which settles it as lost: a firing that late is not
    the one the user expects."""
    from src.server.services.automation_scheduler import AutomationScheduler

    if restart_drain.draining():
        raise RuntimeError("draining; the next process starts it")
    if datetime.now(timezone.utc) - job["created_at"] > timedelta(
        seconds=ABANDONED_AFTER_SECONDS
    ):
        logger.warning(
            f"[AUTOMATION_EXEC] Leaving a stale hand-off to the sweep: "
            f"execution_id={job['payload']['execution_id']}"
        )
        return
    payload = job["payload"]
    automation = await auto_db.get_automation(payload["automation_id"], payload["user_id"])
    if automation is None:
        return
    if checked := payload.get("checked_computer_id"):
        # A hand-off from a build that kept the checked computer in the job
        # rather than on the row: onto the row, where the start reads it.
        await exec_db.transition_execution(
            payload["execution_id"], from_statuses=("pending",), to="pending",
            checked_computer_id=checked,
        )
    AutomationScheduler.get_instance().dispatch(
        automation,
        payload["execution_id"],
        name=f"handed_exec_{payload['automation_id'][:8]}",
    )


def register_outbox_executors() -> None:
    from src.server.services.hook_outbox import register_hook_executor

    register_hook_executor(AUTOMATION_HANDOFF_HOOK, start_handed_off)


@dataclass
class _Firing:
    """How far a firing got, so it settles against the thread and run it
    reached whichever way it ends.

    ``automation`` is re-read after every wait, and ``delivery`` follows its
    entries as they then stand. ``route`` is where the turn was sent, resolved
    once for the automation as it stood then.
    """

    automation: Dict[str, Any]
    # The computer a computer token's run now was checked to run on; its
    # turn's START refuses any other (``admission.refuse_moved_off``).
    checked_computer_id: Optional[str] = None
    workspace_owner_id: Optional[str] = None
    route: Optional[TurnRoute] = None
    thread_id: Optional[str] = None
    run_id: Optional[str] = None
    # The run ledger holds ``run_id`` and the firing records it: the turn went
    # ahead, and the firing now ends however that run ends.
    admitted: bool = False
    # How the firing's start left its delivery: a run the messaging service
    # holds, whose agent sends to its targets and whose settle ends it
    # there, or one left to the webhook; None while nothing was asked.
    delivery: Optional[automation_delivery.Delivery] = None
    # The starts made, and the entries the last one was made for.
    delivery_starts: int = 0
    delivery_entries: Optional[list[str]] = None
    wake_context: Optional[str] = None  # DALTON-HOOK(preflight)
    # What the run tells its agent about starting late, once a wait for its
    # computer held it past when it was due; None when it is on time.
    late: Optional[str] = None

    @property
    def held(self) -> bool:
        """The messaging service holds the run's delivery."""
        return self.delivery is not None and self.delivery.held

    @property
    def workspace_id(self) -> Optional[str]:
        return self.route.workspace_id if self.route else None

    @property
    def delivery_workspace_id(self) -> Optional[str]:
        """The workspace whose chats an app-only delivery entry falls to: an
        Analyst's. Home is none of the user's workspaces, so a run there names
        none and the entry takes the app's preferred chat."""
        if self.route is None or self.route.role != "analyst":
            return None
        return self.route.workspace_id


async def _thread_busy(thread_id: str) -> bool:
    """A turn is live on the thread, a turn a restart stopped waits for its
    resume (``report_back.restart``), or a compact or offload is rewriting
    it.

    All are read where every worker sees them: the run ledger and the
    mutation registry's advertised op.
    """
    from src.server.services.thread_mutation import ThreadMutationRunner

    if await tl_db.get_active_run(thread_id) is not None:
        return True
    latest = await tl_db.get_latest_attempt(thread_id)
    if is_restart_interrupted(latest) and not restart_resume_expired(
        latest["cancel_requested_at"]
    ):
        return True
    return await ThreadMutationRunner.get_instance().is_mutating(thread_id)


async def _keep_alive(execution_id: str) -> None:
    while True:
        await asyncio.sleep(_HEARTBEAT_SECONDS)
        try:
            await exec_db.touch_execution(execution_id)
        except Exception as e:
            # One missed beat is harmless; the sweep waits for five.
            logger.warning(
                f"[AUTOMATION_EXEC] Heartbeat failed: "
                f"execution_id={execution_id} error={e}"
            )


async def _link_run(execution_id: str, run_id: str) -> bool:
    """Record the firing's run on its execution; False once the firing is no
    longer running.

    The one writer of the link. The run's finalize settles the firing either
    way; the link lets the live run be opened from the firing, and lets the
    sweep settle the firing by that run should its finalize job never run.
    """
    return (
        await exec_db.transition_execution(
            execution_id,
            from_statuses=("running",),
            to="running",
            conversation_response_id=run_id,
        )
        is not None
    )


async def _keep_delivery(
    execution_id: str, delivery: Optional[automation_delivery.Delivery]
) -> None:
    """Keep how the firing's start left its delivery in its execution's
    ``delivery`` column, or clear what an earlier start kept when this one
    asked nothing.

    The turn's run row carries the delivery only from START. A process that
    dies before then, or before its turn could start at all, leaves the
    sweep the execution row alone, and without this the sweep would settle
    a run the messaging service holds by webhook, never finishing it there.
    A failed write leaves only that window open: the firing goes on, and its
    run row carries the delivery from START.
    """
    try:
        await exec_db.stamp_delivery(
            execution_id,
            automation_delivery.stamp(delivery) if delivery is not None else None,
        )
    except Exception as e:
        logger.warning(
            f"[AUTOMATION_EXEC] Keeping the delivery on the execution failed: "
            f"execution_id={execution_id} error={e}"
        )


def _lost_thread_race(e: BaseException) -> bool:
    """Someone else's turn took the thread between the idle check and this
    turn's admission: the busy 409, or the run slot the ledger holds for a
    live run."""
    from src.server.database.runs.lifecycle import RunSlotBusyError
    from src.server.database.runs.subagent_runs import TaskRunSlotBusyError

    if isinstance(e, (RunSlotBusyError, TaskRunSlotBusyError)):
        return True
    return (
        isinstance(e, HTTPException)
        and e.status_code == 409
        and isinstance(e.detail, dict)
        and e.detail.get("code") in BUSY_STATES
    )


def _limit_message(e: BaseException) -> Optional[str]:
    """The quota service's words when ``e`` is a usage limit refusing the
    firing; None for anything else.

    Relayed verbatim: which limit applies and how to say so is the
    service's, and the credit gate's own fallback stands in only when a
    denial arrives with none.
    """
    # Lazy: the middleware package imports the automation tools, which
    # import this module.
    from ptc_agent.agent.middleware.credit_gate import DEFAULT_STOP_MESSAGE

    if not (
        isinstance(e, HTTPException)
        and e.status_code == 429
        and isinstance(e.detail, dict)
        and e.detail.get("type") not in _OUR_429_TYPES
    ):
        return None
    return e.detail.get("message") or DEFAULT_STOP_MESSAGE


def _computer_unreachable(e: BaseException) -> bool:
    """The turn's computer did not answer its connect: a machine of the
    user's own asleep, offline or disconnected, which the firing is skipped
    for, not failed. One that answered and refused is the automation's, and
    the tunnel gateway down is ours (``unreachable_code``)."""
    from ptc_agent.core.sandbox.unreachable import unreachable_code

    return unreachable_code(e) is not None


def _our_failure(e: BaseException) -> bool:
    """An outage of ours rather than the automation's failure.

    The credit gate's 503 is its fail-closed answer when the quota service
    gives no verdict, a burst or unavailable 429 is our capacity, an
    unavailable writer guard is the checkpoint store, the tunnel gateway not
    answering is the service's own, and a workspace moving to another
    computer is its owner's doing, not the automation's. A usage limit is not
    here either: it is ``_limit_message``'s, and never a strike.
    """
    from ptc_agent.core.sandbox.unreachable import unanswered_code
    from src.server.database.workspace import WorkspaceMovingError
    from src.server.services.writer_guard import WriterGuardUnavailable

    if isinstance(e, (WriterGuardUnavailable, WorkspaceMovingError)):
        return True
    if unanswered_code(e) == "gateway_unreachable":
        return True
    if not isinstance(e, HTTPException):
        return False
    return e.status_code == 503 or (
        e.status_code == 429
        and isinstance(e.detail, dict)
        and e.detail.get("type") in _OUR_429_TYPES
    )


async def _failure_outcome(e: BaseException, user_id: str) -> tuple[Outcome, Optional[str]]:
    """How a firing that failed before its run took it over settles, and the
    error it records: the account suspended, the computer not answering, a
    Claude plan or usage limit (none a strike), an outage of ours, or the
    automation's own failure."""
    # Lazy: the Claude Code services import the automation tools.
    from src.server.services.account import AccountSuspended
    from src.server.services.llm.claude_code import plan_detail

    if isinstance(e, AccountSuspended):
        return Outcome.SUSPENDED, str(e)
    if _computer_unreachable(e):
        return Outcome.UNREACHABLE, None
    if (plan := plan_detail(e)) is not None:
        message = await plan_message(plan, user_id)
        return Outcome.PLAN_LIMITED, message or clean_error_text(str(plan.get("message")))
    if (limit := _limit_message(e)) is not None:
        return Outcome.LIMITED, limit
    error = f"{type(e).__name__}: {clean_error_text(str(e))}"
    return (Outcome.FAILED_OURS if _our_failure(e) else Outcome.FAILED), error


async def _credentials(user_id: str) -> tuple[bool, bool]:
    """``(has_byok, has_cred)``, read at the turn that uses them.

    has_cred is what the credit gate reports: an OAuth turn pays its own
    vendor bill just as a BYOK one does. The workflow's is_byok is a
    different question, whether to attempt the BYOK ladder, so it keys off
    has_byok alone, or an OAuth-only user gets a futile BYOK prefetch. Own
    access the user's policy denies reads as absent.
    """
    from src.server.services.account import own_access

    own = await own_access(user_id)
    return own.api_keys, own.any


def _pinned_thread(automation: Dict[str, Any]) -> Optional[str]:
    """The thread a 'continue' automation runs on, once it has one."""
    thread_id = automation.get("conversation_thread_id")
    if automation.get("thread_strategy") != "continue" or not thread_id:
        return None
    return str(thread_id)


@dataclass(frozen=True)
class _Place:
    """Where a firing runs: the workspace row its turn names, and the pinned
    thread it continues (None starts one)."""

    workspace: Dict[str, Any]
    thread_id: Optional[str]


async def _turn_place(automation: Dict[str, Any]) -> _Place:
    """Where the automation's turn runs: always its stored workspace.

    A pinned thread is continued while it lives there. Every write keeps a
    pin in the automation's workspace, and the fold that moves a thread to
    Home moves its automations with it, so a pin found anywhere else is one
    this run cannot use: it starts a new thread instead, which 'continue'
    pins. A pinned thread that is gone is made again where the automation
    lives.
    """
    workspace = await _stored_workspace(automation)
    thread_id = _pinned_thread(automation)
    if thread_id is not None:
        from src.server.database.conversation import get_thread_auth_meta

        meta = await get_thread_auth_meta(thread_id)
        if meta is not None and (
            str(meta["workspace_id"]) != str(workspace["workspace_id"])
            or str(meta["user_id"]) != str(automation["user_id"])
        ):
            logger.info(
                f"[AUTOMATION_EXEC] The pinned thread is not in the automation's "
                f"workspace, starting a new thread there: "
                f"automation_id={automation.get('automation_id')} thread_id={thread_id}"
            )
            thread_id = None
    return _Place(workspace, thread_id)


async def _stored_workspace(automation: Dict[str, Any]) -> Dict[str, Any]:
    """The workspace row the automation lives in. For an automation in Home
    that is Home's row, which routes there."""
    from src.server.database.workspace import get_workspace

    ws_id = automation.get("workspace_id")
    if not ws_id:
        raise ValueError(
            "The automation names no workspace "
            "(workspace may have been deleted)"
        )
    workspace = await get_workspace(str(ws_id))
    if workspace is None:
        # Refused here, before a thread is made in it: the turn could never
        # start there.
        raise ValueError(
            "The automation's workspace was not found "
            "(workspace may have been deleted)"
        )
    # DALTON-HOOK(coverage-units): allow web write access when auth enabled; fail closed when disabled or revoked
    await _verify_executor_workspace_access(
        workspace,
        automation,
        str(workspace.get("workspace_id") or ws_id or ""),
    )
    return workspace


async def _verify_executor_workspace_access(
    workspace: Mapping[str, Any],
    automation: Mapping[str, Any],
    workspace_id: str,
) -> None:
    """Verify automation owner has web write access to ``workspace`` (fast path for owner; fail-closed otherwise)."""
    ws_owner = str(workspace.get("user_id") or "").strip()
    auto_owner = str(automation.get("user_id") or "").strip()
    if ws_owner and auto_owner and ws_owner == auto_owner:
        return

    from src.dalton.settings import (
        is_coverage_units_enabled,
        is_dalton_supabase_auth_enabled,
    )
    from src.dalton.sharing import ColleagueAccessRevokedError, require_workspace_access

    if is_coverage_units_enabled() and is_dalton_supabase_auth_enabled():
        if not ws_owner or not auto_owner:
            raise ValueError(
                "The automation's workspace does not belong to the automation owner"
            )
        eff_ws_id = str(workspace.get("workspace_id") or workspace_id or "").strip()
        try:
            await require_workspace_access(
                eff_ws_id,
                auto_owner,
                channel="web",
                need="write",
                workspace=dict(workspace),
            )
        except ColleagueAccessRevokedError as exc:
            raise ValueError(
                f"The automation's workspace does not belong to the automation owner: {exc}"
            ) from exc
        except HTTPException as exc:
            if exc.status_code in (403, 404):
                raise ValueError(
                    f"The automation's workspace does not belong to the automation owner: {exc.detail}"
                ) from exc
            raise
    elif ws_owner and auto_owner and ws_owner != auto_owner:
        raise ValueError(
            "The automation's workspace does not belong to the automation owner"
        )


async def _verify_thread_matches_automation(
    thread_id: str,
    automation: Mapping[str, Any],
    *,
    expected_workspace_id: Optional[str],
) -> None:
    """Re-verify ``thread_id`` still belongs to ``automation``'s workspace and user."""
    from src.server.database.conversation import get_thread_auth_meta

    meta = await get_thread_auth_meta(thread_id)
    if meta is None:
        return
    thread_ws = str(meta.get("workspace_id") or "").strip()
    expected_ws = str(expected_workspace_id or "").strip()
    thread_uid = str(meta.get("user_id") or "").strip()
    auto_uid = str(automation.get("user_id") or "").strip()
    if (expected_ws and thread_ws != expected_ws) or thread_uid != auto_uid:
        raise ValueError(
            "Automation thread does not match automation workspace or owner"
        )


class AutomationExecutor:
    """Singleton that executes automation runs."""

    _instance: Optional["AutomationExecutor"] = None

    @classmethod
    def get_instance(cls) -> "AutomationExecutor":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        # Set while the server shuts down: a waiting firing gives up its place
        # rather than holding shutdown for its whole wait.
        self._stopping = asyncio.Event()

    def stop_waiting(self) -> None:
        self._stopping.set()

    def resume_waiting(self) -> None:
        self._stopping.clear()

    async def _precreate_titled_thread(
        self,
        automation: Dict[str, Any],
        thread_id: str,
        workspace_id: str,
    ) -> None:
        """Pre-create the run's thread titled "<automation name> — <date>".

        Automation runs bypass POST /threads, so without this the thread would be
        born inside ensure_thread_exists with the raw instruction as title (then
        LLM-retitled). A stable name+date reads better for recurring runs. Never
        raises — on failure the run proceeds and the ensure path creates the row.
        """
        name = (automation.get("name") or "").strip()
        if not name:
            return  # no name to stamp — let the ensure path LLM-title it
        try:
            from src.server.database.conversation import create_thread
            from src.utils.timezone_utils import zone_or_none

            tz = zone_or_none(automation.get("timezone")) or timezone.utc
            run_date = datetime.now(tz).strftime("%Y-%m-%d")
            await create_thread(
                conversation_thread_id=thread_id,
                workspace_id=workspace_id,
                current_status="completed",
                title=f"{name} — {run_date}"[:255],
                # DALTON-HOOK(coverage-units): pre-created thread belongs to automation owner even in colleague workspaces
                user_id=str(automation["user_id"]),
                metadata={
                    "origin": {
                        "type": "automation",
                        "id": str(automation["automation_id"]),
                        "name": name,
                    }
                },
            )
        except Exception as e:
            logger.warning(f"[AUTOMATION_EXEC] Thread pre-create failed: {e}")

    async def _resolve_thread(
        self, automation: Dict[str, Any], place: _Place, workspace_id: str
    ) -> str:
        """The pinned thread ``place`` continues, else a new titled one, which
        'continue' pins in place of any pin the run left behind."""
        # DALTON-HOOK(coverage-units): verify web write access or ownership before pre-creating thread
        if workspace_id and str((place.workspace or {}).get("workspace_id") or "") != str(workspace_id):
            raise ValueError(
                "The automation's workspace does not belong to the automation owner"
            )
        await _verify_executor_workspace_access(
            place.workspace or {},
            automation,
            str(workspace_id or (place.workspace or {}).get("workspace_id") or ""),
        )
        if place.thread_id is not None:
            return place.thread_id
        thread_id = str(uuid4())
        if automation.get("thread_strategy") == "continue":
            # DALTON-HOOK(company-automations): runtime thread write-back does not bump config_version or append history
            from src.dalton.services.company_automations import (
                automation_history_context,
            )

            with automation_history_context(record_history=False):
                await auto_db.update_automation(
                    str(automation["automation_id"]), automation["user_id"],
                    conversation_thread_id=thread_id,
                )
        await self._precreate_titled_thread(automation, thread_id, workspace_id)
        return thread_id

    async def _wait_for_thread(
        self,
        automation: Dict[str, Any],
        execution_id: str,
        thread_id: str,
        deadline: float,
    ) -> Optional[Dict[str, Any]]:
        """Hold the firing as ``waiting`` until its thread's turn ends.

        The automation as it stands now, once the thread is free and the
        execution is running again. None when the firing ended skipped
        instead: by the reader, because an earlier firing of it is already
        waiting, because the automation was paused or removed, at
        ``deadline``, or because the server is stopping. Whoever skipped it
        settled the firing.
        """
        # ``started_at`` becomes the wait's start, which the run stamps again
        # when it starts.
        if not await exec_db.transition_execution(
            execution_id,
            from_statuses=("pending", "running"),
            to="waiting",
            conversation_thread_id=thread_id,
            started_at=datetime.now(timezone.utc),
            wait_reason="thread_busy",
        ):
            return None
        logger.info(
            f"[AUTOMATION_EXEC] Waiting for thread: execution_id={execution_id} "
            f"thread_id={thread_id}"
        )
        await publish_automation_wait(
            user_id=automation["user_id"],
            thread_id=thread_id,
            automation_execution_id=execution_id,
            waiting=True,
        )
        try:
            while True:
                try:
                    step = await self._look_again(
                        automation, execution_id, thread_id, deadline
                    )
                except Exception as e:
                    # A failed look is no verdict; the next one decides.
                    logger.warning(
                        f"[AUTOMATION_EXEC] Waiting on the thread failed, still "
                        f"waiting: execution_id={execution_id} error={e}"
                    )
                else:
                    if step is not _STILL_WAITING:
                        return step
                try:
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=_WAIT_POLL_SECONDS
                    )
                except asyncio.TimeoutError:
                    continue
                # The server is stopping: one draining for a restart hands the
                # firing on, still in line for the thread.
                if restart_drain.draining():
                    await hand_off(
                        automation, execution_id, thread_id, from_status="waiting"
                    )
                else:
                    await self._skip_interrupted(automation, execution_id)
                return None
        except asyncio.CancelledError:
            await self._skip_interrupted(automation, execution_id)
            raise

    async def _skip_interrupted(
        self, automation: Dict[str, Any], execution_id: str
    ) -> None:
        try:
            await settle(
                automation, execution_id, Outcome.SKIPPED, skip_reason="interrupted"
            )
        except Exception as e:
            # The sweep skips it once the heartbeat stops.
            logger.error(
                f"[AUTOMATION_EXEC] Skipping an interrupted wait failed: "
                f"execution_id={execution_id} error={e}"
            )

    async def _look_again(
        self,
        automation: Dict[str, Any],
        execution_id: str,
        thread_id: str,
        deadline: float,
    ) -> Any:
        """One look at the line: the fresh automation once this firing may
        run, None once it ended, ``_STILL_WAITING`` otherwise."""
        automation_id = str(automation["automation_id"])
        if await exec_db.get_execution_status(execution_id) != "waiting":
            return None
        # One firing in line is enough: a frequent schedule must not stack up
        # copies of the same run behind a long turn. Asked on every look, since
        # an earlier firing can join the line after this one did.
        if await exec_db.has_earlier_waiting_execution(automation_id, execution_id):
            await settle(
                automation, execution_id, Outcome.SKIPPED, skip_reason="thread_busy"
            )
            return None
        if not await _thread_busy(thread_id):
            fresh = await auto_db.get_automation(automation_id, automation["user_id"])
            if stopped_meanwhile(fresh, automation["status"]):
                await settle(
                    automation, execution_id, Outcome.SKIPPED, skip_reason="user"
                )
                return None
            if not await exec_db.transition_execution(
                execution_id,
                from_statuses=("waiting",),
                to="running",
                started_at=datetime.now(timezone.utc),
            ):
                return None
            await publish_automation_wait(
                user_id=automation["user_id"],
                thread_id=thread_id,
                automation_execution_id=execution_id,
                waiting=False,
            )
            return fresh
        if time.monotonic() >= deadline:
            await settle(
                automation, execution_id, Outcome.SKIPPED, skip_reason="thread_busy"
            )
            return None
        return _STILL_WAITING

    async def _start_delivery(self, execution_id: str, firing: _Firing) -> None:
        """Hand the firing's delivery to the messaging service for its
        automation as it now stands, once per set of entries.

        Called before the turn and again after each wait, since the user can
        change where the automation delivers while it waits. The service
        answers an id it already holds with the targets it resolved then, so
        a firing whose entries changed starts again under an id of its own,
        ``<execution_id>.<n>``, which its sends and its settle name from then
        on. The start made for the old entries is never finished: the
        service lets it lapse and posts nothing for it, where a finish would
        post to chats the user took off.

        What the start left is kept in the execution's ``delivery`` column
        before anything else runs (``_keep_delivery``), each start's
        replacing the last's.
        """
        # DALTON-HOOK(company-automations): do not start delivery after a soft delete
        from src.dalton.database.automation_recycle import automation_is_soft_deleted

        automation_id = str(firing.automation.get("automation_id") or "")
        if automation_id and await automation_is_soft_deleted(automation_id):
            logger.info(
                f"[AUTOMATION_EXEC] Not delivering a deleted automation: "
                f"execution_id={execution_id}"
            )
            return
        entries = automation_delivery.entries_of(firing.automation)
        if firing.delivery_starts and entries == firing.delivery_entries:
            return
        firing.delivery_starts += 1
        firing.delivery_entries = entries
        delivery_id = (
            execution_id
            if firing.delivery_starts == 1
            else f"{execution_id}.{firing.delivery_starts}"
        )
        if firing.delivery_starts > 1:
            logger.info(
                f"[AUTOMATION_EXEC] Delivery changed during the wait, starting it "
                f"again: execution_id={execution_id} delivery_id={delivery_id}"
            )
        kept = firing.delivery
        firing.delivery = await automation_delivery.start_run(
            firing.automation, delivery_id, firing.delivery_workspace_id,
            thread_id=firing.thread_id,
        )
        if firing.delivery is not None or kept is not None:
            await _keep_delivery(execution_id, firing.delivery)

    async def _start_delivery_unstarted(
        self, execution_id: str, firing: _Firing
    ) -> None:
        """Start the delivery of a firing that ends before its start was made
        (a credit refusal, a workspace or thread that couldn't be had), so a
        run the messaging service takes ends there, its targets told it
        didn't finish, rather than by webhook.

        The route is resolved for the workspace an app-only entry falls to,
        which only an Analyst's names, and nothing is bound for a firing that
        won't run. Never raises: the firing settles either way, by webhook
        when nothing was started.
        """
        if firing.delivery_starts:
            return
        try:
            if firing.route is None:
                automation = firing.automation
                firing.route = turn_route(
                    automation["user_id"], (await _turn_place(automation)).workspace
                )
        except Exception as e:
            logger.warning(
                f"[AUTOMATION_EXEC] No route for the delivery of a firing that "
                f"won't run, naming no workspace: execution_id={execution_id} "
                f"error={e}"
            )
        try:
            await self._start_delivery(execution_id, firing)
        except Exception as e:
            logger.error(
                f"[AUTOMATION_EXEC] Starting the delivery of a firing that won't "
                f"run failed: execution_id={execution_id} error={e}"
            )

    async def _note_admission(self, execution_id: str, firing: _Firing) -> bool:
        """Once the ledger holds the turn's run, link it and announce the
        start; False while there is no run row to go by yet, or when
        recording it failed and the next event should retry.

        The turn is already running then, and a bookkeeping error must not
        fail it; the drain's end links the run if no event ever did. A run the
        admission itself cancelled was never the automation's turn.

        The start goes out only while the run is still going. The terminal
        notice leaves from the run's settle job, possibly on another worker,
        and a start landing after it would leave a channel showing a run
        that already ended. This narrows that window; it does not close it.
        A run the messaging service holds announces nothing: its agent sends
        the result itself.
        """
        try:
            run = await tl_db.get_run(firing.run_id)
            if run is None:
                return False
            if run["status"] == "cancelled":
                return True
            if (
                await _link_run(execution_id, firing.run_id)
                and run["status"] == "in_progress"
                and not firing.held
            ):
                await WebhookClient().fire_event(
                    "automation.started", firing.automation, execution_id,
                    firing.thread_id, firing.workspace_id, run_id=firing.run_id,
                )
            firing.admitted = True
            return True
        except Exception as e:
            logger.warning(
                f"[AUTOMATION_EXEC] Recording the admitted run failed, retrying "
                f"on the next event: execution_id={execution_id} error={e}"
            )
            return False

    async def _hand_to_run(self, execution_id: str, firing: _Firing) -> bool:
        """Leave the firing to its run; False when the turn never reached the
        ledger, or a concurrent turn superseded it at admission.

        The run outlives this task in the run executor, and its finalize
        settles the firing as the run ended, on whichever worker drains the
        outbox job (``automation_settlement``): a rejected key or a usage
        limit must not read as this task's failure. A superseded run owes
        the firing no such job.
        """
        run = await tl_db.get_run(firing.run_id) if firing.run_id else None
        if run is None or (run.get("metadata") or {}).get("superseded"):
            return False
        await _link_run(execution_id, firing.run_id)
        return True

    async def _left_to_run(
        self, execution_id: str, error: Exception, firing: _Firing
    ) -> bool:
        """A failure after START is this task's, not the run's: the run's
        finalize settles the firing. False when there is no run to go by, and
        the error then settles it here."""
        try:
            handed = await self._hand_to_run(execution_id, firing)
        except Exception as e:
            logger.error(
                f"[AUTOMATION_EXEC] Linking the run failed: "
                f"execution_id={execution_id} run_id={firing.run_id} error={e}"
            )
            # An admitted run's finalize settles the firing all the same.
            handed = firing.admitted
        if handed:
            logger.error(
                f"[AUTOMATION_EXEC] Following the run failed, leaving the "
                f"firing to it: execution_id={execution_id} "
                f"run_id={firing.run_id} "
                f"error={type(error).__name__}: {clean_error_text(str(error))}"
            )
        return handed

    # DALTON-HOOK(automation-effort): preserve first-turn effort on restart redispatch and ignore stale effort on Antigravity models
    async def _resolve_turn_reasoning_effort(
        self,
        *,
        execution_id: str,
        thread_id: str | None,
        user_id: str,
        llm_model: str | None,
        stored_effort: str | None,
    ) -> str | None:
        from src.dalton.llm.antigravity_series import ANTIGRAVITY_PROVIDER
        from src.dalton.services import automation_thread_model
        from src.llms.llm import LLM as LLMFactory
        from src.server.services.llm import user_models

        effective_effort: str | None = stored_effort
        if thread_id:
            try:
                (
                    _first_model,
                    prior_configured,
                    prior_effort,
                ) = await automation_thread_model._get_execution_first_turn_settings(
                    {
                        "automation_execution_id": execution_id,
                        "conversation_thread_id": thread_id,
                    },
                    fallback_thread_id=thread_id,
                )
            except RuntimeError as exc:
                if "pool" not in str(exc).lower():
                    logger.warning(
                        f"[AUTOMATION_EXEC] Failed reading prior turn effort for execution_id={execution_id}: {exc}"
                    )
            except Exception as exc:
                logger.warning(
                    f"[AUTOMATION_EXEC] Failed reading prior turn effort for execution_id={execution_id}: {exc}"
                )
            else:
                if prior_configured:
                    effective_effort = prior_effort

        # DALTON-HOOK(automation-effort): ignore stale column values when the automation runs a built-in Antigravity model
        if effective_effort is not None and llm_model:
            is_builtin_antigravity = False
            try:
                source, entry = await user_models.classify_model(user_id, llm_model)
                is_builtin_antigravity = (
                    source == user_models.ModelSource.SYSTEM
                    and isinstance(entry, dict)
                    and entry.get("provider") == ANTIGRAVITY_PROVIDER
                )
            except RuntimeError as exc:
                if "pool" in str(exc).lower():
                    try:
                        builtin = LLMFactory.get_model_config().get_model_config(llm_model)
                        is_builtin_antigravity = (
                            isinstance(builtin, dict)
                            and builtin.get("provider") == ANTIGRAVITY_PROVIDER
                        )
                    except Exception:
                        is_builtin_antigravity = False
                else:
                    raise
            if is_builtin_antigravity:
                return None

        return effective_effort  # DALTON-HOOK(automation-effort)

    async def _run_turn(
        self,
        execution_id: str,
        firing: _Firing,
        *,
        has_byok: bool,
        deadline: float,
    ) -> bool:
        """Drain the automation's turn on ``firing.thread_id``; False when the
        firing ended while back in line for the thread instead.

        Losing the thread to another turn before admission means the reader
        started one between the idle check and this one: the firing gets
        back in line behind it, then reads its automation and credentials
        again, starts its delivery again if its entries changed, and reruns
        the credit gate, whose verdict a long wait would outlive.
        """
        # TODO(layering): sanctioned services→handlers residual: automation
        # is an alternate run driver; fixing this means moving the run
        # entrypoints into services/runs/ with handlers as thin adapters.
        from src.server.handlers.chat import astream_ptc_workflow

        while True:
            automation = firing.automation
            user_id = automation["user_id"]
            recorded_as = carried_recorded_as(automation)
            instruction = automation["instruction"]
            additional_context = automation.get("additional_context")
            run_metadata = {
                "automation_execution_id": execution_id,
                "automation_id": str(automation["automation_id"]),
            }
            if name := (automation.get("name") or "").strip():
                # What the run's start and settle notices call it
                # (``services/notices``).
                run_metadata["automation_name"] = name
            # DALTON-HOOK(dispatch-delivery): Every server-started run names its
            # execution row. A held run then keeps that hold's stamp intact,
            # including the id its sends use. On the first start that id is
            # this execution; a restarted hold keeps the id it was filed under.
            extra_configurable: dict[str, Any] = {
                "automation_execution_id": execution_id,
            }
            if firing.late:
                additional_context = [
                    *(additional_context or []),
                    {"type": "directive", "content": firing.late},
                ]
            if firing.held:
                additional_context = [
                    *(additional_context or []),
                    {
                        "type": "directive",
                        "content": automation_delivery.reminder(firing.delivery.targets),
                    },
                ]
                extra_configurable.update(
                    automation_delivery.turn_configurable(firing.delivery)
                )
            if firing.wake_context:  # DALTON-HOOK(preflight)
                additional_context = [
                    *(additional_context or []),
                    {"type": "directive", "content": firing.wake_context},
                ]
            if firing.delivery is not None:
                run_metadata.update(automation_delivery.run_metadata(firing.delivery))
            # DALTON-HOOK(coverage-units): stamp workspace_owner_id and origin_channel=web for cross-user colleague runs
            if firing.workspace_owner_id and str(firing.workspace_owner_id) != str(user_id):
                run_metadata["workspace_owner_id"] = firing.workspace_owner_id
                run_metadata["origin_channel"] = "web"
                extra_configurable["workspace_owner_id"] = firing.workspace_owner_id
            # DALTON-HOOK(automation-effort): stamp two-state reasoning_effort on first-turn run metadata
            # and forward automation reasoning_effort into ChatRequest (preserving prior first-turn effort on
            # restart redispatch and ignoring stale column values on built-in Antigravity models).
            effective_effort = await self._resolve_turn_reasoning_effort(
                execution_id=execution_id,
                thread_id=firing.thread_id,
                user_id=user_id,
                llm_model=automation.get("llm_model"),
                stored_effort=automation.get("reasoning_effort"),
            )
            run_metadata["automation_reasoning_effort_configured"] = True
            run_metadata["automation_reasoning_effort"] = effective_effort
            # DALTON-HOOK(team-model-catalog): when refiring an interrupted automation execution
            # (FiringRunsAgain), pass its prior run_id so ptc_run verifies the live route against
            # the interrupted run's snapshot before building any LLM client.
            refire_source_run_id: str | None = None
            try:
                from src.server.database.runs import lifecycle as _auto_tl_db

                _prior_attempt = await _auto_tl_db.get_latest_attempt(firing.thread_id)
                if _prior_attempt is not None:
                    _prior_meta = _prior_attempt.get("metadata") or {}
                    if str(_prior_meta.get("automation_execution_id") or "") == str(execution_id):
                        refire_source_run_id = (
                            str(_prior_attempt.get("conversation_response_id") or "").strip()
                            or None
                        )
            except Exception:
                refire_source_run_id = None
            request = ChatRequest(
                workspace_id=firing.workspace_id,
                messages=[ChatMessage(role="user", content=instruction)],
                llm_model=automation.get("llm_model"),
                reasoning_effort=effective_effort,
                additional_context=additional_context,
                origin=ThreadOrigin(
                    type="automation",
                    id=str(automation["automation_id"]),
                    name=(automation.get("name") or "").strip() or None,
                ),
                checked_computer_id=firing.checked_computer_id,
                resume_of_run_id=refire_source_run_id,
            )
            # Generate run_id locally: automations don't pass through the
            # HTTP handler that normally creates it. Per-turn keying for
            # BTM / persistence / Redis stream.
            firing.run_id = str(uuid4())
            firing.admitted = admission_known = False
            turn = astream_ptc_workflow(
                request=request,
                thread_id=firing.thread_id,
                run_id=firing.run_id,
                user_input=instruction,
                user_id=user_id,
                route=firing.route,
                identity=await TurnIdentity.of(user_id, firing.route.owner_id, carried=recorded_as),
                is_byok=has_byok,
                from_server=True,
                steerable=False,
                # Stamped on the run at START, so its finalize settles this
                # firing whichever worker is left to drain the job, and
                # delivers the way its start chose.
                run_metadata=run_metadata,
                # Tools read the turn from the graph's config:
                # ``send_message`` names the run the service holds, so its
                # sends reach its targets, and a hand-off records them for
                # the turn that reports its result back.
                extra_configurable=extra_configurable,
            )

            # Drain the async generator: no HTTP client to consume SSE
            event_count = 0
            try:
                async for _event in turn:
                    event_count += 1
                    if not admission_known:
                        admission_known = await self._note_admission(
                            execution_id, firing
                        )
            except Exception as e:
                from src.dalton.sharing import ColleagueAccessRevokedError

                if isinstance(e, ColleagueAccessRevokedError) or (
                    isinstance(e, HTTPException) and e.status_code in (403, 404)
                ):
                    raise ValueError(str(getattr(e, "detail", e))) from e
                if firing.admitted or not _lost_thread_race(e):
                    raise
            else:
                logger.info(
                    f"[AUTOMATION_EXEC] Workflow complete: "
                    f"execution_id={execution_id} events={event_count}"
                )
                return True
            # DALTON-HOOK(coverage-units): capture thread continuation state before waiting
            prior_strategy = str(firing.automation.get("thread_strategy") or "").strip().lower()
            prior_pinned = _pinned_thread(firing.automation)
            firing.run_id = None
            fresh = await self._wait_for_thread(
                automation, execution_id, firing.thread_id, deadline
            )
            if fresh is None:
                return False
            firing.automation = fresh
            from src.server.services.account import read_standing

            (await read_standing(user_id)).refuse_if_suspended()
            # DALTON-HOOK(coverage-units): re-verify owner, resolved workspace, pinned thread, and thread match after wait
            if str(fresh.get("user_id") or "").strip() != str(user_id or "").strip():
                raise ValueError("Automation owner changed while waiting for thread")
            prior_ws_id = firing.workspace_id
            place = await _turn_place(fresh)
            fresh_route = await turn_route(user_id, place.workspace).bind(
                user_id, place.workspace
            )
            if prior_ws_id and fresh_route.workspace_id != prior_ws_id:
                raise ValueError(
                    "Automation workspace changed while waiting for thread; refusing stale thread"
                )
            fresh_strategy = str(fresh.get("thread_strategy") or "").strip().lower()
            fresh_pinned = _pinned_thread(fresh)
            # DALTON-HOOK(coverage-units): cancelling pinned/continue thread settles as SKIPPED instead of FAILED
            if (
                prior_strategy == "continue"
                and fresh_strategy != "continue"
                and firing.thread_id
            ) or (
                prior_pinned is not None
                and firing.thread_id
                and fresh_pinned is None
            ):
                logger.info(
                    "[EXECUTOR] Execution %s settled as skipped: continue/pinned thread %s was cancelled while waiting",
                    execution_id,
                    firing.thread_id,
                )
                settled = await settle(
                    firing.automation,
                    execution_id,
                    Outcome.SKIPPED,
                    workspace_id=fresh_route.workspace_id,
                    skip_reason="user",
                    delivery=firing.delivery,
                    from_statuses=("running",),
                )
                if not settled:
                    logger.warning(
                        "[EXECUTOR] Failed to settle execution %s as skipped after continue/pinned thread cancellation",
                        execution_id,
                    )
                return False
            if (
                prior_pinned is not None
                and firing.thread_id
                and str(fresh_pinned) != str(firing.thread_id)
            ):
                raise ValueError(
                    "Automation pinned thread changed while waiting for thread; refusing stale thread"
                )
            if (
                fresh_pinned is not None
                and firing.thread_id
                and str(fresh_pinned) != str(firing.thread_id)
            ):
                raise ValueError(
                    "Automation pinned thread changed while waiting for thread; refusing stale thread"
                )
            firing.route = fresh_route
            firing.workspace_owner_id = (
                str((place.workspace or {}).get("user_id") or "").strip() or None
            )
            if firing.thread_id:
                await _verify_thread_matches_automation(
                    firing.thread_id,
                    fresh,
                    expected_workspace_id=fresh_route.workspace_id,
                )
            # Before the credit gate, so a refusal there ends the run with
            # the delivery the automation now has.
            await self._start_delivery(execution_id, firing)
            has_byok, has_cred = await _credentials(user_id)
            await enforce_credit_limit(recorded_as, byok=has_cred)

    async def execute(self, automation: Dict[str, Any], execution_id: str) -> None:
        """Run one firing of ``automation`` until it settles or its run does.

        It starts only from ``pending``: the sweep may have settled it while
        it sat in line. Once its turn reached the run ledger, the run's
        finalize settles it. Every other exit goes through ``settle`` here,
        which refuses when a skip or the sweep ended the firing first; the
        firing then stays as they recorded it. A firing a draining process
        handed over (``hand_off``) runs on the thread it had there, and one
        parked for its computer (``park_for_computer``) starts here again
        once the sweep releases it. Every start holds the turn to the computer
        the row's ``checked_computer_id`` names.
        """
        automation_id = str(automation["automation_id"])
        user_id = automation["user_id"]

        logger.info(
            f"[AUTOMATION_EXEC] Starting execution: "
            f"automation_id={automation_id} execution_id={execution_id}"
        )
        # DALTON-HOOK(preflight): start heartbeat early so long waits/preflight are not swept as stale
        keep_alive = asyncio.create_task(
            _keep_alive(execution_id),
            name=f"automation_heartbeat_{execution_id[:8]}",
        )
        try:
            # DALTON-HOOK(company-automations): pending→running only while the parent is not deleted
            started = await exec_db.begin_execution_if_not_deleted(
                execution_id,
                started_at=datetime.now(timezone.utc),
            )
            if not started:
                logger.warning(
                    f"[AUTOMATION_EXEC] Not starting: execution_id={execution_id} "
                    f"was settled or its automation was deleted"
                )
                keep_alive.cancel()
                return
            handed_thread = started.get("conversation_thread_id")
            checked = started.get("checked_computer_id")
            firing = _Firing(
                automation=automation,
                checked_computer_id=str(checked) if checked else None,
                late=await late_note(automation, started),
            )
        except BaseException:
            keep_alive.cancel()
            raise

        _trigger = automation.get("trigger_type") or "unknown"
        _exec_span = _otel_tracer.start_span(
            "automation.execution",
            attributes={
                "automation_id": _obs_hash_id(automation_id),
                "trigger": _trigger,
            },
        )
        wait_deadline = time.monotonic() + _WAIT_CAP_SECONDS
        try:
            # First, so a suspended account's firing starts no delivery.
            from src.server.services.account import read_standing

            (await read_standing(user_id)).refuse_if_suspended()

            # ─── Wait out a running turn ──────────────────────────
            # A run on a pinned thread is a turn of its own, so it waits for
            # the reader's turn to end rather than steering into it. It waits
            # before the credit gate, whose verdict a long wait would outlive.
            pinned = _pinned_thread(automation)
            if pinned and await _thread_busy(pinned):
                fresh = await self._wait_for_thread(
                    automation, execution_id, pinned, wait_deadline
                )
                if fresh is None:
                    _exec_span.set_attribute("status", "skipped")
                    return
                # Nothing is resolved yet, so the run takes the automation
                # as it now stands, its workspace and thread included.
                firing.automation = fresh

            # ─── Wait for the computer ────────────────────────────
            # Also before the credit gate: the wait can last the night. A
            # firing parked here starts over once the computer is back, so
            # its wait for a busy thread is counted from the computer's
            # return, not spent by the night.
            place = await _turn_place(firing.automation)
            route = turn_route(user_id, place.workspace)
            if await park_for_computer(firing.automation, execution_id, route.workspace_id):
                _exec_span.set_attribute("status", "waiting_for_computer")
                return

            # DALTON-HOOK(preflight): zero-LLM wake/skip decision for dalton-* automations.
            # Runs only after thread-busy wait and computer-away park check so signals
            # are never claimed before parking. Recovery checks persisted preflight
            # records by execution_id instead of handed_thread (hand-off during
            # _wait_for_thread sets conversation_thread_id before preflight runs).
            from src.dalton.autonomy import preflight as dalton_preflight
            from src.dalton.database import watch as watch_db

            is_dalton = dalton_preflight.is_dalton_automation(firing.automation)
            persisted_wake_ctx = (
                await watch_db.get_preflight_wake_context(execution_id)
                if is_dalton
                else None
            )
            if persisted_wake_ctx is not None:
                if persisted_wake_ctx:
                    firing.wake_context = persisted_wake_ctx
            else:
                decision = await dalton_preflight.decide(firing.automation, execution_id)
                if decision is not None and decision.skip:
                    await settle(
                        firing.automation,
                        execution_id,
                        Outcome.PRECHECK_SKIPPED,
                        workspace_id=route.workspace_id,
                        skip_reason="precheck",
                        excerpt=decision.summary[:300],
                    )
                    _exec_span.set_attribute("status", "precheck_skipped")
                    return
                firing.wake_context = decision.context if decision else None

            # ─── Credential check + credit gate ───────────────────
            # Again past the wait, which a suspension may have landed in.
            (await read_standing(user_id)).refuse_if_suspended()
            has_byok, has_cred = await _credentials(user_id)
            await enforce_credit_limit(carried_recorded_as(automation), byok=has_cred)

            # ─── Resolve workspace + thread ───────────────────────
            route = await route.bind(user_id, place.workspace)
            firing.route = route
            firing.workspace_owner_id = (
                str((place.workspace or {}).get("user_id") or "").strip() or None
            )
            if handed_thread:
                # DALTON-HOOK(coverage-units): re-verify handed thread matches automation workspace and owner
                await _verify_thread_matches_automation(
                    str(handed_thread),
                    firing.automation,
                    expected_workspace_id=firing.workspace_id,
                )
                firing.thread_id = str(handed_thread)
            else:
                firing.thread_id = await self._resolve_thread(
                    firing.automation, place, firing.workspace_id
                )
            # The thread exists from here on, so record it on the run now: a
            # live run can then be opened and watched while it streams, not
            # only once it settles.
            if not await exec_db.transition_execution(
                execution_id,
                from_statuses=("running",),
                to="running",
                conversation_thread_id=firing.thread_id,
            ):
                logger.warning(
                    f"[AUTOMATION_EXEC] Not running the turn: "
                    f"execution_id={execution_id} was settled elsewhere"
                )
                _exec_span.set_attribute("status", "settled_elsewhere")
                return

            # ─── Hand delivery to the messaging service ───────────
            # Before the turn, so its agent is told where to send. Without
            # the service, or when it won't take the run, the webhook
            # delivers as before.
            await self._start_delivery(execution_id, firing)

            # ─── Run the turn ─────────────────────────────────────
            if not await self._run_turn(
                execution_id, firing, has_byok=has_byok, deadline=wait_deadline
            ):
                _exec_span.set_attribute("status", "skipped")
                return
            # A turn too quick to be seen at an event, or seen only cancelled
            # (a stop), is left to its run here.
            if not firing.admitted and not await self._hand_to_run(
                execution_id, firing
            ):
                raise RuntimeError(
                    f"workflow run finished as 'no run row' (run_id={firing.run_id})"
                )
            _exec_span.set_attribute("status", "handed_to_run")

        except restart_drain.Draining:
            await hand_off(
                firing.automation, execution_id, firing.thread_id, from_status="running"
            )
            _exec_span.set_attribute("status", "handed_off")
        except Exception as e:
            _exec_span.record_exception(e)
            if await self._left_to_run(execution_id, e, firing):
                _exec_span.set_attribute("status", "handed_to_run")
                return
            outcome, error_msg = await _failure_outcome(e, user_id)
            failed = outcome in (Outcome.FAILED, Outcome.FAILED_OURS)
            log = logger.error if failed else logger.warning
            log(
                f"[AUTOMATION_EXEC] The firing ended {outcome}: "
                f"automation_id={automation_id} execution_id={execution_id} "
                f"error={error_msg if error_msg is not None else type(e).__name__}"
            )
            if outcome is not Outcome.SUSPENDED:
                await self._start_delivery_unstarted(execution_id, firing)
            # DALTON-HOOK(preflight): firing.route is bound later at thread resolution, so
            # pre-route failures/cancellations fall back to automation["workspace_id"].
            # DALTON-HOOK(automation-subscription-fallback): release pre-START claimed signals only on executor's synchronous failed settlement;
            # a re-run of the same execution keeps notices from its earlier turns in the excerpt.
            from src.dalton.services.automation_subscription_fallback import apply_execution_fallback_notice

            try:
                fb_row = await exec_db.get_execution_for_settlement(execution_id)
                excerpt, _ = await apply_execution_fallback_notice(
                    execution_id,
                    # Before thread binding the firing has no thread yet; the row may.
                    thread_id=firing.thread_id or (fb_row or {}).get("conversation_thread_id"),
                    exec_row=fb_row,
                )
            except Exception:
                logger.warning(
                    f"[AUTOMATION_EXEC] fallback notice lookup failed: execution_id={execution_id}",
                    exc_info=True,
                )
                excerpt = None
            settled = await settle(
                firing.automation, execution_id, outcome,
                thread_id=firing.thread_id,
                workspace_id=firing.workspace_id or automation.get("workspace_id"),
                error=error_msg, delivery=firing.delivery, excerpt=excerpt,
                release_pre_start_signals=True,
            )
            _exec_span.set_attribute(
                "status",
                "settled_elsewhere" if not settled
                else "limited" if outcome in LIMITS
                else "suspended" if outcome is Outcome.SUSPENDED
                else "failure",
            )
        except asyncio.CancelledError:
            # The server is stopping. A cancel inside a wait already skipped
            # the firing, and this finds it settled. A turn that reached the
            # ledger ends with its run, and anything short of one was
            # interrupted here.
            try:
                if not await self._hand_to_run(execution_id, firing):
                    # DALTON-HOOK(preflight): fall back to automation["workspace_id"] before route bind
                    await settle(
                        firing.automation, execution_id, Outcome.INTERRUPTED,
                        thread_id=firing.thread_id,
                        workspace_id=firing.workspace_id or automation.get("workspace_id"),
                        delivery=firing.delivery,
                    )
            except Exception as e:
                # The sweep settles it once the heartbeat stops.
                logger.error(
                    f"[AUTOMATION_EXEC] Settling an interrupted run failed: "
                    f"execution_id={execution_id} error={e}"
                )
            _exec_span.set_attribute("status", "interrupted")
            raise
        finally:
            keep_alive.cancel()
            _exec_span.end()

