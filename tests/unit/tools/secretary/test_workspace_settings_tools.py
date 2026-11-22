"""Tests for Chief of Staff workspace settings actions: update, create(computer_id), archive, and prompt copy."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

from src.server.database.computer import get_computer
from src.server.database.workspace import (
    get_live_workspace_folders_for_computer,
    get_workspace,
    update_workspace,
)
from src.server.database.workspace_folders import WorkspaceFolderMoving
from src.server.database.workspace_names import (
    HOME_STAND_IN_FOLDER,
    WorkspaceNameInvalid,
    WorkspaceNameTaken,
)
from src.server.services.shared_workspaces.access import (
    Access,
    NoAccess,
    SharedAccess,
    manager_only,
    workspace_access,
)
from src.server.services.shared_workspaces.feed import publish_workspace_changed
from src.server.services.workspace_manager import WorkspaceManager
from src.server.database.shared_workspaces import SharedRow
from src.tools.secretary._commands import puts_card
from src.tools.secretary.chief_of_staff import chief_of_staff_workspaces
from src.tools.secretary.tools import hitl_confirm

USER_ID = "user-1"
OTHER_USER_ID = "user-2"
HOME_ID = "33333333-3333-3333-3333-333333333333"
WS_ID = "44444444-4444-4444-4444-444444444444"
COMPUTER_ID = "55555555-5555-5555-5555-555555555555"


def _tool_call(**args) -> dict:
    return {
        "name": "manage_workspaces",
        "args": {**args, "state": {}},
        "id": "call_1",
        "type": "tool_call",
    }


def _cfg() -> dict:
    return {"configurable": {"user_id": USER_ID, "workspace_id": HOME_ID}}


def _payload(command) -> dict:
    return json.loads(command.update["messages"][0].content)


# --- manage_workspaces(action="update") --------------------------------------


@pytest.mark.asyncio
async def test_workspace_update_own_workspace_renames_and_updates_description():
    row = {
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "name": "Old Name",
        "description": "Old desc",
        "status": "stopped",
        "dir_name": "Old Name",
    }
    updated_row = {
        **row,
        "name": "New Name",
        "description": "Updated desc",
    }
    access_mock = create_autospec(workspace_access, return_value=Access(USER_ID))
    update_mock = create_autospec(update_workspace, return_value=updated_row)
    publish_mock = create_autospec(publish_workspace_changed, return_value=None)

    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access", access_mock
    ), patch(
        "src.server.database.workspace.update_workspace", update_mock
    ), patch(
        "src.server.services.shared_workspaces.feed.publish_workspace_changed",
        publish_mock,
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(
                action="update",
                workspace_id=WS_ID,
                name="New Name",
                description="Updated desc",
            ),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is True
    assert payload["workspace_id"] == WS_ID
    assert payload["name"] == "New Name"
    assert payload["description"] == "Updated desc"
    access_mock.assert_awaited_once_with(row, USER_ID, manage=True)
    update_mock.assert_awaited_once_with(
        workspace_id=WS_ID, name="New Name", description="Updated desc"
    )
    publish_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_update_shared_workspace_as_manager_publishes_change():
    row = {
        "workspace_id": WS_ID,
        "user_id": "holder-org-1",
        "name": "Team Semis",
        "description": "Shared",
        "status": "running",
        "dir_name": "Team Semis",
    }
    shared_row = SharedRow(
        workspace_id=WS_ID,
        org_id="org-1",
        holder_id="holder-org-1",
        everyone=False,
        created_by="owner-1",
        share_out="off",
        team_id=None,
    )
    shared_access = Access(
        "holder-org-1",
        SharedAccess(row=shared_row, caller=USER_ID, role="manager"),
    )
    access_mock = create_autospec(workspace_access, return_value=shared_access)
    update_mock = create_autospec(
        update_workspace, return_value={**row, "name": "Team Chips"}
    )
    publish_mock = create_autospec(publish_workspace_changed, return_value=None)

    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access", access_mock
    ), patch(
        "src.server.database.workspace.update_workspace", update_mock
    ), patch(
        "src.server.services.shared_workspaces.feed.publish_workspace_changed",
        publish_mock,
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="update", workspace_id=WS_ID, name="Team Chips"),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is True
    assert payload["name"] == "Team Chips"
    publish_mock.assert_awaited_once_with("org-1", WS_ID, "updated")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        NoAccess(status_code=403, detail="Forbidden"),
        manager_only(),
    ],
    ids=["foreign_workspace_403", "shared_member_not_manager_403"],
)
async def test_workspace_update_refuses_without_manage_permission(exc):
    row = {
        "workspace_id": WS_ID,
        "user_id": OTHER_USER_ID,
        "name": "Colleague WS",
        "status": "running",
    }
    update_mock = create_autospec(update_workspace)
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, side_effect=exc),
    ), patch("src.server.database.workspace.update_workspace", update_mock):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="update", workspace_id=WS_ID, name="Hijacked"),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "home_row",
    [
        {"workspace_id": HOME_ID, "user_id": USER_ID, "name": "Home", "status": "unbound"},
        {
            "workspace_id": "stand-in-1",
            "user_id": USER_ID,
            "name": "Home",
            "status": "running",
            "config": {"home_stand_in": True},
        },
    ],
    ids=["home", "stand-in"],
)
async def test_workspace_update_refuses_renaming_home(home_row):
    update_mock = create_autospec(update_workspace)
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=home_row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, return_value=Access(USER_ID)),
    ), patch("src.server.database.workspace.update_workspace", update_mock):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(
                action="update",
                workspace_id=home_row["workspace_id"],
                name="My HQ",
            ),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    assert "Home can't be renamed" in payload["error"]
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_workspace_update_allows_home_description_update():
    home_row = {
        "workspace_id": HOME_ID,
        "user_id": USER_ID,
        "name": "Home",
        "description": "Old",
        "status": "unbound",
    }
    update_mock = create_autospec(
        update_workspace, return_value={**home_row, "description": "CoS desk"}
    )
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=home_row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, return_value=Access(USER_ID)),
    ), patch("src.server.database.workspace.update_workspace", update_mock):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="update", workspace_id=HOME_ID, description="CoS desk"),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is True
    assert payload["description"] == "CoS desk"
    update_mock.assert_awaited_once_with(
        workspace_id=HOME_ID, name=None, description="CoS desk"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (WorkspaceNameTaken("NVDA", "ws-existing"), "ws-existing"),
        (WorkspaceNameInvalid('"Home" is reserved; choose another name.', reason="reserved"), "reserved"),
        (WorkspaceFolderMoving(WS_ID), "folder is being moved"),
    ],
)
async def test_workspace_update_surfaces_name_and_folder_validation_errors(exc, expected):
    row = {"workspace_id": WS_ID, "user_id": USER_ID, "name": "Chips", "status": "running"}
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, return_value=Access(USER_ID)),
    ), patch(
        "src.server.database.workspace.update_workspace",
        create_autospec(update_workspace, side_effect=exc),
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="update", workspace_id=WS_ID, name="NVDA"),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    assert expected in payload["error"]


# --- manage_workspaces(action="create", computer_id=...) ---------------------


@pytest.mark.asyncio
async def test_workspace_create_with_computer_id_shows_computer_name_in_confirm_and_creates():
    computer_row = {
        "computer_id": COMPUTER_ID,
        "user_id": USER_ID,
        "name": "Mac Studio",
        "status": "running",
        "config": {},
    }
    manager = create_autospec(WorkspaceManager, instance=True)
    manager.create_workspace.return_value = {
        "workspace_id": WS_ID,
        "name": "Lithium",
        "dir_name": "Lithium",
        "computer_id": COMPUTER_ID,
    }
    manager.resolve_binding.return_value = None
    confirm = create_autospec(hitl_confirm, return_value=(True, {}))

    with patch(
        "src.server.services.workspace_manager.get_computer",
        create_autospec(get_computer, return_value=computer_row),
    ), patch(
        "src.tools.secretary.tools.duplicate_workspace",
        return_value=(None, True),
    ), patch(
        "src.dalton.coverage.dedup.find_existing_company_workspace",
        return_value=None,
    ), patch(
        "src.tools.secretary.tools.hitl_confirm", confirm
    ), patch(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=manager),
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(
                action="create",
                name="Lithium",
                description="Battery metals",
                computer_id=COMPUTER_ID,
            ),
            config=_cfg(),
        )

    confirm.assert_called_once()
    card_type, card_payload = confirm.call_args.args
    assert card_type == "create_workspace"
    assert card_payload["computer_id"] == COMPUTER_ID
    assert card_payload["computer_name"] == "Mac Studio"
    manager.create_workspace.assert_awaited_once_with(
        user_id=USER_ID,
        name="Lithium",
        description="Battery metals",
        computer_id=COMPUTER_ID,
    )
    payload = _payload(result)
    assert payload["success"] is True
    assert payload["computer_id"] == COMPUTER_ID
    assert payload["computer_name"] == "Mac Studio"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "computer_row",
    [
        None,
        {"computer_id": COMPUTER_ID, "user_id": OTHER_USER_ID, "name": "Other Box", "config": {}},
        {"computer_id": COMPUTER_ID, "user_id": USER_ID, "name": "Retiring", "config": {"retire_when_empty": True}},
    ],
    ids=["missing", "foreign_owner", "retiring"],
)
async def test_workspace_create_with_invalid_or_foreign_computer_refused_before_confirm(
    computer_row,
):
    confirm = create_autospec(
        hitl_confirm, side_effect=AssertionError("the user must not be asked")
    )
    with patch(
        "src.server.services.workspace_manager.get_computer",
        create_autospec(get_computer, return_value=computer_row),
    ), patch("src.tools.secretary.tools.hitl_confirm", confirm):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="create", name="Lithium", computer_id=COMPUTER_ID),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    assert "Computer not found" in payload["error"]


@pytest.mark.asyncio
async def test_workspace_create_omitting_computer_id_keeps_default_behavior():
    manager = create_autospec(WorkspaceManager, instance=True)
    manager.create_workspace.return_value = {
        "workspace_id": WS_ID,
        "name": "Semis",
        "dir_name": "Semis",
    }
    manager.resolve_binding.return_value = None
    confirm = create_autospec(hitl_confirm, return_value=(True, {}))
    get_comp = create_autospec(
        get_computer, side_effect=AssertionError("get_computer must not be called")
    )

    with patch(
        "src.server.services.workspace_manager.get_computer", get_comp
    ), patch(
        "src.tools.secretary.tools.duplicate_workspace",
        return_value=(None, True),
    ), patch(
        "src.dalton.coverage.dedup.find_existing_company_workspace",
        return_value=None,
    ), patch(
        "src.tools.secretary.tools.hitl_confirm", confirm
    ), patch(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=manager),
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="create", name="Semis"),
            config=_cfg(),
        )

    _, card_payload = confirm.call_args.args
    assert "computer_id" not in card_payload
    assert "computer_name" not in card_payload
    manager.create_workspace.assert_awaited_once_with(
        user_id=USER_ID, name="Semis", description=None
    )
    assert _payload(result)["success"] is True


# --- manage_workspaces(action="archive") -------------------------------------


@pytest.mark.asyncio
async def test_workspace_archive_lists_affected_workspaces_and_archives_on_approval():
    row = {
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "name": "Lithium",
        "computer_id": COMPUTER_ID,
        "computer_name": "Cloud Box",
        "status": "stopped",
    }
    siblings = [
        {"workspace_id": HOME_ID, "user_id": USER_ID, "name": "Home", "dir_name": "Home"},
        {"workspace_id": WS_ID, "user_id": USER_ID, "name": "Lithium", "dir_name": "Lithium"},
        {"workspace_id": "ws-2", "user_id": USER_ID, "name": "Copper", "dir_name": "Copper"},
        {
            "workspace_id": "stand-in",
            "user_id": USER_ID,
            "name": "Home",
            "dir_name": HOME_STAND_IN_FOLDER,
        },
    ]
    confirm = create_autospec(hitl_confirm, return_value=(True, {}))
    manager = create_autospec(WorkspaceManager, instance=True)
    manager.archive_workspace.return_value = {"workspace_id": WS_ID, "status": "stopped"}

    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, return_value=Access(USER_ID)),
    ), patch(
        "src.server.database.workspace.get_live_workspace_folders_for_computer",
        create_autospec(get_live_workspace_folders_for_computer, return_value=siblings),
    ), patch(
        "src.tools.secretary.tools.hitl_confirm", confirm
    ), patch(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=manager),
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="archive", workspace_id=WS_ID),
            config=_cfg(),
        )

    assert puts_card("manage_workspaces", {"action": "archive"}) is True
    confirm.assert_called_once()
    card_type, card_payload = confirm.call_args.args
    assert card_type == "archive_workspace"
    assert card_payload["workspace_id"] == WS_ID
    assert card_payload["workspace_name"] == "Lithium"
    assert card_payload["affected_workspace_names"] == ["Home", "Lithium", "Copper"]
    assert [w["workspace_id"] for w in card_payload["affected_workspaces"]] == [
        HOME_ID,
        WS_ID,
        "ws-2",
    ]
    manager.archive_workspace.assert_awaited_once_with(WS_ID)
    payload = _payload(result)
    assert payload["success"] is True
    assert payload["status"] == "stopped"


@pytest.mark.asyncio
async def test_workspace_archive_declined_does_not_call_manager():
    row = {
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "name": "Lithium",
        "computer_id": COMPUTER_ID,
        "status": "stopped",
    }
    manager = create_autospec(WorkspaceManager, instance=True)
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, return_value=Access(USER_ID)),
    ), patch(
        "src.server.database.workspace.get_live_workspace_folders_for_computer",
        create_autospec(get_live_workspace_folders_for_computer, return_value=[row]),
    ), patch(
        "src.tools.secretary.tools.hitl_confirm",
        create_autospec(hitl_confirm, return_value=(False, {})),
    ), patch(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=manager),
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="archive", workspace_id=WS_ID),
            config=_cfg(),
        )

    assert "declined" in result.update["messages"][0].content.lower()
    manager.archive_workspace.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "home_row",
    [
        {"workspace_id": HOME_ID, "user_id": USER_ID, "name": "Home", "status": "unbound"},
        {
            "workspace_id": "stand-in-1",
            "user_id": USER_ID,
            "name": "Home",
            "status": "stopped",
            "config": {"home_stand_in": True},
        },
    ],
    ids=["home", "stand-in"],
)
async def test_workspace_archive_refuses_home_before_confirm(home_row):
    confirm = create_autospec(
        hitl_confirm, side_effect=AssertionError("the user must not be asked")
    )
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=home_row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, return_value=Access(USER_ID)),
    ), patch("src.tools.secretary.tools.hitl_confirm", confirm):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="archive", workspace_id=home_row["workspace_id"]),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    assert "Home cannot be archived" in payload["error"]


@pytest.mark.asyncio
async def test_workspace_archive_refuses_without_manage_permission_before_confirm():
    confirm = create_autospec(
        hitl_confirm, side_effect=AssertionError("the user must not be asked")
    )
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(
            get_workspace,
            return_value={"workspace_id": WS_ID, "user_id": OTHER_USER_ID, "name": "Other"},
        ),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(
            workspace_access, side_effect=NoAccess(status_code=403, detail="Forbidden")
        ),
    ), patch("src.tools.secretary.tools.hitl_confirm", confirm):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="archive", workspace_id=WS_ID),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    assert "Forbidden" in payload["error"]


@pytest.mark.asyncio
async def test_workspace_archive_surfaces_running_computer_refusal():
    row = {
        "workspace_id": WS_ID,
        "user_id": USER_ID,
        "name": "Lithium",
        "computer_id": COMPUTER_ID,
        "status": "running",
    }
    manager = create_autospec(WorkspaceManager, instance=True)
    manager.archive_workspace.side_effect = RuntimeError(
        "Cannot archive computer in 'running' state. Only stopped computers can be archived."
    )
    with patch(
        "src.server.database.workspace.get_workspace",
        create_autospec(get_workspace, return_value=row),
    ), patch(
        "src.server.services.shared_workspaces.access.workspace_access",
        create_autospec(workspace_access, return_value=Access(USER_ID)),
    ), patch(
        "src.server.database.workspace.get_live_workspace_folders_for_computer",
        create_autospec(get_live_workspace_folders_for_computer, return_value=[row]),
    ), patch(
        "src.tools.secretary.tools.hitl_confirm",
        create_autospec(hitl_confirm, return_value=(True, {})),
    ), patch(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=manager),
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="archive", workspace_id=WS_ID),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    assert "Only stopped computers can be archived" in payload["error"]


# --- Docstring & Prompt checks -----------------------------------------------


def test_manage_workspaces_docstring_and_cos_prompt_cover_new_actions():
    desc = chief_of_staff_workspaces.description or ""
    assert "DALTON-HOOK" not in desc
    assert '"update"' in desc
    assert '"archive"' in desc
    assert "computer_id" in desc
    assert "folder name" in desc.lower()

    prompt_path = (
        Path(__file__).resolve().parents[4]
        / "src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2"
    )
    text = prompt_path.read_text(encoding="utf-8")
    section = text.split("## Choosing the workspace", 1)[1].split("## After a hand-off", 1)[0]
    assert 'manage_workspaces(action="update"' in section
    assert "computer_id" in section
    assert 'manage_workspaces(action="archive"' in section
    assert 'manage_workspaces(action="delete"' in section
    assert "explicitly asks" in section
    assert "confirmation" in section
