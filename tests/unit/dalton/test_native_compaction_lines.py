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


def test_handoff_on_switch_across_official_and_dalton_routes() -> None:
    """P0-6: Native compaction summaries only replay on the same official lineage;
    switching to or from any Dalton third-party route falls back to text handoff."""
    from langchain_core.messages import AIMessage, HumanMessage

    from src.llms.llm import create_llm
    from src.llms.provider_compaction import (
        SIDECAR_KEY,
        _native,
        adopt,
        replays,
        sidecar,
        without_blocks,
    )

    official_anthropic = create_llm("claude-opus-5-5", api_key="dummy")
    claude_oauth = create_llm("claude-opus-5-5-oauth", api_key="dummy")
    official_openai = create_llm("gpt-6.1-sol", api_key="dummy")
    codex_oauth = create_llm("gpt-6.1-sol-oauth", api_key="dummy")
    antigravity = create_llm("gemini-4-argon-high-oauth", api_key="dummy")
    muse_oauth = create_llm("muse-wonder-1.0-oauth", api_key="dummy")
    xai = create_llm("grok-4.7-oauth", api_key="dummy")
    broker = create_llm("ocb-claude-cli-gateway-claude-opus-5-5", api_key="dummy")

    anthropic_summary = HumanMessage(
        content="signed-summary-text",
        additional_kwargs={
            "lc_source": "summarization",
            SIDECAR_KEY: sidecar(
                {"block": {"type": "compaction", "content": "signed-summary-text"}},
                "anthropic",
                "pointer-text",
            ),
        },
    )
    openai_summary = HumanMessage(
        content="plain fallback text",
        additional_kwargs={
            "lc_source": "summarization",
            SIDECAR_KEY: sidecar(
                {"items": [{"type": "compaction", "encrypted_content": "enc123"}]},
                "openai",
                "pointer-text",
            ),
        },
    )

    # 1. Same-family official -> official replays natively
    adopted_anth = adopt([anthropic_summary], official_anthropic, "anthropic")
    assert adopted_anth is not None
    assert adopted_anth[0].content[0] == {"type": "compaction", "content": "signed-summary-text"}
    assert replays(anthropic_summary, official_anthropic) is True

    adopted_oai = adopt([openai_summary], official_openai, "openai")
    assert adopted_oai is not None
    assert adopt([openai_summary], codex_oauth, "codex-oauth") is not None
    assert replays(openai_summary, codex_oauth) is True

    # 2. Cross-family official <-> official cannot replay natively -> text handoff
    assert adopt([anthropic_summary], official_openai, "openai") is None
    assert adopt([anthropic_summary], codex_oauth, "codex-oauth") is None
    assert adopt([openai_summary], official_anthropic, "anthropic") is None

    # 3. Official -> Dalton third-party route cannot replay natively -> text handoff
    for relay_client, relay_route in (
        (claude_oauth, "claude-oauth"),
        (antigravity, "antigravity"),
        (muse_oauth, "muse-oauth"),
        (xai, "xai"),
        (broker, "openclaw-broker"),
    ):
        assert adopt([anthropic_summary], relay_client, relay_route) is None
        assert replays(anthropic_summary, relay_client) is False
        assert adopt([openai_summary], relay_client, relay_route) is None
        assert replays(openai_summary, relay_client) is False

    stripped = without_blocks([anthropic_summary, AIMessage(content="next")])
    assert stripped is not None
    assert SIDECAR_KEY not in stripped[0].additional_kwargs
    assert stripped[0].content == "signed-summary-text"

    # 4. Dalton third-party origin -> Official route cannot replay natively
    for relay_origin in (
        "claude-oauth",
        "antigravity",
        "muse-oauth",
        "xai",
        "openclaw-broker",
        "openclaw-broker|https://relay.example/v1",
    ):
        relay_origin_summary = HumanMessage(
            content="from-relay",
            additional_kwargs={
                "lc_source": "summarization",
                SIDECAR_KEY: sidecar(
                    {"block": {"type": "compaction", "content": "from-relay"}},
                    relay_origin,
                    "pointer-text",
                ),
            },
        )
        assert _native(relay_origin_summary, "anthropic", "anthropic") is None
        assert adopt([relay_origin_summary], official_anthropic, "anthropic") is None
        assert replays(relay_origin_summary, official_anthropic) is False
        assert adopt([relay_origin_summary], official_openai, "openai") is None
        assert adopt([relay_origin_summary], codex_oauth, "codex-oauth") is None
