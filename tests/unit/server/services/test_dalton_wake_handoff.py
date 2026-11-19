"""Tests for restoring persisted Dalton preflight wake_context on restart handoff/refire.

DALTON-HOOK(preflight)
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

import pytest

from src.dalton.autonomy import preflight as dalton_preflight
from src.dalton.database import watch as watch_db
from src.server.database import automation as auto_db
from src.server.database import automation_executions as exec_db
from src.server.services import automation_executor as executor_mod
from src.server.services.automation_executor import AutomationExecutor
from src.server.services.automation_settlement import Outcome
from src.server.services.turn_runtime import TurnRoute

_EXEC_MOD = "src.server.services.automation_executor"
_EXEC_ID = "00000000-0000-0000-0000-00000000d037"


def _dalton_automation(**overrides: Any) -> dict[str, Any]:
    return {
        "automation_id": "auto-dalton-1",
        "user_id": "user-1",
        "name": "dalton-watch-NVDA",
        "instruction": "Check signals",
        "trigger_type": "cron",
        "workspace_id": "ws-nvda-1",
        "thread_strategy": "new",
        "conversation_thread_id": None,
        "llm_model": None,
        "additional_context": None,
        "trigger_config": None,
        "status": "active",
        "metadata": {"dalton_managed": True, "ticker": "NVDA"},
        **overrides,
    }


async def _run_handed_off_execution(
    stored_wake_context: str | None,
    *,
    handed_thread: str | None = "thread-handed-1",
    parked_for_computer: bool = False,
    decide_return: dalton_preflight.PreflightResult | None = None,
    decide_side_effect: BaseException | None = None,
    automation: dict[str, Any] | None = None,
) -> SimpleNamespace:
    captured_firings: list[Any] = []
    decide_mock = create_autospec(
        dalton_preflight.decide,
        return_value=decide_return,
        side_effect=decide_side_effect,
    )
    get_ctx_mock = create_autospec(
        watch_db.get_preflight_wake_context,
        return_value=stored_wake_context,
    )
    begin_mock = create_autospec(
        exec_db.begin_execution_if_not_deleted,
        return_value={
            "conversation_thread_id": handed_thread,
            "conversation_response_id": None,
        },
    )
    transition_mock = create_autospec(
        exec_db.transition_execution,
        return_value=True,
    )
    settle_fn = create_autospec(executor_mod.settle, return_value=True)
    target_automation = automation or _dalton_automation()
    get_auto_mock = create_autospec(auto_db.get_automation, return_value=target_automation)
    route_mock = create_autospec(
        executor_mod.turn_route,
        return_value=TurnRoute(target_automation["workspace_id"], "analyst"),
    )
    route_bind_mock = create_autospec(TurnRoute.bind, return_value=None)
    credentials_mock = create_autospec(executor_mod._credentials, return_value=(False, False))
    credit_mock = create_autospec(executor_mod.enforce_credit_limit, return_value=None)
    turn_place_mock = create_autospec(
        executor_mod._turn_place,
        return_value=SimpleNamespace(
            workspace={"workspace_id": target_automation["workspace_id"]},
            thread_id=target_automation.get("conversation_thread_id"),
        ),
    )
    park_computer_mock = create_autospec(
        executor_mod.park_for_computer,
        return_value=parked_for_computer,
    )

    async def _capture_run_turn(self: AutomationExecutor, execution_id: str, firing: Any, **kwargs: Any) -> bool:
        captured_firings.append(firing)
        return False

    run_turn_mock = create_autospec(
        AutomationExecutor._run_turn,
        side_effect=_capture_run_turn,
    )
    start_delivery_mock = create_autospec(AutomationExecutor._start_delivery, return_value=None)
    keep_alive_mock = create_autospec(executor_mod._keep_alive, return_value=None)

    with ExitStack() as stack:
        stack.enter_context(patch.object(dalton_preflight, "decide", decide_mock))
        stack.enter_context(patch.object(watch_db, "get_preflight_wake_context", get_ctx_mock))
        stack.enter_context(patch.object(exec_db, "begin_execution_if_not_deleted", begin_mock))
        stack.enter_context(patch.object(exec_db, "transition_execution", transition_mock))
        stack.enter_context(patch.object(auto_db, "get_automation", get_auto_mock))
        stack.enter_context(patch.object(executor_mod, "settle", settle_fn))
        stack.enter_context(patch.object(executor_mod, "turn_route", route_mock))
        stack.enter_context(patch.object(TurnRoute, "bind", route_bind_mock))
        stack.enter_context(patch.object(executor_mod, "_credentials", credentials_mock))
        stack.enter_context(patch.object(executor_mod, "enforce_credit_limit", credit_mock))
        stack.enter_context(patch.object(executor_mod, "_turn_place", turn_place_mock))
        stack.enter_context(patch.object(executor_mod, "park_for_computer", park_computer_mock))
        stack.enter_context(patch.object(executor_mod, "_keep_alive", keep_alive_mock))
        stack.enter_context(patch.object(executor_mod, "_thread_busy", create_autospec(executor_mod._thread_busy, return_value=False)))
        stack.enter_context(patch.object(AutomationExecutor, "_start_delivery", start_delivery_mock))
        stack.enter_context(patch.object(AutomationExecutor, "_run_turn", run_turn_mock))

        await AutomationExecutor().execute(target_automation, _EXEC_ID)

    return SimpleNamespace(
        decide=decide_mock,
        get_ctx=get_ctx_mock,
        captured_firings=captured_firings,
        run_turn=run_turn_mock,
        settle=settle_fn,
    )


@pytest.mark.asyncio
async def test_handed_off_execution_skips_preflight_and_restores_wake_context() -> None:
    """已预检的交接执行（dalton_preflight_log 有记录）跳过预检并将持久化 wake_context 填入 _Firing。"""
    expected_ctx = "<dalton_wake>\nSignal 1\n</dalton_wake>\n<coverage_group_refresh>\nrefreshed\n</coverage_group_refresh>"
    res = await _run_handed_off_execution(expected_ctx)

    res.decide.assert_not_awaited()
    res.get_ctx.assert_awaited_once_with(_EXEC_ID)
    assert len(res.captured_firings) == 1
    assert res.captured_firings[0].wake_context == expected_ctx


@pytest.mark.asyncio
async def test_handed_off_execution_that_woke_without_context_does_not_rerun_preflight() -> None:
    """预检已决定唤醒但没有上下文（日志行 wake_context 为空）时，恢复执行不得再跑预检重复认领信号。"""
    res = await _run_handed_off_execution("")

    res.decide.assert_not_awaited()
    res.get_ctx.assert_awaited_once_with(_EXEC_ID)
    assert len(res.captured_firings) == 1
    assert not res.captured_firings[0].wake_context


@pytest.mark.asyncio
async def test_prethread_execution_restores_wake_context_by_execution_id() -> None:
    """在创建 thread 前挂起过等待的执行恢复时，即使 handed_thread 为空也能按 execution_id 找回 wake_context。"""
    expected_ctx = "<dalton_wake>\nSignal before park\n</dalton_wake>"
    res = await _run_handed_off_execution(expected_ctx, handed_thread=None)

    res.decide.assert_not_awaited()
    res.get_ctx.assert_awaited_once_with(_EXEC_ID)
    assert len(res.captured_firings) == 1
    assert res.captured_firings[0].wake_context == expected_ctx


@pytest.mark.asyncio
async def test_parked_for_computer_skips_preflight_until_computer_returns() -> None:
    """需要等待电脑上线时先挂起，不调用会认领信号的 dalton_preflight.decide。"""
    res = await _run_handed_off_execution(
        None, handed_thread=None, parked_for_computer=True
    )

    res.get_ctx.assert_not_awaited()
    res.decide.assert_not_awaited()
    assert res.captured_firings == []


@pytest.mark.asyncio
async def test_handed_off_execution_without_preflight_record_runs_preflight() -> None:
    """未预检的交接执行（handed_thread 非空但 dalton_preflight_log 无记录）仍应执行预检。"""
    woken_ctx = "<dalton_wake>\nFresh signal after handoff\n</dalton_wake>"
    res = await _run_handed_off_execution(
        None,
        handed_thread="thread-wait-1",
        decide_return=dalton_preflight.PreflightResult(
            should_wake=True,
            decision="wake",
            reason="signal_detected",
            signals=[{"summary": "NVDA news"}],
            wake_context=woken_ctx,
        ),
    )

    res.get_ctx.assert_awaited_once_with(_EXEC_ID)
    res.decide.assert_awaited_once_with(_dalton_automation(), _EXEC_ID)
    assert res.run_turn.await_count == 1
    assert res.captured_firings[0].wake_context == woken_ctx


@pytest.mark.asyncio
async def test_wait_for_thread_handoff_then_resume_skips_turn_when_no_new_signals() -> None:
    """固定线程繁忙在 _wait_for_thread 期间因重启交接，恢复后无持久化记录时执行一次预检且无信号不启动回合。"""
    pinned_auto = _dalton_automation(
        thread_strategy="continue",
        conversation_thread_id="thread-pinned-1",
    )
    executor = AutomationExecutor()
    decide_mock = create_autospec(
        dalton_preflight.decide,
        return_value=dalton_preflight.PreflightResult(
            should_wake=False,
            decision="skip",
            reason="no_new_signals",
            signals=[],
        ),
    )
    get_ctx_mock = create_autospec(watch_db.get_preflight_wake_context, return_value=None)
    begin_mock = create_autospec(
        exec_db.begin_execution_if_not_deleted,
        side_effect=[
            {"conversation_thread_id": None, "conversation_response_id": None},
            {"conversation_thread_id": "thread-pinned-1", "conversation_response_id": None},
        ],
    )
    transition_mock = create_autospec(exec_db.transition_execution, return_value=True)
    exec_status_mock = create_autospec(exec_db.get_execution_status, return_value="waiting")
    earlier_mock = create_autospec(exec_db.has_earlier_waiting_execution, return_value=False)
    hand_off_mock = create_autospec(executor_mod.hand_off, return_value=None)
    settle_fn = create_autospec(executor_mod.settle, return_value=True)
    turn_place_mock = create_autospec(
        executor_mod._turn_place,
        return_value=SimpleNamespace(
            workspace={"workspace_id": "ws-nvda-1"},
            thread_id="thread-pinned-1",
        ),
    )
    run_turn_mock = create_autospec(AutomationExecutor._run_turn, return_value=False)

    draining_state = {"active": True}

    async def _thread_busy_first(thread_id: str) -> bool:
        return thread_id == "thread-pinned-1" and draining_state["active"]

    with ExitStack() as stack:
        stack.enter_context(patch.object(dalton_preflight, "decide", decide_mock))
        stack.enter_context(patch.object(watch_db, "get_preflight_wake_context", get_ctx_mock))
        stack.enter_context(patch.object(exec_db, "begin_execution_if_not_deleted", begin_mock))
        stack.enter_context(patch.object(exec_db, "transition_execution", transition_mock))
        stack.enter_context(patch.object(exec_db, "get_execution_status", exec_status_mock))
        stack.enter_context(patch.object(exec_db, "has_earlier_waiting_execution", earlier_mock))
        stack.enter_context(patch.object(auto_db, "get_automation", create_autospec(auto_db.get_automation, return_value=pinned_auto)))
        stack.enter_context(patch.object(executor_mod, "hand_off", hand_off_mock))
        stack.enter_context(patch.object(executor_mod, "settle", settle_fn))
        stack.enter_context(patch.object(executor_mod, "publish_automation_wait", create_autospec(executor_mod.publish_automation_wait, return_value=None)))
        stack.enter_context(patch.object(executor_mod, "_thread_busy", create_autospec(executor_mod._thread_busy, side_effect=_thread_busy_first)))
        stack.enter_context(patch.object(executor_mod, "turn_route", create_autospec(executor_mod.turn_route, return_value=TurnRoute("ws-nvda-1", "analyst"))))
        stack.enter_context(patch.object(TurnRoute, "bind", create_autospec(TurnRoute.bind, return_value=None)))
        stack.enter_context(patch.object(executor_mod, "_credentials", create_autospec(executor_mod._credentials, return_value=(False, False))))
        stack.enter_context(patch.object(executor_mod, "enforce_credit_limit", create_autospec(executor_mod.enforce_credit_limit, return_value=None)))
        stack.enter_context(patch.object(executor_mod, "_turn_place", turn_place_mock))
        stack.enter_context(patch.object(executor_mod, "park_for_computer", create_autospec(executor_mod.park_for_computer, return_value=False)))
        stack.enter_context(patch.object(executor_mod, "_keep_alive", create_autospec(executor_mod._keep_alive, return_value=None)))
        stack.enter_context(patch.object(executor_mod.restart_drain, "draining", lambda: draining_state["active"]))
        stack.enter_context(patch.object(AutomationExecutor, "_run_turn", run_turn_mock))

        # 阶段 1：等待线程期间收到停机信号 -> hand_off，未跑预检
        executor._stopping.set()
        await executor.execute(pinned_auto, _EXEC_ID)

        assert hand_off_mock.await_count == 1
        assert decide_mock.await_count == 0

        # 阶段 2：另一 worker 恢复（handed_thread 非空但无 preflight 记录）-> 跑 1 次预检，无信号不启动回合
        draining_state["active"] = False
        executor._stopping.clear()
        await executor.execute(pinned_auto, _EXEC_ID)

    assert decide_mock.await_count == 1
    assert run_turn_mock.await_count == 0
    settle_fn.assert_awaited_once_with(
        pinned_auto,
        _EXEC_ID,
        Outcome.PRECHECK_SKIPPED,
        workspace_id="ws-nvda-1",
        skip_reason="precheck",
        excerpt="no_new_signals",
    )


@pytest.mark.asyncio
async def test_preflight_exception_and_cancel_settle_with_automation_workspace_id() -> None:
    """route 尚未绑定时 preflight 异常或取消，结算仍传入 automation workspace_id 以免 webhook 丢失工作区。"""
    res_err = await _run_handed_off_execution(
        None,
        handed_thread=None,
        decide_side_effect=RuntimeError("preflight failed before route bind"),
    )
    res_err.settle.assert_awaited_once()
    assert res_err.settle.await_args.kwargs["workspace_id"] == "ws-nvda-1"
    assert res_err.settle.await_args.args[2] == Outcome.FAILED

    with pytest.raises(asyncio.CancelledError):
        await _run_handed_off_execution(
            None,
            handed_thread=None,
            decide_side_effect=asyncio.CancelledError(),
        )

    # 显式验证 CancelledError 分支结算时的 workspace_id
    auto = _dalton_automation()
    decide_mock = create_autospec(dalton_preflight.decide, side_effect=asyncio.CancelledError())
    settle_cancel = create_autospec(executor_mod.settle, return_value=True)
    with ExitStack() as stack:
        stack.enter_context(patch.object(dalton_preflight, "decide", decide_mock))
        stack.enter_context(patch.object(watch_db, "get_preflight_wake_context", create_autospec(watch_db.get_preflight_wake_context, return_value=None)))
        stack.enter_context(patch.object(exec_db, "begin_execution_if_not_deleted", create_autospec(exec_db.begin_execution_if_not_deleted, return_value={"conversation_thread_id": None, "conversation_response_id": None})))
        stack.enter_context(patch.object(executor_mod, "settle", settle_cancel))
        stack.enter_context(patch.object(executor_mod, "turn_route", create_autospec(executor_mod.turn_route, return_value=TurnRoute("ws-nvda-1", "analyst"))))
        stack.enter_context(patch.object(executor_mod, "_turn_place", create_autospec(executor_mod._turn_place, return_value=SimpleNamespace(workspace={"workspace_id": "ws-nvda-1"}, thread_id=None))))
        stack.enter_context(patch.object(executor_mod, "park_for_computer", create_autospec(executor_mod.park_for_computer, return_value=False)))
        stack.enter_context(patch.object(executor_mod, "_keep_alive", create_autospec(executor_mod._keep_alive, return_value=None)))
        stack.enter_context(patch.object(executor_mod, "_thread_busy", create_autospec(executor_mod._thread_busy, return_value=False)))

        with pytest.raises(asyncio.CancelledError):
            await AutomationExecutor().execute(auto, _EXEC_ID)

    settle_cancel.assert_awaited_once()
    assert settle_cancel.await_args.kwargs["workspace_id"] == "ws-nvda-1"
    assert settle_cancel.await_args.args[2] == Outcome.INTERRUPTED


class _AsyncCtx:
    def __init__(self, obj: Any) -> None:
        self._obj = obj

    async def __aenter__(self) -> Any:
        return self._obj

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.mark.asyncio
async def test_get_preflight_wake_context_reads_latest_wake_row_or_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """watch_db.get_preflight_wake_context 按 execution_id 查最新 decision='wake' 行。"""
    import psycopg

    executed: list[tuple[str, Any]] = []

    class _FakeCursor:
        def __init__(self, row: dict[str, Any] | None) -> None:
            self._row = row

        async def execute(self, sql: str, params: Any = None) -> None:
            executed.append((" ".join(sql.split()), params))

        async def fetchone(self) -> dict[str, Any] | None:
            return self._row

    fake_cur = create_autospec(_FakeCursor, instance=True)

    async def _exec(sql: str, params: Any = None) -> None:
        executed.append((" ".join(sql.split()), params))

    fake_cur.execute.side_effect = _exec
    fake_cur.fetchone.return_value = {"wake_context": "<dalton_wake>ok</dalton_wake>"}

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.cursor.return_value = _AsyncCtx(fake_cur)

    monkeypatch.setattr(
        watch_db,
        "get_db_connection",
        create_autospec(watch_db.get_db_connection, return_value=_AsyncCtx(fake_conn)),
    )

    ctx = await watch_db.get_preflight_wake_context(_EXEC_ID)
    assert ctx == "<dalton_wake>ok</dalton_wake>"
    assert len(executed) == 1
    assert "decision = 'wake'" in executed[0][0]
    assert "ORDER BY checked_at DESC, log_id DESC" in executed[0][0]

    fake_cur.fetchone.return_value = None
    assert await watch_db.get_preflight_wake_context(_EXEC_ID) is None
    assert await watch_db.get_preflight_wake_context("not-a-uuid") is None

