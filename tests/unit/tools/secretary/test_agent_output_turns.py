"""The ``turns`` arg must flow agent_output -> extract_text_from_thread.

agent_output exposes an optional ``turns`` window (default 1 = latest turn).
These pin that the value reaches the extraction layer instead of being
silently dropped at the tool boundary, and that ThreadOutput | None is mapped
to the tool's dict contract.
"""

from __future__ import annotations

import json
from unittest.mock import create_autospec, patch

import pytest

from ptc_agent.agent.limits import is_pending_read
from src.server.services.thread_output import ThreadOutput, extract_text_from_thread
from src.tools.secretary.chief_of_staff import agent_output, verify_thread_owner

USER_ID = "u-1"
THREAD_ID = "33333333-3333-3333-3333-333333333333"
WORKSPACE_ID = "44444444-4444-4444-4444-444444444444"


def _extract_stub(output: ThreadOutput | None = None, **kwargs):
    """Autospec standing in for extract_text_from_thread."""
    if output is None and "return_value" not in kwargs and "side_effect" not in kwargs:
        output = ThreadOutput(workspace_id="", status="completed", text="ok")
    if "return_value" not in kwargs and "side_effect" not in kwargs:
        kwargs["return_value"] = output
    return create_autospec(extract_text_from_thread, **kwargs)


def _payload(result) -> dict:
    return json.loads(result.update["messages"][0].content)


@pytest.mark.asyncio
async def test_agent_output_defaults_to_one_turn():
    extract = _extract_stub(ThreadOutput(workspace_id=WORKSPACE_ID, status="completed", text="ok"))
    with patch(
        "src.tools.secretary.chief_of_staff.verify_thread_owner",
        create_autospec(verify_thread_owner, return_value=None),
    ), patch(
        "src.server.services.thread_output.extract_text_from_thread",
        extract,
    ):
        result = await agent_output.ainvoke(
            {"name": "agent_output", "args": {"thread_id": THREAD_ID}, "id": "c1", "type": "tool_call"},
            config={"configurable": {"user_id": USER_ID}},
        )

    extract.assert_awaited_once_with(THREAD_ID, 1, timezone="UTC")
    assert _payload(result) == {
        "thread_id": THREAD_ID,
        "workspace_id": WORKSPACE_ID,
        "status": "completed",
        "text": "ok",
    }
    assert not is_pending_read(result.update["messages"][0])


@pytest.mark.asyncio
async def test_agent_output_forwards_turns():
    extract = _extract_stub()
    with patch(
        "src.tools.secretary.chief_of_staff.verify_thread_owner",
        create_autospec(verify_thread_owner, return_value=None),
    ), patch(
        "src.server.services.thread_output.extract_text_from_thread",
        extract,
    ):
        await agent_output.ainvoke(
            {
                "name": "agent_output",
                "args": {"thread_id": THREAD_ID, "turns": 5},
                "id": "c2",
                "type": "tool_call",
            },
            config={"configurable": {"user_id": USER_ID, "timezone": "Asia/Tokyo"}},
        )

    extract.assert_awaited_once_with(THREAD_ID, 5, timezone="Asia/Tokyo")


@pytest.mark.asyncio
async def test_agent_output_returns_not_found_when_thread_deleted_mid_read():
    """None from extract_text_from_thread maps to status='not_found'."""
    extract = _extract_stub(return_value=None)
    with patch(
        "src.tools.secretary.chief_of_staff.verify_thread_owner",
        create_autospec(verify_thread_owner, return_value=None),
    ), patch(
        "src.server.services.thread_output.extract_text_from_thread",
        extract,
    ):
        result = await agent_output.ainvoke(
            {"name": "agent_output", "args": {"thread_id": THREAD_ID}, "id": "c3", "type": "tool_call"},
            config={"configurable": {"user_id": USER_ID}},
        )

    assert _payload(result) == {
        "text": "",
        "status": "not_found",
        "thread_id": THREAD_ID,
        "workspace_id": "",
    }
    assert not is_pending_read(result.update["messages"][0])


@pytest.mark.asyncio
async def test_agent_output_surfaces_read_failure_as_error():
    """A read failure becomes an error payload, not an empty success result."""
    extract = _extract_stub(side_effect=RuntimeError("db down"))
    with patch(
        "src.tools.secretary.chief_of_staff.verify_thread_owner",
        create_autospec(verify_thread_owner, return_value=None),
    ), patch(
        "src.server.services.thread_output.extract_text_from_thread",
        extract,
    ):
        result = await agent_output.ainvoke(
            {"name": "agent_output", "args": {"thread_id": THREAD_ID}, "id": "c4", "type": "tool_call"},
            config={"configurable": {"user_id": USER_ID}},
        )

    assert _payload(result)["success"] is False
