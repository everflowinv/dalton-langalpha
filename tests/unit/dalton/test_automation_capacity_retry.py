"""Unit tests for automation capacity backoff retry and pre-START thread status reset.

Covers:
1. Capacity exhaustion -> 2nd attempt acquires slot -> completes normally with a single
   execution row and unchanged failure_count.
2. Capacity always exhausted -> exceeds 15-minute wait cap -> settles as FAILED_OURS
   and resets conversation_threads.current_status away from 'in_progress'.
3. Normal (non-capacity) failure path also resets conversation_threads.current_status
   away from 'in_progress'.
4. Two workers scanning concurrently -> only one worker releases and dispatches the retry.
5. DALTON_AUTOMATION_CAPACITY_RETRY switch (default True; when disabled, behaves
   identically to upstream with immediate FAILED_OURS settlement).
6. Backoff growth with jitter, heartbeat refresh, paused-automation skip, delivery JSONB
   preservation across stamp_delivery, and non-capacity error exclusions.

All test doubles strictly use ``create_autospec``.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException
from psycopg import AsyncConnection, AsyncCursor

from src.dalton.automations import capacity_retry as cr
from src.dalton.database.automation_recycle import automation_is_soft_deleted
from src.dalton.settings import is_automation_capacity_retry_enabled
from src.server.database import automation as auto_db_mod
from src.server.database import automation_executions as exec_db_mod
from src.server.database import conversation as conv_db_mod
from src.server.database import workspace as ws_db_mod
from src.server.database.api_keys import is_byok_active
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.oauth_tokens import has_any_oauth_token
from src.server.database.runs import lifecycle as tl_db_mod
from src.server.dependencies.usage_limits import enforce_credit_limit
from src.server.handlers.chat import astream_ptc_workflow
from src.server.services import automation_executor as exec_mod
from src.server.services import automation_settlement as settle_mod
from src.server.services import home_placement
from src.server.services import ssh_computers
from src.server.services.automation_executor import AutomationExecutor
from src.server.services.automation_outcomes import Outcome
from src.server.services.automation_scheduler import AutomationScheduler
from src.server.services.thread_lifecycle_feed import publish_automation_wait
from src.server.services.webhook_client import WebhookClient
from src.server.services.workspace_manager import WorkspaceManager
from src.server.services.writer_guard import WriterGuardUnavailable

_USER = "user-cap-1"
_AUTO = "00000000-0000-0000-0000-000000000101"
_EXEC = "00000000-0000-0000-0000-000000000202"
_THREAD = "00000000-0000-0000-0000-000000000303"
_HOME = get_home_workspace_id(_USER)


def _automation(**overrides: Any) -> dict[str, Any]:
    return {
        "automation_id": _AUTO,
        "user_id": _USER,
        "name": "dalton-nvda-delta",
        "instruction": "Run delta analysis",
        "trigger_type": "cron",
        "workspace_id": _HOME,
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "llm_model": None,
        "additional_context": None,
        "trigger_config": None,
        "status": "active",
        "failure_count": 0,
        **overrides,
    }


@dataclass
class _FakeState:
    """In-memory relational state simulating Postgres rows for multi-worker tests."""

    now: datetime = field(
        default_factory=lambda: datetime(2026, 11, 28, 3, 0, 0, tzinfo=timezone.utc)
    )
    automation: dict[str, Any] = field(default_factory=_automation)
    executions: dict[str, dict[str, Any]] = field(default_factory=dict)
    threads: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    touch_count: int = 0
    release_calls: int = 0

    def seed_execution(
        self,
        execution_id: str = _EXEC,
        *,
        status: str = "pending",
        thread_id: Optional[str] = None,
        awaits_until: Optional[datetime] = None,
        wait_reason: Optional[str] = None,
        parked_status: Optional[str] = None,
        delivery: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        row = {
            "automation_execution_id": execution_id,
            "automation_id": self.automation["automation_id"],
            "status": status,
            "conversation_thread_id": thread_id,
            "conversation_response_id": None,
            "scheduled_at": self.now,
            "started_at": None,
            "completed_at": None,
            "heartbeat_at": self.now,
            "wait_reason": wait_reason,
            "awaits_computer": None,
            "checked_computer_id": None,
            "parked_status": parked_status,
            "awaits_until": awaits_until,
            "delivery": delivery,
            "error_message": None,
            "skip_reason": None,
            "failure_reason": None,
            "created_at": self.now,
        }
        self.executions[execution_id] = row
        return row

    def seed_thread(
        self,
        thread_id: str = _THREAD,
        *,
        current_status: str = "completed",
    ) -> dict[str, Any]:
        row = {
            "conversation_thread_id": thread_id,
            "workspace_id": self.automation["workspace_id"],
            "user_id": self.automation["user_id"],
            "current_status": current_status,
        }
        self.threads[thread_id] = row
        return row

    def make_db_connection_cm(self):
        """Return an async context manager yielding a ``create_autospec(AsyncConnection)``."""

        @asynccontextmanager
        async def _get_conn():
            conn = create_autospec(AsyncConnection, instance=True)
            cur = create_autospec(AsyncCursor, instance=True)
            cur.rowcount = 0
            fetched_one: list[Any] = [None]
            fetched_all: list[list[Any]] = [[]]

            async def _execute(sql: str, params: Any = None) -> None:
                norm = " ".join(sql.split())
                cur.rowcount = 0
                fetched_one[0] = None
                fetched_all[0] = []

                if norm.startswith("SELECT status, awaits_until, parked_status, delivery"):
                    eid = params[0]
                    row = self.executions.get(eid)
                    if row and row["status"] in ("pending", "running"):
                        fetched_one[0] = {**row, "db_now": self.now}
                    return

                if norm.startswith("UPDATE automation_executions SET status = 'waiting'"):
                    eid = params["execution_id"]
                    row = self.executions.get(eid)
                    if row and row["status"] in ("pending", "running"):
                        row["status"] = "waiting"
                        row["wait_reason"] = params["wait_reason"]
                        if row["parked_status"] is None:
                            row["parked_status"] = params["parked_status"]
                        if row["awaits_until"] is None:
                            row["awaits_until"] = params["deadline"]
                        row["heartbeat_at"] = self.now
                        base = dict(row["delivery"] or {})
                        payload = params["retry_payload"]
                        base[cr.CAPACITY_RETRY_KEY] = (
                            payload.obj if hasattr(payload, "obj") else payload
                        )
                        row["delivery"] = base
                        cur.rowcount = 1
                    return

                if norm.startswith("UPDATE conversation_threads ct SET current_status = COALESCE"):
                    fallback, tid = params
                    trow = self.threads.get(str(tid))
                    if trow and trow["current_status"] in ("in_progress", "active"):
                        has_live = any(
                            r["conversation_thread_id"] == str(tid)
                            and r["status"] == "in_progress"
                            for r in self.runs.values()
                        )
                        if not has_live:
                            prior = [
                                r
                                for r in self.runs.values()
                                if r["conversation_thread_id"] == str(tid)
                            ]
                            trow["current_status"] = (
                                prior[-1]["status"] if prior else fallback
                            )
                            cur.rowcount = 1
                    return

                if norm.startswith("UPDATE automation_executions SET heartbeat_at = NOW() WHERE status = 'waiting'"):
                    count = 0
                    for row in self.executions.values():
                        if (
                            row["status"] == "waiting"
                            and row["wait_reason"] == cr.CAPACITY_WAIT_REASON
                            and (
                                row["awaits_until"] is None
                                or row["awaits_until"] > self.now
                            )
                        ):
                            row["heartbeat_at"] = self.now
                            count += 1
                    self.touch_count += count
                    cur.rowcount = count
                    return

                if norm.startswith("SELECT e.automation_execution_id, e.automation_id, e.parked_status"):
                    out = []
                    for row in self.executions.values():
                        if (
                            row["status"] != "waiting"
                            or row["wait_reason"] != cr.CAPACITY_WAIT_REASON
                        ):
                            continue
                        if self.automation.get("deleted_at") is not None:
                            continue
                        expired = bool(
                            row["awaits_until"] is not None
                            and row["awaits_until"] <= self.now
                        )
                        retry_meta = (row["delivery"] or {}).get(cr.CAPACITY_RETRY_KEY) or {}
                        next_raw = retry_meta.get("next_retry_at")
                        due = (
                            True
                            if not next_raw
                            else datetime.fromisoformat(next_raw) <= self.now
                        )
                        if expired or due:
                            out.append(
                                {
                                    "automation_execution_id": row["automation_execution_id"],
                                    "automation_id": row["automation_id"],
                                    "parked_status": row["parked_status"],
                                    "conversation_thread_id": row["conversation_thread_id"],
                                    "delivery": row["delivery"],
                                    "awaits_until": row["awaits_until"],
                                    "user_id": self.automation["user_id"],
                                    "workspace_id": self.automation["workspace_id"],
                                    "expired": expired,
                                    "due": due,
                                }
                            )
                    fetched_all[0] = out
                    return

                if norm.startswith("UPDATE automation_executions e SET status = 'pending'"):
                    self.release_calls += 1
                    eid = params["execution_id"]
                    row = self.executions.get(eid)
                    if (
                        row
                        and row["status"] == "waiting"
                        and row["wait_reason"] == cr.CAPACITY_WAIT_REASON
                        and self.automation.get("deleted_at") is None
                    ):
                        expired = bool(
                            row["awaits_until"] is not None
                            and row["awaits_until"] <= self.now
                        )
                        retry_meta = (row["delivery"] or {}).get(cr.CAPACITY_RETRY_KEY) or {}
                        next_raw = retry_meta.get("next_retry_at")
                        due = (
                            True
                            if not next_raw
                            else datetime.fromisoformat(next_raw) <= self.now
                        )
                        if not expired and due:
                            row["status"] = "pending"
                            row["heartbeat_at"] = self.now
                            cur.rowcount = 1
                    return

                raise AssertionError(f"Unexpected SQL in fake DB: {norm}")

            cur.execute.side_effect = _execute
            cur.fetchone.side_effect = lambda: fetched_one[0]
            cur.fetchall.side_effect = lambda: list(fetched_all[0])

            @asynccontextmanager
            async def _cursor_cm(*_a: Any, **_kw: Any):
                yield cur

            @asynccontextmanager
            async def _tx_cm(*_a: Any, **_kw: Any):
                yield conn

            conn.cursor.side_effect = _cursor_cm
            conn.transaction.side_effect = _tx_cm
            yield conn

        return _get_conn


def _bind_executor_env(
    stack: ExitStack,
    state: _FakeState,
    turns: list[Any],
    *,
    credit_side_effect: Any = None,
) -> dict[str, Any]:
    """Wire AutomationExecutor + capacity_retry against ``state`` using ``create_autospec``."""
    call_idx = [0]

    async def _begin_exec(execution_id: str, *, started_at: datetime):
        row = state.executions.get(execution_id)
        if not row or row["status"] != "pending":
            return None
        row["status"] = "running"
        row["started_at"] = started_at
        return {
            "conversation_thread_id": row["conversation_thread_id"],
            "conversation_response_id": row["conversation_response_id"],
            "scheduled_at": row["scheduled_at"],
            "wait_reason": row["wait_reason"],
            "awaits_computer": row["awaits_computer"],
            "checked_computer_id": row["checked_computer_id"],
        }

    async def _transition_exec(
        execution_id: str,
        *,
        from_statuses: Any,
        to: str,
        automation_id: Optional[str] = None,
        wait_reason: Optional[str] = None,
        **fields: Any,
    ):
        row = state.executions.get(execution_id)
        if not row or row["status"] not in from_statuses:
            return None
        row["status"] = to
        if wait_reason is not None:
            row["wait_reason"] = wait_reason
        for k, v in fields.items():
            if v is not None:
                row[k] = v
        return dict(row)

    async def _settle_exec(
        execution_id: str,
        *,
        automation_id: str,
        from_statuses: Any,
        to: str,
        strike: Optional[str] = None,
        schedule: Optional[str] = None,
        quiet_for: Optional[int] = None,
        expected_run_id: Any = exec_db_mod.UNSET_RUN_ID,
        check_run_state_in_lock: Any = None,
        parked: bool = False,
        in_transaction: Any = None,
        **fields: Any,
    ):
        row = state.executions.get(execution_id)
        if not row or row["status"] not in from_statuses:
            return None
        prev_status = row["status"]
        row["status"] = to
        for k, v in fields.items():
            if v is not None:
                row[k] = v
        if strike == "count":
            state.automation["failure_count"] = int(state.automation.get("failure_count", 0)) + 1
        elif strike == "reset":
            state.automation["failure_count"] = 0
        return {
            **row,
            "settled_from": prev_status,
            "previous_failure_reason": None,
            "previous_delivery_result": None,
        }

    async def _create_thread(**kwargs: Any):
        tid = kwargs["conversation_thread_id"]
        state.threads[tid] = {
            "conversation_thread_id": tid,
            "workspace_id": kwargs["workspace_id"],
            "user_id": kwargs.get("user_id", _USER),
            "current_status": kwargs["current_status"],
        }
        return tid

    async def _get_thread_auth_meta(thread_id: str):
        trow = state.threads.get(thread_id)
        if trow:
            return {"workspace_id": trow["workspace_id"], "user_id": trow["user_id"]}
        return {"workspace_id": _HOME, "user_id": _USER}

    async def _get_run(run_id: str):
        return state.runs.get(run_id)

    def _astream(**kwargs: Any):
        idx = call_idx[0]
        call_idx[0] += 1
        return turns[idx](**kwargs)

    begin_mock = create_autospec(
        exec_db_mod.begin_execution_if_not_deleted, side_effect=_begin_exec
    )
    trans_mock = create_autospec(
        exec_db_mod.transition_execution, side_effect=_transition_exec
    )
    settle_mock = create_autospec(
        exec_db_mod.settle_execution, side_effect=_settle_exec
    )
    get_auto_mock = create_autospec(
        auto_db_mod.get_automation,
        side_effect=lambda aid, uid: (
            dict(state.automation)
            if str(aid) == str(state.automation["automation_id"])
            else None
        ),
    )
    get_exec_settle_mock = create_autospec(
        exec_db_mod.get_execution_for_settlement,
        side_effect=lambda eid: (
            dict(state.executions[eid]) if eid in state.executions else None
        ),
    )
    credit_mock = create_autospec(enforce_credit_limit, side_effect=credit_side_effect)
    astream_mock = create_autospec(astream_ptc_workflow, side_effect=_astream)

    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    ws_mgr.ensure_home_bound.return_value = None

    webhook_client = create_autospec(WebhookClient, instance=True)
    webhook_client.fire_event.return_value = None

    for ctx in (
        patch.object(cr, "get_db_connection", side_effect=state.make_db_connection_cm()),
        patch.object(exec_mod.exec_db, "begin_execution_if_not_deleted", new=begin_mock),
        patch.object(exec_mod.exec_db, "transition_execution", new=trans_mock),
        patch.object(exec_mod.exec_db, "get_execution_for_settlement", new=get_exec_settle_mock),
        patch.object(exec_mod.exec_db, "stamp_delivery", new=create_autospec(exec_db_mod.stamp_delivery, return_value=True)),
        patch.object(exec_mod.exec_db, "touch_execution", new=create_autospec(exec_db_mod.touch_execution, return_value=None)),
        patch.object(settle_mod.exec_db, "settle_execution", new=settle_mock),
        patch.object(settle_mod.exec_db, "record_delivery", new=create_autospec(exec_db_mod.record_delivery, return_value=None)),
        patch.object(exec_mod.auto_db, "get_automation", new=get_auto_mock),
        patch.object(cr.auto_db, "get_automation", new=get_auto_mock),
        patch.object(settle_mod.auto_db, "get_automation", new=get_auto_mock),
        patch("src.dalton.database.automation_recycle.automation_is_soft_deleted", new=create_autospec(automation_is_soft_deleted, return_value=False)),
        patch("src.dalton.autonomy.preflight.is_dalton_automation", return_value=False),
        patch("src.server.database.api_keys.is_byok_active", new=create_autospec(is_byok_active, return_value=False)),
        patch("src.server.database.oauth_tokens.has_any_oauth_token", new=create_autospec(has_any_oauth_token, return_value=False)),
        patch.object(exec_mod, "enforce_credit_limit", new=credit_mock),
        patch.object(ws_db_mod, "get_workspace", new=create_autospec(ws_db_mod.get_workspace, return_value={"workspace_id": _HOME, "user_id": _USER, "status": "running"})),
        patch.object(conv_db_mod, "create_thread", new=create_autospec(conv_db_mod.create_thread, side_effect=_create_thread)),
        patch.object(conv_db_mod, "get_thread_auth_meta", new=create_autospec(conv_db_mod.get_thread_auth_meta, side_effect=_get_thread_auth_meta)),
        patch.object(WorkspaceManager, "get_instance", return_value=ws_mgr),
        patch.object(ssh_computers, "computer_away", new=create_autospec(ssh_computers.computer_away, return_value=None)),
        patch.object(home_placement, "place_home_turn", new=create_autospec(home_placement.place_home_turn, return_value=None)),
        patch.object(exec_mod, "WebhookClient", return_value=webhook_client),
        patch.object(settle_mod, "WebhookClient", return_value=webhook_client),
        patch.object(exec_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)),
        patch.object(settle_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)),
        patch.object(tl_db_mod, "get_run", new=create_autospec(tl_db_mod.get_run, side_effect=_get_run)),
        patch.object(tl_db_mod, "get_latest_attempt", new=create_autospec(tl_db_mod.get_latest_attempt, return_value=None)),
        patch("src.server.handlers.chat.astream_ptc_workflow", new=astream_mock),
    ):
        stack.enter_context(ctx)

    return {
        "begin": begin_mock,
        "transition": trans_mock,
        "settle": settle_mock,
        "credit": credit_mock,
        "astream": astream_mock,
    }


# ---------------------------------------------------------------------------
# 1. Quota exhausted -> 2nd attempt succeeds -> single execution, 0 strikes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cap_exc",
    [
        WriterGuardUnavailable("pinned-session budget exhausted (25 held)"),
        HTTPException(
            status_code=429,
            detail={"type": "burst_limit", "message": "Burst capacity reached"},
        ),
        HTTPException(
            status_code=429,
            detail={"type": "service_unavailable", "message": "Quota service busy"},
        ),
        HTTPException(
            status_code=503,
            detail={"type": "service_unavailable", "message": "Credit gate 503"},
        ),
    ],
    ids=["writer_guard", "429_burst_limit", "429_service_unavailable", "503_credit_gate"],
)
async def test_capacity_exhausted_retries_and_succeeds_on_second_attempt(
    monkeypatch, cap_exc
):
    """Capacity error parks the execution, releases thread in_progress, retries after
    backoff, and completes on attempt 2 with one execution row and unchanged failure_count."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_execution(_EXEC)

    def _turn_fails_prestart(**kwargs):
        async def _gen():
            # Simulate ensure_thread flipping thread status to 'in_progress' ahead of START
            tid = kwargs["thread_id"]
            state.threads[tid]["current_status"] = "in_progress"
            raise cap_exc
            yield

        return _gen()

    def _turn_succeeds(**kwargs):
        async def _gen():
            tid = kwargs["thread_id"]
            rid = kwargs["run_id"]
            state.threads[tid]["current_status"] = "in_progress"
            state.runs[rid] = {
                "conversation_response_id": rid,
                "conversation_thread_id": tid,
                "status": "completed",
                "metadata": {},
            }
            state.threads[tid]["current_status"] = "completed"
            yield "event: message_chunk\ndata: {}\n\n"

        return _gen()

    executor = AutomationExecutor()
    dispatched: list[tuple[dict[str, Any], str]] = []

    def _dispatch(auto: dict[str, Any], eid: str, *, name: str) -> None:
        dispatched.append((auto, eid))

    dispatch_mock = create_autospec(_dispatch, side_effect=_dispatch)

    with ExitStack() as stack:
        mocks = _bind_executor_env(stack, state, [_turn_fails_prestart, _turn_succeeds])
        # Attempt 1: hits capacity error -> parked in DB, NOT failed
        await executor.execute(state.automation, _EXEC)

        exec_row = state.executions[_EXEC]
        assert len(state.executions) == 1
        assert exec_row["status"] == "waiting"
        assert exec_row["wait_reason"] == cr.CAPACITY_WAIT_REASON
        assert exec_row["awaits_until"] == state.now + timedelta(
            seconds=cr.DEFAULT_MAX_WAIT_SECONDS
        )
        assert exec_row["parked_status"] == "active"
        assert state.automation["failure_count"] == 0
        mocks["settle"].assert_not_awaited()

        # Thread created on attempt 1 was reset out of 'in_progress' while queued
        tid = exec_row["conversation_thread_id"]
        assert tid is not None
        assert state.threads[tid]["current_status"] != "in_progress"

        # Before backoff elapses: sweep refreshes heartbeat and does NOT dispatch
        state.now += timedelta(seconds=10)
        await cr.sweep_capacity_parked(dispatch_mock, stopping=lambda: False)
        assert state.touch_count >= 1
        assert exec_row["heartbeat_at"] == state.now
        dispatch_mock.assert_not_called()

        # After backoff elapses: sweep releases waiting -> pending and dispatches
        state.now += timedelta(seconds=60)
        await cr.sweep_capacity_parked(dispatch_mock, stopping=lambda: False)
        dispatch_mock.assert_called_once()
        assert dispatched[0][1] == _EXEC
        assert exec_row["status"] == "pending"

        # Attempt 2: reuses the exact same execution_id & thread_id and completes
        await executor.execute(dispatched[0][0], dispatched[0][1])

        assert len(state.executions) == 1
        assert exec_row["conversation_thread_id"] == tid
        assert exec_row["conversation_response_id"] is not None
        assert state.automation["failure_count"] == 0
        assert state.threads[tid]["current_status"] == "completed"
        mocks["settle"].assert_not_awaited()
        assert mocks["astream"].call_count == 2


# ---------------------------------------------------------------------------
# 2. Always unavailable -> exceeds 15m cap -> FAILED_OURS + thread status reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_exhausted_past_deadline_settles_failed_ours_and_resets_thread(
    monkeypatch,
):
    """When capacity stays unavailable past the 15-minute cap, the sweep settles as
    FAILED_OURS (server_error, no strike) and guarantees thread status != in_progress."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_execution(_EXEC)

    def _always_exhausted(**kwargs):
        async def _gen():
            tid = kwargs["thread_id"]
            state.threads[tid]["current_status"] = "in_progress"
            raise WriterGuardUnavailable("pinned-session budget exhausted (25 held)")
            yield

        return _gen()

    executor = AutomationExecutor()
    dispatch_mock = create_autospec(
        AutomationScheduler.dispatch,
        spec_set=False,
    )

    with ExitStack() as stack:
        mocks = _bind_executor_env(stack, state, [_always_exhausted])
        await executor.execute(state.automation, _EXEC)

        exec_row = state.executions[_EXEC]
        tid = exec_row["conversation_thread_id"]
        assert exec_row["status"] == "waiting"
        assert exec_row["wait_reason"] == cr.CAPACITY_WAIT_REASON

        # Force thread back to in_progress to prove sweep expiry also resets it
        state.threads[tid]["current_status"] = "in_progress"

        # Advance past 15-minute cap
        state.now += timedelta(seconds=cr.DEFAULT_MAX_WAIT_SECONDS + 1)
        await cr.sweep_capacity_parked(dispatch_mock, stopping=lambda: False)

        dispatch_mock.assert_not_called()
        mocks["settle"].assert_awaited_once()
        assert exec_row["status"] == "failed"
        assert exec_row["failure_reason"] == "server_error"
        assert "WriterGuardUnavailable" in (exec_row["error_message"] or "")
        assert state.automation["failure_count"] == 0
        assert state.threads[tid]["current_status"] == "error"


@pytest.mark.asyncio
async def test_capacity_exhausted_inline_at_deadline_falls_through_to_failed_ours_and_resets_thread(
    monkeypatch,
):
    """If an attempt fails right after awaits_until already passed, park returns False
    so execute() immediately settles as FAILED_OURS and resets thread current_status."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_thread(_THREAD, current_status="in_progress")
    state.seed_execution(
        _EXEC,
        status="pending",
        thread_id=_THREAD,
        awaits_until=state.now - timedelta(seconds=1),
    )

    def _always_exhausted(**kwargs):
        async def _gen():
            state.threads[_THREAD]["current_status"] = "in_progress"
            raise WriterGuardUnavailable("pinned-session budget exhausted (25 held)")
            yield

        return _gen()

    with ExitStack() as stack:
        mocks = _bind_executor_env(stack, state, [_always_exhausted])
        await AutomationExecutor().execute(state.automation, _EXEC)

        mocks["settle"].assert_awaited_once()
        exec_row = state.executions[_EXEC]
        assert exec_row["status"] == "failed"
        assert exec_row["failure_reason"] == "server_error"
        assert state.automation["failure_count"] == 0
        assert state.threads[_THREAD]["current_status"] == "error"


# ---------------------------------------------------------------------------
# 3. Normal (non-capacity) failure also resets thread current_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_non_capacity_failure_resets_thread_status_from_in_progress(
    monkeypatch,
):
    """A normal pre-START RuntimeError settles as FAILED (with strike) and resets
    conversation_threads.current_status away from 'in_progress'."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_execution(_EXEC)

    def _boom(**kwargs):
        async def _gen():
            tid = kwargs["thread_id"]
            state.threads[tid]["current_status"] = "in_progress"
            raise RuntimeError("unexpected pre-START failure")
            yield

        return _gen()

    with ExitStack() as stack:
        mocks = _bind_executor_env(stack, state, [_boom])
        await AutomationExecutor().execute(state.automation, _EXEC)

        mocks["settle"].assert_awaited_once()
        exec_row = state.executions[_EXEC]
        tid = exec_row["conversation_thread_id"]
        assert exec_row["status"] == "failed"
        assert exec_row["failure_reason"] is None
        assert state.automation["failure_count"] == 1
        assert state.threads[tid]["current_status"] == "error"


@pytest.mark.asyncio
async def test_thread_status_reset_never_clobbers_concurrent_live_run():
    """On a continue thread with a live in_progress run, reset leaves current_status='in_progress'."""
    state = _FakeState()
    state.seed_thread(_THREAD, current_status="in_progress")
    state.runs["live-run-1"] = {
        "conversation_response_id": "live-run-1",
        "conversation_thread_id": _THREAD,
        "status": "in_progress",
    }

    with patch.object(
        cr, "get_db_connection", side_effect=state.make_db_connection_cm()
    ):
        updated = await cr.reset_thread_status_after_prestart_exit(
            _THREAD, outcome=Outcome.FAILED
        )

    assert updated is False
    assert state.threads[_THREAD]["current_status"] == "in_progress"


# ---------------------------------------------------------------------------
# 4. Two workers scanning concurrently -> only one retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_workers_scanning_concurrently_only_one_retries(monkeypatch):
    """Two workers sweeping the same due capacity-parked execution race on the atomic
    DB transition waiting -> pending; strictly one worker wins and dispatches."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_execution(
        _EXEC,
        status="waiting",
        thread_id=_THREAD,
        wait_reason=cr.CAPACITY_WAIT_REASON,
        parked_status="active",
        awaits_until=state.now + timedelta(minutes=10),
        delivery={
            cr.CAPACITY_RETRY_KEY: {
                "attempt": 1,
                "next_retry_at": (state.now - timedelta(seconds=5)).isoformat(),
                "last_error": "WriterGuardUnavailable: pinned-session budget exhausted (25 held)",
            }
        },
    )

    worker_a_dispatched: list[str] = []
    worker_b_dispatched: list[str] = []

    def _disp_a(auto: dict[str, Any], eid: str, *, name: str) -> None:
        worker_a_dispatched.append(eid)

    def _disp_b(auto: dict[str, Any], eid: str, *, name: str) -> None:
        worker_b_dispatched.append(eid)

    disp_a = create_autospec(_disp_a, side_effect=_disp_a)
    disp_b = create_autospec(_disp_b, side_effect=_disp_b)

    # Barrier so both workers read the due row BEFORE either attempts the atomic UPDATE
    both_listed = asyncio.Event()
    list_arrivals = [0]
    orig_list = cr.list_capacity_parked_executions

    async def _synchronized_list(limit: int = 50):
        rows = await orig_list(limit=limit)
        list_arrivals[0] += 1
        if list_arrivals[0] >= 2:
            both_listed.set()
        await both_listed.wait()
        return rows

    get_auto_mock = create_autospec(
        auto_db_mod.get_automation,
        side_effect=lambda aid, uid: dict(state.automation),
    )

    with (
        patch.object(cr, "get_db_connection", side_effect=state.make_db_connection_cm()),
        patch.object(cr.auto_db, "get_automation", new=get_auto_mock),
        patch.object(
            cr,
            "list_capacity_parked_executions",
            new=create_autospec(
                cr.list_capacity_parked_executions, side_effect=_synchronized_list
            ),
        ),
    ):
        await asyncio.gather(
            cr.sweep_capacity_parked(disp_a, stopping=lambda: False),
            cr.sweep_capacity_parked(disp_b, stopping=lambda: False),
        )

    assert state.release_calls == 2
    assert len(worker_a_dispatched) + len(worker_b_dispatched) == 1
    assert state.executions[_EXEC]["status"] == "pending"


# ---------------------------------------------------------------------------
# 5. Feature switch DALTON_AUTOMATION_CAPACITY_RETRY (default ON; OFF = legacy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_switch_default_on_and_off_preserves_legacy_behavior(monkeypatch):
    """Default is enabled; when DALTON_AUTOMATION_CAPACITY_RETRY=0, capacity failures
    settle immediately as FAILED_OURS without parking, and sweep is a no-op."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    assert is_automation_capacity_retry_enabled() is True

    monkeypatch.setenv("DALTON_AUTOMATION_CAPACITY_RETRY", "0")
    assert is_automation_capacity_retry_enabled() is False

    state = _FakeState()
    state.seed_execution(_EXEC)

    def _exhausted(**kwargs):
        async def _gen():
            tid = kwargs["thread_id"]
            state.threads[tid]["current_status"] = "in_progress"
            raise WriterGuardUnavailable("pinned-session budget exhausted (25 held)")
            yield

        return _gen()

    with ExitStack() as stack:
        mocks = _bind_executor_env(stack, state, [_exhausted])
        await AutomationExecutor().execute(state.automation, _EXEC)

        mocks["settle"].assert_awaited_once()
        exec_row = state.executions[_EXEC]
        tid = exec_row["conversation_thread_id"]
        assert exec_row["status"] == "failed"
        assert exec_row["failure_reason"] == "server_error"
        assert exec_row["wait_reason"] is None
        assert state.automation["failure_count"] == 0
        # Thread status reset still heals the stuck 'in_progress' bug
        assert state.threads[tid]["current_status"] == "error"

    # Sweep is a complete no-op when switch is OFF
    dispatch_mock = create_autospec(AutomationScheduler.dispatch, spec_set=False)
    with patch.object(
        cr,
        "list_capacity_parked_executions",
        new=create_autospec(cr.list_capacity_parked_executions),
    ) as list_mock:
        await cr.sweep_capacity_parked(dispatch_mock, stopping=lambda: False)
        list_mock.assert_not_awaited()
        dispatch_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Classification, backoff schedule, and paused automation handling
# ---------------------------------------------------------------------------


def test_capacity_error_classification_and_backoff_schedule():
    assert cr.is_capacity_retryable_error(WriterGuardUnavailable("exhausted"))
    assert cr.is_capacity_retryable_error(HTTPException(status_code=503, detail="down"))
    assert cr.is_capacity_retryable_error(
        HTTPException(status_code=429, detail={"type": "burst_limit"})
    )
    assert cr.is_capacity_retryable_error(
        HTTPException(status_code=429, detail={"type": "service_unavailable"})
    )
    # User usage limit 429 and normal errors are NOT retried as capacity
    assert not cr.is_capacity_retryable_error(
        HTTPException(status_code=429, detail={"type": "credit_limit"})
    )
    assert not cr.is_capacity_retryable_error(RuntimeError("boom"))

    # Deterministic backoff check (jitter=1.0)
    assert cr.compute_backoff_seconds(1, jitter_fn=lambda a, b: 1.0) == 30.0
    assert cr.compute_backoff_seconds(2, jitter_fn=lambda a, b: 1.0) == 60.0
    assert cr.compute_backoff_seconds(3, jitter_fn=lambda a, b: 1.0) == 120.0
    assert cr.compute_backoff_seconds(4, jitter_fn=lambda a, b: 1.0) == 240.0
    assert cr.compute_backoff_seconds(5, jitter_fn=lambda a, b: 1.0) == 240.0
    # Jitter stays within [0.85x, 1.25x]
    jittered = cr.compute_backoff_seconds(1)
    assert 25.5 <= jittered <= 37.5


@pytest.mark.asyncio
async def test_credit_gate_503_before_thread_creation_parks_and_retries(monkeypatch):
    """Credit-gate 503 / burst 429 happens before thread resolution; parks cleanly with
    thread_id=None and creates the thread and completes on retry."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_execution(_EXEC)

    credit_calls = [0]

    async def _credit_flaky(user_id: str, *, byok: bool = False):
        credit_calls[0] += 1
        if credit_calls[0] == 1:
            raise HTTPException(
                status_code=503,
                detail={"type": "service_unavailable", "message": "Credit gate 503"},
            )

    def _turn_ok(**kwargs):
        async def _gen():
            tid = kwargs["thread_id"]
            rid = kwargs["run_id"]
            state.runs[rid] = {
                "conversation_response_id": rid,
                "conversation_thread_id": tid,
                "status": "completed",
                "metadata": {},
            }
            state.threads[tid]["current_status"] = "completed"
            yield "event: message_chunk\ndata: {}\n\n"

        return _gen()

    dispatched: list[tuple[dict[str, Any], str]] = []

    def _disp(auto: dict[str, Any], eid: str, *, name: str) -> None:
        dispatched.append((auto, eid))

    disp_mock = create_autospec(_disp, side_effect=_disp)

    with ExitStack() as stack:
        mocks = _bind_executor_env(
            stack, state, [_turn_ok], credit_side_effect=_credit_flaky
        )
        await AutomationExecutor().execute(state.automation, _EXEC)

        exec_row = state.executions[_EXEC]
        assert exec_row["status"] == "waiting"
        assert exec_row["wait_reason"] == cr.CAPACITY_WAIT_REASON
        assert exec_row["conversation_thread_id"] is None
        mocks["astream"].assert_not_called()

        state.now += timedelta(seconds=60)
        await cr.sweep_capacity_parked(disp_mock, stopping=lambda: False)
        disp_mock.assert_called_once()

        await AutomationExecutor().execute(dispatched[0][0], dispatched[0][1])
        assert mocks["astream"].call_count == 1
        assert state.automation["failure_count"] == 0
        mocks["settle"].assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_automation_while_capacity_parked_is_skipped_and_resets_thread(
    monkeypatch,
):
    """An automation paused while parked for capacity settles as SKIPPED (user) and
    resets any stuck thread status."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_thread(_THREAD, current_status="in_progress")
    state.seed_execution(
        _EXEC,
        status="waiting",
        thread_id=_THREAD,
        wait_reason=cr.CAPACITY_WAIT_REASON,
        parked_status="active",
        awaits_until=state.now + timedelta(minutes=10),
        delivery={
            cr.CAPACITY_RETRY_KEY: {
                "attempt": 1,
                "next_retry_at": (state.now - timedelta(seconds=5)).isoformat(),
                "last_error": "WriterGuardUnavailable: budget",
            }
        },
    )
    state.automation["status"] = "paused"

    disp_mock = create_autospec(AutomationScheduler.dispatch, spec_set=False)
    with ExitStack() as stack:
        mocks = _bind_executor_env(stack, state, [])
        await cr.sweep_capacity_parked(disp_mock, stopping=lambda: False)

        disp_mock.assert_not_called()
        mocks["settle"].assert_awaited_once()
        assert state.executions[_EXEC]["status"] == "skipped"
        assert state.executions[_EXEC]["skip_reason"] == "user"
        assert state.threads[_THREAD]["current_status"] == "completed"


@pytest.mark.asyncio
async def test_prestart_cancelled_error_resets_thread_status_from_in_progress(
    monkeypatch,
):
    """Cancellation before START settles as INTERRUPTED and resets thread current_status."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    state = _FakeState()
    state.seed_execution(_EXEC)

    def _cancelled_turn(**kwargs):
        async def _gen():
            tid = kwargs["thread_id"]
            state.threads[tid]["current_status"] = "in_progress"
            raise asyncio.CancelledError()
            yield

        return _gen()

    with ExitStack() as stack:
        mocks = _bind_executor_env(stack, state, [_cancelled_turn])
        with pytest.raises(asyncio.CancelledError):
            await AutomationExecutor().execute(state.automation, _EXEC)

        mocks["settle"].assert_awaited_once()
        exec_row = state.executions[_EXEC]
        tid = exec_row["conversation_thread_id"]
        assert exec_row["status"] == "failed"
        assert exec_row["failure_reason"] == "interrupted"
        assert state.threads[tid]["current_status"] == "cancelled"

