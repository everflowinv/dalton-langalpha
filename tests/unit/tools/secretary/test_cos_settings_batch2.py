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
    assert "confirm_token" not in req
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
        check_duplicate_snapshot=True,
        expected_computer_kind="docker",
        computer_id=CLOUD_1,
        expected_computer_name="Cloud Primary",
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
        check_duplicate_snapshot=True,
        expected_computer_kind="daytona",
        computer_id=CLOUD_2,
        expected_computer_name="GPU Cloud",
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
    msg = res.update["messages"][0]
    assert msg.status == "error"
    payload = json.loads(msg.content)
    assert payload == {
        "success": False,
        "error": "情况已变化，请重新发起",
        "code": "scope_changed",
        "preapproved": True,
        "workspace_name": "NVDA",
        "workspace_description": "",
        "computer_name": "Cloud",
    }

    # Interactive (non-preapproved) failure omits preapproved metadata and sets status="error".
    confirm = create_autospec(hitl_confirm, return_value=(True, {}))
    with patch("src.tools.secretary.tools.hitl_confirm", confirm):
        res_interactive = await workspaces_create(
            USER_ID, "NVDA", "", "call-drift-2", preapproved=False, snapshot=snap
        )
    interactive_msg = res_interactive.update["messages"][0]
    assert interactive_msg.status == "error"
    interactive_payload = json.loads(interactive_msg.content)
    assert interactive_payload == {
        "success": False,
        "error": "情况已变化，请重新发起",
        "code": "scope_changed",
    }


@pytest.mark.asyncio
async def test_scope_changed_resume_consumes_interrupt_with_error_status_including_pre_upgrade_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Decision 4: when settle_resume_request stamps scope_changed=True
    (including for a pre-upgrade card missing approval_snapshots), LangGraph
    consumes the interrupt and emits a terminal ToolMessage(status='error',
    code='scope_changed') without creating a workspace."""
    mgr = _manager_double()
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
                        "args": {"action": "create", "name": "LegacyWS", "description": ""},
                        "id": "call-legacy-1",
                    }
                ],
            ),
            AIMessage(content="Please re-initiate."),
        ]
    )
    # No StandingApprovalMiddleware -> simulates pre-upgrade card with no snapshot.
    agent = create_agent(
        model,
        tools=[chief_of_staff_workspaces],
        checkpointer=InMemorySaver(),
    )
    cfg = {"configurable": {"thread_id": "t-legacy-1", "user_id": USER_ID}}

    paused = await agent.ainvoke({"messages": [("user", "create LegacyWS")]}, cfg)
    (pending,) = paused["__interrupt__"]

    resumed = await agent.ainvoke(
        Command(
            resume={
                pending.id: {
                    "decisions": [{"type": "approve"}],
                    "scope_changed": True,
                }
            }
        ),
        cfg,
    )
    assert "__interrupt__" not in resumed
    mgr.create_workspace.assert_not_awaited()
    tool_msgs = [m for m in resumed["messages"] if m.type == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].status == "error"
    assert json.loads(tool_msgs[0].content) == {
        "success": False,
        "error": "情况已变化，请重新发起",
        "code": "scope_changed",
    }


@pytest.mark.asyncio
async def test_preapproved_early_returns_company_exists_and_coverage_suggestion_carry_preapproved_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 3 Decision 6: standing-approval early returns for company_workspace_exists
    and coverage_suggestion must include preapproved metadata so the frontend draws a card."""
    snap = {
        "action": "create_workspace",
        "tool_call_id": "call-early-1",
        "workspace_name": "NVDA",
        "workspace_description": "GPU research",
        "computer_id": CLOUD_1,
        "computer_name": "Cloud Primary",
        "computer_kind": "docker",
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
    }

    # Case 1: company_workspace_exists
    dup_exists = SimpleNamespace(
        matches=[{"workspace_id": WS_1, "name": "NVIDIA"}],
        message="已存在同名公司工作区 NVIDIA",
        suggestion=None,
        suggestion_payload=lambda: None,
        matches_payload=lambda: [{"workspace_id": WS_1, "name": "NVIDIA"}],
    )
    monkeypatch.setattr(
        "src.dalton.coverage.dedup.find_existing_company_workspace",
        create_autospec(
            lambda _uid, _name, channel="cos": None,
            side_effect=lambda *_a, **_kw: _async_val(dup_exists),
        ),
    )
    monkeypatch.setattr(
        "src.dalton.coverage.units.is_workspace_coverage_registration_active",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val(True),
        ),
    )
    res_exists = await workspaces_create(
        USER_ID, "NVDA", "GPU research", "call-early-1", preapproved=True, snapshot=snap
    )
    payload_exists = json.loads(res_exists.update["messages"][0].content)
    assert payload_exists["success"] is False
    assert payload_exists["code"] == "company_workspace_exists"
    assert payload_exists["preapproved"] is True
    assert payload_exists["workspace_name"] == "NVDA"
    assert payload_exists["computer_name"] == "Cloud Primary"

    # Case 2: coverage_suggestion
    dup_sugg = SimpleNamespace(
        matches=[],
        message="",
        suggestion=SimpleNamespace(coverage_kind="company", ticker="NVDA"),
        suggestion_payload=lambda: {
            "coverage_kind": "company",
            "ticker": "NVDA",
            "display_name": "NVIDIA",
        },
        matches_payload=lambda: [],
    )
    monkeypatch.setattr(
        "src.dalton.coverage.dedup.find_existing_company_workspace",
        create_autospec(
            lambda _uid, _name, channel="cos": None,
            side_effect=lambda *_a, **_kw: _async_val(dup_sugg),
        ),
    )
    res_sugg = await workspaces_create(
        USER_ID, "NVDA", "GPU research", "call-early-2", preapproved=True, snapshot=snap
    )
    payload_sugg = json.loads(res_sugg.update["messages"][0].content)
    assert payload_sugg["success"] is False
    assert payload_sugg["code"] == "coverage_suggestion"
    assert payload_sugg["preapproved"] is True
    assert payload_sugg["workspace_name"] == "NVDA"
    assert payload_sugg["computer_name"] == "Cloud Primary"


async def _async_val(val: Any) -> Any:
    return val


@pytest.mark.asyncio
async def test_snapshot_field_drift_name_or_kind_and_bootstrap_uses_snapshotted_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex Med 5:
    1) WorkspaceManager.create_workspace rejects computer rename or kind change
       with ApprovalScopeChangedError when verify_snapshot=True.
    2) bootstrap_cloud_primary_computer uses the snapshotted expected_kind rather
       than re-reading deployment default sandbox.provider, and aborts with
       ApprovalScopeChangedError if that kind is no longer offered."""
    from contextlib import asynccontextmanager

    ws_mgr = object.__new__(WorkspaceManager)
    ws_mgr._chosen_computer = create_autospec(
        WorkspaceManager._chosen_computer,
        return_value=_computer_row(CLOUD_1, name="Renamed Box", kind="docker"),
    )

    with pytest.raises(ApprovalScopeChangedError):
        await ws_mgr.create_workspace(
            user_id=USER_ID,
            name="NVDA",
            computer_id=CLOUD_1,
            verify_snapshot=True,
            expected_computer_name="Original Box",
            expected_computer_kind="docker",
        )

    with pytest.raises(ApprovalScopeChangedError):
        await ws_mgr.create_workspace(
            user_id=USER_ID,
            name="NVDA",
            computer_id=CLOUD_1,
            verify_snapshot=True,
            expected_computer_name="Renamed Box",
            expected_computer_kind="daytona",
        )

    @asynccontextmanager
    async def _fake_lock(_uid: str):
        yield object()

    cm = object.__new__(ComputerManager)
    # Deployment default changed from daytona -> docker, but daytona is still offered.
    cm.config = SimpleNamespace(sandbox=_sandbox_cfg("docker", ("docker", "daytona")))
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.computer_capacity_lock",
        _fake_lock,
    )
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.get_primary_computer",
        create_autospec(get_primary_computer, return_value=None),
    )
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines.get_user",
        create_autospec(get_user, return_value={"user_id": USER_ID, "computer_choice": None}),
    )
    create_mock = create_autospec(
        cm.create_computer_for_user,
        return_value={"computer_id": CLOUD_2, "kind": "daytona", "is_primary": True},
    )
    monkeypatch.setattr(cm, "create_computer_for_user", create_mock)
    monkeypatch.setattr(
        "src.server.services.computer_manager._machines._record_added",
        lambda *_a, **_kw: None,
    )

    created = await cm.bootstrap_cloud_primary_computer(
        USER_ID, default_name="FirstWS", expected_kind="daytona"
    )
    assert created["kind"] == "daytona"
    assert create_mock.await_args.kwargs["kind"] == "daytona"

    # If the snapshotted kind is no longer offered -> ApprovalScopeChangedError.
    cm.config = SimpleNamespace(sandbox=_sandbox_cfg("docker", ("docker",)))
    with pytest.raises(ApprovalScopeChangedError):
        await cm.bootstrap_cloud_primary_computer(
            USER_ID, default_name="FirstWS", expected_kind="daytona"
        )


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
    assert body == {
        "success": False,
        "error": "failed to create workspace",
        "preapproved": True,
        "workspace_name": "NVDA",
        "workspace_description": "",
    }

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


@pytest.mark.asyncio
async def test_duplicate_recheck_always_enabled_even_when_duplicate_check_unavailable_and_passes_expected_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Decision 1:
    1) Even when snapshot.duplicate_check_unavailable is True, workspaces_create
       still passes check_duplicate_snapshot=True to WorkspaceManager.create_workspace.
    2) When the card listed a duplicate, both expected_duplicate_workspace_id and
       expected_duplicate_workspace_name are passed for locked re-verification."""
    from src.tools.secretary.tools import workspaces_create

    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": WS_1,
        "name": "700.HK",
        "computer_id": CLOUD_1,
        "dir_name": "700.HK",
    }
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr),
    )
    monkeypatch.setattr(
        "src.dalton.coverage.dedup.find_existing_company_workspace",
        create_autospec(
            lambda _uid, _name, channel="cos": None,
            side_effect=lambda *_a, **_kw: _async_val(None),
        ),
    )
    monkeypatch.setattr(
        "src.tools.secretary.tools._folder_ready_result",
        create_autospec(
            _folder_ready_result,
            return_value={"folder_ready": False, "folder_path": None},
        ),
    )
    monkeypatch.setattr(
        "src.tools.secretary.tools.hitl_confirm",
        lambda _action, _payload: (True, {"decisions": [{"type": "approve"}]}),
    )

    # Case A: duplicate_check_unavailable=True must still enable locked duplicate recheck.
    snap_unavail = {
        "action": "create_workspace",
        "tool_call_id": "call-unavail-1",
        "workspace_name": "700.HK",
        "workspace_description": "",
        "computer_id": CLOUD_1,
        "computer_name": "Cloud Primary",
        "computer_kind": "docker",
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
        "duplicate_workspace_id": None,
        "duplicate_workspace_name": None,
        "duplicate_check_unavailable": True,
        "precheck_error": None,
    }
    res_a = await workspaces_create(
        USER_ID, "700.HK", "", "call-unavail-1", preapproved=False, snapshot=snap_unavail
    )
    assert json.loads(res_a.update["messages"][0].content)["success"] is True
    kwargs_a = mgr.create_workspace.await_args.kwargs
    assert kwargs_a["check_duplicate_snapshot"] is True
    assert "expected_duplicate_workspace_id" not in kwargs_a

    # Case B: listed duplicate carries both ID and name for locked rename/delete checks.
    mgr.create_workspace.reset_mock()
    snap_dup = {
        **snap_unavail,
        "tool_call_id": "call-dup-1",
        "duplicate_check_unavailable": False,
        "duplicate_workspace_id": WS_1,
        "duplicate_workspace_name": "0700.HK",
    }
    res_b = await workspaces_create(
        USER_ID, "700.HK", "", "call-dup-1", preapproved=False, snapshot=snap_dup
    )
    assert json.loads(res_b.update["messages"][0].content)["success"] is True
    kwargs_b = mgr.create_workspace.await_args.kwargs
    assert kwargs_b["check_duplicate_snapshot"] is True
    assert kwargs_b["expected_duplicate_workspace_id"] == WS_1
    assert kwargs_b["expected_duplicate_workspace_name"] == "0700.HK"


@pytest.mark.asyncio
async def test_real_langgraph_resume_reads_scope_changed_and_reject_before_company_dedup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Decision 3: using a real LangGraph StateGraph + InMemorySaver,
    when a pre-upgrade create_workspace interrupt resumes with server-stamped
    scope_changed=True (or user reject) after a matching company workspace
    appeared, the tool reads the interrupt answer first and returns
    status='error' code='scope_changed' (or User declined) without running
    find_existing_company_workspace."""
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import Command as LGCommand

    from src.tools.secretary.tools import workspaces_create

    dedup_calls: list[str] = []
    company_exists_now = {"flag": False}

    async def _fake_find_company(uid: str, ws_name: str, channel: str = "cos"):
        dedup_calls.append(ws_name)
        if company_exists_now["flag"]:
            return SimpleNamespace(
                matches=[SimpleNamespace(workspace_id=WS_1, name="NVIDIA")],
                message="已存在同名公司工作区 NVIDIA",
                suggestion=None,
                suggestion_payload=lambda: None,
                matches_payload=lambda: [{"workspace_id": WS_1, "name": "NVIDIA"}],
            )
        return None

    monkeypatch.setattr(
        "src.dalton.coverage.dedup.find_existing_company_workspace",
        _fake_find_company,
    )
    monkeypatch.setattr(
        "src.tools.secretary.approvals.duplicate_workspace",
        create_autospec(
            lambda _uid, _name: None,
            side_effect=lambda *_a, **_kw: _async_val((None, True)),
        ),
    )

    async def _tool_node(_state: MessagesState):
        return await workspaces_create(
            USER_ID,
            "NVDA",
            "GPU research",
            "call-lg-1",
            preapproved=False,
            snapshot=None,
        )

    builder = StateGraph(MessagesState)
    builder.add_node("create", _tool_node)
    builder.add_edge(START, "create")
    builder.add_edge("create", END)

    # Scenario 1: server-stamped scope_changed=True must NOT be preempted by company_workspace_exists.
    graph_sc = builder.compile(checkpointer=InMemorySaver())
    cfg_sc = {"configurable": {"thread_id": "lg-scope-changed"}}
    paused_sc = await graph_sc.ainvoke({"messages": []}, config=cfg_sc)
    assert "__interrupt__" in paused_sc
    intr_sc_id = paused_sc["__interrupt__"][0].id
    assert len(dedup_calls) == 1

    # While paused, a matching company workspace appears.
    company_exists_now["flag"] = True
    dedup_calls.clear()

    resumed_sc = await graph_sc.ainvoke(
        LGCommand(
            resume={
                intr_sc_id: {
                    "decisions": [{"type": "approve"}],
                    "scope_changed": True,
                }
            }
        ),
        config=cfg_sc,
    )
    assert dedup_calls == []
    tool_msg_sc = resumed_sc["messages"][-1]
    assert tool_msg_sc.status == "error"
    body_sc = json.loads(tool_msg_sc.content)
    assert body_sc["success"] is False
    assert body_sc["code"] == "scope_changed"

    # Scenario 2: explicit reject on resume must NOT be preempted by company_workspace_exists either.
    company_exists_now["flag"] = False
    dedup_calls.clear()
    graph_rej = builder.compile(checkpointer=InMemorySaver())
    cfg_rej = {"configurable": {"thread_id": "lg-reject"}}
    paused_rej = await graph_rej.ainvoke({"messages": []}, config=cfg_rej)
    intr_rej_id = paused_rej["__interrupt__"][0].id
    assert len(dedup_calls) == 1

    company_exists_now["flag"] = True
    dedup_calls.clear()

    resumed_rej = await graph_rej.ainvoke(
        LGCommand(resume={intr_rej_id: {"decisions": [{"type": "reject"}]}}),
        config=cfg_rej,
    )
    assert dedup_calls == []
    tool_msg_rej = resumed_rej["messages"][-1]
    assert tool_msg_rej.content.startswith("User declined workspace creation")


@pytest.mark.asyncio
async def test_workspace_manager_bootstrap_reclaims_orphan_computer_if_insert_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 4 Decision 5: if create_workspace_on_computer returns None after
    bootstrap_cloud_primary_computer minted a cloud primary, WorkspaceManager
    reclaims the empty bootstrapped computer before raising ApprovalScopeChangedError."""
    ws_mgr = object.__new__(WorkspaceManager)
    bootstrap_mock = create_autospec(
        ws_mgr.bootstrap_cloud_primary_computer,
        return_value=_computer_row(CLOUD_2, name="FirstWS", kind="docker", is_primary=True),
    )
    reclaim_mock = create_autospec(
        WorkspaceManager._reclaim_empty_bootstrapped_computer,
        return_value=None,
    )
    ws_mgr.bootstrap_cloud_primary_computer = bootstrap_mock
    ws_mgr._reclaim_empty_bootstrapped_computer = reclaim_mock
    monkeypatch.setattr(
        "src.server.services.workspace_manager.create_workspace_on_computer",
        create_autospec(
            lambda *a, **kw: None,
            side_effect=lambda *_a, **_kw: _async_val(None),
        ),
    )

    with pytest.raises(ApprovalScopeChangedError):
        await ws_mgr.create_workspace(
            user_id=USER_ID,
            name="700.HK",
            bootstrap_cloud_primary=True,
            verify_snapshot=True,
            expected_computer_kind="docker",
            check_duplicate_snapshot=True,
        )

    assert bootstrap_mock.await_args.kwargs["check_duplicate_snapshot"] is True
    assert bootstrap_mock.await_args.kwargs["workspace_name"] == "700.HK"
    reclaim_mock.assert_awaited_once_with(CLOUD_2)
