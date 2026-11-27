"""Unit tests for HF2: Antigravity multimodal delivery + refusal notice & cross-family backup retry."""

from __future__ import annotations

import base64
import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.config import get_config
from PIL import Image
import pypdf

from ptc_agent.agent.middleware.file_operations.multimodal import build_content_blocks
from ptc_agent.agent.middleware.model_resilience import FALLBACK_KEY, ModelResilienceMiddleware
from ptc_agent.agent.middleware.tool import empty_call_retry
from ptc_agent.agent.middleware.tool.empty_call_retry import (
    _EMPTY_REPLY_NOTICE,
    EmptyToolCallRetryMiddleware,
)
from scripts.dalton.ops.prod.probe_antigravity_multimodal import (
    _answer_matches,
    _build_probe_messages,
    make_number_pdf,
    make_number_png,
)
from src.dalton.llm.providers.antigravity.client import ChatAntigravity, _messages_to_gemini
from src.dalton.middleware import refusal_fallback
from src.dalton.middleware.refusal_fallback import (
    REFUSAL_NOTICE_CHANNEL,
    REFUSAL_NOTICE_WEB,
    refusal_notice_text,
    supports_slash_new,
)
from src.llms.model_identity import CATALOG_MODEL_KEY

IMG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nHF2IMAGEPAYLOAD" * 3).decode("ascii")
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
    monkeypatch.setattr(refusal_fallback, "_turn_config_safe", lambda: cfg)


def _refusal_msg(model: str = "claude-opus-5-5-oauth", content: str = "") -> AIMessage:
    return AIMessage(
        content=content,
        response_metadata={"stop_reason": "refusal", "model_name": model},
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
# Antigravity multimodal conversion (Requirements 1, 2, 3)
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
                tool_calls=[{"id": "call_img", "name": "Read", "args": {"file_path": "/work/short_interest.png"}}],
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
# Refusal notice & cross-family backup retry (Requirements 4 & 5)
# ---------------------------------------------------------------------------


class TestRefusalNoticeAndBackupRetry:
    def test_all_refusal_retries_emit_channel_notice_with_slash_new(self, monkeypatch) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = SimpleNamespace(messages=[HumanMessage(content="how to cook rice?")])

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(_handler_sig, side_effect=[_resp(_refusal_msg())] * 3)
        result = mw.wrap_model_call(request, handler)

        assert handler.call_count == 3
        assert len(result.result) == 1
        assert result.result[0].content == REFUSAL_NOTICE_CHANNEL
        assert "/new" in result.result[0].content

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

        handler = create_autospec(_handler_sig, side_effect=[_resp(_refusal_msg())] * 3)
        result = mw.wrap_model_call(request, handler)

        assert result.result[0].content == REFUSAL_NOTICE_WEB
        assert "/new" not in result.result[0].content
        assert "新建对话" in result.result[0].content

    def test_mixed_empty_and_refusal_keeps_generic_empty_notice(self, monkeypatch) -> None:
        _patch_config(monkeypatch, _config(platform="feishu"))
        mw = EmptyToolCallRetryMiddleware(max_retries=2)
        request = SimpleNamespace(messages=[HumanMessage(content="hello")])
        plain_empty = AIMessage(
            content="",
            response_metadata={"stop_reason": "end_turn", "model_name": "claude-opus-5-5-oauth"},
        )

        def _handler_sig(req: ModelRequest) -> ModelResponse:
            raise NotImplementedError

        handler = create_autospec(
            _handler_sig,
            side_effect=[_resp(plain_empty), _resp(_refusal_msg()), _resp(_refusal_msg())],
        )
        result = mw.wrap_model_call(request, handler)

        assert result.result[0].content == _EMPTY_REPLY_NOTICE

    @pytest.mark.asyncio
    async def test_refusal_exhausted_switches_once_to_cross_family_catalog_backup(
        self, monkeypatch
    ) -> None:
        _patch_config(monkeypatch, _config(platform="feishu", user_id="u-1", thread_id="t-1"))

        primary_client = _fake_model("claude-opus-5-5", "claude-opus-5-5-oauth")
        backup_client = _fake_model("gemini-4-argon-medium", "gemini-4-argon-medium-oauth")

        async def _catalog_stub() -> list[str]:
            return ["claude-sonnet-5-5-oauth", "gemini-4-argon-medium-oauth"]

        async def _avail_stub(user_id: str, *, require_tool_calling: bool) -> dict[str, dict]:
            return {
                "claude-sonnet-5-5-oauth": {"available": True, "family": "anthropic-claude"},
                "gemini-4-argon-medium-oauth": {"available": True, "family": "google-gemini"},
            }

        async def _family_stub(model_key: str | None, user_id: str | None) -> str:
            if model_key and "claude" in model_key:
                return "anthropic-claude"
            if model_key and "gemini" in model_key:
                return "google-gemini"
            return "unknown"

        async def _resolve_stub(user_id: str, model_key: str, *, thread_id: str | None):
            assert model_key == "gemini-4-argon-medium-oauth"
            return backup_client

        monkeypatch.setattr(
            refusal_fallback,
            "_load_catalog_candidates",
            create_autospec(refusal_fallback._load_catalog_candidates, side_effect=_catalog_stub),
        )
        monkeypatch.setattr(
            refusal_fallback,
            "_explain_availability",
            create_autospec(refusal_fallback._explain_availability, side_effect=_avail_stub),
        )
        monkeypatch.setattr(
            refusal_fallback,
            "_resolve_family",
            create_autospec(refusal_fallback._resolve_family, side_effect=_family_stub),
        )
        monkeypatch.setattr(
            refusal_fallback,
            "_resolve_backup_client",
            create_autospec(refusal_fallback._resolve_backup_client, side_effect=_resolve_stub),
        )

        resilience = ModelResilienceMiddleware(
            primary_name="claude-opus-5-5-oauth",
            primary_client=primary_client,
            fallbacks=[],
            user_id="u-1",
        )
        retry_mw = EmptyToolCallRetryMiddleware(max_retries=2)

        calls: list[tuple[str, int]] = []

        async def _leaf_model_call(req) -> ModelResponse:
            calls.append((req.model.model, len(req.messages)))
            if req.model is primary_client:
                return _resp(_refusal_msg("claude-opus-5-5"))
            return _resp(
                AIMessage(
                    content="煮米饭按 1:1.2 水米比例即可。",
                    response_metadata={"stop_reason": "STOP", "model_name": "gemini-4-argon-medium"},
                )
            )

        async def _stack(req) -> ModelResponse:
            return await retry_mw.awrap_model_call(req, _leaf_model_call)

        request = _FakeModelRequest(
            model=primary_client,
            messages=[HumanMessage(content="怎么做蛋炒饭？")],
        )

        result = await resilience.awrap_model_call(request, _stack)

        # Primary tried 1 + 2 retries = 3 times, then cross-family backup tried once with clean messages.
        assert [m for m, _ in calls] == [
            "claude-opus-5-5",
            "claude-opus-5-5",
            "claude-opus-5-5",
            "gemini-4-argon-medium",
        ]
        # Backup call received the clean original 1 message (no stale refusal/hint from primary).
        assert calls[-1][1] == 1
        assert result.result[0].content == "煮米饭按 1:1.2 水米比例即可。"
        assert result.result[0].response_metadata[FALLBACK_KEY] == "gemini-4-argon-medium-oauth"
        assert result.result[0].response_metadata[CATALOG_MODEL_KEY] == "gemini-4-argon-medium-oauth"

    @pytest.mark.asyncio
    async def test_refusal_backup_also_refuses_falls_back_to_refusal_notice(
        self, monkeypatch
    ) -> None:
        _patch_config(monkeypatch, _config(platform="web", user_id="u-1", thread_id="t-2"))

        primary_client = _fake_model("claude-opus-5-5", "claude-opus-5-5-oauth")
        backup_client = _fake_model("gpt-6.2-sol", "gpt-6.2-sol-oauth")

        async def _select_stub(*args, **kwargs):
            return "gpt-6.2-sol-oauth", backup_client

        monkeypatch.setattr(
            refusal_fallback,
            "select_cross_family_backup",
            create_autospec(refusal_fallback.select_cross_family_backup, side_effect=_select_stub),
        )

        resilience = ModelResilienceMiddleware(
            primary_name="claude-opus-5-5-oauth",
            primary_client=primary_client,
            fallbacks=[],
            user_id="u-1",
        )
        retry_mw = EmptyToolCallRetryMiddleware(max_retries=2)

        calls: list[str] = []

        async def _leaf_model_call(req) -> ModelResponse:
            calls.append(req.model.model)
            return _resp(_refusal_msg(req.model.model))

        async def _stack(req) -> ModelResponse:
            return await retry_mw.awrap_model_call(req, _leaf_model_call)

        request = _FakeModelRequest(
            model=primary_client,
            messages=[HumanMessage(content="hello")],
        )

        result = await resilience.awrap_model_call(request, _stack)

        # Backup is retried ONLY ONCE (3 primary + 1 backup = 4 total calls).
        assert calls == [
            "claude-opus-5-5",
            "claude-opus-5-5",
            "claude-opus-5-5",
            "gpt-6.2-sol",
        ]
        assert result.result[0].content == REFUSAL_NOTICE_WEB

    @pytest.mark.asyncio
    async def test_no_cross_family_backup_available_returns_refusal_notice(
        self, monkeypatch
    ) -> None:
        _patch_config(monkeypatch, _config(platform="feishu", user_id="u-1", thread_id="t-3"))
        primary_client = _fake_model("claude-opus-5-5", "claude-opus-5-5-oauth")

        async def _select_none(*args, **kwargs):
            return None

        monkeypatch.setattr(
            refusal_fallback,
            "select_cross_family_backup",
            create_autospec(refusal_fallback.select_cross_family_backup, side_effect=_select_none),
        )

        resilience = ModelResilienceMiddleware(
            primary_name="claude-opus-5-5-oauth",
            primary_client=primary_client,
            fallbacks=[],
            user_id="u-1",
        )
        retry_mw = EmptyToolCallRetryMiddleware(max_retries=2)

        async def _leaf_model_call(req) -> ModelResponse:
            return _resp(_refusal_msg("claude-opus-5-5"))

        async def _stack(req) -> ModelResponse:
            return await retry_mw.awrap_model_call(req, _leaf_model_call)

        request = _FakeModelRequest(
            model=primary_client,
            messages=[HumanMessage(content="hello")],
        )

        result = await resilience.awrap_model_call(request, _stack)
        assert result.result[0].content == REFUSAL_NOTICE_CHANNEL


# ---------------------------------------------------------------------------
# Offline verification of probe script asset generators (Requirement 6)
# ---------------------------------------------------------------------------


class TestProbeScriptAssets:
    def test_generated_png_and_pdf_are_valid_and_build_expected_payloads(self) -> None:
        png_bytes = make_number_png("7391")
        img = Image.open(io.BytesIO(png_bytes))
        assert img.format == "PNG"
        assert img.width > 100 and img.height > 50

        pdf_bytes = make_number_pdf("5924")
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        assert len(reader.pages) == 1
        assert "5924" in (reader.pages[0].extract_text() or "")

        img_b64 = base64.b64encode(png_bytes).decode("ascii")
        pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
        probes = _build_probe_messages(img_b64, pdf_b64)
        assert len(probes) == 3

        # Verify all 3 probe payloads actually deliver inlineData through _messages_to_gemini
        for _label, expected, messages, _use_tools in probes:
            _, contents = _messages_to_gemini(messages, gemini_target=True)
            assert any("inlineData" in p for c in contents for p in c["parts"])
            assert _answer_matches(f"The number is {expected}.", expected)
            assert not _answer_matches(f"The number is 1{expected}.", expected)
