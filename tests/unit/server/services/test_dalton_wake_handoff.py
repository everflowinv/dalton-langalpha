"""Tests for restoring persisted Dalton preflight wake_context on restart handoff/refire."""

from __future__ import annotations

import logging
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
from src.server.services.turn_runtime import TurnRoute

_EXEC_MOD = "src.server.services.automation_executor"
_EXEC_ID = "00000000-0000-0000-0000-00000000d037"


def _dalton_automation() -> dict[str, Any]:
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
    }


async def _run_handed_off_execution(
    stored_wake_context: str | None,
) -> tuple[Any, Any, list[Any]]:
    captured_firings: list[Any] = []
    decide_mock = create_autospec(dalton_preflight.decide)
    get_ctx_mock = create_autospec(
        watch_db.get_preflight_wake_context,
        return_value=stored_wake_context,
    )
    begin_mock = create_autospec(
        exec_db.begin_execution_if_not_deleted,
        return_value={
            "conversation_thread_id": "thread-handed-1",
            "conversation_response_id": None,
        },
    )
    transition_mock = create_autospec(
        exec_db.transition_execution,
        return_value=True,
    )
    settle_fn = create_autospec(executor_mod.settle, return_value=True)
    get_auto_mock = create_autospec(auto_db.get_automation, return_value=_dalton_automation())
    route_mock = create_autospec(
        executor_mod.turn_route,
        return_value=TurnRoute("ws-nvda-1", "analyst"),
    )
    route_bind_mock = create_autospec(TurnRoute.bind, return_value=None)
    credentials_mock = create_autospec(executor_mod._credentials, return_value=(False, False))
    credit_mock = create_autospec(executor_mod.enforce_credit_limit, return_value=None)
    turn_place_mock = create_autospec(
        executor_mod._turn_place,
        return_value=SimpleNamespace(workspace={"workspace_id": "ws-nvda-1"}),
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
        stack.enter_context(patch.object(executor_mod, "_keep_alive", keep_alive_mock))
        stack.enter_context(patch.object(executor_mod, "_thread_busy", create_autospec(executor_mod._thread_busy, return_value=False)))
        fake_task = create_autospec(executor_mod.asyncio.Task, instance=True)
        stack.enter_context(
            patch(
                f"{_EXEC_MOD}.asyncio.create_task",
                create_autospec(
                    executor_mod.asyncio.create_task,
                    side_effect=lambda coro, **_kw: (coro.close(), fake_task)[1],
                ),
            )
        )
        stack.enter_context(patch.object(AutomationExecutor, "_start_delivery", start_delivery_mock))
        stack.enter_context(patch.object(AutomationExecutor, "_run_turn", run_turn_mock))

        await AutomationExecutor().execute(_dalton_automation(), _EXEC_ID)

    return decide_mock, get_ctx_mock, captured_firings


@pytest.mark.asyncio
async def test_handed_off_execution_skips_preflight_and_restores_wake_context() -> None:
    """交接执行（handed_thread 非空）跳过预检并将持久化 wake_context 填入 _Firing。"""
    expected_ctx = "<dalton_wake>\nSignal 1\n</dalton_wake>\n<coverage_group_refresh>\nrefreshed\n</coverage_group_refresh>"
    decide_mock, get_ctx_mock, captured_firings = await _run_handed_off_execution(expected_ctx)

    decide_mock.assert_not_awaited()
    get_ctx_mock.assert_awaited_once_with(_EXEC_ID)
    assert len(captured_firings) == 1
    assert captured_firings[0].wake_context == expected_ctx


@pytest.mark.asyncio
async def test_handed_off_execution_missing_wake_context_sets_none_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """交接执行查不到持久化 wake_context 时保持 None 并记录 warning 日志。"""
    with caplog.at_level(logging.WARNING):
        decide_mock, get_ctx_mock, captured_firings = await _run_handed_off_execution(None)

    decide_mock.assert_not_awaited()
    get_ctx_mock.assert_awaited_once_with(_EXEC_ID)
    assert len(captured_firings) == 1
    assert captured_firings[0].wake_context is None
    assert any(_EXEC_ID in rec.message and rec.levelno >= logging.WARNING for rec in caplog.records)


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

