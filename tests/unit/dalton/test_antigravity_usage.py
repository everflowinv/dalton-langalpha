"""Gemini usage from Antigravity keeps the cached part of the prompt visible to usage and cost."""

from __future__ import annotations

from src.dalton.llm.providers.antigravity.client import ChatAntigravity
from langchain_core.messages import AIMessage

from src.llms.token_counter import extract_token_usage


def test_cached_prompt_tokens_become_cache_read() -> None:
    usage = ChatAntigravity._usage_metadata(
        {
            "usageMetadata": {
                "promptTokenCount": 10_000,
                "cachedContentTokenCount": 8_000,
                "candidatesTokenCount": 300,
                "thoughtsTokenCount": 200,
                "totalTokenCount": 10_500,
            }
        }
    )
    assert usage is not None
    assert usage["input_tokens"] == 10_000
    assert usage["input_token_details"]["cache_read"] == 8_000
    assert usage["output_tokens"] == 500
    assert usage["output_token_details"]["reasoning"] == 200

    # What usage tracking and cost read from the message.
    counted = extract_token_usage(AIMessage(content="ok", usage_metadata=usage))
    assert counted.get("cached_tokens") == 8_000


def test_no_cache_field_leaves_input_details_out() -> None:
    usage = ChatAntigravity._usage_metadata(
        {"usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10, "totalTokenCount": 110}}
    )
    assert usage is not None
    assert "input_token_details" not in usage
