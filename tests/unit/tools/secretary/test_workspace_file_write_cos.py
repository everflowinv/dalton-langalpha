"""workspace_file_write_cos 的写入落点：返回路径必须是目标工作区分析师能读到的相对路径。

线上线程 80e48793：CoS 把 `DASH/sources/…md` 写进"新建的 DASH 工作区"，返回成功，
分析师在工作区里找不到，文件最终在 `Home/DASH/sources` 下——写入用了 Home 的
work_dir 加上兄弟工作区文件夹名当相对路径。这里的用例锁定三种调用形状的落点。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import create_autospec

import pytest

from ptc_agent.core.sandbox.ptc_sandbox import PTCSandbox
import src.server.database.workspace as ws_db
from src.tools.secretary import tools as secretary_tools
from src.tools.secretary.tools import workspace_file_write_cos

HOME_ROW = {
    "workspace_id": "ws-home",
    "user_id": "u-1",
    "name": "Home",
    "dir_name": "Home",
    "computer_id": "comp-1",
    "status": "running",
    "computer_root_dir": "/root",
}

DASH_ROW = {
    "workspace_id": "ws-dash",
    "user_id": "u-1",
    "name": "DASH",
    "dir_name": "DASH",
    "computer_id": "comp-1",
    "status": "running",
    "computer_root_dir": "/root",
}


def _install(
    monkeypatch: pytest.MonkeyPatch,
    ws_row: dict[str, Any],
    *,
    live_folders: list[dict[str, Any]] | None = None,
    existing_subdirs: list[str] | None = None,
) -> Any:
    """Patch the write path's seams; returns the spec'd sandbox.

    ``_contained_target`` is stubbed with the real fold it performs (work_dir +
    relative path), because the containment machinery itself has its own
    tests; what these cases pin is WHICH relative path reaches it."""
    monkeypatch.setattr(
        secretary_tools,
        "verify_workspace_owner",
        create_autospec(secretary_tools.verify_workspace_owner, return_value=None),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(ws_db.get_workspace, return_value=ws_row),
    )
    default_live = [
        {"workspace_id": "ws-home", "name": "Home", "dir_name": "Home"},
        {"workspace_id": "ws-dash", "name": "DASH", "dir_name": "DASH"},
        {"workspace_id": "ws-meta", "name": "META", "dir_name": "META"},
    ]
    monkeypatch.setattr(
        ws_db,
        "get_live_workspace_folders_for_computer",
        create_autospec(
            ws_db.get_live_workspace_folders_for_computer,
            return_value=default_live if live_folders is None else live_folders,
        ),
    )

    sandbox = create_autospec(PTCSandbox, instance=True)
    sandbox.awrite_file_text.return_value = True
    sandbox.als_directory.return_value = [
        {"name": d, "path": f"/root/{ws_row.get('dir_name')}/{d}", "is_dir": True}
        for d in (existing_subdirs or [])
    ]

    import src.server.app.workspace_files._shared as shared_mod
    import src.server.app.workspace_files.crud as crud_mod

    contained_calls: list[tuple[str, str]] = []

    async def _contained_target(sandbox_: Any, path: str, work_dir: str, previous_dirs=()):
        contained_calls.append((path, work_dir))
        return f"{work_dir.rstrip('/')}/{path.lstrip('/')}"

    monkeypatch.setattr(crud_mod, "_contained_target", _contained_target)

    @asynccontextmanager
    async def _acquire(workspace_id: str, user_id: str):
        yield sandbox, ws_row

    monkeypatch.setattr(shared_mod, "_acquire_sandbox_to_change", _acquire)
    sandbox._contained_calls = contained_calls  # type: ignore[attr-defined]
    return sandbox


def _payload(command: Any) -> dict[str, Any]:
    message = command.update["messages"][0]
    return json.loads(message.content)


@pytest.mark.asyncio
async def test_sibling_folder_prefix_is_refused_not_silently_mislanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The incident's shape: workspace_id names Home and the path starts with
    the sibling workspace's folder name — refused with the fix, so nothing
    lands at Home/DASH/sources."""
    sandbox = _install(monkeypatch, HOME_ROW)

    command = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-home",
        path="DASH/sources/brief.md",
        content="# 简报\n",
        tool_call_id="tc-1",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "DASH" in payload.get("error", "")
    assert "另一个工作区" in payload.get("error", "")
    sandbox.awrite_file_text.assert_not_called()


@pytest.mark.asyncio
async def test_own_folder_prefix_without_subdir_returns_error_not_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """workspace_id names DASH and the caller prefixed the folder name
    ("DASH/sources/brief.md") when DASH has no DASH/ subdirectory: instead of
    silently stripping the prefix, return an explicit error and do not write."""
    sandbox = _install(monkeypatch, DASH_ROW, existing_subdirs=[])

    command = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-dash",
        path="DASH/sources/brief.md",
        content="# 简报\n",
        tool_call_id="tc-2",
    )
    payload = _payload(command)
    assert payload.get("success") is False
    assert "路径应相对该工作区自己的文件夹" in payload.get("error", "")
    assert "去掉开头的 `DASH/`" in payload.get("error", "")
    sandbox.awrite_file_text.assert_not_called()


@pytest.mark.asyncio
async def test_own_folder_prefix_with_existing_subdir_writes_normally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When workspace DASH genuinely has a DASH/ subdirectory, writing
    DASH/readme.md writes into that subdirectory without stripping or failing."""
    sandbox = _install(monkeypatch, DASH_ROW, existing_subdirs=["DASH"])

    command = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-dash",
        path="DASH/readme.md",
        content="# 子目录说明\n",
        tool_call_id="tc-2b",
    )
    payload = _payload(command)
    assert payload["success"] is True
    sandbox.awrite_file_text.assert_called_once_with(
        "/root/DASH/DASH/readme.md", "# 子目录说明\n"
    )
    assert payload["path"] == "DASH/readme.md"


@pytest.mark.asyncio
async def test_home_existing_subdir_matching_live_sibling_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Home already has a legitimate NVDA/ subdirectory; even if NVDA is also a
    live sibling workspace on the same computer, writing NVDA/notes.md into
    Home is allowed."""
    sandbox = _install(
        monkeypatch,
        HOME_ROW,
        live_folders=[
            {"workspace_id": "ws-home", "name": "Home", "dir_name": "Home"},
            {"workspace_id": "ws-nvda", "name": "NVDA", "dir_name": "NVDA"},
        ],
        existing_subdirs=["NVDA"],
    )

    command = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-home",
        path="NVDA/notes.md",
        content="# 笔记\n",
        tool_call_id="tc-home-nvda",
    )
    payload = _payload(command)
    assert payload["success"] is True
    sandbox.awrite_file_text.assert_called_once_with(
        "/root/Home/NVDA/notes.md", "# 笔记\n"
    )


@pytest.mark.asyncio
async def test_deleted_tombstone_workspace_does_not_block_home_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted (tombstone) workspace is excluded from live folders, so Home
    can write NVDA/notes.md even without a pre-existing NVDA/ directory."""
    sandbox = _install(
        monkeypatch,
        HOME_ROW,
        live_folders=[
            {"workspace_id": "ws-home", "name": "Home", "dir_name": "Home"},
            {"workspace_id": "ws-dash", "name": "DASH", "dir_name": "DASH"},
        ],
        existing_subdirs=[],
    )

    command = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-home",
        path="NVDA/notes.md",
        content="# 墓碑工作区不阻断\n",
        tool_call_id="tc-tombstone",
    )
    payload = _payload(command)
    assert payload["success"] is True
    sandbox.awrite_file_text.assert_called_once_with(
        "/root/Home/NVDA/notes.md", "# 墓碑工作区不阻断\n"
    )


@pytest.mark.asyncio
async def test_missing_computer_id_logs_warning_and_allows_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When computer_id is empty, log a warning and allow the write."""
    home_no_comp = {**HOME_ROW, "computer_id": None}
    sandbox = _install(monkeypatch, home_no_comp)

    cmd = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-home",
        path="DASH/notes.md",
        content="# 放行\n",
        tool_call_id="tc-nocomp",
    )
    assert _payload(cmd)["success"] is True
    sandbox.awrite_file_text.assert_called_once_with(
        "/root/Home/DASH/notes.md", "# 放行\n"
    )


@pytest.mark.asyncio
async def test_folder_query_failure_logs_warning_and_allows_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When folder lookup fails, log a warning and allow the write rather than blocking."""
    sandbox = _install(monkeypatch, HOME_ROW)
    ws_db.get_live_workspace_folders_for_computer.side_effect = RuntimeError("db down")

    cmd = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-home",
        path="DASH/notes.md",
        content="# 放行2\n",
        tool_call_id="tc-dberr",
    )
    assert _payload(cmd)["success"] is True
    sandbox.awrite_file_text.assert_called_once_with(
        "/root/Home/DASH/notes.md", "# 放行2\n"
    )


@pytest.mark.asyncio
async def test_plain_relative_path_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain relative path keeps today's behavior: write inside the named
    workspace, return its relative spelling."""
    sandbox = _install(monkeypatch, DASH_ROW)

    command = await workspace_file_write_cos(
        user_id="u-1",
        workspace_id="ws-dash",
        path="sources/brief.md",
        content="# 简报\n",
        tool_call_id="tc-3",
    )
    payload = _payload(command)
    assert payload["success"] is True
    sandbox.awrite_file_text.assert_called_once_with(
        "/root/DASH/sources/brief.md", "# 简报\n"
    )
    assert payload["path"] == "sources/brief.md"
