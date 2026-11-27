"""Unit tests for HF2 Round 2: Antigravity multimodal delivery, parallel tool ordering,
cross-family signature isolation, safety-refusal notice scope, and live probe script.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import create_autospec

import pypdf
import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_config
from PIL import Image

from ptc_agent.agent.middleware.file_operations.multimodal import build_content_blocks
from ptc_agent.agent.middleware.model_resilience import ModelResilienceMiddleware
from ptc_agent.agent.middleware.tool import empty_call_retry
from ptc_agent.agent.middleware.tool.empty_call_retry import (
    _EMPTY_REPLY_NOTICE,
    _OUTPUT_CAP_NOTICE,
    EmptyToolCallRetryMiddleware,
)
from scripts.dalton.ops.prod import probe_antigravity_multimodal as probe_mod
from scripts.dalton.ops.prod.probe_antigravity_multimodal import (
    _answer_matches,
    _answer_matches_all,
    _build_probe_messages,
    _build_readonly_client,
    make_number_pdf,
    make_number_png,
)
from src.dalton.llm.providers.antigravity.client import (
    _CALL_SIGNATURES_KEY,
    _SIGNATURE_FAMILY_KEY,
    _SKIP_SIGNATURE,
    _THOUGHT_SIGNATURES_KEY,
    ChatAntigravity,
    _messages_to_gemini,
)
from src.dalton.middleware.refusal_fallback import (
    REFUSAL_NOTICE_CHANNEL,
    REFUSAL_NOTICE_WEB,
    refusal_notice_text,
    supports_slash_new,
)
from src.llms.llm import LLM as LLMFactory
from src.server.database import oauth_tokens as oauth_tokens_db

IMG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nHF2IMAGEPAYLOAD" * 3).decode("ascii")
IMG2_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nHF2SECONDIMAGE!!" * 3).decode("ascii")
PDF_B64 = base64.b64encode(b"%PDF-1.4\nHF2PDFPAYLOAD\n%%EOF" * 3).decode("ascii")


def _config(
    *,
    origin: str = "user",
    platform: str | None = "feishu",
    user_id: str = "u-1",
    thread_id: str = "t-hf2",
) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
            "user_id": user_id,
            "platform": platform,
            "wake_turn": False,
            "checkpoint_ns": "",
        },
        "metadata": {"origin": origin, "platform": platform},
    }


def _patch_config(monkeypatch, cfg: dict) -> None:
    getter = create_autospec(get_config, return_value=cfg)
    monkeypatch.setattr(empty_call_retry, "get_config", getter)


def _msg_with_reason(
    meta_key: str,
    reason: str,
    *,
    model: str = "claude-opus-5-5-medium-oauth",
    content: str = "",
) -> AIMessage:
    return AIMessage(
        content=content,
        response_metadata={meta_key: reason, "model_name": model},
        tool_calls=[],
    )


def _resp(msg: AIMessage) -> ModelResponse:
    return ModelResponse(result=[msg])


def _fake_model(model_name: str, catalog_key: str) -> BaseChatModel:
    model = create_autospec(BaseChatModel, instance=True)
    model.model = model_name
    model.metadata = {
        "manifest_model": catalog_key,
        "billing_type": "oauth",
        "pricing_provider": "antigravity-oauth",
    }
    model.profile = {"max_input_tokens": 200_000}
    return model


class _FakeModelRequest:
    """Minimal ModelRequest-compatible carrier supporting `.override(...)`."""

    def __init__(self, *, model, messages, tools=None, model_settings=None, system_message=None):
        self.model = model
        self.messages = list(messages)
        self.tools = list(tools) if tools is not None else []
        self.model_settings = dict(model_settings) if model_settings is not None else {}
        self.system_message = system_message
        self.state = {"messages": list(messages)}

    def override(self, **kwargs):
        return _FakeModelRequest(
            model=kwargs.get("model", self.model),
            messages=kwargs.get("messages", self.messages),
            tools=kwargs.get("tools", self.tools),
            model_settings=kwargs.get("model_settings", self.model_settings),
            system_message=kwargs.get("system_message", self.system_message),
        )


# ---------------------------------------------------------------------------
# Antigravity multimodal conversion & parallel tool ordering
# ---------------------------------------------------------------------------


class TestAntigravityMultimodal:
    @pytest.mark.parametrize("gemini_target", [True, False], ids=["gemini", "claude-via-antigravity"])
    def test_tool_message_image_placed_after_function_response_in_same_user_turn(
        self, gemini_target: bool
    ) -> None:
        blocks = build_content_blocks(IMG_B64, "/work/short_interest.png", "image/png")
        messages = [
            HumanMessage(content="Read the screenshot"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call_img", "name": "Read", "args": {"file_path": "/work/short_interest.png"}}
                ],
            ),
            ToolMessage(content=blocks, tool_call_id="call_img", name="Read"),
        ]

        _, contents = _messages_to_gemini(messages, gemini_target=gemini_target)

        tool_turn = contents[2]
        assert tool_turn["role"] == "user"
        assert len(tool_turn["parts"]) == 2
        assert "functionResponse" in tool_turn["parts"][0]
        assert tool_turn["parts"][0]["functionResponse"]["name"] == "Read"
        assert tool_turn["parts"][0]["functionResponse"]["id"] == "call_img"
        assert tool_turn["parts"][0]["functionResponse"]["response"] == {"result": "[Viewing image]"}
        assert tool_turn["parts"][1] == {
            "inlineData": {"mimeType": "image/png", "data": IMG_B64}
        }

    @pytest.mark.parametrize("gemini_target", [True, False], ids=["gemini", "claude-via-antigravity"])
    def test_parallel_tool_results_with_images_grouped_into_single_user_turn(
        self, gemini_target: bool
    ) -> None:
        blocks_1 = build_content_blocks(IMG_B64, "/work/img1.png", "image/png")
        blocks_2 = build_content_blocks(IMG2_B64, "/work/img2.png", "image/png")
        messages = [
            HumanMessage(content="Read both screenshots"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call_1", "name": "Read", "args": {"file_path": "/work/img1.png"}},
                    {"id": "call_2", "name": "Read", "args": {"file_path": "/work/img2.png"}},
                ],
            ),
            ToolMessage(content=blocks_1, tool_call_id="call_1", name="Read"),
            ToolMessage(content=blocks_2, tool_call_id="call_2", name="Read"),
        ]

        _, contents = _messages_to_gemini(messages, gemini_target=gemini_target)

        # Human -> Model -> ONE single user turn holding [FR1, FR2, img1, img2]
        assert len(contents) == 3
        tool_turn = contents[2]
        assert tool_turn["role"] == "user"
        parts = tool_turn["parts"]
        assert len(parts) == 4
        assert parts[0]["functionResponse"]["id"] == "call_1"
        assert parts[0]["functionResponse"]["name"] == "Read"
        assert parts[1]["functionResponse"]["id"] == "call_2"
        assert parts[1]["functionResponse"]["name"] == "Read"
        assert parts[2] == {"inlineData": {"mimeType": "image/png", "data": IMG_B64}}
        assert parts[3] == {"inlineData": {"mimeType": "image/png", "data": IMG2_B64}}

    def test_parallel_tool_results_out_of_order_sorted_by_call_order(self) -> None:
        blocks_1 = build_content_blocks(IMG_B64, "/work/img1.png", "image/png")
        blocks_2 = build_content_blocks(IMG2_B64, "/work/img2.png", "image/png")
        messages = [
            HumanMessage(content="Read both"),
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "call_1", "name": "Read", "args": {"file_path": "/work/img1.png"}},
                    {"id": "call_2", "name": "Read", "args": {"file_path": "/work/img2.png"}},
                ],
            ),
            # Arrive reversed relative to AIMessage.tool_calls
            ToolMessage(content=blocks_2, tool_call_id="call_2", name="Read"),
            ToolMessage(content=blocks_1, tool_call_id="call_1", name="Read"),
        ]

        _, contents = _messages_to_gemini(messages, gemini_target=True)

        assert len(contents) == 3
        parts = contents[2]["parts"]
        assert [p["functionResponse"]["id"] for p in parts[:2]] == ["call_1", "call_2"]
        assert [p["inlineData"]["data"] for p in parts[2:]] == [IMG_B64, IMG2_B64]

    def test_tool_message_pdf_sends_inline_data_without_internal_pages_field(self) -> None:
        blocks = build_content_blocks(
            PDF_B64, "/work/filing.pdf", "application/pdf", pages=12, marker="[Pages 1-12 of 20.]"
        )
        messages = [
            HumanMessage(content="Inspect filing.pdf"),
            AIMessage(
                content="",
                tool_calls=[{"id": "call_pdf", "name": "Read", "args": {"file_path": "/work/filing.pdf"}}],
            ),
            ToolMessage(content=blocks, tool_call_id="call_pdf", name="Read"),
        ]

        client = ChatAntigravity(model="gemini-4-argon-medium", api_key="fake")
        payload = client._build_payload(messages)
        tool_turn = payload["request"]["contents"][2]

        assert tool_turn["role"] == "user"
        assert len(tool_turn["parts"]) == 2
        assert "functionResponse" in tool_turn["parts"][0]
        assert "[Viewing PDF: filing.pdf]" in tool_turn["parts"][0]["functionResponse"]["response"]["result"]
        assert tool_turn["parts"][1] == {
            "inlineData": {"mimeType": "application/pdf", "data": PDF_B64}
        }
        assert "pages" not in json.dumps(tool_turn["parts"][1])
        assert "filename" not in json.dumps(tool_turn["parts"][1])

    def test_human_message_pdf_sends_inline_data_without_internal_pages_field(self) -> None:
        blocks = build_content_blocks(PDF_B64, "/uploads/report.pdf", "application/pdf", pages=5)
        messages = [HumanMessage(content=blocks)]

        _, contents = _messages_to_gemini(messages, gemini_target=True)

        assert len(contents) == 1
        assert contents[0]["role"] == "user"
        assert contents[0]["parts"][0] == {"text": "[Viewing PDF: report.pdf]"}
        assert contents[0]["parts"][1] == {
            "inlineData": {"mimeType": "application/pdf", "data": PDF_B64}
        }
        assert "pages" not in contents[0]["parts"][1]["inlineData"]
        assert "pages" not in contents[0]["parts"][1]

    def test_non_data_url_image_logs_warning_and_emits_placeholder(self, caplog) -> None:
        messages = [
            HumanMessage(
                content=[
                    {"type": "text", "text": "Look at this"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}},
                ]
            ),
            AIMessage(
                content="",
                tool_calls=[{"id": "c1", "name": "Read", "args": {"file_path": "https://example.com/a.png"}}],
            ),
            ToolMessage(
                content=[
                    {"type": "text", "text": "[Viewing image]"},
                    {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                    {"type": "audio", "filename": "call.mp3"},
                ],
                tool_call_id="c1",
                name="Read",
            ),
        ]

        with caplog.at_level(logging.WARNING, logger="src.dalton.llm.providers.antigravity.client"):
            _, contents = _messages_to_gemini(messages, gemini_target=True)

        assert any("Non-data-URL" in r.message for r in caplog.records)
        assert any("Unsupported content block" in r.message for r in caplog.records)

        user_parts = contents[0]["parts"]
        assert user_parts == [
            {"text": "Look at this"},
            {"text": "[附件未能送达当前模型：image_url]"},
        ]

        tool_parts = contents[2]["parts"]
        assert len(tool_parts) == 1
        tool_text = tool_parts[0]["functionResponse"]["response"]["result"]
        assert "[附件未能送达当前模型：image_url]" in tool_text
        assert "[附件未能送达当前模型：call.mp3]" in tool_text


# ---------------------------------------------------------------------------
# Cross-family thought signature isolation within Antigravity
# ---------------------------------------------------------------------------


class TestAntigravityCrossFamilySignatures:
    def test_claude_signature_not_replayed_to_gemini_keeps_tool_ids_and_uses_skip_signature(
        self,
    ) -> None:
        claude_client = ChatAntigravity(model="claude-opus-5-5-medium", api_key="fake")
        chunks = claude_client._chunks_from_frame(
            {
                "response": {
                    "modelVersion": "claude-opus-5-5-medium",
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {"name": "Read", "args": {"file_path": "/a"}, "id": "call_c1"},
                                        "thoughtSignature": "claude-sig-1",
                                    },
                                    {
                                        "functionCall": {"name": "Read", "args": {"file_path": "/b"}, "id": "call_c2"},
                                        "thoughtSignature": "claude-sig-2",
                                    },
                                ]
                            },
                            "finishReason": "STOP",
                        }
                    ],
                }
            }
        )
        claude_msg = claude_client._final_message(chunks)
        assert claude_msg.additional_kwargs[_SIGNATURE_FAMILY_KEY] == "claude"
        assert claude_msg.additional_kwargs[_CALL_SIGNATURES_KEY] == {
            "call_c1": "claude-sig-1",
            "call_c2": "claude-sig-2",
        }

        # Now switch the thread to Antigravity Gemini and replay history.
        gemini_client = ChatAntigravity(model="gemini-4-argon-high", api_key="fake")
        payload = gemini_client._build_payload(
            [
                HumanMessage(content="inspect files"),
                claude_msg,
                ToolMessage(content="ok1", tool_call_id="call_c1", name="Read"),
                ToolMessage(content="ok2", tool_call_id="call_c2", name="Read"),
            ]
        )

        model_parts = payload["request"]["contents"][1]["parts"]
        fc_parts = [p for p in model_parts if "functionCall" in p]
        assert [p["functionCall"]["id"] for p in fc_parts] == ["call_c1", "call_c2"]
        assert [p["thoughtSignature"] for p in fc_parts] == [_SKIP_SIGNATURE, _SKIP_SIGNATURE]
        assert "claude-sig-1" not in json.dumps(payload)
        assert "claude-sig-2" not in json.dumps(payload)

    def test_legacy_claude_model_version_signature_not_replayed_to_gemini(self) -> None:
        # Message from before _SIGNATURE_FAMILY_KEY existed, identified only by response_metadata.
        legacy_claude_msg = AIMessage(
            content="",
            tool_calls=[{"name": "Read", "args": {"file_path": "/a"}, "id": "call_legacy"}],
            additional_kwargs={
                _THOUGHT_SIGNATURES_KEY: ["claude-legacy-sig"],
                _CALL_SIGNATURES_KEY: {"call_legacy": "claude-legacy-sig"},
            },
            response_metadata={"model_version": "claude-sonnet-5-5-high"},
        )
        gemini_client = ChatAntigravity(model="gemini-4-argon-medium", api_key="fake")
        payload = gemini_client._build_payload(
            [
                HumanMessage(content="go"),
                legacy_claude_msg,
                ToolMessage(content="done", tool_call_id="call_legacy", name="Read"),
            ]
        )

        fc_part = payload["request"]["contents"][1]["parts"][0]
        assert fc_part["functionCall"]["id"] == "call_legacy"
        assert fc_part["thoughtSignature"] == _SKIP_SIGNATURE
        assert "claude-legacy-sig" not in json.dumps(payload)

    def test_gemini_signature_not_replayed_to_claude_keeps_tool_ids(self) -> None:
        gemini_msg = AIMessage(
            content="",
            tool_calls=[{"name": "Read", "args": {"file_path": "/a"}, "id": "call_g1"}],
            additional_kwargs={
                _SIGNATURE_FAMILY_KEY: "gemini",
                _THOUGHT_SIGNATURES_KEY: ["gemini-sig-text", "gemini-sig-call"],
                _CALL_SIGNATURES_KEY: {"call_g1": "gemini-sig-call"},
            },
            response_metadata={"model_version": "gemini-4-argon-high"},
        )
        claude_client = ChatAntigravity(model="claude-sonnet-5-5-medium", api_key="fake")
        payload = claude_client._build_payload(
            [
                HumanMessage(content="go"),
                gemini_msg,
                ToolMessage(content="done", tool_call_id="call_g1", name="Read"),
            ]
        )

        model_parts = payload["request"]["contents"][1]["parts"]
        assert len(model_parts) == 1
        assert model_parts[0]["functionCall"]["id"] == "call_g1"
        assert "thoughtSignature" not in model_parts[0]
        assert "gemini-sig" not in json.dumps(payload)

    def test_same_family_antigravity_replays_signatures(self) -> None:
        gemini_msg = AIMessage(
            content="",
            tool_calls=[{"name": "Read", "args": {"file_path": "/a"}, "id": "call_g1"}],
            additional_kwargs={
                _SIGNATURE_FAMILY_KEY: "gemini",
                _THOUGHT_SIGNATURES_KEY: ["gemini-sig-call"],
                _CALL_SIGNATURES_KEY: {"call_g1": "gemini-sig-call"},
            },
        )
        gemini_client = ChatAntigravity(model="gemini-4-carbon-high", api_key="fake")
        payload = gemini_client._build_payload([HumanMessage(content="go"), gemini_msg])
        assert payload["request"]["contents"][1]["parts"][0]["thoughtSignature"] == "gemini-sig-call"

        claude_msg = AIMessage(
            content="",
            tool_calls=[{"name": "Read", "args": {"file_path": "/a"}, "id": "call_c1"}],
            additional_kwargs={
                _SIGNATURE_FAMILY_KEY: "claude",
                _THOUGHT_SIGNATURES_KEY: ["claude-sig-call"],
                _CALL_SIGNATURES_KEY: {"call_c1": "claude-sig-call"},
            },
        )
        claude_client = ChatAntigravity(model="claude-sonnet-5-5-high", api_key="fake")
        payload_c = claude_client._build_payload([HumanMessage(content="go"), claude_msg])
        assert payload_c["request"]["contents"][1]["parts"][0]["thoughtSignature"] == "claude-sig-call"


# ---------------------------------------------------------------------------
# Safety refusal notice scope & no backup model switch
# ---------------------------------------------------------------------------


class TestRefusalNoticeAndScope:
    def test_anthropic_refusal_emits_channel_notice_with_slash_new(self, monkeypatch) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = SimpleNamespace(messages=[HumanMessage(content="how to cook rice?")])

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _handler_sig,
            side_effect=[_resp(_msg_with_reason("stop_reason", "refusal"))] * 3,
        )
        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        assert len(result.result) == 1
        assert result.result[0].content == REFUSAL_NOTICE_CHANNEL
        assert "/new" in result.result[0].content

    @pytest.mark.asyncio
    async def test_anthropic_refusal_two_attempts_async_emits_notice(self, monkeypatch) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        request = SimpleNamespace(messages=[HumanMessage(content="how to cook rice?")])

        async def _ahandler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _ahandler_sig,
            side_effect=[_resp(_msg_with_reason("stop_reason", "refusal"))] * 2,
        )
        result = await mw.awrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[0].content == REFUSAL_NOTICE_CHANNEL

    @pytest.mark.parametrize("platform", [None, "", "web", "market_view", "market_view:AAPL"])
    def test_all_refusal_retries_emit_web_notice_without_slash_new(
        self, monkeypatch, platform: str | None
    ) -> None:
        _patch_config(monkeypatch, _config(platform=platform))
        assert supports_slash_new(_config(platform=platform)) is False
        assert refusal_notice_text(_config(platform=platform)) == REFUSAL_NOTICE_WEB

        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = SimpleNamespace(messages=[HumanMessage(content="how to cook rice?")])

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _handler_sig,
            side_effect=[_resp(_msg_with_reason("stop_reason", "refusal"))] * 3,
        )
        result = mw.wrap_model_call(request, handler)

        assert result.result[0].content == REFUSAL_NOTICE_WEB
        assert "/new" not in result.result[0].content
        assert "新建对话" in result.result[0].content

    @pytest.mark.parametrize(
        "finish_reason",
        ["SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII"],
    )
    def test_gemini_safety_finish_reasons_emit_refusal_notice(
        self, monkeypatch, finish_reason: str
    ) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        request = SimpleNamespace(messages=[HumanMessage(content="analyze this")])

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _handler_sig,
            side_effect=[
                _resp(_msg_with_reason("finish_reason", finish_reason, model="gemini-4-argon-medium"))
            ]
            * 2,
        )
        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[0].content == REFUSAL_NOTICE_CHANNEL

    @pytest.mark.parametrize(
        ("meta_key", "reason"),
        [
            ("finish_reason", "STOP"),
            ("stop_reason", "end_turn"),
            ("finish_reason", "content_filter"),
            ("finish_reason", "RECITATION"),
        ],
    )
    def test_non_refusal_empty_replies_keep_generic_empty_notice(
        self, monkeypatch, meta_key: str, reason: str
    ) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        request = SimpleNamespace(messages=[HumanMessage(content="hello")])

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _handler_sig,
            side_effect=[_resp(_msg_with_reason(meta_key, reason))] * 2,
        )
        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[0].content == _EMPTY_REPLY_NOTICE

    @pytest.mark.parametrize(
        ("meta_key", "reason"),
        [
            ("finish_reason", "MAX_TOKENS"),
            ("stop_reason", "max_tokens"),
            ("finish_reason", "length"),
        ],
    )
    def test_output_cap_empty_replies_keep_output_cap_notice(
        self, monkeypatch, meta_key: str, reason: str
    ) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=1)
        request = SimpleNamespace(messages=[HumanMessage(content="write a long report")])

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _handler_sig,
            side_effect=[_resp(_msg_with_reason(meta_key, reason))] * 2,
        )
        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 2
        assert result.result[-1].content == _OUTPUT_CAP_NOTICE

    def test_mixed_empty_and_refusal_keeps_generic_empty_notice(self, monkeypatch) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = SimpleNamespace(messages=[HumanMessage(content="hello")])
        plain_empty = _msg_with_reason("stop_reason", "end_turn")
        refusal = _msg_with_reason("stop_reason", "refusal")

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _handler_sig,
            side_effect=[_resp(plain_empty), _resp(refusal), _resp(refusal)],
        )
        result = mw.wrap_model_call(request, handler)

        assert result.result[0].content == _EMPTY_REPLY_NOTICE

    def test_non_user_turn_refusal_is_not_retried(self, monkeypatch) -> None:
        _patch_config(monkeypatch, _config(origin="system", platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = SimpleNamespace(messages=[HumanMessage(content="scheduled wake")])
        refusal = _msg_with_reason("stop_reason", "refusal")

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(_handler_sig, side_effect=[_resp(refusal)])
        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 1
        assert result.result[0] is refusal

    @pytest.mark.asyncio
    async def test_refusal_retries_primary_only_and_never_switches_to_fallback_model(
        self, monkeypatch
    ) -> None:
        _patch_config(monkeypatch, _config(platform="feishu", user_id="u-1", thread_id="t-no-switch"))

        primary_client = _fake_model("claude-opus-5-5-medium", "claude-opus-5-5-medium-oauth")
        fallback_client = _fake_model("gemini-4-argon-medium", "gemini-4-argon-medium-oauth")

        resilience = ModelResilienceMiddleware(
            primary_name="claude-opus-5-5-medium-oauth",
            primary_client=primary_client,
            fallbacks=[("gemini-4-argon-medium-oauth", fallback_client)],
            user_id="u-1",
        )
        retry_mw = EmptyToolCallRetryMiddleware(max_retries=2)

        called_models: list[str] = []

        async def _leaf_model_call(req) -> ModelResponse:
            called_models.append(req.model.model)
            return _resp(_msg_with_reason("stop_reason", "refusal", model=req.model.model))

        async def _stack(req) -> ModelResponse:
            return await retry_mw.awrap_model_call(req, _leaf_model_call)

        request = _FakeModelRequest(
            model=primary_client,
            messages=[HumanMessage(content="hello")],
        )
        result = await resilience.awrap_model_call(request, _stack)

        assert called_models == [
            "claude-opus-5-5-medium",
            "claude-opus-5-5-medium",
            "claude-opus-5-5-medium",
        ]
        assert result.result[0].content == REFUSAL_NOTICE_CHANNEL


# ---------------------------------------------------------------------------
# Offline verification of probe script assets & startup path
# ---------------------------------------------------------------------------


class TestProbeScriptAssets:
    def test_generated_png_and_pdf_are_valid_and_build_four_expected_payloads(self) -> None:
        png_bytes = make_number_png("7391")
        png2_bytes = make_number_png("4816")
        img = Image.open(io.BytesIO(png_bytes))
        assert img.format == "PNG"
        assert img.width > 100 and img.height > 50

        pdf_bytes = make_number_pdf("5924")
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == 1
        assert "5924" in (reader.pages[0].extract_text() or "")

        img_b64 = base64.b64encode(png_bytes).decode("ascii")
        img2_b64 = base64.b64encode(png2_bytes).decode("ascii")
        pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
        probes = _build_probe_messages(img_b64, pdf_b64, img2_b64)
        assert len(probes) == 4

        # Verify all 4 probe payloads deliver inlineData through _messages_to_gemini
        for _label, expected, messages, _use_tools in probes:
            _, contents = _messages_to_gemini(messages, gemini_target=True)
            assert any("inlineData" in p for c in contents for p in c["parts"])
            assert _answer_matches_all(" ".join(expected), expected)
            for code in expected:
                assert _answer_matches(f"The number is {code}.", code)
                assert not _answer_matches(f"The number is 1{code}.", code)

        # Probe 4: two parallel Read calls each with an image -> one user turn [FR1, FR2, img1, img2]
        label_4, expected_4, messages_4, use_tools_4 = probes[3]
        assert "parallel" in label_4
        assert expected_4 == ("7391", "4816")
        assert use_tools_4 is True
        _, contents_4 = _messages_to_gemini(messages_4, gemini_target=True)
        assert len(contents_4) == 3
        parts_4 = contents_4[2]["parts"]
        assert len(parts_4) == 4
        assert [p["functionResponse"]["id"] for p in parts_4[:2]] == [
            "probe_call_parallel_1",
            "probe_call_parallel_2",
        ]
        assert [p["inlineData"]["data"] for p in parts_4[2:]] == [img_b64, img2_b64]

    @pytest.mark.asyncio
    async def test_build_readonly_client_validates_antigravity_provider_and_passes_custom_entry(
        self, monkeypatch
    ) -> None:
        # Ensure we exercise the real subscription_route signature (requires keyword-only custom_entry).
        mc = LLMFactory.get_model_config()
        assert mc.get_model_config("gemini-4-argon-medium-oauth") is not None

        # Non-antigravity OAuth model (Claude direct OAuth) must be rejected before any DB/network call.
        get_tokens_mock = create_autospec(oauth_tokens_db.get_oauth_tokens)
        monkeypatch.setattr(oauth_tokens_db, "get_oauth_tokens", get_tokens_mock)
        with pytest.raises(RuntimeError, match="antigravity-oauth"):
            await _build_readonly_client("claude-sonnet-5-5-oauth", "u-1")
        assert get_tokens_mock.await_count == 0

        # Valid Antigravity OAuth model resolves read-only via get_oauth_tokens + create_llm.
        from src.llms import llm as llm_mod

        fake_client = _fake_model("gemini-4-argon-medium", "gemini-4-argon-medium-oauth")
        get_tokens_mock.return_value = {
            "access_token": "tok-readonly-never-logged",
            "refresh_token": "ref-unused",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
        create_llm_mock = create_autospec(llm_mod.create_llm, return_value=fake_client)
        monkeypatch.setattr(llm_mod, "create_llm", create_llm_mock)

        client = await _build_readonly_client("gemini-4-argon-medium-oauth", "u-1")
        assert client is fake_client
        get_tokens_mock.assert_awaited_once_with("u-1", "antigravity-oauth")
        create_llm_mock.assert_called_once_with(
            "gemini-4-argon-medium-oauth", api_key="tok-readonly-never-logged"
        )
        # Help text must not point operators at direct Claude OAuth.
        assert "claude-sonnet-5-5-medium-oauth" in (probe_mod.__doc__ or "")
