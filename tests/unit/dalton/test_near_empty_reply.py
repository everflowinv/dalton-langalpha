"""Unit tests for near-empty reply detection, shared 1-retry sub-budget, HF2 refusal coexistence,
and mixed-sequence bounds (§6)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_config

from ptc_agent.agent.middleware.tool import empty_call_retry
from ptc_agent.agent.middleware.tool.empty_call_retry import (
    _EMPTY_REPLY_NOTICE,
    _OUTPUT_CAP_NOTICE,
    EmptyToolCallRetryMiddleware,
)
from src.dalton.llm.near_empty_reply import (
    NEAR_EMPTY_REPLY_HINT,
    NEAR_EMPTY_REPLY_NOTICE,
    is_near_empty_reply,
)
from src.dalton.middleware.refusal_fallback import REFUSAL_NOTICE_CHANNEL


def _cfg(*, platform: str = "feishu", origin: str = "user") -> dict:
    return {
        "configurable": {
            "thread_id": "th-near-empty",
            "checkpoint_ns": "",
            "platform": platform,
        },
        "metadata": {"origin": origin, "platform": platform},
    }


def _patch_user_turn(monkeypatch, config: dict | None = None) -> list[dict]:
    monkeypatch.setattr(
        empty_call_retry,
        "get_config",
        create_autospec(get_config, return_value=config or _cfg()),
    )
    emitted: list[dict] = []

    def _emit(event: dict, *, is_subagent: bool) -> None:
        if not is_subagent:
            emitted.append(event)

    from src.dalton.llm import near_empty_reply as ner_mod

    monkeypatch.setattr(
        ner_mod,
        "emit_main_stream_event",
        create_autospec(ner_mod.emit_main_stream_event, side_effect=_emit),
    )
    return emitted


def _make_request(messages, *, state_messages=None):
    req = SimpleNamespace(messages=list(messages))
    if state_messages is not None:
        req.state = {"messages": list(state_messages)}
    return req


def _resp(msg: AIMessage) -> ModelResponse:
    return ModelResponse(result=[msg])


def _sync_sig(request):
    return ModelResponse(result=[])


async def _async_sig(request):
    return ModelResponse(result=[])


def _refusal_msg(msg_id: str = "m-ref") -> AIMessage:
    return AIMessage(
        id=msg_id,
        content="I cannot assist with that.",
        response_metadata={"stop_reason": "refusal", "model_name": "claude-opus-5-5-oauth"},
    )


def _symbol_msg(content: str = "═", msg_id: str = "m-sym") -> AIMessage:
    return AIMessage(
        id=msg_id,
        content=content,
        response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5-oauth"},
    )


def _empty_msg(msg_id: str = "m-empty") -> AIMessage:
    return AIMessage(
        id=msg_id,
        content=[{"type": "thinking", "thinking": "long thought", "signature": "sig"}],
        response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5-oauth"},
    )


class TestNearEmptyDetection:
    @pytest.mark.parametrize("text", ["═", "═══", "---", "... ", "**", "```", "═" * 16])
    def test_symbol_only_within_16_chars_caught(self, text: str) -> None:
        turn = [HumanMessage(content="请详细分析贵州茅台第三季度财报的现金流变化")]
        assert is_near_empty_reply(_symbol_msg(text), turn) is True

    def test_symbol_only_over_16_chars_allowed(self) -> None:
        turn = [HumanMessage(content="请画一条分隔线")]
        assert is_near_empty_reply(_symbol_msg("═" * 17), turn) is False

    def test_symbol_explicitly_requested_by_short_prompt_exempt(self) -> None:
        turn = [HumanMessage(content="只输出符号 ═")]
        assert is_near_empty_reply(_symbol_msg("═"), turn) is False

    def test_refusal_reply_always_defers_to_hf2(self) -> None:
        turn = [HumanMessage(content="请分析这份财报")]
        refused_symbol = AIMessage(
            content="═",
            response_metadata={"stop_reason": "refusal"},
        )
        assert is_near_empty_reply(refused_symbol, turn) is False

    @pytest.mark.parametrize(
        "tool_name",
        [
            "send_message",
            "show_widget",
            "ShowWidget",
            "dalton_monitor",
            "schedule_wakeup",
            "delegate_to_analyst",
        ],
    )
    def test_global_delivery_tool_pre_exemption_covers_symbol_and_single_char(
        self, tool_name: str
    ) -> None:
        turn = [
            HumanMessage(content="把分析发到群里，然后给我确认"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "tc1", "name": tool_name, "args": {}, "type": "tool_call"}
                ],
            ),
            ToolMessage(content="ok", tool_call_id="tc1", name=tool_name, status="success"),
        ]
        assert is_near_empty_reply(_symbol_msg("═"), turn) is False
        assert is_near_empty_reply(_symbol_msg("我"), turn) is False

    def test_failed_delivery_tool_does_not_exempt(self) -> None:
        turn = [
            HumanMessage(content="把分析发到群里，然后给我详细说明原因"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "tc1", "name": "send_message", "args": {}, "type": "tool_call"}
                ],
            ),
            ToolMessage(content="failed", tool_call_id="tc1", name="send_message", status="error"),
        ]
        assert is_near_empty_reply(_symbol_msg("═"), turn) is True

    @pytest.mark.parametrize("frag", ["我", "根", "1."])
    def test_isolated_single_char_fragments_caught_on_long_prompt(self, frag: str) -> None:
        turn = [HumanMessage(content="请详细分析这家公司的毛利率变化原因并给出估值区间")]
        assert is_near_empty_reply(_symbol_msg(frag), turn) is True

    @pytest.mark.parametrize("ok_reply", ["好的", "收到", "是的", "OK", "Yes", "No"])
    def test_two_plus_semantic_chars_allowed(self, ok_reply: str) -> None:
        turn = [HumanMessage(content="请确认是否收到刚才上传的 2026 年三季报全文文档")]
        assert is_near_empty_reply(_symbol_msg(ok_reply), turn) is False

    def test_single_char_exemptions_a_and_b(self) -> None:
        # Exemption A: short prompt <= 8 non-space chars
        assert is_near_empty_reply(_symbol_msg("我"), [HumanMessage(content="在吗")]) is False
        # Exemption A: explicit single-word / option prompt
        assert (
            is_near_empty_reply(
                _symbol_msg("B"),
                [HumanMessage(content="综合上述全部材料，请选择 B 或 C 中最稳健的一个方案")],
            )
            is False
        )
        # Exemption B: valid single-char ideograph
        assert (
            is_near_empty_reply(
                _symbol_msg("是。"),
                [HumanMessage(content="结合最新财报数据，这家公司本季度现金流是否转正了？")],
            )
            is False
        )
        # Exemption B: digit on count/calc prompt (but NOT "1.")
        calc_prompt = [HumanMessage(content="请帮我数一下上面列出的重点风险条目总共有几个？")]
        assert is_near_empty_reply(_symbol_msg("5"), calc_prompt) is False
        assert is_near_empty_reply(_symbol_msg("1."), calc_prompt) is True
        # Exemption B: option letter when option list present
        opt_prompt = [HumanMessage(content="哪个最合理？\nA. 降价\nB. 提价\nC. 维持")]
        assert is_near_empty_reply(_symbol_msg("B"), opt_prompt) is False


class TestSharedBudgetAndMixedSequences:
    def test_symbol_then_good_recovers_and_emits_discard(self, monkeypatch) -> None:
        emitted = _patch_user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        req = _make_request([HumanMessage(content="请给出完整的三季报点评与估值结论")])
        good = AIMessage(id="m-good", content="三季报收入同比增 22%。", response_metadata={"stop_reason": "end_turn"})
        handler = create_autospec(_sync_sig, side_effect=[_resp(_symbol_msg("═", "m-0")), _resp(good)])

        res = mw.wrap_model_call(req, handler)

        assert handler.call_count == 2
        assert res.result[-1] is good
        assert req.messages[-1].content == NEAR_EMPTY_REPLY_HINT
        assert {"type": "message_discarded", "agent": "main", "id": "m-0"} in emitted

    @pytest.mark.parametrize(
        ("first", "second", "expected_notice"),
        [
            (_symbol_msg("═", "m-0"), _empty_msg("m-1"), _EMPTY_REPLY_NOTICE),
            (_empty_msg("m-0"), _symbol_msg("═", "m-1"), NEAR_EMPTY_REPLY_NOTICE),
            (_empty_msg("m-0"), _empty_msg("m-1"), _EMPTY_REPLY_NOTICE),
            (_symbol_msg("═", "m-0"), _symbol_msg("═", "m-1"), NEAR_EMPTY_REPLY_NOTICE),
        ],
        ids=["sym-then-empty", "empty-then-sym", "empty-then-empty", "sym-then-sym"],
    )
    def test_non_refusal_empty_and_near_empty_share_one_retry_budget(
        self, monkeypatch, first: AIMessage, second: AIMessage, expected_notice: str
    ) -> None:
        emitted = _patch_user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        req = _make_request([HumanMessage(content="请详细分析这份季报的核心变化")])
        handler = create_autospec(_sync_sig, side_effect=[_resp(first), _resp(second), _resp(second)])

        res = mw.wrap_model_call(req, handler)

        assert handler.call_count == 2
        assert len(res.result) == 1
        assert res.result[0].content == expected_notice
        assert any(e["type"] == "exhausted_notice" and e["content"] == expected_notice for e in emitted)

    def test_full_refusal_preserves_hf2_two_retries_and_refusal_notice(self, monkeypatch) -> None:
        emitted = _patch_user_turn(monkeypatch, _cfg(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        req = _make_request([HumanMessage(content="分析这家公司")])
        handler = create_autospec(
            _sync_sig,
            side_effect=[_resp(_refusal_msg("r0")), _resp(_refusal_msg("r1")), _resp(_refusal_msg("r2"))],
        )

        res = mw.wrap_model_call(req, handler)

        assert handler.call_count == 3
        assert res.result[0].content == REFUSAL_NOTICE_CHANNEL
        assert any(
            e["type"] == "exhausted_notice" and e["content"] == REFUSAL_NOTICE_CHANNEL
            for e in emitted
        )

    @pytest.mark.parametrize(
        ("seq", "expected_calls", "expected_notice"),
        [
            (
                [_refusal_msg("r0"), _symbol_msg("═", "s1"), _symbol_msg("═", "s2")],
                3,
                NEAR_EMPTY_REPLY_NOTICE,
            ),
            (
                [_symbol_msg("═", "s0"), _refusal_msg("r1"), _refusal_msg("r2")],
                3,
                _EMPTY_REPLY_NOTICE,
            ),
        ],
        ids=["refusal-sym-sym", "sym-refusal-refusal"],
    )
    def test_refusal_and_near_empty_alternation_respects_outer_cap_and_all_refused_false(
        self, monkeypatch, seq: list[AIMessage], expected_calls: int, expected_notice: str
    ) -> None:
        _patch_user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        req = _make_request([HumanMessage(content="请分析三季报并总结核心变化")])
        handler = create_autospec(_sync_sig, side_effect=[_resp(m) for m in seq])

        res = mw.wrap_model_call(req, handler)

        assert handler.call_count == expected_calls
        assert res.result[0].content == expected_notice
        assert res.result[0].content != REFUSAL_NOTICE_CHANNEL

    @pytest.mark.asyncio
    async def test_mixed_symbol_then_output_cap_then_continuation_succeeds(self, monkeypatch) -> None:
        emitted = _patch_user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        req = _make_request([HumanMessage(content="请输出完整的深度研报")])
        sym = _symbol_msg("═", "m-0")
        capped = AIMessage(
            id="m-1",
            content="前半段",
            response_metadata={"finish_reason": "MAX_TOKENS", "model_name": "gemini-4-neon"},
        )
        cont = AIMessage(
            id="m-2",
            content="后半段",
            response_metadata={"finish_reason": "STOP", "model_name": "gemini-4-neon"},
        )
        handler = create_autospec(_async_sig, side_effect=[_resp(sym), _resp(capped), _resp(cont)])

        res = await mw.awrap_model_call(req, handler)

        assert handler.await_count == 3
        assert res.result[-1] is cont
        assert cont.additional_kwargs.get("continued_from_message_id") == "m-1"
        assert sym.additional_kwargs.get("dalton_retried_attempt") is True
        assert {"type": "message_discarded", "agent": "main", "id": "m-0"} in emitted
        assert {
            "type": "continuation_link",
            "agent": "main",
            "continued_from_message_id": "m-1",
        } in emitted

    def test_mixed_symbol_then_output_cap_then_output_cap_keeps_prefix_and_cap_notice(
        self, monkeypatch
    ) -> None:
        _patch_user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        req = _make_request([HumanMessage(content="请输出完整的深度研报")])
        sym = _symbol_msg("═", "m-0")
        cap1 = AIMessage(
            id="m-1",
            content="前半段",
            response_metadata={"finish_reason": "MAX_TOKENS"},
        )
        cap2 = AIMessage(
            id="m-2",
            content="中半段",
            response_metadata={"finish_reason": "MAX_TOKENS"},
        )
        handler = create_autospec(_sync_sig, side_effect=[_resp(sym), _resp(cap1), _resp(cap2)])

        res = mw.wrap_model_call(req, handler)

        assert handler.call_count == 3
        assert res.result[-1].content == _OUTPUT_CAP_NOTICE
        kept_texts = [
            m.content
            for m in res.result
            if isinstance(m, AIMessage) and not (m.additional_kwargs or {}).get("dalton_retried_attempt")
        ]
        assert kept_texts == ["前半段", "中半段", _OUTPUT_CAP_NOTICE]


class TestR2MandatoryFixes:
    @pytest.mark.parametrize("emoji", ["👍", "✅", "🙏", "👌", "👍🏻", "❤️", "🤝", "🎉"])
    def test_common_reaction_emojis_are_valid_short_replies(self, emoji: str) -> None:
        turn = [HumanMessage(content="请详细分析这份财报并在确认后告诉我")]
        assert is_near_empty_reply(_symbol_msg(emoji), turn) is False

    def test_failed_send_message_with_tool_status_success_does_not_exempt_near_empty(self) -> None:
        turn_failed = [
            HumanMessage(content="把分析发到飞书群里"),
            AIMessage(
                id="a-call",
                content="",
                tool_calls=[{"id": "tc-1", "name": "send_message", "args": {"target": "feishu:oc_1", "text": "hello"}}],
            ),
            ToolMessage(
                name="send_message",
                status="success",
                tool_call_id="tc-1",
                content="status: failed\ncode: not_allowed\nNothing was sent.",
                artifact={"type": "message_delivery", "status": "failed", "code": "not_allowed"},
            ),
        ]
        assert is_near_empty_reply(_symbol_msg("═"), turn_failed) is True

        turn_unknown = [
            HumanMessage(content="把分析发到飞书群里"),
            ToolMessage(
                name="send_message",
                status="success",
                tool_call_id="tc-2",
                content="status: unknown\ncode: unavailable\nDelivery unconfirmed.",
                artifact={"type": "message_delivery", "status": "unknown", "code": "unavailable"},
            ),
        ]
        assert is_near_empty_reply(_symbol_msg("═"), turn_unknown) is True

        turn_sent = [
            HumanMessage(content="把分析发到飞书群里"),
            ToolMessage(
                name="send_message",
                status="success",
                tool_call_id="tc-3",
                content="status: sent\nto: feishu:oc_1",
                artifact={"type": "message_delivery", "status": "sent"},
            ),
        ]
        assert is_near_empty_reply(_symbol_msg("═"), turn_sent) is False

    def test_near_empty_and_empty_ignore_commentary_phase(self) -> None:
        from src.dalton.llm.near_empty_reply import is_deliverable_reply_empty

        turn = [HumanMessage(content="请分析三季度利润表变化并给出结论")]
        commentary_then_symbol = AIMessage(
            id="m-phased-sym",
            content=[
                {"type": "text", "text": "让我检查一下财务数据和附注。", "phase": "commentary"},
                {"type": "text", "text": "═", "phase": "final_answer"},
            ],
            response_metadata={"stop_reason": "end_turn"},
        )
        assert is_near_empty_reply(commentary_then_symbol, turn) is True

        commentary_only = AIMessage(
            id="m-commentary-only",
            content=[
                {"type": "text", "text": "我先看一下利润表数据。", "phase": "commentary"},
            ],
            response_metadata={"stop_reason": "end_turn"},
        )
        assert is_deliverable_reply_empty(commentary_only) is True

    def test_output_cap_sanitize_preserves_phase_and_replay_strips_commentary(self, monkeypatch) -> None:
        from src.dalton.channels.deliverable_text import (
            extract_deliverable_text_from_events,
            extract_deliverable_text_from_messages,
        )
        from src.server.services.history.projector import (
            history_events_to_sse,
            messages_to_history_events,
        )

        _patch_user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        req = _make_request([HumanMessage(content="请给出完整财报分析")])
        phased_cap = AIMessage(
            id="m-cap-1",
            content=[
                {"type": "text", "text": "先整理一下数据。", "phase": "commentary"},
                {"type": "text", "text": "正式前半段", "phase": "final_answer"},
            ],
            response_metadata={"finish_reason": "MAX_TOKENS"},
        )
        cont = AIMessage(
            id="m-cap-2",
            content=[{"type": "text", "text": "正式后半段", "phase": "final_answer"}],
            response_metadata={"finish_reason": "STOP"},
        )
        handler = create_autospec(_sync_sig, side_effect=[_resp(phased_cap), _resp(cont)])

        res = mw.wrap_model_call(req, handler)

        sanitized = next(m for m in res.result if isinstance(m, AIMessage) and m.id == "m-cap-1")
        assert isinstance(sanitized.content, list)
        assert [b.get("phase") for b in sanitized.content] == ["commentary", "final_answer"]

        ckpt_res = extract_deliverable_text_from_messages(res.result)
        assert ckpt_res.text == "正式前半段正式后半段"

        replay_events = history_events_to_sse(
            messages_to_history_events(res.result), thread_id="th-phased"
        )
        replay_res = extract_deliverable_text_from_events(replay_events)
        assert replay_res.text == "正式前半段正式后半段"

    @pytest.mark.asyncio
    async def test_output_cap_exhausted_preserves_prefix_across_checkpoint_live_and_replay(
        self, monkeypatch
    ) -> None:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, MessagesState, StateGraph

        from src.dalton.channels.deliverable_text import (
            DeliverableTextAccumulator,
            extract_deliverable_text_from_events,
            extract_deliverable_text_from_messages,
        )
        from src.server.services.history.projector import (
            history_events_to_sse,
            messages_to_history_events,
        )
        from src.server.services.runs.sse_producer import RunSSEProducer

        monkeypatch.setattr(
            empty_call_retry,
            "get_config",
            create_autospec(get_config, return_value=_cfg()),
        )
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        sym = _symbol_msg("═", "m-0")
        cap1 = AIMessage(
            id="m-1",
            content="前半段",
            response_metadata={"finish_reason": "MAX_TOKENS"},
        )
        cap2 = AIMessage(
            id="m-2",
            content="后半段",
            response_metadata={"finish_reason": "MAX_TOKENS"},
        )
        responses = [_resp(sym), _resp(cap1), _resp(cap2)]
        idx = 0

        async def _model_handler(_req):
            nonlocal idx
            r = responses[idx]
            idx += 1
            return r

        recorded_result: list = []

        async def model_node(state: MessagesState):
            req = _make_request(state["messages"], state_messages=state["messages"])
            out = await mw.awrap_model_call(req, _model_handler)
            recorded_result.extend(out.result)
            return {"messages": list(out.result)}

        builder = StateGraph(MessagesState)
        builder.add_node("model", model_node)
        builder.add_edge(START, "model")
        builder.add_edge("model", END)
        graph = builder.compile(checkpointer=MemorySaver())

        producer = RunSSEProducer(thread_id="th-cap-exhaust", run_id="run-cap-exhaust")
        live_acc = DeliverableTextAccumulator()
        notice_frames: list[dict] = []

        async for frame in producer._stream_frames(
            graph,
            {"messages": [HumanMessage(content="请输出完整的深度研报")]},
            {"configurable": {"thread_id": "th-cap-exhaust"}, "metadata": {"origin": "user"}},
        ):
            if frame is None:
                continue
            ev = frame.event
            data = frame.data
            live_acc.push_event(ev, data)
            if ev == "message_chunk" and data.get("content") == _OUTPUT_CAP_NOTICE:
                notice_frames.append(data)

        expected = f"前半段后半段{_OUTPUT_CAP_NOTICE}"

        # 1. Notice carries continued_from_message_id pointing to last valid prefix segment.
        notice_msg = recorded_result[-1]
        assert notice_msg.additional_kwargs.get("continued_from_message_id") == "m-2"

        # 2. Notice emitted ONCE on real LangGraph live stream (no custom+node duplication).
        assert len(notice_frames) == 1
        assert live_acc.result().text == expected

        # 3. Checkpoint extractor preserves prefix + notice verbatim.
        assert extract_deliverable_text_from_messages(recorded_result).text == expected

        # 4. /replay projection + extractor preserves prefix + notice verbatim.
        replay_events = history_events_to_sse(
            messages_to_history_events(recorded_result), thread_id="th-cap-exhaust"
        )
        assert extract_deliverable_text_from_events(replay_events).text == expected

    @pytest.mark.asyncio
    async def test_real_langgraph_node_emits_exhausted_notice_once_for_symbol_then_empty(
        self, monkeypatch
    ) -> None:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, MessagesState, StateGraph

        from src.dalton.channels.deliverable_text import DeliverableTextAccumulator
        from src.server.services.runs.sse_producer import RunSSEProducer

        monkeypatch.setattr(
            empty_call_retry,
            "get_config",
            create_autospec(get_config, return_value=_cfg()),
        )
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        seq = [_resp(_symbol_msg("═", "m-s0")), _resp(_empty_msg("m-e1"))]
        idx = 0

        async def _model_handler(_req):
            nonlocal idx
            r = seq[idx]
            idx += 1
            return r

        async def model_node(state: MessagesState):
            req = _make_request(state["messages"], state_messages=state["messages"])
            out = await mw.awrap_model_call(req, _model_handler)
            return {"messages": list(out.result)}

        builder = StateGraph(MessagesState)
        builder.add_node("model", model_node)
        builder.add_edge(START, "model")
        builder.add_edge("model", END)
        graph = builder.compile(checkpointer=MemorySaver())

        producer = RunSSEProducer(thread_id="th-dedup", run_id="run-dedup")
        live_acc = DeliverableTextAccumulator()
        notice_chunks: list[dict] = []

        async for frame in producer._stream_frames(
            graph,
            {"messages": [HumanMessage(content="请分析财报")]},
            {"configurable": {"thread_id": "th-dedup"}, "metadata": {"origin": "user"}},
        ):
            if frame is None:
                continue
            live_acc.push_event(frame.event, frame.data)
            if (
                frame.event == "message_chunk"
                and frame.data.get("content_type") == "text"
                and _EMPTY_REPLY_NOTICE in str(frame.data.get("content") or "")
            ):
                notice_chunks.append(frame.data)

        assert idx == 2
        assert len(notice_chunks) == 1
        assert live_acc.result().text == _EMPTY_REPLY_NOTICE

    @pytest.mark.asyncio
    async def test_sse_producer_message_discarded_carries_both_final_id_and_stream_chunk_id(
        self,
    ) -> None:
        from langchain_core.messages import AIMessageChunk

        from src.dalton.channels.deliverable_text import DeliverableTextAccumulator
        from src.server.services.runs.sse_producer import RunSSEProducer

        producer = RunSSEProducer(thread_id="th-mismatch", run_id="run-mismatch")
        live_acc = DeliverableTextAccumulator()

        async for frame in producer._process_message_chunk(
            AIMessageChunk(id="lc_run--chunk-1", content="═"),
            "main",
            {"langgraph_node": "model"},
        ):
            live_acc.push_event(frame.event, frame.data)

        handled, discard_frames = producer._dalton_stream_control.handle_custom_event(
            {"type": "message_discarded", "agent": "main", "id": "msg-final-1"},
            "main",
            producer._build_sse_event,
        )
        assert handled is True
        assert len(discard_frames) == 1
        ddata = discard_frames[0].data
        assert ddata["id"] == "msg-final-1"
        assert ddata["stream_id"] == "lc_run--chunk-1"
        assert set(ddata["ids"]) == {"msg-final-1", "lc_run--chunk-1"}

        live_acc.push_event(discard_frames[0].event, ddata)
        live_acc.push_event(
            "message_chunk",
            {
                "agent": "main",
                "id": "msg-final-2",
                "content_type": "text",
                "content": "完整回答",
            },
        )
        assert live_acc.result().text == "完整回答"

    def test_retry_generated_send_message_reuses_turn_idempotency_key(self, monkeypatch) -> None:
        from src.dalton.llm.near_empty_reply import attach_retry_send_dedup

        cfg = _cfg()
        cfg["configurable"]["run_id"] = "run-dedup-send"
        _patch_user_turn(monkeypatch, cfg)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        turn_msgs = [
            HumanMessage(content="请发日报到飞书"),
            AIMessage(
                id="m-prev",
                content="",
                tool_calls=[
                    {
                        "id": "tc-first",
                        "name": "send_message",
                        "args": {"target": "feishu:oc_group1", "text": "日报"},
                    }
                ],
            ),
            ToolMessage(
                name="send_message",
                status="success",
                tool_call_id="tc-first",
                content="status: sent\nto: feishu:oc_group1",
                artifact={"type": "message_delivery", "status": "sent"},
            ),
        ]
        req = _make_request(turn_msgs, state_messages=turn_msgs)
        # First attempt triggers tool_calls retry; second attempt re-generates send_message with new id.
        broken = AIMessage(
            id="m-broken",
            content="",
            tool_calls=[],
            response_metadata={"stop_reason": "tool_use"},
        )
        retry_send = AIMessage(
            id="m-retry-send",
            content="",
            tool_calls=[
                {
                    "id": "tc-retry-new",
                    "name": "send_message",
                    "args": {"target": "feishu:oc_group1", "text": "日报"},
                }
            ],
            response_metadata={"stop_reason": "tool_use"},
        )
        handler = create_autospec(_sync_sig, side_effect=[_resp(broken), _resp(retry_send)])

        res = mw.wrap_model_call(req, handler)
        assert res.result[-1] is retry_send

        payload = attach_retry_send_dedup(
            {
                "thread_id": "th-near-empty",
                "run_id": "run-dedup-send",
                "tool_call_id": "tc-retry-new",
                "target": "feishu:oc_group1",
                "text": "日报",
            },
            run_id="run-dedup-send",
            tool_call_id="tc-retry-new",
        )
        assert payload["idempotency_key"] == "run-dedup-send:tc-first"
        assert payload["retry_dedup_by_target"] is True


class TestR3MandatoryFixes:
    @pytest.mark.asyncio
    async def test_r3_output_cap_then_near_empty_discard_then_success_keeps_prefix_across_checkpoint_live_and_replay(
        self, monkeypatch
    ) -> None:
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, MessagesState, StateGraph

        from src.dalton.channels.deliverable_text import (
            DeliverableTextAccumulator,
            extract_deliverable_text_from_events,
            extract_deliverable_text_from_messages,
        )
        from src.server.services.history.projector import (
            history_events_to_sse,
            messages_to_history_events,
        )
        from src.server.services.runs.sse_producer import RunSSEProducer

        monkeypatch.setattr(
            empty_call_retry,
            "get_config",
            create_autospec(get_config, return_value=_cfg()),
        )
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        cap_prefix = AIMessage(
            id="p",
            content="前半段",
            response_metadata={"finish_reason": "MAX_TOKENS"},
        )
        near_empty = _symbol_msg("═", "m-near-empty")
        final_tail = AIMessage(
            id="m-tail",
            content="后半段",
            response_metadata={"finish_reason": "STOP"},
        )
        responses = [_resp(cap_prefix), _resp(near_empty), _resp(final_tail)]
        idx = 0

        async def _model_handler(_req):
            nonlocal idx
            r = responses[idx]
            idx += 1
            return r

        recorded_result: list = []

        async def model_node(state: MessagesState):
            req = _make_request(state["messages"], state_messages=state["messages"])
            out = await mw.awrap_model_call(req, _model_handler)
            recorded_result.extend(out.result)
            return {"messages": list(out.result)}

        builder = StateGraph(MessagesState)
        builder.add_node("model", model_node)
        builder.add_edge(START, "model")
        builder.add_edge("model", END)
        graph = builder.compile(checkpointer=MemorySaver())

        producer = RunSSEProducer(thread_id="th-r3-chain", run_id="run-r3-chain")
        live_acc = DeliverableTextAccumulator()
        async for frame in producer._stream_frames(
            graph,
            {"messages": [HumanMessage(content="请输出完整的深度研报")]},
            {"configurable": {"thread_id": "th-r3-chain"}, "metadata": {"origin": "user"}},
        ):
            if frame is not None:
                live_acc.push_event(frame.event, frame.data)

        assert idx == 3
        assert recorded_result[-1].id == "m-tail"
        assert recorded_result[-1].additional_kwargs.get("continued_from_message_id") == "p"
        # 1. Checkpoint extraction
        assert extract_deliverable_text_from_messages(recorded_result).text == "前半段后半段"
        # 2. Live SSE stream
        assert live_acc.result().text == "前半段后半段"
        # 3. /replay projection
        replay_events = history_events_to_sse(
            messages_to_history_events(recorded_result), thread_id="th-r3-chain"
        )
        assert extract_deliverable_text_from_events(replay_events).text == "前半段后半段"

    @pytest.mark.asyncio
    async def test_r3_producer_attempt_id_attribution_with_lc_run_chunks_and_exhausted_notice(
        self,
    ) -> None:
        from langchain_core.messages import AIMessageChunk

        from src.dalton.channels.deliverable_text import DeliverableTextAccumulator
        from src.server.services.runs.sse_producer import RunSSEProducer

        producer = RunSSEProducer(thread_id="th-r3-attr", run_id="run-r3-attr")
        live_acc = DeliverableTextAccumulator()

        # Attempt 0: streams truncated prefix under lc_run--prefix, committed id is msg-prefix
        producer._dalton_stream_control.handle_custom_event(
            {"type": "attempt_start", "agent": "main", "attempt": 0},
            "main",
            producer._build_sse_event,
        )
        async for frame in producer._process_message_chunk(
            AIMessageChunk(
                id="lc_run--prefix",
                content="前半段",
                response_metadata={"finish_reason": "MAX_TOKENS"},
            ),
            "main",
            {"langgraph_node": "model"},
        ):
            live_acc.push_event(frame.event, frame.data)

        producer._dalton_stream_control.handle_custom_event(
            {
                "type": "continuation_link",
                "agent": "main",
                "continued_from_message_id": "msg-prefix",
            },
            "main",
            producer._build_sse_event,
        )

        # Attempt 1: empty aggregate with NO streamed chunk of its own
        producer._dalton_stream_control.handle_custom_event(
            {"type": "attempt_start", "agent": "main", "attempt": 1},
            "main",
            producer._build_sse_event,
        )
        handled, discard_frames = producer._dalton_stream_control.handle_custom_event(
            {"type": "message_discarded", "agent": "main", "id": "msg-empty-1"},
            "main",
            producer._build_sse_event,
        )
        assert handled is True
        assert len(discard_frames) == 1
        ddata = discard_frames[0].data
        # Must NOT misattribute attempt 0's lc_run--prefix to attempt 1's discard!
        assert ddata["id"] == "msg-empty-1"
        assert "stream_id" not in ddata
        assert "ids" not in ddata
        live_acc.push_event(discard_frames[0].event, ddata)

        # Exhausted notice chained onto committed id msg-prefix MUST NOT be dropped
        # just because live stream saw lc_run--prefix instead of msg-prefix.
        handled, notice_frames = producer._dalton_stream_control.handle_custom_event(
            {
                "type": "exhausted_notice",
                "agent": "main",
                "id": "dalton-notice-r3",
                "content": _OUTPUT_CAP_NOTICE,
                "continued_from_message_id": "msg-prefix",
            },
            "main",
            producer._build_sse_event,
        )
        assert handled is True
        assert len(notice_frames) == 1
        ndata = notice_frames[0].data
        assert ndata["continued_from_message_id"] == "lc_run--prefix"
        live_acc.push_event(notice_frames[0].event, ndata)

        assert live_acc.result().text == f"前半段{_OUTPUT_CAP_NOTICE}"

        # Also test deferred notice is flushed at stream end when no prefix ever streamed.
        producer2 = RunSSEProducer(thread_id="th-r3-defer", run_id="run-r3-defer")
        handled2, immediate2 = producer2._dalton_stream_control.handle_custom_event(
            {
                "type": "exhausted_notice",
                "agent": "main",
                "id": "dalton-notice-defer",
                "content": _OUTPUT_CAP_NOTICE,
                "continued_from_message_id": "unseen-prefix",
            },
            "main",
            producer2._build_sse_event,
        )
        assert handled2 is True
        assert immediate2 == []
        flushed = producer2._dalton_stream_control.flush_deferred_notices(
            producer2._build_sse_event
        )
        assert len(flushed) == 1
        assert flushed[0].data["content"] == _OUTPUT_CAP_NOTICE

    @pytest.mark.asyncio
    async def test_r3_retry_send_dedup_persists_across_restart_and_allows_distinct_content(
        self, monkeypatch
    ) -> None:
        from src.dalton.llm import near_empty_reply as ner_mod
        from src.dalton.llm.near_empty_reply import (
            RETRY_SEND_DEDUP_KWARG,
            RetrySendDedupInfo,
            aattach_retry_send_dedup,
            compute_send_payload_digest,
        )

        run_id = "11111111-1111-4111-8111-111111111111"
        cfg = _cfg()
        cfg["configurable"]["run_id"] = run_id
        _patch_user_turn(monkeypatch, cfg)

        persisted_store: dict[tuple[str, str], dict] = {}

        async def _fake_persist(rid: str | None, records: dict[str, dict]) -> bool:
            if rid:
                for k, v in records.items():
                    persisted_store[(rid, k)] = dict(v)
            return True

        async def _fake_load(rid: str | None, tcid: str | None) -> RetrySendDedupInfo | None:
            entry = persisted_store.get((str(rid), str(tcid)))
            if not entry:
                return None
            return RetrySendDedupInfo(
                delivery_key=entry.get("delivery_key"),
                retry_dedup_by_target=bool(entry.get("retry_dedup_by_target", True)),
                payload_digest=entry.get("payload_digest"),
            )

        monkeypatch.setattr(
            ner_mod,
            "persist_retry_send_dedup_to_db",
            create_autospec(ner_mod.persist_retry_send_dedup_to_db, side_effect=_fake_persist),
        )
        monkeypatch.setattr(
            ner_mod,
            "load_retry_send_dedup_from_db",
            create_autospec(ner_mod.load_retry_send_dedup_from_db, side_effect=_fake_load),
        )

        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        turn_msgs = [
            HumanMessage(content="查询后发结论"),
            AIMessage(
                id="m-status",
                content="",
                tool_calls=[
                    {
                        "id": "tc-progress",
                        "name": "send_message",
                        "args": {"target": None, "text": "正在查询，请稍候"},
                    }
                ],
            ),
            ToolMessage(
                name="send_message",
                status="success",
                tool_call_id="tc-progress",
                content="status: sent",
                artifact={"type": "message_delivery", "status": "sent"},
            ),
        ]
        req = _make_request(turn_msgs, state_messages=turn_msgs)
        broken = AIMessage(
            id="m-broken",
            content="",
            tool_calls=[],
            response_metadata={"stop_reason": "tool_use"},
        )
        retry_diff = AIMessage(
            id="m-retry-diff",
            content="",
            tool_calls=[
                {
                    "id": "tc-final-conclusion",
                    "name": "send_message",
                    "args": {"target": None, "text": "完整结论：营收同比+25%"},
                }
            ],
            response_metadata={"stop_reason": "tool_use"},
        )
        handler = create_autospec(_async_sig, side_effect=[_resp(broken), _resp(retry_diff)])

        res = await mw.awrap_model_call(req, handler)
        assert res.result[-1] is retry_diff
        # Persisted in checkpoint additional_kwargs AND DB
        assert RETRY_SEND_DEDUP_KWARG in (retry_diff.additional_kwargs or {})
        assert (run_id, "tc-final-conclusion") in persisted_store
        # Distinct content MUST NOT reuse tc-progress's idempotency_key!
        assert persisted_store[(run_id, "tc-final-conclusion")]["delivery_key"] is None

        # Simulate process restart: clear in-process cache and reload from DB
        with ner_mod._RETRY_SEND_DEDUP_LOCK:
            ner_mod._RETRY_SEND_DEDUP_REGISTRY.clear()

        payload = await aattach_retry_send_dedup(
            {
                "thread_id": "th-near-empty",
                "run_id": run_id,
                "tool_call_id": "tc-final-conclusion",
                "target": None,
                "text": "完整结论：营收同比+25%",
                "files": [],
            },
            run_id=run_id,
            tool_call_id="tc-final-conclusion",
        )
        assert "idempotency_key" not in payload
        assert payload["retry_dedup_by_target"] is True
        assert payload["payload_digest"] == compute_send_payload_digest("完整结论：营收同比+25%", [])

    @pytest.mark.asyncio
    async def test_r3_authorize_and_claim_delivery_retry_dedup_sql_locks_logical_turn_and_compares_digest(
        self, monkeypatch
    ) -> None:
        from contextlib import asynccontextmanager

        from src.dalton.database import channel_gateway as cg_db
        from src.dalton.llm.near_empty_reply import compute_send_payload_digest

        root_run = "11111111-1111-4111-8111-111111111110"
        retry_run = "11111111-1111-4111-8111-111111111112"
        digest_same = compute_send_payload_digest("完整结论：营收+20%", [])
        digest_diff = compute_send_payload_digest("补充更新：现金流转正", [])

        executed: list[tuple[str, tuple]] = []
        prior_row_holder: list[dict | None] = [None]

        class _Cur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def execute(self, sql, params=()):
                self._last_sql = " ".join(str(sql).split())
                executed.append((self._last_sql, tuple(params) if isinstance(params, (tuple, list)) else (params,)))

            async def fetchone(self):
                s = self._last_sql
                if "FROM dalton_channel_deliveries WHERE delivery_key" in s:
                    return None
                if "FROM dalton_channel_deliveries WHERE run_id = ANY" in s:
                    return prior_row_holder[0]
                if "FROM dalton_channel_inbound_events" in s:
                    return None
                if "FROM dalton_channel_background_runs" in s:
                    return None
                if "FROM dalton_channel_identities" in s:
                    return {"platform_user_id": "ou_1", "user_id": "u-1"}
                return None

            async def fetchall(self):
                s = self._last_sql
                if "WITH RECURSIVE ancestors" in s:
                    return [
                        {"run_id": root_run, "root_run_id": root_run},
                        {"run_id": retry_run, "root_run_id": root_run},
                    ]
                return []

        class _Conn:
            @asynccontextmanager
            async def transaction(self):
                yield

            def cursor(self, **_kw):
                return _Cur()

        @asynccontextmanager
        async def _fake_conn(_conn=None):
            yield _Conn()

        monkeypatch.setattr(cg_db, "get_db_connection", _fake_conn)
        monkeypatch.setattr(cg_db, "reject_company_system_auth", create_autospec(cg_db.reject_company_system_auth))
        monkeypatch.setattr(cg_db, "require_channel_platform_capability", create_autospec(cg_db.require_channel_platform_capability))
        monkeypatch.setattr(cg_db, "get_home_workspace_id", create_autospec(cg_db.get_home_workspace_id, return_value=None))
        monkeypatch.setattr(
            cg_db.wx_db,
            "verify_gateway_epoch_in_tx",
            create_autospec(cg_db.wx_db.verify_gateway_epoch_in_tx, return_value=None),
        )

        # 1. Concurrent in_flight duplicate in the same /retry attempt chain -> in_flight_or_unknown
        prior_row_holder[0] = {
            "delivery_key": f"{root_run}:tc-1",
            "status": "in_flight",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "is_current": False,
            "sent_chunks": 0,
            "delivery_attempt": 1,
        }
        res_inflight = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"{retry_run}:tc-2",
            kind="agent_send",
            user_id="u-1",
            run_id=retry_run,
            platform="feishu",
            target="feishu:@me",
            retry_dedup_by_target=True,
            payload_digest=digest_same,
        )
        assert res_inflight["claim_status"] == "in_flight_or_unknown"
        # Advisory lock used logical turn root + normalized target + payload_digest
        lock_params = [p for sql, p in executed if "pg_advisory_xact_lock" in sql]
        assert (f"dalton_retry_send:{root_run}:feishu:@me:{digest_same}",) in lock_params
        # Dedup SQL walks attempt_run_ids AND compares payload_digest WITHOUT blind is_current match
        dedup_sqls = [sql for sql, _ in executed if "FROM dalton_channel_deliveries WHERE run_id = ANY" in sql]
        assert len(dedup_sqls) == 1
        assert "payload_digest" in dedup_sqls[0]
        assert "is_current = TRUE" not in dedup_sqls[0]

        # 2. Matching sent record in earlier /retry attempt -> already_completed
        prior_row_holder[0] = {
            "delivery_key": f"{root_run}:tc-1",
            "status": "sent",
            "platform": "feishu",
            "target_address": "feishu:@me",
            "is_current": False,
            "first_response_json": {"status": "sent", "payload_digest": digest_same},
            "delivery_attempt": 1,
        }
        res_sent = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"{retry_run}:tc-3",
            kind="agent_send",
            user_id="u-1",
            run_id=retry_run,
            platform="feishu",
            target="feishu:@me",
            retry_dedup_by_target=True,
            payload_digest=digest_same,
        )
        assert res_sent["claim_status"] == "already_completed"

        # 3. Distinct payload_digest -> no prior match -> claimed (NOT blocked!)
        prior_row_holder[0] = None
        res_diff = await cg_db.authorize_and_claim_delivery(
            delivery_key=f"{retry_run}:tc-4",
            kind="agent_send",
            user_id="u-1",
            run_id=retry_run,
            platform="feishu",
            target="feishu:@me",
            retry_dedup_by_target=True,
            payload_digest=digest_diff,
        )
        assert res_diff["claim_status"] == "claimed"


class TestR4MandatoryFixes:
    @pytest.mark.asyncio
    async def test_r4_output_cap_then_empty_tool_use_then_continuation_live_checkpoint_replay_parity(
        self, monkeypatch
    ) -> None:
        from langchain_core.messages import AIMessageChunk
        from langgraph.checkpoint.memory import MemorySaver
        from langgraph.graph import END, START, MessagesState, StateGraph

        from src.dalton.channels.deliverable_text import (
            DeliverableTextAccumulator,
            extract_deliverable_text_from_events,
            extract_deliverable_text_from_messages,
        )
        from src.server.services.history.projector import (
            history_events_to_sse,
            messages_to_history_events,
        )
        from src.server.services.runs.sse_producer import RunSSEProducer

        monkeypatch.setattr(
            empty_call_retry,
            "get_config",
            create_autospec(get_config, return_value=_cfg()),
        )
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        cap_prefix = AIMessage(
            id="p-cap",
            content="前半段",
            response_metadata={"finish_reason": "MAX_TOKENS"},
        )
        empty_tool_use = AIMessage(
            id="m-empty-tool",
            content="",
            tool_calls=[],
            response_metadata={"stop_reason": "tool_use"},
        )
        final_tail = AIMessage(
            id="m-cont-ok",
            content="后半段",
            response_metadata={"finish_reason": "STOP"},
        )
        responses = [_resp(cap_prefix), _resp(empty_tool_use), _resp(final_tail)]
        idx = 0

        async def _model_handler(_req):
            nonlocal idx
            r = responses[idx]
            idx += 1
            return r

        recorded_result: list = []

        async def model_node(state: MessagesState):
            req = _make_request(state["messages"], state_messages=state["messages"])
            out = await mw.awrap_model_call(req, _model_handler)
            recorded_result.extend(out.result)
            return {"messages": list(out.result)}

        builder = StateGraph(MessagesState)
        builder.add_node("model", model_node)
        builder.add_edge(START, "model")
        builder.add_edge("model", END)
        graph = builder.compile(checkpointer=MemorySaver())

        # 1. Full LangGraph stream through RunSSEProducer
        producer = RunSSEProducer(thread_id="th-r4-empty-tool", run_id="run-r4-empty-tool")
        live_acc = DeliverableTextAccumulator()
        async for frame in producer._stream_frames(
            graph,
            {"messages": [HumanMessage(content="请输出完整的深度研报")]},
            {"configurable": {"thread_id": "th-r4-empty-tool"}, "metadata": {"origin": "user"}},
        ):
            if frame is not None:
                live_acc.push_event(frame.event, frame.data)

        assert idx == 3
        assert recorded_result[-1].id == "m-cont-ok"
        assert recorded_result[-1].additional_kwargs.get("continued_from_message_id") == "p-cap"
        assert extract_deliverable_text_from_messages(recorded_result).text == "前半段后半段"
        assert live_acc.result().text == "前半段后半段"
        assert live_acc.result().saw_business_tool_call is False

        replay_events = history_events_to_sse(
            messages_to_history_events(recorded_result), thread_id="th-r4-empty-tool"
        )
        replay_res = extract_deliverable_text_from_events(replay_events)
        assert replay_res.text == "前半段后半段"
        assert replay_res.saw_business_tool_call is False

        # 2. Direct live streaming chunks including Anthropic stop_reason="tool_use" with NO tool calls
        producer2 = RunSSEProducer(thread_id="th-r4-stream-empty-tc", run_id="run-r4-stream-empty-tc")
        stream_acc = DeliverableTextAccumulator()
        producer2._dalton_stream_control.handle_custom_event(
            {"type": "attempt_start", "agent": "main", "attempt": 0},
            "main",
            producer2._build_sse_event,
        )
        async for frame in producer2._process_message_chunk(
            AIMessageChunk(
                id="lc_run--cap",
                content="前半段",
                response_metadata={"finish_reason": "MAX_TOKENS"},
            ),
            "main",
            {"langgraph_node": "model"},
        ):
            stream_acc.push_event(frame.event, frame.data)
        producer2._dalton_stream_control.handle_custom_event(
            {"type": "continuation_link", "agent": "main", "continued_from_message_id": "p-cap"},
            "main",
            producer2._build_sse_event,
        )
        producer2._dalton_stream_control.handle_custom_event(
            {"type": "attempt_start", "agent": "main", "attempt": 1},
            "main",
            producer2._build_sse_event,
        )
        async for frame in producer2._process_message_chunk(
            AIMessageChunk(
                id="lc_run--empty-tool",
                content="",
                response_metadata={"stop_reason": "tool_use"},
            ),
            "main",
            {"langgraph_node": "model"},
        ):
            stream_acc.push_event(frame.event, frame.data)
        _, discard_frames = producer2._dalton_stream_control.handle_custom_event(
            {"type": "message_discarded", "agent": "main", "id": "m-empty-tool", "attempt": 1},
            "main",
            producer2._build_sse_event,
        )
        for df in discard_frames:
            stream_acc.push_event(df.event, df.data)
        producer2._dalton_stream_control.handle_custom_event(
            {"type": "attempt_start", "agent": "main", "attempt": 2},
            "main",
            producer2._build_sse_event,
        )
        async for frame in producer2._process_message_chunk(
            AIMessageChunk(
                id="lc_run--tail",
                content="后半段",
                response_metadata={"finish_reason": "STOP"},
            ),
            "main",
            {"langgraph_node": "model"},
        ):
            stream_acc.push_event(frame.event, frame.data)
        assert stream_acc.result().text == "前半段后半段"
        assert stream_acc.result().saw_business_tool_call is False

    @pytest.mark.asyncio
    async def test_r4_send_payload_digest_distinguishes_same_filename_across_two_workspaces(
        self, monkeypatch
    ) -> None:
        import hashlib
        import json

        from src.dalton.llm import near_empty_reply as ner_mod
        from src.dalton.llm.near_empty_reply import (
            aattach_retry_send_dedup,
            compute_send_payload_digest,
            register_retry_send_dedup,
        )

        ws_a = "11111111-1111-4111-8111-111111111111"
        ws_b = "22222222-2222-4222-8222-222222222222"
        run_id = "33333333-3333-4333-8333-333333333333"

        digest_a_str = compute_send_payload_digest(
            "请查收报告", ["report.pdf"], default_workspace_id=ws_a
        )
        digest_a_dict = compute_send_payload_digest(
            "请查收报告", [{"path": "report.pdf", "workspace_id": ws_a}], default_workspace_id=ws_b
        )
        digest_b_dict = compute_send_payload_digest(
            "请查收报告", [{"path": "report.pdf", "workspace_id": ws_b}], default_workspace_id=ws_a
        )
        expected_canonical_a = json.dumps(
            {
                "files": [{"path": "report.pdf", "workspace_id": ws_a}],
                "text": "请查收报告",
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        assert digest_a_str == hashlib.sha256(expected_canonical_a.encode("utf-8")).hexdigest()
        assert digest_a_str == digest_a_dict
        assert digest_a_dict != digest_b_dict

        # Chief of Staff already delivered ws_a/report.pdf; retry sends ws_b/report.pdf with same text & target
        turn_msgs = [
            HumanMessage(content="把两个工作区的 report.pdf 都发给我"),
            AIMessage(
                id="m-send-a",
                content="",
                tool_calls=[
                    {
                        "id": "tc-send-a",
                        "name": "send_message",
                        "args": {
                            "target": "feishu:@me",
                            "text": "请查收报告",
                            "files": ["report.pdf"],
                            "workspace_id": ws_a,
                        },
                    }
                ],
            ),
            ToolMessage(
                name="send_message",
                status="success",
                tool_call_id="tc-send-a",
                content="status: sent",
                artifact={"type": "message_delivery", "status": "sent"},
            ),
        ]
        retry_send_b = AIMessage(
            id="m-send-b",
            content="",
            tool_calls=[
                {
                    "id": "tc-send-b",
                    "name": "send_message",
                    "args": {
                        "target": "feishu:@me",
                        "text": "请查收报告",
                        "files": ["report.pdf"],
                        "workspace_id": ws_b,
                    },
                }
            ],
        )
        retry_send_a_dup = AIMessage(
            id="m-send-a-dup",
            content="",
            tool_calls=[
                {
                    "id": "tc-send-a-dup",
                    "name": "send_message",
                    "args": {
                        "target": "feishu:@me",
                        "text": "请查收报告",
                        "files": ["report.pdf"],
                        "workspace_id": ws_a,
                    },
                }
            ],
        )
        cfg = {"configurable": {"run_id": run_id, "workspace_id": "00000000-0000-4000-8000-000000000001"}}
        recs_b = register_retry_send_dedup(retry_send_b, turn_msgs, config=cfg, attempt=1)
        recs_a = register_retry_send_dedup(retry_send_a_dup, turn_msgs, config=cfg, attempt=1)

        # Different workspace -> MUST NOT reuse tc-send-a's delivery_key!
        assert recs_b["tc-send-b"]["delivery_key"] is None
        assert recs_b["tc-send-b"]["payload_digest"] == digest_b_dict
        # Same workspace + same file -> DOES reuse tc-send-a's delivery_key!
        assert recs_a["tc-send-a-dup"]["delivery_key"] == f"{run_id}:tc-send-a"
        assert recs_a["tc-send-a-dup"]["payload_digest"] == digest_a_dict

        monkeypatch.setattr(
            ner_mod,
            "load_retry_send_dedup_from_db",
            create_autospec(ner_mod.load_retry_send_dedup_from_db, return_value=None),
        )
        body_b = await aattach_retry_send_dedup(
            {
                "thread_id": "th-ws",
                "run_id": run_id,
                "tool_call_id": "tc-send-b",
                "workspace_id": "00000000-0000-4000-8000-000000000001",
                "target": "feishu:@me",
                "text": "请查收报告",
                "files": [{"path": "report.pdf", "workspace_id": ws_b}],
            },
            run_id=run_id,
            tool_call_id="tc-send-b",
        )
        assert "idempotency_key" not in body_b
        assert body_b["payload_digest"] == digest_b_dict

        body_a_dup = await aattach_retry_send_dedup(
            {
                "thread_id": "th-ws",
                "run_id": run_id,
                "tool_call_id": "tc-send-a-dup",
                "workspace_id": "00000000-0000-4000-8000-000000000001",
                "target": "feishu:@me",
                "text": "请查收报告",
                "files": [{"path": "report.pdf", "workspace_id": ws_a}],
            },
            run_id=run_id,
            tool_call_id="tc-send-a-dup",
        )
        assert body_a_dup["idempotency_key"] == f"{run_id}:tc-send-a"
        assert body_a_dup["payload_digest"] == digest_a_dict
