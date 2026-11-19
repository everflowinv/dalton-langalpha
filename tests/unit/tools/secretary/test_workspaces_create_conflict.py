"""What ``workspaces_create`` tells the model when the name is taken."""

from __future__ import annotations

import json
from unittest.mock import create_autospec, patch

import pytest

from src.server.database.workspace_names import WorkspaceNameTaken
from src.server.services.workspace_manager import WorkspaceManager
from src.tools.secretary.tools import hitl_confirm, workspaces_create


async def _error_for(exc: WorkspaceNameTaken) -> str:
    mgr = create_autospec(WorkspaceManager, instance=True)
    mgr.create_workspace.side_effect = exc
    with patch(
        "src.tools.secretary.tools.hitl_confirm",
        create_autospec(hitl_confirm, return_value=(True, {})),
    ), patch(
        "src.server.services.workspace_manager.WorkspaceManager.get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=mgr),
    ):
        result = await workspaces_create("user-1", "Research", None, "call-1")
    return json.loads(result.update["messages"][0].content)["error"]


@pytest.mark.asyncio
async def test_a_known_holder_is_named_for_reuse():
    error = await _error_for(WorkspaceNameTaken("Research", "ws-1"))
    assert "Its workspace_id is ws-1: use that workspace" in error


@pytest.mark.asyncio
async def test_a_holder_gone_before_it_was_named_is_not_offered():
    """The holder lookup can miss a row renamed or deleted after the conflict;
    the model must not be told to use workspace None."""
    error = await _error_for(WorkspaceNameTaken("Research"))
    assert "None" not in error
    assert "Try again" in error
