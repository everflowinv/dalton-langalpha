"""Unit tests for automation capacity backoff retry, delivery reconciliation,
and atomic pre-START thread status reset.

Design rules enforced here:
- Every test double uses ``create_autospec`` (no bare ``patch(..., return_value=...)``
  without signature validation).
- Test doubles do NOT re-implement PostgreSQL atomic conditional UPDATE semantics;
  real PostgreSQL concurrency, lock ordering, and SQL atomicity live in
  ``tests/integration/dalton/test_automation_capacity_retry_db.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import create_autospec, patch

import pytest
from fastapi import HTTPException
from psycopg import AsyncConnection, AsyncCursor

from src.dalton.automations import capacity_retry as cr
from src.dalton.autonomy import preflight as preflight_mod
from src.dalton.database import automation_recycle as recycle_mod
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import watch as watch_db
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
from src.server.services import automation_delivery as delivery_mod
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
_RUN = "00000000-0000-0000-0000-000000000404"
_HOME = get_home_workspace_id(_USER)
_NOW = datetime(2026, 11, 28, 3, 0, 0, tzinfo=timezone.utc)


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
        "delivery_config": None,
        "status": "active",
        "failure_count": 0,
        **overrides,
    }


def _make_recording_db(
    *,
    fetchone_results: Optional[list[Any]] = None,
    fetchall_results: Optional[list[list[Any]]] = None,
    rowcounts: Optional[list[int]] = None,
):
    """Build a ``create_autospec(AsyncConnection)`` DB context manager that records SQL."""
    executed: list[tuple[str, Any]] = []
    one_iter = iter(fetchone_results or [])
    all_iter = iter(fetchall_results or [])
    rc_iter = iter(rowcounts or [])

    @asynccontextmanager
    async def _get_conn():
        conn = create_autospec(AsyncConnection, instance=True)
        cur = create_autospec(AsyncCursor, instance=True)
        cur.rowcount = 0

        async def _execute(sql: str, params: Any = None) -> None:
            executed.append((" ".join(sql.split()), params))
            try:
                cur.rowcount = next(rc_iter)
            except StopIteration:
                cur.rowcount = 1

        async def _fetchone() -> Any:
            try:
                return next(one_iter)
            except StopIteration:
                return None

        async def _fetchall() -> list[Any]:
            try:
                return next(all_iter)
            except StopIteration:
                return []

        cur.execute.side_effect = _execute
        cur.fetchone.side_effect = _fetchone
        cur.fetchall.side_effect = _fetchall

        @asynccontextmanager
        async def _cursor_cm(*_a: Any, **_kw: Any):
            yield cur

        @asynccontextmanager
        async def _tx_cm(*_a: Any, **_kw: Any):
            yield conn

        conn.cursor.side_effect = _cursor_cm
        conn.transaction.side_effect = _tx_cm
        yield conn

    return _get_conn, executed


def _make_tx_conn(
    *,
    fetchone_results: Optional[list[Any]] = None,
    rowcounts: Optional[list[int]] = None,
):
    """Build a single ``create_autospec(AsyncConnection)`` for in-transaction callbacks."""
    executed: list[tuple[str, Any]] = []
    one_iter = iter(fetchone_results or [])
    rc_iter = iter(rowcounts or [])

    conn = create_autospec(AsyncConnection, instance=True)
    cur = create_autospec(AsyncCursor, instance=True)
    cur.rowcount = 0

    async def _execute(sql: str, params: Any = None) -> None:
        executed.append((" ".join(sql.split()), params))
        try:
            cur.rowcount = next(rc_iter)
        except StopIteration:
            cur.rowcount = 1

    async def _fetchone() -> Any:
        try:
            return next(one_iter)
        except StopIteration:
            return None

    cur.execute.side_effect = _execute
    cur.fetchone.side_effect = _fetchone

    @asynccontextmanager
    async def _cursor_cm(*_a: Any, **_kw: Any):
        yield cur

    @asynccontextmanager
    async def _tx_cm(*_a: Any, **_kw: Any):
        yield conn

    conn.cursor.side_effect = _cursor_cm
    conn.transaction.side_effect = _tx_cm
    return conn, executed


def _make_dispatch_mock(*, side_effect: Any = None):
    sched = create_autospec(AutomationScheduler, instance=True)
    if side_effect is not None:
        sched.dispatch.side_effect = side_effect
    return sched.dispatch


def _make_webhook_client_cls(fire_event_return: Any = None):
    client = create_autospec(WebhookClient, instance=True)
    client.fire_event.return_value = fire_event_return
    cls_mock = create_autospec(WebhookClient, return_value=client)
    return cls_mock, client


def _bind_executor_mocks(
    stack: ExitStack,
    *,
    automation: dict[str, Any],
    turns: list[Any],
    execution_row: Optional[dict[str, Any]] = None,
    credit_side_effect: Any = None,
    park_side_effect: Any = None,
    settle_side_effect: Any = None,
    start_run_side_effect: Any = None,
    revoke_side_effect: Any = None,
    is_dalton: bool = False,
) -> dict[str, Any]:
    """Wire AutomationExecutor with 100% ``create_autospec`` doubles."""
    call_idx = [0]
    row_state = dict(
        execution_row
        or {
            "automation_execution_id": _EXEC,
            "automation_id": automation["automation_id"],
            "status": "pending",
            "conversation_thread_id": None,
            "conversation_response_id": None,
            "scheduled_at": _NOW,
            "wait_reason": None,
            "awaits_computer": None,
            "checked_computer_id": None,
            "delivery": None,
        }
    )

    async def _begin_exec(execution_id: str, *, started_at: datetime):
        row_state["status"] = "running"
        row_state["started_at"] = started_at
        return {
            "conversation_thread_id": row_state.get("conversation_thread_id"),
            "conversation_response_id": row_state.get("conversation_response_id"),
            "scheduled_at": row_state.get("scheduled_at"),
            "wait_reason": row_state.get("wait_reason"),
            "awaits_computer": row_state.get("awaits_computer"),
            "checked_computer_id": row_state.get("checked_computer_id"),
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
        row_state["status"] = to
        if wait_reason is not None:
            row_state["wait_reason"] = wait_reason
        for k, v in fields.items():
            if v is not None:
                row_state[k] = v
        return dict(row_state)

    async def _stamp_delivery(execution_id: str, stamp: Optional[dict[str, Any]]) -> bool:
        existing = dict(row_state.get("delivery") or {})
        preserved = {
            k: v
            for k, v in existing.items()
            if k in ("subscription_fallback", "capacity_retry", "delivery_meta")
        }
        if stamp is not None:
            row_state["delivery"] = {**stamp, **preserved}
        else:
            row_state["delivery"] = preserved or None
        return True

    async def _persist_meta(
        execution_id: str, *, entries: list[str], starts: int, registered: bool
    ) -> None:
        existing = dict(row_state.get("delivery") or {})
        existing[cr.DELIVERY_META_KEY] = {
            "entries": list(entries),
            "starts": starts,
            "registered": registered,
        }
        row_state["delivery"] = existing

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
    stamp_mock = create_autospec(
        exec_db_mod.stamp_delivery, side_effect=_stamp_delivery
    )
    get_exec_mock = create_autospec(
        exec_db_mod.get_execution_for_settlement,
        side_effect=lambda eid: dict(row_state),
    )
    get_auto_mock = create_autospec(
        auto_db_mod.get_automation,
        side_effect=lambda aid, uid: dict(automation),
    )
    credit_mock = create_autospec(enforce_credit_limit, side_effect=credit_side_effect)
    astream_mock = create_autospec(astream_ptc_workflow, side_effect=_astream)
    settle_mock = create_autospec(
        settle_mod.settle,
        side_effect=settle_side_effect if settle_side_effect is not None else (lambda *a, **kw: True),
    )
    park_mock = create_autospec(
        cr.try_park_for_capacity,
        side_effect=park_side_effect,
    ) if park_side_effect is not None else None
    start_run_mock = create_autospec(
        delivery_mod.start_run,
        side_effect=start_run_side_effect,
        return_value=None,
    )
    revoke_mock = create_autospec(
        cr._revoke_prior_channel_registration,
        side_effect=revoke_side_effect,
        return_value=None,
    )
    persist_meta_mock = create_autospec(
        cr._persist_delivery_meta,
        side_effect=_persist_meta,
    )
    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    ws_mgr.ensure_home_bound.return_value = None
    ws_mgr_get = create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr)
    webhook_cls, webhook_client = _make_webhook_client_cls()
    is_dalton_mock = create_autospec(
        preflight_mod.is_dalton_automation, return_value=is_dalton
    )

    get_run_mock = create_autospec(tl_db_mod.get_run, return_value=None)
    patches = [
        patch.object(exec_mod.exec_db, "begin_execution_if_not_deleted", new=begin_mock),
        patch.object(exec_mod.exec_db, "transition_execution", new=trans_mock),
        patch.object(exec_mod.exec_db, "get_execution_for_settlement", new=get_exec_mock),
        patch.object(cr.exec_db, "get_execution_for_settlement", new=get_exec_mock),
        patch.object(exec_mod.exec_db, "stamp_delivery", new=stamp_mock),
        patch.object(exec_mod.exec_db, "touch_execution", new=create_autospec(exec_db_mod.touch_execution, return_value=None)),
        patch.object(exec_mod, "settle", new=settle_mock),
        patch.object(exec_mod.auto_db, "get_automation", new=get_auto_mock),
        patch.object(recycle_mod, "automation_is_soft_deleted", new=create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False)),
        patch.object(preflight_mod, "is_dalton_automation", new=is_dalton_mock),
        patch("src.server.database.api_keys.is_byok_active", new=create_autospec(is_byok_active, return_value=False)),
        patch("src.server.database.oauth_tokens.has_any_oauth_token", new=create_autospec(has_any_oauth_token, return_value=False)),
        patch.object(exec_mod, "enforce_credit_limit", new=credit_mock),
        patch.object(ws_db_mod, "get_workspace", new=create_autospec(ws_db_mod.get_workspace, return_value={"workspace_id": _HOME, "user_id": _USER, "status": "running"})),
        patch.object(conv_db_mod, "create_thread", new=create_autospec(conv_db_mod.create_thread, return_value=_THREAD)),
        patch.object(conv_db_mod, "get_thread_auth_meta", new=create_autospec(conv_db_mod.get_thread_auth_meta, return_value={"workspace_id": _HOME, "user_id": _USER})),
        patch.object(WorkspaceManager, "get_instance", new=ws_mgr_get),
        patch.object(ssh_computers, "computer_away", new=create_autospec(ssh_computers.computer_away, return_value=None)),
        patch.object(home_placement, "place_home_turn", new=create_autospec(home_placement.place_home_turn, return_value=None)),
        patch.object(exec_mod, "WebhookClient", new=webhook_cls),
        patch.object(exec_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)),
        patch.object(tl_db_mod, "get_run", new=get_run_mock),
        patch.object(tl_db_mod, "get_latest_attempt", new=create_autospec(tl_db_mod.get_latest_attempt, return_value=None)),
        patch("src.server.handlers.chat.astream_ptc_workflow", new=astream_mock),
        patch.object(delivery_mod, "start_run", new=start_run_mock),
        patch.object(cr, "_revoke_prior_channel_registration", new=revoke_mock),
        patch.object(cr, "_persist_delivery_meta", new=persist_meta_mock),
    ]
    if park_mock is not None:
        patches.append(patch.object(cr, "try_park_for_capacity", new=park_mock))

    for ctx in patches:
        stack.enter_context(ctx)

    return {
        "row": row_state,
        "begin": begin_mock,
        "transition": trans_mock,
        "stamp": stamp_mock,
        "settle": settle_mock,
        "credit": credit_mock,
        "astream": astream_mock,
        "start_run": start_run_mock,
        "revoke": revoke_mock,
        "park": park_mock,
        "webhook": webhook_client,
        "get_run": get_run_mock,
    }


# ---------------------------------------------------------------------------
# 1. Isolated deadline: NEVER reads or writes awaits_until
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_retry_sql_never_reads_or_writes_awaits_until() -> None:
    """All capacity-retry SQL statements operate exclusively on delivery.capacity_retry
    and never touch the awaits_until column reserved for computer-away waits."""
    get_conn, executed = _make_recording_db(
        fetchone_results=[
            {
                "status": "running",
                "parked_status": None,
                "delivery": None,
                "conversation_thread_id": _THREAD,
                "db_now": _NOW,
            },
            {"current_status": "in_progress"},
        ],
        fetchall_results=[[]],
        rowcounts=[1, 1, 1, 1, 1, 1, 1],
    )
    with patch.object(cr, "get_db_connection", side_effect=get_conn):
        parked, tid = await cr.park_execution_for_capacity(
            _EXEC, parked_status="active", error_message="WriterGuardUnavailable: full"
        )
        await cr.touch_capacity_parked_executions()
        await cr.list_capacity_parked_executions()
        await cr.release_capacity_parked_execution(_EXEC)

    assert parked is True
    assert tid == _THREAD
    assert len(executed) >= 6
    for sql, _params in executed:
        assert "awaits_until" not in sql.lower(), f"SQL touched awaits_until: {sql}"

    # Verify deadline_at was written inside delivery.capacity_retry
    park_update = next(
        (params for sql, params in executed if sql.startswith("UPDATE automation_executions SET status = 'waiting'")),
        None,
    )
    assert park_update is not None
    payload = park_update["retry_payload"].obj
    assert payload["deadline_at"] == (_NOW + timedelta(seconds=cr.DEFAULT_MAX_WAIT_SECONDS)).isoformat()
    assert payload["attempt"] == 1


@pytest.mark.asyncio
async def test_park_keeps_original_deadline_at_and_refuses_once_expired() -> None:
    """Subsequent capacity parks keep the original deadline_at and return False once reached."""
    original_deadline = _NOW + timedelta(minutes=15)
    # 1) Second park before deadline keeps original deadline_at unchanged
    get_conn_ok, exec_ok = _make_recording_db(
        fetchone_results=[
            {
                "status": "running",
                "parked_status": "active",
                "delivery": {
                    cr.CAPACITY_RETRY_KEY: {
                        "attempt": 1,
                        "first_parked_at": _NOW.isoformat(),
                        "deadline_at": original_deadline.isoformat(),
                    }
                },
                "conversation_thread_id": None,
                "db_now": _NOW + timedelta(minutes=2),
            }
        ],
        rowcounts=[1, 1],
    )
    with patch.object(cr, "get_db_connection", side_effect=get_conn_ok):
        parked, _ = await cr.park_execution_for_capacity(
            _EXEC, parked_status="active", error_message="WriterGuardUnavailable: full"
        )
    assert parked is True
    payload = exec_ok[1][1]["retry_payload"].obj
    assert payload["attempt"] == 2
    assert payload["deadline_at"] == original_deadline.isoformat()

    # 2) Once db_now >= deadline_at, returns False without updating
    get_conn_exp, exec_exp = _make_recording_db(
        fetchone_results=[
            {
                "status": "running",
                "parked_status": "active",
                "delivery": {
                    cr.CAPACITY_RETRY_KEY: {
                        "attempt": 4,
                        "first_parked_at": _NOW.isoformat(),
                        "deadline_at": original_deadline.isoformat(),
                    }
                },
                "conversation_thread_id": _THREAD,
                "db_now": original_deadline + timedelta(seconds=1),
            }
        ]
    )
    with patch.object(cr, "get_db_connection", side_effect=get_conn_exp):
        parked_exp, tid_exp = await cr.park_execution_for_capacity(
            _EXEC, parked_status="active", error_message="WriterGuardUnavailable: full"
        )
    assert parked_exp is False
    assert tid_exp == _THREAD
    assert len(exec_exp) == 1  # SELECT only, no UPDATE


# ---------------------------------------------------------------------------
# 2. Claiming & dispatch safety (dispatch lease release on dispatch failure)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_only_dispatches_when_claim_returns_true_and_releases_lease_on_error() -> None:
    """When two workers sweep the same row, only the worker whose DB claim returns True
    dispatches; if dispatch raises synchronously, the dispatch lease is released immediately."""
    row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "waiting",
        "parked_status": "active",
        "conversation_thread_id": _THREAD,
        "delivery": {cr.CAPACITY_RETRY_KEY: {"attempt": 1, "last_error": "WriterGuardUnavailable"}},
        "user_id": _USER,
        "workspace_id": _HOME,
        "expired": False,
        "due": True,
    }
    auto = _automation()

    # Worker A wins claim (returns token), Worker B loses claim (None)
    disp_a = _make_dispatch_mock()
    disp_b = _make_dispatch_mock()
    claim_results = iter(["tok-worker-a", None])
    claim_mock = create_autospec(
        cr.release_capacity_parked_execution, side_effect=lambda eid: next(claim_results)
    )
    get_auto_mock = create_autospec(auto_db_mod.get_automation, return_value=auto)

    with (
        patch.object(cr.auto_db, "get_automation", new=get_auto_mock),
        patch.object(cr, "release_capacity_parked_execution", new=claim_mock),
    ):
        await cr.sweep_one_capacity_parked(row, disp_a, stopping=lambda: False)
        await cr.sweep_one_capacity_parked(row, disp_b, stopping=lambda: False)

    disp_a.assert_called_once()
    assert disp_a.call_args.kwargs["claim_token"] == "tok-worker-a"
    disp_b.assert_not_called()

    # If dispatch fails after claim, release_dispatch_lease is called with the claim_token
    failing_disp = _make_dispatch_mock(side_effect=RuntimeError("loop closing"))
    release_lease_mock = create_autospec(cr.release_dispatch_lease, return_value=True)
    claim_win = create_autospec(
        cr.release_capacity_parked_execution, return_value="tok-fail-dispatch"
    )
    with (
        patch.object(cr.auto_db, "get_automation", new=get_auto_mock),
        patch.object(cr, "release_capacity_parked_execution", new=claim_win),
        patch.object(cr, "release_dispatch_lease", new=release_lease_mock),
    ):
        with pytest.raises(RuntimeError, match="loop closing"):
            await cr.sweep_one_capacity_parked(row, failing_disp, stopping=lambda: False)

    release_lease_mock.assert_awaited_once_with(_EXEC, claim_token="tok-fail-dispatch")


# ---------------------------------------------------------------------------
# 3. Atomic thread status reset: lock thread row FOR UPDATE first, then UPDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thread_status_reset_locks_thread_for_update_before_checking_responses() -> None:
    """Thread reset executes SELECT ... FOR UPDATE first, then UPDATE checking
    conversation_responses on a fresh statement snapshot in the same transaction."""
    conn, executed = _make_tx_conn(
        fetchone_results=[{"current_status": "in_progress"}],
        rowcounts=[1, 1],
    )
    updated = await cr.reset_thread_status_after_prestart_exit(
        _THREAD, outcome=Outcome.FAILED_OURS, conn=conn
    )
    assert updated is True
    assert len(executed) == 2
    assert "FROM conversation_threads" in executed[0][0]
    assert "FOR UPDATE" in executed[0][0]
    assert executed[1][0].startswith("UPDATE conversation_threads ct")
    assert "NOT EXISTS" in executed[1][0]
    assert executed[1][1] == ("error", _THREAD)


@pytest.mark.asyncio
async def test_executor_failure_and_cancellation_pass_reset_in_settlement_transaction(
    monkeypatch,
) -> None:
    """Both normal pre-START failure and CancelledError pass reset_prestart_thread_status=True
    into settle() so failure settlement and thread reset commit in one transaction."""
    monkeypatch.setenv("DALTON_AUTOMATION_CAPACITY_RETRY", "0")
    auto = _automation()

    def _boom(**kwargs):
        async def _gen():
            raise RuntimeError("pre-start boom")
            yield

        return _gen()

    with ExitStack() as stack:
        mocks = _bind_executor_mocks(stack, automation=auto, turns=[_boom])
        await AutomationExecutor().execute(auto, _EXEC)
        mocks["settle"].assert_awaited_once()
        kw = mocks["settle"].await_args.kwargs
        assert kw["reset_prestart_thread_status"] is True
        assert kw["release_pre_start_signals"] is True

    def _cancelled(**kwargs):
        async def _gen():
            raise asyncio.CancelledError()
            yield

        return _gen()

    with ExitStack() as stack:
        mocks = _bind_executor_mocks(stack, automation=auto, turns=[_cancelled])
        with pytest.raises(asyncio.CancelledError):
            await AutomationExecutor().execute(auto, _EXEC)
        mocks["settle"].assert_awaited_once()
        kw = mocks["settle"].await_args.kwargs
        assert mocks["settle"].await_args.args[2] is Outcome.INTERRUPTED
        assert kw["reset_prestart_thread_status"] is True


@pytest.mark.asyncio
async def test_settle_runs_thread_reset_inside_in_transaction_and_propagates_failure() -> None:
    """settle(..., reset_prestart_thread_status=True) runs thread reset inside
    settle_execution's transaction callback on the same connection."""
    tx_conn, executed = _make_tx_conn(
        fetchone_results=[{"current_status": "in_progress"}],
        rowcounts=[1, 1],
    )
    settled_row = {
        "conversation_thread_id": _THREAD,
        "conversation_response_id": None,
        "settled_from": "running",
        "delivery": None,
        "previous_failure_reason": None,
        "previous_delivery_result": None,
    }

    async def _settle_exec(_eid, *, in_transaction=None, **_fields):
        if in_transaction is not None:
            await in_transaction(tx_conn, settled_row)
        return settled_row

    webhook_cls, _ = _make_webhook_client_cls()
    with (
        patch.object(
            settle_mod.exec_db,
            "settle_execution",
            new=create_autospec(exec_db_mod.settle_execution, side_effect=_settle_exec),
        ),
        patch.object(settle_mod, "WebhookClient", new=webhook_cls),
        patch.object(
            recycle_mod,
            "automation_is_soft_deleted",
            new=create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False),
        ),
    ):
        ok = await settle_mod.settle(
            _automation(),
            _EXEC,
            Outcome.FAILED_OURS,
            thread_id=_THREAD,
            reset_prestart_thread_status=True,
        )
        await settle_mod.drain_tails()

    assert ok is True
    assert len(executed) == 2
    assert "FOR UPDATE" in executed[0][0]
    assert executed[1][0].startswith("UPDATE conversation_threads ct")


# ---------------------------------------------------------------------------
# 4. Retry succeeds -> execution settles to completed with a single delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_succeeds_execution_completes_and_delivers_once(monkeypatch) -> None:
    """Attempt 1 hits WriterGuardUnavailable -> parked; Attempt 2 links run and
    settle_by_run settles execution to status='completed' with exactly one delivery."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    auto = _automation(delivery_config={"methods": ["slack:T/C1"]})
    held_delivery = delivery_mod.Delivery(
        id=_EXEC,
        targets=[delivery_mod.Target(entry="slack:T/C1", address="slack:T/C1", name="#alerts", ok=True)],
    )

    def _turn_1_fails(**kwargs):
        async def _gen():
            raise WriterGuardUnavailable("pinned-session budget exhausted (25 held)")
            yield

        return _gen()

    def _turn_2_ok(**kwargs):
        async def _gen():
            yield "event: message_chunk\ndata: {}\n\n"

        return _gen()

    park_calls: list[str] = []

    async def _park_ok(exc, *, automation, execution_id, thread_id=None):
        park_calls.append(execution_id)
        return True

    with ExitStack() as stack:
        mocks = _bind_executor_mocks(
            stack,
            automation=auto,
            turns=[_turn_1_fails, _turn_2_ok],
            park_side_effect=_park_ok,
            start_run_side_effect=lambda *a, **kw: held_delivery,
        )
        # Attempt 1: registers delivery, hits WriterGuardUnavailable, parks without settling
        await AutomationExecutor().execute(auto, _EXEC)
        assert park_calls == [_EXEC]
        mocks["settle"].assert_not_awaited()
        assert mocks["start_run"].await_count == 1

        # Attempt 2: hydrates persisted delivery (no duplicate start_run!), links run
        mocks["row"]["status"] = "pending"
        run_row = {
            "conversation_response_id": _RUN,
            "conversation_thread_id": _THREAD,
            "status": "completed",
            "metadata": delivery_mod.run_metadata(held_delivery),
        }
        mocks["get_run"].return_value = run_row
        await AutomationExecutor().execute(auto, _EXEC)
        assert mocks["start_run"].await_count == 1  # unchanged recipients -> NOT re-registered
        assert mocks["row"]["conversation_response_id"] is not None

    # Now drive post-run settlement (settle_by_run) and assert status='completed' & 1 delivery
    finish_mock = create_autospec(
        delivery_mod.finish_run,
        return_value=delivery_mod.Finish(
            [{"method": "slack:T/C1", "address": "slack:T/C1", "name": "#alerts", "success": True, "via": "fallback", "error": None}],
            answered=True,
        ),
    )
    final_status_captured: list[str] = []

    async def _settle_complete(eid, *, to, **kw):
        final_status_captured.append(to)
        return {
            "conversation_thread_id": _THREAD,
            "conversation_response_id": _RUN,
            "settled_from": "running",
            "delivery": delivery_mod.stamp(held_delivery),
            "previous_failure_reason": None,
            "previous_delivery_result": None,
        }

    with (
        patch.object(
            settle_mod.exec_db,
            "settle_execution",
            new=create_autospec(exec_db_mod.settle_execution, side_effect=_settle_complete),
        ),
        patch.object(
            settle_mod.exec_db,
            "record_delivery",
            new=create_autospec(exec_db_mod.record_delivery, return_value=None),
        ),
        patch.object(
            settle_mod,
            "read_run_answer",
            new=create_autospec(
                settle_mod.read_run_answer,
                return_value=settle_mod.RunAnswer(text="Delta complete"),
            ),
        ),
        patch.object(delivery_mod, "finish_run", new=finish_mock),
        patch.object(
            recycle_mod,
            "automation_is_soft_deleted",
            new=create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False),
        ),
        patch(
            "src.dalton.notify.dispatch.on_automation_settled",
            new=create_autospec(
                __import__("src.dalton.notify.dispatch", fromlist=["on_automation_settled"]).on_automation_settled,
                return_value=None,
            ),
        ),
    ):
        outcome = await settle_mod.settle_by_run(
            auto,
            _EXEC,
            _RUN,
            run_row,
            thread_id=_THREAD,
            workspace_id=_HOME,
        )
        await settle_mod.drain_tails()

    assert outcome is Outcome.COMPLETED
    assert final_status_captured == ["completed"]
    finish_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. Switch OFF stops new parks AND drains already-waiting executions to FAILED_OURS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_off_stops_new_parks_and_drains_existing_waiting_as_failed_ours(
    monkeypatch,
) -> None:
    """When DALTON_AUTOMATION_CAPACITY_RETRY=0, new capacity failures do not park, and
    sweep_capacity_parked drains all remaining capacity_limit rows to FAILED_OURS."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    assert is_automation_capacity_retry_enabled() is True

    monkeypatch.setenv("DALTON_AUTOMATION_CAPACITY_RETRY", "0")
    assert is_automation_capacity_retry_enabled() is False

    # 1) New capacity error does NOT park
    assert (
        await cr.try_park_for_capacity(
            WriterGuardUnavailable("exhausted"),
            automation=_automation(),
            execution_id=_EXEC,
            thread_id=_THREAD,
        )
        is False
    )

    # 2) Existing waiting execution IS drained to FAILED_OURS (not left stranded in waiting)
    waiting_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "waiting",
        "parked_status": "active",
        "conversation_thread_id": _THREAD,
        "delivery": {
            cr.CAPACITY_RETRY_KEY: {
                "attempt": 1,
                "next_retry_at": (_NOW + timedelta(minutes=2)).isoformat(),
                "deadline_at": (_NOW + timedelta(minutes=14)).isoformat(),
                "last_error": "WriterGuardUnavailable: exhausted",
            }
        },
        "user_id": _USER,
        "workspace_id": _HOME,
        "expired": False,
        "due": False,
    }
    list_mock = create_autospec(
        cr.list_capacity_parked_executions, return_value=[waiting_row]
    )
    touch_mock = create_autospec(cr.touch_capacity_parked_executions, return_value=0)
    get_auto_mock = create_autospec(auto_db_mod.get_automation, return_value=_automation())
    settle_mock = create_autospec(cr.settle, return_value=True)
    disp_mock = _make_dispatch_mock()

    drain_notify_mock = create_autospec(
        cr.drain_pending_failure_notifications, return_value=0
    )
    with (
        patch.object(cr, "list_capacity_parked_executions", new=list_mock),
        patch.object(cr, "touch_capacity_parked_executions", new=touch_mock),
        patch.object(cr.auto_db, "get_automation", new=get_auto_mock),
        patch.object(cr, "settle", new=settle_mock),
        patch.object(cr, "drain_pending_failure_notifications", new=drain_notify_mock),
    ):
        await cr.sweep_capacity_parked(disp_mock, stopping=lambda: False)

    touch_mock.assert_not_awaited()
    list_mock.assert_awaited_once_with(drain_all=True)
    disp_mock.assert_not_called()
    settle_mock.assert_awaited_once()
    assert settle_mock.await_args.args[2] is Outcome.FAILED_OURS
    kw = settle_mock.await_args.kwargs
    assert kw["from_statuses"] == ("waiting", "pending")
    assert kw["expected_wait_reason"] == cr.CAPACITY_WAIT_REASON
    assert kw["expected_claim_token"] is None
    assert kw["require_capacity_expired"] is False
    assert kw["release_pre_start_signals"] is True
    assert kw["reset_prestart_thread_status"] is True
    assert kw["enqueue_failure_notify"] is True
    drain_notify_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# 6. Expiry sweep enqueues durable failure notify in-tx & delivers once on winner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiry_sweep_initializes_delivery_only_on_winning_worker() -> None:
    """When credit-gate 503 parked a threadless execution before delivery start,
    two workers racing to settle at expiry enqueue the durable failure-notify
    task strictly once in the winning settlement transaction and deliver once."""
    auto = _automation(delivery_config={"methods": ["slack:T/C1"]})
    expired_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "waiting",
        "parked_status": "active",
        "conversation_thread_id": None,
        "delivery": {
            cr.CAPACITY_RETRY_KEY: {
                "attempt": 5,
                "last_error": "HTTPException: Credit gate 503",
            }
        },
        "user_id": _USER,
        "workspace_id": _HOME,
        "expired": True,
        "due": True,
    }
    settled_return = {
        "conversation_thread_id": None,
        "conversation_response_id": None,
        "settled_from": "waiting",
        "delivery": expired_row["delivery"],
        "previous_failure_reason": None,
        "previous_delivery_result": None,
    }
    tx_conn, tx_executed = _make_tx_conn(rowcounts=[1, 1, 1])
    won_once = [False]

    async def _settle_exec_race(_eid, *, in_transaction=None, **kw):
        if won_once[0]:
            return None
        won_once[0] = True
        if in_transaction is not None:
            await in_transaction(tx_conn, settled_return)
        return settled_return

    settle_exec_mock = create_autospec(
        exec_db_mod.settle_execution,
        side_effect=_settle_exec_race,
    )
    resolved_delivery = delivery_mod.Delivery(
        id=_EXEC,
        targets=[delivery_mod.Target(entry="slack:T/C1", address="slack:T/C1", name="#ops", ok=True)],
    )
    start_run_mock = create_autospec(delivery_mod.start_run, return_value=resolved_delivery)
    finish_run_mock = create_autospec(
        delivery_mod.finish_run,
        return_value=delivery_mod.Finish(
            [{"method": "slack:T/C1", "address": "slack:T/C1", "name": "#ops", "success": True, "via": "notice", "error": None}],
            answered=True,
        ),
    )
    claimed_row = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "conversation_thread_id": None,
        "conversation_response_id": None,
        "error_message": "HTTPException: Credit gate 503",
        "delivery": {
            **expired_row["delivery"],
            cr.FAILURE_NOTIFY_KEY: {
                "status": "claimed",
                "claim_token": "fn-tok-1",
                "execution_id": _EXEC,
                "automation_id": _AUTO,
                "user_id": _USER,
                "thread_id": None,
                "workspace_id": _HOME,
                "outcome": Outcome.FAILED_OURS.value,
                "error": "HTTPException: Credit gate 503",
                "attempts": 1,
                "recorded": False,
                "answered": False,
            },
        },
    }
    claim_fn_results = iter([claimed_row, None])
    claim_fn_mock = create_autospec(
        cr.claim_failure_notification,
        side_effect=lambda eid, **kw: next(claim_fn_results),
    )
    update_fn_mock = create_autospec(
        cr._update_failure_notification_state, return_value=True
    )
    disp_mock = _make_dispatch_mock()
    webhook_cls, webhook_client = _make_webhook_client_cls()

    with (
        patch.object(cr.auto_db, "get_automation", new=create_autospec(auto_db_mod.get_automation, return_value=auto)),
        patch.object(settle_mod.exec_db, "settle_execution", new=settle_exec_mock),
        patch.object(cr, "claim_failure_notification", new=claim_fn_mock),
        patch.object(cr, "_update_failure_notification_state", new=update_fn_mock),
        patch.object(delivery_mod, "start_run", new=start_run_mock),
        patch.object(delivery_mod, "finish_run", new=finish_run_mock),
        patch.object(cr, "resolve_delivery_workspace_id", new=create_autospec(cr.resolve_delivery_workspace_id, return_value=None)),
        patch.object(recycle_mod, "automation_is_soft_deleted", new=create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False)),
        patch.object(cr, "WebhookClient", new=webhook_cls),
        patch.object(settle_mod, "WebhookClient", new=webhook_cls),
        patch.object(settle_mod, "publish_automation_wait", new=create_autospec(publish_automation_wait, return_value=None)),
    ):
        await cr.sweep_one_capacity_parked(expired_row, disp_mock, stopping=lambda: False)
        await cr.sweep_one_capacity_parked(expired_row, disp_mock, stopping=lambda: False)
        await settle_mod.drain_tails()

    disp_mock.assert_not_called()
    # Durable failure_notify task was written inside the winning settlement transaction (thread_id=None!)
    assert any("'{failure_notify}'" in sql for sql, _p in tx_executed)
    start_run_mock.assert_awaited_once()
    finish_run_mock.assert_awaited_once()
    update_fn_mock.assert_awaited_once()
    assert update_fn_mock.await_args.kwargs["status"] == "done"
    webhook_client.fire_event.assert_not_awaited()


# ---------------------------------------------------------------------------
# 7. Recipient edits / clears during capacity wait -> ZERO deliveries to old targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_changed_or_cleared_during_capacity_wait_re_resolves_or_revokes(
    monkeypatch,
) -> None:
    """When recipients change during capacity wait, Attempt 2 re-resolves under a new
    delivery start and replaces the stamp. When recipients are cleared, Attempt 2 clears
    the persisted stamp and revokes the old gateway registration -> zero posts to old targets."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    old_delivery = delivery_mod.Delivery(
        id=_EXEC,
        targets=[delivery_mod.Target(entry="slack:T/OLD", address="slack:T/OLD", name="#old", ok=True)],
    )
    new_delivery = delivery_mod.Delivery(
        id=f"{_EXEC}.2",
        targets=[delivery_mod.Target(entry="slack:T/NEW", address="slack:T/NEW", name="#new", ok=True)],
    )

    def _turn_ok(**kwargs):
        async def _gen():
            yield "event: message_chunk\ndata: {}\n\n"

        return _gen()

    # Subcase A: Changed recipients [OLD] -> [NEW]
    auto_changed = _automation(delivery_config={"methods": ["slack:T/NEW"]})
    persisted_old = {
        **delivery_mod.stamp(old_delivery),
        cr.DELIVERY_META_KEY: {"entries": ["slack:T/OLD"], "starts": 1, "registered": True},
    }
    with ExitStack() as stack:
        mocks = _bind_executor_mocks(
            stack,
            automation=auto_changed,
            turns=[_turn_ok],
            execution_row={
                "automation_execution_id": _EXEC,
                "automation_id": _AUTO,
                "status": "pending",
                "conversation_thread_id": _THREAD,
                "conversation_response_id": None,
                "scheduled_at": _NOW,
                "wait_reason": cr.CAPACITY_WAIT_REASON,
                "awaits_computer": None,
                "checked_computer_id": None,
                "delivery": persisted_old,
            },
            start_run_side_effect=lambda a, did, ws, thread_id=None: new_delivery,
        )
        mocks["get_run"].return_value = {
            "conversation_response_id": _RUN,
            "status": "completed",
            "metadata": {},
        }
        await AutomationExecutor().execute(auto_changed, _EXEC)
        mocks["start_run"].assert_awaited_once()
        assert mocks["start_run"].await_args.args[1] == f"{_EXEC}.2"
        stamped = delivery_mod.read_stamp(mocks["row"]["delivery"], _EXEC)
        assert stamped is not None
        assert [t.entry for t in stamped.targets] == ["slack:T/NEW"]

    # Subcase B: Cleared all recipients [OLD] -> []
    auto_cleared = _automation(delivery_config={"methods": []})
    with ExitStack() as stack:
        mocks = _bind_executor_mocks(
            stack,
            automation=auto_cleared,
            turns=[_turn_ok],
            execution_row={
                "automation_execution_id": _EXEC,
                "automation_id": _AUTO,
                "status": "pending",
                "conversation_thread_id": _THREAD,
                "conversation_response_id": None,
                "scheduled_at": _NOW,
                "wait_reason": cr.CAPACITY_WAIT_REASON,
                "awaits_computer": None,
                "checked_computer_id": None,
                "delivery": dict(persisted_old),
            },
            start_run_side_effect=lambda a, did, ws, thread_id=None: None,
        )
        mocks["get_run"].return_value = {
            "conversation_response_id": _RUN,
            "status": "completed",
            "metadata": {},
        }
        await AutomationExecutor().execute(auto_cleared, _EXEC)
        mocks["revoke"].assert_awaited_once_with(_EXEC, user_id=_USER)
        assert delivery_mod.read_stamp(mocks["row"]["delivery"], _EXEC) is None


# ---------------------------------------------------------------------------
# 8. Pausing / disabling / skipping releases pre-START claimed signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_and_skip_release_pre_start_claimed_signals_in_same_transaction() -> None:
    """Pausing or manually skipping a waiting Dalton execution confirms no START run exists
    and calls watch_db.release_signals_claimed_by inside the settlement transaction."""
    dalton_auto = _automation(name="dalton-nvda-delta", status="paused")
    tx_conn, _executed = _make_tx_conn(
        fetchone_results=[None, {"current_status": "in_progress"}],
        rowcounts=[0, 1, 1],
    )
    settled_row = {
        "conversation_thread_id": _THREAD,
        "conversation_response_id": None,
        "settled_from": "waiting",
        "delivery": None,
        "previous_failure_reason": None,
        "previous_delivery_result": None,
    }

    async def _settle_with_tx(_eid, *, in_transaction=None, **_fields):
        if in_transaction is not None:
            await in_transaction(tx_conn, settled_row)
        return settled_row

    release_mock = create_autospec(watch_db.release_signals_claimed_by, return_value=2)
    webhook_cls, _ = _make_webhook_client_cls()
    with (
        patch.object(
            settle_mod.exec_db,
            "settle_execution",
            new=create_autospec(exec_db_mod.settle_execution, side_effect=_settle_with_tx),
        ),
        patch.object(watch_db, "release_signals_claimed_by", new=release_mock),
        patch.object(settle_mod, "WebhookClient", new=webhook_cls),
        patch.object(
            settle_mod,
            "publish_automation_wait",
            new=create_autospec(publish_automation_wait, return_value=None),
        ),
        patch.object(
            recycle_mod,
            "automation_is_soft_deleted",
            new=create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False),
        ),
    ):
        ok = await settle_mod.settle(
            dalton_auto,
            _EXEC,
            Outcome.SKIPPED,
            thread_id=_THREAD,
            workspace_id=_HOME,
            skip_reason="user",
            release_pre_start_signals=True,
            reset_prestart_thread_status=True,
        )
        await settle_mod.drain_tails()

    assert ok is True
    release_mock.assert_awaited_once_with(_EXEC, conn=tx_conn)


def test_capacity_error_classification_and_backoff_schedule() -> None:
    """Capacity error classification and jittered exponential backoff bounds."""
    assert cr.is_capacity_retryable_error(WriterGuardUnavailable("exhausted"))
    assert cr.is_capacity_retryable_error(HTTPException(status_code=503, detail="down"))
    assert cr.is_capacity_retryable_error(
        HTTPException(status_code=429, detail={"type": "burst_limit"})
    )
    assert cr.is_capacity_retryable_error(
        HTTPException(status_code=429, detail={"type": "service_unavailable"})
    )
    assert not cr.is_capacity_retryable_error(
        HTTPException(status_code=429, detail={"type": "credit_limit"})
    )
    assert not cr.is_capacity_retryable_error(RuntimeError("boom"))

    assert cr.compute_backoff_seconds(1, jitter_fn=lambda a, b: 1.0) == 30.0
    assert cr.compute_backoff_seconds(2, jitter_fn=lambda a, b: 1.0) == 60.0
    assert cr.compute_backoff_seconds(3, jitter_fn=lambda a, b: 1.0) == 120.0
    assert cr.compute_backoff_seconds(4, jitter_fn=lambda a, b: 1.0) == 240.0
    assert cr.compute_backoff_seconds(5, jitter_fn=lambda a, b: 1.0) == 240.0
    jittered = cr.compute_backoff_seconds(1)
    assert 25.5 <= jittered <= 37.5
    assert cg_db._normalize_automation_run_uuid(f"{_EXEC}.2") == _EXEC
    assert cg_db._normalize_automation_run_uuid("not-a-uuid.2") is None


# ---------------------------------------------------------------------------
# 9. R3 fixes: claim-token fencing, locked expiry recheck, default target
#    re-resolution, durable failure retry, pending skip & rollback signals,
#    and abandoned thread reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_token_propagates_to_begin_and_fences_lease_release() -> None:
    """Executor forwards claim_token into begin_execution_if_not_deleted, whose
    SQL checks both claim_token and capacity deadline; release_dispatch_lease
    also checks claim_token."""
    begin_mock = create_autospec(
        exec_db_mod.begin_execution_if_not_deleted, return_value=None
    )
    with (
        patch.object(exec_mod.exec_db, "begin_execution_if_not_deleted", new=begin_mock),
        patch.object(
            exec_mod.exec_db,
            "touch_execution",
            new=create_autospec(exec_db_mod.touch_execution, return_value=None),
        ),
    ):
        await AutomationExecutor().execute(
            _automation(), _EXEC, claim_token="tok-stale-1"
        )
    begin_mock.assert_awaited_once()
    assert begin_mock.await_args.kwargs["claim_token"] == "tok-stale-1"

    # Verify SQL emitted by begin_execution_if_not_deleted checks claim_token & deadline
    get_conn, executed = _make_recording_db(fetchone_results=[None])
    with (
        patch.object(exec_db_mod, "get_db_connection", side_effect=get_conn),
        patch(
            "src.dalton.settings.is_coverage_units_enabled",
            new=create_autospec(
                __import__(
                    "src.dalton.settings", fromlist=["is_coverage_units_enabled"]
                ).is_coverage_units_enabled,
                return_value=False,
            ),
        ),
    ):
        res = await exec_db_mod.begin_execution_if_not_deleted(
            _EXEC, started_at=_NOW, claim_token="tok-live-2"
        )
    assert res is None
    assert len(executed) == 1
    sql, params = executed[0]
    assert "claim_token" in sql
    assert "deadline_at" in sql
    assert "> NOW()" in sql
    assert "tok-live-2" in params

    # Verify release_dispatch_lease fences on claim_token
    get_conn2, executed2 = _make_recording_db(rowcounts=[0])
    with patch.object(cr, "get_db_connection", side_effect=get_conn2):
        ok = await cr.release_dispatch_lease(_EXEC, claim_token="tok-stale-1")
    assert ok is False
    assert "claim_token" in executed2[0][0]
    assert "tok-stale-1" in executed2[0][1]


@pytest.mark.asyncio
async def test_settle_execution_rechecks_wait_reason_claim_token_and_deadline_in_lock() -> None:
    """Under FOR UPDATE, settle_execution refuses capacity settlement when
    wait_reason moved to computer_unreachable, claim_token changed, or deadline
    is not yet expired."""
    # Case 1: wait_reason moved to computer_unreachable
    get_conn_cw, exec_cw = _make_recording_db(
        fetchone_results=[
            {"?column?": 1},
            {
                "status": "waiting",
                "wait_reason": "computer_unreachable",
                "created_at": _NOW - timedelta(minutes=20),
                "delivery": {
                    cr.CAPACITY_RETRY_KEY: {
                        "deadline_at": (_NOW - timedelta(minutes=1)).isoformat(),
                        "claim_token": "tok-1",
                    }
                },
                "conversation_thread_id": None,
                "conversation_response_id": None,
                "coverage_unit_id": None,
                "coverage_unit_version": None,
                "db_now": _NOW,
            },
        ]
    )
    with patch.object(exec_db_mod, "get_db_connection", side_effect=get_conn_cw):
        settled = await exec_db_mod.settle_execution(
            _EXEC,
            automation_id=_AUTO,
            from_statuses=("waiting", "pending"),
            to="failed",
            expected_wait_reason=cr.CAPACITY_WAIT_REASON,
            expected_claim_token="tok-1",
            require_capacity_expired=True,
        )
    assert settled is None
    assert not any(sql.startswith("UPDATE automation_executions") for sql, _ in exec_cw)

    # Case 2: claim_token changed (another worker reclaimed)
    get_conn_tok, exec_tok = _make_recording_db(
        fetchone_results=[
            {"?column?": 1},
            {
                "status": "pending",
                "wait_reason": cr.CAPACITY_WAIT_REASON,
                "created_at": _NOW - timedelta(minutes=20),
                "delivery": {
                    cr.CAPACITY_RETRY_KEY: {
                        "deadline_at": (_NOW - timedelta(minutes=1)).isoformat(),
                        "claim_token": "tok-new",
                    }
                },
                "conversation_thread_id": None,
                "conversation_response_id": None,
                "coverage_unit_id": None,
                "coverage_unit_version": None,
                "db_now": _NOW,
            },
        ]
    )
    with patch.object(exec_db_mod, "get_db_connection", side_effect=get_conn_tok):
        settled2 = await exec_db_mod.settle_execution(
            _EXEC,
            automation_id=_AUTO,
            from_statuses=("waiting", "pending"),
            to="failed",
            expected_wait_reason=cr.CAPACITY_WAIT_REASON,
            expected_claim_token="tok-old",
            require_capacity_expired=True,
        )
    assert settled2 is None
    assert not any(sql.startswith("UPDATE automation_executions") for sql, _ in exec_tok)

    # Case 3: deadline not yet reached
    get_conn_not_exp, exec_not_exp = _make_recording_db(
        fetchone_results=[
            {"?column?": 1},
            {
                "status": "waiting",
                "wait_reason": cr.CAPACITY_WAIT_REASON,
                "created_at": _NOW - timedelta(minutes=5),
                "delivery": {
                    cr.CAPACITY_RETRY_KEY: {
                        "deadline_at": (_NOW + timedelta(minutes=10)).isoformat(),
                    }
                },
                "conversation_thread_id": None,
                "conversation_response_id": None,
                "coverage_unit_id": None,
                "coverage_unit_version": None,
                "db_now": _NOW,
            },
        ]
    )
    with patch.object(exec_db_mod, "get_db_connection", side_effect=get_conn_not_exp):
        settled3 = await exec_db_mod.settle_execution(
            _EXEC,
            automation_id=_AUTO,
            from_statuses=("waiting", "pending"),
            to="failed",
            expected_wait_reason=cr.CAPACITY_WAIT_REASON,
            expected_claim_token=None,
            require_capacity_expired=True,
        )
    assert settled3 is None
    assert not any(sql.startswith("UPDATE automation_executions") for sql, _ in exec_not_exp)


@pytest.mark.asyncio
async def test_default_feishu_and_discord_targets_re_resolve_on_retry_and_expiry(
    monkeypatch,
) -> None:
    """When methods contains a bare default app entry ('feishu' / 'discord'),
    both retry execution and expiry failure settlement re-resolve the default
    group even though methods stayed unchanged."""
    monkeypatch.delenv("DALTON_AUTOMATION_CAPACITY_RETRY", raising=False)
    auto = _automation(delivery_config={"methods": ["feishu", "discord"]})
    old_delivery = delivery_mod.Delivery(
        id=_EXEC,
        targets=[
            delivery_mod.Target(entry="feishu", address="feishu:oc_old", name="Old Feishu", ok=True),
            delivery_mod.Target(entry="discord", address="discord:g/old", name="Old Discord", ok=True),
        ],
    )
    new_delivery = delivery_mod.Delivery(
        id=f"{_EXEC}.2",
        targets=[
            delivery_mod.Target(entry="feishu", address="feishu:oc_new", name="New Feishu", ok=True),
            delivery_mod.Target(entry="discord", address="discord:g/new", name="New Discord", ok=True),
        ],
    )
    persisted_old = {
        **delivery_mod.stamp(old_delivery),
        cr.DELIVERY_META_KEY: {"entries": ["feishu", "discord"], "starts": 1, "registered": True},
    }

    def _turn_ok(**kwargs):
        async def _gen():
            yield "event: message_chunk\ndata: {}\n\n"

        return _gen()

    # 1) Retry attempt re-resolves unchanged default entries ['feishu', 'discord']
    with ExitStack() as stack:
        mocks = _bind_executor_mocks(
            stack,
            automation=auto,
            turns=[_turn_ok],
            execution_row={
                "automation_execution_id": _EXEC,
                "automation_id": _AUTO,
                "status": "pending",
                "conversation_thread_id": _THREAD,
                "conversation_response_id": None,
                "scheduled_at": _NOW,
                "wait_reason": cr.CAPACITY_WAIT_REASON,
                "awaits_computer": None,
                "checked_computer_id": None,
                "delivery": dict(persisted_old),
            },
            start_run_side_effect=lambda a, did, ws, thread_id=None: new_delivery,
        )
        mocks["get_run"].return_value = {
            "conversation_response_id": _RUN,
            "status": "completed",
            "metadata": {},
        }
        await AutomationExecutor().execute(auto, _EXEC)
        mocks["start_run"].assert_awaited_once()
        assert mocks["start_run"].await_args.args[1] == f"{_EXEC}.2"
        stamped = delivery_mod.read_stamp(mocks["row"]["delivery"], _EXEC)
        assert stamped is not None
        assert [t.address for t in stamped.targets] == ["feishu:oc_new", "discord:g/new"]

    # 2) Expiry failure reconciliation also re-resolves unchanged default entries
    start_run_exp = create_autospec(delivery_mod.start_run, return_value=new_delivery)
    with (
        patch.object(
            recycle_mod,
            "automation_is_soft_deleted",
            new=create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False),
        ),
        patch.object(
            cr,
            "resolve_delivery_workspace_id",
            new=create_autospec(cr.resolve_delivery_workspace_id, return_value=_HOME),
        ),
        patch.object(delivery_mod, "start_run", new=start_run_exp),
    ):
        reconciled = await cr.reconcile_delivery_for_settled_failure(
            auto,
            _EXEC,
            settled_row={"delivery": dict(persisted_old)},
            thread_id=None,
            current_delivery=old_delivery,
        )
    start_run_exp.assert_awaited_once()
    assert start_run_exp.await_args.args[1] == f"{_EXEC}.2"
    assert reconciled == new_delivery


@pytest.mark.asyncio
async def test_durable_failure_notification_retries_on_transient_503_and_completes() -> None:
    """A durable failure_notify task retries via status='pending' + backoff when
    finish_run returns retry=True, and marks status='done' once confirmed."""
    auto = _automation(delivery_config={"methods": ["feishu"]})
    claimed = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "conversation_thread_id": None,
        "conversation_response_id": None,
        "error_message": "HTTPException: 503",
        "delivery": {
            cr.FAILURE_NOTIFY_KEY: {
                "status": "claimed",
                "claim_token": "tok-retry-1",
                "execution_id": _EXEC,
                "automation_id": _AUTO,
                "user_id": _USER,
                "thread_id": None,
                "workspace_id": _HOME,
                "outcome": Outcome.FAILED_OURS.value,
                "error": "HTTPException: 503",
                "attempts": 1,
                "recorded": False,
                "answered": False,
            }
        },
    }
    held = delivery_mod.Delivery(
        id=_EXEC,
        targets=[delivery_mod.Target(entry="feishu", address="feishu:oc_new", name="Group", ok=True)],
    )
    finish_retry = create_autospec(
        delivery_mod.finish_run,
        return_value=delivery_mod.Finish(
            [{"method": "feishu", "address": "feishu:oc_new", "name": "Group", "success": False, "via": None, "error": "Delivery couldn't be confirmed."}],
            retry=True,
            answered=False,
        ),
    )
    update_state_mock = create_autospec(
        cr._update_failure_notification_state, return_value=True
    )
    with (
        patch.object(cr, "claim_failure_notification", new=create_autospec(cr.claim_failure_notification, return_value=claimed)),
        patch.object(cr, "_update_failure_notification_state", new=update_state_mock),
        patch.object(recycle_mod, "automation_is_soft_deleted", new=create_autospec(recycle_mod.automation_is_soft_deleted, return_value=False)),
        patch.object(cr.auto_db, "get_automation", new=create_autospec(auto_db_mod.get_automation, return_value=auto)),
        patch.object(cr, "reconcile_delivery_for_settled_failure", new=create_autospec(cr.reconcile_delivery_for_settled_failure, return_value=held)),
        patch.object(delivery_mod, "finish_run", new=finish_retry),
    ):
        ok = await cr.deliver_settled_failure_notification(_EXEC)
    assert ok is True
    update_state_mock.assert_awaited_once()
    kw = update_state_mock.await_args.kwargs
    assert kw["claim_token"] == "tok-retry-1"
    assert kw["status"] == "pending"
    assert kw["delay_seconds"] > 0
    assert kw["delivery_result"][0]["retrying"] is True


@pytest.mark.asyncio
async def test_manual_skip_allows_capacity_pending_and_rollback_releases_prestart_signals() -> None:
    """1) skip_execution passes allow_capacity_pending=True; settle_execution accepts
    status='pending' + wait_reason='capacity_limit' while rejecting ordinary pending.
    2) Rollback to a paused version checks START and releases pre-START signals in-tx."""
    from src.dalton.services import automation_recycle as recycle_svc
    from src.server.services.automations import lifecycle as auto_lifecycle

    # Part 1a: skip_execution forwards allow_capacity_pending=True
    settle_spy = create_autospec(auto_lifecycle.settle, return_value=True)
    with (
        patch.object(
            auto_lifecycle.auto_db,
            "get_automation",
            new=create_autospec(auto_db_mod.get_automation, return_value=_automation()),
        ),
        patch.object(auto_lifecycle, "settle", new=settle_spy),
    ):
        res = await auto_lifecycle.skip_execution(_AUTO, _EXEC, _USER)
    assert res is not None and res["status"] == "skipped"
    assert settle_spy.await_args.kwargs["allow_capacity_pending"] is True

    # Part 1b: settle_execution rejects ordinary pending (wait_reason=None) even with allow_capacity_pending=True
    get_conn_ord, exec_ord = _make_recording_db(
        fetchone_results=[
            {"?column?": 1},
            {
                "status": "pending",
                "wait_reason": None,
                "created_at": _NOW,
                "delivery": None,
                "conversation_thread_id": None,
                "conversation_response_id": None,
                "coverage_unit_id": None,
                "coverage_unit_version": None,
                "db_now": _NOW,
            },
        ]
    )
    with patch.object(exec_db_mod, "get_db_connection", side_effect=get_conn_ord):
        rejected = await exec_db_mod.settle_execution(
            _EXEC,
            automation_id=_AUTO,
            from_statuses=("waiting",),
            to="skipped",
            allow_capacity_pending=True,
        )
    assert rejected is None
    assert not any(sql.startswith("UPDATE automation_executions") for sql, _ in exec_ord)

    # Part 2: _release_skipped_unstarted_executions checks START and releases signals in same tx
    tx_conn, _ = _make_tx_conn(
        fetchone_results=[None, {"current_status": "in_progress"}],
        rowcounts=[0, 1, 1],
    )
    release_mock = create_autospec(watch_db.release_signals_claimed_by, return_value=1)
    dalton_auto = _automation(name="dalton-nvda-delta", status="paused")
    unfinished = [
        {
            "automation_execution_id": _EXEC,
            "status": "waiting",
            "conversation_thread_id": _THREAD,
            "conversation_response_id": None,
        },
        {
            "automation_execution_id": "00000000-0000-0000-0000-000000000299",
            "status": "running",
            "conversation_thread_id": _THREAD,
            "conversation_response_id": _RUN,
        },
    ]
    with patch.object(watch_db, "release_signals_claimed_by", new=release_mock):
        await recycle_svc._release_skipped_unstarted_executions(
            tx_conn,
            automation=dalton_auto,
            unfinished=unfinished,
        )
    release_mock.assert_awaited_once_with(_EXEC, conn=tx_conn)


@pytest.mark.asyncio
async def test_settle_abandoned_without_run_enables_prestart_thread_reset() -> None:
    """Abandoned executions that never created a conversation_response row enable
    reset_prestart_thread_status=True in the settlement transaction."""
    settle_spy = create_autospec(settle_mod.settle, return_value=True)
    recent_now = datetime.now(timezone.utc)
    abandoned_running = {
        "automation_execution_id": _EXEC,
        "automation_id": _AUTO,
        "status": "running",
        "conversation_thread_id": _THREAD,
        "conversation_response_id": None,
        "started_at": recent_now,
        "created_at": recent_now,
        "delivery": None,
        "user_id": _USER,
        "workspace_id": _HOME,
    }
    abandoned_waiting = {
        **abandoned_running,
        "status": "waiting",
    }
    with (
        patch.object(settle_mod, "settle", new=settle_spy),
        patch.object(
            settle_mod.exec_db,
            "get_settling_run",
            new=create_autospec(exec_db_mod.get_settling_run, return_value=None),
        ),
        patch(
            "src.dalton.services.automation_subscription_fallback.apply_execution_fallback_notice",
            new=create_autospec(
                __import__(
                    "src.dalton.services.automation_subscription_fallback",
                    fromlist=["apply_execution_fallback_notice"],
                ).apply_execution_fallback_notice,
                return_value=(None, None),
            ),
        ),
    ):
        out_running = await settle_mod.settle_abandoned(_automation(), abandoned_running, 300)
        out_waiting = await settle_mod.settle_abandoned(_automation(), abandoned_waiting, 300)

    assert out_running is Outcome.INTERRUPTED
    assert out_waiting is Outcome.SKIPPED
    assert settle_spy.await_count == 2
    assert settle_spy.await_args_list[0].kwargs["reset_prestart_thread_status"] is True
    assert settle_spy.await_args_list[1].kwargs["reset_prestart_thread_status"] is True
