"""The duplicate guard on the agent's workspace create.

The decision "this create must ask" is frozen when the model proposes the
call: ``StandingApprovalMiddleware`` withholds the standing approval for a
name the user already holds a live workspace under — the same name ignoring
case and outer whitespace, or the same listing once the symbol spelling is
normalized (``700.HK`` and ``0700.HK``) — and when the read that would tell
cannot run. Freezing it there is what makes a resume honest: the tool re-runs
from the top, so a duplicate deleted while its card was pending must not hand
the create back to the standing approval the user's answer already replaced.
"""

from __future__ import annotations

import json
from contextlib import ExitStack, asynccontextmanager, contextmanager
from typing import Annotated, Any
from unittest.mock import create_autospec, patch

import psycopg
import pytest
from langchain.agents import create_agent
from langchain.tools import InjectedState
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

try:
    from langchain.tools import InjectedToolCallId
except ImportError:
    from langchain_core.tools import InjectedToolCallId

from src.server.database.sql_fences import FENCE_LIVE_WORKSPACE
from src.server.database.computer import get_computer as _REAL_GET_COMPUTER
from src.server.database.user import get_user_preferences as _REAL_PREFS
from src.server.database.workspace import (
    get_live_workspace_names as _REAL_LIVE_NAMES,
)
from src.server.services.workspace_manager import WorkspaceManager
from src.tools.secretary._commands import hitl_confirm as _REAL_CONFIRM
from src.tools.secretary.approvals import (
    AUTO_APPROVE_WORKSPACE_CREATION,
    StandingApprovalMiddleware,
    is_duplicate_name,
    preapproved,
)
from src.tools.secretary.tools import workspaces_create

USER = "user-1"
EXISTING_ID = "11111111-1111-1111-1111-111111111111"


def _names_double(*rows: dict) -> Any:
    """The fenced name read, returning ``rows`` as the user's live workspaces."""
    return create_autospec(_REAL_LIVE_NAMES, return_value=list(rows))


def _names_failing() -> Any:
    return create_autospec(_REAL_LIVE_NAMES, side_effect=RuntimeError("pool closed"))


def _manager() -> Any:
    """A ``WorkspaceManager`` whose create returns a fresh workspace.

    The autospec'd async method is configured in place, not replaced, so a
    signature change on ``create_workspace`` still fails these tests.
    """
    from src.server.services.workspace_manager import WorkspaceManager as _WM

    mgr = create_autospec(_WM, instance=True)
    mgr.create_workspace.return_value = {"workspace_id": "ws-new", "name": "New"}
    return mgr


def _payload(result: Any) -> dict:
    return json.loads(result.update["messages"][0].content)


def _asked(confirm: Any) -> dict:
    """The action-request payload of the card the create asked on."""
    assert confirm.call_count == 1
    return confirm.call_args[0][1]


# --- Which names count as one workspace ---------------------------------------


@pytest.mark.parametrize(
    ("new", "existing", "dup"),
    [
        (" research ", "Research", True),   # plain rule: strip + casefold
        ("META", "meta", True),
        ("计划①", "计划① ", True),
        ("700.HK", "0700.HK", True),        # ticker rule: one listing
        ("600519.ss", "600519.SH", True),
        ("计划①", "计划1", False),          # not tickers: no symbol fold
        ("Research Notes", "research notes", True),
        ("Research", "Research Notes", False),
        ("META", "Meta Platforms", False),
    ],
)
def test_what_counts_as_a_duplicate_name(new, existing, dup):
    assert is_duplicate_name(new, existing) is dup


# --- The standing approval is withheld at proposal time ------------------------


async def _recorded(tool_calls: list[dict], user_id: str = USER) -> dict | None:
    state = {"messages": [AIMessage(content="", tool_calls=tool_calls)]}
    return await StandingApprovalMiddleware(user_id).aafter_model(state, None)


def _create_call(call_id: str, name: str) -> dict:
    return {"name": "manage_workspaces", "args": {"action": "create", "name": name}, "id": call_id}


@pytest.fixture
def workspace_creation_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.server.database.user.get_user_preferences",
        create_autospec(
            _REAL_PREFS,
            return_value={"other_preference": {AUTO_APPROVE_WORKSPACE_CREATION: True}},
        ),
    )


@pytest.mark.asyncio
async def test_a_duplicate_withholds_the_standing_approval(
    monkeypatch, workspace_creation_granted
):
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        _names_double({"workspace_id": EXISTING_ID, "name": "0700.HK"}),
    )
    update = await _recorded([_create_call("c1", "700.HK")])
    assert update == {"preapproved_calls": []}


@pytest.mark.asyncio
async def test_a_read_that_cannot_run_withholds_it_too(
    monkeypatch, workspace_creation_granted
):
    """As with a setting that cannot be read: ask, never auto-create."""
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names", _names_failing()
    )
    update = await _recorded([_create_call("c1", "Research")])
    assert update == {"preapproved_calls": []}


@pytest.mark.asyncio
async def test_a_clean_create_keeps_its_standing_approval(
    monkeypatch, workspace_creation_granted
):
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        _names_double({"workspace_id": EXISTING_ID, "name": "0700.HK"}),
    )
    update = await _recorded([_create_call("c1", "META")])
    assert update == {"preapproved_calls": ["c1"]}


@pytest.mark.asyncio
async def test_a_plain_name_folding_is_not_a_duplicate(
    monkeypatch, workspace_creation_granted
):
    """``计划①`` and ``计划1`` are different names: the symbol fold never
    applies to a name that does not read as a ticker."""
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        _names_double({"workspace_id": EXISTING_ID, "name": "计划1"}),
    )
    update = await _recorded([_create_call("c1", "计划①")])
    assert update == {"preapproved_calls": ["c1"]}


# --- The tool asks on the frozen record, and the card says why ----------------


@contextmanager
def _patched(confirm: Any, names: Any, mgr: Any):
    """The card, the duplicate read and the manager, patched for one create."""
    with ExitStack() as stack:
        for p in (
            patch("src.tools.secretary.tools.hitl_confirm", confirm),
            patch("src.server.database.workspace.get_live_workspace_names", names),
            patch(
                "src.server.services.workspace_manager.WorkspaceManager.get_instance",
                create_autospec(WorkspaceManager.get_instance, return_value=mgr),
            ),
        ):
            stack.enter_context(p)
        yield


@pytest.mark.asyncio
async def test_an_ask_for_a_duplicate_names_the_workspace_to_reuse():
    confirm = create_autospec(_REAL_CONFIRM, return_value=(False, {}))
    mgr = _manager()
    with _patched(confirm, _names_double({"workspace_id": EXISTING_ID, "name": "0700.HK"}), mgr):
        result = await workspaces_create(USER, "700.HK", None, "call-1")

    mgr.create_workspace.assert_not_awaited()
    payload = _asked(confirm)
    assert payload["duplicate_workspace_name"] == "0700.HK"
    assert payload["duplicate_workspace_id"] == EXISTING_ID
    said = result.update["messages"][0].content
    assert '"0700.HK"' in said and EXISTING_ID in said
    assert "use that workspace instead" in said


@pytest.mark.asyncio
async def test_an_ask_whose_check_cannot_run_says_so_on_the_card():
    confirm = create_autospec(_REAL_CONFIRM, return_value=(False, {}))
    mgr = _manager()
    with _patched(confirm, _names_failing(), mgr):
        result = await workspaces_create(USER, "Research", None, "call-1")

    mgr.create_workspace.assert_not_awaited()
    assert _asked(confirm)["duplicate_check_unavailable"] is True
    # A declined card declines; the plain message, since no workspace is named.
    assert result.update["messages"][0].content.startswith("User declined workspace creation.")


@pytest.mark.asyncio
async def test_a_clean_ask_carries_no_duplicate_fields():
    confirm = create_autospec(_REAL_CONFIRM, return_value=(True, {}))
    mgr = _manager()
    with _patched(confirm, _names_double(), mgr):
        await workspaces_create(USER, "META", None, "call-1")

    payload = _asked(confirm)
    assert "duplicate_workspace_name" not in payload
    assert "duplicate_check_unavailable" not in payload


@pytest.mark.asyncio
async def test_a_duplicate_the_user_approves_on_the_card_still_creates():
    confirm = create_autospec(_REAL_CONFIRM, return_value=(True, {}))
    mgr = _manager()
    with _patched(
        confirm, _names_double({"workspace_id": EXISTING_ID, "name": "META"}), mgr
    ):
        result = await workspaces_create(USER, "meta", None, "call-1")

    mgr.create_workspace.assert_awaited_once()
    payload = _payload(result)
    assert payload["success"] is True
    # Card-approved, not standing-approved: the result is not marked as such.
    assert "preapproved" not in payload


@pytest.mark.asyncio
async def test_a_frozen_ask_is_honored_even_when_the_duplicate_is_gone():
    """The record frozen at proposal time says ask; on the re-run after the
    user's answer the duplicate may already be deleted — the answer, not a
    fresh duplicate read, decides."""
    confirm = create_autospec(_REAL_CONFIRM, return_value=(False, {}))
    mgr = _manager()
    with _patched(confirm, _names_double(), mgr):
        result = await workspaces_create(USER, "700.HK", None, "call-1")

    mgr.create_workspace.assert_not_awaited()
    assert result.update["messages"][0].content.startswith(
        "User declined workspace creation."
    )


@pytest.mark.asyncio
async def test_a_standing_approval_creates_without_reading_names():
    """The duplicate read belongs to the ask; a create the middleware cleared
    never pays it."""
    confirm = create_autospec(
        _REAL_CONFIRM, side_effect=AssertionError("must not ask")
    )
    names = create_autospec(_REAL_LIVE_NAMES, side_effect=AssertionError("must not read"))
    mgr = _manager()
    with _patched(confirm, names, mgr), patch(
        "src.server.database.computer.get_computer",
        create_autospec(_REAL_GET_COMPUTER, side_effect=RuntimeError("no db")),
    ):
        result = await workspaces_create(USER, "META", None, "call-1", preapproved=True)

    mgr.create_workspace.assert_awaited_once()
    # folder_ready/folder_hint/folder_note ride every create result now
    # (workspace-folder-ready); against this test's machine double the folder
    # path is never ready, which is the degraded shape.
    assert _payload(result) == {
        "success": True,
        "workspace_id": "ws-new",
        "workspace_name": "New",
        "preapproved": True,
        "workspace_description": "",
        "folder_ready": False,
        "folder_hint": (
            "计算机启动后才会创建该文件夹；需要交接文件时，先放在 Home 下，并把路径写进派发说明"
        ),
        "folder_note": "复制或清理前先确认 folder_path 已存在且非空",
    }


# --- The resume end to end -----------------------------------------------------


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _create_agent(created: list[str]):
    """An agent whose manage_workspaces stand-in branches exactly as the real
    tool does: on the preapproval frozen into the thread state, else the card."""

    @tool("manage_workspaces")
    async def manage_workspaces(
        action: str,
        state: Annotated[dict, InjectedState],
        name: str | None = None,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Create a workspace, asking first unless approved in advance."""
        if action != "create":
            return "listed"
        if not preapproved(state, tool_call_id):
            approved, _ = _REAL_CONFIRM(
                "create_workspace", {"workspace_name": name or ""}
            )
            if not approved:
                return f"declined {name}"
        created.append(name or "?")
        return f"created {name}"

    model = _Model(responses=[
        AIMessage(content="", tool_calls=[{
            "name": "manage_workspaces",
            "args": {"action": "create", "name": "700.HK"},
            "id": "call-1",
        }]),
        AIMessage(content="done"),
    ])
    return create_agent(
        model,
        tools=[manage_workspaces],
        middleware=[StandingApprovalMiddleware(USER)],
        checkpointer=InMemorySaver(),
    )


@pytest.mark.asyncio
async def test_a_duplicate_deleted_while_its_card_waited_still_takes_the_users_no(
    monkeypatch, workspace_creation_granted
):
    rows = [{"workspace_id": EXISTING_ID, "name": "0700.HK"}]
    monkeypatch.setattr(
        "src.server.database.workspace.get_live_workspace_names",
        create_autospec(_REAL_LIVE_NAMES, side_effect=lambda user_id: list(rows)),
    )
    created: list[str] = []
    agent = _create_agent(created)
    config = {"configurable": {"thread_id": "t-resume"}}

    out = await agent.ainvoke({"messages": [("user", "go")]}, config)
    # The card: the standing approval was withheld at proposal time.
    (pending,) = out["__interrupt__"]

    # The duplicate is deleted while the card waits; the user declines.
    rows.clear()
    out = await agent.ainvoke(
        Command(resume={pending.id: {"decisions": [{"type": "reject"}]}}), config
    )

    assert created == []
    assert [m.content for m in out["messages"] if m.type == "tool"] == [
        "declined 700.HK"
    ]


# --- What the duplicate read counts -------------------------------------------


@pytest.mark.asyncio
async def test_the_duplicate_read_carries_the_live_workspace_fence():
    """A deleted or unbound workspace is not a duplicate: the read the guard
    consults fences rows exactly like the name-key lookups do."""
    from src.server.database import workspace as workspace_db

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.__aenter__.return_value = cursor
    cursor.fetchall.return_value = [
        {"workspace_id": EXISTING_ID, "name": "META"}
    ]
    ran: dict[str, Any] = {}

    async def _execute(sql, params):
        ran["sql"], ran["params"] = sql, params

    cursor.execute.side_effect = _execute
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.cursor.return_value = cursor

    @asynccontextmanager
    async def _get_db_connection(_conn=None):
        yield conn

    with patch.object(workspace_db, "get_db_connection", _get_db_connection):
        rows = await workspace_db.get_live_workspace_names(USER)

    assert FENCE_LIVE_WORKSPACE in ran["sql"]
    assert "workspace_id, name" in ran["sql"]
    assert ran["params"] == (USER,)
    assert rows == [{"workspace_id": EXISTING_ID, "name": "META"}]
