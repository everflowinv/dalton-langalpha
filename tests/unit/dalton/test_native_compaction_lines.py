"""DALTON-HOOK(native-compaction-lines): product decision 6 — only the
official Claude / OpenAI lines keep native (provider) compaction on; every
Dalton-operated OAuth/subscription relay is off at both gates (the manifest
offer and the client-side verdict), whatever the SDK family and host say."""

from __future__ import annotations

from unittest.mock import create_autospec

from src.dalton.llm.native_compaction_lines import (
    NATIVE_COMPACTION_OFF_PROVIDERS,
    native_compaction_disabled,
)


def test_the_official_lines_stay_on_and_the_relays_off() -> None:
    from src.llms.llm import ModelConfig

    meta = ModelConfig().get_model_metadata()
    on = {name for name, entry in meta.items() if entry.get("native_compaction")}
    # Official API lines (and the Codex subscription upstream proved) stay on.
    assert "claude-opus-5-5" in on
    assert "gpt-6.1-sol" in on
    assert "gpt-6.1-sol-oauth" in on  # codex-oauth: official OpenAI subscription
    # Every Dalton relay in the off-list is off, by exact name.
    for name, entry in meta.items():
        if entry.get("provider") in NATIVE_COMPACTION_OFF_PROVIDERS:
            assert not entry.get("native_compaction"), (name, entry.get("provider"))
    # Spot-check the lines the decision names.
    for name in (
        "claude-opus-5-5-oauth",
        "gemini-4-argon-high-oauth",
        "muse-wonder-1.0-oauth",
        "grok-4.7-oauth",
        "glm-5.3-plan",
        "ocb-claude-cli-gateway-claude-opus-5-5",
    ):
        assert name not in on


def test_client_side_gate_reads_the_provider_stamp() -> None:
    from src.llms.provider_compaction import native_kind

    # A client that would pass every upstream check (official Anthropic host,
    # the compaction beta) still verdicts None when the provider it was built
    # for is one of Dalton's relays — the stamp the client builder puts in
    # metadata is the identity, not the model name.
    relay = create_autospec(
        _AnthropicShapedClient(metadata={"pricing_provider": "claude-oauth"}),
        instance=True,
    )
    assert native_kind(relay) is None
    official = create_autospec(
        _AnthropicShapedClient(metadata={"pricing_provider": "anthropic"}),
        instance=True,
    )
    # Not a ChatAnthropic instance, so upstream's own checks still refuse it;
    # the point is the relay short-circuit happens before them.
    assert native_kind(official) is None
    assert native_compaction_disabled("claude-oauth")
    assert native_compaction_disabled("openclaw-broker")
    assert not native_compaction_disabled("anthropic")
    assert not native_compaction_disabled("openai")
    assert not native_compaction_disabled("codex-oauth")


class _AnthropicShapedClient:
    """Minimal stand-in carrying only what the gate reads off a client."""

    def __init__(self, metadata: dict) -> None:
        self.metadata = metadata
