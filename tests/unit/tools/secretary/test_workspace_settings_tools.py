"""Tests for Chief of Staff manage_workspaces(action="update") and prompt/docstring copy."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest

from src.server.database.shared_workspaces import SharedRow
from src.server.database.workspace import get_workspace, update_workspace
from src.server.database.workspace_folders import WorkspaceFolderMoving
from src.server.database.workspace_names import (
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
from src.tools.secretary.chief_of_staff import chief_of_staff_workspaces

USER_ID = "user-1"
OTHER_USER_ID = "user-2"
HOME_ID = "33333333-3333-3333-3333-333333333333"
WS_ID = "44444444-4444-4444-4444-444444444444"


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
    ("kwargs", "expected_field"),
    [
        ({"description": "x" * 1001}, "description"),
        ({"name": ""}, "name"),
        ({"name": "a" * 81}, "name"),
    ],
    ids=["description_over_1000", "empty_name", "name_over_80"],
)
async def test_workspace_update_rejects_invalid_fields_via_workspace_update_model(
    kwargs, expected_field
):
    get_mock = create_autospec(
        get_workspace, side_effect=AssertionError("DB must not be queried on schema error")
    )
    update_mock = create_autospec(update_workspace)
    with patch("src.server.database.workspace.get_workspace", get_mock), patch(
        "src.server.database.workspace.update_workspace", update_mock
    ):
        result = await chief_of_staff_workspaces.ainvoke(
            _tool_call(action="update", workspace_id=WS_ID, **kwargs),
            config=_cfg(),
        )

    payload = _payload(result)
    assert payload["success"] is False
    assert expected_field in payload["error"]
    update_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (WorkspaceNameTaken("NVDA", "ws-existing"), "ws-existing"),
        (
            WorkspaceNameInvalid('"Home" is reserved; choose another name.', reason="reserved"),
            "reserved",
        ),
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


def test_manage_workspaces_docstring_and_cos_prompt_cover_update_and_delete():
    desc = chief_of_staff_workspaces.description or ""
    assert "DALTON-HOOK" not in desc
    assert '"update"' in desc
    # DALTON-HOOK(cos-settings-tools): Batch 3 enables manage_workspaces(action="archive")
    assert '"archive"' in desc
    assert "computer_id" not in desc
    assert "folder name" in desc.lower()
    returns_block = desc.split("Returns:", 1)[1].strip()
    assert len(returns_block.splitlines()) == 1

    prompt_path = (
        Path(__file__).resolve().parents[4]
        / "src/ptc_agent/agent/prompts/templates/components/chief_of_staff.md.j2"
    )
    text = prompt_path.read_text(encoding="utf-8")
    section = text.split("## Choosing the workspace", 1)[1].split("## After a hand-off", 1)[0]
    assert 'manage_workspaces(action="update"' in section
    assert 'manage_workspaces(action="archive"' in section
    assert 'manage_workspaces(action="delete"' in section
    assert "computer_id" not in section
    assert "explicitly asks" in section
    assert "confirmation" in section
