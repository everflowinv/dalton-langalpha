"""Which lines Dalton allows native (provider) compaction on.

Product decision 6 (roadmap §6): only the official Claude / OpenAI API lines
keep upstream's default (native compaction on). Every line Dalton operates or
relays itself — the Claude-Code OAuth subscription route, Antigravity, Muse,
xAI/Grok OAuth, the GLM coding-plan route, and the broker that fronts them —
is off until its real capability is proven, exactly because the gate upstream
reads (SDK family + official host + beta) says nothing about whether *our*
credential's endpoint serves the signed-summary protocol this build relies on.
A model name containing "claude" is not capability.

Both gates read this list: the manifest verdict
(``src.llms.provider_compaction.native_compaction_kind``, via the call site in
``LLM._build_manifest``) and the client-side verdict (``native_kind``, read
off the ``pricing_provider`` stamp the client builder puts in metadata).
"""

from __future__ import annotations

#: Manifest provider names whose native compaction Dalton keeps OFF. Everything
#: not listed follows upstream's own verdict (official api-key Claude / OpenAI
#: and the Codex subscription line stay on, as upstream proved them).
NATIVE_COMPACTION_OFF_PROVIDERS = frozenset(
    {
        # The Claude Code OAuth subscription route — official host, but
        # Dalton's own credential and beta set; unproven for compaction.
        "claude-oauth",
        # Antigravity (Google One) OAuth relays.
        "antigravity",
        "antigravity-oauth",
        # Meta Muse OAuth relays.
        "meta",
        "muse-oauth",
        # xAI Grok API and OAuth relays.
        "xai",
        "xai-oauth",
        # OpenClaw broker gateway.
        "openclaw-broker",
        # GLM coding-plan subscription routes (Anthropic-compatible wire).
        "z-ai-coding",
        "z-ai-cn-coding",
        # Kimi / MiniMax / Doubao coding plans (Anthropic-compatible wire).
        "moonshot-coding",
        "minimax-coding",
        "doubao-coding",
    }
)


def native_compaction_disabled(provider: str | None) -> bool:
    """Whether Dalton keeps native compaction off for ``provider`` outright.

    Unknown/absent names are Dalton's own overlay lines in practice; they
    already verdict None upstream, so only the listed names matter.
    """
    return provider in NATIVE_COMPACTION_OFF_PROVIDERS
