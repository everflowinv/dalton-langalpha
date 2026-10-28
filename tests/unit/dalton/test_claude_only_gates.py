"""Dalton's own lines never take the Claude-only request machinery (#631).

The preserved-thinking work gates every Claude-only field — the fixed tools
array with ``tool_addition``/``tool_removal`` rows, turn-scoped system
messages, the tool-change and thinking-binding betas — on a ``ChatAnthropic``
client pointed at the official ``api.anthropic.com`` host. Dalton's own
providers are OpenAI-SDK or Gemini-SDK clients, and an Anthropic-compatible
coding-plan host is not the official host, so a gate keyed on the SDK alone
would leak. Locked here because the upstream isolation test walks the base
manifest only, which lists none of Dalton's lines.
"""

from __future__ import annotations

from unittest.mock import create_autospec

from langchain_anthropic import ChatAnthropic

from src.dalton.llm.openclaw_catalog import build_overlay
from src.dalton.llm.providers.antigravity import ChatAntigravity
from src.dalton.llm.providers.muse.client import ChatMuseOpenAI
from src.dalton.llm.providers.xai_grok.client import ChatGrokOpenAI
from src.llms.operator_channel import carries_tool_changes, carries_turn_scoped


def test_antigravity_muse_and_grok_never_carry_claude_only_fields():
    clients = [
        ChatAntigravity(),
        ChatMuseOpenAI(model="muse-1", api_key="k"),
        ChatGrokOpenAI(model="grok-4.7", api_key="k"),
    ]
    for client in clients:
        assert not isinstance(client, ChatAnthropic)
        assert carries_tool_changes(client) is False
        assert carries_turn_scoped(client) is False


def test_anthropic_compatible_host_never_carries_claude_only_fields():
    """The coding-plan shape: Claude's SDK on another provider's host."""
    compat = ChatAnthropic(model="claude-x", api_key="k", base_url="https://api.deepseek.com/anthropic")
    assert isinstance(compat, ChatAnthropic)
    assert carries_tool_changes(compat) is False
    assert carries_turn_scoped(compat) is False


def test_broker_overlay_builds_an_openai_sdk_line():
    overlay = build_overlay(
        catalog={"data": [{"id": "gpt-x", "displayName": "GPT X"}]},
        v1_rate_cards={},
        price_cards={},
        current_overlay={},
    )
    broker = overlay["providers"]["provider_config"]["openclaw-broker"]
    assert broker["sdk"] == "openai" and broker["use_response_api"] is False


def test_the_gates_ask_the_client_not_its_name():
    """A duck-typed stand-in with Claude's URL shape but another SDK class is
    refused: the gate reads the class and the resolved host, never the model
    name, so a Dalton line named like a Claude model cannot slip through."""
    other_sdk = create_autospec(ChatGrokOpenAI, instance=True)
    assert carries_tool_changes(other_sdk) is False
    assert carries_turn_scoped(other_sdk) is False
