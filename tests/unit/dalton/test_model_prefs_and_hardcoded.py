"""Unit tests for Dalton model preference resolution, precedence, assist/reviewer wiring, and hardcoded model literal guard."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml

from src.dalton import settings as settings_mod
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import model_prefs as mp_mod
from src.dalton.llm import role_policy as rp_mod
from src.dalton.settings import DaltonConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
DALTON_SRC_DIR = REPO_ROOT / "src" / "dalton"

# Matches string literals containing model-specific prefixes/suffixes required by the guard:
# -oauth", "ocb-, "gpt-, "claude-, "gemini-, "grok-, "muse-, "glm-, "deepseek
_MODEL_LITERAL_RE = re.compile(
    r"""(?P<quote>["'])(?:[^"'\n]*-oauth|(?:ocb-|gpt-|claude-|gemini-|grok-|muse-|glm-|deepseek)[^"'\n]*)(?P=quote)"""
)

# Explicit allowlist of (relative_path_under_src_dalton, unquoted_literal) -> reason.
# Every entry here is a provider ID, a family/broker prefix pattern (ending with '-' or '-oauth'),
# or an OAuth CLI User-Agent string — never a concrete model name.
HARDCODED_LITERAL_ALLOWLIST: dict[tuple[str, str], str] = {
    # Provider IDs (OAuth and API providers)
    ("app/oauth.py", "antigravity-oauth"): "OAuth provider identifier in router table",
    ("app/oauth.py", "claude-oauth"): "OAuth provider identifier in router table",
    ("app/oauth.py", "codex-oauth"): "OAuth provider identifier in router table",
    ("app/oauth.py", "muse-oauth"): "OAuth provider identifier in router table",
    ("app/oauth.py", "xai-oauth"): "OAuth provider identifier in router table",
    ("budget/service.py", "antigravity-oauth"): "Provider ID returned by _resolve_provider_for_model",
    ("budget/service.py", "claude-oauth"): "Provider ID returned by _resolve_provider_for_model",
    ("budget/service.py", "codex-oauth"): "Provider ID returned by _resolve_provider_for_model",
    ("budget/service.py", "deepseek"): "Provider ID returned by _resolve_provider_for_model",
    ("budget/service.py", "muse-oauth"): "Provider ID returned by _resolve_provider_for_model",
    ("budget/service.py", "xai-oauth"): "Provider ID returned by _resolve_provider_for_model",
    ("database/muse_tokens.py", "muse-oauth"): "OAuth provider identifier in token DB cleanup",
    ("llm/complete.py", "muse-oauth"): "Provider ID in Muse auth-expired error handling",
    ("llm/native_catalog.py", "antigravity-oauth"): "Native OAuth provider ID in native catalog builder",
    ("llm/native_catalog.py", "claude-oauth"): "Native OAuth provider ID in native catalog builder",
    ("llm/native_catalog.py", "codex-oauth"): "Native OAuth provider ID in native catalog builder",
    ("llm/native_catalog.py", "muse-oauth"): "Native OAuth provider ID in native catalog builder",
    ("llm/native_catalog.py", "xai-oauth"): "Native OAuth provider ID in native catalog builder",
    ("llm/openclaw_catalog.py", "claude-cli-gateway"): "OpenClaw gateway provider identifier",
    ("llm/openclaw_catalog.py", "deepseek"): "Model family name in openclaw_catalog family table",
    ("llm/openclaw_catalog.py", "muse-cli-gateway"): "OpenClaw gateway provider identifier",
    ("llm/overlay.py", "antigravity-oauth"): "Native OAuth provider ID in catalog overlay merge",
    ("llm/overlay.py", "claude-oauth"): "Native OAuth provider ID in catalog overlay merge",
    ("llm/overlay.py", "codex-oauth"): "Native OAuth provider ID in catalog overlay merge",
    ("llm/overlay.py", "muse-oauth"): "Native OAuth provider ID in catalog overlay merge",
    ("llm/overlay.py", "xai-oauth"): "Native OAuth provider ID in catalog overlay merge",
    ("llm/providers/__init__.py", "antigravity-oauth"): "Provider registry key",
    ("llm/providers/__init__.py", "claude-oauth"): "Provider registry key",
    ("llm/providers/__init__.py", "codex-oauth"): "Provider registry key",
    ("llm/providers/__init__.py", "muse-oauth"): "Provider registry key",
    ("llm/providers/__init__.py", "xai-oauth"): "Provider registry key",
    ("llm/providers/antigravity/oauth.py", "antigravity-oauth"): "OAuth token storage provider key",
    ("llm/providers/claude_oauth/oauth.py", "claude-oauth"): "OAuth token storage provider key",
    ("llm/providers/codex/oauth.py", "codex-oauth"): "OAuth token storage provider key",
    ("llm/providers/muse/client.py", "muse-oauth"): "Provider key for error tagging",
    ("llm/providers/muse/oauth.py", "muse-oauth"): "OAuth token storage provider key",
    ("llm/providers/xai_grok/client.py", "xai-oauth"): "Provider key for error tagging",
    ("llm/providers/xai_grok/oauth.py", "xai-oauth"): "OAuth token storage provider key",
    ("llm/role_policy.py", "antigravity-oauth"): "Provider ID in availability/family checks",
    ("llm/role_policy.py", "claude-cli-gateway"): "OpenClaw gateway provider identifier",
    ("llm/role_policy.py", "claude-oauth"): "Provider ID in availability/family checks",
    ("llm/role_policy.py", "codex-oauth"): "Provider ID in availability/family checks",
    ("llm/role_policy.py", "deepseek"): "Model family name and provider ID in family_of()",
    ("llm/role_policy.py", "muse-cli-gateway"): "OpenClaw gateway provider identifier",
    ("llm/role_policy.py", "muse-oauth"): "Provider ID in availability/family checks",
    ("llm/role_policy.py", "xai-oauth"): "Provider ID in availability/family checks",
    ("publish/review.py", "deepseek"): "Model family name in review.py family_of()",
    ("services/provider_status.py", "antigravity-oauth"): "Provider ID in status table",
    ("services/provider_status.py", "claude-oauth"): "Provider ID in status table",
    ("services/provider_status.py", "codex-oauth"): "Provider ID in status table",
    ("services/provider_status.py", "deepseek"): "Provider ID in status table",
    ("services/provider_status.py", "muse-oauth"): "Provider ID in status table",
    ("services/provider_status.py", "xai-oauth"): "Provider ID in status table",
    # Model family / broker prefix and suffix patterns (used for prefix/suffix classification, not model names)
    ("budget/service.py", "-oauth"): "OAuth model suffix check (.endswith('-oauth'))",
    ("budget/service.py", "claude-"): "Model family prefix check (.startswith('claude-'))",
    ("budget/service.py", "deepseek-"): "Model family prefix check (.startswith('deepseek-'))",
    ("budget/service.py", "gemini-"): "Model family prefix check (.startswith('gemini-'))",
    ("budget/service.py", "glm-"): "Model family prefix check (.startswith('glm-'))",
    ("budget/service.py", "gpt-"): "Model family prefix check (.startswith('gpt-'))",
    ("budget/service.py", "grok-"): "Model family prefix check (.startswith('grok-'))",
    ("budget/service.py", "muse-"): "Model family prefix check (.startswith('muse-'))",
    ("budget/service.py", "ocb-"): "Broker model prefix check (.startswith('ocb-'))",
    ("llm/complete.py", "-oauth"): "OAuth model suffix check (.endswith('-oauth'))",
    ("llm/complete.py", "ocb-"): "Broker model prefix check (.startswith('ocb-'))",
    ("llm/openclaw_catalog.py", "-oauth"): "OAuth model suffix check in catalog builder",
    ("llm/openclaw_catalog.py", "claude-"): "Claude family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "deepseek-"): "DeepSeek family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "gemini-"): "Gemini family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "glm-"): "GLM family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "gpt-"): "GPT family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "gpt-oss-"): "GPT-OSS family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "grok-"): "Grok family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "muse-"): "Muse family prefix in catalog family table",
    ("llm/openclaw_catalog.py", "ocb-"): "Broker catalog prefix construction",
    ("llm/providers/antigravity/client.py", "-oauth"): "Strip -oauth suffix to derive upstream model_id",
    ("llm/providers/antigravity/client.py", "claude-"): "Detect Antigravity-hosted Claude family prefix",
    ("llm/providers/antigravity/client.py", "gemini-3-"): "Detect Gemini 3 thinking parameter prefix",
    ("llm/providers/antigravity/client.py", "gemini-3."): "Detect Gemini 3.x thinking parameter prefix",
    ("llm/providers/antigravity/client.py", "gemini-4"): "Detect Gemini 4 thinking parameter prefix",
    ("llm/providers/antigravity/client.py", "gpt-oss"): "Detect Antigravity-hosted GPT-OSS family prefix",
    ("llm/providers/claude_oauth/client.py", "-oauth"): "Strip -oauth suffix to derive upstream model_id",
    ("llm/providers/codex/client.py", "-oauth"): "Strip -oauth suffix to derive upstream model_id",
    ("llm/providers/muse/client.py", "-contributor-oauth"): "Contributor variant suffix check",
    ("llm/providers/muse/client.py", "-oauth"): "Strip -oauth suffix to derive upstream model_id",
    ("llm/providers/xai_grok/client.py", "-oauth"): "Strip -oauth suffix to derive upstream model_id",
    ("llm/providers/xai_grok/client.py", "{key}-oauth"): "f-string suffix template for OAuth model key",
    ("llm/native_catalog.py", "{upstream_id}-oauth"): "f-string suffix template for native OAuth model key",
    ("llm/role_policy.py", "-contributor-oauth"): "L21 contributor variant suffix check",
    ("llm/role_policy.py", "-oauth"): "OAuth model suffix check",
    ("llm/role_policy.py", "claude-"): "Anthropic Claude family prefix in _FAMILY_PREFIX_RULES",
    ("llm/role_policy.py", "deepseek-"): "DeepSeek family prefix in _FAMILY_PREFIX_RULES",
    ("llm/role_policy.py", "gemini-"): "Google Gemini family prefix in _FAMILY_PREFIX_RULES",
    ("llm/role_policy.py", "glm-"): "Zhipu GLM family prefix in _FAMILY_PREFIX_RULES",
    ("llm/role_policy.py", "gpt-"): "OpenAI GPT family prefix in _FAMILY_PREFIX_RULES",
    ("llm/role_policy.py", "grok-"): "xAI Grok family prefix in _FAMILY_PREFIX_RULES",
    ("llm/role_policy.py", "muse-"): "Meta Muse family prefix in _FAMILY_PREFIX_RULES",
    ("llm/role_policy.py", "ocb-"): "OpenClaw broker prefix in family_of() / select_independent_reviewer()",
    ("publish/review.py", "-oauth"): "OAuth suffix strip in review.py family_of()",
    ("publish/review.py", "claude-"): "Claude family prefix in review.py family_of()",
    ("publish/review.py", "deepseek-"): "DeepSeek family prefix in review.py family_of()",
    ("publish/review.py", "gemini-"): "Gemini family prefix in review.py family_of()",
    ("publish/review.py", "glm-"): "GLM family prefix in review.py family_of()",
    ("publish/review.py", "gpt-"): "GPT family prefix in review.py family_of()",
    ("publish/review.py", "grok-"): "Grok family prefix in review.py family_of()",
    ("publish/review.py", "muse-"): "Muse family prefix in review.py family_of()",
    ("publish/review.py", "ocb-"): "Broker prefix strip in review.py family_of()",
    ("services/provider_status.py", "-oauth"): "OAuth suffix check in provider_status",
    ("services/provider_status.py", "ocb-"): "Broker prefix check in provider_status",
    # OAuth protocol User-Agent headers & Redis cache key prefixes (not model names)
    ("database/muse_tokens.py", "muse-api-key:invalid"): "Redis invalidation flag key for Muse API key",
    ("llm/providers/claude_oauth/oauth.py", "claude-cli/2.1.63 (external, cli)"): "Required Claude CLI OAuth User-Agent header",
    ("llm/providers/muse/client.py", "muse-build/1.4.12 (non-interactive; macos-aarch64; "): "Required Meta Muse CLI User-Agent header prefix",
    ("llm/providers/muse/oauth.py", "muse-api-key:invalid"): "Redis invalidation flag key for Muse API key",
    ("llm/providers/muse/oauth.py", "muse-api-key:v1"): "Redis cache key for minted Muse API key",
    ("llm/providers/muse/oauth.py", "muse-build/0.1.0"): "Required Meta Muse OAuth User-Agent header",
    ("llm/providers/muse/oauth.py", "muse-code/launcher-1.4.12"): "Required Meta Muse OAuth launcher header",
    # Fallback nominal rate table in budget/service.py when models.json price card is unavailable
    ("budget/service.py", "claude-opus-5-5-high-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "claude-opus-5-5-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "claude-sonnet-5-5-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "gemini-3.8-flash-low-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "gemini-3.8-flash-medium-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "gemini-4-argon-high-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "gpt-6-astra-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "gpt-6.1-sol-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    ("budget/service.py", "muse-spark-1.3-oauth"): "Fallback nominal rate in _notional_price_index when models.json price card is unavailable",
    # Default model field on ChatAntigravity LangChain wrapper
    ("llm/providers/antigravity/client.py", "gemini-4-argon-medium"): "Default model attribute on ChatAntigravity LangChain wrapper",
}


def test_no_hardcoded_model_names_in_dalton_python_sources() -> None:
    """Scan src/dalton/**/*.py (excluding migrations) for hardcoded model literals."""
    violations: list[str] = []
    for py_path in sorted(DALTON_SRC_DIR.rglob("*.py")):
        rel = py_path.relative_to(DALTON_SRC_DIR).as_posix()
        if rel.startswith("migrations/") or "__pycache__" in rel:
            continue
        source = py_path.read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            for match in _MODEL_LITERAL_RE.finditer(line):
                literal = match.group(0)[1:-1]
                if (rel, literal) not in HARDCODED_LITERAL_ALLOWLIST:
                    violations.append(f"src/dalton/{rel}:{lineno}: {match.group(0)}")

    assert violations == [], (
        "Found non-allowlisted hardcoded model literals in src/dalton/**/*.py:\n"
        + "\n".join(violations)
    )


def test_settings_defaults_empty_when_dalton_yaml_missing_and_single_assist_source() -> None:
    """Verify DaltonConfig() has empty model defaults and agent_config.dalton.yaml does not define llm.assist."""
    empty_cfg = DaltonConfig()
    assert empty_cfg.models.assist_model == ""
    assert empty_cfg.models.assist_fallback_model == []
    assert empty_cfg.publish.audit_model == ""
    assert empty_cfg.publish.audit_fallback_models == []
    assert empty_cfg.publish.audit_timeout_s == 300.0
    assert empty_cfg.publish.audit_concurrency == 4
    assert empty_cfg.publish.audit_total_timeout_s == 1800.0
    assert empty_cfg.publish.reviewer_models == []
    assert empty_cfg.conversations.extractor_model == ""
    assert empty_cfg.conversations.prompt_version == "conv_card_v2_3"
    assert empty_cfg.conversations.embedding_model == ""
    assert empty_cfg.conversations.embedding_model_preferred == ""

    # agent_config.dalton.yaml must not define llm.assist (single source in src/dalton/dalton.yaml)
    agent_dalton_cfg = yaml.safe_load(
        (REPO_ROOT / "agent_config.dalton.yaml").read_text(encoding="utf-8")
    )
    assert "assist" not in (agent_dalton_cfg.get("llm") or {})

    # src/dalton/dalton.yaml defines the defaults
    real_settings = settings_mod.get_dalton_settings()
    assert real_settings.models.assist_model != ""
    assert len(settings_mod.get_assist_fallback_models()) >= 1
    assert "ocb-deepseek-deepseek-flash" not in settings_mod.get_assist_fallback_models()
    assert real_settings.publish.audit_model == "gemini-4-argon-medium-oauth"
    assert real_settings.publish.audit_fallback_models == [
        "gpt-6.1-sol-oauth",
        "claude-opus-5-5-oauth",
    ]
    assert real_settings.publish.audit_timeout_s == 300.0
    assert 60 <= real_settings.publish.audit_timeout_s <= 1800
    assert len(real_settings.publish.reviewer_models) >= 1
    assert real_settings.conversations.extractor_model == "deepseek-flash"
    assert not hasattr(real_settings.publish, "extract_model")

    sys_defaults = mp_mod.get_dalton_system_defaults()
    assert sys_defaults["dalton_audit_model"] == "gemini-4-argon-medium-oauth"
    assert sys_defaults["dalton_audit_fallback_models"] == [
        "gpt-6.1-sol-oauth",
        "claude-opus-5-5-oauth",
    ]
    assert sys_defaults["dalton_audit_timeout_s"] == 300
    assert sys_defaults["dalton_extract_model"] == "deepseek-flash"


@pytest.mark.asyncio
async def test_dalton_model_pref_precedence_user_then_holder_then_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify precedence: user's model_preference -> first PM holder's model_preference -> dalton.yaml default."""
    prefs_by_user: dict[str, dict[str, Any]] = {
        "user-member": {
            "model_preference": {
                "dalton_assist_model": "gpt-6.1-sol-oauth",
                "dalton_audit_timeout_s": 420,
            }
        },
        "user-pm-holder": {
            "model_preference": {
                "dalton_assist_model": "claude-sonnet-5-5-oauth",
                "dalton_audit_model": "claude-opus-5-5-oauth",
                "dalton_reviewer_models": ["grok-4.7-oauth", "gpt-6.1-sol-oauth"],
                "dalton_audit_timeout_s": 600,
            }
        },
    }

    async def _fake_get_prefs(uid: str) -> dict[str, Any] | None:
        return prefs_by_user.get(uid)

    mock_get_prefs = create_autospec(mp_mod.get_user_preferences, side_effect=_fake_get_prefs)
    mock_holder = create_autospec(
        mp_mod.get_team_credential_holder_id, return_value="user-pm-holder"
    )
    monkeypatch.setattr(mp_mod, "get_user_preferences", mock_get_prefs)
    monkeypatch.setattr(mp_mod, "get_team_credential_holder_id", mock_holder)

    # 1. Member set dalton_assist_model and dalton_audit_timeout_s -> member's own setting wins over PM holder
    assert (
        await mp_mod.dalton_model_pref("user-member", "dalton_assist_model")
        == "gpt-6.1-sol-oauth"
    )
    assert (
        await mp_mod.dalton_model_pref("user-member", "dalton_audit_timeout_s") == 420
    )

    # 2. Member did NOT set dalton_audit_model or dalton_reviewer_models -> PM holder's setting wins over dalton.yaml
    assert (
        await mp_mod.dalton_model_pref("user-member", "dalton_audit_model")
        == "claude-opus-5-5-oauth"
    )
    assert await mp_mod.dalton_model_pref("user-member", "dalton_reviewer_models") == [
        "grok-4.7-oauth",
        "gpt-6.1-sol-oauth",
    ]

    # 3. Neither member nor PM holder set dalton_extract_model -> uses conversations.extractor_model (not models.assist_model)
    real_cfg = settings_mod.get_dalton_settings()
    expected_extract = real_cfg.conversations.extractor_model
    assert expected_extract == "deepseek-flash"
    assert expected_extract != real_cfg.models.assist_model
    assert (
        await mp_mod.dalton_model_pref("user-member", "dalton_extract_model")
        == "deepseek-flash"
    )
    assert (
        await assist_mod.resolve_assist_model(role="extract", user_id="user-member")
        == "deepseek-flash"
    )

    # When conversations.extractor_model is empty in config, it does NOT fall back to models.assist_model
    cfg_without_extract = real_cfg.model_copy(
        update={
            "conversations": real_cfg.conversations.model_copy(
                update={"extractor_model": ""}
            )
        }
    )
    monkeypatch.setattr(
        mp_mod,
        "get_dalton_settings",
        create_autospec(mp_mod.get_dalton_settings, return_value=cfg_without_extract),
    )
    assert await mp_mod.dalton_model_pref("user-member", "dalton_extract_model") is None
    assert (
        await assist_mod.resolve_assist_model(role="extract", user_id="user-member")
        == ""
    )


@pytest.mark.asyncio
async def test_assist_and_reviewer_use_dalton_model_pref_and_fail_cleanly_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify resolve_assist_model, resolve_assist_fallback_models, assist_json, and select_independent_reviewer use dalton_model_pref."""
    prefs_by_user: dict[str, dict[str, Any]] = {
        "user-custom": {
            "model_preference": {
                "dalton_assist_model": "gpt-6.1-sol-oauth",
                "dalton_assist_fallback_models": ["grok-4.7-oauth"],
                "dalton_extract_model": "claude-sonnet-5-5-oauth",
                "dalton_audit_model": "claude-opus-5-5-oauth",
                "dalton_audit_fallback_models": ["grok-4.7-oauth", "gpt-6.1-sol-oauth"],
                "dalton_reviewer_models": ["grok-4.7-oauth", "claude-opus-5-5-oauth"],
            }
        }
    }

    mock_get_prefs = create_autospec(
        mp_mod.get_user_preferences,
        side_effect=lambda uid: prefs_by_user.get(uid),
    )
    mock_holder = create_autospec(mp_mod.get_team_credential_holder_id, return_value="")
    monkeypatch.setattr(mp_mod, "get_user_preferences", mock_get_prefs)
    monkeypatch.setattr(mp_mod, "get_team_credential_holder_id", mock_holder)

    assert (
        await assist_mod.resolve_assist_model(user_id="user-custom")
        == "gpt-6.1-sol-oauth"
    )
    assert (
        await assist_mod.resolve_assist_model(role="extract", user_id="user-custom")
        == "claude-sonnet-5-5-oauth"
    )
    assert (
        await assist_mod.resolve_assist_model(role="audit", user_id="user-custom")
        == "claude-opus-5-5-oauth"
    )
    assert await assist_mod.resolve_assist_fallback_models(user_id="user-custom") == [
        "grok-4.7-oauth"
    ]
    assert await assist_mod.resolve_assist_fallback_models(
        role="audit_fallback", user_id="user-custom"
    ) == [
        "grok-4.7-oauth",
        "gpt-6.1-sol-oauth",
    ]

    # select_independent_reviewer reads user-custom's dalton_reviewer_models (picks grok-4.7-oauth for claude author)
    chosen_rev = await rp_mod.select_independent_reviewer(
        "claude-opus-5-5-oauth", user_id="user-custom"
    )
    assert chosen_rev == "grok-4.7-oauth"
    assert not hasattr(mp_mod, "dalton_model_pref_sync")

    # When dalton.yaml is empty/missing and user has no preferences, resolve_assist_model returns ""
    # and assist_json reports "未配置" without calling dalton_complete.
    monkeypatch.setattr(
        mp_mod,
        "get_dalton_settings",
        create_autospec(mp_mod.get_dalton_settings, return_value=DaltonConfig()),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_assist_fallback_models",
        create_autospec(mp_mod.get_assist_fallback_models, return_value=[]),
    )
    assert await assist_mod.resolve_assist_model(user_id="user-empty") == ""

    mock_complete = create_autospec(assist_mod.dalton_complete)
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete)
    res = await assist_mod.assist_json(
        task="test.unconfigured",
        instructions="Return JSON",
        payload={"k": "v"},
        schema={"type": "object"},
        user_id="user-empty",
    )
    assert res.ok is False
    assert res.error_kind == "unconfigured_model"
    assert "未配置" in (res.error or "")
    mock_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_pm_dalton_reviewer_models_used_by_publish_run_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PM sets dalton_reviewer_models, publish.review.run_review uses it end-to-end without mocking the preference/selection chain."""
    from src.dalton.llm import complete as complete_mod
    from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
    from src.dalton.publish.review import ReviewBundle, ReviewVerdict, run_review

    prefs_by_user: dict[str, dict[str, Any]] = {
        "user-analyst": {"model_preference": {}},
        "user-pm-holder": {
            "model_preference": {
                # dalton.yaml has gpt-6.1-sol-oauth first; PM overrides to grok-4.7-oauth first
                "dalton_reviewer_models": ["grok-4.7-oauth", "gpt-6.1-sol-oauth"],
            }
        },
    }

    mock_get_prefs = create_autospec(
        mp_mod.get_user_preferences,
        side_effect=lambda uid: prefs_by_user.get(uid),
    )
    mock_holder = create_autospec(
        mp_mod.get_team_credential_holder_id, return_value="user-pm-holder"
    )
    monkeypatch.setattr(mp_mod, "get_user_preferences", mock_get_prefs)
    monkeypatch.setattr(mp_mod, "get_team_credential_holder_id", mock_holder)

    called_models: list[str] = []

    async def _fake_complete(
        llm_service: Any = None,
        *,
        user_id: str | None = None,
        system_prompt: str | None = None,
        user_prompt: str = "",
        scope_ref: str | None = None,
        purpose: str = "dalton.complete",
        model: str | None = None,
        request_model: str | None = None,
        response_schema: Any = None,
        slot: str = "primary",
        reasoning_effort: str | None = None,
        timeout_s: float = 60.0,
        is_byok: bool = False,
        disable_tracing: bool = False,
        workspace_id: str | None = None,
        thread_id: str | None = None,
        stale_after_s: float = 360.0,
        resend_indeterminate: bool = False,
        raise_on_error: bool = False,
    ) -> DaltonCompleteResult:
        eff = str(request_model or model or "")
        called_models.append(eff)
        return DaltonCompleteResult(
            ok=True,
            status="done",
            model=eff,
            data=ReviewVerdict(verdict="pass", summary="Approved by PM reviewer list"),
            usage={"input_tokens": 50, "output_tokens": 10},
            cash_usd=0.0,
            notional_usd=0.01,
            invocation_id="lj1:pm-rev",
        )

    mock_complete = create_autospec(dalton_complete, side_effect=_fake_complete)
    monkeypatch.setattr(complete_mod, "dalton_complete", mock_complete)

    bundle = ReviewBundle(
        title="META 3Q26",
        kind="earnings",
        draft_markdown="# Draft",
        audit_summary={},
    )
    res = await run_review(
        user_id="user-analyst",
        author_model="claude-opus-5-5-oauth",
        bundle=bundle,
        available_models={
            "grok-4.7-oauth",
            "gpt-6.1-sol-oauth",
            "claude-opus-5-5-oauth",
        },
    )
    assert res.status == "done"
    assert res.model == "grok-4.7-oauth"
    assert res.family == "xai-grok"
    assert called_models == ["grok-4.7-oauth"]


@pytest.mark.asyncio
async def test_restore_probe_models_budget_fallback_and_complete_empty_model_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify probe_models config lookup, budget fallback nominal rates, and dalton_complete empty-model guard."""
    from src.dalton.budget import service as budget_mod
    from src.dalton.llm import complete as complete_mod
    from src.dalton.services import provider_status as ps_mod

    # 1. _pick_probe_model reads from dalton.yaml models.probe_models table
    assert (
        ps_mod._pick_probe_model(
            "claude-oauth", ["claude-sonnet-5-5-oauth", "claude-opus-5-5-oauth"], {}
        )
        == "claude-sonnet-5-5-oauth"
    )
    assert (
        ps_mod._pick_probe_model(
            "codex-oauth", ["gpt-6-astra-oauth", "gpt-6.1-sol-oauth"], {}
        )
        == "gpt-6.1-sol-oauth"
    )

    # 2. _notional_price_index retains fallback nominal rates when price card is empty/missing
    monkeypatch.setattr(
        budget_mod,
        "load_overlay_data",
        create_autospec(budget_mod.load_overlay_data, return_value={}),
    )
    monkeypatch.setattr(
        budget_mod,
        "load_dalton_meta",
        create_autospec(budget_mod.load_dalton_meta, return_value={}),
    )
    rates = budget_mod._notional_price_index()
    assert rates["claude-opus-5-5-oauth"]["input"] == 5.0
    assert rates["claude-opus-5-5-oauth"]["output"] == 25.0
    assert rates["gpt-6.1-sol-oauth"]["input"] == 2.0
    assert rates["gpt-6.1-sol-oauth"]["output"] == 10.0

    # 3. dalton_complete raises ValueError immediately when model is omitted and config default is empty
    monkeypatch.setattr(
        rp_mod,
        "_load_default_agent_config",
        create_autospec(rp_mod._load_default_agent_config, return_value={"llm": {}}),
    )
    monkeypatch.setattr(
        settings_mod,
        "get_dalton_settings",
        create_autospec(settings_mod.get_dalton_settings, return_value=DaltonConfig()),
    )
    with pytest.raises(ValueError, match="默认模型未配置"):
        await complete_mod.dalton_complete(user_id="u1", user_prompt="hello")


def test_apply_role_models_script_dry_run_and_normal_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify scripts/dalton/apply_role_models.py writes valid catalog model names in --dry-run and normal mode."""
    import json
    import scripts.dalton.apply_role_models as apply_mod
    from src.llms.llm import ModelConfig

    catalog_keys = set((ModelConfig().llm_config or {}).keys())
    orig_md = apply_mod.REVIEWER_MD_PATH.read_text(encoding="utf-8")
    temp_md = tmp_path / "dalton-reviewer.md"
    temp_md.write_text(
        orig_md.replace("model: gpt-6.1-sol-oauth", "model: placeholder-model"),
        encoding="utf-8",
    )

    monkeypatch.setattr(apply_mod, "REVIEWER_MD_PATH", temp_md)
    monkeypatch.setattr(
        apply_mod,
        "explain_model_availability",
        create_autospec(apply_mod.explain_model_availability, return_value={}),
    )

    # 1. --dry-run outputs valid model names and does not modify file on disk
    rc_dry = apply_mod.main(["--dry-run"])
    assert rc_dry == 0
    out_dry = json.loads(capsys.readouterr().out)
    assert out_dry["subagent_reviewer_model"] in catalog_keys
    assert out_dry["server_reviewer_model"] in catalog_keys
    assert out_dry["reviewer_md_updated"] is False
    assert "model: placeholder-model" in temp_md.read_text(encoding="utf-8")

    # 2. Normal mode writes the selected valid model name into dalton-reviewer.md frontmatter
    rc_normal = apply_mod.main([])
    assert rc_normal == 0
    out_normal = json.loads(capsys.readouterr().out)
    chosen = out_normal["subagent_reviewer_model"]
    assert chosen in catalog_keys
    assert out_normal["reviewer_md_updated"] is True
    updated_md = temp_md.read_text(encoding="utf-8")
    assert f"model: {chosen}" in updated_md
    assert "coroutine" not in updated_md


@pytest.mark.asyncio
async def test_provider_status_uses_dalton_model_pref_for_assist_and_reviewer_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify get_providers_status resolves assist, assist_fallback, and reviewer_models from dalton_model_pref."""
    from src.dalton.services import provider_status as ps_mod

    # In dalton.yaml, assist_fallback_model includes deepseek-flash (family 'deepseek').
    # If the user overrides both dalton_assist_fallback_models=['grok-4.7-oauth'] (family 'xai-grok')
    # and dalton_reviewer_models=['deepseek-flash'] (family 'deepseek'), comparing user reviewer_models
    # against yaml assist_fallback_model would falsely emit 'assist_fallback_same_family'.
    # When both come from dalton_model_pref, there is no same-family conflict.
    prefs_by_user: dict[str, dict[str, Any]] = {
        "user-status": {
            "model_preference": {
                "dalton_assist_model": "gemini-3.8-flash-low-oauth",
                "dalton_assist_fallback_models": ["grok-4.7-oauth"],
                "dalton_reviewer_models": ["deepseek-flash", "gpt-6.1-sol-oauth"],
            }
        }
    }
    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(
            mp_mod.get_user_preferences, side_effect=lambda uid: prefs_by_user.get(uid)
        ),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_team_credential_holder_id",
        create_autospec(mp_mod.get_team_credential_holder_id, return_value=""),
    )
    monkeypatch.setattr(
        ps_mod,
        "is_broker_models_enabled",
        create_autospec(ps_mod.is_broker_models_enabled, return_value=False),
    )
    monkeypatch.setattr(
        ps_mod,
        "explain_model_availability",
        create_autospec(
            ps_mod.explain_model_availability,
            return_value={
                "claude-opus-5-5-oauth": {"available": True},
                "gemini-3.8-flash-low-oauth": {"available": True},
                "grok-4.7-oauth": {"available": True},
                "deepseek-flash": {"available": True},
                "gpt-6.1-sol-oauth": {"available": True},
                "ocb-xai-grok-4.7": {"available": True},
            },
        ),
    )

    status_payload = await ps_mod.get_providers_status(user_id="user-status")
    same_fam_warnings = [
        w
        for w in status_payload["role_warnings"]
        if w.get("code") == "assist_fallback_same_family"
    ]
    assert same_fam_warnings == []




@pytest.mark.asyncio
async def test_dalton_pref_shadowed_by_later_custom_model_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Saved while the name was a catalog model; a later custom_models-only
    # patch (which skips Dalton validation) redefines the same name.
    prefs_by_user: dict[str, Any] = {
        "user-member": {
            "model_preference": {
                "dalton_audit_model": "gpt-6.1-sol-oauth",
                "dalton_reviewer_models": ["gpt-6.1-sol-oauth", "grok-4.7-oauth"],
                "custom_models": [{"name": "gpt-6.1-sol-oauth", "model_id": "x"}],
            }
        },
        "user-pm-holder": {"model_preference": {"dalton_audit_model": "claude-opus-5-5-oauth"}},
    }

    async def _fake_get_prefs(uid: str) -> dict[str, Any] | None:
        return prefs_by_user.get(uid)

    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(mp_mod.get_user_preferences, side_effect=_fake_get_prefs),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_team_credential_holder_id",
        create_autospec(mp_mod.get_team_credential_holder_id, return_value="user-pm-holder"),
    )

    assert (
        await mp_mod.dalton_model_pref("user-member", "dalton_audit_model")
        == "claude-opus-5-5-oauth"
    )
    assert await mp_mod.dalton_model_pref("user-member", "dalton_reviewer_models") == [
        "grok-4.7-oauth"
    ]

    # Caller's custom_models also shadows PM-inherited and yaml-inherited models:
    # (a) PM holder's dalton_audit_model ('claude-opus-5-5-oauth') is shadowed by caller's custom_models
    #     -> skips PM and falls through to yaml ('gemini-4-argon-medium-oauth');
    #     PM holder's fallback list filters out caller-shadowed entries.
    prefs_by_user["user-member"]["model_preference"] = {
        "custom_models": [
            {"name": "claude-opus-5-5-oauth", "model_id": "custom-opus"},
        ],
    }
    prefs_by_user["user-pm-holder"]["model_preference"] = {
        "dalton_audit_model": "claude-opus-5-5-oauth",
        "dalton_audit_fallback_models": ["claude-opus-5-5-oauth", "gpt-6.1-sol-oauth"],
    }
    assert (
        await mp_mod.dalton_model_pref("user-member", "dalton_audit_model")
        == "gemini-4-argon-medium-oauth"
    )
    assert await mp_mod.dalton_model_pref(
        "user-member", "dalton_audit_fallback_models"
    ) == ["gpt-6.1-sol-oauth"]

    # (b) Both PM holder's model and yaml default ('gemini-4-argon-medium-oauth') are shadowed by
    #     caller's custom_models -> returns None / "" and assist_json returns unconfigured_model;
    #     yaml fallback list also filters out caller-shadowed entries.
    prefs_by_user["user-member"]["model_preference"] = {
        "custom_models": [
            {"name": "claude-opus-5-5-oauth", "model_id": "custom-opus"},
            {"name": "gemini-4-argon-medium-oauth", "model_id": "custom-argon"},
        ],
    }
    prefs_by_user["user-pm-holder"]["model_preference"] = {
        "dalton_audit_model": "claude-opus-5-5-oauth",
    }
    assert await mp_mod.dalton_model_pref("user-member", "dalton_audit_model") is None
    assert await mp_mod.resolve_dalton_audit_model("user-member") == ""
    assert await mp_mod.resolve_dalton_audit_fallback_models("user-member") == [
        "gpt-6.1-sol-oauth"
    ]
    mock_complete = create_autospec(assist_mod.dalton_complete)
    monkeypatch.setattr(assist_mod, "dalton_complete", mock_complete)
    shadowed_res = await assist_mod.assist_json(
        task="audit.judge",
        instructions="Judge figures",
        payload={"items": []},
        schema={"type": "object"},
        user_id="user-member",
        role="audit",
    )
    assert shadowed_res.ok is False
    assert shadowed_res.error_kind == "unconfigured_model"
    mock_complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_yaml_audit_timeout_s_preserves_positive_float_without_user_range_clamping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify yaml audit_timeout_s keeps positive float values (e.g. 30 or 300.5) without 60-1800 clamping or int rounding."""
    orig_get_prefs = mp_mod.get_user_preferences
    orig_get_settings = mp_mod.get_dalton_settings

    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(orig_get_prefs, return_value=None),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_team_credential_holder_id",
        create_autospec(mp_mod.get_team_credential_holder_id, return_value=""),
    )

    real_cfg = orig_get_settings()

    # 1. yaml audit_timeout_s = 30 (below user setting minimum 60) is preserved as 30
    cfg_30 = real_cfg.model_copy(
        update={"publish": real_cfg.publish.model_copy(update={"audit_timeout_s": 30})}
    )
    monkeypatch.setattr(
        mp_mod,
        "get_dalton_settings",
        create_autospec(orig_get_settings, return_value=cfg_30),
    )
    assert mp_mod.dalton_model_default("dalton_audit_timeout_s") == 30
    assert await mp_mod.resolve_dalton_audit_timeout_s("user-1") == 30

    # 2. yaml audit_timeout_s = 300.5 (fractional float) is preserved as 300.5 without int() truncation
    cfg_300_5 = real_cfg.model_copy(
        update={
            "publish": real_cfg.publish.model_copy(update={"audit_timeout_s": 300.5})
        }
    )
    monkeypatch.setattr(
        mp_mod,
        "get_dalton_settings",
        create_autospec(orig_get_settings, return_value=cfg_300_5),
    )
    val_default = mp_mod.dalton_model_default("dalton_audit_timeout_s")
    val_resolved = await mp_mod.resolve_dalton_audit_timeout_s("user-1")
    assert val_default == 300.5
    assert val_resolved == 300.5
    assert isinstance(val_resolved, float)
    assert mp_mod.get_dalton_system_defaults()["dalton_audit_timeout_s"] == 300.5

    # 3. User-stored timeout outside 60..1800 is still rejected and falls back to yaml (300.5)
    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(
            orig_get_prefs,
            return_value={"model_preference": {"dalton_audit_timeout_s": 30}},
        ),
    )
    assert await mp_mod.resolve_dalton_audit_timeout_s("user-1") == 300.5


@pytest.mark.asyncio
async def test_extract_segment_cards_and_archive_pipeline_use_resolved_extract_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify conversation archive extraction resolves model via resolve_dalton_extract_model (explicit -> user -> PM -> yaml)."""
    import scripts.dalton.conversation_archive as archive_cli
    from src.dalton.conversations import extractor as extractor_mod

    prefs_by_user: dict[str, dict[str, Any]] = {
        "user-owner": {
            "model_preference": {
                "dalton_extract_model": "gpt-6.1-sol-oauth",
            }
        }
    }

    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(
            mp_mod.get_user_preferences, side_effect=lambda uid: prefs_by_user.get(uid)
        ),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_team_credential_holder_id",
        create_autospec(mp_mod.get_team_credential_holder_id, return_value="user-owner"),
    )
    monkeypatch.setattr(
        archive_cli,
        "archive_billing_user_id",
        create_autospec(archive_cli.archive_billing_user_id, return_value="user-owner"),
    )

    seen_models: list[str | None] = []

    async def _stub_assist(*args: Any, **kwargs: Any) -> assist_mod.AssistResult:
        m = kwargs.get("model")
        seen_models.append(m)
        return assist_mod.AssistResult(
            ok=True,
            status="ok",
            data={"category": "private", "topic_label": "t", "cards": []},
            model=str(m or ""),
            usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    monkeypatch.setattr(
        extractor_mod,
        "assist_json",
        create_autospec(assist_mod.assist_json, side_effect=_stub_assist),
    )

    seg = [
        {
            "agent": "main",
            "session_id": "s1",
            "event_id": "e1",
            "seq": 1,
            "ts": "2026-10-23T10:00:00+00:00",
            "speaker": {"role": "owner", "is_owner": True},
            "text": "test",
        }
    ]

    # 1. User set dalton_extract_model -> extract_segment_cards and run_archive_pipeline use user's model
    out_user = await extractor_mod.extract_segment_cards(seg, user_id="user-owner")
    assert out_user.ok is True
    assert out_user.model_used == "gpt-6.1-sol-oauth"
    assert seen_models[-1] == "gpt-6.1-sol-oauth"

    summary_user = await archive_cli.run_archive_pipeline(
        mode="run",
        dry_run=True,
        skip_host_export=True,
        inbox_dir=REPO_ROOT / "var" / "nonexistent_inbox_for_test",
    )
    assert summary_user["extractor_model"] == "gpt-6.1-sol-oauth"

    # 2. Explicit extractor_model has highest priority over user setting
    out_explicit = await extractor_mod.extract_segment_cards(
        seg, user_id="user-owner", extractor_model="claude-sonnet-5-5-oauth"
    )
    assert out_explicit.model_used == "claude-sonnet-5-5-oauth"
    assert seen_models[-1] == "claude-sonnet-5-5-oauth"

    summary_explicit = await archive_cli.run_archive_pipeline(
        mode="run",
        dry_run=True,
        skip_host_export=True,
        extractor_model="claude-sonnet-5-5-oauth",
        inbox_dir=REPO_ROOT / "var" / "nonexistent_inbox_for_test",
    )
    assert summary_explicit["extractor_model"] == "claude-sonnet-5-5-oauth"

    # 3. When user and PM have no setting -> falls back to yaml default ('deepseek-flash')
    prefs_by_user.clear()
    out_default = await extractor_mod.extract_segment_cards(seg, user_id="user-owner")
    assert out_default.model_used == "deepseek-flash"
    assert seen_models[-1] == "deepseek-flash"

    summary_default = await archive_cli.run_archive_pipeline(
        mode="run",
        dry_run=True,
        skip_host_export=True,
        inbox_dir=REPO_ROOT / "var" / "nonexistent_inbox_for_test",
    )
    assert summary_default["extractor_model"] == "deepseek-flash"


@pytest.mark.asyncio
async def test_audit_entry_points_use_user_and_pm_dalton_audit_preferences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify aaudit_draft, label_figures_with_status, and judge_figures_batch_with_llm resolve audit model/fallbacks/timeout via model_prefs."""
    from src.dalton.publish import audit as audit_mod
    from src.dalton.publish import numbers as numbers_mod

    prefs_by_user: dict[str, dict[str, Any]] = {
        "user-analyst": {
            "model_preference": {
                "dalton_audit_timeout_s": 240,
            }
        },
        "user-pm-holder": {
            "model_preference": {
                "dalton_audit_model": "claude-opus-5-5-oauth",
                "dalton_audit_fallback_models": ["grok-4.7-oauth"],
                "dalton_audit_timeout_s": 600,
            }
        },
    }

    async def _fake_get_prefs(uid: str) -> dict[str, Any] | None:
        return prefs_by_user.get(uid)

    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(mp_mod.get_user_preferences, side_effect=_fake_get_prefs),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_team_credential_holder_id",
        create_autospec(mp_mod.get_team_credential_holder_id, return_value="user-pm-holder"),
    )

    seen_calls: list[dict[str, Any]] = []

    async def _stub_assist(*args: Any, **kwargs: Any) -> assist_mod.AssistResult:
        seen_calls.append({"task": args[0] if args else kwargs.get("task"), **kwargs})
        return assist_mod.AssistResult(
            ok=False, data=None, error="stub", model=str(kwargs.get("model"))
        )

    stub = create_autospec(assist_mod.assist_json, side_effect=_stub_assist)
    monkeypatch.setattr(numbers_mod, "assist_json", stub)
    monkeypatch.setattr(audit_mod, "assist_json", stub)

    draft = "## 业绩\n公司2Q26广告收入为512亿美元，同比增长21%。\n"
    prov = {
        "P1": {
            "body": "2Q26 advertising revenue was $51.2 billion, up 21% year over year.",
            "kind": "sec",
        }
    }
    await audit_mod.aaudit_draft(
        draft, provenance_records=prov, use_llm_judge=True, user_id="user-analyst"
    )
    assert {c["task"] for c in seen_calls} == {"audit.label_figures", "audit.judge"}
    for call in seen_calls:
        assert call["model"] == "claude-opus-5-5-oauth"
        assert call["fallback_models"] == ["grok-4.7-oauth"]
        assert call["timeout_s"] == 240
        assert call["deadline"] is not None



@pytest.mark.asyncio
async def test_pm_only_custom_model_does_not_shadow_a_members_inherited_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The PM later defines a custom model with the same name as their saved
    # system choice; a member without that custom model still inherits it,
    # because the call resolves the name as the member.
    prefs_by_user: dict[str, Any] = {
        "user-member": {"model_preference": {}},
        "user-pm-holder": {
            "model_preference": {
                "dalton_audit_model": "gpt-6.1-sol-oauth",
                "custom_models": [{"name": "gpt-6.1-sol-oauth", "model_id": "x"}],
            }
        },
    }

    async def _fake_get_prefs(uid: str) -> dict[str, Any] | None:
        return prefs_by_user.get(uid)

    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(mp_mod.get_user_preferences, side_effect=_fake_get_prefs),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_team_credential_holder_id",
        create_autospec(mp_mod.get_team_credential_holder_id, return_value="user-pm-holder"),
    )

    assert (
        await mp_mod.dalton_model_pref("user-member", "dalton_audit_model")
        == "gpt-6.1-sol-oauth"
    )
