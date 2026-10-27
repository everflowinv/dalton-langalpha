"""workspace-folder-ready: what a create tells the model about the new folder."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from ptc_agent.core.paths import SandboxLayout
from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
from ptc_agent.core.session import Session
from src.server.app.workspace_files._shared import work_dir_for
from src.server.database.home_workspace import get_home_workspace_id
from src.server.database.workspace import get_workspace
from src.server.services.workspace_manager import WorkspaceManager
from src.tools.secretary.tools import _FOLDER_CHECK_NOTE, workspaces_create

USER_ID = "user-1"
ROW = {
    "workspace_id": "ws-1",
    "name": "DASH",
    "dir_name": "DASH",
    "computer_id": "c-1",
}

# The live sandbox's working dir deliberately differs from the binding root:
# the created folder must land where the sandbox's own layout says, which is
# what bring-up's _ensure_workspace_dirs would mkdir.
_SANDBOX_ROOT = "/sandbox-root"
_BINDING_ROOT = "/w"


def _sandbox_double(mkdir_code: int = 0, test_code: int = 0) -> PTCSandbox:
    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.layout.for_workspace.return_value = SandboxLayout(
        _SANDBOX_ROOT
    ).for_workspace("DASH")
    sandbox.execute_bash_command.side_effect = [
        {"exit_code": mkdir_code, "stdout": "", "stderr": ""},
        {"exit_code": test_code, "stdout": "", "stderr": ""},
    ]
    return sandbox


def _manager_double(
    sandbox: PTCSandbox | None,
    *,
    binding_error: Exception | None = None,
) -> WorkspaceManager:
    manager = create_autospec(WorkspaceManager, instance=True)
    manager.create_workspace.return_value = dict(ROW)
    if binding_error is not None:
        manager.resolve_binding.side_effect = binding_error
    else:
        manager.resolve_binding.return_value = SimpleNamespace(
            root_dir=_BINDING_ROOT, dir_name="DASH"
        )
    session = None
    if sandbox is not None:
        session = create_autospec(Session, instance=True)
        session.sandbox = sandbox
    manager._cached_session.return_value = session
    return manager


async def _create_result(
    monkeypatch: pytest.MonkeyPatch,
    manager: WorkspaceManager,
    *,
    home_computer_id: str = "c-1",
):
    monkeypatch.setattr(
        WorkspaceManager,
        "get_instance",
        create_autospec(WorkspaceManager.get_instance, return_value=manager),
    )
    monkeypatch.setattr(
        "src.server.database.home_workspace.get_home_workspace_id",
        create_autospec(get_home_workspace_id, return_value="home-ws"),
    )
    monkeypatch.setattr(
        "src.server.database.workspace.get_workspace",
        create_autospec(
            get_workspace,
            return_value={"workspace_id": "home-ws", "computer_id": home_computer_id},
        ),
    )
    command = await workspaces_create(
        USER_ID, "DASH", None, "call-1", preapproved=True
    )
    return json.loads(command.update["messages"][0].content)


@pytest.mark.asyncio
async def test_same_computer_creates_at_the_sandbox_root(monkeypatch: pytest.MonkeyPatch):
    """Home's machine, live turn session: the folder is created — and its path
    answered — from the sandbox's own layout, not the binding root."""
    sandbox = _sandbox_double(mkdir_code=0, test_code=0)
    manager = _manager_double(sandbox)
    payload = await _create_result(monkeypatch, manager)

    assert payload["success"] is True
    assert payload["folder_ready"] is True
    assert payload["folder_path"] == f"{_SANDBOX_ROOT}/DASH"
    assert payload["folder_note"] == _FOLDER_CHECK_NOTE
    commands = [
        call.kwargs["command"] for call in sandbox.execute_bash_command.await_args_list
    ]
    assert commands == [
        f"mkdir -p {_SANDBOX_ROOT}/DASH",
        f"test -d {_SANDBOX_ROOT}/DASH",
    ]
    # The whole point: no acquire, no locks, no attach.
    manager.get_session_for_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_mkdir_ok_but_verify_fails_reports_not_ready(
    monkeypatch: pytest.MonkeyPatch,
):
    """mkdir exit 0 with a failed `test -d` never reports ready."""
    sandbox = _sandbox_double(mkdir_code=0, test_code=1)
    manager = _manager_double(sandbox)
    payload = await _create_result(monkeypatch, manager)

    assert payload["folder_ready"] is False
    assert payload["folder_hint"]
    assert payload["folder_path"] == f"{_SANDBOX_ROOT}/DASH"
    manager.get_session_for_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_read_failure_never_builds(monkeypatch: pytest.MonkeyPatch):
    """A failed binding read answers a display-only default-derived path and
    never runs mkdir, even with a live session on this worker."""
    sandbox = _sandbox_double()
    manager = _manager_double(sandbox, binding_error=RuntimeError("db down"))
    monkeypatch.setattr(
        "src.server.app.workspace_files._shared.work_dir_for",
        create_autospec(work_dir_for, return_value="/default-root/DASH"),
    )
    payload = await _create_result(monkeypatch, manager)

    assert payload["folder_ready"] is False
    assert payload["folder_hint"]
    assert payload["folder_path"] == "/default-root/DASH"
    sandbox.execute_bash_command.assert_not_awaited()
    manager.get_session_for_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_different_computer_reports_not_ready_with_path(
    monkeypatch: pytest.MonkeyPatch,
):
    """The new workspace lands on another machine: path stated, never created."""
    sandbox = _sandbox_double()
    manager = _manager_double(sandbox)
    payload = await _create_result(monkeypatch, manager, home_computer_id="c-other")

    assert payload["folder_ready"] is False
    assert payload["folder_hint"]
    assert payload["folder_path"] == f"{_BINDING_ROOT}/DASH"
    sandbox.execute_bash_command.assert_not_awaited()
    manager.get_session_for_workspace.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_live_session_reports_not_ready_with_path(
    monkeypatch: pytest.MonkeyPatch,
):
    """Machine without this worker's turn session (stopped): path stated, not created."""
    manager = _manager_double(sandbox=None)
    payload = await _create_result(monkeypatch, manager)

    assert payload["folder_ready"] is False
    assert payload["folder_hint"]
    assert payload["folder_path"] == f"{_BINDING_ROOT}/DASH"
    manager.get_session_for_workspace.assert_not_awaited()
