"""Unit tests for Batch 3: CoS manage_workspaces(action='archive'), snapshot precheck, and whitelisted logs."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest
from langchain_core.messages import AIMessage

from ptc_agent.core.sandbox.runtime import SandboxTransientError
from src.server.database import computer as computer_db
from src.server.database import workspace as workspace_db
from src.server.database import workspace_folders
from src.server.handlers.checkpoint_handler import _snapshot_matches_action
from src.server.services import home_placement
from src.server.services.computer_errors import ArchiveScopeChangedError
from src.server.services.workspace_manager import WorkspaceManager
from src.tools.secretary import tools as secretary_tools
from src.tools.secretary._commands import puts_card
from src.tools.secretary.approvals import (
    StandingApprovalMiddleware,
    build_archive_workspace_snapshot,
)
from src.tools.secretary.chief_of_staff import chief_of_staff_workspaces

USER_ID = "user-cos-b3"
OTHER_USER = "user-other-b3"
COMP_DAYTONA = "11111111-1111-4111-8111-111111111111"
COMP_COS = "22222222-2222-4222-8222-222222222222"
WS_NVDA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WS_HOME = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
WS_STAND_IN = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _tool_call(**args: Any) -> dict[str, Any]:
    return {
        "name": "manage_workspaces",
        "args": args,
        "id": "call-archive-1",
        "type": "tool_call",
    }


def _cfg() -> dict[str, Any]:
    return {"configurable": {"user_id": USER_ID, "thread_id": "thread-1"}}


def _payload(cmd: Any) -> dict[str, Any]:
    return json.loads(cmd.update["messages"][0].content)


def _dummy_db_conn():
    class _Cur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _Conn:
        def cursor(self):
            return _Cur()

    @asynccontextmanager
    async def _get_conn():
        yield _Conn()

    return _get_conn


def test_archive_action_is_registered_as_confirmation_card():
    assert puts_card("manage_workspaces", {"action": "archive"}) is True


@pytest.mark.asyncio
async def test_archive_snapshot_precheck_rules(monkeypatch):
    ws_nvda = {
        "workspace_id": WS_NVDA,
        "user_id": USER_ID,
        "name": "NVDA",
        "status": "stopped",
        "config": {},
    }
    monkeypatch.setattr(
        workspace_db,
        "get_workspace",
        create_autospec(workspace_db.get_workspace, return_value=ws_nvda),
    )
    monkeypatch.setattr(
        home_placement,
        "home_turn_computer",
        create_autospec(
            home_placement.home_turn_computer,
            return_value=({"computer_id": COMP_COS, "name": "CoS Box"}, False),
        ),
    )
    monkeypatch.setattr(
        computer_db,
        "computer_has_blocking_archive_move",
        create_autospec(computer_db.computer_has_blocking_archive_move, return_value=False),
    )
    monkeypatch.setattr(
        workspace_folders,
        "computer_run_in_progress",
        create_autospec(workspace_folders.computer_run_in_progress, return_value=False),
    )
    monkeypatch.setattr(
        workspace_folders,
        "computer_command_running",
        create_autospec(workspace_folders.computer_command_running, return_value=False),
    )
    monkeypatch.setattr("src.server.database.pool.get_db_connection", _dummy_db_conn())

    orig_get_comp = computer_db.get_computer_for_workspace

    # 1. Running CoS computer on Docker / E2B / SSH -> rejected as archive_unsupported BEFORE web guidance
    for kind in ("docker", "e2b", "ssh"):
        monkeypatch.setattr(
            computer_db,
            "get_computer_for_workspace",
            create_autospec(
                orig_get_comp,
                return_value={
                    "computer_id": COMP_COS,
                    "name": f"{kind}-cos-box",
                    "kind": kind,
                    "status": "running",
                    "provider_ref": "sbx-cos",
                    "config": {},
                },
            ),
        )
        snap_unsupported_running = await build_archive_workspace_snapshot(
            USER_ID, _tool_call(action="archive", workspace_id=WS_NVDA)
        )
        assert snap_unsupported_running is not None
        assert snap_unsupported_running["precheck_error_code"] == "archive_unsupported"
        assert "Daytona" in snap_unsupported_running["precheck_error"]
        assert "网页端" not in snap_unsupported_running["precheck_error"]

    # 2. Running CoS computer on Daytona -> guided to web BEFORE card
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(
            orig_get_comp,
            return_value={
                "computer_id": COMP_COS,
                "name": "CoS Box",
                "kind": "daytona",
                "status": "running",
                "provider_ref": "sbx-cos",
                "config": {},
            },
        ),
    )
    snap_cos = await build_archive_workspace_snapshot(
        USER_ID, _tool_call(action="archive", workspace_id=WS_NVDA)
    )
    assert snap_cos is not None
    assert snap_cos["precheck_error_code"] == "running_cos_computer"
    assert "网页端" in snap_cos["precheck_error"]

    # 3. Stopped Docker / E2B / SSH -> rejected BEFORE card with archive_unsupported
    for kind in ("docker", "e2b", "ssh"):
        monkeypatch.setattr(
            computer_db,
            "get_computer_for_workspace",
            create_autospec(
                orig_get_comp,
                return_value={
                    "computer_id": COMP_DAYTONA,
                    "name": f"{kind}-box",
                    "kind": kind,
                    "status": "stopped",
                    "provider_ref": "sbx-1",
                    "config": {},
                },
            ),
        )
        snap = await build_archive_workspace_snapshot(
            USER_ID, _tool_call(action="archive", workspace_id=WS_NVDA)
        )
        assert snap is not None
        assert snap["precheck_error_code"] == "archive_unsupported"
        assert "Daytona" in snap["precheck_error"]

    # 3. Stopped Daytona with Home row while CoS runs on stand-in -> full snapshot includes Home + stand_in
    expected_ws = [
        {"workspace_id": WS_NVDA, "name": "NVDA", "dir_name": "NVDA", "status": "stopped", "is_stand_in": False},
        {"workspace_id": WS_HOME, "name": "Home", "dir_name": "Home", "status": "stopped", "is_stand_in": False},
        {"workspace_id": WS_STAND_IN, "name": "StandIn", "dir_name": "StandIn", "status": "stopped", "is_stand_in": True},
    ]
    monkeypatch.setattr(
        computer_db,
        "get_computer_for_workspace",
        create_autospec(
            orig_get_comp,
            return_value={
                "computer_id": COMP_DAYTONA,
                "name": "Daytona Research",
                "kind": "daytona",
                "status": "stopped",
                "provider_ref": "sbx-daytona",
                "config": {},
            },
        ),
    )
    monkeypatch.setattr(
        computer_db,
        "get_live_workspaces_for_archive_snapshot",
        create_autospec(
            computer_db.get_live_workspaces_for_archive_snapshot,
            return_value=expected_ws,
        ),
    )
    snap_ok = await build_archive_workspace_snapshot(
        USER_ID, _tool_call(action="archive", workspace_id=WS_NVDA)
    )
    assert snap_ok is not None
    assert snap_ok["precheck_error"] is None
    assert snap_ok["computer_id"] == COMP_DAYTONA
    assert snap_ok["expected_workspaces"] == expected_ws


@pytest.mark.asyncio
async def test_standing_approval_middleware_persists_archive_snapshot_and_never_preapproves(
    monkeypatch,
):
    from src.tools.secretary import approvals as approvals_mod

    fake_snap = {
        "action": "archive_workspace",
        "tool_call_id": "call-archive-1",
        "workspace_id": WS_NVDA,
        "workspace_name": "NVDA",
        "computer_id": COMP_DAYTONA,
        "computer_name": "Daytona Box",
        "expected_workspaces": [],
        "precheck_error": None,
        "precheck_error_code": None,
    }
    monkeypatch.setattr(
        approvals_mod,
        "build_archive_workspace_snapshot",
        create_autospec(build_archive_workspace_snapshot, return_value=fake_snap),
    )
    ai_msg = AIMessage(
        content="",
        tool_calls=[_tool_call(action="archive", workspace_id=WS_NVDA)],
    )
    mw = StandingApprovalMiddleware(USER_ID)
    update = await mw.aafter_model({"messages": [ai_msg]}, runtime=None)
    assert update is not None
    assert update["preapproved_calls"] == []
    assert update["approval_snapshots"]["call-archive-1"] == fake_snap


@pytest.mark.asyncio
async def test_manage_workspaces_archive_executes_only_from_snapshot_and_verifies_checkpoint(
    monkeypatch, caplog
):
    expected_ws = [
        {"workspace_id": WS_NVDA, "name": "NVDA", "dir_name": "NVDA", "status": "stopped", "is_stand_in": False},
        {"workspace_id": WS_STAND_IN, "name": "StandIn", "dir_name": "StandIn", "status": "stopped", "is_stand_in": True},
    ]
    snapshot = {
        "action": "archive_workspace",
        "tool_call_id": "call-archive-1",
        "workspace_id": WS_NVDA,
        "workspace_name": "NVDA",
        "computer_id": COMP_DAYTONA,
        "computer_name": "Daytona Box",
        "expected_workspaces": expected_ws,
        "precheck_error": None,
        "precheck_error_code": None,
    }

    # Checkpoint matcher validates every shown field including expected_workspaces
    action_req = {
        "type": "archive_workspace",
        "workspace_id": WS_NVDA,
        "workspace_name": "NVDA",
        "computer_id": COMP_DAYTONA,
        "computer_name": "Daytona Box",
        "expected_workspaces": expected_ws,
    }
    assert _snapshot_matches_action(snapshot, action_req) is True
    assert (
        _snapshot_matches_action(
            snapshot,
            {**action_req, "expected_workspaces": expected_ws[:1]},
        )
        is False
    )

    ws_mgr = create_autospec(WorkspaceManager, instance=True)
    ws_mgr.archive_workspace.return_value = {"status": "stopped"}
    monkeypatch.setattr(
        secretary_tools,
        "hitl_confirm",
        create_autospec(
            secretary_tools.hitl_confirm,
            return_value=(True, {"decisions": [{"type": "approve"}]}),
        ),
    )
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )

    state = {"approval_snapshots": {"call-archive-1": snapshot}}

    # 1. Approved -> calls archive_workspace strictly with snapshot
    res = await chief_of_staff_workspaces.ainvoke(
        _tool_call(action="archive", workspace_id=WS_NVDA, state=state),
        config=_cfg(),
    )
    data = _payload(res)
    assert data["success"] is True
    assert data["archived_workspace_count"] == 1  # stand-in excluded from displayed count
    ws_mgr.archive_workspace.assert_awaited_once_with(
        WS_NVDA,
        expected_computer_id=COMP_DAYTONA,
        expected_workspaces=expected_ws,
    )

    # 2. Scope changed -> returns terminal code=scope_changed
    ws_mgr.archive_workspace.reset_mock()
    ws_mgr.archive_workspace.side_effect = ArchiveScopeChangedError("情况已变化，请重新发起")
    res_sc = await chief_of_staff_workspaces.ainvoke(
        _tool_call(action="archive", workspace_id=WS_NVDA, state=state),
        config=_cfg(),
    )
    data_sc = _payload(res_sc)
    assert data_sc["success"] is False
    assert data_sc["code"] == "scope_changed"
    assert data_sc["error"] == "情况已变化，请重新发起"

    # 3. Provider still archiving -> reports in_progress / 归档进行中
    ws_mgr.archive_workspace.reset_mock()
    ws_mgr.archive_workspace.side_effect = None
    ws_mgr.archive_workspace.return_value = {
        "status": "stopped",
        "archive_state": "archiving",
    }
    res_ing = await chief_of_staff_workspaces.ainvoke(
        _tool_call(action="archive", workspace_id=WS_NVDA, state=state),
        config=_cfg(),
    )
    data_ing = _payload(res_ing)
    assert data_ing["success"] is True
    assert data_ing["in_progress"] is True
    assert data_ing["archive_state"] == "archiving"
    assert "归档进行中" in data_ing["message"]

    # 4. SandboxTransientError with secrets -> whitelisted log only, no secret leakage
    caplog.set_level(logging.WARNING)
    caplog.clear()
    ws_mgr.archive_workspace.side_effect = SandboxTransientError(
        "Bearer eyJSECRET sk-proj_1234567890abcdef postgresql://u:s3cr3tP4ss@10.0.0.1/db"
    )
    res_err = await chief_of_staff_workspaces.ainvoke(
        _tool_call(action="archive", workspace_id=WS_NVDA, state=state),
        config=_cfg(),
    )
    data_err = _payload(res_err)
    assert data_err["success"] is False
    assert "s3cr3tP4ss" not in data_err["error"]
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cr3tP4ss" not in log_text
    assert "sk-proj_" not in log_text
    assert "eyJSECRET" not in log_text
