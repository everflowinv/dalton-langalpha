"""Tests for the empty-turn guard on EmptyToolCallRetryMiddleware.

A turn a user sent may not end on a reply with no visible text (thread
65c4641e: thinking-only replies and blank ones ended the turn "normally"
while the user got nothing). The middleware should:

1. Retry an empty final reply (thinking-only, blank, or nothing) with the
   empty-reply hint, reusing the injected-hint machinery.
2. Replace the last reply with an explicit failure notice once retries run
   out, and log a warning naming the thread.
3. Leave notification turns (a wakeup's, a report-back's) and subagent runs
   alone — their empty ends are legitimate — unless the user steered into
   them, and cover a Chief-of-Staff hand-off (origin "agent") too.
4. Leave compaction and summary calls inside the model node alone: a native
   compaction answer has no text on purpose, and an empty self-summary is
   the summarizer's to handle.
5. Keep normal text replies, tool calls, and the upstream tool_use-but-empty
   tool_calls retry exactly as they were.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.config import get_config
from langgraph.constants import TAG_NOSTREAM

from ptc_agent.agent.middleware.tool import empty_call_retry
from ptc_agent.agent.middleware.tool.empty_call_retry import (
    _EMPTY_REPLY_HINT,
    _EMPTY_REPLY_NOTICE,
    _EXHAUSTED_FAILURE_KWARG,
    _MALFORMED_CALL_HINT,
    _MALFORMED_CALL_NOTICE,
    _OUTPUT_CAP_CONTINUE_HINT,
    _OUTPUT_CAP_HINT,
    _OUTPUT_CAP_NOTICE,
    _REMEDIATION_HINT,
    EmptyToolCallRetryMiddleware,
)
from ptc_agent.agent.transcript.classify import human_kind
from src.llms.error_classification import UnfinishedReplyError


def _config(
    *,
    origin: str | None = "user",
    wake_turn: bool = False,
    checkpoint_ns: str = "",
    thread_id: str = "65c4641e",
    tags: list[str] | None = None,
) -> dict:
    """The turn-provenance slice of the run config get_config() returns."""
    config: dict = {
        "configurable": {
            "thread_id": thread_id,
            "wake_turn": wake_turn,
            "checkpoint_ns": checkpoint_ns,
        },
        "metadata": {"origin": origin},
    }
    if tags is not None:
        config["tags"] = tags
    return config


def _user_turn(monkeypatch, config: dict | None = None) -> None:
    """Point the middleware's get_config at ``config``; default a user turn."""
    if config is None:
        config = _config()
    monkeypatch.setattr(
        empty_call_retry,
        "get_config",
        create_autospec(get_config, return_value=config),
    )


def _make_request(messages, model_settings: dict | None = None):
    request = SimpleNamespace(messages=list(messages))
    if model_settings is not None:
        request.model_settings = model_settings
    return request


def _steer_message(text: str = "请继续，把结果写完。", count: int = 1) -> HumanMessage:
    """A user steer as SteeringMiddleware delivers it into a running turn."""
    return HumanMessage(
        content=f"[Steering from User]\n{text}",
        additional_kwargs={
            "lc_source": "steering",
            "steering_delivered": {"count": count},
        },
    )


def _compaction_reply() -> AIMessage:
    """A Claude native-compaction answer: a block with no text, on purpose."""
    return AIMessage(
        content=[
            {
                "type": "compaction",
                "content": "The user asked for data.",
                "signature": "cmp-1",
            }
        ],
        response_metadata={"stop_reason": "compaction", "model_name": "claude-sonnet-5-5"},
        tool_calls=[],
    )


_CLAUDE_THINKING = {"type": "thinking", "thinking": "I should answer now.", "signature": "sig"}


def _thinking_only_reply(model: str = "claude-sonnet-5-5") -> AIMessage:
    return AIMessage(
        content=[dict(_CLAUDE_THINKING)],
        response_metadata={"stop_reason": "end_turn", "model_name": model},
        tool_calls=[],
    )


def _good_text_reply(model: str = "claude-sonnet-5-5") -> AIMessage:
    return AIMessage(
        content=[dict(_CLAUDE_THINKING), {"type": "text", "text": "已写好 PWR earnings update。"}],
        response_metadata={"stop_reason": "end_turn", "model_name": model},
        tool_calls=[],
    )


def _resp(ai_msg: AIMessage) -> ModelResponse:
    return ModelResponse(result=[ai_msg])


class TestEmptyTurnGuard:
    def test_empty_final_reply_is_retried_until_answered(self, monkeypatch):
        # checkpoint_ns as the main agent's model node really sees it — a
        # node namespace, not a task one.
        _user_turn(monkeypatch, _config(checkpoint_ns="model:node-1"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="写 PWR earnings update")])
        empty = _thinking_only_reply()
        good = _good_text_reply()
        handler = MagicMock(side_effect=[_resp(empty), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[-1] is good
        # The empty reply and the corrective hint were injected, stamped so
        # replay never reads the hint as a turn opener.
        assert request.messages[-2] is empty
        assert request.messages[-1].content == _EMPTY_REPLY_HINT
        assert human_kind(request.messages[-1]) == "empty-call-retry"
        # The good answer carries Claude's thinking, so history holds what it
        # was produced behind, as for the upstream tool-call retry.
        assert result.result == [*request.messages[-2:], good]

    @pytest.mark.parametrize(
        "empty_reply",
        [
            _thinking_only_reply(),
            # Gemini/OpenRouter-style reasoning rides kwargs, content stays empty.
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "plenty of reasoning"},
                response_metadata={"stop_reason": "stop", "model_name": "gemini-3-pro"},
                tool_calls=[],
            ),
            AIMessage(
                content=[],
                response_metadata={"stop_reason": "stop", "model_name": "gemini-3-pro"},
                tool_calls=[],
            ),
            AIMessage(
                content="   \n  ",
                response_metadata={"stop_reason": "end_turn", "model_name": "claude-sonnet-5-5"},
                tool_calls=[],
            ),
        ],
        ids=["thinking-only", "reasoning-kwargs-only", "no-content", "whitespace-only"],
    )
    def test_reply_with_no_visible_text_counts_as_empty(self, monkeypatch, empty_reply):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        good = AIMessage(
            content=[{"type": "text", "text": "回答"}],
            response_metadata={"stop_reason": "stop", "model_name": "gemini-3-pro"},
            tool_calls=[],
        )
        handler = MagicMock(side_effect=[_resp(empty_reply), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result == [good]

    def test_exhausted_retries_end_with_the_failure_notice(self, monkeypatch, caplog):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        handler = MagicMock(side_effect=[_resp(_thinking_only_reply())] * 3)

        with caplog.at_level(
            logging.WARNING,
            logger="ptc_agent.agent.middleware.tool.empty_call_retry",
        ):
            result = mw.wrap_model_call(request, handler)

        # Non-refusal empty_reply shares a 1-retry sub-budget (§6.3): 1 initial + 1 retry = 2 calls.
        assert handler.call_count == 2
        assert len(result.result) == 1
        assert result.result[0].content == _EMPTY_REPLY_NOTICE
        assert any("65c4641e" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_async_path_ends_with_the_failure_notice(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        request = _make_request([HumanMessage(content="hello")])
        handler = AsyncMock(side_effect=[_resp(_thinking_only_reply())] * 2)

        result = await mw.awrap_model_call(request, handler)

        assert handler.await_count == 2
        assert result.result[0].content == _EMPTY_REPLY_NOTICE

    @pytest.mark.parametrize(
        "config",
        [
            _config(origin="system", wake_turn=True),
            _config(origin="system"),
            _config(origin="automation"),
            _config(checkpoint_ns="task:1f0a|model:3c"),
            _config(origin="user", checkpoint_ns="task:1f0a"),
            {},
        ],
        ids=["wake-turn", "report-back", "automation", "subagent-run", "subagent-head", "no-config"],
    )
    def test_turns_nobody_sent_are_left_alone(self, monkeypatch, config):
        _user_turn(monkeypatch, config)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        empty = _thinking_only_reply()
        handler = MagicMock(return_value=_resp(empty))

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result == [empty]
        assert request.messages == [HumanMessage(content="hello")]

    def test_normal_replies_on_a_user_turn_are_untouched(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        text = AIMessage(
            content="正常回答",
            response_metadata={"stop_reason": "end_turn"},
            tool_calls=[],
        )
        called = AIMessage(
            content=[],
            response_metadata={"stop_reason": "tool_use", "model_name": "claude-sonnet-5-5"},
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "x.md", "content": "hi"},
                    "id": "1",
                    "type": "tool_call",
                }
            ],
        )
        for reply in (text, called):
            r = _make_request([HumanMessage(content="hello")])
            result = mw.wrap_model_call(r, MagicMock(return_value=_resp(reply)))
            assert result.result == [reply]

    def test_tool_use_with_empty_calls_still_gets_the_json_hint(self, monkeypatch):
        """When both readings fit, the upstream tool-call shape wins, so its
        JSON-malformation hint is the one the model sees."""
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        broken = AIMessage(
            content=[dict(_CLAUDE_THINKING)],
            response_metadata={"stop_reason": "tool_use", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        good = _good_text_reply()
        handler = MagicMock(side_effect=[_resp(broken), _resp(good)])

        mw.wrap_model_call(request, handler)

        assert request.messages[-1].content == _REMEDIATION_HINT

    # --- compaction and summary calls (agy case 1) ---

    def test_provider_compaction_answer_is_returned_as_is(self, monkeypatch):
        """A native compaction reply carries no text on purpose; retrying it
        would break the compaction that asked for it."""
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request(
            [HumanMessage(content="hello")],
            model_settings={
                "extra_body": {
                    "compaction": {"type": "summarize", "instructions": "summarize"}
                }
            },
        )
        reply = _compaction_reply()
        handler = MagicMock(return_value=_resp(reply))

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result == [reply]
        assert request.messages == [HumanMessage(content="hello")]

    def test_summary_call_tagged_nostream_is_not_retried(self, monkeypatch):
        """A self-summary's empty text is the summarizer's to handle: every
        summary call rides the TAG_NOSTREAM tag CompactionMiddleware sets."""
        _user_turn(monkeypatch, _config(tags=[TAG_NOSTREAM]))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")], model_settings={})
        empty = AIMessage(
            content="",
            response_metadata={"stop_reason": "stop", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        handler = MagicMock(return_value=_resp(empty))

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result == [empty]

    def test_compaction_block_without_request_markers_is_still_not_empty(self, monkeypatch):
        """Defence from the answer's side: a reply holding a compaction block
        is never an empty final reply, whatever the request carried."""
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        reply = _compaction_reply()
        handler = MagicMock(return_value=_resp(reply))

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result == [reply]

    # --- a steer into a running turn makes it a turn someone waits on ---

    def test_user_steer_into_a_wake_turn_enables_the_guard(self, monkeypatch):
        _user_turn(monkeypatch, _config(origin="system", wake_turn=True))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request(
            [
                AIMessage(content="上一轮的回复"),
                HumanMessage(content="A wakeup arrived."),
                _steer_message(),
            ]
        )
        good = _good_text_reply()
        handler = MagicMock(side_effect=[_resp(_thinking_only_reply()), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[-1] is good

    def test_steer_that_landed_ahead_of_the_turn_input_enables_the_guard(self, monkeypatch):
        """A steer sent while the turn was starting sits ahead of its input,
        after the previous turn's last reply — quiet.py reads that window."""
        _user_turn(monkeypatch, _config(origin="system", wake_turn=True))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request(
            [
                AIMessage(content="上一轮的回复"),
                _steer_message(),
                HumanMessage(content="A wakeup arrived."),
            ]
        )
        good = _good_text_reply()
        handler = MagicMock(side_effect=[_resp(_thinking_only_reply()), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[-1] is good

    def test_steer_folded_into_a_compaction_summary_still_enables_the_guard(self, monkeypatch):
        """After a compaction the model sees a summary plus a kept tail, which
        no longer carries the steer's stamp; the turn's state still does."""
        _user_turn(monkeypatch, _config(origin="system", wake_turn=True))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        full_turn = [
            AIMessage(content="上一轮的回复"),
            HumanMessage(content="A wakeup arrived."),
            _steer_message(),
            AIMessage(content="处理中"),
        ]
        request = _make_request(
            [HumanMessage(content="[summary of the earlier conversation]")]
        )
        request.state = {"messages": full_turn}
        good = _good_text_reply()
        handler = MagicMock(side_effect=[_resp(_thinking_only_reply()), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[-1] is good

    def test_between_turns_hand_off_is_never_retried(self, monkeypatch):
        """The model-switch hand-off summary runs between turns; an empty
        answer there is the summarizer's to handle, not a reply to replace."""
        from ptc_agent.agent.middleware.compaction import between_turns

        _user_turn(monkeypatch, _config(origin="user"))
        monkeypatch.setattr(between_turns, "asked", lambda: object())
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        empty = _thinking_only_reply()
        handler = MagicMock(return_value=_resp(empty))

        result = mw.wrap_model_call(_make_request([HumanMessage(content="hi")]), handler)

        assert handler.call_count == 1
        assert result.result[-1] is empty

    def test_agent_origin_turn_enables_the_guard(self, monkeypatch):
        """A Chief-of-Staff hand-off waits on the analyst's result as the
        user does (delegate_to_analyst posts origin "agent")."""
        _user_turn(monkeypatch, _config(origin="agent"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="分析 PWR")])
        good = _good_text_reply()
        handler = MagicMock(side_effect=[_resp(_thinking_only_reply()), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[-1] is good

    # --- the failure notice keeps the turn's bookkeeping (agy case 3) ---

    def test_failure_notice_keeps_usage_and_metadata_but_not_the_id(self, monkeypatch, caplog):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        last = AIMessage(
            content="",
            id="msg-empty-3",
            usage_metadata={"input_tokens": 4100, "output_tokens": 7, "total_tokens": 4107},
            response_metadata={"stop_reason": "end_turn", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        handler = MagicMock(side_effect=[_resp(last)] * 3)

        with caplog.at_level(
            logging.WARNING,
            logger="ptc_agent.agent.middleware.tool.empty_call_retry",
        ):
            result = mw.wrap_model_call(request, handler)

        notice = result.result[0]
        assert notice.content == _EMPTY_REPLY_NOTICE
        assert notice.usage_metadata == last.usage_metadata
        assert notice.response_metadata.get("model_name") == "claude-sonnet-5-5"
        assert notice.response_metadata.get("stop_reason") == "end_turn"
        # A fresh id: the stream's dedupe has already seen the last reply's.
        assert notice.id != last.id

    # --- the hint follows the current failure shape; logs stay honest ---

    def test_hint_is_reworded_when_the_failure_shape_changes(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        broken_call = AIMessage(
            content=[dict(_CLAUDE_THINKING)],
            response_metadata={"stop_reason": "tool_use", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        good = _good_text_reply()
        empty = _thinking_only_reply()
        handler = MagicMock(
            side_effect=[_resp(broken_call), _resp(empty), _resp(good)]
        )

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        hints = [
            m
            for m in request.messages
            if isinstance(m, HumanMessage)
            and (m.additional_kwargs or {}).get("lc_source") == "empty_call_retry"
        ]
        # One hint, reworded for the shape failing now; its own broken reply
        # replaces the earlier one so every hint follows the reply it answers.
        assert len(hints) == 1
        assert hints[0].content == _EMPTY_REPLY_HINT
        assert request.messages[-2] is empty
        assert result.result[-1] is good

    def test_no_retrying_log_past_the_last_retry(self, monkeypatch, caplog):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        handler = MagicMock(side_effect=[_resp(_thinking_only_reply())] * 3)

        with caplog.at_level(
            logging.WARNING,
            logger="ptc_agent.agent.middleware.tool.empty_call_retry",
        ):
            mw.wrap_model_call(request, handler)

        retrying = [r for r in caplog.records if "retrying" in r.message]
        assert len(retrying) == 1
        assert not any("(3/2)" in r.message for r in caplog.records)

    def test_consecutive_same_kind_non_sanitized_retry_replaces_reply_with_latest(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="hello")])
        broken1 = AIMessage(
            content=[dict(_CLAUDE_THINKING), {"type": "text", "text": "attempt-1"}],
            response_metadata={"stop_reason": "tool_use", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        broken2 = AIMessage(
            content=[dict(_CLAUDE_THINKING), {"type": "text", "text": "attempt-2"}],
            response_metadata={"stop_reason": "tool_use", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        good = _good_text_reply()
        mw.wrap_model_call(request, MagicMock(side_effect=[_resp(broken1), _resp(broken2), _resp(good)]))
        assert request.messages[-2] is broken2
        assert broken1 not in request.messages
        assert request.messages[-1].content == _REMEDIATION_HINT

        req_empty = _make_request([HumanMessage(content="hello")])
        empty1 = AIMessage(
            content=[{"type": "thinking", "thinking": "t1", "signature": "s1"}],
            response_metadata={"stop_reason": "refusal", "model_name": "claude-sonnet-5-5"},
        )
        empty2 = AIMessage(
            content=[{"type": "thinking", "thinking": "t2", "signature": "s2"}],
            response_metadata={"stop_reason": "refusal", "model_name": "claude-sonnet-5-5"},
        )
        mw.wrap_model_call(req_empty, MagicMock(side_effect=[_resp(empty1), _resp(empty2), _resp(good)]))
        assert req_empty.messages[-2] is empty2
        assert empty1 not in req_empty.messages
        assert req_empty.messages[-1].content == _EMPTY_REPLY_HINT


def _sync_handler_sig(request):
    return ModelResponse(result=[])


async def _async_handler_sig(request):
    return ModelResponse(result=[])


def _sync_handler(**kwargs):
    return create_autospec(_sync_handler_sig, **kwargs)


def _async_handler(**kwargs):
    return create_autospec(_async_handler_sig, **kwargs)


def _output_cap_reply(
    *,
    response_metadata: dict | None = None,
    usage_metadata: dict | None = None,
    tool_calls: list | None = None,
    invalid_tool_calls: list | None = None,
    content: str = "我将立即创建并运行……",
    msg_id: str | None = None,
) -> AIMessage:
    return AIMessage(
        content=content,
        id=msg_id,
        response_metadata=response_metadata
        if response_metadata is not None
        else {"finish_reason": "MAX_TOKENS", "model_name": "gemini-4-neon"},
        usage_metadata=usage_metadata,
        tool_calls=tool_calls or [],
        invalid_tool_calls=invalid_tool_calls or [],
    )


class TestOutputCapRetry:
    def test_max_tokens_without_tool_calls_retries_and_continues(self, monkeypatch, caplog):
        _user_turn(monkeypatch, _config(thread_id="13e2d7b5"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="搭一个上千公式的 Excel 模型")])
        capped = _output_cap_reply(
            msg_id="msg-cap-1",
            usage_metadata={"input_tokens": 12000, "output_tokens": 65536, "total_tokens": 77536},
        )
        good = AIMessage(
            content="已分步创建骨架脚本。",
            response_metadata={"finish_reason": "STOP", "model_name": "gemini-4-neon"},
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "build.py", "content": "# skeleton"},
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )
        handler = _sync_handler(side_effect=[_resp(capped), _resp(good)])

        with caplog.at_level(
            logging.WARNING,
            logger="ptc_agent.agent.middleware.tool.empty_call_retry",
        ):
            result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        sanitized = request.messages[-2]
        assert isinstance(sanitized, AIMessage)
        assert sanitized.content == "我将立即创建并运行……"
        assert sanitized.id == "msg-cap-1"
        assert sanitized.usage_metadata == capped.usage_metadata
        assert sanitized.response_metadata == capped.response_metadata
        assert request.messages[-1].content == _OUTPUT_CAP_CONTINUE_HINT
        assert human_kind(request.messages[-1]) == "empty-call-retry"
        assert result.result == [sanitized, request.messages[-1], good]
        assert any(
            "MAX_TOKENS" in r.message
            and "65536" in r.message
            and "(1/2)" in r.message
            for r in caplog.records
        )

    def test_max_tokens_with_tool_calls_retries_and_drops_truncated_calls(self, monkeypatch):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.tools import tool

        executed: list[str] = []

        @tool
        def write_file(file_path: str, content: str) -> str:
            """Write a file."""
            executed.append(file_path)
            return "ok"

        _user_turn(monkeypatch, _config(thread_id="13e2d7b5"))
        truncated_call = _output_cap_reply(
            msg_id="msg-cap-tools",
            response_metadata={"stop_reason": "max_tokens", "model_name": "claude-sonnet-5-5"},
            content="我将写入脚本",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "half.py", "content": "x = 'unterminated"},
                    "id": "toolu_half",
                    "type": "tool_call",
                }
            ],
        )
        good = AIMessage(
            content="已完成分段写入。",
            response_metadata={"stop_reason": "end_turn", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        class _ToolFakeChatModel(GenericFakeChatModel):
            def bind_tools(self, tools, *, tool_choice=None, **kwargs):
                return self

        model = _ToolFakeChatModel(messages=iter([truncated_call, good]))
        agent = create_agent(
            model=model,
            tools=[write_file],
            middleware=[EmptyToolCallRetryMiddleware(max_retries=2)],
        )

        out = agent.invoke({"messages": [HumanMessage(content="write file")]})

        assert executed == []
        ai_msgs = [m for m in out["messages"] if isinstance(m, AIMessage)]
        assert ai_msgs[0].content == "我将写入脚本"
        assert ai_msgs[0].tool_calls == []
        assert ai_msgs[-1].content == "已完成分段写入。"
        hints = [
            m
            for m in out["messages"]
            if isinstance(m, HumanMessage) and human_kind(m) == "empty-call-retry"
        ]
        assert [h.content for h in hints] == [_OUTPUT_CAP_HINT]

    @pytest.mark.parametrize(
        ("response_metadata", "expected_reason"),
        [
            ({"stop_reason": "max_tokens", "model_name": "claude-sonnet-5-5"}, "max_tokens"),
            ({"finish_reason": "length", "model_name": "gpt-5.4"}, "length"),
            (
                {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "model_name": "gpt-5.4",
                },
                "incomplete:max_output_tokens",
            ),
            (
                {
                    "status": "incomplete",
                    "incomplete_details": SimpleNamespace(reason="max_output_tokens"),
                    "model_name": "gpt-5.4",
                },
                "incomplete:max_output_tokens",
            ),
        ],
        ids=[
            "anthropic-max_tokens",
            "openai-length",
            "responses-api-dict",
            "responses-api-obj",
        ],
    )
    def test_output_cap_stop_reasons_retry_across_providers(
        self, monkeypatch, caplog, response_metadata, expected_reason
    ):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="go")])
        capped = _output_cap_reply(response_metadata=response_metadata, usage_metadata=None)
        good = AIMessage(
            content="继续完成",
            response_metadata={"stop_reason": "end_turn"},
            tool_calls=[],
        )
        handler = _sync_handler(side_effect=[_resp(capped), _resp(good)])

        with caplog.at_level(
            logging.WARNING,
            logger="ptc_agent.agent.middleware.tool.empty_call_retry",
        ):
            result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert request.messages[-1].content == _OUTPUT_CAP_CONTINUE_HINT
        assert result.result == [request.messages[-2], request.messages[-1], good]
        assert any(
            expected_reason in r.message
            and "output_tokens=None" in r.message
            and "(1/2)" in r.message
            for r in caplog.records
        )

    @pytest.mark.parametrize(
        ("finish_reason", "should_retry"),
        [
            ("MAX_TOKENS", True),
            ("STOPMAX_TOKENS", True),
            ("STOPSTOPMAX_TOKENS", True),
            ("max_tokens", True),
            ("length", True),
            ("STOP", False),
            ("STOPSTOP", False),
            ("end_turn", False),
            ("stop", False),
        ],
    )
    def test_output_cap_suffix_matching_and_non_cap_stops(
        self, monkeypatch, finish_reason, should_retry
    ):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="go")])
        first = _output_cap_reply(
            response_metadata={"finish_reason": finish_reason, "model_name": "gemini-4-neon"},
            content="正文",
        )
        good = AIMessage(content="续写完成", response_metadata={"finish_reason": "STOP"})
        handler = _sync_handler(
            side_effect=[_resp(first), _resp(good)] if should_retry else [_resp(first)]
        )

        result = mw.wrap_model_call(request, handler)

        if should_retry:
            assert handler.call_count == 2
            assert result.result[-1] is good
        else:
            assert handler.call_count == 1
            assert result.result == [first]

    def test_responses_api_non_cap_incomplete_is_not_retried(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="go")])
        filtered = _output_cap_reply(
            response_metadata={
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
                "model_name": "gpt-5.4",
            },
            content="部分内容",
        )
        handler = _sync_handler(return_value=_resp(filtered))

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result == [filtered]

    def test_sanitizes_anthropic_and_gemini_tool_and_thinking_blocks_and_converts_cleanly(
        self, monkeypatch
    ):
        from langchain_anthropic.chat_models import _format_messages

        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)

        anthropic_capped = AIMessage(
            id="msg-anth-cap",
            content=[
                {"type": "thinking", "thinking": "partial thought", "signature": ""},
                {"type": "text", "text": "前半段说明"},
                {
                    "type": "tool_use",
                    "id": "toolu_trunc",
                    "name": "write_file",
                    "input": {"file_path": "a.py"},
                },
            ],
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "a.py"},
                    "id": "toolu_trunc",
                    "type": "tool_call",
                }
            ],
            invalid_tool_calls=[
                {
                    "name": "write_file",
                    "args": '{"file_path": "b.py"',
                    "id": "toolu_bad",
                    "error": "eof",
                    "type": "invalid_tool_call",
                }
            ],
            response_metadata={"stop_reason": "max_tokens", "model_name": "claude-sonnet-5-5"},
            usage_metadata={"input_tokens": 100, "output_tokens": 32000, "total_tokens": 32100},
        )
        good_anth = AIMessage(content="ok", response_metadata={"stop_reason": "end_turn"})
        req_anth = _make_request([HumanMessage(content="hello")])
        handler_anth = _sync_handler(side_effect=[_resp(anthropic_capped), _resp(good_anth)])

        mw.wrap_model_call(req_anth, handler_anth)

        sanitized_anth = req_anth.messages[1]
        assert isinstance(sanitized_anth, AIMessage)
        assert sanitized_anth.content == "前半段说明"
        assert sanitized_anth.id == "msg-anth-cap"
        assert sanitized_anth.response_metadata == anthropic_capped.response_metadata
        assert sanitized_anth.usage_metadata == anthropic_capped.usage_metadata
        assert sanitized_anth.tool_calls == []
        assert sanitized_anth.invalid_tool_calls == []
        assert req_anth.messages[2].content == _OUTPUT_CAP_HINT

        _, formatted = _format_messages(req_anth.messages, model="claude-sonnet-5-5")
        blocks = [
            b
            for msg in formatted
            if isinstance(msg["content"], list)
            for b in msg["content"]
        ]
        assert not any(b.get("type") in {"tool_use", "thinking"} for b in blocks)

        gemini_capped = AIMessage(
            id="msg-gem-cap",
            content=[
                {"type": "thinking", "thinking": "gem thought"},
                {"type": "text", "text": "Gemini 前半段"},
                {"type": "functionCall", "name": "write_file", "args": {"file_path": "a.py"}},
                {"functionCall": {"name": "bash", "args": {"command": "ls"}}},
            ],
            tool_calls=[],
            response_metadata={"finish_reason": "MAX_TOKENS", "model_name": "gemini-4-neon"},
            usage_metadata={"input_tokens": 200, "output_tokens": 65536, "total_tokens": 65736},
        )
        req_gem = _make_request([HumanMessage(content="hello")])
        handler_gem = _sync_handler(side_effect=[_resp(gemini_capped), _resp(good_anth)])

        mw.wrap_model_call(req_gem, handler_gem)

        sanitized_gem = req_gem.messages[1]
        assert isinstance(sanitized_gem, AIMessage)
        assert sanitized_gem.content == "Gemini 前半段"
        assert sanitized_gem.id == "msg-gem-cap"
        assert sanitized_gem.tool_calls == []
        assert sanitized_gem.invalid_tool_calls == []
        assert req_gem.messages[2].content == _OUTPUT_CAP_HINT

    def test_pure_text_truncation_continuation_keeps_prefix_hint_and_reply(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="写一份长分析")])
        capped = _output_cap_reply(
            msg_id="msg-text-1",
            content="第一部分：行业格局……",
            response_metadata={"finish_reason": "STOPMAX_TOKENS", "model_name": "gemini-4-neon"},
        )
        continuation = AIMessage(
            content="第二部分：估值结论。",
            response_metadata={"finish_reason": "STOP", "model_name": "gemini-4-neon"},
        )
        handler = _sync_handler(side_effect=[_resp(capped), _resp(continuation)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert len(result.result) == 3
        assert isinstance(result.result[0], AIMessage)
        assert result.result[0].content == "第一部分：行业格局……"
        assert result.result[0].id == "msg-text-1"
        assert isinstance(result.result[1], HumanMessage)
        assert result.result[1].content == _OUTPUT_CAP_CONTINUE_HINT
        assert result.result[2] is continuation

    def test_output_cap_exhausted_keeps_prefix_and_appends_notice(self, monkeypatch, caplog):
        _user_turn(monkeypatch, _config(thread_id="13e2d7b5"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="build model")])
        first = _output_cap_reply(msg_id="msg-cap-1", content="第一段已展示内容")
        second = _output_cap_reply(msg_id="msg-cap-2", content="第二段已展示内容")
        last = _output_cap_reply(
            msg_id="msg-cap-3",
            content="第三段已展示内容",
            usage_metadata={"input_tokens": 5000, "output_tokens": 65536, "total_tokens": 70536},
        )
        handler = _sync_handler(side_effect=[_resp(first), _resp(second), _resp(last)])

        with caplog.at_level(
            logging.WARNING,
            logger="ptc_agent.agent.middleware.tool.empty_call_retry",
        ):
            result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        assert len(result.result) == 7
        assert [m.content for m in result.result] == [
            "第一段已展示内容",
            _OUTPUT_CAP_CONTINUE_HINT,
            "第二段已展示内容",
            _OUTPUT_CAP_CONTINUE_HINT,
            "第三段已展示内容",
            _OUTPUT_CAP_CONTINUE_HINT,
            _OUTPUT_CAP_NOTICE,
        ]
        assert [result.result[0].id, result.result[2].id, result.result[4].id] == [
            "msg-cap-1",
            "msg-cap-2",
            "msg-cap-3",
        ]
        notice = result.result[-1]
        assert "模型回复多次超出输出上限而被截断，任务未完成" in notice.content
        assert "您" not in notice.content
        assert notice.usage_metadata == last.usage_metadata
        assert notice.response_metadata.get("finish_reason") == "MAX_TOKENS"
        assert notice.id != last.id
        assert any("13e2d7b5" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_async_output_cap_exhausted_keeps_prefix_and_appends_notice(self, monkeypatch):
        _user_turn(monkeypatch, _config(thread_id="13e2d7b5"))
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        request = _make_request([HumanMessage(content="build model")])
        first = _output_cap_reply(msg_id="msg-async-1", content="第一段")
        second = _output_cap_reply(msg_id="msg-async-2", content="第二段")
        handler = _async_handler(side_effect=[_resp(first), _resp(second)])

        result = await mw.awrap_model_call(request, handler)

        assert handler.await_count == 2
        assert [m.content for m in result.result] == [
            "第一段",
            _OUTPUT_CAP_CONTINUE_HINT,
            "第二段",
            _OUTPUT_CAP_CONTINUE_HINT,
            _OUTPUT_CAP_NOTICE,
        ]

    def test_cross_kind_output_cap_then_empty_replies_uses_output_cap_notice(self, monkeypatch):
        _user_turn(monkeypatch, _config(thread_id="13e2d7b5"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="build model")])
        capped = _output_cap_reply(msg_id="msg-cap-first", content="截断前正文")
        empty1 = _thinking_only_reply()
        empty2 = _thinking_only_reply()
        handler = _sync_handler(side_effect=[_resp(capped), _resp(empty1), _resp(empty2)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        assert [m.content for m in result.result] == [
            "截断前正文",
            _OUTPUT_CAP_CONTINUE_HINT,
            empty1.content,
            _EMPTY_REPLY_HINT,
            _OUTPUT_CAP_NOTICE,
        ]

    def test_multiple_output_caps_append_each_prefix_and_expose_all_to_next_call(self, monkeypatch):
        _user_turn(monkeypatch, _config(thread_id="13e2d7b5"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="写长报告")])
        part_a = _output_cap_reply(msg_id="msg-a", content="正文 A")
        part_b = _output_cap_reply(msg_id="msg-b", content="正文 B")
        part_c = AIMessage(content="正文 C", response_metadata={"finish_reason": "STOP"})

        seen_messages: list[list] = []

        def record_and_reply(req):
            seen_messages.append(list(req.messages))
            return _resp([part_a, part_b, part_c][len(seen_messages) - 1])

        handler = _sync_handler(side_effect=record_and_reply)

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        assert [m.content for m in result.result] == [
            "正文 A",
            _OUTPUT_CAP_CONTINUE_HINT,
            "正文 B",
            _OUTPUT_CAP_CONTINUE_HINT,
            "正文 C",
        ]
        third_call_contents = [m.content for m in seen_messages[2]]
        assert "正文 A" in third_call_contents
        assert "正文 B" in third_call_contents

    def test_empty_reply_then_output_cap_keeps_all_injected(self, monkeypatch):
        _user_turn(monkeypatch, _config(thread_id="13e2d7b5"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="写长报告")])
        empty = _thinking_only_reply()
        part_a = _output_cap_reply(msg_id="msg-a", content="正文 A")
        part_c = AIMessage(content="正文 C", response_metadata={"finish_reason": "STOP"})
        handler = _sync_handler(side_effect=[_resp(empty), _resp(part_a), _resp(part_c)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        assert [m.content for m in result.result] == [
            empty.content,
            _EMPTY_REPLY_HINT,
            "正文 A",
            _OUTPUT_CAP_CONTINUE_HINT,
            "正文 C",
        ]

    def test_sanitize_strips_image_blocks_without_placeholder(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="analyze")])
        capped = AIMessage(
            id="msg-img-cap",
            content=[
                {"type": "text", "text": "图表说明"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
            response_metadata={"finish_reason": "MAX_TOKENS", "model_name": "gemini-4-neon"},
        )
        good = AIMessage(content="完成", response_metadata={"finish_reason": "STOP"})
        handler = _sync_handler(side_effect=[_resp(capped), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert result.result[0].content == "图表说明"
        assert "[image]" not in result.result[0].content

    @pytest.mark.parametrize(
        "config",
        [
            _config(origin="automation"),
            _config(origin="system", wake_turn=True),
            _config(origin="user", checkpoint_ns="task:sub-1|model:1"),
        ],
        ids=["automation", "wake-turn", "subagent"],
    )
    def test_output_cap_also_retries_automation_wake_and_subagent_turns(self, monkeypatch, config):
        _user_turn(monkeypatch, config)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="run")])
        good = AIMessage(content="ok", response_metadata={"finish_reason": "STOP"}, tool_calls=[])
        handler = _sync_handler(side_effect=[_resp(_output_cap_reply()), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert request.messages[-1].content == _OUTPUT_CAP_CONTINUE_HINT
        assert result.result == [request.messages[-2], request.messages[-1], good]

    def test_output_cap_beats_empty_reply_and_omits_ai_when_visible_text_empty(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="run")])
        truncated = _output_cap_reply(
            content="",
            invalid_tool_calls=[
                {"name": "write_file", "args": '{"file_path": "x.py"', "id": "1", "error": "eof", "type": "invalid_tool_call"}
            ],
        )
        good = AIMessage(content="done", response_metadata={"finish_reason": "STOP"}, tool_calls=[])
        handler = _sync_handler(side_effect=[_resp(truncated), _resp(good)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert len(request.messages) == 2
        assert isinstance(request.messages[1], HumanMessage)
        assert request.messages[1].content == _OUTPUT_CAP_HINT
        assert result.result == [request.messages[1], good]

    def test_compaction_call_hitting_output_cap_is_not_retried(self, monkeypatch):
        _user_turn(monkeypatch, _config(tags=[TAG_NOSTREAM]))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="summarize")], model_settings={})
        capped = _output_cap_reply()
        handler = _sync_handler(return_value=_resp(capped))

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result == [capped]
        assert len(request.messages) == 1

    def test_tool_calls_then_empty_reply_then_output_cap_keeps_each_hint_paired(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=3)
        request = _make_request([HumanMessage(content="run")])
        broken_call = AIMessage(
            content=[dict(_CLAUDE_THINKING)],
            response_metadata={"stop_reason": "tool_use", "model_name": "claude-sonnet-5-5"},
            tool_calls=[],
        )
        empty = _thinking_only_reply()
        capped = _output_cap_reply(msg_id="msg-cap-3", content="第三步写了一半")
        good = AIMessage(content="全部完成", response_metadata={"finish_reason": "STOP"})
        handler = _sync_handler(
            side_effect=[_resp(broken_call), _resp(empty), _resp(capped), _resp(good)]
        )

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 4
        assert broken_call not in request.messages
        assert [m.content for m in result.result] == [
            empty.content,
            _EMPTY_REPLY_HINT,
            "第三步写了一半",
            _OUTPUT_CAP_CONTINUE_HINT,
            "全部完成",
        ]

    def test_provider_compaction_request_hitting_output_cap_is_not_retried(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request(
            [HumanMessage(content="summarize")],
            model_settings={
                "extra_body": {
                    "compaction": {"type": "summarize", "instructions": "summarize"}
                }
            },
        )
        capped = _output_cap_reply()
        handler = _sync_handler(return_value=_resp(capped))

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result == [capped]
        assert len(request.messages) == 1


class TestMalformedCallRetry:
    @pytest.mark.parametrize(
        "finish_reason",
        [
            "MALFORMED_FUNCTION_CALL",
            "STOPMALFORMED_FUNCTION_CALL",
            "UNEXPECTED_TOOL_CALL",
        ],
    )
    def test_malformed_call_retries_and_sanitizes(self, monkeypatch, caplog, finish_reason):
        _user_turn(monkeypatch, _config(origin="automation", thread_id="a30afade"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = _make_request([HumanMessage(content="生成投资日报")])
        malformed = AIMessage(
            id="msg-malformed-1",
            content=[
                {"type": "thinking", "thinking": "thought", "signature": ""},
                {"type": "text", "text": "正在保存信号裁决……"},
            ],
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"file_path": "report.md", "content": "bad"},
                    "id": "call-bad",
                    "type": "tool_call",
                }
            ],
            response_metadata={"finish_reason": finish_reason, "model_name": "gemini-4-carbon-high-oauth"},
            usage_metadata={"input_tokens": 1000, "output_tokens": 26690, "total_tokens": 27690},
        )
        good = AIMessage(
            content="日报已写入。",
            response_metadata={"finish_reason": "STOP", "model_name": "gemini-4-carbon-high-oauth"},
        )
        handler = _sync_handler(side_effect=[_resp(malformed), _resp(good)])

        with caplog.at_level(
            logging.WARNING,
            logger="ptc_agent.agent.middleware.tool.empty_call_retry",
        ):
            result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert [m.content for m in result.result] == [
            "正在保存信号裁决……",
            _MALFORMED_CALL_HINT,
            "日报已写入。",
        ]
        sanitized = result.result[0]
        assert sanitized.id == "msg-malformed-1"
        assert sanitized.tool_calls == []
        assert sanitized.invalid_tool_calls == []
        assert any(
            finish_reason in r.message and "26690" in r.message and "(1/2)" in r.message
            for r in caplog.records
        )

    def test_malformed_call_exhausted_keeps_prefix_and_appends_notice(self, monkeypatch):
        _user_turn(monkeypatch, _config(origin="user", thread_id="a30afade"))
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        request = _make_request([HumanMessage(content="run")])
        first = AIMessage(
            id="m1",
            content="正在保存信号裁决……",
            response_metadata={"finish_reason": "MALFORMED_FUNCTION_CALL"},
        )
        second = AIMessage(
            id="m2",
            content="再次尝试……",
            response_metadata={"finish_reason": "MALFORMED_FUNCTION_CALL"},
        )
        handler = _sync_handler(side_effect=[_resp(first), _resp(second)])

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert [m.content for m in result.result] == [
            "正在保存信号裁决……",
            _MALFORMED_CALL_HINT,
            "再次尝试……",
            _MALFORMED_CALL_HINT,
            _MALFORMED_CALL_NOTICE,
        ]
        assert "模型多次生成的工具调用格式错误（均未执行），任务未完成" in result.result[-1].content
        assert "您" not in result.result[-1].content

    def test_priority_output_cap_beats_malformed_call_beats_empty_reply(self, monkeypatch):
        _user_turn(monkeypatch)
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        both = AIMessage(
            content="",
            response_metadata={"stop_reason": "max_tokens", "finish_reason": "MALFORMED_FUNCTION_CALL"},
        )
        good = AIMessage(content="ok", response_metadata={"finish_reason": "STOP"})
        req = _make_request([HumanMessage(content="go")])
        mw.wrap_model_call(req, _sync_handler(side_effect=[_resp(both), _resp(good)]))
        assert req.messages[-1].content == _OUTPUT_CAP_HINT

        only_malformed = AIMessage(
            content="",
            response_metadata={"finish_reason": "MALFORMED_FUNCTION_CALL"},
        )
        req2 = _make_request([HumanMessage(content="go")])
        mw.wrap_model_call(req2, _sync_handler(side_effect=[_resp(only_malformed), _resp(good)]))
        assert req2.messages[-1].content == _MALFORMED_CALL_HINT

    @pytest.mark.parametrize(
        ("stop_meta", "expected_notice"),
        [
            ({"finish_reason": "MAX_TOKENS", "model_name": "gemini-4-neon"}, _OUTPUT_CAP_NOTICE),
            ({"finish_reason": "MALFORMED_FUNCTION_CALL", "model_name": "gemini-4-carbon-high-oauth"}, _MALFORMED_CALL_NOTICE),
        ],
        ids=["output-cap", "malformed-call"],
    )
    def test_automation_exhausted_commits_notice_and_marks_turn_error(
        self, monkeypatch, stop_meta, expected_notice
    ):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langgraph.checkpoint.memory import InMemorySaver
        from src.server.services.automation_outcomes import Outcome, ledger_outcome
        from src.server.services.runs.stream_errors import failure_kind_metadata

        _user_turn(monkeypatch, _config(origin="automation", thread_id="a30afade"))
        replies = [
            AIMessage(content="正文片段", response_metadata=dict(stop_meta), id="r-1"),
            AIMessage(content="二次尝试", response_metadata=dict(stop_meta), id="r-2"),
        ]
        saver = InMemorySaver()
        agent = create_agent(
            model=GenericFakeChatModel(messages=iter(replies)),
            tools=[],
            middleware=[EmptyToolCallRetryMiddleware(max_retries=1)],
            checkpointer=saver,
        )
        cfg = {"configurable": {"thread_id": "a30afade"}}

        with pytest.raises(UnfinishedReplyError) as exc_info:
            agent.invoke({"messages": [HumanMessage(content="run automation")]}, cfg)

        assert str(exc_info.value) == expected_notice
        assert failure_kind_metadata(exc_info.value) == {"error_kind": "upstream"}
        assert (
            ledger_outcome(
                {
                    "status": "error",
                    "metadata": failure_kind_metadata(exc_info.value),
                    "errors": [str(exc_info.value)],
                }
            )
            == Outcome.FAILED
        )
        saved = agent.get_state(cfg).values["messages"]
        assert saved[-1].content == expected_notice
        assert saved[-1].additional_kwargs.get(_EXHAUSTED_FAILURE_KWARG) == {
            "kind": "output_cap" if expected_notice == _OUTPUT_CAP_NOTICE else "malformed_call",
            "fail_turn": True,
        }
        assert "正文片段" in [m.content for m in saved]

    def test_retry_from_checkpoint_with_fresh_middleware_still_raises_without_calling_model(self):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langgraph.checkpoint.memory import InMemorySaver
        from ptc_agent.agent.middleware.model_resilience import RESILIENCE_TRACE_ATTR
        from src.server.handlers.chat.request_prep import build_graph_config
        from src.server.models.chat import ChatRequest
        from src.server.services.runs.stream_errors import failure_kind_metadata

        saver = InMemorySaver()
        thread_id = "a30afade"
        initial_req = ChatRequest(
            workspace_id="ws-1",
            messages=[{"role": "user", "content": "run automation"}],
            origin={"type": "automation", "id": "auto-1"},
        )
        initial_cfg = build_graph_config(
            thread_id=thread_id,
            user_id="user-1",
            workspace_id="ws-1",
            timezone_str="UTC",
            token_callback=None,
            request=initial_req,
            effective_model="gemini-4-carbon-high-oauth",
            from_server=True,
        )
        assert initial_cfg["metadata"]["origin"] == "automation"

        agent1 = create_agent(
            model=GenericFakeChatModel(
                messages=iter([
                    AIMessage(
                        content="正文片段",
                        response_metadata={
                            "finish_reason": "MALFORMED_FUNCTION_CALL",
                            "model_name": "gemini-4-carbon-high-oauth",
                        },
                        id="r-1",
                    ),
                    AIMessage(
                        content="二次尝试",
                        response_metadata={
                            "finish_reason": "MALFORMED_FUNCTION_CALL",
                            "model_name": "gemini-4-carbon-high-oauth",
                        },
                        id="r-2",
                    ),
                ])
            ),
            tools=[],
            middleware=[EmptyToolCallRetryMiddleware(max_retries=1)],
            checkpointer=saver,
        )
        with pytest.raises(UnfinishedReplyError):
            agent1.invoke({"messages": [HumanMessage(content="run automation")]}, initial_cfg)

        # Simulate /retry route (messaging.py:2022 + request_prep.py:1005), where
        # ChatRequest omits origin so metadata.origin becomes "user".
        latest_ckpt = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        assert latest_ckpt is not None
        retry_req = ChatRequest(
            workspace_id="ws-1",
            messages=[],
            checkpoint_id=latest_ckpt.config["configurable"]["checkpoint_id"],
            subagents_enabled=None,
        )
        retry_cfg = build_graph_config(
            thread_id=thread_id,
            user_id="user-1",
            workspace_id="ws-1",
            timezone_str="UTC",
            token_callback=None,
            request=retry_req,
            effective_model="gemini-4-carbon-high-oauth",
            from_server=False,
        )
        assert retry_cfg["metadata"]["origin"] == "user"

        resumed_model_calls = 0

        class _NeverCalledModel(GenericFakeChatModel):
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                nonlocal resumed_model_calls
                resumed_model_calls += 1
                raise AssertionError("resumed /retry after_agent must not invoke the model again")

        agent2 = create_agent(
            model=_NeverCalledModel(messages=iter([])),
            tools=[],
            middleware=[EmptyToolCallRetryMiddleware(max_retries=1)],
            checkpointer=saver,
        )
        with pytest.raises(UnfinishedReplyError) as exc_info:
            agent2.invoke(None, retry_cfg)

        assert resumed_model_calls == 0
        assert str(exc_info.value) == _MALFORMED_CALL_NOTICE
        assert failure_kind_metadata(exc_info.value) == {"error_kind": "upstream"}
        assert getattr(exc_info.value, RESILIENCE_TRACE_ATTR, None) == {
            "model": "gemini-4-carbon-high-oauth"
        }

    def test_legacy_string_exhausted_marker_falls_back_to_current_origin(self, monkeypatch):
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        legacy_msg = AIMessage(
            content=_OUTPUT_CAP_NOTICE,
            additional_kwargs={_EXHAUSTED_FAILURE_KWARG: "output_cap"},
            response_metadata={"model_name": "gemini-4-neon"},
        )
        _user_turn(monkeypatch, _config(origin="automation"))
        with pytest.raises(UnfinishedReplyError):
            mw.after_agent({"messages": [HumanMessage(content="run"), legacy_msg]})

        _user_turn(monkeypatch, _config(origin="user"))
        assert mw.after_agent({"messages": [HumanMessage(content="hi"), legacy_msg]}) is None

    def test_user_and_subagent_exhausted_do_not_raise_error(self, monkeypatch):
        from langchain.agents import create_agent
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langgraph.checkpoint.memory import InMemorySaver

        for cfg in (_config(origin="user"), _config(origin="automation", checkpoint_ns="task:sub-1|model:1")):
            _user_turn(monkeypatch, cfg)
            replies = [
                AIMessage(content="片段", response_metadata={"finish_reason": "MALFORMED_FUNCTION_CALL"}, id="u-1"),
                AIMessage(content="片段2", response_metadata={"finish_reason": "MALFORMED_FUNCTION_CALL"}, id="u-2"),
            ]
            saver = InMemorySaver()
            agent = create_agent(
                model=GenericFakeChatModel(messages=iter(replies)),
                tools=[],
                middleware=[EmptyToolCallRetryMiddleware(max_retries=1)],
                checkpointer=saver,
            )
            out = agent.invoke(
                {"messages": [HumanMessage(content="go")]},
                {"configurable": {"thread_id": "t-ok"}},
            )
            assert out["messages"][-1].content == _MALFORMED_CALL_NOTICE
