"""Real-PostgreSQL integration test for 4-way deliverable-text consistency and
channel delivery ledger progress / single-delivery idempotency (§5.5).

Requires ``DALTON_TEST_PG_DSN`` pointing to a PostgreSQL database whose name
ends with ``_test`` (safety guard).
"""

from __future__ import annotations

import json
import os
import uuid
from urllib.parse import urlparse

import psycopg
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from psycopg.rows import dict_row

from ptc_agent.agent.middleware.plan_close_out import CLOSE_OUT_KEY
from ptc_agent.agent.state import DeltaAgentState
from src.dalton.channels.deliverable_text import (
    AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY,
    CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE,
    CONTINUED_FROM_KWARG,
    RETRIED_ATTEMPT_KWARG,
    build_channel_empty_delivery_notice,
    extract_deliverable_text_from_events,
    resolve_automation_empty_excerpt,
)
from src.dalton.database.channel_gateway import (
    get_execution_agent_send_delivery_status,
    update_inbound_progress,
)
from src.server.services.automation_excerpt import read_run_answer
from src.server.services.history.committed import _main_text
from src.server.services.history.projector import (
    history_events_to_sse,
    messages_to_history_events,
)
from src.server.services.history.reader import CheckpointHistoryReader
from src.server.services.runs.sse_producer import RunSSEProducer

pytestmark = pytest.mark.integration


def _require_test_pg_dsn() -> str:
    dsn = os.getenv("DALTON_TEST_PG_DSN", "").strip()
    if not dsn:
        pytest.skip("DALTON_TEST_PG_DSN is not set")
    db_name = urlparse(dsn).path.lstrip("/")
    if not db_name.endswith("_test"):
        pytest.fail(
            f"Refusing to run integration test against non-_test database: {db_name!r}"
        )
    return dsn


@pytest.mark.asyncio
async def test_four_way_deliverable_text_consistency_and_empty_outlet_policies(
    monkeypatch,
) -> None:
    """Layer 1: live SSE, checkpoint replay, thread output, and read_run_answer match byte-for-byte.
    Layer 2: per-outlet empty-delivery policies + real-DB dalton_channel_deliveries progress &
    shared fallback:{run_id} single-delivery idempotency."""
    dsn = _require_test_pg_dsn()

    # --- Layer 1a: mixed phase + close_out UpdatePlan + continuation chain ---
    saver = InMemorySaver()
    checkpoint_msgs = [
        AIMessage(
            id="m-bad",
            content="═",
            additional_kwargs={RETRIED_ATTEMPT_KWARG: True},
        ),
        AIMessage(
            id="m-0",
            content=[
                {"type": "text", "text": "过程解说", "phase": "commentary"},
                {"type": "text", "text": "前半段", "phase": "final_answer"},
            ],
        ),
        AIMessage(
            id="m-1",
            content="后半段",
            additional_kwargs={CONTINUED_FROM_KWARG: "m-0"},
        ),
        AIMessage(
            id="m-close",
            content="",
            tool_calls=[
                {"id": "tc-p", "name": "UpdatePlan", "args": {}, "type": "tool_call"}
            ],
            additional_kwargs={CLOSE_OUT_KEY: True},
        ),
        ToolMessage(content="ok", tool_call_id="tc-p", name="UpdatePlan"),
    ]

    graph = (
        StateGraph(DeltaAgentState)
        .add_node("agent", lambda _s: {"messages": checkpoint_msgs})
        .add_edge(START, "agent")
        .compile(checkpointer=saver)
    )
    await graph.ainvoke(
        {"messages": [HumanMessage(content="深度分析", id="h-1")]},
        {"configurable": {"thread_id": "t-four"}, "metadata": {"run_id": "run-four"}},
    )

    # 1. Live SSE stream
    producer = RunSSEProducer(thread_id="t-four", run_id="run-four")

    async def _live_stream():
        yield ((), "messages", (AIMessageChunk(id="m-bad", content="═"), {"langgraph_node": "model"}))
        yield ((), "custom", {"type": "message_discarded", "agent": "main", "id": "m-bad"})
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="m-0",
                    content=[{"type": "text", "text": "过程解说", "phase": "commentary"}],
                ),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="m-0",
                    content=[{"type": "text", "text": "前半段", "phase": "final_answer"}],
                    response_metadata={"finish_reason": "MAX_TOKENS"},
                ),
                {"langgraph_node": "model"},
            ),
        )
        yield (
            (),
            "custom",
            {"type": "continuation_link", "agent": "main", "continued_from_message_id": "m-0"},
        )
        yield (
            (),
            "messages",
            (
                AIMessageChunk(
                    id="m-1",
                    content="后半段",
                    response_metadata={"finish_reason": "STOP"},
                ),
                {"langgraph_node": "model"},
            ),
        )

    class _FakeGraph:
        async def astream(self, *_args, **_kwargs):
            async for item in _live_stream():
                yield item

    live_events: list[dict] = []
    async for frame in producer.stream_workflow(_FakeGraph(), {}, {}):
        ev_t = None
        ev_d = None
        for line in frame.strip().splitlines():
            if line.startswith("event: "):
                ev_t = line[7:].strip()
            elif line.startswith("data: "):
                ev_d = json.loads(line[6:])
        if ev_t and isinstance(ev_d, dict):
            live_events.append({"event": ev_t, "data": ev_d})

    live_text = extract_deliverable_text_from_events(live_events).text

    # 2. Replay projection from checkpoint
    reader = CheckpointHistoryReader(saver)
    turn = await reader.aget_run_turn("t-four", "run-four")
    assert turn is not None
    replay_sse = history_events_to_sse(
        messages_to_history_events(turn.messages), thread_id="t-four"
    )
    replay_text = extract_deliverable_text_from_events(replay_sse).text

    # 3. Thread output (`committed._main_text`)
    output_text = _main_text(replay_sse)

    # 4. Automation `read_run_answer`
    monkeypatch.setattr(CheckpointHistoryReader, "get_instance", lambda: reader)
    run_answer = await read_run_answer("t-four", "run-four")

    assert live_text == replay_text == output_text == (run_answer.text or "") == "前半段后半段"

    # --- Layer 2: real PG dalton_channel_deliveries progress & automation excerpt ---
    run_id = f"run-db-{uuid.uuid4().hex[:10]}"
    exec_id = str(uuid.uuid4())
    user_id = f"user-{uuid.uuid4().hex[:8]}"

    async with await psycopg.AsyncConnection.connect(dsn, row_factory=dict_row) as conn:
        try:
            async with conn.transaction():
                # Insert a failed agent_send to current chat for channel progress verification
                await conn.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, files_status
                    )
                    VALUES (%s, 'agent_send', %s, %s, %s::uuid, TRUE, TRUE, 'feishu', 'feishu:@me', 'failed', '[]'::jsonb)
                    """,
                    (f"tool:{run_id}:c1", user_id, run_id, exec_id),
                )

            prog = await update_inbound_progress(run_id=run_id, conn=conn)
            assert prog["has_delivered_current_agent_send"] is False
            assert prog["has_failed_current_agent_send"] is True
            assert (
                build_channel_empty_delivery_notice(
                    saw_business_tool_call=True,
                    sent_to_current_count=prog["sent_to_current_count"],
                    has_delivered_current_agent_send=prog["has_delivered_current_agent_send"],
                    has_failed_current_agent_send=prog["has_failed_current_agent_send"],
                )
                == CHANNEL_SEND_FAILED_AFTER_TOOLS_NOTICE
            )

            exec_status = await get_execution_agent_send_delivery_status(exec_id, conn=conn)
            assert exec_status["has_delivered_agent_send"] is False
            assert (
                resolve_automation_empty_excerpt(
                    extracted_text="",
                    sent_text="失败发送参数",
                    has_delivered_agent_send=exec_status["has_delivered_agent_send"],
                    has_delivered_files_only=exec_status["has_delivered_files_only"],
                    saw_business_tool_call=True,
                )
                == AUTOMATION_EMPTY_AFTER_TOOLS_SUMMARY
            )

            # Now insert a files-only sent delivery to current chat -> channel skips notice
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, run_id, execution_id,
                        is_current, has_non_empty_text, platform, target_address,
                        status, files_status
                    )
                    VALUES (%s, 'agent_send', %s, %s, %s::uuid, TRUE, FALSE, 'feishu', 'feishu:@me', 'sent', '[{"path":"a.pdf","status":"sent"}]'::jsonb)
                    """,
                    (f"tool:{run_id}:c2", user_id, run_id, exec_id),
                )

            prog2 = await update_inbound_progress(run_id=run_id, conn=conn)
            assert prog2["has_delivered_current_agent_send"] is True
            assert prog2["has_delivered_current_files_only"] is True
            assert prog2["has_failed_current_agent_send"] is False
            assert (
                build_channel_empty_delivery_notice(
                    saw_business_tool_call=True,
                    sent_to_current_count=prog2["sent_to_current_count"],
                    has_delivered_current_agent_send=prog2["has_delivered_current_agent_send"],
                    has_failed_current_agent_send=prog2["has_failed_current_agent_send"],
                )
                is None
            )

            # Verify fallback:{run_id} PK uniqueness enforces single delivery across live & /replay
            fallback_key = f"fallback:{run_id}"
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, run_id,
                        is_current, has_non_empty_text, platform, target_address, status
                    )
                    VALUES (%s, 'fallback', %s, %s, TRUE, TRUE, 'feishu', 'feishu:@me', 'sent')
                    ON CONFLICT (delivery_key) DO NOTHING
                    """,
                    (fallback_key, user_id, run_id),
                )
                await conn.execute(
                    """
                    INSERT INTO dalton_channel_deliveries (
                        delivery_key, kind, user_id, run_id,
                        is_current, has_non_empty_text, platform, target_address, status
                    )
                    VALUES (%s, 'fallback', %s, %s, TRUE, TRUE, 'feishu', 'feishu:@me', 'sent')
                    ON CONFLICT (delivery_key) DO NOTHING
                    """,
                    (fallback_key, user_id, run_id),
                )
                row = await (
                    await conn.execute(
                        "SELECT COUNT(*) AS cnt FROM dalton_channel_deliveries WHERE delivery_key = %s",
                        (fallback_key,),
                    )
                ).fetchone()
                assert int(row["cnt"]) == 1
        finally:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM dalton_channel_deliveries WHERE run_id = %s",
                    (run_id,),
                )
