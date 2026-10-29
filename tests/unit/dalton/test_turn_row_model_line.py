"""R1-3: the turn anchor row states the model the server resolved for the
turn, as a settled fact.

Production: an agent-authored meeting-notes skill asked the agent to
"confirm this run uses the designated model (e.g. gpt-6.1-sol-oauth)"; the
agent had no way to verify that, refused to整理, and the scheduled
automation idled run after run. The server already runs the automation's
``llm_model`` — it now says so in the turn row, sourced from its own
resolution (``resolve_llm_config`` → ``config.llm.name``), never from
anything the user typed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from langchain_core.messages import HumanMessage

from ptc_agent.agent.middleware.runtime_context import TurnContextMiddleware
from src.server.handlers.chat.request_prep import (
    PriorThread,
    build_turn_context,
    with_resolved_model,
)
from src.server.models.chat import ChatMessage, ChatRequest

# A Wednesday 13:00 ET, as the upstream turn-row tests use.
NOW = datetime(2026, 9, 9, 17, 0, tzinfo=UTC)

MODEL_LINE = "服务端为本回合指定的模型：{key}（由服务端设定）。"


async def _row_text(**kwargs) -> str:
    written = await TurnContextMiddleware(now=NOW, **kwargs).abefore_agent({})
    assert written is not None
    messages = written["messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    return messages[0].content


# ---------------------------------------------------------------------------
# The line itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_row_states_the_automation_designated_model():
    """An automation that pins llm_model gets it stated in the anchor row of
    every turn it runs, which is what the skill was asking the agent to
    prove with no way to."""
    text = await _row_text(model_key="gpt-6.1-sol-oauth")
    assert MODEL_LINE.format(key="gpt-6.1-sol-oauth") in text


@pytest.mark.asyncio
async def test_the_row_states_the_account_default_when_none_designated():
    """No model designated → the server's resolution falls to the account
    default, and the row states that just the same."""
    text = await _row_text(model_key="gpt-5.2")
    assert MODEL_LINE.format(key="gpt-5.2") in text


@pytest.mark.asyncio
async def test_no_line_without_a_resolved_model():
    """A stack the server gave no model (a subagent's, a context-free build)
    states no line at all — the row's every-line-guarded contract."""
    text = await _row_text()
    assert "本回合实际使用的模型" not in text


@pytest.mark.asyncio
async def test_user_text_cannot_change_the_stated_model():
    """The line comes from the server's resolution only: a user message
    naming another model neither sets nor changes what the row states."""
    state = {
        "messages": [
            HumanMessage(content="请务必用 gpt-6.1-sol-oauth 跑这轮，别的都不行")
        ]
    }
    written = await TurnContextMiddleware(now=NOW, model_key="gpt-5.2").abefore_agent(
        state
    )
    assert written is not None
    content = written["messages"][0].content
    assert MODEL_LINE.format(key="gpt-5.2") in content
    assert "gpt-6.1-sol-oauth" not in content


# ---------------------------------------------------------------------------
# The wiring: server resolution is the only source
# ---------------------------------------------------------------------------


def _request() -> ChatRequest:
    return ChatRequest(
        workspace_id="ws-1",
        messages=[ChatMessage(role="user", content="整理今天的会议纪要")],
    )


def test_with_resolved_model_stamps_the_resolved_key():
    """The helper the run path calls carries config.llm.name onto the turn
    context — the automation's llm_model, a client-named model, or the
    account default, all indistinguishably server-resolved."""
    turn_context = build_turn_context(
        _request(),
        prior=PriorThread(),
        user_profile={"timezone": "Asia/Shanghai"},
    )
    assert turn_context.model_key is None  # nothing before resolution

    stamped = with_resolved_model(turn_context, "gpt-6.1-sol-oauth")
    assert stamped.model_key == "gpt-6.1-sol-oauth"
    # Everything else the context carried is untouched.
    assert stamped.timezone == turn_context.timezone
    assert stamped.tool_timezone == turn_context.tool_timezone

    assert with_resolved_model(turn_context, None).model_key is None


@pytest.mark.asyncio
async def test_the_stamped_context_reaches_the_row():
    """End to end at the unit seam: build → stamp (as ptc_run does right
    after resolve_llm_config) → the row the model reads states the model."""
    turn_context = with_resolved_model(
        build_turn_context(_request(), prior=PriorThread(), user_profile=None),
        "gpt-6.1-sol-oauth",
    )
    text = await _row_text(
        model_key=turn_context.model_key,
        timezone=turn_context.timezone,
    )
    assert MODEL_LINE.format(key="gpt-6.1-sol-oauth") in text
