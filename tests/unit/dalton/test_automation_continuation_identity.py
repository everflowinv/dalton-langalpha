"""Continuation identity, open-execution hold, timeout cap, and concurrency tests."""

from __future__ import annotations

import logging
import re
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

import pytest
from psycopg._queries import PostgresQuery
from psycopg.adapt import Transformer

from src.dalton.database import ai_finance as ai_finance_db
from src.dalton.database import automation_reports as reports_db
from src.dalton.tools import automation_report_tools as report_tools
from src.server.database import automation_executions as exec_db
from src.server.handlers.chat import ptc_run, request_prep
from src.server.models.chat import ChatRequest, CommandReport, TaskReport
from src.server.services import automation_delivery, automation_settlement, user_skills
from src.server.services.automation_settlement import (
    Outcome,
    TIMEOUT_ERROR,
    _settle_finished_run,
    execution_timeout_seconds,
    is_execution_timed_out,
    settle_abandoned,
    settle_thread_execution_if_idle,
    take_over_firing,
)
from src.server.services.egress.direct_tools import order_ledger_for_turn
from src.server.services.report_back import command as command_rb
from src.server.services.report_back import subagent as subagent_rb
from src.server.services.report_back.handoff import carry
from src.server.services.thread_agent import AgentInputs
from src.server.services.turn_runtime import TurnRoute
from src.server.services.wakeups import cancel_notice
from src.server.services.workspace_manager import WorkspaceManager
from ptc_agent.core.session import Session

_SETTLE_MOD = "src.server.services.automation_settlement"
_PTC = "src.server.handlers.chat.ptc_run"

_USER = "user-1"
_WORKSPACE = "22222222-2222-4222-8222-222222222222"
_THREAD = "33333333-3333-4333-8333-333333333333"
_AUTOMATION = "55555555-5555-4555-8555-555555555555"
_EXECUTION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_RUN_1 = "44444444-4444-4444-8444-444444444441"
_RUN_2 = "44444444-4444-4444-8444-444444444442"
_RUN_3 = "44444444-4444-4444-8444-444444444443"
_USER_RUN = "44444444-4444-4444-8444-444444444499"


class _CapturingCursor:
    def __init__(self, *, fetchone_value=None, fetchall_value=None, rowcount: int = 1):
        self.calls: list[tuple[str, object]] = []
        self._fetchone = fetchone_value
        self._fetchall = fetchall_value if fetchall_value is not None else []
        self.rowcount = rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))

    async def fetchone(self):
        return self._fetchone

    async def fetchall(self):
        return self._fetchall


class _FakeConn:
    def __init__(self, cur: _CapturingCursor):
        self._cur = cur

    def cursor(self, **kwargs):
        return self._cur

    @asynccontextmanager
    async def transaction(self):
        yield


def _pool_patch(target: str, cur: _CapturingCursor):
    @asynccontextmanager
    async def _get_db_connection():
        yield _FakeConn(cur)

    return patch(target, new=_get_db_connection)


def _automation(*, timeout_seconds=None):
    auto = {
        "automation_id": _AUTOMATION,
        "user_id": _USER,
        "workspace_id": _WORKSPACE,
        "trigger_type": "cron",
        "name": "ai-finance-daily",
        "metadata": {},
    }
    if timeout_seconds is not None:
        auto["metadata"] = {"timeout_seconds": timeout_seconds}
    return auto


def _exec_row(*, status="running", run_id=_RUN_1, started_ago=timedelta(minutes=1)):
    now = datetime.now(timezone.utc)
    return {
        "automation_execution_id": _EXECUTION,
        "automation_id": _AUTOMATION,
        "status": status,
        "conversation_thread_id": _THREAD,
        "conversation_response_id": run_id,
        "started_at": now - started_ago,
        "created_at": now - started_ago,
        "heartbeat_at": now,
        "user_id": _USER,
        "workspace_id": _WORKSPACE,
    }


def _finalize_job(run_id=_RUN_1):
    return {
        "hook_outbox_id": "job-1",
        "run_id": run_id,
        "conversation_thread_id": _THREAD,
        "hook_type": "automation_settle",
        "payload": {
            "execution_id": _EXECUTION,
            "automation_id": _AUTOMATION,
            "user_id": _USER,
            "workspace_id": _WORKSPACE,
        },
        "attempts": 0,
    }


class _CapturedConfig(Exception):
    """Stop workflow immediately after build_graph_config returns."""


async def _run_ptc_through_graph_config(
    *,
    request: ChatRequest,
    run_id: str,
    take_over_mock: AsyncMock | None = None,
) -> dict[str, object]:
    captured: list[dict[str, object]] = []
    real_build_graph_config = ptc_run.build_graph_config

    def _capture_build_graph_config(*args, **kwargs):
        cfg = real_build_graph_config(*args, **kwargs)
        captured.append(cfg)
        return cfg

    config = SimpleNamespace(
        llm=SimpleNamespace(name="claude-test"),
        subagents=SimpleNamespace(enabled=False),
        claude_code_admitted=None,
        claude_code=False,
        user_skill_dir=None,
        disabled_skills=None,
        filesystem=SimpleNamespace(enabled_tools=None),
    )
    session_mock = create_autospec(Session, instance=True)
    session_mock.sandbox = None
    ws_manager_instance = create_autospec(WorkspaceManager, instance=True)
    ws_manager_instance.has_ready_session.return_value = True
    ws_manager_instance.get_session_for_workspace.return_value = session_mock
    ws_manager_instance.restore_sibling_folders.return_value = None
    ws_manager_cls = create_autospec(WorkspaceManager)
    ws_manager_cls.get_instance.return_value = ws_manager_instance

    registry_store_instance = create_autospec(ptc_run.BackgroundRegistryStore, instance=True)
    registry_store_instance.get_or_create_registry.return_value = SimpleNamespace()
    registry_store_cls = create_autospec(ptc_run.BackgroundRegistryStore)
    registry_store_cls.get_instance.return_value = registry_store_instance

    setup_mock = SimpleNamespace(
        agent_config=SimpleNamespace(
            tool_exposure_mode="ptc",
            enabled_tools=None,
            subagents=SimpleNamespace(enabled=False),
        )
    )
    run_handle = SimpleNamespace(
        turn_index=2,
        finalized=False,
        tail_turn_no_new_work=False,
        checkpointer=None,
        guard=None,
    )
    inputs_mock = create_autospec(AgentInputs, instance=True)
    inputs_mock.project = SimpleNamespace(workspace_root="/tmp/ws")
    inputs_mock.direct_mcp = None
    inputs_mock.build_kwargs.return_value = {}
    take_over = take_over_mock or create_autospec(
        automation_settlement.take_over_firing, return_value=None
    )
    start_conn = object()

    async def _fake_begin_run(*_args, **kwargs):
        if kwargs.get("in_start") is not None:
            await kwargs["in_start"](start_conn)
        return run_handle

    patches = [
        patch(f"{_PTC}.setup", setup_mock),
        patch(f"{_PTC}.WorkspaceManager", ws_manager_cls),
        patch(f"{_PTC}.BackgroundRegistryStore", registry_store_cls),
        patch(f"{_PTC}.ensure_thread", create_autospec(ptc_run.ensure_thread, return_value=request_prep.PriorThread())),
        patch(f"{_PTC}.wait_or_steer", create_autospec(ptc_run.wait_or_steer, return_value=(True, None))),
        patch(f"{_PTC}.read_disk_notice", create_autospec(ptc_run.read_disk_notice, return_value=(None, False))),
        patch(f"{_PTC}.get_user_profile_for_prompt", create_autospec(ptc_run.get_user_profile_for_prompt, return_value=None)),
        patch(f"{_PTC}._resolve_fork", create_autospec(ptc_run._resolve_fork, return_value=("system", None))),
        patch(f"{_PTC}._resolve_origin_meta", create_autospec(ptc_run._resolve_origin_meta, return_value={})),
        patch(f"{_PTC}.begin_run", create_autospec(ptc_run.begin_run, side_effect=_fake_begin_run)),
        patch(f"{_PTC}.keep_named_model", create_autospec(ptc_run.keep_named_model, return_value=None)),
        patch(f"{_PTC}.init_tracking", create_autospec(ptc_run.init_tracking, return_value=(object(), object()))),
        patch(f"{_PTC}.build_run_credit_gate", create_autospec(ptc_run.build_run_credit_gate, return_value=None)),
        patch(f"{_PTC}.apply_fetch_override", create_autospec(ptc_run.apply_fetch_override, return_value=None)),
        patch(f"{_PTC}.update_workspace_activity", create_autospec(ptc_run.update_workspace_activity, return_value=None)),
        patch(f"{_PTC}.build_ptc_graph_with_session", create_autospec(ptc_run.build_ptc_graph_with_session, return_value=object())),
        patch(f"{_PTC}.turn_start_checkpoint_id", create_autospec(ptc_run.turn_start_checkpoint_id, return_value=None)),
        patch(
            f"{_PTC}._fire_and_forget",
            create_autospec(
                ptc_run._fire_and_forget,
                side_effect=lambda coro, **_kw: coro.close() if hasattr(coro, "close") else None,
            ),
        ),
        patch(f"{_PTC}.build_graph_config", create_autospec(ptc_run.build_graph_config, side_effect=_capture_build_graph_config)),
        patch(f"{_PTC}.open_with_steers_ahead", create_autospec(ptc_run.open_with_steers_ahead, side_effect=_CapturedConfig())),
        patch.object(carry, "carried_pair", create_autospec(carry.carried_pair, return_value={})),
        patch.object(carry, "carried_delivery", create_autospec(carry.carried_delivery, return_value=None)),
        patch.object(automation_settlement, "take_over_firing", take_over),
        patch.object(automation_delivery, "turn_metadata", create_autospec(automation_delivery.turn_metadata, return_value={})),
        patch.object(user_skills, "skills_delivery_signature", create_autospec(user_skills.skills_delivery_signature, return_value="sig")),
        patch("src.server.services.thread_agent.AgentInputs.read", create_autospec(AgentInputs.read, return_value=inputs_mock)),
        patch("src.server.services.egress.direct_tools.order_ledger_for_turn", create_autospec(order_ledger_for_turn, return_value=None)),
        patch.object(cancel_notice, "ThreadCancelNotices", create_autospec(cancel_notice.ThreadCancelNotices)),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        gen = ptc_run.astream_ptc_workflow(
            request=request,
            thread_id=_THREAD,
            run_id=run_id,
            user_input="continue",
            user_id=_USER,
            route=TurnRoute(_WORKSPACE, "analyst"),
            is_byok=False,
            config=config,
        )
        try:
            async for _ in gen:
                pass
        except _CapturedConfig:
            pass
        finally:
            await gen.aclose()
    assert len(captured) == 1
    return captured[0]


# ---------------------------------------------------------------------------
# 1. Execution stays open while background commands / subagents remain open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_turn_with_open_background_work_keeps_execution_running_until_all_finish():
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    exec_row = _exec_row(run_id=_RUN_1)
    run_1 = {"conversation_response_id": _RUN_1, "status": "completed", "metadata": {}}
    run_2 = {"conversation_response_id": _RUN_2, "status": "completed", "metadata": {}}

    with (
        patch(f"{_SETTLE_MOD}.auto_db.get_automation_including_deleted", create_autospec(automation_settlement.auto_db.get_automation_including_deleted, return_value=_automation())),
        patch(f"{_SETTLE_MOD}.exec_db.get_execution_for_settlement", create_autospec(exec_db.get_execution_for_settlement, return_value=exec_row)),
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, side_effect=[run_1, run_2])),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, side_effect=[True, False])),
        patch(f"{_SETTLE_MOD}.read_run_answer", create_autospec(automation_settlement.read_run_answer, return_value=automation_settlement.RunAnswer("Done"))),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        # Turn 1 ends while command/subagent is still running -> stays open
        await _settle_finished_run(_finalize_job(_RUN_1))
        settle_mock.assert_not_awaited()

        # Continuation turn finishes once all background work is done -> settles completed
        exec_row["conversation_response_id"] = _RUN_2
        await _settle_finished_run(_finalize_job(_RUN_2))
        settle_mock.assert_awaited_once()
        assert settle_mock.await_args.args[2] is Outcome.COMPLETED
        assert settle_mock.await_args.kwargs["run_id"] == _RUN_2


@pytest.mark.asyncio
async def test_sweep_leaves_open_execution_with_pending_background_work_alone():
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    with (
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value={"status": "completed", "metadata": {}})),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, return_value=True)),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        outcome = await settle_abandoned(_automation(), _exec_row(run_id=_RUN_1), 300)
    assert outcome is None
    settle_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_report_without_turn_triggers_idle_execution_settlement():
    idle_settle = create_autospec(settle_thread_execution_if_idle, return_value=Outcome.COMPLETED)
    wake = create_autospec(subagent_rb.publish_cleared_wake, return_value=None)
    with (
        patch("src.server.services.report_back.subagent.publish_cleared_wake", wake),
        patch("src.server.services.automation_settlement.settle_thread_execution_if_idle", idle_settle),
    ):
        await command_rb._on_command_report_back_closed(
            {"conversation_thread_id": _THREAD, "payload": {"command_id": "bg-0123456789ab"}}
        )
        await subagent_rb._on_subagent_report_back_closed(
            {"conversation_thread_id": _THREAD, "payload": {"task_run_id": "tr-1"}}
        )
    assert idle_settle.await_count == 2


# ---------------------------------------------------------------------------
# 2. Continuation turns inherit identity; manual user turns on same thread do not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report_source, db_fn_name",
    [
        (CommandReport(kind="command_report", command_id="bg-0123456789ab", tool="ExecuteCode", status="completed"), "get_open_execution_for_command"),
        (TaskReport(kind="task", task_id="task-1", task_run_id="tr-task-1", display_id="Task-1", failed=False), "get_open_execution_for_task"),
    ],
)
async def test_continuation_turn_inherits_identity_and_passes_delivery_tool_checks(report_source, db_fn_name):
    request = ChatRequest(
        workspace_id=_WORKSPACE,
        messages=[],
        query_type="system",
        report_source=report_source,
        timezone="UTC",
        locale="en-US",
    )
    open_row = {"automation_execution_id": _EXECUTION, "automation_id": _AUTOMATION}
    taken_over: dict[str, object] = {}

    async def _take_over(execution_id: str, run_id: str, *, conn=None) -> None:
        taken_over["execution_id"] = execution_id
        taken_over["run_id"] = run_id
        taken_over["in_tx"] = conn is not None

    take_over_mock = create_autospec(automation_settlement.take_over_firing, side_effect=_take_over)

    with (
        patch("src.server.database.runs.lifecycle.get_latest_attempt", AsyncMock(return_value={"status": "completed", "metadata": {}})),
        patch(f"src.server.database.automation_executions.{db_fn_name}", AsyncMock(return_value=open_row)),
    ):
        graph_config = await _run_ptc_through_graph_config(
            request=request,
            run_id=_RUN_2,
            take_over_mock=take_over_mock,
        )

    assert taken_over == {"execution_id": _EXECUTION, "run_id": _RUN_2, "in_tx": True}
    cfg = graph_config["configurable"]
    assert cfg["turn_origin_automation_id"] == _AUTOMATION
    assert cfg["automation_execution_id"] == _EXECUTION

    # Delivery tools (automation_report & ai_finance) read configurable and verify DB row
    tool_ident = report_tools._server_identity(graph_config)
    assert tool_ident is not None
    db_ident = reports_db._clean_identity(
        user_id=tool_ident["user_id"],
        workspace_id=tool_ident["workspace_id"],
        thread_id=tool_ident["thread_id"],
        run_id=tool_ident["run_id"],
        automation_id=tool_ident["turn_origin_automation_id"],
        execution_id=tool_ident["automation_execution_id"],
    )
    assert reports_db._early_identity_code(db_ident) is None

    locked_exec_row = {
        "automation_execution_id": _EXECUTION,
        "automation_id": _AUTOMATION,
        "user_id": _USER,
        "workspace_id": _WORKSPACE,
        "conversation_thread_id": _THREAD,
        "conversation_response_id": _RUN_2,
        "status": "running",
        "scheduled_at": datetime(2026, 11, 15, 6, 0, tzinfo=timezone.utc),
        "started_at": datetime(2026, 11, 15, 6, 1, tzinfo=timezone.utc),
        "metadata": {
            "report_profile": {
                "profile": "ai-finance-daily",
                "report_targets": ["feishu:chat:oc_test"],
                "failure_targets": ["discord:user:12345"],
            }
        },
    }
    assert reports_db._identity_code(locked_exec_row, db_ident) is None

    cur = _CapturingCursor(fetchone_value=locked_exec_row)
    with patch("src.dalton.database.ai_finance.lock_report_execution", AsyncMock(return_value=locked_exec_row)):
        auth_res = await ai_finance_db.authorize_ai_finance_execution(
            user_id=db_ident["user_id"],
            workspace_id=db_ident["workspace_id"],
            thread_id=db_ident["thread_id"],
            run_id=db_ident["run_id"],
            automation_id=db_ident["automation_id"],
            execution_id=db_ident["execution_id"],
            conn=_FakeConn(cur),
        )
    assert auth_res["ok"] is True


@pytest.mark.asyncio
async def test_manual_user_message_on_open_thread_gets_no_automation_identity():
    manual_request = ChatRequest(
        workspace_id=_WORKSPACE,
        messages=[],
        query_type=None,
        report_source=None,
        timezone="UTC",
        locale="en-US",
    )
    take_over_mock = create_autospec(automation_settlement.take_over_firing)

    with (
        patch("src.server.database.automation_executions.get_open_execution_for_command", AsyncMock(return_value={"automation_execution_id": _EXECUTION, "automation_id": _AUTOMATION})) as cmd_lookup,
        patch("src.server.database.automation_executions.get_open_execution_for_task", AsyncMock(return_value={"automation_execution_id": _EXECUTION, "automation_id": _AUTOMATION})) as task_lookup,
    ):
        graph_config = await _run_ptc_through_graph_config(
            request=manual_request,
            run_id=_USER_RUN,
            take_over_mock=take_over_mock,
        )

    cmd_lookup.assert_not_awaited()
    task_lookup.assert_not_awaited()
    take_over_mock.assert_not_awaited()
    cfg = graph_config["configurable"]
    assert "turn_origin_automation_id" not in cfg
    assert "automation_execution_id" not in cfg
    assert report_tools._server_identity(graph_config) is None


@pytest.mark.asyncio
async def test_sql_only_resolves_running_execution_on_same_thread():
    cur = _CapturingCursor(fetchone_value=None)
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur):
        assert await exec_db.get_open_execution_for_command(_THREAD, "bg-0123456789ab") is None
        assert await exec_db.get_open_execution_for_task(_THREAD, "tr-task-1") is None

    cmd_sql = cur.calls[0][0]
    task_sql = cur.calls[1][0]
    assert "e.conversation_thread_id = %s::uuid" in cmd_sql
    assert "e.status = 'running'" in cmd_sql
    assert "e.automation_execution_id::text = pr.metadata->>'automation_execution_id'" in cmd_sql
    assert "e.conversation_thread_id = %s::uuid" in task_sql
    assert "e.status = 'running'" in task_sql
    assert "sr.task_run_id = %s::uuid" in task_sql
    assert "e.automation_execution_id::text = pr.metadata->>'automation_execution_id'" in task_sql
    assert "ORDER BY sr.started_at DESC" not in task_sql


# ---------------------------------------------------------------------------
# 3. Hard timeout cap (configured timeout or max(3h, workflow_timeout))
# ---------------------------------------------------------------------------


def test_execution_timeout_defaults_to_three_hours_and_honors_automation_override():
    # Platform config.yaml sets workflow_timeout=21600 (> 3h min 10800)
    assert execution_timeout_seconds(_automation()) == 21600.0
    assert execution_timeout_seconds(_automation(timeout_seconds=600)) == 600.0
    with patch("src.config.settings.get_workflow_timeout", return_value=3600):
        assert exec_db.default_execution_timeout_seconds() == float(exec_db.EXECUTION_TIMEOUT_MIN_SECONDS) == 10800.0
        assert execution_timeout_seconds(_automation(timeout_seconds=0)) == 10800.0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auto_timeout, started_ago",
    [
        (None, timedelta(hours=6, seconds=1)),
        (900, timedelta(seconds=901)),
    ],
    ids=["default_workflow_timeout_6h", "custom_900s"],
)
async def test_sweep_settles_timed_out_execution_through_failure_path(auto_timeout, started_ago):
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    timed_out_row = _exec_row(run_id=_RUN_1, started_ago=started_ago)
    with (
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value={"status": "completed", "metadata": {}})),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, return_value=True)),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        outcome = await settle_abandoned(
            _automation(timeout_seconds=auto_timeout), timed_out_row, 300
        )

    assert outcome is Outcome.TIMEOUT
    settle_mock.assert_awaited_once()
    assert settle_mock.await_args.args[2] is Outcome.TIMEOUT
    assert settle_mock.await_args.kwargs["error"] == TIMEOUT_ERROR
    assert settle_mock.await_args.kwargs["quiet_for"] is None


@pytest.mark.asyncio
async def test_finalize_settles_timed_out_execution_as_timeout():
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    timed_out_row = _exec_row(run_id=_RUN_1, started_ago=timedelta(hours=6, minutes=5))
    with (
        patch(f"{_SETTLE_MOD}.auto_db.get_automation_including_deleted", create_autospec(automation_settlement.auto_db.get_automation_including_deleted, return_value=_automation())),
        patch(f"{_SETTLE_MOD}.exec_db.get_execution_for_settlement", create_autospec(exec_db.get_execution_for_settlement, return_value=timed_out_row)),
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value={"status": "completed", "metadata": {}})),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, return_value=True)),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        await _settle_finished_run(_finalize_job(_RUN_1))
    assert settle_mock.await_args.args[2] is Outcome.TIMEOUT


# ---------------------------------------------------------------------------
# 4. Multiple background jobs & concurrent / out-of-order protection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_background_tasks_only_settle_after_final_continuation_and_ignore_stale_finalize():
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    # Turn 1 finishes while S1 and C1 are active.
    # Continuation Turn 2 finishes while C1 is still pending.
    # Stale finalize for Turn 2 after Turn 3 already took over is ignored.
    # Turn 3 finishes with nothing pending -> settles with run_id=_RUN_3.
    exec_rows = [
        _exec_row(run_id=_RUN_1),
        _exec_row(run_id=_RUN_2),
        _exec_row(run_id=_RUN_3),  # Turn 3 already claimed conversation_response_id
        _exec_row(run_id=_RUN_3),
    ]
    run_done = {"status": "completed", "metadata": {}}
    with (
        patch(f"{_SETTLE_MOD}.auto_db.get_automation_including_deleted", create_autospec(automation_settlement.auto_db.get_automation_including_deleted, return_value=_automation())),
        patch(f"{_SETTLE_MOD}.exec_db.get_execution_for_settlement", create_autospec(exec_db.get_execution_for_settlement, side_effect=exec_rows)),
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value=run_done)),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, side_effect=[True, True, False])),
        patch(f"{_SETTLE_MOD}.read_run_answer", create_autospec(automation_settlement.read_run_answer, return_value=automation_settlement.RunAnswer("Final brief"))),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        await _settle_finished_run(_finalize_job(_RUN_1))
        await _settle_finished_run(_finalize_job(_RUN_2))
        # Stale/out-of-order finalize for _RUN_2 after _RUN_3 took over:
        await _settle_finished_run(_finalize_job(_RUN_2))
        settle_mock.assert_not_awaited()

        # Final continuation turn (_RUN_3):
        await _settle_finished_run(_finalize_job(_RUN_3))
        settle_mock.assert_awaited_once()
        assert settle_mock.await_args.args[2] is Outcome.COMPLETED
        assert settle_mock.await_args.kwargs["run_id"] == _RUN_3


@pytest.mark.asyncio
async def test_take_over_firing_uses_run_seq_and_in_progress_db_guard(caplog):
    cur = _CapturingCursor(rowcount=0)
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur):
        with caplog.at_level(logging.WARNING):
            await take_over_firing(_EXECUTION, _RUN_2)

    lock_sql, lock_params = cur.calls[0]
    sql, params = cur.calls[1]
    assert "FOR UPDATE" in lock_sql
    assert lock_params == {"execution_id": _EXECUTION}
    assert "e.status = 'running'" in sql
    assert "r.status = 'in_progress'" in sql
    assert "r.conversation_thread_id = e.conversation_thread_id" in sql
    assert "cur.run_seq > cand.run_seq" in sql
    assert params == {"execution_id": _EXECUTION, "run_id": _RUN_2}
    assert f"execution_id={_EXECUTION}" in caplog.text
    assert f"run_id={_RUN_2}" in caplog.text


# ---------------------------------------------------------------------------
# 5. Round 2 Review Fixes (R1-R7)
# ---------------------------------------------------------------------------


class _ScriptedCursor:
    """Sequential scripted cursor supporting multi-query transactions."""

    def __init__(self, steps: list[dict[str, object]]):
        self._steps = list(steps)
        self.calls: list[tuple[str, object]] = []
        self._current_step: dict[str, object] = {}
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if not self._steps:
            raise AssertionError(f"Unexpected SQL call #{len(self.calls)}: {sql}")
        self._current_step = self._steps.pop(0)
        self.rowcount = int(self._current_step.get("rowcount", 1))

    async def fetchone(self):
        return self._current_step.get("fetchone")

    async def fetchall(self):
        return self._current_step.get("fetchall", [])


@pytest.mark.asyncio
async def test_r1_psycopg_sql_explicit_casts_for_nullable_parameters():
    """R1: Every nullable parameter in added/modified SQL binds validly in psycopg and casts IS NULL."""
    cap_queries: list[tuple[str, object]] = []

    pending_cur = _CapturingCursor(fetchone_value={"exists": False})
    with _pool_patch("src.server.database.automation_executions.get_db_connection", pending_cur):
        await exec_db.has_pending_background_work(_EXECUTION, thread_id=_THREAD, excluding_run_id=None)
        await exec_db.has_pending_background_work(_EXECUTION, thread_id=_THREAD, excluding_run_id=_RUN_1)
    cap_queries.extend(pending_cur.calls)

    lookup_cur = _CapturingCursor(fetchone_value=None, fetchall_value=[], rowcount=0)
    with _pool_patch("src.server.database.automation_executions.get_db_connection", lookup_cur):
        await exec_db.list_abandoned_executions(300, 50)
        await exec_db.get_execution_for_settlement(_EXECUTION)
        await exec_db.get_running_executions_for_thread(_THREAD)
        await exec_db.get_open_execution_for_command(_THREAD, "bg-1")
        await exec_db.get_open_execution_for_task(_THREAD, "tr-1")
        await exec_db.take_over_execution_run(_EXECUTION, _RUN_2)
    cap_queries.extend(lookup_cur.calls)

    lock_steps = [
        {"fetchone": {"1": 1}},
        {
            "fetchone": {
                "status": "running",
                "created_at": datetime.now(timezone.utc),
                "delivery": None,
                "conversation_thread_id": _THREAD,
                "conversation_response_id": None,
            }
        },
        {"fetchone": {"exists": False}},
        {"fetchone": None},
        {"rowcount": 1, "fetchone": {"conversation_thread_id": _THREAD, "conversation_response_id": None}},
    ]
    lock_cur = _ScriptedCursor(lock_steps)
    with _pool_patch("src.server.database.automation_executions.get_db_connection", lock_cur):
        settled = await exec_db.settle_execution(
            _EXECUTION,
            automation_id=_AUTOMATION,
            from_statuses=("running",),
            to="failed",
            expected_run_id=None,
            error_message="boom",
        )
    assert settled is not None
    assert settled["settled_from"] == "running"

    all_queries = cap_queries + lock_cur.calls
    assert len(all_queries) >= 10
    untyped_null_re = re.compile(r"\$\d+\s+(?:IS\s+(?:NOT\s+)?NULL|IS\s+(?:NOT\s+)?DISTINCT\s+FROM)", re.IGNORECASE)
    transformer = Transformer()
    for sql, params in all_queries:
        pq = PostgresQuery(transformer)
        pq.convert(sql, params)
        rendered = pq.query.decode("utf-8")
        bare = untyped_null_re.findall(rendered)
        assert not bare, f"Untyped nullable parameter(s) {bare} in query:\n{rendered}"
        assert "IS NOT DISTINCT FROM" not in rendered or not re.search(
            r"IS\s+(?:NOT\s+)?DISTINCT\s+FROM\s+\$\d+(?!::)", rendered, re.IGNORECASE
        ), f"Untyped RHS in IS NOT DISTINCT FROM:\n{rendered}"


@pytest.mark.asyncio
async def test_r2_in_progress_run_never_times_out_even_past_cap_and_sql_excludes_live_runs():
    """R2: Live in_progress runs are never marked timed out, even after 6h+."""
    assert not is_execution_timed_out(_exec_row(run_id=_RUN_1, started_ago=timedelta(hours=4)), _automation())
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    old_row = _exec_row(run_id=_RUN_1, started_ago=timedelta(hours=8))
    live_run = {"conversation_response_id": _RUN_1, "status": "in_progress", "metadata": {}}

    with (
        patch(f"{_SETTLE_MOD}.auto_db.get_automation_including_deleted", create_autospec(automation_settlement.auto_db.get_automation_including_deleted, return_value=_automation())),
        patch(f"{_SETTLE_MOD}.exec_db.get_execution_for_settlement", create_autospec(exec_db.get_execution_for_settlement, return_value=old_row)),
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value=live_run)),
        patch(f"{_SETTLE_MOD}.exec_db.get_running_executions_for_thread", create_autospec(exec_db.get_running_executions_for_thread, return_value=[old_row])),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        assert await settle_abandoned(_automation(), old_row, 300) is None
        await _settle_finished_run(_finalize_job(_RUN_1))
        assert await settle_thread_execution_if_idle(_THREAD) is None
    settle_mock.assert_not_awaited()

    cur = _CapturingCursor(fetchall_value=[])
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur):
        await exec_db.list_abandoned_executions(300, 50)
    sql = cur.calls[1][0]
    # NOT EXISTS (... in_progress ...) must guard the outer WHERE before the timeout/quiet OR block
    not_exists_pos = sql.index("NOT EXISTS (")
    timeout_pos = sql.index("make_interval(secs =>")
    assert not_exists_pos < timeout_pos
    assert "COALESCE(e.started_at, e.created_at)" in sql


@pytest.mark.asyncio
async def test_r3_completed_idle_execution_settles_completed_even_past_timeout_cap():
    """R3: Already completed run with no pending background work settles COMPLETED, not TIMEOUT."""
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    old_row = _exec_row(run_id=_RUN_1, started_ago=timedelta(hours=8))
    done_run = {"conversation_response_id": _RUN_1, "status": "completed", "metadata": {}}

    with (
        patch(f"{_SETTLE_MOD}.auto_db.get_automation_including_deleted", create_autospec(automation_settlement.auto_db.get_automation_including_deleted, return_value=_automation())),
        patch(f"{_SETTLE_MOD}.exec_db.get_execution_for_settlement", create_autospec(exec_db.get_execution_for_settlement, return_value=old_row)),
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value=done_run)),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, return_value=False)),
        patch(f"{_SETTLE_MOD}.read_run_answer", create_autospec(automation_settlement.read_run_answer, return_value=automation_settlement.RunAnswer("Report OK"))),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        await _settle_finished_run(_finalize_job(_RUN_1))
        sweep_outcome = await settle_abandoned(_automation(), old_row, 300)

    assert sweep_outcome is Outcome.COMPLETED
    assert settle_mock.await_count == 2
    for call in settle_mock.await_args_list:
        assert call.args[2] is Outcome.COMPLETED
        assert call.kwargs["check_run_state_in_lock"] == "completed_idle"
        assert call.kwargs["quiet_for"] is None


@pytest.mark.asyncio
async def test_r4_settle_execution_aborts_under_for_update_when_run_swapped_live_or_busy():
    """R4: Under FOR UPDATE lock, concurrent takeover or in_progress/pending state aborts settlement."""
    # Case 1: Turn B took over conversation_response_id before FOR UPDATE acquired
    cur_swapped = _ScriptedCursor([
        {"fetchone": {"1": 1}},
        {
            "fetchone": {
                "status": "running",
                "created_at": datetime.now(timezone.utc),
                "delivery": None,
                "conversation_response_id": _RUN_2,
                "conversation_thread_id": _THREAD,
            }
        },
    ])
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur_swapped):
        res = await exec_db.settle_execution(
            _EXECUTION,
            automation_id=_AUTOMATION,
            from_statuses=("running",),
            to="completed",
            expected_run_id=_RUN_1,
            check_run_state_in_lock="completed_idle",
        )
    assert res is None
    assert len(cur_swapped.calls) == 2
    assert "FOR UPDATE" in cur_swapped.calls[1][0]

    # Case 2: Re-read under lock sees linked run transitioned back/live to in_progress
    cur_live = _ScriptedCursor([
        {"fetchone": {"1": 1}},
        {
            "fetchone": {
                "status": "running",
                "created_at": datetime.now(timezone.utc),
                "delivery": None,
                "conversation_response_id": _RUN_1,
                "conversation_thread_id": _THREAD,
            }
        },
        {"fetchone": {"exists": False}},
        {"fetchone": {"status": "in_progress", "metadata": {}, "cancel_requested_at": None}},
    ])
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur_live):
        res = await exec_db.settle_execution(
            _EXECUTION,
            automation_id=_AUTOMATION,
            from_statuses=("running",),
            to="failed",
            expected_run_id=_RUN_1,
            check_run_state_in_lock="timeout",
        )
    assert res is None
    assert len(cur_live.calls) == 4

    # Case 3: Re-read under lock finds background work pending for completed_idle
    cur_busy = _ScriptedCursor([
        {"fetchone": {"1": 1}},
        {
            "fetchone": {
                "status": "running",
                "created_at": datetime.now(timezone.utc),
                "delivery": None,
                "conversation_response_id": _RUN_1,
                "conversation_thread_id": _THREAD,
            }
        },
        {"fetchone": {"exists": False}},
        {"fetchone": {"status": "completed", "metadata": {}, "cancel_requested_at": None}},
        {"fetchone": {"exists": True}},
    ])
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur_busy):
        res = await exec_db.settle_execution(
            _EXECUTION,
            automation_id=_AUTOMATION,
            from_statuses=("running",),
            to="completed",
            expected_run_id=_RUN_1,
            check_run_state_in_lock="completed_idle",
        )
    assert res is None
    assert len(cur_busy.calls) == 5

    # Case 4: CAS mismatch on final UPDATE (IS NOT DISTINCT FROM %s::uuid returns no row)
    cur_cas = _ScriptedCursor([
        {"fetchone": {"1": 1}},
        {
            "fetchone": {
                "status": "running",
                "created_at": datetime.now(timezone.utc),
                "delivery": None,
                "conversation_response_id": _RUN_1,
                "conversation_thread_id": _THREAD,
            }
        },
        {"fetchone": {"exists": False}},
        {"fetchone": {"status": "completed", "metadata": {}, "cancel_requested_at": None}},
        {"fetchone": {"exists": False}},
        {"fetchone": None},
        {"rowcount": 0, "fetchone": None},
    ])
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur_cas):
        res = await exec_db.settle_execution(
            _EXECUTION,
            automation_id=_AUTOMATION,
            from_statuses=("running",),
            to="completed",
            expected_run_id=_RUN_1,
            check_run_state_in_lock="completed_idle",
        )
    assert res is None
    assert "conversation_response_id IS NOT DISTINCT FROM %s::uuid" in cur_cas.calls[-1][0]


@pytest.mark.asyncio
async def test_r5_subagent_task_report_carries_exact_task_run_id_without_task_id_crosstalk():
    """R5: Subagent continuation carries exact task_run_id so reused task_id resolves right firing."""
    payload = {
        "task_id": "task-shared-1",
        "task_run_id": "task-run-old-1",
        "tool_call_id": "tc-1",
        "status": "completed",
        "role": "equity-analyst",
        "summary": "Done",
    }
    report = subagent_rb.task_report(payload)
    assert isinstance(report, TaskReport)
    assert report.task_run_id == "task-run-old-1"

    open_exec_old = {"automation_execution_id": _EXECUTION, "automation_id": _AUTOMATION}

    async def _fake_lookup(thread_id: str, task_run_id: str):
        return open_exec_old if task_run_id == "task-run-old-1" else None

    with patch("src.server.database.automation_executions.get_open_execution_for_task", side_effect=_fake_lookup) as mock_fn:
        hit = await carry._report_source_firing(
            SimpleNamespace(report_source=report),
            _THREAD,
        )
        miss = await carry._report_source_firing(
            SimpleNamespace(report_source={"kind": "task", "task_id": "task-shared-1", "task_run_id": "task-run-user-2"}),
            _THREAD,
        )

    assert hit == open_exec_old
    assert miss == {}
    assert mock_fn.await_args_list[0].args == (_THREAD, "task-run-old-1")
    assert mock_fn.await_args_list[1].args == (_THREAD, "task-run-user-2")


@pytest.mark.asyncio
async def test_r6_settle_thread_execution_if_idle_scans_all_running_executions():
    """R6: Busy execution on a shared thread does not block later idle executions from settling."""
    exec_busy = {**_exec_row(run_id=_RUN_1), "automation_execution_id": "exec-busy", "automation_id": "auto-busy"}
    exec_idle = {**_exec_row(run_id=_RUN_2), "automation_execution_id": "exec-idle", "automation_id": "auto-idle"}
    auto_busy = {**_automation(), "automation_id": "auto-busy"}
    auto_idle = {**_automation(), "automation_id": "auto-idle"}
    run_done = {"status": "completed", "metadata": {}}
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)

    with (
        patch(f"{_SETTLE_MOD}.exec_db.get_running_executions_for_thread", create_autospec(exec_db.get_running_executions_for_thread, return_value=[exec_busy, exec_idle])),
        patch(f"{_SETTLE_MOD}.auto_db.get_automation_including_deleted", create_autospec(automation_settlement.auto_db.get_automation_including_deleted, side_effect=[auto_busy, auto_idle])),
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value=run_done)),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, side_effect=[True, False])),
        patch(f"{_SETTLE_MOD}.read_run_answer", create_autospec(automation_settlement.read_run_answer, return_value=automation_settlement.RunAnswer("Idle finished"))),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        outcome = await settle_thread_execution_if_idle(_THREAD)

    assert outcome is Outcome.COMPLETED
    settle_mock.assert_awaited_once()
    assert settle_mock.await_args.args[1] == "exec-idle"
    assert settle_mock.await_args.kwargs["run_id"] == _RUN_2


@pytest.mark.asyncio
async def test_s1_settle_aborts_when_live_continuation_in_progress_even_past_timeout_and_before_takeover():
    """S1: Any live continuation run on the thread for this execution aborts settlement under FOR UPDATE."""
    for lock_mode, target_status in (("timeout", "failed"), (None, "failed"), ("completed_idle", "completed")):
        cur_live_cont = _ScriptedCursor([
            {"fetchone": {"1": 1}},
            {
                "fetchone": {
                    "status": "running",
                    "created_at": datetime.now(timezone.utc) - timedelta(hours=8),
                    "delivery": None,
                    "conversation_response_id": _RUN_1,
                    "conversation_thread_id": _THREAD,
                }
            },
            # Turn B already in_progress in conversation_responses before updating conversation_response_id
            {"fetchone": {"exists": True}},
        ])
        with _pool_patch("src.server.database.automation_executions.get_db_connection", cur_live_cont):
            res = await exec_db.settle_execution(
                _EXECUTION,
                automation_id=_AUTOMATION,
                from_statuses=("running",),
                to=target_status,
                expected_run_id=_RUN_1,
                check_run_state_in_lock=lock_mode,
            )
        assert res is None
        assert len(cur_live_cont.calls) == 3
        cont_sql, cont_params = cur_live_cont.calls[2]
        assert "r_live.status = 'in_progress'" in cont_sql
        assert "r_live.metadata->>'automation_execution_id' = %s::text" in cont_sql
        assert cont_params == (_THREAD, _EXECUTION)

    # list_abandoned_executions also excludes any execution with a live continuation run on the thread
    cur_sweep = _CapturingCursor(fetchall_value=[])
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur_sweep):
        await exec_db.list_abandoned_executions(300, 50)
    sweep_sql = cur_sweep.calls[1][0]
    assert "r.metadata->>'automation_execution_id' = e.automation_execution_id::text" in sweep_sql


@pytest.mark.asyncio
async def test_s1_start_locks_execution_and_takes_over_inside_same_transaction():
    """S1: Continuation START locks execution FOR UPDATE and takes over in_start tx; B later settles cleanly."""
    tx_cursor = _CapturingCursor(rowcount=1)

    class _TxConn:
        def cursor(self, **_kw):
            return tx_cursor

    request = ChatRequest(
        workspace_id=_WORKSPACE,
        messages=[],
        query_type="system",
        report_source=CommandReport(
            kind="command_report",
            command_id="bg-0123456789ab",
            tool="ExecuteCode",
            status="completed",
        ),
        timezone="UTC",
        locale="en-US",
    )
    events: list[str] = []
    tx_conn = _TxConn()
    original_take_over = automation_settlement.take_over_firing
    real_build_graph_config = ptc_run.build_graph_config

    async def _run_inside_begin(*_args, **kwargs):
        events.append("begin_run_enter")
        assert kwargs.get("in_start") is not None
        await kwargs["in_start"](tx_conn)
        events.append("begin_run_commit")
        return SimpleNamespace(
            turn_index=2,
            finalized=False,
            tail_turn_no_new_work=False,
            checkpointer=None,
            guard=None,
        )

    async def _wrapped_take_over(execution_id: str, run_id: str, *, conn=None):
        events.append("in_start_take_over")
        assert conn is tx_conn
        await original_take_over(execution_id, run_id, conn=conn)

    with (
        patch("src.server.database.automation_executions.get_open_execution_for_command", AsyncMock(return_value={"automation_execution_id": _EXECUTION, "automation_id": _AUTOMATION})),
        patch("src.server.database.runs.lifecycle.get_latest_attempt", AsyncMock(return_value={"status": "completed", "metadata": {}})),
        patch(f"{_PTC}.begin_run", side_effect=_run_inside_begin),
        patch.object(automation_settlement, "take_over_firing", side_effect=_wrapped_take_over),
    ):
        captured: list[dict[str, object]] = []
        session_mock = create_autospec(Session, instance=True)
        session_mock.sandbox = None
        ws_manager_instance = create_autospec(WorkspaceManager, instance=True)
        ws_manager_instance.has_ready_session.return_value = True
        ws_manager_instance.get_session_for_workspace.return_value = session_mock
        ws_manager_instance.restore_sibling_folders.return_value = None
        ws_manager_cls = create_autospec(WorkspaceManager)
        ws_manager_cls.get_instance.return_value = ws_manager_instance
        registry_store_instance = create_autospec(ptc_run.BackgroundRegistryStore, instance=True)
        registry_store_instance.get_or_create_registry.return_value = SimpleNamespace()
        registry_store_cls = create_autospec(ptc_run.BackgroundRegistryStore)
        registry_store_cls.get_instance.return_value = registry_store_instance
        setup_mock = SimpleNamespace(
            agent_config=SimpleNamespace(tool_exposure_mode="ptc", enabled_tools=None, subagents=SimpleNamespace(enabled=False))
        )
        inputs_mock = create_autospec(AgentInputs, instance=True)
        inputs_mock.project = SimpleNamespace(workspace_root="/tmp/ws")
        inputs_mock.direct_mcp = None
        inputs_mock.build_kwargs.return_value = {}
        config = SimpleNamespace(
            llm=SimpleNamespace(name="claude-test"),
            subagents=SimpleNamespace(enabled=False),
            claude_code_admitted=None,
            claude_code=False,
            user_skill_dir=None,
            disabled_skills=None,
            filesystem=SimpleNamespace(enabled_tools=None),
        )
        def _capture_cfg(*args, **kwargs):
            cfg = real_build_graph_config(*args, **kwargs)
            captured.append(cfg)
            return cfg

        with ExitStack() as stack:
            for p in (
                patch(f"{_PTC}.setup", setup_mock),
                patch(f"{_PTC}.WorkspaceManager", ws_manager_cls),
                patch(f"{_PTC}.BackgroundRegistryStore", registry_store_cls),
                patch(f"{_PTC}.ensure_thread", create_autospec(ptc_run.ensure_thread, return_value=request_prep.PriorThread())),
                patch(f"{_PTC}.wait_or_steer", create_autospec(ptc_run.wait_or_steer, return_value=(True, None))),
                patch(f"{_PTC}.read_disk_notice", create_autospec(ptc_run.read_disk_notice, return_value=(None, False))),
                patch(f"{_PTC}.get_user_profile_for_prompt", create_autospec(ptc_run.get_user_profile_for_prompt, return_value=None)),
                patch(f"{_PTC}._resolve_fork", create_autospec(ptc_run._resolve_fork, return_value=("system", None))),
                patch(f"{_PTC}._resolve_origin_meta", create_autospec(ptc_run._resolve_origin_meta, return_value={})),
                patch(f"{_PTC}.keep_named_model", create_autospec(ptc_run.keep_named_model, return_value=None)),
                patch(f"{_PTC}.init_tracking", create_autospec(ptc_run.init_tracking, return_value=(object(), object()))),
                patch(f"{_PTC}.build_run_credit_gate", create_autospec(ptc_run.build_run_credit_gate, return_value=None)),
                patch(f"{_PTC}.apply_fetch_override", create_autospec(ptc_run.apply_fetch_override, return_value=None)),
                patch(f"{_PTC}.update_workspace_activity", create_autospec(ptc_run.update_workspace_activity, return_value=None)),
                patch(f"{_PTC}.build_ptc_graph_with_session", create_autospec(ptc_run.build_ptc_graph_with_session, return_value=object())),
                patch(f"{_PTC}.turn_start_checkpoint_id", create_autospec(ptc_run.turn_start_checkpoint_id, return_value=None)),
                patch(f"{_PTC}._fire_and_forget", create_autospec(ptc_run._fire_and_forget, side_effect=lambda c, **_kw: c.close() if hasattr(c, "close") else None)),
                patch(f"{_PTC}.build_graph_config", create_autospec(ptc_run.build_graph_config, side_effect=_capture_cfg)),
                patch(f"{_PTC}.open_with_steers_ahead", create_autospec(ptc_run.open_with_steers_ahead, side_effect=_CapturedConfig())),
                patch.object(carry, "carried_pair", create_autospec(carry.carried_pair, return_value={})),
                patch.object(carry, "carried_delivery", create_autospec(carry.carried_delivery, return_value=None)),
                patch.object(automation_delivery, "turn_metadata", create_autospec(automation_delivery.turn_metadata, return_value={})),
                patch.object(user_skills, "skills_delivery_signature", create_autospec(user_skills.skills_delivery_signature, return_value="sig")),
                patch("src.server.services.thread_agent.AgentInputs.read", create_autospec(AgentInputs.read, return_value=inputs_mock)),
                patch("src.server.services.egress.direct_tools.order_ledger_for_turn", create_autospec(order_ledger_for_turn, return_value=None)),
                patch.object(cancel_notice, "ThreadCancelNotices", create_autospec(cancel_notice.ThreadCancelNotices)),
            ):
                stack.enter_context(p)
            gen = ptc_run.astream_ptc_workflow(
                request=request,
                thread_id=_THREAD,
                run_id=_RUN_2,
                user_input="continue",
                user_id=_USER,
                route=TurnRoute(_WORKSPACE, "analyst"),
                is_byok=False,
                config=config,
            )
            try:
                async for _ in gen:
                    pass
            except _CapturedConfig:
                pass
            finally:
                await gen.aclose()

    assert events == ["begin_run_enter", "in_start_take_over", "begin_run_commit"]
    assert len(tx_cursor.calls) == 2
    assert "FOR UPDATE" in tx_cursor.calls[0][0]
    assert "UPDATE automation_executions e" in tx_cursor.calls[1][0]

    # Once Turn B took over (_RUN_2) and completed with no pending work, B settles normally as COMPLETED
    settle_mock = create_autospec(automation_settlement.settle, return_value=True)
    exec_after_takeover = _exec_row(run_id=_RUN_2)
    run_b_done = {"conversation_response_id": _RUN_2, "status": "completed", "metadata": {}}
    with (
        patch(f"{_SETTLE_MOD}.auto_db.get_automation_including_deleted", create_autospec(automation_settlement.auto_db.get_automation_including_deleted, return_value=_automation())),
        patch(f"{_SETTLE_MOD}.exec_db.get_execution_for_settlement", create_autospec(exec_db.get_execution_for_settlement, return_value=exec_after_takeover)),
        patch(f"{_SETTLE_MOD}.exec_db.get_settling_run", create_autospec(exec_db.get_settling_run, return_value=run_b_done)),
        patch(f"{_SETTLE_MOD}.exec_db.has_pending_background_work", create_autospec(exec_db.has_pending_background_work, return_value=False)),
        patch(f"{_SETTLE_MOD}.read_run_answer", create_autospec(automation_settlement.read_run_answer, return_value=automation_settlement.RunAnswer("Report OK"))),
        patch(f"{_SETTLE_MOD}.settle", settle_mock),
    ):
        await _settle_finished_run(_finalize_job(_RUN_2))
    settle_mock.assert_awaited_once()
    assert settle_mock.await_args.args[2] is Outcome.COMPLETED
    assert settle_mock.await_args.kwargs["run_id"] == _RUN_2


@pytest.mark.asyncio
async def test_s2_timeout_parsing_single_source_and_strict_validation():
    """S2/R7: Single timeout parser accepts positive ints / lossless numeric strings, rejects junk, and feeds SQL."""
    assert automation_settlement.execution_timeout_seconds is exec_db.execution_timeout_seconds
    default_val = exec_db.default_execution_timeout_seconds()

    for raw in (" 600 ", "600", "600.0", 600, 600.0):
        assert execution_timeout_seconds({"metadata": {"timeout_seconds": raw}}) == 600.0

    for bad in ("6e2", "1e-3", "nan", "inf", "-5", -5, 0, "0", "0.0", 600.5, "600.5", "600s", "600.", "", True):
        assert execution_timeout_seconds({"metadata": {"timeout_seconds": bad}}) == default_val

    # trigger_config is consulted only if metadata has no valid timeout_seconds
    assert execution_timeout_seconds({"metadata": {"timeout_seconds": "nan"}, "trigger_config": {"timeout_seconds": " 900.0 "}}) == 900.0

    # SQL reads overrides once in Python and passes parsed seconds into unnest(%s::uuid[], %s::double precision[])
    override_rows = [
        {"automation_id": _AUTOMATION, "metadata": {"timeout_seconds": " 600 "}, "trigger_config": {}},
        {"automation_id": "66666666-6666-4666-8666-666666666666", "metadata": {"timeout_seconds": "6e2"}, "trigger_config": {}},
    ]
    cur = _ScriptedCursor([
        {"fetchall": override_rows},
        {"fetchall": []},
    ])
    with _pool_patch("src.server.database.automation_executions.get_db_connection", cur):
        await exec_db.list_abandoned_executions(300, 50)

    assert len(cur.calls) == 2
    main_sql, main_params = cur.calls[1]
    assert "unnest(%s::uuid[], %s::double precision[])" in main_sql
    assert main_params[0] == [_AUTOMATION, "66666666-6666-4666-8666-666666666666"]
    assert main_params[1] == [600.0, default_val]


