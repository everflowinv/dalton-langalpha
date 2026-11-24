"""Batch 2 unit tests: confirmation snapshot persistence, specified-computer create,
RECREATABLE_KINDS cloud bootstrap, standing-approval withholding, and structured logging.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec, patch

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ptc_agent.config.core import SandboxConfig
from src.server.database.computer import (
    get_computer,
    get_computer_roster,
    get_computers_for_user,
    get_primary_computer,
    update_computer_status,
)
from src.server.database.user import get_user, get_user_preferences
from src.server.database.workspace import get_workspaces_for_user
from src.server.services.computer_errors import (
    ApprovalScopeChangedError,
    ComputerArchivingError,
    ComputerNotFoundError,
    ProviderNotOffered,
)
from src.server.services.computer_manager import ComputerManager
from src.server.services.workspace_manager import WorkspaceManager
from src.tools.secretary._commands import hitl_confirm
from src.tools.secretary.approvals import (
    AUTO_APPROVE_WORKSPACE_CREATION,
    BOOTSTRAP_CLOUD_COMPUTER_LABEL,
    StandingApprovalMiddleware,
    _sandbox_config_for_bootstrap,
)
from src.tools.secretary.chief_of_staff import chief_of_staff_workspaces
from src.tools.secretary.tools import _folder_ready_result, workspaces_create

USER_ID = "user-b2"
CLOUD_1 = "11111111-1111-1111-1111-111111111111"
CLOUD_2 = "22222222-2222-2222-2222-222222222222"
MAC_1 = "33333333-3333-3333-3333-333333333333"
WS_1 = "44444444-4444-4444-4444-444444444444"


def _computer_row(
    computer_id: str,
    *,
    name: str,
    kind: str = "docker",
    status: str = "stopped",
    is_primary: bool = False,
    provider_ref: str | None = "sbx-1",
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "computer_id": computer_id,
        "user_id": USER_ID,
        "name": name,
        "kind": kind,
        "status": status,
        "is_primary": is_primary,
        "provider_ref": provider_ref,
        "root_dir": "/home/workspace",
        "provider_config": {},
        "config": config or {},
        "is_home": is_primary,
    }


def _sandbox_cfg(provider: str, creatable: tuple[str, ...]) -> Any:
    cfg = create_autospec(SandboxConfig, instance=True)
    cfg.provider = provider
    cfg.creatable_providers.return_value = creatable
    return cfg


def _manager_double(workspace_row: dict[str, Any] | None = None) -> WorkspaceManager:
    mgr = create_autospec(WorkspaceManager, instance=True)
    if workspace_row is not None:
        mgr.create_workspace.return_value = workspace_row
    mgr.resolve_binding.return_value = None
    mgr._cached_session.return_value = None
    return mgr


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _tool_payloads(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in out["messages"] if m.type == "tool"]


@pytest.mark.asyncio
async def test_resume_reads_only_persisted_snapshot_when_primary_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Switching the primary computer while interrupt() is pending must not
    change the computer executed on resume: resume reads only approval_snapshots."""
    c1 = _computer_row(CLOUD_1, name="Cloud Primary", kind="docker", is_primary=True)
    c2 = _computer_row(CLOUD_2, name="New Primary", kind="docker", is_primary=True)
    current_primary = {"row": c1}

    async def _primary(uid: str, *, conn=None):
        return current_primary["row"]

    primary_mock = create_autospec(get_primary_computer, side_effect=_primary)
    prefs_mock = create_autospec(get_user_preferences, return_value={"other_preference": {}})
    mgr = _manager_double(
        {"workspace_id": WS_1, "name": "Alpha", "computer_id": CLOUD_1, "dir_name": "Alpha"}
    )

    monkeypatch.setattr("src.server.database.computer.get_primary_computer", primary_mock)
    monkeypatch.setattr("src.server.database.user.get_user_preferences", prefs_mock)
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr),
    )

    model = _Model(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "manage_workspaces",
                        "args": {"action": "create", "name": "Alpha", "description": "desc"},
                        "id": "call-snap-1",
                    }
                ],
            ),
            AIMessage(content="Created"),
        ]
    )
    agent = create_agent(
        model,
        tools=[chief_of_staff_workspaces],
        middleware=[StandingApprovalMiddleware(USER_ID)],
        checkpointer=InMemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "t-snap-1", "user_id": USER_ID}}

    paused = await agent.ainvoke({"messages": [("user", "create Alpha")]}, cfg)
    (pending,) = paused["__interrupt__"]
    req = pending.value["action_requests"][0]
    assert req["type"] == "create_workspace"
    assert req["tool_call_id"] == "call-snap-1"
    assert req["computer_id"] == CLOUD_1
    assert req["computer_name"] == "Cloud Primary"
    assert req["confirm_token"]
    assert primary_mock.await_count == 1

    # Switch primary computer while the card is waiting for approval.
    current_primary["row"] = c2

    resumed = await agent.ainvoke(
        Command(resume={pending.id: {"decisions": [{"type": "approve"}]}}), cfg
    )
    # Primary was never re-queried on resume.
    assert primary_mock.await_count == 1
    mgr.create_workspace.assert_awaited_once_with(
        user_id=USER_ID,
        name="Alpha",
        description="desc",
        verify_snapshot=True,
        computer_id=CLOUD_1,
    )
    payloads = _tool_payloads(resumed)
    assert payloads[0]["success"] is True
    assert payloads[0]["computer_id"] == CLOUD_1
    assert payloads[0]["computer_name"] == "Cloud Primary"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selector",
    ["My Mac Studio", MAC_1, MAC_1[:8]],
    ids=["display_name", "full_uuid", "8char_prefix"],
)
async def test_explicit_computer_resolves_by_name_uuid_or_prefix_and_withholds_auto_approve_for_own_machine(
    monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    """Own machine (MACHINE_KINDS) must withhold auto_approve_workspace_creation
    even when enabled, and stamp computer_is_own_machine=True in the snapshot."""
    mac = _computer_row(MAC_1, name="My Mac Studio", kind="ssh", is_primary=False)
    cloud = _computer_row(CLOUD_1, name="Cloud Primary", kind="docker", is_primary=True)

    monkeypatch.setattr(
        "src.server.database.user.get_user_preferences",
        create_autospec(
            get_user_preferences,
            return_value={"other_preference": {AUTO_APPROVE_WORKSPACE_CREATION: True}},
        ),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_computer_roster",
        create_autospec(get_computer_roster, return_value=[cloud, mac]),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_computer",
        create_autospec(get_computer, return_value=mac),
    )

    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "manage_workspaces",
                        "args": {
                            "action": "create",
                            "name": "NVDA",
                            "computer": selector,
                        },
                        "id": "call-own-1",
                    }
                ],
            )
        ]
    }
    update = await StandingApprovalMiddleware(USER_ID).aafter_model(state, None)
    assert update is not None
    assert update["preapproved_calls"] == []
    snap = update["approval_snapshots"]["call-own-1"]
    assert snap["computer_id"] == MAC_1
    assert snap["computer_name"] == "My Mac Studio"
    assert snap["computer_is_own_machine"] is True
    assert snap["bootstrap_cloud_primary"] is False


@pytest.mark.asyncio
async def test_standing_approval_on_cloud_computer_uses_same_snapshot_computer_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cloud = _computer_row(CLOUD_2, name="GPU Cloud", kind="daytona", is_primary=False)
    monkeypatch.setattr(
        "src.server.database.user.get_user_preferences",
        create_autospec(
            get_user_preferences,
            return_value={"other_preference": {AUTO_APPROVE_WORKSPACE_CREATION: True}},
        ),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_computer_roster",
        create_autospec(get_computer_roster, return_value=[cloud]),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_computer",
        create_autospec(get_computer, return_value=cloud),
    )

    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "manage_workspaces",
                        "args": {"action": "create", "name": "Semis", "computer": "GPU Cloud"},
                        "id": "call-cloud-1",
                    }
                ],
            )
        ]
    }
    update = await StandingApprovalMiddleware(USER_ID).aafter_model(state, None)
    assert update is not None
    assert update["preapproved_calls"] == ["call-cloud-1"]
    snap = update["approval_snapshots"]["call-cloud-1"]
    assert snap["computer_id"] == CLOUD_2
    assert snap["computer_is_own_machine"] is False

    mgr = _manager_double(
        {"workspace_id": WS_1, "name": "Semis", "computer_id": CLOUD_2, "dir_name": "Semis"}
    )
    confirm = create_autospec(hitl_confirm, side_effect=AssertionError("must not prompt"))
    with patch("src.tools.secretary.tools.hitl_confirm", confirm), patch(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr),
    ):
        res = await workspaces_create(
            USER_ID,
            "Semis",
            "Chip research",
            "call-cloud-1",
            preapproved=True,
            computer="GPU Cloud",
            snapshot=snap,
        )
    mgr.create_workspace.assert_awaited_once_with(
        user_id=USER_ID,
        name="Semis",
        description="Chip research",
        verify_snapshot=True,
        computer_id=CLOUD_2,
    )
    payload = json.loads(res.update["messages"][0].content)
    assert payload["success"] is True
    assert payload["preapproved"] is True
    assert payload["computer_id"] == CLOUD_2
    assert payload["computer_name"] == "GPU Cloud"


@pytest.mark.asyncio
async def test_no_primary_bootstrap_cloud_primary_ignores_existing_own_machine_and_limits_to_recreatable_kinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no primary exists and the user already has an unpromoted own machine:
    - If default provider is in RECREATABLE_KINDS (e.g. docker): snapshot sets
      bootstrap_cloud_primary=True, computer_id=None, never promotes own machine.
    - If default provider is 'local' (not in RECREATABLE_KINDS): snapshot fails
      precheck with provider_not_offered and never grants standing approval."""
    own_mac = _computer_row(MAC_1, name="MacBook", kind="ssh", is_primary=False)
    monkeypatch.setattr(
        "src.server.database.user.get_user_preferences",
        create_autospec(
            get_user_preferences,
            return_value={"other_preference": {AUTO_APPROVE_WORKSPACE_CREATION: True}},
        ),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_primary_computer",
        create_autospec(get_primary_computer, return_value=None),
    )
    monkeypatch.setattr(
        "src.server.database.user.get_user",
        create_autospec(get_user, return_value={"user_id": USER_ID, "computer_choice": None}),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_computers_for_user",
        create_autospec(get_computers_for_user, return_value=[own_mac]),
    )

    # Case 1: docker (RECREATABLE_KINDS) -> bootstrap_cloud_primary=True
    monkeypatch.setattr(
        "src.tools.secretary.approvals._sandbox_config_for_bootstrap",
        create_autospec(
            _sandbox_config_for_bootstrap,
            return_value=_sandbox_cfg("docker", ("docker",)),
        ),
    )
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "manage_workspaces",
                        "args": {"action": "create", "name": "FirstWS"},
                        "id": "call-boot-1",
                    }
                ],
            )
        ]
    }
    update = await StandingApprovalMiddleware(USER_ID).aafter_model(state, None)
    assert update is not None
    assert update["preapproved_calls"] == ["call-boot-1"]
    snap = update["approval_snapshots"]["call-boot-1"]
    assert snap["computer_id"] is None
    assert snap["bootstrap_cloud_primary"] is True
    assert snap["computer_name"] == BOOTSTRAP_CLOUD_COMPUTER_LABEL
    assert snap["computer_is_own_machine"] is False

    # Case 2: local (NOT in RECREATABLE_KINDS, Codex r3 #6) -> precheck_error, no auto-approve
    monkeypatch.setattr(
        "src.tools.secretary.approvals._sandbox_config_for_bootstrap",
        create_autospec(
            _sandbox_config_for_bootstrap,
            return_value=_sandbox_cfg("local", ("local",)),
        ),
    )
    update_local = await StandingApprovalMiddleware(USER_ID).aafter_model(state, None)
    assert update_local is not None
    assert update_local["preapproved_calls"] == []
    snap_local = update_local["approval_snapshots"]["call-boot-1"]
    assert snap_local["bootstrap_cloud_primary"] is False
    assert snap_local["precheck_error_code"] == "provider_not_offered"
    assert "MacBook" in snap_local["precheck_error"]


@pytest.mark.asyncio
async def test_bootstrap_cloud_primary_computer_aborts_when_primary_appeared_or_provider_not_recreatable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_lock(_uid: str):
        yield object()

    mgr = object.__new__(ComputerManager)
    mgr.config = SimpleNamespace(sandbox=_sandbox_cfg("local", ("local",)))
    with pytest.raises(ProviderNotOffered):
        await mgr.bootstrap_cloud_primary_computer(USER_ID, default_name="FirstWS")

    # Switch to docker; simulate a primary computer appearing before execution.
    mgr.config = SimpleNamespace(sandbox=_sandbox_cfg("docker", ("docker",)))
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.computer_capacity_lock",
        _fake_lock,
    )
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.get_primary_computer",
        create_autospec(
            get_primary_computer,
            return_value=_computer_row(MAC_1, name="Mac", kind="ssh", is_primary=True),
        ),
    )
    with pytest.raises(ApprovalScopeChangedError):
        await mgr.bootstrap_cloud_primary_computer(USER_ID, default_name="FirstWS")

    # Simulate race where create_computer_for_user returns is_primary=False -> tombstoned & scope_changed.
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.get_primary_computer",
        create_autospec(get_primary_computer, return_value=None),
    )
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.get_user",
        create_autospec(get_user, return_value={"user_id": USER_ID, "computer_choice": None}),
    )
    create_mock = create_autospec(
        mgr.create_computer_for_user,
        return_value={"computer_id": CLOUD_2, "is_primary": False},
    )
    tombstone_mock = create_autospec(update_computer_status, return_value=None)
    monkeypatch.setattr(mgr, "create_computer_for_user", create_mock)
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.update_computer_status",
        tombstone_mock,
    )
    with pytest.raises(ApprovalScopeChangedError):
        await mgr.bootstrap_cloud_primary_computer(USER_ID, default_name="FirstWS")
    tombstone_mock.assert_awaited_once_with(CLOUD_2, "deleted")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        ApprovalScopeChangedError("changed"),
        ComputerNotFoundError("gone"),
        ComputerArchivingError("archiving"),
    ],
)
async def test_snapshot_drift_during_create_returns_scope_changed(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    mgr = _manager_double()
    mgr.create_workspace.side_effect = exc
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr),
    )
    snap = {
        "action": "create_workspace",
        "tool_call_id": "call-drift-1",
        "workspace_name": "NVDA",
        "workspace_description": "",
        "computer_id": CLOUD_1,
        "computer_name": "Cloud",
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
    }
    res = await workspaces_create(
        USER_ID, "NVDA", "", "call-drift-1", preapproved=True, snapshot=snap
    )
    payload = json.loads(res.update["messages"][0].content)
    assert payload == {
        "success": False,
        "error": "情况已变化，请重新发起",
        "code": "scope_changed",
    }


@pytest.mark.asyncio
async def test_workspaces_list_includes_computer_id_only_with_multiple_computers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_row = {
        "workspace_id": WS_1,
        "name": "NVDA",
        "description": "",
        "dir_name": "NVDA",
        "status": "stopped",
        "computer_id": CLOUD_1,
        "created_at": "2026-01-01",
        "last_activity_at": "2026-01-02",
    }
    monkeypatch.setattr(
        "src.server.database.workspace.get_workspaces_for_user",
        create_autospec(get_workspaces_for_user, return_value=([ws_row], 1)),
    )
    monkeypatch.setattr("src.dalton.settings.is_dalton_sharing_auth_enabled", lambda: False)

    # Single computer -> omit computer & computer_id
    monkeypatch.setattr(
        "src.server.database.computer.get_computer_roster",
        create_autospec(
            get_computer_roster,
            return_value=[_computer_row(CLOUD_1, name="Cloud", is_primary=True)],
        ),
    )
    res_one = await chief_of_staff_workspaces.ainvoke(
        {"name": "manage_workspaces", "args": {"action": "list", "state": {}}, "id": "c1", "type": "tool_call"},
        config={"configurable": {"user_id": USER_ID}},
    )
    row_one = json.loads(res_one.update["messages"][0].content)["workspaces"][0]
    assert "computer" not in row_one
    assert "computer_id" not in row_one

    # Two computers -> emit both computer and computer_id
    monkeypatch.setattr(
        "src.server.database.computer.get_computer_roster",
        create_autospec(
            get_computer_roster,
            return_value=[
                _computer_row(CLOUD_1, name="Cloud", is_primary=True),
                _computer_row(MAC_1, name="Mac Studio", kind="ssh", is_primary=False),
            ],
        ),
    )
    res_two = await chief_of_staff_workspaces.ainvoke(
        {"name": "manage_workspaces", "args": {"action": "list", "state": {}}, "id": "c2", "type": "tool_call"},
        config={"configurable": {"user_id": USER_ID}},
    )
    row_two = json.loads(res_two.update["messages"][0].content)["workspaces"][0]
    assert row_two["computer"] == "Cloud"
    assert row_two["computer_id"] == CLOUD_1


@pytest.mark.asyncio
async def test_folder_ready_and_create_logs_never_leak_raw_exception_secrets(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_blob = (
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 "
        "sk-proj_1234567890abcdef "
        "AIzaSyA1234567890abcdefXYZ "
        "postgresql://dbuser:s3cr3tP4ss@10.0.0.8:5432/app "
        "RAW_PROVIDER_BODY_MARKER"
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.resolve_binding.side_effect = RuntimeError(secret_blob)
    mgr._cached_session.return_value = None

    with caplog.at_level(logging.WARNING, logger="src.tools.secretary.tools"):
        res = await _folder_ready_result(
            mgr,
            USER_ID,
            {"workspace_id": WS_1, "dir_name": "NVDA", "computer_id": CLOUD_1},
        )
    assert res["folder_ready"] is False

    mgr_fail = create_autospec(WorkspaceManager, instance=True)
    mgr_fail.create_workspace.side_effect = RuntimeError(secret_blob)
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr_fail),
    )
    with caplog.at_level(logging.ERROR, logger="src.tools.secretary.tools"):
        cmd = await workspaces_create(USER_ID, "NVDA", None, "call-log-1", preapproved=True)
    body = json.loads(cmd.update["messages"][0].content)
    assert body == {"success": False, "error": "failed to create workspace"}

    combined_logs = "\n".join(rec.getMessage() for rec in caplog.records)
    for forbidden in (
        "eyJhbGci",
        "sk-proj_",
        "AIzaSyA",
        "s3cr3tP4ss",
        "RAW_PROVIDER_BODY_MARKER",
    ):
        assert forbidden not in combined_logs
        assert forbidden not in json.dumps(body)
    assert "folder_ready_error" in combined_logs
    assert "workspace_create_failed" in combined_logs


def test_cos_prompt_and_docstring_cover_computer_and_confirmation_flow() -> None:
    desc = chief_of_staff_workspaces.description or ""
    assert "computer:" in desc
    assert "Own machines always show a confirmation card" in desc

    prompt_path = (
        Path(__file__).resolve().parents[4]
        / "src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2"
    )
    text = prompt_path.read_text(encoding="utf-8")
    section = text.split("## Choosing the workspace", 1)[1].split("## After a hand-off", 1)[0]
    assert "pass `computer`" in section
    assert "scope_changed" in section
    assert "confirmation card" in section
