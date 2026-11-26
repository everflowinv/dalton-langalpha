"""Batch 2 unit tests: confirmation snapshot persistence, specified-computer create,
RECREATABLE_KINDS cloud bootstrap, standing-approval withholding, and structured logging.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any
from unittest.mock import create_autospec, patch

from langchain.tools import InjectedState, InjectedToolCallId

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ptc_agent.config.core import SandboxConfig
from src.dalton.coverage.dedup import (
    find_existing_company_workspace as _REAL_FIND_EXISTING_COMPANY_WORKSPACE,
    recheck_company_workspace_before_create as _REAL_RECHECK_COMPANY_WORKSPACE,
)
from src.server.database.computer import (
    get_computer,
    get_computer_roster,
    get_computers_for_user,
    get_primary_computer,
    update_computer_status,
)
from src.server.database.user import get_user, get_user_preferences
from src.server.database.workspace import (
    get_live_workspace_names as _REAL_GET_LIVE_WORKSPACE_NAMES,
    get_workspace_dir_names_for_computer as _REAL_GET_WORKSPACE_DIR_NAMES_FOR_COMPUTER,
    get_workspaces_for_user,
    lock_user_writes as _REAL_LOCK_USER_WRITES,
)
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
@pytest.mark.parametrize(
    ("scenario", "new_ws_owner", "new_ws_name", "owner_name", "owner_email"),
    [
        ("company_name_tencent", USER_ID, "腾讯控股", "Alice", "alice@team.com"),
        ("colleague_ticker_0700hk", "user-colleague-1", "0700.HK", "Bob", "bob@team.com"),
    ],
)
async def test_real_langgraph_approve_two_phase_dedup_matches_expected_workspace_id_lock_free(
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    new_ws_owner: str,
    new_ws_name: str,
    owner_name: str,
    owner_email: str,
) -> None:
    """Round 8 Decisions 1 & 3: two-phase duplicate verification on approve.

    Phase 1 (lock-free, after approval and before acquiring lock_user_writes)
    runs full company dedup (with LLM + colleague visibility):
    - Asserts recheck_company_workspace_before_create returns status='matched'
      with the EXACT expected matched_workspace_ids (cannot be confused with a
      parse failure returning 'unavailable').
    - Asserts LLM runs while lock_user_writes is NOT held, no write lock is
      taken, and INSERT INTO workspaces never executes.
    - Positive control: when LLM reports no match (status='clear'), Phase 2
      deterministic in-lock recheck runs with ZERO LLM calls under lock and
      executes INSERT INTO workspaces (success=True)."""
    from contextlib import asynccontextmanager

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import Command as LGCommand

    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import DedupLlmResult, DedupMatchItem
    from src.server.database import workspace as ws_db
    from src.tools.secretary.tools import workspaces_create

    dup_ws_id = "55555555-5555-5555-5555-555555555555"
    live_workspaces: list[dict[str, Any]] = []
    lock_state = {
        "held": False,
        "lock_acquires": 0,
        "inserts": 0,
    }
    llm_calls: list[dict[str, Any]] = []
    recheck_results: list[Any] = []
    healthy_computer = _computer_row(
        CLOUD_1, name="Cloud Primary", kind="docker", is_primary=True
    )

    class _FakeCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "INSERT INTO workspaces" in text:
                lock_state["inserts"] += 1
                self._rows = [
                    {
                        "workspace_id": WS_1,
                        "user_id": USER_ID,
                        "name": "700.HK",
                        "description": "Tencent research",
                        "computer_id": CLOUD_1,
                        "dir_name": "700.HK",
                        "status": "stopped",
                    }
                ]
                return
            if "FROM computers" in text:
                self._rows = [dict(healthy_computer)]
                return
            if "SELECT dir_name FROM workspaces" in text:
                self._rows = []
                return
            if "FROM dalton_members" in text:
                self._rows = [{"email": "alice@team.com"}, {"email": "bob@team.com"}]
                return
            if "FROM workspaces w" in text:
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [
                        dict(r) for r in live_workspaces if r["workspace_id"] in wanted
                    ]
                else:
                    self._rows = [dict(r) for r in live_workspaces]
                return
            if "FROM dalton_coverage_units" in text or "FROM dalton_knowledge_versions" in text:
                self._rows = []
                return
            if "FROM workspaces" in text and isinstance(params, dict) and "user_id" in params:
                uid_filter = str(params["user_id"])
                self._rows = [
                    {"workspace_id": r["workspace_id"], "name": r["name"]}
                    for r in live_workspaces
                    if r["user_id"] == uid_filter
                ]
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._rows[0] if self._rows else None

    class _FakeConn:
        def cursor(self, **_kwargs):
            return _FakeCursor()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _fake_folder_allocation(_computer_id: str, conn=None):
        try:
            yield conn or _FakeConn()
        finally:
            lock_state["held"] = False

    @asynccontextmanager
    async def _fake_get_db_connection(conn=None):
        yield conn or _FakeConn()

    async def _fake_lock_user_writes(_cur, _uid: str) -> None:
        lock_state["held"] = True
        lock_state["lock_acquires"] += 1

    emit_match = {"flag": True}

    async def _fake_dalton_complete(**kwargs):
        prompt = str(kwargs.get("user_prompt") or "")
        llm_calls.append({"lock_held": lock_state["held"], "prompt": prompt})
        if emit_match["flag"] and new_ws_name in prompt:
            return SimpleNamespace(
                ok=True,
                data=DedupLlmResult(
                    matches=[
                        DedupMatchItem(
                            id="c1",
                            confidence="high",
                            reason=f"same company ({scenario})",
                        )
                    ]
                ),
            )
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    orig_recheck = dedup_mod.recheck_company_workspace_before_create

    async def _spy_recheck(*args, **kwargs):
        res = await orig_recheck(*args, **kwargs)
        recheck_results.append(res)
        return res

    real_ws_mgr = object.__new__(WorkspaceManager)
    real_ws_mgr.resolve_binding = create_autospec(
        real_ws_mgr.resolve_binding, return_value=None
    )
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=real_ws_mgr),
    )
    monkeypatch.setattr(
        "src.server.services.workspace_manager.get_computer",
        create_autospec(
            lambda _cid: None,
            side_effect=lambda *_a, **_kw: _async_val(dict(healthy_computer)),
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_computer",
        create_autospec(
            lambda _cid, conn=None: None,
            side_effect=lambda *_a, **_kw: _async_val(dict(healthy_computer)),
        ),
    )
    monkeypatch.setattr(ws_db, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(ws_db, "folder_allocation", _fake_folder_allocation)
    monkeypatch.setattr(ws_db, "lock_user_writes", _fake_lock_user_writes)
    monkeypatch.setattr(
        ws_db,
        "start_new_workspace_selection",
        create_autospec(ws_db.start_new_workspace_selection, return_value=None),
    )
    monkeypatch.setattr(
        "src.tools.secretary.tools._folder_ready_result",
        create_autospec(
            _folder_ready_result,
            return_value={"folder_ready": False, "folder_path": None},
        ),
    )
    monkeypatch.setattr(dedup_mod, "recheck_company_workspace_before_create", _spy_recheck)
    monkeypatch.setattr(dedup_mod, "get_db_connection", _fake_get_db_connection)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(dedup_mod.sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        dedup_mod.sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com", "bob@team.com"}),
    )
    monkeypatch.setattr(dedup_mod.sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(dedup_mod.sharing_mod, "is_company_system_user", lambda _u: False)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val("internal"),
        ),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _fake_dalton_complete)
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)
    monkeypatch.setattr(
        dedup_mod.sharing_mod,
        "require_workspace_access",
        create_autospec(
            dedup_mod.sharing_mod.require_workspace_access,
            return_value=None,
        ),
    )

    snap = {
        "action": "create_workspace",
        "tool_call_id": f"call-lg-{scenario}",
        "workspace_name": "700.HK",
        "workspace_description": "Tencent research",
        "computer_id": CLOUD_1,
        "computer_name": "Cloud Primary",
        "computer_kind": "docker",
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
        "duplicate_workspace_id": None,
        "duplicate_workspace_name": None,
        "duplicate_check_unavailable": False,
        "precheck_error": None,
    }

    async def _tool_node(_state: MessagesState):
        return await workspaces_create(
            USER_ID,
            "700.HK",
            "Tencent research",
            f"call-lg-{scenario}",
            preapproved=False,
            snapshot=snap,
        )

    builder = StateGraph(MessagesState)
    builder.add_node("create", _tool_node)
    builder.add_edge(START, "create")
    builder.add_edge("create", END)

    graph = builder.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": f"lg-two-phase-{scenario}"}}

    # Initial turn: no duplicates exist yet -> pauses for confirmation.
    paused = await graph.ainvoke({"messages": []}, config=cfg)
    assert "__interrupt__" in paused
    intr_id = paused["__interrupt__"][0].id
    assert llm_calls == []

    # While waiting for confirmation, the duplicate workspace is created.
    live_workspaces.append(
        {
            "workspace_id": dup_ws_id,
            "user_id": new_ws_owner,
            "name": new_ws_name,
            "status": "stopped",
            "owner_name": owner_name,
            "owner_email": owner_email,
            "unit_ticker": None,
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        }
    )

    resumed = await graph.ainvoke(
        LGCommand(resume={intr_id: {"decisions": [{"type": "approve"}]}}),
        config=cfg,
    )

    # Assert Phase 1 returned an explicit match naming the exact workspace ID,
    # ran lock-free (never under lock_user_writes), and aborted before locking.
    assert len(recheck_results) == 1
    assert recheck_results[0].status == "matched"
    assert recheck_results[0].matched_workspace_ids == (dup_ws_id,)
    assert len(llm_calls) == 1
    assert llm_calls[0]["lock_held"] is False
    assert new_ws_name in llm_calls[0]["prompt"]
    assert lock_state["lock_acquires"] == 0
    assert lock_state["inserts"] == 0

    tool_msg = resumed["messages"][-1]
    assert tool_msg.status == "error"
    body = json.loads(tool_msg.content)
    assert body["success"] is False
    assert body["code"] == "scope_changed"
    assert body["error"] == "情况已变化，请重新发起"

    # Positive control: with a non-duplicate candidate ('BABA') and LLM clear,
    # Phase 1 returns status='clear' with checked_candidates, Phase 2 verifies
    # deterministically under lock (zero LLM calls under lock), and INSERT runs.
    emit_match["flag"] = False
    live_workspaces[:] = [
        {
            "workspace_id": dup_ws_id,
            "user_id": USER_ID,
            "name": "BABA",
            "status": "stopped",
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "unit_ticker": None,
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        }
    ]
    llm_calls.clear()
    recheck_results.clear()
    cfg_ctrl = {"configurable": {"thread_id": f"lg-two-phase-ctrl-{scenario}"}}
    paused_ctrl = await graph.ainvoke({"messages": []}, config=cfg_ctrl)
    intr_ctrl = paused_ctrl["__interrupt__"][0].id
    llm_calls.clear()
    resumed_ctrl = await graph.ainvoke(
        LGCommand(resume={intr_ctrl: {"decisions": [{"type": "approve"}]}}),
        config=cfg_ctrl,
    )
    assert len(recheck_results) == 1
    assert recheck_results[0].status == "clear"
    assert recheck_results[0].checked_candidates == ((dup_ws_id, "BABA", None),)
    assert all(call["lock_held"] is False for call in llm_calls)
    assert lock_state["lock_acquires"] == 1
    assert lock_state["inserts"] == 1
    assert json.loads(resumed_ctrl["messages"][-1].content)["success"] is True


@pytest.mark.asyncio
async def test_lock_free_company_dedup_parse_failure_returns_unavailable_not_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Decision 3: a malformed/unparseable LLM response (e.g. returning
    ``parsed`` instead of ``data``) produces status='unavailable' (never
    status='matched' with workspace IDs), and workspaces_create aborts with
    scope_changed without acquiring the write lock."""
    from contextlib import asynccontextmanager

    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import (
        DedupLlmResult,
        DedupMatchItem,
        recheck_company_workspace_before_create,
    )
    from src.tools.secretary.tools import workspaces_create

    tencent_row = {
        "workspace_id": "55555555-5555-5555-5555-555555555555",
        "user_id": USER_ID,
        "name": "腾讯控股",
        "status": "stopped",
        "owner_name": "Alice",
        "owner_email": "alice@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    class _ParseFailCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM workspaces w" in text:
                self._rows = [dict(tencent_row)]
            else:
                self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _ParseFailConn:
        def cursor(self, **_kwargs):
            return _ParseFailCursor()

    @asynccontextmanager
    async def _use_conn(c=None):
        yield c or _ParseFailConn()

    async def _malformed_dalton_complete(**_kwargs):
        # Deliberately return `parsed` instead of `data` so _parse_llm_payload fails.
        return SimpleNamespace(
            ok=True,
            parsed=DedupLlmResult(
                matches=[DedupMatchItem(id="c1", confidence="high", reason="bad field")]
            ),
        )

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val("internal"),
        ),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _malformed_dalton_complete)
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    recheck = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert recheck.status == "unavailable"
    assert recheck.matched_workspace_ids == ()

    # End-to-end in workspaces_create: unavailable check returns scope_changed
    # without calling WorkspaceManager.create_workspace.
    mgr = create_autospec(WorkspaceManager, instance=True)
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
        "src.tools.secretary.tools.hitl_confirm",
        lambda _action, _payload: (True, {"decisions": [{"type": "approve"}]}),
    )
    snap = {
        "action": "create_workspace",
        "tool_call_id": "call-parse-fail",
        "workspace_name": "700.HK",
        "workspace_description": "",
        "computer_id": CLOUD_1,
        "computer_name": "Cloud Primary",
        "computer_kind": "docker",
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
        "duplicate_workspace_id": None,
        "duplicate_workspace_name": None,
        "duplicate_check_unavailable": False,
        "precheck_error": None,
    }
    cmd = await workspaces_create(
        USER_ID, "700.HK", "", "call-parse-fail", preapproved=False, snapshot=snap
    )
    msg = cmd.update["messages"][0]
    assert msg.status == "error"
    assert json.loads(msg.content)["code"] == "scope_changed"
    mgr.create_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_in_lock_deterministic_recheck_uses_single_conn_no_llm_and_catches_colleague_and_candidate_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Decision 1: Phase 2 in-lock recheck never calls the LLM and
    never acquires a second connection from a 1-connection pool. It catches:
    (a) a colleague workspace matching normalized ticker '0700.HK', and
    (b) a lock-free checked candidate whose name or unit_ticker drifted."""
    from contextlib import asynccontextmanager

    from src.dalton import sharing as sharing_mod
    from src.dalton.coverage import dedup as dedup_mod
    from src.server.database import workspace as ws_db

    pool_state = {"in_use": False, "acquires": 0}
    healthy_computer = _computer_row(
        CLOUD_1, name="Cloud Primary", kind="docker", is_primary=True
    )
    team_workspaces: list[dict[str, Any]] = []

    class _SingleConnCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_members" in text:
                raise AssertionError(
                    "Phase 2 must not query dalton_members (it contains invited external members)"
                )
            if "FROM workspaces w" in text:
                self._rows = [dict(r) for r in team_workspaces]
                return
            if "FROM workspaces" in text and isinstance(params, dict) and "user_id" in params:
                uid_filter = str(params["user_id"])
                self._rows = [
                    {"workspace_id": r["workspace_id"], "name": r["name"]}
                    for r in team_workspaces
                    if r["user_id"] == uid_filter
                ]
                return
            if "FROM computers" in text:
                self._rows = [dict(healthy_computer)]
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._rows[0] if self._rows else None

    class _SingleConn:
        def cursor(self, **_kwargs):
            return _SingleConnCursor()

        @asynccontextmanager
        async def transaction(self):
            yield

    tx_conn = _SingleConn()

    @asynccontextmanager
    async def _single_pool_get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        if pool_state["in_use"]:
            raise TimeoutError("Connection pool exhausted (max_size=1)")
        pool_state["in_use"] = True
        pool_state["acquires"] += 1
        try:
            yield tx_conn
        finally:
            pool_state["in_use"] = False

    @asynccontextmanager
    async def _single_pool_folder_allocation(_computer_id: str, conn=None):
        async with _single_pool_get_db_connection(conn) as owned:
            yield owned

    async def _forbid_llm(**_kwargs):
        raise AssertionError("LLM must never be called during Phase 2 in-lock recheck")

    async def _noop_lock(_cur, _uid: str) -> None:
        return None

    monkeypatch.setattr(ws_db, "folder_allocation", _single_pool_folder_allocation)
    monkeypatch.setattr(ws_db, "get_db_connection", _single_pool_get_db_connection)
    monkeypatch.setattr(ws_db, "lock_user_writes", _noop_lock)
    monkeypatch.setattr(dedup_mod, "dalton_complete", _forbid_llm)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com", "bob@team.com"}),
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)

    # (a) Colleague added '0700.HK' between Phase 1 and Phase 2 -> refused on single conn.
    team_workspaces[:] = [
        {
            "workspace_id": "55555555-5555-5555-5555-555555555555",
            "user_id": "user-colleague-1",
            "name": "0700.HK",
            "owner_email": "bob@team.com",
            "unit_ticker": None,
        }
    ]
    res_colleague = await ws_db.create_workspace_on_computer(
        USER_ID,
        "700.HK",
        CLOUD_1,
        reject_unavailable=True,
        expected_computer_name="Cloud Primary",
        expected_computer_kind="docker",
        check_duplicate_snapshot=True,
        check_colleague_duplicate=True,
    )
    assert res_colleague is None
    assert pool_state["acquires"] == 1

    # (b) Candidate 'BABA' observed in Phase 1 was renamed to '腾讯控股' before Phase 2 -> refused.
    team_workspaces[:] = [
        {
            "workspace_id": "55555555-5555-5555-5555-555555555555",
            "user_id": USER_ID,
            "name": "腾讯控股",
            "owner_email": "alice@team.com",
            "unit_ticker": None,
        }
    ]
    res_drift = await ws_db.create_workspace_on_computer(
        USER_ID,
        "700.HK",
        CLOUD_1,
        reject_unavailable=True,
        expected_computer_name="Cloud Primary",
        expected_computer_kind="docker",
        check_duplicate_snapshot=True,
        verified_company_candidates=[("55555555-5555-5555-5555-555555555555", "BABA", None)],
        check_colleague_duplicate=True,
    )
    assert res_drift is None
    assert pool_state["acquires"] == 2


@pytest.mark.asyncio
async def test_in_lock_recheck_roles_sql_failure_rolls_back_savepoint_and_keeps_outer_tx_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 8 Decision 2: when the ``dalton_user_roles`` query fails with a SQL
    error inside Phase 2 in-lock recheck, the error propagates out of the
    savepoint (never swallowed to return an empty set), rolls back the
    savepoint cleanly, keeps the outer transaction healthy for subsequent
    queries, and returns False (scope_changed)."""
    from contextlib import asynccontextmanager

    from src.server.database import workspace as ws_db

    tx_state = {
        "in_failed_tx": False,
        "savepoint_rolled_back": False,
        "savepoint_released": False,
    }

    class _StrictSavepointCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if tx_state["in_failed_tx"]:
                raise RuntimeError("InFailedSqlTransaction: current transaction is aborted")
            text = " ".join(sql.split())
            if "FROM dalton_user_roles" in text:
                tx_state["in_failed_tx"] = True
                raise RuntimeError("simulated dalton_user_roles SQL failure")
            if "FROM workspaces" in text:
                self._rows = []
                return
            self._rows = [{"ok": 1}]

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._rows[0] if self._rows else None

    class _StrictSavepointConn:
        def cursor(self, **_kwargs):
            return _StrictSavepointCursor()

        @asynccontextmanager
        async def transaction(self):
            try:
                yield
            except BaseException:
                # PostgreSQL ROLLBACK TO SAVEPOINT clears the failed state.
                tx_state["savepoint_rolled_back"] = True
                tx_state["in_failed_tx"] = False
                raise
            else:
                if tx_state["in_failed_tx"]:
                    raise RuntimeError("InFailedSqlTransaction: cannot RELEASE SAVEPOINT")
                tx_state["savepoint_released"] = True

    conn = _StrictSavepointConn()
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: True)

    async with conn.cursor() as cur:
        unchanged = await ws_db.check_duplicate_snapshot_unchanged(
            cur,
            USER_ID,
            "700.HK",
            check_colleague_duplicate=True,
            conn=conn,
        )
        assert unchanged is False
        assert tx_state["savepoint_rolled_back"] is True
        assert tx_state["savepoint_released"] is False
        assert tx_state["in_failed_tx"] is False

        # Outer transaction remains usable after savepoint rollback.
        await cur.execute("SELECT 1 AS ok")
        assert await cur.fetchone() == {"ok": 1}


@pytest.mark.asyncio
async def test_in_lock_recheck_catches_colleague_workspace_with_registered_unit_ticker_00700(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 1 (Codex #1): when a colleague creates a workspace named
    '腾讯控股' with registered company coverage unit ticker '00700' after Phase 1
    and before the write lock is acquired, approving '700.HK' is rejected by
    Phase 2 using the shared coverage-unit ticker normalizer (normalize_ticker_key:
    '700.HK' == '00700') and returns scope_changed with zero INSERTs."""
    from contextlib import asynccontextmanager

    from src.dalton import sharing as sharing_mod
    from src.dalton.coverage.dedup import CompanyDedupRecheckSnapshot
    from src.server.database import workspace as ws_db
    from src.tools.secretary.tools import workspaces_create

    healthy_computer = _computer_row(
        CLOUD_1, name="Cloud Primary", kind="docker", is_primary=True
    )
    inserts = {"count": 0}
    team_workspaces = [
        {
            "workspace_id": "66666666-6666-6666-6666-666666666666",
            "user_id": "user-colleague-1",
            "name": "腾讯控股",
            "owner_email": "bob@team.com",
            "unit_ticker": "00700",
        }
    ]

    class _UnitTickerCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "INSERT INTO workspaces" in text:
                inserts["count"] += 1
                self._rows = [
                    {
                        "workspace_id": "77777777-7777-7777-7777-777777777777",
                        "user_id": USER_ID,
                        "name": "700.HK",
                        "computer_id": CLOUD_1,
                        "status": "stopped",
                    }
                ]
                return
            if "FROM workspaces w" in text:
                self._rows = [dict(r) for r in team_workspaces]
                return
            if "FROM workspaces" in text and isinstance(params, dict) and "user_id" in params:
                self._rows = []
                return
            if "FROM computers" in text:
                self._rows = [dict(healthy_computer)]
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._rows[0] if self._rows else None

    class _UnitTickerConn:
        def cursor(self, **_kwargs):
            return _UnitTickerCursor()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _use_conn(conn=None):
        yield conn or _UnitTickerConn()

    @asynccontextmanager
    async def _use_folder_alloc(_cid: str, conn=None):
        yield conn or _UnitTickerConn()

    async def _noop_lock(_cur, _uid: str) -> None:
        return None

    async def _phase1_clear(*_a, **_kw):
        # Phase 1 ran before the colleague's workspace/unit was committed.
        return CompanyDedupRecheckSnapshot(
            status="clear",
            checked_candidates=(),
            active=True,
            check_colleague_workspaces=True,
        )

    ws_mgr = object.__new__(WorkspaceManager)
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=ws_mgr),
    )
    monkeypatch.setattr(
        "src.server.services.workspace_manager.get_computer",
        create_autospec(
            lambda _cid: None,
            side_effect=lambda *_a, **_kw: _async_val(dict(healthy_computer)),
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_computer",
        create_autospec(
            lambda _cid, conn=None: None,
            side_effect=lambda *_a, **_kw: _async_val(dict(healthy_computer)),
        ),
    )
    monkeypatch.setattr(ws_db, "folder_allocation", _use_folder_alloc)
    monkeypatch.setattr(ws_db, "get_db_connection", _use_conn)
    monkeypatch.setattr(ws_db, "lock_user_writes", _noop_lock)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com", "bob@team.com"}),
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)
    monkeypatch.setattr(
        "src.dalton.coverage.dedup.recheck_company_workspace_before_create",
        _phase1_clear,
    )
    monkeypatch.setattr(
        "src.tools.secretary.tools.hitl_confirm",
        lambda _action, _payload: (True, {"decisions": [{"type": "approve"}]}),
    )

    snap = {
        "action": "create_workspace",
        "tool_call_id": "call-unit-00700",
        "workspace_name": "700.HK",
        "workspace_description": "",
        "computer_id": CLOUD_1,
        "computer_name": "Cloud Primary",
        "computer_kind": "docker",
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
        "duplicate_workspace_id": None,
        "duplicate_workspace_name": None,
        "duplicate_check_unavailable": False,
        "precheck_error": None,
    }
    cmd = await workspaces_create(
        USER_ID, "700.HK", "", "call-unit-00700", preapproved=False, snapshot=snap
    )
    msg = cmd.update["messages"][0]
    assert msg.status == "error"
    assert json.loads(msg.content)["code"] == "scope_changed"
    assert inserts["count"] == 0


@pytest.mark.asyncio
async def test_invited_external_member_with_0700hk_yields_consistent_result_across_both_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 1 (Codex #3): an invited external member (present in
    ``dalton_members`` but NOT in ``DALTON_ALLOWED_EMAILS`` and NOT a PM) who
    already owns '0700.HK' is classified identically by Phase 1 (lock-free) and
    Phase 2 (in-lock) via the shared ``is_internal_team_owner`` helper: both
    exclude the external member's workspace and allow the internal caller's
    '700.HK'."""
    from contextlib import asynccontextmanager

    from src.dalton import sharing as sharing_mod
    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import (
        DedupLlmResult,
        recheck_company_workspace_before_create,
    )
    from src.server.database import workspace as ws_db

    external_ws_row = {
        "workspace_id": "88888888-8888-8888-8888-888888888888",
        "user_id": "user-external-invited",
        "name": "0700.HK",
        "status": "stopped",
        "owner_name": "Guest",
        "owner_email": "ext@guest.com",
        "unit_ticker": "00700",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    class _ConsistentCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_members" in text:
                raise AssertionError(
                    "Neither Phase 1 nor Phase 2 should query dalton_members for internal classification"
                )
            if "FROM workspaces w" in text:
                self._rows = [dict(external_ws_row)]
                return
            if "FROM workspaces" in text and isinstance(params, dict) and "user_id" in params:
                self._rows = []
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._rows[0] if self._rows else None

    class _ConsistentConn:
        def cursor(self, **_kwargs):
            return _ConsistentCursor()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _use_conn(c=None):
        yield c or _ConsistentConn()

    allowed = {"alice@team.com"}
    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val("internal"),
        ),
    )
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod, "get_dalton_allowed_emails", lambda: frozenset(allowed)
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        lambda **_kw: _async_val(SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))),
    )
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    # Invited external member is NOT in DALTON_ALLOWED_EMAILS -> both phases allow.
    phase1 = await recheck_company_workspace_before_create(USER_ID, "700.HK", channel="cos")
    assert phase1.status == "clear"
    assert phase1.checked_candidates == ()

    conn = _ConsistentConn()
    async with conn.cursor() as cur:
        phase2 = await ws_db.check_duplicate_snapshot_unchanged(
            cur,
            USER_ID,
            "700.HK",
            verified_company_candidates=phase1.checked_candidates,
            check_colleague_duplicate=phase1.check_colleague_workspaces,
            conn=conn,
        )
    assert phase2 is True

    # Control: when that email IS an internal allowlisted colleague, BOTH phases reject.
    allowed.add("ext@guest.com")
    phase1_internal = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert phase1_internal.status == "matched"
    async with conn.cursor() as cur:
        phase2_internal = await ws_db.check_duplicate_snapshot_unchanged(
            cur,
            USER_ID,
            "700.HK",
            verified_company_candidates=phase1_internal.checked_candidates,
            check_colleague_duplicate=phase1_internal.check_colleague_workspaces,
            conn=conn,
        )
    assert phase2_internal is False


@pytest.mark.asyncio
async def test_lock_free_company_dedup_partial_batch_failure_returns_unavailable_and_scope_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 9 Decision 2 (Codex #2): when Phase 1 splits candidates into
    multiple LLM batches and one batch returns empty matches while another batch
    fails to parse (no confirmed duplicate), recheck_company_workspace_before_create
    must return status='unavailable' (never 'clear'), causing workspaces_create
    to abort with scope_changed without acquiring the write lock."""
    from contextlib import asynccontextmanager

    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import (
        DedupLlmResult,
        recheck_company_workspace_before_create,
    )
    from src.tools.secretary.tools import workspaces_create

    ws_rows = [
        {
            "workspace_id": "99999999-9999-9999-9999-999999999991",
            "user_id": USER_ID,
            "name": "BABA",
            "status": "stopped",
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        },
        {
            "workspace_id": "99999999-9999-9999-9999-999999999992",
            "user_id": USER_ID,
            "name": "JD",
            "status": "stopped",
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        },
    ]

    class _TwoCandidateCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM workspaces w" in text:
                self._rows = [dict(r) for r in ws_rows]
            else:
                self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _TwoCandidateConn:
        def cursor(self, **_kwargs):
            return _TwoCandidateCursor()

    @asynccontextmanager
    async def _use_conn(c=None):
        yield c or _TwoCandidateConn()

    batch_calls = {"count": 0}

    async def _partial_fail_complete(**_kwargs):
        batch_calls["count"] += 1
        if batch_calls["count"] == 1:
            return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))
        # Second batch returns malformed payload (missing `data`) -> parse failure.
        return SimpleNamespace(ok=True, parsed=DedupLlmResult(matches=[]))

    monkeypatch.setattr(dedup_mod, "DEDUP_BATCH_SIZE", 1)
    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val("internal"),
        ),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "dalton_complete", _partial_fail_complete)
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    recheck = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert batch_calls["count"] == 2
    assert recheck.status == "unavailable"
    assert recheck.matched_workspace_ids == ()

    # End-to-end in workspaces_create: partial failure -> scope_changed without write lock.
    batch_calls["count"] = 0
    mgr = create_autospec(WorkspaceManager, instance=True)
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr),
    )
    monkeypatch.setattr(
        "src.tools.secretary.tools.hitl_confirm",
        lambda _action, _payload: (True, {"decisions": [{"type": "approve"}]}),
    )
    snap = {
        "action": "create_workspace",
        "tool_call_id": "call-partial-fail",
        "workspace_name": "700.HK",
        "workspace_description": "",
        "computer_id": CLOUD_1,
        "computer_name": "Cloud Primary",
        "computer_kind": "docker",
        "computer_is_own_machine": False,
        "bootstrap_cloud_primary": False,
        "duplicate_workspace_id": None,
        "duplicate_workspace_name": None,
        "duplicate_check_unavailable": False,
        "precheck_error": None,
    }
    cmd = await workspaces_create(
        USER_ID, "700.HK", "", "call-partial-fail", preapproved=False, snapshot=snap
    )
    msg = cmd.update["messages"][0]
    assert msg.status == "error"
    assert json.loads(msg.content)["code"] == "scope_changed"
    mgr.create_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_manager_bootstrap_keeps_new_primary_computer_if_insert_aborts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 Decision 1: if create_workspace_on_computer returns None after
    bootstrap_cloud_primary_computer minted a cloud primary, WorkspaceManager
    raises ApprovalScopeChangedError and keeps the new primary computer rather
    than trying to delete/reclaim it."""
    from src.server.database import workspace as ws_db

    ws_mgr = object.__new__(WorkspaceManager)
    bootstrap_mock = create_autospec(
        ws_mgr.bootstrap_cloud_primary_computer,
        return_value=_computer_row(CLOUD_2, name="FirstWS", kind="docker", is_primary=True),
    )
    ws_mgr.bootstrap_cloud_primary_computer = bootstrap_mock
    monkeypatch.setattr(
        "src.server.services.workspace_manager.create_workspace_on_computer",
        create_autospec(
            ws_db.create_workspace_on_computer,
            return_value=None,
        ),
    )

    assert not hasattr(WorkspaceManager, "_reclaim_empty_bootstrapped_computer")
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


@pytest.mark.asyncio
async def test_lock_free_company_dedup_archived_00700_unit_never_returns_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 (Codex R9 #1): when a 00700 coverage unit is active during
    candidate collection, matched by the LLM, and archived before
    _refresh_live_hits runs, deterministic ticker matching must not re-count the
    archived unit as status='matched':
    - all batches succeed -> status='clear'
    - another batch fails to parse -> status='unavailable'
    - LLM returns empty hits while unit becomes archived -> live verification
      drops it and returns status='clear'."""
    from contextlib import asynccontextmanager

    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import (
        DedupLlmResult,
        DedupMatchItem,
        recheck_company_workspace_before_create,
    )

    unit_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    baba_ws_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    unit_state = {"status": "active"}
    extra_workspaces: list[dict[str, Any]] = []

    def _unit_row() -> dict[str, Any]:
        return {
            "unit_id": unit_id,
            "kind": "company",
            "status": unit_state["status"],
            "owner_user_id": USER_ID,
            "lead_analyst_user_id": USER_ID,
            "display_name": "腾讯控股",
            "workspace_id": None,
            "ticker": "00700",
            "unit_updated_at": None,
            "unit_created_at": None,
            "workspace_name": None,
            "workspace_owner_id": None,
            "workspace_status": None,
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "workspace_last_activity_at": None,
            "workspace_updated_at": None,
            "workspace_created_at": None,
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "unit_owner_email": "alice@team.com",
            "lead_name": "Alice",
            "lead_email": "alice@team.com",
        }

    class _ArchivedUnitCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_coverage_units" in text:
                row = _unit_row()
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [row] if unit_id in wanted else []
                else:
                    self._rows = [row] if row["status"] == "active" else []
                return
            if "FROM workspaces w" in text:
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [
                        dict(r) for r in extra_workspaces if r["workspace_id"] in wanted
                    ]
                else:
                    self._rows = [dict(r) for r in extra_workspaces]
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _ArchivedUnitConn:
        def cursor(self, **_kwargs):
            return _ArchivedUnitCursor()

    @asynccontextmanager
    async def _use_conn(c=None):
        yield c or _ArchivedUnitConn()

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val("internal"),
        ),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    # Case 1: All batches succeed; LLM matches the 00700 unit, which becomes
    # archived while waiting for the LLM -> must return status='clear'.
    async def _llm_hits_then_archives(**_kwargs):
        unit_state["status"] = "archived"
        return SimpleNamespace(
            ok=True,
            data=DedupLlmResult(
                matches=[
                    DedupMatchItem(
                        id="c1", confidence="high", reason="00700 is Tencent"
                    )
                ]
            ),
        )

    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_hits_then_archives)
    unit_state["status"] = "active"
    res_clear = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert res_clear.status == "clear"
    assert res_clear.matched_workspace_ids == ()
    assert res_clear.has_unbound_or_unreadable_match is False

    # Case 2: Another batch fails to parse while batch 1 hits the 00700 unit
    # that becomes archived before _refresh_live_hits -> must return 'unavailable'
    # (never 'matched').
    extra_workspaces.append(
        {
            "workspace_id": baba_ws_id,
            "user_id": USER_ID,
            "name": "BABA",
            "status": "stopped",
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        }
    )
    monkeypatch.setattr(dedup_mod, "DEDUP_BATCH_SIZE", 1)

    async def _llm_batch_hit_archive_and_parse_fail(**kwargs):
        prompt = str(kwargs.get("user_prompt") or "")
        if "00700" in prompt:
            unit_state["status"] = "archived"
            return SimpleNamespace(
                ok=True,
                data=DedupLlmResult(
                    matches=[
                        DedupMatchItem(
                            id="c1", confidence="high", reason="00700 is Tencent"
                        )
                    ]
                ),
            )
        # Second batch returns malformed payload (missing `data`) -> parse failure.
        return SimpleNamespace(ok=True, parsed=DedupLlmResult(matches=[]))

    monkeypatch.setattr(
        dedup_mod, "dalton_complete", _llm_batch_hit_archive_and_parse_fail
    )
    unit_state["status"] = "active"
    res_unavail = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert res_unavail.status == "unavailable"
    assert res_unavail.matched_workspace_ids == ()
    assert res_unavail.has_unbound_or_unreadable_match is False

    # Case 3: LLM misses the 00700 unit while it becomes archived -> deterministic
    # fallback verifies live status via DB and drops the archived unit -> 'clear'.
    extra_workspaces.clear()

    async def _llm_empty_then_archives(**_kwargs):
        unit_state["status"] = "archived"
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_empty_then_archives)
    unit_state["status"] = "active"
    res_empty_clear = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert res_empty_clear.status == "clear"
    assert res_empty_clear.matched_workspace_ids == ()
    assert res_empty_clear.has_unbound_or_unreadable_match is False


@pytest.mark.asyncio
async def test_own_00700_workspace_listed_on_card_and_approved_700hk_creates_without_scope_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10 (Codex R9 #2): when the caller already owns a live workspace
    named '00700' and proposes creating '700.HK':
    - proposal snapshot (duplicate_workspace / is_duplicate_name) recognizes
      '00700' via canonical_workspace_ticker_key and binds
      duplicate_workspace_id / duplicate_workspace_name (withholding standing
      auto-approval and listing '00700' on the confirmation card);
    - after the user explicitly approves creating another workspace, lock-free
      Phase 1 excludes expected_duplicate_workspace_id and returns 'clear',
      in-lock Phase 2 accepts the disclosed expected duplicate, and INSERT
      succeeds instead of deadlocking with scope_changed."""
    from contextlib import asynccontextmanager

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.types import Command as LGCommand

    from src.dalton import sharing as sharing_mod
    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import DedupLlmResult
    from src.server.database import workspace as ws_db
    from src.tools.secretary.approvals import (
        _snapshot_allows_standing_create,
        build_create_workspace_snapshot,
        is_duplicate_name,
    )
    from src.tools.secretary.tools import workspaces_create

    assert is_duplicate_name("700.HK", "00700") is True
    assert is_duplicate_name("00700", "700.HK") is True
    assert is_duplicate_name("计划①", "计划1") is False

    own_ws_id = "44444444-4444-4444-4444-444444444444"
    healthy_computer = _computer_row(
        CLOUD_1, name="Cloud Primary", kind="docker", is_primary=True
    )
    live_workspaces = [
        {
            "workspace_id": own_ws_id,
            "user_id": USER_ID,
            "name": "00700",
            "status": "stopped",
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "unit_ticker": None,
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "last_activity_at": None,
            "updated_at": None,
            "created_at": None,
        }
    ]
    inserts = {"count": 0}
    recheck_results: list[Any] = []

    class _Own00700Cursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "INSERT INTO workspaces" in text:
                inserts["count"] += 1
                self._rows = [
                    {
                        "workspace_id": WS_1,
                        "user_id": USER_ID,
                        "name": "700.HK",
                        "description": "Tencent HK",
                        "computer_id": CLOUD_1,
                        "dir_name": "700.HK",
                        "status": "stopped",
                    }
                ]
                return
            if "FROM computers" in text:
                self._rows = [dict(healthy_computer)]
                return
            if "SELECT dir_name FROM workspaces" in text:
                self._rows = [{"dir_name": "00700"}]
                return
            if "FROM workspaces w" in text:
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [
                        dict(r) for r in live_workspaces if r["workspace_id"] in wanted
                    ]
                else:
                    self._rows = [dict(r) for r in live_workspaces]
                return
            if (
                "FROM workspaces" in text
                and isinstance(params, dict)
                and "user_id" in params
            ):
                uid_filter = str(params["user_id"])
                self._rows = [
                    {"workspace_id": r["workspace_id"], "name": r["name"]}
                    for r in live_workspaces
                    if r["user_id"] == uid_filter
                ]
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

        async def fetchone(self) -> dict[str, Any] | None:
            return self._rows[0] if self._rows else None

    class _Own00700Conn:
        def cursor(self, **_kwargs):
            return _Own00700Cursor()

        @asynccontextmanager
        async def transaction(self):
            yield

    @asynccontextmanager
    async def _use_conn(conn=None):
        yield conn or _Own00700Conn()

    @asynccontextmanager
    async def _use_folder_alloc(_cid: str, conn=None):
        yield conn or _Own00700Conn()

    async def _noop_lock(_cur, _uid: str) -> None:
        return None

    orig_recheck = dedup_mod.recheck_company_workspace_before_create

    async def _spy_recheck(*args, **kwargs):
        res = await orig_recheck(*args, **kwargs)
        recheck_results.append(res)
        return res

    real_ws_mgr = object.__new__(WorkspaceManager)
    real_ws_mgr.resolve_binding = create_autospec(
        real_ws_mgr.resolve_binding, return_value=None
    )
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=real_ws_mgr),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_primary_computer",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val(dict(healthy_computer)),
        ),
    )
    monkeypatch.setattr(
        "src.server.services.workspace_manager.get_computer",
        create_autospec(
            lambda _cid: None,
            side_effect=lambda *_a, **_kw: _async_val(dict(healthy_computer)),
        ),
    )
    monkeypatch.setattr(
        ws_db,
        "get_computer",
        create_autospec(
            lambda _cid, conn=None: None,
            side_effect=lambda *_a, **_kw: _async_val(dict(healthy_computer)),
        ),
    )
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        create_autospec(
            _REAL_GET_LIVE_WORKSPACE_NAMES,
            return_value=[{"workspace_id": own_ws_id, "name": "00700"}],
        ),
    )
    monkeypatch.setattr(ws_db, "get_db_connection", _use_conn)
    monkeypatch.setattr(ws_db, "folder_allocation", _use_folder_alloc)
    monkeypatch.setattr(ws_db, "lock_user_writes", _noop_lock)
    monkeypatch.setattr(
        ws_db,
        "start_new_workspace_selection",
        create_autospec(ws_db.start_new_workspace_selection, return_value=None),
    )
    monkeypatch.setattr(
        "src.tools.secretary.tools._folder_ready_result",
        create_autospec(
            _folder_ready_result,
            return_value={"folder_ready": False, "folder_path": None},
        ),
    )
    monkeypatch.setattr(dedup_mod, "recheck_company_workspace_before_create", _spy_recheck)
    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr("src.dalton.settings.is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(sharing_mod, "is_dalton_local_auth_enabled", lambda: False)
    monkeypatch.setattr(
        sharing_mod,
        "get_dalton_allowed_emails",
        lambda: frozenset({"alice@team.com"}),
    )
    monkeypatch.setattr(sharing_mod, "is_invite_pm", lambda _u: False)
    monkeypatch.setattr(sharing_mod, "is_company_system_user", lambda _u: False)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(
            lambda _uid: None,
            side_effect=lambda *_a, **_kw: _async_val("internal"),
        ),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    # LLM returns empty matches (mirrors the exact scenario where only the
    # deterministic canonical ticker key links '700.HK' and '00700').
    monkeypatch.setattr(
        dedup_mod,
        "dalton_complete",
        lambda **_kw: _async_val(SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))),
    )
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    # Proposal phase: snapshot identifies '00700' and withholds standing auto-approval.
    call = {
        "id": "call-own-00700",
        "name": "manage_workspaces",
        "args": {"action": "create", "name": "700.HK", "description": "Tencent HK"},
    }
    snap = await build_create_workspace_snapshot(USER_ID, call)
    assert snap is not None
    assert snap["duplicate_workspace_id"] == own_ws_id
    assert snap["duplicate_workspace_name"] == "00700"
    assert _snapshot_allows_standing_create(snap) is False

    async def _tool_node(_state: MessagesState):
        return await workspaces_create(
            USER_ID,
            "700.HK",
            "Tencent HK",
            "call-own-00700",
            preapproved=False,
            snapshot=snap,
        )

    builder = StateGraph(MessagesState)
    builder.add_node("create", _tool_node)
    builder.add_edge(START, "create")
    builder.add_edge("create", END)

    graph = builder.compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "lg-own-00700-700hk"}}

    # Confirmation card lists '00700' as duplicate_workspace_id / duplicate_workspace_name.
    paused = await graph.ainvoke({"messages": []}, config=cfg)
    assert "__interrupt__" in paused
    intr = paused["__interrupt__"][0]
    card_req = intr.value["action_requests"][0]
    assert card_req["type"] == "create_workspace"
    assert card_req["duplicate_workspace_id"] == own_ws_id
    assert card_req["duplicate_workspace_name"] == "00700"

    # User explicitly approves creating another workspace -> Phase 1 excludes
    # own_ws_id and returns 'clear', Phase 2 matches expected duplicate and
    # INSERT succeeds (never scope_changed).
    resumed = await graph.ainvoke(
        LGCommand(resume={intr.id: {"decisions": [{"type": "approve"}]}}),
        config=cfg,
    )
    assert len(recheck_results) == 1
    assert recheck_results[0].status == "clear"
    assert inserts["count"] == 1
    tool_msg = resumed["messages"][-1]
    body = json.loads(tool_msg.content)
    assert body["success"] is True
    assert body["workspace_name"] == "700.HK"


@pytest.mark.asyncio
async def test_update_workspace_unchanged_name_skips_sibling_check_but_rename_blocks_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 11 (Codex R10 #1): updating description/config with unchanged
    name='700' must skip lock_user_writes and sibling is_duplicate_name check
    even when live sibling '00700' exists; renaming to '700.HK' still checks
    siblings and raises WorkspaceNameTaken."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec

    from src.server.database import workspace as ws_db
    from src.server.database.workspace_names import WorkspaceNameTaken

    ws_700 = "cccccccc-cccc-4ccc-8ccc-000000000700"
    ws_00700 = "cccccccc-cccc-4ccc-8ccc-000000007000"
    sql_log: list[str] = []

    class _UpdateCursor:
        def __init__(self) -> None:
            self._one: dict[str, Any] | None = None
            self._all: list[dict[str, Any]] = []

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            sql_log.append(text)
            self._one = None
            self._all = []
            if text.startswith("SELECT user_id, name FROM workspaces WHERE workspace_id ="):
                self._one = {"user_id": USER_ID, "name": "700"}
            elif "FROM workspaces WHERE workspace_id =" in text and text.startswith("SELECT "):
                self._one = {
                    "workspace_id": ws_700,
                    "user_id": USER_ID,
                    "name": "700",
                    "description": "updated desc",
                }
            elif "SELECT workspace_id, name FROM workspaces" in text:
                self._all = [
                    {"workspace_id": ws_700, "name": "700"},
                    {"workspace_id": ws_00700, "name": "00700"},
                ]
            elif text.startswith("UPDATE workspaces SET"):
                self._one = {
                    "workspace_id": ws_700,
                    "user_id": USER_ID,
                    "name": "700",
                    "description": "updated desc",
                }

        async def fetchone(self) -> dict[str, Any] | None:
            return self._one

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._all)

    shared_cur = _UpdateCursor()

    @asynccontextmanager
    async def _fake_ws_tx(_conn=None):
        yield shared_cur

    lock_mock = create_autospec(ws_db.lock_user_writes, return_value=None)
    monkeypatch.setattr(ws_db, "_ws_transaction", _fake_ws_tx)
    monkeypatch.setattr(ws_db, "lock_user_writes", lock_mock)

    updated = await ws_db.update_workspace(
        ws_700,
        name="700",
        description="updated desc",
    )
    assert updated is not None
    assert updated["description"] == "updated desc"
    assert lock_mock.await_count == 0
    assert not any("SELECT workspace_id, name FROM workspaces" in s for s in sql_log)
    update_sqls = [s for s in sql_log if s.startswith("UPDATE workspaces SET")]
    assert len(update_sqls) == 1
    assert "description = %s" in update_sqls[0]
    assert "name = %s" not in update_sqls[0]
    assert "name_key = %s" not in update_sqls[0]

    # Pure same-name no-op (name="700" with no description/config/is_pinned) skips UPDATE too.
    sql_log.clear()
    noop_row = await ws_db.update_workspace(ws_700, name="700")
    assert noop_row is not None
    assert lock_mock.await_count == 0
    assert any(s.startswith("SELECT") for s in sql_log)
    assert not any(s.startswith("UPDATE workspaces SET") for s in sql_log)

    sql_log.clear()
    with pytest.raises(WorkspaceNameTaken) as exc_info:
        await ws_db.update_workspace(ws_700, name="700.HK")
    assert exc_info.value.workspace_id == ws_00700
    assert exc_info.value.name == "00700"
    assert lock_mock.await_count == 1
    assert any("SELECT workspace_id, name FROM workspaces" in s for s in sql_log)
    assert not any(s.startswith("UPDATE workspaces SET") for s in sql_log)


@pytest.mark.asyncio
async def test_lock_free_company_dedup_keeps_live_workspace_when_bound_unit_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 11 (Codex R10 #2): when a bound 00700 coverage unit archives while
    its underlying workspace 'ws-00700' (name='00700') stays active, both LLM-hit
    _refresh_live_hits and deterministic fallback preserve 'ws-00700' as an
    unclassified matched workspace."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec

    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import (
        DedupLlmResult,
        DedupMatchItem,
        recheck_company_workspace_before_create,
    )

    unit_id = "aaaaaaaa-aaaa-4aaa-8aaa-000000000001"
    ws_00700 = "bbbbbbbb-bbbb-4bbb-8bbb-000000000700"
    unit_state = {"status": "active"}

    def _unit_row() -> dict[str, Any]:
        return {
            "unit_id": unit_id,
            "kind": "company",
            "status": unit_state["status"],
            "owner_user_id": USER_ID,
            "lead_analyst_user_id": USER_ID,
            "display_name": "腾讯控股",
            "workspace_id": ws_00700,
            "ticker": "00700",
            "unit_updated_at": None,
            "unit_created_at": None,
            "workspace_name": "00700",
            "workspace_owner_id": USER_ID,
            "workspace_status": "stopped",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "workspace_last_activity_at": None,
            "workspace_updated_at": None,
            "workspace_created_at": None,
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "unit_owner_email": "alice@team.com",
            "lead_name": "Alice",
            "lead_email": "alice@team.com",
        }

    live_ws_row = {
        "workspace_id": ws_00700,
        "user_id": USER_ID,
        "name": "00700",
        "status": "stopped",
        "owner_name": "Alice",
        "owner_email": "alice@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    class _BoundArchivedCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_coverage_units" in text:
                row = _unit_row()
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [row] if unit_id in wanted else []
                else:
                    self._rows = [row] if row["status"] == "active" else []
                return
            if "FROM workspaces w" in text:
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [dict(live_ws_row)] if ws_00700 in wanted else []
                else:
                    self._rows = []
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _BoundArchivedConn:
        def cursor(self, **_kwargs):
            return _BoundArchivedCursor()

    @asynccontextmanager
    async def _use_conn(c=None):
        yield c or _BoundArchivedConn()

    async def _internal_tier(*_a, **_kw) -> str:
        return "internal"

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "get_user_tier", _internal_tier)
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    # Case 1: LLM matches bound unit before unit archives, while ws-00700 stays live.
    async def _llm_hits_then_archives(**_kwargs):
        unit_state["status"] = "archived"
        return SimpleNamespace(
            ok=True,
            data=DedupLlmResult(
                matches=[
                    DedupMatchItem(
                        id="c1", confidence="high", reason="00700 workspace active"
                    )
                ]
            ),
        )

    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_hits_then_archives)
    unit_state["status"] = "active"
    res_hit = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert res_hit.status == "matched"
    assert ws_00700 in res_hit.matched_workspace_ids

    # Case 2: LLM returns no hits while bound unit archives -> deterministic
    # fallback preserves live ws-00700 as unclassified match.
    async def _llm_empty_then_archives(**_kwargs):
        unit_state["status"] = "archived"
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_empty_then_archives)
    unit_state["status"] = "active"
    res_fallback = await recheck_company_workspace_before_create(
        USER_ID, "700.HK", channel="cos"
    )
    assert res_fallback.status == "matched"
    assert ws_00700 in res_fallback.matched_workspace_ids


@pytest.mark.asyncio
async def test_lock_free_company_dedup_drops_unrelated_workspace_when_bound_unit_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 12 (Codex R11 #2): when an ordinary workspace '行业笔记' (no own ticker)
    only matched '700.HK' via its bound 00700 coverage unit and that unit archives
    during the LLM call, _refresh_live_hits re-checks '行业笔记' without unit info
    and drops it instead of falsely returning company_workspace_exists."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec

    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import (
        DedupLlmResult,
        DedupMatchItem,
        recheck_company_workspace_before_create,
    )

    unit_id = "aaaaaaaa-aaaa-4aaa-8aaa-000000000002"
    ws_notes = "bbbbbbbb-bbbb-4bbb-8bbb-000000000999"
    unit_state = {"status": "active"}

    def _unit_row() -> dict[str, Any]:
        return {
            "unit_id": unit_id,
            "kind": "company",
            "status": unit_state["status"],
            "owner_user_id": USER_ID,
            "lead_analyst_user_id": USER_ID,
            "display_name": "腾讯控股",
            "workspace_id": ws_notes,
            "ticker": "00700",
            "unit_updated_at": None,
            "unit_created_at": None,
            "workspace_name": "行业笔记",
            "workspace_owner_id": USER_ID,
            "workspace_status": "stopped",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "workspace_last_activity_at": None,
            "workspace_updated_at": None,
            "workspace_created_at": None,
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "unit_owner_email": "alice@team.com",
            "lead_name": "Alice",
            "lead_email": "alice@team.com",
        }

    live_ws_row = {
        "workspace_id": ws_notes,
        "user_id": USER_ID,
        "name": "行业笔记",
        "status": "stopped",
        "owner_name": "Alice",
        "owner_email": "alice@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    class _UnrelatedArchivedCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_coverage_units" in text:
                row = _unit_row()
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [row] if unit_id in wanted else []
                else:
                    self._rows = [row] if row["status"] == "active" else []
                return
            if "FROM workspaces w" in text:
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [dict(live_ws_row)] if ws_notes in wanted else []
                else:
                    self._rows = []
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _UnrelatedArchivedConn:
        def cursor(self, **_kwargs):
            return _UnrelatedArchivedCursor()

    @asynccontextmanager
    async def _use_conn(c=None):
        yield c or _UnrelatedArchivedConn()

    async def _internal_tier(*_a, **_kw) -> str:
        return "internal"

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "get_user_tier", _internal_tier)
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)

    llm_prompts: list[str] = []

    async def _llm_rejudge(**kwargs):
        llm_prompts.append(str(kwargs.get("user_prompt") or ""))
        if len(llm_prompts) == 1:
            unit_state["status"] = "archived"
            return SimpleNamespace(
                ok=True,
                data=DedupLlmResult(
                    matches=[DedupMatchItem(id="c1", confidence="high", reason="bound 00700 unit")]
                ),
            )
        # Re-judge receives only workspace "行业笔记" without unit ticker/display_name -> no match.
        return SimpleNamespace(ok=True, data=DedupLlmResult(matches=[]))

    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_rejudge)
    res = await recheck_company_workspace_before_create(USER_ID, "700.HK", channel="cos")
    assert res.status == "clear"
    assert res.matched_workspace_ids == ()
    assert len(llm_prompts) == 2
    assert "00700" in llm_prompts[0]
    assert "00700" not in llm_prompts[1]
    assert "行业笔记" in llm_prompts[1]


@pytest.mark.asyncio
async def test_find_existing_company_workspace_keeps_downgraded_hit_when_rejudge_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 13: _find_matches passes partial_failure_out to _refresh_live_hits;
    when a bound coverage unit archives during the first LLM call and re-judging
    the still-live workspace fails, find_existing_company_workspace must not
    silently drop the live hit and allow creation."""
    from contextlib import asynccontextmanager
    from unittest.mock import create_autospec

    from src.dalton.coverage import dedup as dedup_mod
    from src.dalton.coverage.dedup import (
        DedupLlmResult,
        DedupMatchItem,
        find_existing_company_workspace,
    )

    unit_id = "aaaaaaaa-aaaa-4aaa-8aaa-000000000013"
    ws_live = "bbbbbbbb-bbbb-4bbb-8bbb-000000000013"
    unit_state = {"status": "active"}

    def _unit_row() -> dict[str, Any]:
        return {
            "unit_id": unit_id,
            "kind": "company",
            "status": unit_state["status"],
            "owner_user_id": USER_ID,
            "lead_analyst_user_id": USER_ID,
            "display_name": "腾讯控股",
            "workspace_id": ws_live,
            "ticker": "00700",
            "unit_updated_at": None,
            "unit_created_at": None,
            "workspace_name": "腾讯研究",
            "workspace_owner_id": USER_ID,
            "workspace_status": "stopped",
            "is_home_stand_in": False,
            "is_historical_duplicate": False,
            "workspace_last_activity_at": None,
            "workspace_updated_at": None,
            "workspace_created_at": None,
            "owner_name": "Alice",
            "owner_email": "alice@team.com",
            "unit_owner_email": "alice@team.com",
            "lead_name": "Alice",
            "lead_email": "alice@team.com",
        }

    live_ws_row = {
        "workspace_id": ws_live,
        "user_id": USER_ID,
        "name": "腾讯研究",
        "status": "stopped",
        "owner_name": "Alice",
        "owner_email": "alice@team.com",
        "is_home_stand_in": False,
        "is_historical_duplicate": False,
        "last_activity_at": None,
        "updated_at": None,
        "created_at": None,
    }

    class _RejudgeFailCursor:
        def __init__(self) -> None:
            self._rows: list[dict[str, Any]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            text = " ".join(sql.split())
            if "FROM dalton_coverage_units" in text:
                row = _unit_row()
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [row] if unit_id in wanted else []
                else:
                    self._rows = [row] if row["status"] == "active" else []
                return
            if "FROM workspaces w" in text:
                if isinstance(params, tuple) and params and isinstance(params[0], list):
                    wanted = set(params[0])
                    self._rows = [dict(live_ws_row)] if ws_live in wanted else []
                else:
                    self._rows = []
                return
            self._rows = []

        async def fetchall(self) -> list[dict[str, Any]]:
            return list(self._rows)

    class _RejudgeFailConn:
        def cursor(self, **_kwargs):
            return _RejudgeFailCursor()

    @asynccontextmanager
    async def _use_conn(c=None):
        yield c or _RejudgeFailConn()

    monkeypatch.setattr(dedup_mod, "get_db_connection", _use_conn)
    monkeypatch.setattr(dedup_mod, "is_coverage_units_enabled", lambda: True)
    monkeypatch.setattr(dedup_mod, "is_dalton_supabase_auth_enabled", lambda: True)
    monkeypatch.setattr(
        dedup_mod,
        "get_user_tier",
        create_autospec(dedup_mod._REAL_GET_USER_TIER, return_value="internal"),
    )
    monkeypatch.setattr(dedup_mod, "_is_internal_team_owner", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_model",
        create_autospec(dedup_mod.resolve_assist_model, return_value="gpt-4o-mini"),
    )
    monkeypatch.setattr(
        dedup_mod,
        "resolve_assist_fallback_models",
        create_autospec(dedup_mod.resolve_assist_fallback_models, return_value=[]),
    )
    monkeypatch.setattr(dedup_mod, "model_requires_user_oauth", lambda _m: False)
    monkeypatch.setattr(
        dedup_mod.sharing_mod,
        "require_workspace_access",
        create_autospec(
            dedup_mod.sharing_mod.require_workspace_access,
            return_value=None,
        ),
    )

    call_count = {"n": 0}

    async def _llm_hit_then_fail(**_kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            unit_state["status"] = "archived"
            return SimpleNamespace(
                ok=True,
                data=DedupLlmResult(
                    matches=[
                        DedupMatchItem(
                            id="c1", confidence="high", reason="bound 00700 unit"
                        )
                    ]
                ),
            )
        raise TimeoutError("rejudge timed out")

    monkeypatch.setattr(dedup_mod, "dalton_complete", _llm_hit_then_fail)
    dup = await find_existing_company_workspace(USER_ID, "700.HK", channel="cos")
    assert dup is not None
    assert call_count["n"] == 2
    assert ws_live in dup.matched_workspace_ids
    assert dup.matches
    assert dup.matches[0].workspace_name == "腾讯研究"
    assert dup.suggestion is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_name", "requested_name"),
    [
        ("Research Notes", "Research  Notes"),
        ("Q3/Q4", "Q3-Q4"),
        ("Q3\\Q4", "Q3-Q4"),
    ],
)
async def test_card_and_locked_recheck_share_workspace_name_key_duplicate_rule(
    monkeypatch: pytest.MonkeyPatch,
    existing_name: str,
    requested_name: str,
) -> None:
    """Round 14 (Grok med / Codex low): confirmation card snapshot and locked
    check_duplicate_snapshot_unchanged share is_duplicate_name (including
    workspace_name_key folding of / \\ -> - and collapsed spaces) so folder-key
    collisions are surfaced on the card instead of failing with scope_changed."""
    from src.server.database import workspace as ws_db
    from src.tools.secretary.approvals import build_create_workspace_snapshot

    existing_id = "dddddddd-dddd-4ddd-8ddd-000000000014"
    monkeypatch.setattr(
        "src.server.database.computer.get_primary_computer",
        create_autospec(
            get_primary_computer,
            return_value=_computer_row(CLOUD_1, name="Cloud Primary", kind="docker"),
        ),
    )
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        create_autospec(
            _REAL_GET_LIVE_WORKSPACE_NAMES,
            return_value=[{"workspace_id": existing_id, "name": existing_name}],
        ),
    )

    snap = await build_create_workspace_snapshot(
        USER_ID,
        {
            "id": "call-dup-key-1",
            "name": "manage_workspaces",
            "args": {"action": "create", "name": requested_name},
        },
    )
    assert snap is not None
    assert snap["duplicate_workspace_id"] == existing_id
    assert snap["duplicate_workspace_name"] == existing_name

    # Locked recheck with the card's snapshot passes (no false scope_changed).
    class _FakeCur:
        async def execute(self, _sql: str, _params: Any = None) -> None:
            return None

        async def fetchall(self) -> list[dict[str, Any]]:
            return [{"workspace_id": existing_id, "name": existing_name}]

    ok_with_snap = await ws_db.check_duplicate_snapshot_unchanged(
        _FakeCur(),
        USER_ID,
        requested_name,
        expected_duplicate_workspace_id=existing_id,
        expected_duplicate_workspace_name=existing_name,
    )
    assert ok_with_snap is True

    # Without expected_duplicate_workspace_id (concurrent create after clean card),
    # locked recheck still rejects with False (-> scope_changed).
    ok_without_snap = await ws_db.check_duplicate_snapshot_unchanged(
        _FakeCur(),
        USER_ID,
        requested_name,
        expected_duplicate_workspace_id=None,
    )
    assert ok_without_snap is False


@pytest.mark.asyncio
async def test_claude_code_create_workspace_persists_snapshot_pauses_and_resumes_on_held_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 14 (Codex med 1): Claude Code waits_for_user recognizes
    manage_workspaces(action='create') via puts_card so the call goes through
    StandingApprovalMiddleware.after_model (saving approval_snapshots) -> held
    -> interrupt(), and resumes cleanly after approval without an active CLI."""
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.tools import tool
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from ptc_agent.agent.claude_code.agent import claude_code_graph, waits_for_user
    from ptc_agent.agent.claude_code.compaction import NO_RULES
    from ptc_agent.agent.claude_code.launcher import LocalLauncher
    from ptc_agent.agent.claude_code.mid_turn import BeforeTool
    from ptc_agent.agent.claude_code.runtime import TURN_KEY
    from ptc_agent.agent.claude_code.tools_bridge import Deferred, ToolResults
    from src.dalton.coverage.dedup import CompanyDedupRecheckSnapshot
    from src.llms.claude_code import ClaudeTurnFailed
    from src.llms.llm import create_llm
    from src.tools.secretary.approvals import approval_snapshot, preapproved

    # 1. waits_for_user + BeforeTool defer manage_workspaces(action="create"),
    # while leaving non-card actions (list) un-deferred.
    waits = waits_for_user(frozenset())
    assert waits("manage_workspaces", {"action": "create", "name": "NVDA"}) is True
    assert waits("manage_workspaces", {"action": "delete", "workspace_id": "ws-1"}) is True
    assert waits("manage_workspaces", {"action": "list"}) is False
    assert waits("delegate_to_analyst", {"question": "q"}) is True

    results = ToolResults(lambda _msg: False)
    before = BeforeTool(results, waits)
    assert (
        await before(
            {
                "tool_name": "manage_workspaces",
                "tool_input": {"action": "list"},
            },
            "call-cc-list",
            None,
        )
        == {}
    )
    defer_decision = await before(
        {
            "tool_name": "manage_workspaces",
            "tool_input": {"action": "create", "name": "NVDA"},
        },
        "call-cc-1",
        None,
    )
    assert defer_decision.get("hookSpecificOutput", {}).get("permissionDecision") == "defer"
    assert isinstance(await results.due("call-cc-1"), Deferred)

    # 2. Build a real ClaudeCodeGraph with StandingApprovalMiddleware + InMemorySaver.
    class _Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    @tool("manage_workspaces")
    async def manage_workspaces(
        action: str,
        state: Annotated[dict, InjectedState],
        name: str | None = None,
        description: str | None = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> Command:
        """Manage workspaces."""
        assert action == "create"
        return await workspaces_create(
            USER_ID,
            name or "",
            description,
            tool_call_id,
            preapproved=preapproved(state, tool_call_id),
            snapshot=approval_snapshot(state, tool_call_id),
        )

    mw = StandingApprovalMiddleware(USER_ID)
    agent = create_agent(
        _Model(responses=[]),
        tools=[manage_workspaces],
        middleware=[mw],
    ).with_config({"recursion_limit": 50})
    saver = InMemorySaver()
    target = create_llm("claude-haiku-5-5-code", reasoning_effort="low")
    cc_graph = claude_code_graph(
        agent,
        system_prompt="You are Chief of Staff.",
        target=target,
        user_id=USER_ID,
        checkpointer=saver,
        launcher=lambda _w: LocalLauncher(),
        compaction=NO_RULES,
        middleware=[mw],
    )
    assert cc_graph.turn({"configurable": {"thread_id": "cc-t1"}}).spec.waits(
        "manage_workspaces", {"action": "create", "name": "NVDA"}
    )

    # Patch DB + WorkspaceManager for snapshot creation & resumed execution.
    monkeypatch.setattr(
        "src.server.database.user.get_user_preferences",
        create_autospec(
            get_user_preferences,
            return_value={"other_preference": {}},
        ),
    )
    monkeypatch.setattr(
        "src.server.database.computer.get_primary_computer",
        create_autospec(
            get_primary_computer,
            return_value=_computer_row(CLOUD_1, name="Cloud Primary", kind="docker"),
        ),
    )
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        create_autospec(_REAL_GET_LIVE_WORKSPACE_NAMES, return_value=[]),
    )
    monkeypatch.setattr(
        "src.dalton.coverage.dedup.find_existing_company_workspace",
        create_autospec(
            _REAL_FIND_EXISTING_COMPANY_WORKSPACE,
            return_value=None,
        ),
    )
    monkeypatch.setattr(
        "src.dalton.coverage.dedup.recheck_company_workspace_before_create",
        create_autospec(
            _REAL_RECHECK_COMPANY_WORKSPACE,
            return_value=CompanyDedupRecheckSnapshot(status="clear", active=False),
        ),
    )
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.return_value = {
        "workspace_id": "ws-cc-1",
        "name": "NVDA",
        "computer_id": CLOUD_1,
        "status": "stopped",
    }
    monkeypatch.setattr(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr),
    )
    monkeypatch.setattr(
        "src.tools.secretary.tools._folder_ready_result",
        create_autospec(
            _folder_ready_result,
            return_value={"folder_ready": False, "folder_path": None},
        ),
    )

    class _FakeStore:
        def drain(self, through=None):
            return []

    class _FakeRun:
        def __init__(self) -> None:
            self.store = _FakeStore()

        async def next_message(self, _on_compact):
            return None

        async def finish(self) -> None:
            return None

    class _FakeSteering:
        async def resume(self, _cfg):
            return None

        def opened(self, _state, _cfg) -> None:
            return None

        async def read(self, _msgs) -> None:
            return None

    class _FakeTurn:
        def __init__(self, *, phase: str) -> None:
            self.phase = phase
            self.started = False
            self.steering = _FakeSteering()
            self.waits = SimpleNamespace(drain=lambda: [], after=None)
            self._run = _FakeRun()

        @property
        def current(self):
            raise ClaudeTurnFailed("nothing_to_resume", "no CLI running")

        async def hand_off(self, _state, _cfg):
            return None

        async def _repin(self, _state):
            return None

        async def run(self, _state):
            return self._run

        async def again(self, _state, _cfg, _run):
            if self.phase == "propose":
                self.phase = "proposed"
                return [
                    AIMessage(
                        content="",
                        id="ai-cc-1",
                        tool_calls=[
                            {
                                "name": "manage_workspaces",
                                "args": {"action": "create", "name": "NVDA", "description": "GPU"},
                                "id": "call-cc-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            return None

        def commit(self, _records):
            return {}

        def reading(self, _run):
            return {}

        async def ended(self, _state, _run):
            return {"messages": [AIMessage(content="Workspace NVDA created.", id="ai-cc-2")]}

    # Phase 1: model routes deferred create call into pause -> StandingApprovalMiddleware.after_model -> held -> interrupt().
    cfg_1 = {
        "configurable": {
            "thread_id": "cc-create-thread-1",
            TURN_KEY: _FakeTurn(phase="propose"),
        }
    }
    paused = await cc_graph.graph.ainvoke({"messages": []}, cfg_1)
    assert "__interrupt__" in paused
    intr = paused["__interrupt__"][0]
    action_req = intr.value["action_requests"][0]
    assert action_req["type"] == "create_workspace"
    assert action_req["tool_call_id"] == "call-cc-1"
    assert action_req["computer_id"] == CLOUD_1
    assert action_req["computer_name"] == "Cloud Primary"

    saved_state = await cc_graph.graph.aget_state(cfg_1)
    assert saved_state.values["approval_snapshots"]["call-cc-1"]["computer_id"] == CLOUD_1

    # Phase 2: approve on a fresh turn with NO CLI (accessing turn.current would raise nothing_to_resume).
    cfg_2 = {
        "configurable": {
            "thread_id": "cc-create-thread-1",
            TURN_KEY: _FakeTurn(phase="resume"),
        }
    }
    resumed = await cc_graph.graph.ainvoke(
        Command(resume={intr.id: {"decisions": [{"type": "approve"}]}}),
        cfg_2,
    )
    mgr.create_workspace.assert_awaited_once()
    assert mgr.create_workspace.await_args.kwargs["computer_id"] == CLOUD_1
    assert mgr.create_workspace.await_args.kwargs["verify_snapshot"] is True
    tool_msgs = [m for m in resumed["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    body = json.loads(tool_msgs[0].content)
    assert body["success"] is True
    assert body["workspace_id"] == "ws-cc-1"
    assert body["computer_id"] == CLOUD_1


@pytest.mark.asyncio
async def test_daytona_archive_claim_blocks_workspace_create_across_precheck_manager_and_sql_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 14 (Codex med 3): _claim_machine_for_archive stamps config.archiving=True
    under the stopping CAS and _settle_machine_stop drops it, so precheck
    (approvals.py), WorkspaceManager.create_workspace (chosen & primary), and
    create_workspace_on_computer (FENCE_TAKES_WORKSPACES under FOR SHARE) all
    consistently reject workspace creation while archiving."""
    from contextlib import asynccontextmanager

    from src.server.database import workspace as ws_db
    from src.tools.secretary.approvals import build_create_workspace_snapshot
    from src.server.database.sql_fences import FENCE_TAKES_WORKSPACES
    from src.server.services.computer_manager import ComputerManager, _machines

    stopped_daytona = _computer_row(CLOUD_1, name="Daytona Box", kind="daytona", status="stopped")
    archiving_daytona = {
        **stopped_daytona,
        "status": "stopping",
        "config": {"archiving": True},
    }

    # 1. _claim_machine_for_archive delegates to transactional claim_computer_for_archive.
    from src.server.database.computer import claim_computer_for_archive

    claim_mock = create_autospec(
        claim_computer_for_archive,
        return_value=archiving_daytona,
    )
    monkeypatch.setattr(_machines, "claim_computer_for_archive", claim_mock)
    cm = object.__new__(ComputerManager)
    claimed = await cm._claim_machine_for_archive(CLOUD_1)
    assert claimed["config"]["archiving"] is True
    claim_mock.assert_awaited_once_with(
        CLOUD_1,
        expected_workspaces=None,
        expected_workspace_id=None,
    )

    # 2. Precheck (build_create_workspace_snapshot) rejects the archiving computer.
    monkeypatch.setattr(
        "src.server.database.computer.get_primary_computer",
        create_autospec(get_primary_computer, return_value=archiving_daytona),
    )
    snap = await build_create_workspace_snapshot(
        USER_ID,
        {"id": "call-arch-1", "name": "manage_workspaces", "args": {"action": "create", "name": "NVDA"}},
    )
    assert snap is not None
    assert snap["precheck_error_code"] == "computer_archiving"

    # 3. WorkspaceManager.create_workspace rejects both chosen and primary archiving computers.
    ws_mgr = object.__new__(WorkspaceManager)
    monkeypatch.setattr(
        "src.server.services.workspace_manager.get_computer",
        create_autospec(get_computer, return_value=archiving_daytona),
    )
    with pytest.raises(ComputerArchivingError):
        await ws_mgr.create_workspace(user_id=USER_ID, name="NVDA", computer_id=CLOUD_1)
    with pytest.raises(ApprovalScopeChangedError):
        await ws_mgr.create_workspace(
            user_id=USER_ID, name="NVDA", computer_id=CLOUD_1, verify_snapshot=True
        )
    ws_mgr.ensure_primary_computer = create_autospec(
        ws_mgr.ensure_primary_computer,
        return_value=archiving_daytona,
    )
    with pytest.raises(ComputerArchivingError):
        await ws_mgr.create_workspace(user_id=USER_ID, name="NVDA")
    with pytest.raises(ApprovalScopeChangedError):
        await ws_mgr.create_workspace(user_id=USER_ID, name="NVDA", verify_snapshot=True)

    # 4. Transaction-level SQL fence excludes config.archiving even when reject_unavailable=False.
    assert '{"archiving": true}' in FENCE_TAKES_WORKSPACES
    executed_sql: list[str] = []

    class _FenceCur:
        async def execute(self, sql: str, _params: Any = None) -> None:
            executed_sql.append(sql)

        async def fetchone(self) -> Any:
            return None

    class _FenceConn:
        @asynccontextmanager
        async def transaction(self):
            yield self

    @asynccontextmanager
    async def _alloc(_cid: str, *, conn: Any = None):
        yield _FenceConn()

    @asynccontextmanager
    async def _cur(_conn: Any):
        yield _FenceCur()

    monkeypatch.setattr(ws_db, "folder_allocation", _alloc)
    monkeypatch.setattr(ws_db, "_ws_cursor", _cur)
    monkeypatch.setattr(ws_db, "lock_user_writes", create_autospec(_REAL_LOCK_USER_WRITES, return_value=None))
    monkeypatch.setattr(
        ws_db,
        "get_workspace_dir_names_for_computer",
        create_autospec(_REAL_GET_WORKSPACE_DIR_NAMES_FOR_COMPUTER, return_value=set()),
    )
    monkeypatch.setattr(ws_db, "get_computer", create_autospec(get_computer, return_value=archiving_daytona))
    inserted = await ws_db.create_workspace_on_computer(
        USER_ID, "NVDA", CLOUD_1, reject_unavailable=False
    )
    assert inserted is None
    insert_sql = [s for s in executed_sql if "INSERT INTO workspaces" in s]
    assert insert_sql and FENCE_TAKES_WORKSPACES in insert_sql[0]

    # 5. _settle_machine_stop clears config.archiving by default on terminal
    #    settlement and preserves it only when clear_archiving=False (still archiving).
    settle_status_mock = create_autospec(
        update_computer_status,
        return_value=stopped_daytona,
    )
    monkeypatch.setattr(_machines, "update_computer_status", settle_status_mock)
    cm._revoke_computer_tokens = create_autospec(cm._revoke_computer_tokens, return_value=None)
    cm._revoke_livefs = create_autospec(cm._revoke_livefs, return_value=None)
    await cm._settle_machine_stop(CLOUD_1, "stopped", clear_archiving=False)
    settle_status_mock.assert_awaited_once_with(
        CLOUD_1,
        "stopped",
        expected="stopping",
    )
    settle_status_mock.reset_mock()
    await cm._settle_machine_stop(CLOUD_1, "stopped")
    settle_status_mock.assert_awaited_once_with(
        CLOUD_1,
        "stopped",
        expected="stopping",
        drop_config_key="archiving",
    )
