"""#661 (upstream 4e7b7a9f4): a manual /compact and a model-switch hand-off
now record their model calls on a ``conversation_usages`` row of their own
(``token_usage.between_turns``). Dalton's daily budget ledger takes cash from
two disjoint sources: usage rows no reservation holds
(``fetch_unreserved_usage_today``, bucketed per call by SQL
``dalton_cash_usd``), and ``dalton_llm_journal`` rows, which only
``dalton_complete`` writes for the server's own one-shot calls. A compaction
call runs on the thread's agent chain, never through ``dalton_complete``, so
these tests pin the once-only accounting the ledger depends on: one usage row
per operation, no journal row for the same call, and a row each call of which
lands in cash or notional, never both.

Scope: what these tests verify is the row landing once, in the right ledger
bucket by per-call billing type, with the journal doubles turned away — the
handler itself runs real, its DB write and model call are stands. What they
do not verify: real database writes (``create_usage_record`` and the pool
are patched), the reservation lifecycle (``dalton_budget_reservations``
reserving, settling and reconciling against a real DB), the hand-off verb
(it shares the ``_recorded`` code path — its rows are pinned by upstream's
``test_summary_handoff`` — but is not exercised here), and the SQL sums
themselves: ``_cash_usd_of_row`` is a transcription of ``dalton_cash_usd``
(d007), so the function's own correctness rests on code review and the
migration drill, not on this file."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch
from uuid import UUID

import pytest
from ptc_agent.config.agent import AgentConfig, LLMConfig
from ptc_agent.config.core import (
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from ptc_agent.core.project_context import ProjectContext

from src.dalton.database import llm_journal

HANDLER = "src.server.handlers.thread_maintenance"
ASK = "ptc_agent.agent.middleware.compaction.ask_between_turns"
THREAD_META = "src.server.database.conversation.get_thread_auth_meta"
WORKSPACE_ROW = "src.server.database.workspace.get_workspace"
THREAD_MODEL = "src.server.services.llm.thread_model"
RUNNER_GET_INSTANCE = (
    "src.server.services.thread_mutation.ThreadMutationRunner.get_instance"
)

PROJECT = ProjectContext("ws-1", "Macro")


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        llm=LLMConfig(
            name="system-default-model",
            background="system-background-model",
            compaction="system-compaction",
        ),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test-key")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )


@pytest.fixture
def base_config():
    return _make_agent_config()


@pytest.fixture(autouse=True)
def thread_row():
    """The thread's row and its workspace, as ``thread_route`` reads them."""
    meta = {
        "workspace_id": "ws-1",
        "llm_model": None,
        "reasoning_effort": None,
        "fast_mode": None,
        "msg_type": "ptc",
    }
    workspace = {"workspace_id": "ws-1", "user_id": "user-1", "status": "running"}
    with (
        patch(THREAD_META, new=AsyncMock(side_effect=lambda _t: meta)),
        patch(WORKSPACE_ROW, new=AsyncMock(side_effect=lambda _w: workspace)),
    ):
        yield {"meta": meta, "workspace": workspace}


@pytest.fixture(autouse=True)
def mutation_runner():
    """The unfenced pass-through the upstream handler tests stub in: these
    tests exercise the accounting, not the fence."""
    from src.server.services.thread_mutation import MutationSession

    runner = MagicMock()

    @asynccontextmanager
    async def _exclusive(thread_id, verb):
        yield MutationSession(conn=None, saver=None)

    runner.exclusive = _exclusive
    with patch(RUNNER_GET_INSTANCE, return_value=runner):
        yield runner


def _stub_thread_agent():
    from src.server.handlers.thread_maintenance import ThreadAgent

    graph = MagicMock()
    graph.aupdate_state = AsyncMock(return_value=None)
    state = MagicMock()
    state.values = {"_summarization_event": None}
    messages = [MagicMock(id="m1"), MagicMock(id="m2")]

    async def _stub(
        thread_id, verb, config=None, checkpointer=None, user_id=None, held=None, role="analyst"
    ):
        return ThreadAgent(thread_id, graph, state, messages, "ws-1", None, PROJECT)

    return _stub


def _compaction():
    from langchain_core.messages import HumanMessage

    from ptc_agent.agent.middleware.compaction import Compaction
    from ptc_agent.agent.middleware.compaction.summarize import Summary

    event = {"cutoff_index": 1, "summary_message": HumanMessage("ok", id="s"), "file_path": None}
    return Compaction(event, Summary("ok", "model", []), 2, [])


async def _spend(billing_type: str = "platform") -> None:
    """One model call reporting 1,200 input tokens (1,000 cached) and 300
    output tokens on ``billing_type``, as the agent's model chain would."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

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
    )
    await model.ainvoke("summarize")


def _spending(billing_type: str = "platform"):
    """An ``ask_between_turns`` stand-in that makes one model call, then
    answers with a fresh compaction."""
    compaction = _compaction()

    async def _call(*_args, **_kwargs):
        await _spend(billing_type)
        return compaction

    return AsyncMock(side_effect=_call)


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


@pytest.fixture
def journal_writes():
    """Test doubles for every write into ``dalton_llm_journal``: a compaction
    call has no business near them, and one that did arrive would be counted
    a second time by ``fetch_journal_cash_today``."""
    claimed = create_autospec(
        llm_journal.claim_journal_invocation, return_value=("claimed", {"attempt": 1})
    )
    settled = create_autospec(
        llm_journal.complete_journal_done,
        return_value={"invocation_id": "lj1:test", "state": "done"},
    )
    with (
        patch("src.dalton.llm.complete.claim_journal_invocation", new=claimed),
        patch("src.dalton.llm.complete.complete_journal_done", new=settled),
    ):
        yield claimed, settled


COMPACT_MESSAGES = "ptc_agent.agent.middleware.compaction.compact_messages"


async def _compact_by_the_user(base_config, billing_type: str = "platform"):
    from src.server.handlers.thread_maintenance import trigger_compaction

    with (
        patch("src.server.app.setup.agent_config", base_config),
        patch(f"{HANDLER}._thread_agent", new=_stub_thread_agent()),
        patch(ASK, new=_spending(billing_type)),
        patch(
            COMPACT_MESSAGES,
            new=AsyncMock(side_effect=AssertionError("the summarizer alone ran")),
        ),
        patch(
            "src.server.database.api_keys.is_byok_active",
            new=AsyncMock(return_value=False),
        ),
        patch(f"{THREAD_MODEL}.resolve_turn_config", new=AsyncMock(return_value=base_config)),
    ):
        return await trigger_compaction("thread-1", user_id="user-1")


def _cash_usd_of_row(row: dict) -> float:
    """What SQL ``dalton_cash_usd`` reads out of the row: the per-call costs
    whose billing is not oauth, plus infrastructure credits over the rate
    (d007_llm_call_journal.py:17-34). Mirrored here because the function lives
    in the database."""
    from src.config.env import USD_TO_CREDITS_RATE

    tu = row.get("token_usage") or {}
    calls = tu.get("per_call_costs") if isinstance(tu, dict) else None
    if not isinstance(calls, list):
        return float(row.get("total_credits") or 0) / float(USD_TO_CREDITS_RATE)
    return sum(
        float(c.get("cost") or 0) for c in calls if str(c.get("billing_type", "platform")) != "oauth"
    ) + float(row.get("infrastructure_credits") or 0) / float(USD_TO_CREDITS_RATE)


@pytest.mark.asyncio
async def test_a_manual_compact_is_counted_once_by_the_budget_ledger(
    base_config, usage_rows, journal_writes
):
    """One compaction call, one place it lands: exactly one usage row
    (``between_turns``), no ``dalton_llm_journal`` row for the same call, and
    a response id of its own that no reservation's ``run_ref`` can hold —
    ``reconcile_reservations_sql`` and ``fetch_unreserved_usage_today`` both
    join on ``cu.conversation_response_id = r.run_ref``, so the row stays in
    the unreserved bucket instead of being counted inside a run's reservation
    too."""
    await _compact_by_the_user(base_config, billing_type="platform")

    (row,) = usage_rows
    assert row["token_usage"]["between_turns"] == "compact"
    assert row["token_usage"]["by_model"]["claude-test"]["output_tokens"] == 300

    claimed, settled = journal_writes
    assert claimed.await_count == 0, "a compaction call must not also write dalton_llm_journal"
    assert settled.await_count == 0

    # The row's response id is a fresh UUID the handler minted for it, not a
    # run's: two operations get two different ones, so neither a reservation's
    # run_ref nor another row's id can fold the same call in twice.
    await _compact_by_the_user(base_config, billing_type="platform")
    first, second = (UUID(r["conversation_response_id"]) for r in usage_rows)
    assert first != second
    assert {r["msg_type"] for r in usage_rows} == {"ptc"}


@pytest.mark.asyncio
@pytest.mark.parametrize("billing_type", ["platform", "oauth"])
async def test_a_compact_call_lands_in_cash_or_notional_never_both(
    base_config, usage_rows, journal_writes, monkeypatch, billing_type
):
    """``spent_usd`` draws only on cash: per-call entries whose billing is
    oauth are excluded by ``dalton_cash_usd``'s WHERE, and subscription
    notional is reported next to ``spent_usd``, never inside it. So the row
    this operation writes feeds the ledger exactly as a turn's does — a
    platform call is cash-only, an oauth call costs no cash and shows up once
    in the notional estimate."""
    from src.config.env import USD_TO_CREDITS_RATE
    from src.dalton.budget import service as budget_service

    monkeypatch.setattr(
        budget_service,
        "_notional_price_index",
        lambda: {
            "claude-test": {
                "input": 3_000_000.0,
                "cached_input": 300_000.0,
                "output": 15_000_000.0,
            }
        },
    )

    await _compact_by_the_user(base_config, billing_type=billing_type)

    (row,) = usage_rows
    calls = row["token_usage"]["per_call_costs"]
    assert {c["billing_type"] for c in calls} == {billing_type}
    if billing_type == "platform":
        # Cash side: no entry excluded, and the row priced through the same
        # pass a turn's row uses (credits follow platform_cost).
        assert _cash_usd_of_row(row) == sum(c["cost"] for c in calls)
        assert row["token_credits"] == pytest.approx(
            row["token_usage"]["platform_cost"] * USD_TO_CREDITS_RATE
        )
    else:
        # Notional side: every call is oauth, so the cash sum the SQL takes is
        # zero and the notional estimate carries the spend once.
        assert _cash_usd_of_row(row) == 0
        notional = budget_service.compute_subscription_notional_usd(
            [{"token_usage": row["token_usage"], "is_byok": row["is_byok"]}]
        )
        assert notional == pytest.approx(
            (200 * 3_000_000.0 + 1000 * 300_000.0 + 300 * 15_000_000.0) / 1_000_000.0
        )


@pytest.mark.asyncio
async def test_no_operation_no_row_and_no_journal_noise(base_config, usage_rows, journal_writes):
    """A compact refused before any model call (nothing to compact) writes no
    usage row and touches no journal — the accounting only follows real
    calls."""
    from fastapi import HTTPException

    from src.server.handlers.thread_maintenance import trigger_compaction

    async def _refuse(*_args, **_kwargs):
        raise ValueError("Cannot determine valid cutoff point for compaction")

    with (
        patch("src.server.app.setup.agent_config", base_config),
        patch(f"{HANDLER}._thread_agent", new=_stub_thread_agent()),
        patch(ASK, new=AsyncMock(side_effect=_refuse)),
        pytest.raises(HTTPException) as caught,
    ):
        await trigger_compaction("thread-1", user_id="user-1")

    assert caught.value.status_code == 400
    assert usage_rows == []
    claimed, settled = journal_writes
    assert claimed.await_count == 0
    assert settled.await_count == 0
