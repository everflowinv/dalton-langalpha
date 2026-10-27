"""workspace-folder-ready: what a create tells the model about the new folder."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.server.services.workspace_manager import WorkspaceManager
from src.server.database.computer import get_computer
from src.tools.secretary.tools import _FOLDER_CHECK_NOTE, workspaces_create

USER_ID = "user-1"
ROW = {
    "workspace_id": "ws-1",
    "name": "DASH",
    "dir_name": "DASH",
    "computer_id": "c-1",
}


def _manager_double() -> WorkspaceManager:
    manager = create_autospec(WorkspaceManager, instance=True)
    manager.create_workspace.return_value = dict(ROW)
    manager.get_session_for_workspace.return_value = None
    manager.resolve_binding.return_value = SimpleNamespace(
        root_dir="/workspaces", dir_name="DASH"
    )
    return manager


async def _create_result(monkeypatch: pytest.MonkeyPatch, manager: WorkspaceManager):
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=manager),
    )
    command = await workspaces_create(
        USER_ID, "DASH", None, "call-1", preapproved=True
    )
    return json.loads(command.update["messages"][0].content)


@pytest.mark.asyncio
async def test_running_computer_creates_folder_now(monkeypatch: pytest.MonkeyPatch):
    """Machine running: the standing acquire path creates the folder and says so."""
    manager = _manager_double()
    monkeypatch.setattr(
        "src.server.database.computer.get_computer",
        create_autospec(get_computer, return_value={"computer_id": "c-1", "status": "running"}),
    )
    payload = await _create_result(monkeypatch, manager)

    assert payload["success"] is True
    assert payload["folder_ready"] is True
    assert payload["folder_path"] == "/workspaces/DASH"
    assert payload["folder_note"] == _FOLDER_CHECK_NOTE
    manager.get_session_for_workspace.assert_awaited_once_with(
        "ws-1", user_id=USER_ID
    )


@pytest.mark.asyncio
async def test_stopped_computer_reports_folder_not_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    """Machine not running: no acquire, folder_ready False, hand-off pointed at Home."""
    manager = _manager_double()
    monkeypatch.setattr(
        "src.server.database.computer.get_computer",
        create_autospec(get_computer, return_value={"computer_id": "c-1", "status": "stopped"}),
    )
    payload = await _create_result(monkeypatch, manager)

    assert payload["success"] is True
    assert payload["folder_ready"] is False
    assert "计算机启动后才会创建该文件夹" in payload["folder_hint"]
    assert "Home" in payload["folder_hint"]
    manager.get_session_for_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_acquire_failure_degrades_to_not_ready(monkeypatch: pytest.MonkeyPatch):
    """A failed folder setup never fails the create that already succeeded."""
    manager = _manager_double()
    manager.get_session_for_workspace.side_effect = RuntimeError("sandbox gone")
    monkeypatch.setattr(
        "src.server.database.computer.get_computer",
        create_autospec(get_computer, return_value={"computer_id": "c-1", "status": "running"}),
    )
    payload = await _create_result(monkeypatch, manager)

    assert payload["success"] is True
    assert payload["folder_ready"] is False
    assert payload["folder_hint"]


@pytest.mark.asyncio
async def test_status_read_failure_degrades_to_not_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    manager = _manager_double()

    async def broken_get_computer(computer_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.server.database.computer.get_computer", broken_get_computer)
    payload = await _create_result(monkeypatch, manager)

    assert payload["success"] is True
    assert payload["folder_ready"] is False
    manager.get_session_for_workspace.assert_not_awaited()
