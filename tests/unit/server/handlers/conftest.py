"""Fixtures shared by the handler tests.

The model-resolution tests under this directory were written against an empty
bundled-agent roster, which is what upstream ships: no ``plugins/*/agents/``
bundle of its own. Dalton's evidence and reviewer subagents
(``plugins/dalton/agents/``) make ``bundled_agent_plans()`` non-empty, which
sends ``plugin_bases`` at ``list_account_disables`` — a database query these
tests never mock, and should not have to: what they pin is model resolution,
not the built-in subagents. The roster is pinned empty, restoring exactly the
condition the tests were written for. A test that patches ``plugin_bases`` or
``bundled_agent_plans`` itself is unaffected: its own patch lands on top.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from src.server.services.plugins.bundled import bundled_agent_plans


@pytest.fixture(autouse=True)
def _no_bundled_agents(monkeypatch):
    monkeypatch.setattr(
        "src.server.services.plugins.bundled.bundled_agent_plans",
        create_autospec(bundled_agent_plans, return_value=()),
    )


@pytest.fixture
def spend():
    """One model call, made where the agent's model node makes its own: under
    the runnable config it finds. It reports 1,200 input tokens, 1,000 of them
    read from the cache, and 300 output tokens, on ``billing_type``. With
    ``stopped``, the task making it is cancelled while the answer's end is
    being reported, the moment a Stop or a timeout can land after a call."""
    from langchain_core.callbacks import AsyncCallbackHandler
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    class _StopAsTheEndIsReported(AsyncCallbackHandler):
        run_inline = True

        def __init__(self, caller: asyncio.Task):
            self.caller = caller

        async def on_llm_end(self, *_args, **_kwargs) -> None:
            self.caller.cancel()

    async def _spend(billing_type: str = "platform", *, stopped: bool = False) -> None:
        answer = AIMessage(
            "summary",
            response_metadata={"model_name": "claude-test"},
            usage_metadata={
                "input_tokens": 1200,
                "output_tokens": 300,
                "total_tokens": 1500,
                "input_token_details": {"cache_read": 1000},
            },
        )
        model = GenericFakeChatModel(
            messages=iter([answer]),
            metadata={"billing_type": billing_type},
            callbacks=[_StopAsTheEndIsReported(asyncio.current_task())] if stopped else None,
        )
        await model.ainvoke("summarize")

    return _spend


@pytest.fixture
def usage_rows():
    """The usage records written, as ``create_usage_record`` is handed them."""
    rows: list[dict] = []
    conn = MagicMock()

    @asynccontextmanager
    async def _transaction():
        yield

    @asynccontextmanager
    async def _connection():
        yield conn

    async def _create(data, conn=None):
        rows.append(data)
        return True

    conn.transaction = _transaction
    with (
        patch("src.server.database.pool.get_db_connection", new=_connection),
        patch(
            "src.server.database.conversation.create_usage_record",
            new=AsyncMock(side_effect=_create),
        ),
    ):
        yield rows
