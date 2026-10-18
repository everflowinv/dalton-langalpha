"""Unit tests for P0N.5 native model family resolution, independent reviewer selection, and role checks."""

from __future__ import annotations

import pytest

from src.dalton.llm.role_policy import (
    check_roles,
    family_of,
    is_forbidden_for_roles,
    select_independent_reviewer,
)


def test_family_of_native_and_broker_models():
    """P0N.5 L20: family_of resolves underlying model family regardless of hosting provider."""
    assert family_of("claude-opus-5-5-oauth") == "anthropic-claude"
    assert family_of("claude-opus-5-5-high-oauth") == "anthropic-claude"
    assert family_of("claude-sonnet-5-5-thinking-low-oauth") == "anthropic-claude"
    assert family_of("gpt-6-astra-oauth") == "openai-gpt"
    assert family_of("gpt-6.1-sol-oauth") == "openai-gpt"
    assert family_of("gpt-oss-120b-medium-oauth") == "openai-gpt"
    assert family_of("gemini-4-argon-high-oauth") == "google-gemini"
    assert family_of("gemini-3.8-flash-low-oauth") == "google-gemini"
    assert family_of("grok-4.7") == "xai-grok"
    assert family_of("grok-4.3") == "xai-grok"
    assert family_of("deepseek-flash") == "deepseek"
    assert family_of("glm-5.3-flash") == "zai-glm"
    assert family_of("muse-spark-1.3-oauth") == "meta-muse"
    assert family_of("kimi-k2-thinking") == "moonshot-kimi"
    assert family_of("qwen3-max") == "alibaba-qwen"
    assert family_of("minimax-m2") == "minimax"
    assert family_of("ocb-xai-grok-4.7") == "xai-grok"
    assert family_of("ocb-openai-gpt-6.1-sol") == "openai-gpt"


def test_select_independent_reviewer_across_lead_families():
    """P0N.5 N11: reviewer family always differs from lead family, including Antigravity-hosted Claude."""
    candidates = [
        "gpt-6.1-sol-oauth",
        "gemini-4-argon-high-oauth",
        "grok-4.7",
        "claude-opus-5-5-oauth",
    ]
    # 1. Lead = claude-opus-5-5-oauth -> picks gpt-6.1-sol-oauth
    rev1 = select_independent_reviewer("claude-opus-5-5-oauth", candidates)
    assert rev1 == "gpt-6.1-sol-oauth"
    assert family_of(rev1) != family_of("claude-opus-5-5-oauth")

    # 2. Lead = gpt-6-astra-oauth -> skips gpt-6.1-sol-oauth, picks gemini-4-argon-high-oauth
    rev2 = select_independent_reviewer("gpt-6-astra-oauth", candidates)
    assert rev2 == "gemini-4-argon-high-oauth"
    assert family_of(rev2) != family_of("gpt-6-astra-oauth")

    # 3. Lead = claude-opus-5-5-high-oauth (Antigravity Claude) -> never picks any Claude
    candidates_claude_first = [
        "claude-opus-5-5-oauth",
        "claude-sonnet-5-5-oauth",
        "gpt-6.1-sol-oauth",
        "gemini-4-argon-high-oauth",
    ]
    rev3 = select_independent_reviewer("claude-opus-5-5-high-oauth", candidates_claude_first)
    assert rev3 == "gpt-6.1-sol-oauth"
    assert family_of(rev3) != "anthropic-claude"

    # 4. When gpt-6.1-sol-oauth is unavailable (e.g. ChatGPT disconnected), skips to gemini-4-argon-high-oauth
    available = {"gemini-4-argon-high-oauth", "grok-4.7", "claude-opus-5-5-oauth"}
    rev4 = select_independent_reviewer(
        "claude-opus-5-5-oauth", candidates, available_models=available
    )
    assert rev4 == "gemini-4-argon-high-oauth"


def test_contributor_models_forbidden_for_all_roles():
    """P0N.5 L21 / N24: contributor OAuth variants are forbidden for any role and emit a warning."""
    assert is_forbidden_for_roles("muse-spark-1.3-contributor-oauth") is True
    assert is_forbidden_for_roles("muse-beacon-1.1-contributor-oauth") is True
    assert is_forbidden_for_roles("muse-spark-1.3-oauth") is False

    # Cannot be selected as reviewer even if listed first
    rev = select_independent_reviewer(
        "claude-opus-5-5-oauth",
        ["muse-spark-1.3-contributor-oauth", "grok-4.7"],
    )
    assert rev == "grok-4.7"

    # Emits role_forbidden_model warning when assigned to a subagent
    warnings = check_roles(
        {
            "llm": {"name": "claude-opus-5-5-oauth"},
            "subagents": {
                "definitions": {
                    "research": {"model": "muse-spark-1.3-contributor-oauth"}
                }
            },
        }
    )
    codes = [w["code"] for w in warnings]
    assert "role_forbidden_model" in codes


@pytest.mark.parametrize("slot", ["assist", "background", "compaction", "fetch"])
def test_contributor_models_forbidden_in_auxiliary_slots_and_fail_startup(slot: str):
    """P0H §4 P3 (L21): assist, background, compaction, and fetch slots reject contributor models and fail startup."""
    from src.dalton.llm.role_policy import RolePolicyError, enforce_role_policy_on_startup

    bad_cfg = {
        "llm": {
            "name": "claude-opus-5-5-oauth",
            slot: "muse-spark-1.3-contributor-oauth",
        }
    }
    warnings = check_roles(bad_cfg)
    forbidden = [w for w in warnings if w["code"] == "role_forbidden_model" and w["role"] == slot]
    assert len(forbidden) == 1
    assert forbidden[0]["model"] == "muse-spark-1.3-contributor-oauth"

    with pytest.raises(RolePolicyError, match="muse-spark-1.3-contributor-oauth"):
        enforce_role_policy_on_startup(bad_cfg)


@pytest.mark.asyncio
async def test_resolve_assist_model_and_startup_wiring_reject_contributor_model(
    monkeypatch: pytest.MonkeyPatch,
):
    """P0H §4 P3: resolve_assist_model and verify_local_wiring raise RolePolicyError if assist is set to a contributor variant."""
    import src.dalton.budget.gate as gate_mod
    import src.dalton.llm.assist as assist_mod
    import src.dalton.llm.role_policy as rp_mod
    from src.dalton.llm.role_policy import RolePolicyError

    with pytest.raises(RolePolicyError, match="muse-spark-1.3-contributor-oauth"):
        assist_mod.resolve_assist_model(
            role="assist", explicit_model="muse-spark-1.3-contributor-oauth"
        )

    # assist_json catches RolePolicyError and returns ok=False without ever calling dalton_complete
    res = await assist_mod.assist_json(
        task="test.forbidden",
        instructions="Return JSON",
        payload={"x": 1},
        schema={"type": "object"},
        model="muse-spark-1.3-contributor-oauth",
    )
    assert res.ok is False
    assert "muse-spark-1.3-contributor-oauth" in (res.error or "")

    # Backend startup (verify_local_wiring) fails when agent_config.dalton.yaml has assist = contributor variant
    monkeypatch.setattr(
        rp_mod,
        "_load_default_agent_config",
        lambda: {
            "llm": {
                "name": "claude-opus-5-5-oauth",
                "assist": "muse-spark-1.3-contributor-oauth",
            }
        },
    )
    with pytest.raises(RolePolicyError, match="assist=muse-spark-1.3-contributor-oauth"):
        await gate_mod.verify_local_wiring()


def test_default_role_config_has_zero_warnings():
    """P0N.5: default agent_config.dalton.yaml produces 0 warnings from check_roles()."""
    default_cfg = {
        "llm": {
            "name": "claude-opus-5-5-oauth",
            "background": "deepseek-flash",
            "assist": "gemini-3.8-flash-low-oauth",
        },
        "subagents": {
            "definitions": {
                "research": {"model": "grok-4.7"},
                "data-prep": {"model": "grok-4.7"},
                "general-purpose": {"model": "grok-4.7"},
            }
        },
    }
    assert check_roles(default_cfg) == []

