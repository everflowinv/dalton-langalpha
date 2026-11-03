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
    _REMEDIATION_HINT,
    EmptyToolCallRetryMiddleware,
)
from ptc_agent.agent.transcript.classify import human_kind


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

        # 1 initial + 2 retries, then the last reply is replaced outright.
        assert handler.call_count == 3
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
        handler = MagicMock(
            side_effect=[_resp(broken_call), _resp(_thinking_only_reply()), _resp(good)]
        )

        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        hints = [
            m
            for m in request.messages
            if isinstance(m, HumanMessage)
            and (m.additional_kwargs or {}).get("lc_source") == "empty_call_retry"
        ]
        # One hint, reworded for the shape failing now; the first broken
        # reply stays ahead of it.
        assert len(hints) == 1
        assert hints[0].content == _EMPTY_REPLY_HINT
        assert request.messages[-2] is broken_call
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
        assert len(retrying) == 2
        assert not any("(3/2)" in r.message for r in caplog.records)
