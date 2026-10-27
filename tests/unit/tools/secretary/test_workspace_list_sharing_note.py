"""manage_workspaces(action="list") 对同事共享工作区的只读说明。

线上问题：CoS 先告诉用户可以在 rw 共享的同事工作区里提问/派发，用户照做后
delegate_to_analyst 三次被拒。说明必须出现在 CoS 读列表的地方，让它一开始
就按"只能读"承诺。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import create_autospec

import pytest

import src.dalton.settings as dalton_settings
import src.dalton.sharing as dalton_sharing
import src.server.database.workspace as ws_db
from src.tools.secretary.tools import workspaces_list


def _install_list_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    colleague_workspaces: list[dict[str, Any]] | None,
) -> None:
    monkeypatch.setattr(
        dalton_settings,
        "is_dalton_supabase_auth_enabled",
        create_autospec(dalton_settings.is_dalton_supabase_auth_enabled, return_value=True),
    )

    async def _colleagues(user_id: str) -> list[dict[str, Any]]:
        return colleague_workspaces or []

    monkeypatch.setattr(
        dalton_sharing,
        "list_colleague_cos_workspaces",
        create_autospec(
            dalton_sharing.list_colleague_cos_workspaces, side_effect=_colleagues
        ),
    )
    # The tools import these inside the function from their home modules.
    monkeypatch.setattr(
        ws_db, "get_workspaces_for_user", create_autospec(ws_db.get_workspaces_for_user)
    )

    async def _for_user(**kwargs: Any):
        return [], 0

    ws_db.get_workspaces_for_user.side_effect = _for_user

    from src.server.database import home_workspace

    monkeypatch.setattr(
        home_workspace,
        "get_home_workspace_id",
        create_autospec(home_workspace.get_home_workspace_id, return_value="ws-home"),
    )
    monkeypatch.setattr(
        ws_db,
        "get_workspace",
        create_autospec(
            ws_db.get_workspace,
            return_value={"workspace_id": "ws-home", "computer_id": "comp-1"},
        ),
    )


def _payload(command) -> dict[str, Any]:
    return json.loads(command.update["messages"][0].content)


@pytest.mark.asyncio
async def test_colleague_workspaces_come_with_read_only_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有同事共享工作区时，列表带说明：只能 list/read（rw 可写文件），不能派发。"""
    _install_list_seams(
        monkeypatch,
        colleague_workspaces=[
            {"workspace_id": "ws-colleague", "name": "RW 实验仓", "cos_access": "rw"}
        ],
    )
    payload = _payload(await workspaces_list("u-1", "tc-1"))
    assert payload["success"] is True
    note = payload.get("colleague_workspaces_note")
    assert note and "delegate_to_analyst" in note
    assert "不能向同事的工作区派发" in note
    assert "新建工作区" in note


@pytest.mark.asyncio
async def test_no_colleague_workspaces_no_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有同事共享工作区时不加说明——无事可提醒时不产生噪音。"""
    _install_list_seams(monkeypatch, colleague_workspaces=[])
    payload = _payload(await workspaces_list("u-1", "tc-2"))
    assert payload["success"] is True
    assert "colleague_workspaces_note" not in payload
    assert payload.get("colleague_workspaces") == []
