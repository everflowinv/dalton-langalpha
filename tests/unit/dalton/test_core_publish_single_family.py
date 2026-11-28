"""Unit tests for Standalone Core Batch 5 (Rounds 1 & 2):
- Unified effective role model resolution (availability checks, skipping stale/forbidden, no agent_config fallback)
- Evidence-based tool_calling metadata, cached annotation performance, and Core /api/v1/keys/test tool probe
- Config-based model family resolution (anti-spoofing for custom gateways)
- Single-family / unknown-family / two-family formal review & retry exclusion
- Core 2-round check_language gate on publish (Everflow unchanged)
- Archived publication banner & approval payload review_mode rendering
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import create_autospec

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from ptc_agent.config.agent import AgentConfig, LLMConfig
from ptc_agent.config.core import (
    DaytonaConfig,
    FilesystemConfig,
    LoggingConfig,
    MCPConfig,
    SandboxConfig,
    SecurityConfig,
)
from src.config.core import clear_config_cache
from src.dalton.database import publications as db_publications
from src.dalton.llm import assist as assist_mod
from src.dalton.llm import complete as complete_mod
from src.dalton.llm import model_prefs as mp_mod
from src.dalton.llm import role_policy as rp_mod
from src.dalton.llm import tool_probe as probe_mod
from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
from src.dalton.llm.role_policy import (
    RolePolicyError,
    annotate_model_metadata_tool_calling,
    check_roles,
    clear_tool_capability_cache,
    enforce_role_policy_on_startup,
    family_of_for_config,
    resolve_customer_configured_model,
    resolve_effective_role_model,
    resolve_model_family,
)
from src.dalton.publish import audit as audit_mod
from src.dalton.publish import files as publish_files
from src.dalton.publish import language as language_mod
from src.dalton.publish import review as review_mod
from src.dalton.publish import service as publish_svc
from src.dalton.publish.review import (
    ReviewBundle,
    ReviewCheckItem,
    ReviewVerdict,
    classify_review_mode,
    pick_reviewer_model,
    run_review,
)
from src.dalton.settings import get_dalton_config
from tests.conftest import create_test_app


def _set_profile(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_env: str | None,
    dalton_profile: str | None = None,
) -> None:
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    if dalton_profile is None:
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
    else:
        monkeypatch.setenv("DALTON_PROFILE", dalton_profile)
    clear_config_cache()
    get_dalton_config.cache_clear()
    clear_tool_capability_cache()
    probe_mod.clear_tool_probe_cache()


def _nine_items(result: str = "pass") -> list[ReviewCheckItem]:
    return [ReviewCheckItem(check=n, result=result, reason="ok") for n in range(1, 10)]


def _sample_bundle(draft: str = "# Draft\nRevenue was stable.") -> ReviewBundle:
    return ReviewBundle(
        title="ACME Q3 Review",
        kind="earnings_review",
        draft_markdown=draft,
        audit_summary={},
    )


def _language_sidecar_json(draft_text: str, *, include_round2: bool = True, override_sha: str | None = None) -> str:
    sha = override_sha or hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
    rounds: list[dict[str, Any]] = [
        {"round": 1, "draft_sha256": "0" * 64, "issues": []},
    ]
    if include_round2:
        rounds.append({"round": 2, "draft_sha256": sha, "issues": []})
    return json.dumps({"rounds": rounds})


from src.dalton import credentials as _cred_mod_orig
from src.server.database import api_keys as _db_api_keys_orig

_ORIG_GET_HOLDER = _cred_mod_orig.get_team_credential_holder_id
_ORIG_GET_USER_API_KEYS = _db_api_keys_orig.get_user_api_keys


def _mock_user_prefs(
    monkeypatch: pytest.MonkeyPatch,
    prefs_by_user: dict[str, dict[str, Any]],
    *,
    keys_by_user: dict[str, dict[str, Any]] | None = None,
    holder_id: str | None = None,
) -> None:
    async def _fake_get_prefs(user_id: str) -> dict[str, Any] | None:
        return prefs_by_user.get(user_id)

    monkeypatch.setattr(
        mp_mod,
        "get_user_preferences",
        create_autospec(mp_mod._DEFAULT_GET_USER_PREFERENCES, side_effect=_fake_get_prefs),
    )
    monkeypatch.setattr(
        mp_mod,
        "get_team_credential_holder_id",
        create_autospec(_ORIG_GET_HOLDER, return_value=holder_id),
    )
    monkeypatch.setattr(
        _cred_mod_orig,
        "get_team_credential_holder_id",
        create_autospec(_ORIG_GET_HOLDER, return_value=holder_id),
    )

    effective_keys = keys_by_user if keys_by_user is not None else {}

    async def _fake_get_keys(user_id: str) -> dict[str, Any]:
        return effective_keys.get(user_id, {"byok_enabled": True, "keys": {}, "base_urls": {}})

    monkeypatch.setattr(
        _db_api_keys_orig,
        "get_user_api_keys",
        create_autospec(_ORIG_GET_USER_API_KEYS, side_effect=_fake_get_keys),
    )


def _patch_complete_pass(
    monkeypatch: pytest.MonkeyPatch,
    recorded_models: list[str],
    *,
    fail_models: set[str] | None = None,
) -> None:
    failing = fail_models or set()

    async def _fake_complete(
        llm_service: Any = None,
        *,
        user_id: str | None = None,
        system_prompt: str | None = None,
        user_prompt: str = "",
        request_model: str | None = None,
        response_schema: Any = None,
        raise_on_error: bool = False,
        **_kwargs: Any,
    ) -> DaltonCompleteResult:
        m = str(request_model or "")
        recorded_models.append(m)
        if m in failing:
            return DaltonCompleteResult(
                ok=False,
                status="failed",
                model=m,
                error=f"simulated failure on {m}",
                error_kind="http_500",
            )
        return DaltonCompleteResult(
            ok=True,
            status="done",
            model=m,
            data=ReviewVerdict(
                verdict="pass",
                summary="审稿通过",
                items=_nine_items("pass"),
                must_fix=[],
                nice_to_have=[],
            ),
            usage={"input_tokens": 100, "output_tokens": 50},
            cash_usd=0.002,
            notional_usd=0.002,
            invocation_id="lj1:test-review-pass",
        )

    monkeypatch.setattr(
        complete_mod,
        "dalton_complete",
        create_autospec(dalton_complete, side_effect=_fake_complete),
    )


def _custom_gw_pref(
    *,
    name: str = "acme-gateway-llm",
    model_id: str = "internal-v1",
    provider: str = "acme-gateway",
    preferred_model: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pref: dict[str, Any] = {
        "preferred_model": preferred_model if preferred_model is not None else name,
        "custom_providers": [
            {"name": provider, "parent_provider": "openai", "base_url": "https://gw.acme.test/v1"}
        ],
        "custom_models": [
            {"name": name, "model_id": model_id, "provider": provider}
        ],
    }
    if extra:
        pref.update(extra)
    return {"model_preference": pref}


def _custom_gw_keys(provider: str = "acme-gateway") -> dict[str, Any]:
    return {
        "byok_enabled": True,
        "keys": {provider: "sk-acme-test-key"},
        "base_urls": {provider: "https://gw.acme.test/v1"},
    }


class TestUnifiedEffectiveRoleModelResolution:
    """Core profile resolves unset assist/audit/language/extract/reviewer roles with availability checks."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("app_env", "dalton_profile"),
        [("core", None), ("", "core")],
    )
    async def test_core_roles_follow_customer_primary_and_background_models(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str | None,
    ) -> None:
        _set_profile(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
        _mock_user_prefs(
            monkeypatch,
            {
                "u-core": {
                    "model_preference": {
                        "preferred_model": "gpt-6.2-astra",
                        "preferred_background_model": "gpt-6-luna",
                    }
                }
            },
            keys_by_user={"u-core": {"byok_enabled": True, "keys": {"openai": "sk-openai-test"}, "base_urls": {}}},
        )

        # Assist prefers background slot; audit, language & reviewer prefer primary slot.
        assert await assist_mod.resolve_assist_model(role="assist", user_id="u-core") == "gpt-6-luna"
        assert await mp_mod.resolve_dalton_assist_model("u-core") == "gpt-6-luna"
        assert await mp_mod.resolve_dalton_audit_model("u-core") == "gpt-6.2-astra"
        assert await mp_mod.resolve_dalton_language_model("u-core") == "gpt-6.2-astra"
        assert await resolve_effective_role_model(role="reviewer", user_id="u-core") == "gpt-6.2-astra"

    @pytest.mark.asyncio
    async def test_core_single_custom_gateway_model_serves_all_roles(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {"u-gateway": _custom_gw_pref()},
            keys_by_user={"u-gateway": _custom_gw_keys()},
        )

        assert await assist_mod.resolve_assist_model(role="assist", user_id="u-gateway") == "acme-gateway-llm"
        assert await mp_mod.resolve_dalton_audit_model("u-gateway") == "acme-gateway-llm"
        assert await mp_mod.resolve_dalton_language_model("u-gateway") == "acme-gateway-llm"
        assert await resolve_effective_role_model(role="reviewer", user_id="u-gateway") == "acme-gateway-llm"

    @pytest.mark.asyncio
    async def test_core_skips_stale_unavailable_and_forbidden_candidates_and_tries_next(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        # Role pref points to a forbidden contributor model, background is an unavailable
        # Anthropic model (no key), primary is a deleted custom alias — falls through cleanly
        # to the working OpenAI custom/BYOK model without stopping on the first broken slot.
        _mock_user_prefs(
            monkeypatch,
            {
                "u-fallback": {
                    "model_preference": {
                        "dalton_assist_model": "muse-spark-1.3-contributor-oauth",
                        "preferred_background_model": "claude-sonnet-5-5",
                        "preferred_model": "deleted-stale-model",
                        "custom_providers": [
                            {"name": "acme-gateway", "parent_provider": "openai", "base_url": "https://gw.acme.test/v1"}
                        ],
                        "custom_models": [
                            {"name": "acme-working", "model_id": "corp-1", "provider": "acme-gateway"}
                        ],
                    }
                }
            },
            keys_by_user={"u-fallback": _custom_gw_keys()},
        )
        assert await assist_mod.resolve_assist_model(role="assist", user_id="u-fallback") == "acme-working"

        # When ONLY a forbidden contributor variant exists (no usable fallback), raise RolePolicyError.
        _mock_user_prefs(
            monkeypatch,
            {
                "u-only-bad": {
                    "model_preference": {
                        "preferred_model": "muse-spark-1.3-contributor-oauth",
                    }
                }
            },
        )
        with pytest.raises(RolePolicyError, match="contributor"):
            await assist_mod.resolve_assist_model(role="assist", user_id="u-only-bad")

    @pytest.mark.asyncio
    async def test_core_resolves_custom_models_under_execution_account_not_caller(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        # Team holder holds the execution credentials & custom model; caller defines a different
        # custom alias that the execution account cannot run. Resolution must pick holder's model.
        _mock_user_prefs(
            monkeypatch,
            {
                "u-caller": _custom_gw_pref(name="caller-only-alias", provider="caller-gw"),
                "u-holder": _custom_gw_pref(name="holder-exec-model", provider="holder-gw"),
            },
            keys_by_user={"u-holder": _custom_gw_keys("holder-gw")},
            holder_id="u-holder",
        )
        assert await resolve_effective_role_model(role="assist", user_id="u-caller") == "holder-exec-model"

    @pytest.mark.asyncio
    async def test_core_empty_config_and_prefs_returns_empty_never_upstream_agent_config_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(monkeypatch, {"u-empty": {"model_preference": {}}})
        # Even if an upstream Anthropic key were present in env, Core MUST NOT pull
        # agent_config.core.yaml's upstream Claude default when customer prefs and dalton.core.yaml are empty.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-be-picked")

        assert await resolve_customer_configured_model("u-empty", prefer_slot="primary") == ""
        assert await resolve_effective_role_model(role="assist", user_id="u-empty") == ""
        assert await resolve_effective_role_model(role="audit", user_id="u-empty") == ""

        # deployment_only=True reads ONLY dalton.core.yaml (empty -> "") and never reads user prefs.
        _mock_user_prefs(
            monkeypatch,
            {"u-has-pref": _custom_gw_pref()},
            keys_by_user={"u-has-pref": _custom_gw_keys()},
        )
        assert await resolve_effective_role_model(role="assist", user_id="u-has-pref", deployment_only=True) == ""
        assert await resolve_customer_configured_model("u-has-pref", deployment_only=True) == ""

    @pytest.mark.asyncio
    async def test_core_deployment_only_with_configured_model_resolves_without_none_crash(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression for codex H2: deployment_only=True with configured model in Core must not pass None to model_resolves."""
        _set_profile(monkeypatch, app_env="core")
        orig_default = mp_mod.dalton_model_default
        monkeypatch.setattr(
            mp_mod,
            "dalton_model_default",
            create_autospec(
                orig_default,
                side_effect=lambda key: "gpt-6.2-astra" if key == "dalton_assist_model" else None,
            ),
        )
        assert await resolve_effective_role_model(role="assist", deployment_only=True) == "gpt-6.2-astra"

        # Skips forbidden contributor model and picks next usable configured deployment model.
        monkeypatch.setattr(
            mp_mod,
            "dalton_model_default",
            create_autospec(
                orig_default,
                side_effect=lambda key: (
                    "muse-spark-1.3-contributor-oauth"
                    if key == "dalton_language_model"
                    else ("gpt-6.2-astra" if key == "dalton_audit_model" else None)
                ),
            ),
        )
        assert await resolve_effective_role_model(role="language", deployment_only=True) == "gpt-6.2-astra"

    @pytest.mark.asyncio
    async def test_everflow_keeps_existing_role_defaults_and_no_primary_fallback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="dalton")
        _mock_user_prefs(
            monkeypatch,
            {
                "u-ef": {
                    "model_preference": {
                        "preferred_model": "acme-gateway-llm",
                    }
                }
            },
        )

        # Everflow reads dalton.yaml role defaults rather than preferred_model.
        assert await mp_mod.resolve_dalton_audit_model("u-ef") == "gemini-4-neon-medium-oauth"
        assert await assist_mod.resolve_assist_model(role="assist", user_id="u-ef") != "acme-gateway-llm"

        # When dalton.yaml has no model configured in Everflow, returns "" (no fallback to preferred_model).
        monkeypatch.setattr(
            mp_mod,
            "dalton_model_default",
            create_autospec(mp_mod.dalton_model_default, return_value=None),
        )
        assert await mp_mod.resolve_dalton_audit_model("u-ef") == ""
        assert await assist_mod.resolve_assist_model(role="assist", user_id="u-ef") == ""


class TestRolePolicyCheckByProfile:
    """Core relaxes cross-family & empty audit_model warnings while Everflow enforces them."""

    def test_core_check_roles_allows_single_family_and_empty_audit_model(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        cfg = {"llm": {"name": "gpt-6.2-astra", "background": "gpt-6.2-astra"}}
        warnings = check_roles(cfg, reviewer_models=[], available_models={"gpt-6.2-astra"})
        codes = {w["code"] for w in warnings}
        assert "reviewer_same_family" not in codes
        assert "audit_model_missing" not in codes
        assert "assist_fallback_same_family" not in codes
        assert enforce_role_policy_on_startup(cfg) == []

        # L21 contributor ban still fails startup in Core.
        bad_cfg = {"llm": {"name": "muse-spark-1.3-contributor-oauth"}}
        with pytest.raises(RolePolicyError):
            enforce_role_policy_on_startup(bad_cfg)

    def test_everflow_check_roles_still_warns_on_missing_cross_family_reviewer(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="dalton")
        cfg = {"llm": {"name": "gpt-6.2-astra-oauth"}}
        warnings = check_roles(cfg, reviewer_models=["gpt-6.2-sol-oauth"])
        codes = {w["code"] for w in warnings}
        assert "reviewer_same_family" in codes


class TestConfigBasedFamilyAndSingleUnknownReview:
    """Config-aware family resolution (anti-spoofing), single/unknown/two-family review, and retry logic."""

    @pytest.mark.asyncio
    async def test_custom_gateway_alias_cannot_spoof_cross_family_in_core_or_everflow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Customer names a custom OpenAI-compatible gateway model "claude-gateway-alias"
        # backed by a GPT model behind a custom URL. Actual inference config must classify
        # it as "unknown" (never "anthropic-claude").
        spoofed_pref = {
            "custom_providers": [
                {"name": "my-gw", "parent_provider": "openai", "base_url": "https://gw.corp.test/v1"}
            ],
            "custom_models": [
                {"name": "claude-gateway-alias", "model_id": "gpt-4o", "provider": "my-gw"}
            ],
        }
        assert family_of_for_config("claude-gateway-alias", model_pref=spoofed_pref) == "unknown"

        # Built-in provider with a custom base_url override also classifies as "unknown".
        assert (
            family_of_for_config(
                "gpt-6.2-astra",
                model_pref={},
                base_urls={"openai": "https://proxy.corp.test/v1"},
            )
            == "unknown"
        )

        # First-party custom model on built-in anthropic provider with real Claude model_id verifies cleanly.
        first_party_pref = {
            "custom_models": [
                {"name": "corp-research-alias", "model_id": "claude-sonnet-4-5", "provider": "anthropic"}
            ]
        }
        assert family_of_for_config("corp-research-alias", model_pref=first_party_pref) == "anthropic-claude"

        # Core review: author gpt-6.2-astra + reviewer claude-gateway-alias -> unknown_family (NOT cross_family).
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {"u-spoof": {"model_preference": spoofed_pref}},
            keys_by_user={"u-spoof": {"byok_enabled": True, "keys": {"openai": "sk-o", "my-gw": "sk-g"}, "base_urls": {"my-gw": "https://gw.corp.test/v1"}}},
        )
        assert await resolve_model_family("claude-gateway-alias", user_id="u-spoof") == "unknown"
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called)
        res_core = await run_review(
            user_id="u-spoof",
            author_model="gpt-6.2-astra",
            bundle=_sample_bundle(),
            candidates=["claude-gateway-alias"],
            available_models={"gpt-6.2-astra", "claude-gateway-alias"},
        )
        assert res_core.status == "done"
        assert res_core.family == "unknown"
        assert res_core.review_mode == "unknown_family"

        # Everflow restores original name-based family_of (never reads user/holder custom config or base_urls).
        _set_profile(monkeypatch, app_env="dalton")
        assert await resolve_model_family("gpt-6.2-astra", user_id="u-spoof") == "openai-gpt"
        assert await resolve_model_family("claude-gateway-alias", user_id="u-spoof") == "anthropic-claude"
        # Unknown-named models are still rejected in Everflow as before.
        res_ef = await run_review(
            user_id="u-spoof",
            author_model="acme-gateway-llm",
            bundle=_sample_bundle(),
            candidates=["gpt-6.2-astra"],
            available_models={"acme-gateway-llm", "gpt-6.2-astra"},
        )
        assert res_ef.status == "unavailable"

    @pytest.mark.asyncio
    async def test_core_single_family_reuses_author_model_and_records_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {"u-core": {"model_preference": {"preferred_model": "gpt-6.2-astra"}}},
            keys_by_user={"u-core": {"byok_enabled": True, "keys": {"openai": "sk-o"}, "base_urls": {}}},
        )
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called)

        assert await pick_reviewer_model(
            "gpt-6.2-astra",
            candidates=["gpt-6.2-astra"],
            available_models={"gpt-6.2-astra"},
            user_id="u-core",
        ) == "gpt-6.2-astra"

        res = await run_review(
            user_id="u-core",
            author_model="gpt-6.2-astra",
            bundle=_sample_bundle(),
            candidates=[],
            available_models={"gpt-6.2-astra"},
        )
        assert res.status == "done"
        assert res.model == "gpt-6.2-astra"
        assert res.family == "openai-gpt"
        assert res.review_mode == "single_family"
        assert res.to_dict()["review_mode"] == "single_family"
        assert called == ["gpt-6.2-astra"]

    @pytest.mark.asyncio
    async def test_core_unknown_family_custom_gateway_completes_review(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {"u-gw": _custom_gw_pref()},
            keys_by_user={"u-gw": _custom_gw_keys()},
        )
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called)

        assert review_mod.family_of("acme-gateway-llm") == "unknown"
        assert await pick_reviewer_model(
            "acme-gateway-llm",
            candidates=[],
            user_id="u-gw",
        ) == "acme-gateway-llm"

        res = await run_review(
            user_id="u-gw",
            author_model="acme-gateway-llm",
            bundle=_sample_bundle(),
            candidates=[],
        )
        assert res.status == "done"
        assert res.model == "acme-gateway-llm"
        assert res.family == "unknown"
        assert res.review_mode == "unknown_family"
        assert res.to_dict()["review_mode"] == "unknown_family"
        assert called == ["acme-gateway-llm"]

    @pytest.mark.asyncio
    async def test_core_review_retry_excludes_tried_models_before_selecting_and_includes_custom_models(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {
                "u-retry": {
                    "model_preference": {
                        "preferred_model": "gpt-6.2-astra",
                        "custom_providers": [
                            {"name": "acme-gateway", "parent_provider": "openai", "base_url": "https://gw.acme.test/v1"}
                        ],
                        "custom_models": [
                            {"name": "acme-backup-llm", "model_id": "corp-backup", "provider": "acme-gateway"}
                        ],
                    }
                }
            },
            keys_by_user={
                "u-retry": {
                    "byok_enabled": True,
                    "keys": {"openai": "sk-o", "acme-gateway": "sk-gw"},
                    "base_urls": {"acme-gateway": "https://gw.acme.test/v1"},
                }
            },
        )
        # First candidate (gpt-6.2-astra) fails; retry excludes tried_models BEFORE selecting
        # and cleanly picks the customer's usable custom model acme-backup-llm!
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called, fail_models={"gpt-6.2-astra"})

        res = await run_review(
            user_id="u-retry",
            author_model="gpt-6.2-astra",
            bundle=_sample_bundle(),
            candidates=["gpt-6.2-astra"],
            available_models={"gpt-6.2-astra", "acme-backup-llm"},
        )
        assert res.status == "done"
        assert called == ["gpt-6.2-astra", "acme-backup-llm"]
        assert res.model == "acme-backup-llm"
        assert res.review_mode == "unknown_family"

    @pytest.mark.asyncio
    async def test_core_two_families_prefers_cross_family_and_records_cross_family_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(monkeypatch, {})
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called)

        res = await run_review(
            user_id="u-two",
            author_model="claude-opus-5-5",
            bundle=_sample_bundle(),
            candidates=["claude-sonnet-5-5", "gpt-6.2-astra"],
            available_models={"claude-opus-5-5", "claude-sonnet-5-5", "gpt-6.2-astra"},
        )
        assert res.status == "done"
        assert res.model == "gpt-6.2-astra"
        assert res.family == "openai-gpt"
        assert res.review_mode == "cross_family"
        assert res.to_dict()["review_mode"] == "cross_family"
        assert called == ["gpt-6.2-astra"]

    @pytest.mark.asyncio
    async def test_everflow_rejects_single_family_and_unknown_family(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="dalton")
        _mock_user_prefs(monkeypatch, {})
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called)

        assert await pick_reviewer_model(
            "gpt-6.2-astra-oauth",
            candidates=["gpt-6.2-sol-oauth"],
            user_id="u-ef",
        ) is None
        res_single = await run_review(
            user_id="u-ef",
            author_model="gpt-6.2-astra-oauth",
            bundle=_sample_bundle(),
            candidates=["gpt-6.2-sol-oauth"],
        )
        assert res_single.status == "unavailable"
        assert "无法发布" in (res_single.reason or "")

        assert await pick_reviewer_model(
            "acme-gateway-llm",
            candidates=["gpt-6.2-sol-oauth"],
            user_id="u-ef",
        ) is None
        res_unknown = await run_review(
            user_id="u-ef",
            author_model="acme-gateway-llm",
            bundle=_sample_bundle(),
            candidates=["gpt-6.2-sol-oauth"],
        )
        assert res_unknown.status == "unavailable"
        assert "unknown" in (res_unknown.reason or "")
        assert called == []

    @pytest.mark.asyncio
    async def test_everflow_two_families_succeeds_with_cross_family_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="dalton")
        _mock_user_prefs(monkeypatch, {})
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called)

        res = await run_review(
            user_id="u-ef",
            author_model="claude-opus-5-5-oauth",
            bundle=_sample_bundle(),
            candidates=["gpt-6.2-sol-oauth"],
        )
        assert res.status == "done"
        assert res.model == "gpt-6.2-sol-oauth"
        assert res.review_mode == "cross_family"
        assert res.to_dict()["review_mode"] == "cross_family"
        assert classify_review_mode("claude-opus-5-5-oauth", "gpt-6.2-sol-oauth") == "cross_family"
        assert called == ["gpt-6.2-sol-oauth"]


class TestAuditTwoRoundLanguageAndBannerDisplay:
    """Audit fallback, Core 2-round check_language gate, banner review_mode, and DB persistence."""

    @pytest.mark.asyncio
    async def test_audit_reuses_customer_model_in_core_and_rejects_missing_in_everflow(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {"u-core": _custom_gw_pref()},
            keys_by_user={"u-core": _custom_gw_keys()},
        )
        eff_model, _fb, _ver, _sha = await audit_mod.resolve_audit_config_fingerprint("u-core")
        assert eff_model == "acme-gateway-llm"

        monkeypatch.setattr(
            audit_mod,
            "resolve_dalton_audit_model",
            create_autospec(audit_mod.resolve_dalton_audit_model, return_value=""),
        )
        assert await audit_mod._resolve_effective_audit_model("u-core") == "acme-gateway-llm"

        _set_profile(monkeypatch, app_env="dalton")
        with pytest.raises(ValueError, match="数字核验未配置模型"):
            await audit_mod.aaudit_draft(
                "# Draft\nRevenue $100M.",
                user_id="u-ef",
                use_llm_judge=True,
            )

    def test_publication_banner_renders_review_mode_only_in_core_and_preserves_everflow_format(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        common = dict(
            title="ACME Q3",
            kind="earnings_review",
            published_date="2026-11-23",
            approver_user_id="pm-1",
            approved_at="2026-11-23T08:00:00Z",
            reviewer_model="gpt-6.2-astra",
            reviewer_family="openai-gpt",
            publication_id="pub-1",
        )
        # Core profile: renders explicit review_mode label & description for all 3 modes + legacy missing.
        _set_profile(monkeypatch, app_env="core")
        cross = publish_files.prepend_publication_banner("# Body", **common, review_mode="cross_family")
        assert "**独立审稿**" in cross
        assert "跨家族独立审稿" in cross

        single = publish_files.prepend_publication_banner("# Body", **common, review_mode="single_family")
        assert "**同家族复审**" in single
        assert "**独立审稿**" not in single

        unknown = publish_files.prepend_publication_banner("# Body", **common, review_mode="unknown_family")
        assert "**模型复审**" in unknown
        assert "未知家族复审" in unknown
        assert "**独立审稿**" not in unknown

        legacy = publish_files.prepend_publication_banner("# Body", **common, review_mode=None)
        assert "**审稿**" in legacy
        assert "历史记录未标注模式" in legacy

        # Everflow profile: restores exact original banner line with no mode suffix (both new & legacy).
        _set_profile(monkeypatch, app_env="dalton")
        ef_new = publish_files.prepend_publication_banner("# Body", **common, review_mode="cross_family")
        assert "> - **独立审稿**：`gpt-6.2-astra`（`openai-gpt`）· 通过\n" in ef_new
        assert "跨家族独立审稿" not in ef_new

        ef_legacy = publish_files.prepend_publication_banner("# Body", **common, review_mode=None)
        assert "> - **独立审稿**：`gpt-6.2-astra`（`openai-gpt`）· 通过\n" in ef_legacy
        assert "历史记录" not in ef_legacy

    @pytest.mark.asyncio
    async def test_core_publish_requires_two_language_rounds_matching_final_draft_and_persists_review_mode(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core publish blocks before review until check_language rounds 1 & 2 match current draft;
        once satisfied, persists review_mode in request, publication, approval card, and banner."""
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {"pm-user": _custom_gw_pref()},
            keys_by_user={"pm-user": _custom_gw_keys()},
        )
        called: list[str] = []
        _patch_complete_pass(monkeypatch, called)

        req_id = "00000000-0000-0000-0000-0000000000b5"
        pub_id = "00000000-0000-0000-0000-0000000000c5"
        draft_body = "# ACME Q3\nNo unverified figures here.\n"
        state: dict[str, Any] = {
            "publish_request_id": req_id,
            "user_id": "pm-user",
            "workspace_id": "ws-core",
            "conversation_thread_id": "th-core",
            "tool_call_id": "tc-1",
            "kind": "earnings_review",
            "title": "ACME Q3",
            "slug": "acme-q3",
            "draft_path": "drafts/acme-q3.md",
            "attachments": [],
            "bundle_sha256": "",
            "updates_thesis": False,
            "thesis_version_no": None,
            "status": "reviewing",
            "audit": {"figures": 0, "matched": 0, "verified": 0, "matches": []},
            "review": None,
            "decided_by": None,
            "decided_at": None,
            "decision_message": None,
            "publication_id": None,
            "error": None,
        }
        captured_insert: dict[str, Any] = {}
        captured_interrupt: dict[str, Any] = {}

        class _MemBackend:
            workspace_dir = "/workspace"

            def __init__(self) -> None:
                self.files: dict[str, str] = {"drafts/acme-q3.md": draft_body}

            async def aread_text(self, path: str) -> str | None:
                return self.files.get(path)

            async def awrite_text(self, path: str, content: str) -> bool:
                self.files[path] = content
                return True

        backend = _MemBackend()

        async def _fake_verify_ws(workspace_id: str, user_id: str, **_kw: Any) -> dict[str, Any]:
            return {"workspace_id": workspace_id, "user_id": "pm-user"}

        async def _fake_get_or_create(**kwargs: Any) -> tuple[dict[str, Any], bool]:
            state["bundle_sha256"] = kwargs["bundle_sha256"]
            return dict(state), True

        async def _fake_transition(
            publish_request_id: str,
            from_statuses: Any,
            to: str,
            **cols: Any,
        ) -> dict[str, Any]:
            state["status"] = to
            state.update(cols)
            return dict(state)

        async def _fake_consume(publish_request_id: str, bundle_sha256: str) -> dict[str, Any]:
            state["status"] = "publishing"
            return dict(state)

        async def _fake_insert_pub(**kwargs: Any) -> dict[str, Any]:
            captured_insert.update(kwargs)
            state["status"] = "published"
            state["publication_id"] = pub_id
            return {"publication_id": pub_id, **kwargs}

        from src.dalton.coverage import identity as cov_id_mod
        from src.dalton.database import knowledge as db_knowledge

        monkeypatch.setattr(
            publish_svc,
            "verify_workspace_access",
            create_autospec(publish_svc.verify_workspace_access, side_effect=_fake_verify_ws),
        )
        monkeypatch.setattr(
            db_knowledge,
            "list_all_latest_for_workspace",
            create_autospec(
                db_knowledge.list_all_latest_for_workspace,
                return_value={"company": {"content": {"ticker": "ACME"}}},
            ),
        )
        monkeypatch.setattr(
            cov_id_mod,
            "resolve_workspace_coverage_identity",
            create_autospec(
                cov_id_mod.resolve_workspace_coverage_identity,
                return_value=cov_id_mod.WorkspaceCoverageIdentity(
                    kind=cov_id_mod.KIND_COMPANY,
                    ticker="ACME",
                    source=cov_id_mod.SOURCE_COMPAT,
                ),
            ),
        )
        monkeypatch.setattr(
            db_publications,
            "get_or_create_request",
            create_autospec(db_publications.get_or_create_request, side_effect=_fake_get_or_create),
        )
        monkeypatch.setattr(
            db_publications,
            "transition",
            create_autospec(db_publications.transition, side_effect=_fake_transition),
        )
        monkeypatch.setattr(
            db_publications,
            "consume_approval",
            create_autospec(db_publications.consume_approval, side_effect=_fake_consume),
        )
        monkeypatch.setattr(
            db_publications,
            "insert_publication",
            create_autospec(db_publications.insert_publication, side_effect=_fake_insert_pub),
        )
        monkeypatch.setattr(
            db_publications,
            "list_publications",
            create_autospec(db_publications.list_publications, return_value=[]),
        )
        from src.dalton.auth import roles as local_roles

        async def _fake_get_user_role(uid: str, *, conn: Any = None) -> str | None:
            return "admin" if uid == "pm-user" else None

        monkeypatch.setattr(
            local_roles,
            "get_user_role",
            create_autospec(local_roles.get_user_role, side_effect=_fake_get_user_role),
        )
        monkeypatch.setattr(
            publish_svc,
            "get_dalton_config",
            create_autospec(
                publish_svc.get_dalton_config,
                return_value=get_dalton_config().model_copy(
                    update={"pm_user_ids": ["pm-user"]}
                ),
            ),
        )

        def _fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
            captured_interrupt.update(payload)
            return {"decisions": [{"type": "approve", "message": "LGTM"}]}

        # 1. Missing .language.json -> refused before run_review is called.
        res_missing = await publish_svc._run_publish_state_machine(
            user_id="pm-user",
            workspace_id="ws-core",
            thread_id="th-core",
            tool_call_id="tc-1",
            draft_path="drafts/acme-q3.md",
            title="ACME Q3",
            kind="earnings_review",
            slug="acme-q3",
            backend=backend,
            author_model="acme-gateway-llm",
            interrupt_fn=_fake_interrupt,
        )
        assert res_missing["ok"] is False
        assert "check_language" in res_missing["message"]
        assert called == []

        # 2. Only round 1 -> refused.
        backend.files["drafts/acme-q3.md.language.json"] = _language_sidecar_json(
            draft_body, include_round2=False
        )
        res_r1_only = await publish_svc._run_publish_state_machine(
            user_id="pm-user",
            workspace_id="ws-core",
            thread_id="th-core",
            tool_call_id="tc-1",
            draft_path="drafts/acme-q3.md",
            title="ACME Q3",
            kind="earnings_review",
            slug="acme-q3",
            backend=backend,
            author_model="acme-gateway-llm",
            interrupt_fn=_fake_interrupt,
        )
        assert res_r1_only["ok"] is False
        assert "缺少第 2 轮" in res_r1_only["message"]
        assert called == []

        # 3. Stale round 2 sha256 -> refused.
        backend.files["drafts/acme-q3.md.language.json"] = _language_sidecar_json(
            draft_body, override_sha="f" * 64
        )
        res_stale = await publish_svc._run_publish_state_machine(
            user_id="pm-user",
            workspace_id="ws-core",
            thread_id="th-core",
            tool_call_id="tc-1",
            draft_path="drafts/acme-q3.md",
            title="ACME Q3",
            kind="earnings_review",
            slug="acme-q3",
            backend=backend,
            author_model="acme-gateway-llm",
            interrupt_fn=_fake_interrupt,
        )
        assert res_stale["ok"] is False
        assert "旧草稿" in res_stale["message"]
        assert called == []

        # 4. Valid 2-round check_language matching final draft -> succeeds and persists review_mode everywhere.
        backend.files["drafts/acme-q3.md.language.json"] = _language_sidecar_json(draft_body)
        res = await publish_svc._run_publish_state_machine(
            user_id="pm-user",
            workspace_id="ws-core",
            thread_id="th-core",
            tool_call_id="tc-1",
            draft_path="drafts/acme-q3.md",
            title="ACME Q3",
            kind="earnings_review",
            slug="acme-q3",
            backend=backend,
            author_model="acme-gateway-llm",
            interrupt_fn=_fake_interrupt,
        )

        assert res["ok"] is True
        assert res["status"] == "published"
        assert called == ["acme-gateway-llm"]
        assert state["review"]["review_mode"] == "unknown_family"
        assert captured_insert["reviewer"]["review_mode"] == "unknown_family"
        assert captured_insert["reviewer"]["model"] == "acme-gateway-llm"
        approval_reviewer = captured_interrupt["action_requests"][0]["args"]["reviewer"]
        assert approval_reviewer["review_mode"] == "unknown_family"
        pub_files = [
            v
            for k, v in backend.files.items()
            if k.startswith("published/") and k.endswith("-acme-q3.md")
        ]
        assert len(pub_files) == 1
        assert "**模型复审**" in pub_files[0]
        assert "未知家族复审" in pub_files[0]
        assert "**独立审稿**" not in pub_files[0]


class TestModelsEndpointToolCapabilityAndKeyProbe:
    """GET /api/v1/models capability annotation, caching, and Core /api/v1/keys/test tool probe."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("app_env", "dalton_profile", "expected"),
        [
            ("core", None, "core"),
            ("", "core", "core"),
            ("dalton", None, "everflow"),
            ("", None, "everflow"),
        ],
    )
    async def test_models_endpoint_returns_explicit_dalton_profile(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str | None,
        expected: str,
    ) -> None:
        _set_profile(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
        from src.server.app import api_keys as api_keys_mod
        from src.server.app import setup as setup_mod
        from src.llms import llm as llm_mod

        mock_mc = create_autospec(llm_mod.ModelConfig, instance=True)
        mock_mc.get_display_name.side_effect = lambda p: p.title()
        mock_mc.get_model_metadata.return_value = {
            "gpt-6.2-astra": {"sdk": "openai", "provider": "openai", "access_type": "api_key"},
        }
        mock_mc.llm_config = {}
        mock_mc.flat_providers = {}

        monkeypatch.setattr(
            llm_mod,
            "get_configured_llm_models",
            create_autospec(llm_mod.get_configured_llm_models, return_value={"openai": ["gpt-6.2-astra"]}),
        )
        monkeypatch.setattr(
            llm_mod.LLM,
            "get_model_config",
            create_autospec(llm_mod.LLM.get_model_config, return_value=mock_mc),
        )
        monkeypatch.setattr(
            api_keys_mod,
            "provider_catalog",
            create_autospec(api_keys_mod.provider_catalog, return_value=[]),
        )
        monkeypatch.setattr(
            api_keys_mod,
            "key_candidates",
            create_autospec(api_keys_mod.key_candidates, return_value={}),
        )
        monkeypatch.setattr(
            setup_mod,
            "agent_config",
            AgentConfig(
                llm=LLMConfig(name="gpt-6.2-astra"),
                security=SecurityConfig(),
                logging=LoggingConfig(),
                sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
                mcp=MCPConfig(),
                filesystem=FilesystemConfig(),
            ),
        )

        app = create_test_app(api_keys_mod.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/models")

        assert resp.status_code == 200
        body = resp.json()
        assert body["dalton_profile"] == expected

    def test_get_model_metadata_allowlist_surfaces_authored_tool_calling(self) -> None:
        from src.llms.llm import ModelConfig

        mc = ModelConfig()
        sample_key = next(iter(mc.llm_config))
        mc.llm_config[sample_key] = {**mc.llm_config[sample_key], "visible": True, "tool_calling": True}
        meta = mc.get_model_metadata()
        assert meta[sample_key]["tool_calling"] is True

    def test_annotate_model_metadata_is_evidence_based_and_cached_per_catalog_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clear_tool_capability_cache()
        load_calls = 0

        def _fake_load_meta() -> dict[str, dict[str, Any]]:
            nonlocal load_calls
            load_calls += 1
            return {
                "declared-capable": {"tool_calling": True},
                "declared-text-only": {"tool_calling": False},
            }

        monkeypatch.setattr(
            rp_mod,
            "load_dalton_meta",
            create_autospec(rp_mod.load_dalton_meta, side_effect=_fake_load_meta),
        )
        monkeypatch.setattr(
            rp_mod,
            "_load_latest_native_smoke",
            create_autospec(
                rp_mod._load_latest_native_smoke,
                return_value={"smoke-failed": {"checks": {"N3": "fail"}}},
            ),
        )
        stamp_box = ["stamp-v1"]
        monkeypatch.setattr(
            rp_mod,
            "current_overlay_stamp",
            create_autospec(rp_mod.current_overlay_stamp, side_effect=lambda *a, **kw: stamp_box[0]),
        )

        raw = {
            "declared-capable": {"sdk": "openai", "provider": "openai"},
            "declared-text-only": {"sdk": "openai", "provider": "openai"},
            "smoke-failed": {"sdk": "openai", "provider": "openai", "tool_calling": True},
            "unverified-byok": {"sdk": "openai", "provider": "openai"},
        }
        annotated_1 = annotate_model_metadata_tool_calling(raw)
        annotated_2 = annotate_model_metadata_tool_calling(raw)

        # Disk overlay loader invoked ONCE across both calls and all models under the same catalog version.
        assert load_calls == 1
        assert annotated_1["declared-capable"]["tool_calling"] is True
        assert annotated_1["declared-text-only"]["tool_calling"] is False
        assert annotated_1["smoke-failed"]["tool_calling"] is False
        assert "tool_calling" not in annotated_1["unverified-byok"]
        assert annotated_2 == annotated_1

        # Changing overlay stamp refreshes the cache once.
        stamp_box[0] = "stamp-v2"
        annotate_model_metadata_tool_calling(raw)
        assert load_calls == 2

    @pytest.mark.asyncio
    async def test_core_key_test_probes_per_model_respecting_chat_completions_vs_responses_while_everflow_skips(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1. Core profile: Chat Completions custom provider whose parent_provider='openai' has
        #    use_response_api=True MUST send probes to /chat/completions (NOT /responses) and
        #    record ONLY per-model verdicts (no provider-wide verdict).
        _set_profile(monkeypatch, app_env="core")
        probe_urls: list[str] = []
        probe_models: list[str] = []

        def _handler_per_model(request: httpx.Request) -> httpx.Response:
            probe_urls.append(str(request.url))
            body = json.loads(request.content.decode("utf-8"))
            mid = body.get("model", "")
            probe_models.append(mid)
            if mid == "corp-plain":
                return httpx.Response(400, json={"error": {"message": "tools unsupported"}})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "dalton_probe_ping",
                                            "arguments": '{"value":"ok"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        res_ok = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-core",
            provider="acme-gateway",
            # Parent 'openai' has use_response_api=True, but custom provider is Chat Completions!
            provider_info={"sdk": "openai", "use_response_api": True, "base_url": "https://api.openai.com/v1"},
            api_key="sk-test",
            base_url="https://gw.acme.test/v1",
            check_result={"success": True, "model": "fallback-first", "models_found": 2},
            models=["corp-v1", "corp-plain"],
            custom_provider_entry={"name": "acme-gateway", "parent_provider": "openai"},
            transport=httpx.MockTransport(_handler_per_model),
        )
        assert res_ok["success"] is True
        assert res_ok["tool_calling"] is True
        assert res_ok["tool_calling_model"] == "corp-v1"
        assert res_ok["tool_calling_models"] == {"corp-v1": True, "corp-plain": False}
        assert all(u.endswith("/chat/completions") for u in probe_urls)
        assert "corp-v1" in probe_models and "corp-plain" in probe_models

        attached = await probe_mod.attach_tool_probe_results(
            "u-core",
            {"byok_enabled": True, "providers": [{"provider": "acme-gateway", "has_key": True}]},
        )
        # Strictly per-model: NO provider-level "tool_calling" key so unprobed corp-2 stays unverified.
        assert "tool_calling" not in attached["providers"][0]
        assert attached["providers"][0]["tool_calling_models"] == {"corp-v1": True, "corp-plain": False}

        # 2. Core profile: custom provider with explicit use_response_api=True probes /responses.
        resp_urls: list[str] = []

        def _handler_responses(request: httpx.Request) -> httpx.Response:
            resp_urls.append(str(request.url))
            return httpx.Response(200, json={"output": [{"type": "function_call", "name": "dalton_probe_ping"}]})

        res_resp = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-core",
            provider="acme-responses",
            provider_info={"sdk": "openai", "use_response_api": True},
            api_key="sk-test",
            base_url="https://resp.acme.test/v1",
            check_result={"success": True},
            model="resp-m1",
            custom_provider_entry={"name": "acme-responses", "parent_provider": "openai", "use_response_api": True},
            transport=httpx.MockTransport(_handler_responses),
        )
        assert res_resp["tool_calling"] is True
        assert resp_urls == ["https://resp.acme.test/v1/responses"]

        # 3. Core profile: gateway with NO /models endpoint (returns 404) and only serving corp-1
        #    (rejects gpt-4.1-nano with 404) MUST probe corp-1 directly on /api/v1/keys/test and
        #    record tool_calling_models for ALL submitted models (> 5 models, never truncated at 5).
        from src.server.app import api_keys as api_keys_mod
        from src.server.utils.api import get_current_user_id

        probe_mod.clear_tool_probe_cache()
        eight_models = [f"corp-{i}" for i in range(1, 9)]
        seen_chat_models: list[str] = []
        seen_tool_models: list[str] = []

        def _handler_no_models_list(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(404, json={"error": "not found"})
            body = json.loads(request.content.decode("utf-8"))
            mid = body.get("model", "")
            seen_chat_models.append(mid)
            if mid not in eight_models:
                return httpx.Response(404, json={"error": f"unknown model {mid}"})
            if body.get("tools"):
                seen_tool_models.append(mid)
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": f"call_{mid}",
                                            "type": "function",
                                            "function": {
                                                "name": "dalton_probe_ping",
                                                "arguments": '{"value":"ok"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]},
            )

        monkeypatch.setattr(
            api_keys_mod,
            "_get_custom_providers",
            create_autospec(
                api_keys_mod._get_custom_providers,
                return_value=[{"name": "no-list-gw", "parent_provider": "openai"}],
            ),
        )
        monkeypatch.setattr(
            api_keys_mod,
            "throttle",
            create_autospec(api_keys_mod.throttle, return_value=True),
        )
        monkeypatch.setattr(
            api_keys_mod,
            "_user_endpoint_transport",
            create_autospec(
                api_keys_mod._user_endpoint_transport,
                return_value=httpx.MockTransport(_handler_no_models_list),
            ),
        )

        app = create_test_app(api_keys_mod.router)
        app.dependency_overrides[get_current_user_id] = lambda: "u-core-nolist"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "no-list-gw",
                    "api_key": "sk-nolist",
                    "base_url": "https://nolist.acme.test/v1",
                    "model": "corp-1",
                    "models": eight_models,
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["model"] == "corp-1"
        # Hardcoded gpt-4.1-nano must never be sent when caller submitted corp-1.
        assert "gpt-4.1-nano" not in seen_chat_models
        # All 8 models (not capped at 5) must be probed and recorded.
        assert set(seen_tool_models) == set(eight_models)
        assert body["tool_calling_models"] == {m: True for m in eight_models}
        attached_eight = await probe_mod.attach_tool_probe_results(
            "u-core-nolist",
            {"byok_enabled": True, "providers": [{"provider": "no-list-gw", "has_key": True}]},
        )
        assert attached_eight["providers"][0]["tool_calling_models"] == {m: True for m in eight_models}

        # 4. Everflow profile: strict no-op (makes zero extra HTTP calls and adds no fields).
        _set_profile(monkeypatch, app_env="dalton")
        ef_calls = 0

        def _handler_should_not_run(request: httpx.Request) -> httpx.Response:
            nonlocal ef_calls
            ef_calls += 1
            return httpx.Response(500)

        base_check = {"success": True, "model": "gpt-6.2-astra"}
        ef_res = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-ef",
            provider="openai",
            provider_info={"sdk": "openai"},
            api_key="sk-test",
            base_url=None,
            check_result=base_check,
            transport=httpx.MockTransport(_handler_should_not_run),
        )
        assert ef_calls == 0
        assert ef_res == base_check
        assert "tool_calling" not in ef_res

    @pytest.mark.asyncio
    async def test_core_key_test_handles_501_models_endpoint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Core /api/v1/keys/test falls back to submitted model when /models returns 501; Everflow keeps 501 failing."""
        import httpx
        from httpx import ASGITransport, AsyncClient

        from src.dalton.llm import tool_probe as probe_mod
        from src.server.app import api_keys as api_keys_mod
        from src.server.utils.api import get_current_user_id

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))

        seen_chat_models: list[str] = []

        def _handler_501(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(501, json={"error": "models listing not implemented"})
            body = json.loads(request.content.decode("utf-8"))
            mid = body.get("model", "")
            seen_chat_models.append(mid)
            if mid != "corp-501":
                return httpx.Response(404, json={"error": f"unknown model {mid}"})
            if body.get("tools"):
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_501",
                                            "type": "function",
                                            "function": {
                                                "name": "dalton_probe_ping",
                                                "arguments": '{"value":"ok"}',
                                            },
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hi"}}]})

        monkeypatch.setattr(
            api_keys_mod,
            "_get_custom_providers",
            create_autospec(
                api_keys_mod._get_custom_providers,
                return_value=[{"name": "gw-501", "parent_provider": "openai"}],
            ),
        )
        monkeypatch.setattr(api_keys_mod, "throttle", create_autospec(api_keys_mod.throttle, return_value=True))
        monkeypatch.setattr(
            api_keys_mod,
            "_user_endpoint_transport",
            create_autospec(
                api_keys_mod._user_endpoint_transport,
                return_value=httpx.MockTransport(_handler_501),
            ),
        )

        app = create_test_app(api_keys_mod.router)
        app.dependency_overrides[get_current_user_id] = lambda: "u-core-501"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "gw-501",
                    "api_key": "sk-501",
                    "base_url": "https://gw501.acme.test/v1",
                    "model": "corp-501",
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["model"] == "corp-501"
        assert data["tool_calling"] is True
        assert data["tool_calling_models"] == {"corp-501": True}
        assert "gpt-4.1-nano" not in seen_chat_models

        # Everflow profile: 501 from /models is NOT rewritten (original behavior preserved).
        _set_profile(monkeypatch, app_env="dalton")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            ef_resp = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "gw-501",
                    "api_key": "sk-501",
                    "base_url": "https://gw501.acme.test/v1",
                    "model": "corp-501",
                },
            )
        assert ef_resp.status_code == 200
        assert ef_resp.json()["success"] is False

    @pytest.mark.asyncio
    async def test_core_key_test_handles_responses_only_gateway(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Responses-only gateway: saved Chat Completions format reports unverified + hint (not confirmed),
        whereas saved Responses format confirms tool calling normally."""
        import httpx
        from httpx import ASGITransport, AsyncClient

        from src.dalton.llm import tool_probe as probe_mod
        from src.server.app import api_keys as api_keys_mod
        from src.server.utils.api import get_current_user_id

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))

        expected_hint = "网关只支持 Responses 接口，请在提供商设置里打开 Responses 格式"
        models_status = 404
        paths_hit: list[str] = []

        def _handler_responses_only(request: httpx.Request) -> httpx.Response:
            paths_hit.append(request.url.path)
            if request.url.path.endswith("/models"):
                if models_status == 200:
                    return httpx.Response(200, json={"data": [{"id": "resp-only-1"}]})
                return httpx.Response(models_status, json={"error": "no models endpoint"})
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(404, json={"error": "chat completions unsupported"})
            if request.url.path.endswith("/responses"):
                body = json.loads(request.content.decode("utf-8"))
                if body.get("model") != "resp-only-1":
                    return httpx.Response(404, json={"error": "unknown model"})
                if body.get("tools"):
                    return httpx.Response(
                        200,
                        json={"output": [{"type": "function_call", "name": "dalton_probe_ping"}]},
                    )
                return httpx.Response(200, json={"output": [{"type": "message", "content": "hi"}]})
            return httpx.Response(404)

        custom_entry: list[dict[str, Any]] = [{"name": "resp-only-gw", "parent_provider": "openai"}]
        monkeypatch.setattr(
            api_keys_mod,
            "_get_custom_providers",
            create_autospec(
                api_keys_mod._get_custom_providers,
                side_effect=lambda _uid: list(custom_entry),
            ),
        )
        monkeypatch.setattr(api_keys_mod, "throttle", create_autospec(api_keys_mod.throttle, return_value=True))
        async def _fake_user_endpoint_transport(
            base_url: str | None, *, user_id: str | None = None
        ):
            return httpx.MockTransport(_handler_responses_only)

        monkeypatch.setattr(
            api_keys_mod,
            "_user_endpoint_transport",
            create_autospec(
                api_keys_mod._user_endpoint_transport,
                side_effect=_fake_user_endpoint_transport,
            ),
        )

        app = create_test_app(api_keys_mod.router)
        app.dependency_overrides[get_current_user_id] = lambda: "u-core-resp-only"

        # Case A: Saved as Chat Completions + /models 404 -> key check succeeds via /responses fallback,
        # but tool calling is marked unverified with the format-mismatch hint (NOT confirmed/persisted).
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_chat_fmt = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "resp-only-gw",
                    "api_key": "sk-resp",
                    "base_url": "https://responly.acme.test/v1",
                    "model": "resp-only-1",
                },
            )
        assert resp_chat_fmt.status_code == 200
        data_a = resp_chat_fmt.json()
        assert data_a["success"] is True
        assert data_a["model"] == "resp-only-1"
        assert data_a["tool_calling"] is None
        assert data_a["tool_calling_models"] == {"resp-only-1": None}
        assert data_a["tool_calling_details"]["resp-only-1"] == {
            "status": "unverified",
            "tool_calling": None,
            "reason": expected_hint,
        }
        assert data_a["tool_calling_hint"] == expected_hint
        assert data_a["tool_calling_error"] == expected_hint
        assert await probe_mod.get_recorded_tool_probes("u-core-resp-only") == {}

        # Case B: Saved as Chat Completions + /models 200 -> tool probe hits /chat/completions 404
        # and falls back to /responses 200 -> ALSO unverified with the exact format-mismatch hint.
        models_status = 200
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_chat_listed = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "resp-only-gw",
                    "api_key": "sk-resp",
                    "base_url": "https://responly.acme.test/v1",
                    "model": "resp-only-1",
                },
            )
        assert resp_chat_listed.status_code == 200
        data_b = resp_chat_listed.json()
        assert data_b["success"] is True
        assert data_b["tool_calling"] is None
        assert data_b["tool_calling_details"]["resp-only-1"]["status"] == "unverified"
        assert data_b["tool_calling_details"]["resp-only-1"]["reason"] == expected_hint
        assert data_b["tool_calling_hint"] == expected_hint
        assert await probe_mod.get_recorded_tool_probes("u-core-resp-only") == {}

        # Case C: Saved as Responses format (use_response_api=True) -> confirmed and persisted normally.
        models_status = 404
        paths_hit.clear()
        custom_entry[:] = [{"name": "resp-only-gw", "parent_provider": "openai", "use_response_api": True}]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_responses_fmt = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "resp-only-gw",
                    "api_key": "sk-resp",
                    "base_url": "https://responly.acme.test/v1",
                    "model": "resp-only-1",
                },
            )
        assert resp_responses_fmt.status_code == 200
        data_c = resp_responses_fmt.json()
        assert data_c["success"] is True
        assert data_c["model"] == "resp-only-1"
        assert data_c["tool_calling"] is True
        assert data_c["tool_calling_models"] == {"resp-only-1": True}
        assert "tool_calling_hint" not in data_c
        assert paths_hit.count("/v1/responses") == 2
        assert await probe_mod.get_recorded_tool_probes("u-core-resp-only") == {
            "resp-only-gw": {"models": {"resp-only-1": True}}
        }

    @pytest.mark.asyncio
    async def test_core_key_test_platform_mode_held_transport_lifecycle(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Under HOST_MODE=platform, the non-shared HeldTransport from egress.user_endpoint_transport
        stays open across /models, fallback key validation, and all tool probes, closing exactly once."""
        import httpx
        from httpx import ASGITransport, AsyncClient

        from src.config import settings as settings_mod
        from src.dalton.llm import tool_probe as probe_mod
        from src.server.app import api_keys as api_keys_mod
        from src.server.services.llm import egress as egress_mod
        from src.server.utils.api import get_current_user_id

        _set_profile(monkeypatch, app_env="core")
        monkeypatch.setattr(settings_mod, "HOST_MODE", "platform")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))
        monkeypatch.setattr(egress_mod, "endpoint_refusal", create_autospec(egress_mod.endpoint_refusal, return_value=None))
        monkeypatch.setattr(egress_mod, "is_operator_endpoint", create_autospec(egress_mod.is_operator_endpoint, return_value=False))
        monkeypatch.setattr(api_keys_mod, "throttle", create_autospec(api_keys_mod.throttle, return_value=True))
        monkeypatch.setattr(
            api_keys_mod,
            "_get_custom_providers",
            create_autospec(
                api_keys_mod._get_custom_providers,
                return_value=[{"name": "plat-gw", "parent_provider": "openai"}],
            ),
        )

        created_transports: list[Any] = []
        models_status = 501

        class _StrictAsyncPool(httpx.AsyncBaseTransport):
            def __init__(self) -> None:
                self.closed = False
                self.close_count = 0

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                if self.closed:
                    raise RuntimeError("HeldTransport async pool already closed")
                if request.url.path.endswith("/models"):
                    if models_status == 200:
                        return httpx.Response(
                            200,
                            json={"data": [{"id": "plat-1"}, {"id": "plat-2"}, {"id": "plat-3"}]},
                        )
                    return httpx.Response(models_status, json={"error": "not implemented"})
                body = json.loads(request.content.decode("utf-8"))
                mid = body.get("model", "")
                if body.get("tools"):
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "tool_calls": [
                                            {
                                                "id": f"call_{mid}",
                                                "type": "function",
                                                "function": {"name": "dalton_probe_ping", "arguments": '{"value":"ok"}'},
                                            }
                                        ],
                                    }
                                }
                            ]
                        },
                    )
                return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

            async def aclose(self) -> None:
                self.closed = True
                self.close_count += 1

        orig_http_transport = egress_mod.http_transport

        def _tracked_http_transport():
            held = orig_http_transport()
            assert held._shared is False
            pool = _StrictAsyncPool()
            held._async = pool
            created_transports.append(pool)
            return held

        monkeypatch.setattr(
            egress_mod,
            "http_transport",
            create_autospec(orig_http_transport, side_effect=_tracked_http_transport),
        )

        app = create_test_app(api_keys_mod.router)
        app.dependency_overrides[get_current_user_id] = lambda: "u-core-platform"

        # 1. /models returns 501 -> key check falls back to submitted model and tool probe completes.
        models_status = 501
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_501 = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "plat-gw",
                    "api_key": "sk-plat",
                    "base_url": "https://plat.acme.test/v1",
                    "model": "plat-501",
                },
            )
        assert resp_501.status_code == 200
        d501 = resp_501.json()
        assert d501["success"] is True
        assert d501["model"] == "plat-501"
        assert d501["tool_calling_models"] == {"plat-501": True}
        assert len(created_transports) == 1
        assert created_transports[0].closed is True
        assert created_transports[0].close_count == 1

        # 2. /models returns 200 -> all submitted models get tool-calling verdicts on the same HeldTransport.
        models_status = 200
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp_200 = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "plat-gw",
                    "api_key": "sk-plat",
                    "base_url": "https://plat.acme.test/v1",
                    "models": ["plat-1", "plat-2", "plat-3"],
                },
            )
        assert resp_200.status_code == 200
        d200 = resp_200.json()
        assert d200["success"] is True
        assert d200["tool_calling_models"] == {"plat-1": True, "plat-2": True, "plat-3": True}
        assert len(created_transports) == 2
        assert created_transports[1].closed is True
        assert created_transports[1].close_count == 1

    @pytest.mark.asyncio
    async def test_core_tool_probe_concurrent_redis_writes_do_not_overwrite(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Interleaved concurrent Redis writes persist per-model atomically without overwriting each other."""
        import asyncio

        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()

        both_started = asyncio.Event()
        entered = 0

        class _InterleavingFakeRedis:
            def __init__(self) -> None:
                self.hashes: dict[str, dict[str, str]] = {}
                self.ttls: dict[str, int] = {}
                self.tx_count = 0

            def pipeline(self, *, transaction: bool = True):
                assert transaction is True
                parent = self

                class _Pipe:
                    def __init__(self) -> None:
                        self._ops: list[tuple[str, tuple[Any, ...]]] = []

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args: Any) -> None:
                        return None

                    def hset(self, key: str, field: str, value: str):
                        self._ops.append(("hset", (key, field, value)))
                        return self

                    def expire(self, key: str, ttl: int):
                        self._ops.append(("expire", (key, ttl)))
                        return self

                    async def execute(self) -> list[Any]:
                        nonlocal entered
                        entered += 1
                        if entered >= 2:
                            both_started.set()
                        await both_started.wait()
                        await asyncio.sleep(0)
                        parent.tx_count += 1
                        # Verify HSET + EXPIRE always travel together in the same transaction.
                        assert [op for op, _ in self._ops] == ["hset", "expire"]
                        for op, op_args in self._ops:
                            if op == "hset":
                                k, f, v = op_args
                                parent.hashes.setdefault(k, {})[f] = v
                            elif op == "expire":
                                k, ttl = op_args
                                parent.ttls[k] = ttl
                        return [1, True]

                return _Pipe()

            async def hgetall(self, key: str) -> dict[str, str]:
                return dict(self.hashes.get(key, {}))

            async def get(self, key: str) -> str | None:
                return None

        fake_redis = _InterleavingFakeRedis()
        monkeypatch.setattr(
            probe_mod,
            "_get_redis_client",
            create_autospec(probe_mod._get_redis_client, return_value=fake_redis),
        )

        await asyncio.gather(
            probe_mod.record_tool_calling_probe("u-redis-race", "acme-gw", "m1", True),
            probe_mod.record_tool_calling_probe("u-redis-race", "acme-gw", "m2", False),
        )
        assert fake_redis.tx_count == 2

        # Simulate a different worker with empty in-process cache reading solely from Redis.
        probe_mod.clear_tool_probe_cache("u-redis-race")
        loaded = await probe_mod.get_recorded_tool_probes("u-redis-race")
        assert loaded == {"acme-gw": {"models": {"m1": True, "m2": False}}}

    @pytest.mark.asyncio
    async def test_core_tool_probe_reprobe_unverified_overwrites_stale_true_and_uses_pipeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-probing a model as unverified (e.g. after switching Responses -> Chat Completions)
        or unsupported overwrites the stale True in Redis so attach_tool_probe_results no longer returns True."""
        import httpx

        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()

        class _TxFakeRedis:
            def __init__(self) -> None:
                self.hashes: dict[str, dict[str, str]] = {}
                self.ttls: dict[str, int] = {}
                self.tx_ops: list[list[str]] = []

            def pipeline(self, *, transaction: bool = True):
                assert transaction is True
                parent = self

                class _Pipe:
                    def __init__(self) -> None:
                        self._ops: list[tuple[str, tuple[Any, ...]]] = []

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args: Any) -> None:
                        return None

                    def hset(self, key: str, field: str, value: str):
                        self._ops.append(("hset", (key, field, value)))
                        return self

                    def expire(self, key: str, ttl: int):
                        self._ops.append(("expire", (key, ttl)))
                        return self

                    async def execute(self) -> list[Any]:
                        parent.tx_ops.append([op for op, _ in self._ops])
                        for op, op_args in self._ops:
                            if op == "hset":
                                k, f, v = op_args
                                parent.hashes.setdefault(k, {})[f] = v
                            elif op == "expire":
                                k, ttl = op_args
                                parent.ttls[k] = ttl
                        return [1, True]

                return _Pipe()

            async def hgetall(self, key: str) -> dict[str, str]:
                return dict(self.hashes.get(key, {}))

            async def get(self, key: str) -> str | None:
                return None

        fake_redis = _TxFakeRedis()
        monkeypatch.setattr(
            probe_mod,
            "_get_redis_client",
            create_autospec(probe_mod._get_redis_client, return_value=fake_redis),
        )

        def _responses_only_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(404, json={"error": "chat completions unsupported"})
            if request.url.path.endswith("/responses"):
                return httpx.Response(
                    200,
                    json={"object": "response", "output": [{"type": "function_call", "name": "dalton_probe_ping"}]},
                )
            return httpx.Response(404)

        # 1. Probe with saved format = Responses -> confirmed True in Redis.
        res_true = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-overwrite",
            provider="resp-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-ow",
            base_url="https://ow.acme.test/v1",
            check_result={"success": True},
            model="resp-m1",
            custom_provider_entry={"name": "resp-gw", "parent_provider": "openai", "use_response_api": True},
            transport=httpx.MockTransport(_responses_only_handler),
        )
        assert res_true["tool_calling"] is True
        attached_1 = await probe_mod.attach_tool_probe_results(
            "u-overwrite",
            {"byok_enabled": True, "providers": [{"provider": "resp-gw", "has_key": True}]},
        )
        assert attached_1["providers"][0]["tool_calling_models"] == {"resp-m1": True}

        # 2. Switch saved format to Chat Completions and re-probe -> unverified overwrites True in Redis.
        res_unverified = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-overwrite",
            provider="resp-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-ow",
            base_url="https://ow.acme.test/v1",
            check_result={"success": True, "used_response_api": True},
            model="resp-m1",
            custom_provider_entry={"name": "resp-gw", "parent_provider": "openai"},
            transport=httpx.MockTransport(_responses_only_handler),
        )
        assert res_unverified["tool_calling"] is None
        assert res_unverified["tool_calling_hint"] == "网关只支持 Responses 接口，请在提供商设置里打开 Responses 格式"

        # Clear local cache so read comes purely from Redis: stale True must NOT be returned.
        probe_mod.clear_tool_probe_cache("u-overwrite")
        attached_2 = await probe_mod.attach_tool_probe_results(
            "u-overwrite",
            {"byok_enabled": True, "providers": [{"provider": "resp-gw", "has_key": True}]},
        )
        assert "tool_calling_models" not in attached_2["providers"][0]
        assert fake_redis.tx_ops == [["hset", "expire"], ["hset", "expire"]]

    @pytest.mark.asyncio
    async def test_core_tool_probe_redis_is_source_of_truth_across_workers(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two independent worker in-process caches sharing one Redis: worker A writes null,
        worker B writes true, and worker A reads true (never shadowed by stale in-process state)."""
        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")

        class _SharedFakeRedis:
            def __init__(self) -> None:
                self.hashes: dict[str, dict[str, str]] = {}

            def pipeline(self, *, transaction: bool = True):
                assert transaction is True
                parent = self

                class _Pipe:
                    def __init__(self) -> None:
                        self._ops: list[tuple[str, tuple[Any, ...]]] = []

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *args: Any) -> None:
                        return None

                    def hset(self, key: str, field: str, value: str):
                        self._ops.append(("hset", (key, field, value)))
                        return self

                    def expire(self, key: str, ttl: int):
                        self._ops.append(("expire", (key, ttl)))
                        return self

                    async def execute(self) -> list[Any]:
                        for op, op_args in self._ops:
                            if op == "hset":
                                k, f, v = op_args
                                parent.hashes.setdefault(k, {})[f] = v
                        return [1, True]

                return _Pipe()

            async def hgetall(self, key: str) -> dict[str, str]:
                return dict(self.hashes.get(key, {}))

            async def get(self, key: str) -> str | None:
                return None

        shared_redis = _SharedFakeRedis()
        monkeypatch.setattr(
            probe_mod,
            "_get_redis_client",
            create_autospec(probe_mod._get_redis_client, return_value=shared_redis),
        )

        # Simulate worker A and worker B having separate in-process fallback dicts.
        # Pre-seed worker A's in-process dict with stale None to prove Redis overrides it.
        worker_a_store: dict[str, Any] = {"u-multi": {"gw": {"models": {"m1": None}}}}
        worker_b_store: dict[str, Any] = {}

        # Worker A writes None (unverified).
        monkeypatch.setattr(probe_mod, "_LOCAL_PROBE_STORE", worker_a_store)
        await probe_mod.record_tool_calling_probe("u-multi", "gw", "m1", None)
        assert await probe_mod.get_recorded_tool_probes("u-multi") == {}

        # Worker B fixes format and writes True to shared Redis.
        monkeypatch.setattr(probe_mod, "_LOCAL_PROBE_STORE", worker_b_store)
        await probe_mod.record_tool_calling_probe("u-multi", "gw", "m1", True)

        # Worker A (whose in-process dict still holds None!) reads True from Redis.
        monkeypatch.setattr(probe_mod, "_LOCAL_PROBE_STORE", worker_a_store)
        assert await probe_mod.get_recorded_tool_probes("u-multi") == {"gw": {"models": {"m1": True}}}

        # Reverse direction: worker A holds stale True locally, worker B overwrites Redis with None -> A reads {}.
        worker_a_store["u-multi"]["gw"]["models"]["m1"] = True
        monkeypatch.setattr(probe_mod, "_LOCAL_PROBE_STORE", worker_b_store)
        await probe_mod.record_tool_calling_probe("u-multi", "gw", "m1", None)
        monkeypatch.setattr(probe_mod, "_LOCAL_PROBE_STORE", worker_a_store)
        assert await probe_mod.get_recorded_tool_probes("u-multi") == {}

    @pytest.mark.asyncio
    async def test_core_tool_probe_responses_fallback_requires_valid_responses_json(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falling back to /responses only triggers the Responses format hint when the body is genuine
        Responses JSON; HTML or non-Responses JSON is marked unverified with reason and no format hint."""
        import httpx
        from httpx import ASGITransport, AsyncClient

        from src.dalton.llm import tool_probe as probe_mod
        from src.server.app import api_keys as api_keys_mod
        from src.server.utils.api import get_current_user_id

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))

        # Case A: Reverse proxy returns HTTP 200 HTML page on /responses.
        def _html_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(404, json={"error": "not found"})
            if request.url.path.endswith("/responses"):
                return httpx.Response(200, text="<html><body>Welcome</body></html>", headers={"content-type": "text/html"})
            return httpx.Response(404)

        res_html = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-html",
            provider="proxy-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-html",
            base_url="https://proxy.acme.test/v1",
            check_result={"success": True},
            model="m-html",
            custom_provider_entry={"name": "proxy-gw", "parent_provider": "openai"},
            transport=httpx.MockTransport(_html_handler),
        )
        assert res_html["tool_calling_models"] == {"m-html": None}
        assert res_html["tool_calling_details"]["m-html"]["status"] == "unverified"
        assert "non-json" in res_html["tool_calling_details"]["m-html"]["reason"].lower()
        assert "tool_calling_hint" not in res_html

        # Case B: Endpoint returns HTTP 200 non-Responses JSON on /responses.
        def _other_json_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/chat/completions"):
                return httpx.Response(404, json={"error": "not found"})
            if request.url.path.endswith("/responses"):
                return httpx.Response(200, json={"status": "ok", "message": "hello"})
            return httpx.Response(404)

        res_other = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-other-json",
            provider="proxy-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-json",
            base_url="https://proxy.acme.test/v1",
            check_result={"success": True},
            model="m-json",
            custom_provider_entry={"name": "proxy-gw", "parent_provider": "openai"},
            transport=httpx.MockTransport(_other_json_handler),
        )
        assert res_other["tool_calling_models"] == {"m-json": None}
        assert res_other["tool_calling_details"]["m-json"]["status"] == "unverified"
        assert "non-responses" in res_other["tool_calling_details"]["m-json"]["reason"].lower()
        assert "tool_calling_hint" not in res_other

        # Case C: End-to-end /api/v1/keys/test when /models 404, /chat/completions 404, /responses 200 HTML.
        monkeypatch.setattr(
            api_keys_mod,
            "_get_custom_providers",
            create_autospec(
                api_keys_mod._get_custom_providers,
                return_value=[{"name": "proxy-gw", "parent_provider": "openai"}],
            ),
        )
        monkeypatch.setattr(api_keys_mod, "throttle", create_autospec(api_keys_mod.throttle, return_value=True))
        monkeypatch.setattr(
            api_keys_mod,
            "_user_endpoint_transport",
            create_autospec(
                api_keys_mod._user_endpoint_transport,
                return_value=httpx.MockTransport(_html_handler),
            ),
        )
        app = create_test_app(api_keys_mod.router)
        app.dependency_overrides[get_current_user_id] = lambda: "u-html-e2e"
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            e2e = await client.post(
                "/api/v1/keys/test",
                json={
                    "provider": "proxy-gw",
                    "api_key": "sk-html",
                    "base_url": "https://proxy.acme.test/v1",
                    "model": "m-html",
                },
            )
        assert e2e.status_code == 200
        body = e2e.json()
        assert body["tool_calling_models"] == {"m-html": None}
        assert body["tool_calling_details"]["m-html"]["status"] == "unverified"
        assert "tool_calling_hint" not in body

    @pytest.mark.asyncio
    async def test_core_tool_probe_retry_after_up_to_30s_and_total_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Retry-After honors actual server delay up to 30s; exceeding total probe timeout yields unverified + reason."""
        import httpx

        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))

        # Verify clamping bounds: 12s honored verbatim; 90s capped at 30s.
        assert probe_mod._parse_retry_after_s(httpx.Response(429, headers={"Retry-After": "12"}), 0) == 12.0
        assert probe_mod._parse_retry_after_s(httpx.Response(429, headers={"Retry-After": "90"}), 0) == 30.0

        slept: list[float] = []

        async def _fake_sleep(delay: float) -> None:
            slept.append(delay)

        monkeypatch.setattr(probe_mod.asyncio, "sleep", create_autospec(probe_mod.asyncio.sleep, side_effect=_fake_sleep))

        calls = 0

        def _handler_retry_12s(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "12"}, json={"error": "slow down"})
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_12s",
                                        "type": "function",
                                        "function": {"name": "dalton_probe_ping", "arguments": '{"value":"ok"}'},
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        res_ok = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-ra-12s",
            provider="ra-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-ra",
            base_url="https://ra.acme.test/v1",
            check_result={"success": True},
            model="m-12s",
            custom_provider_entry={"name": "ra-gw", "parent_provider": "openai"},
            transport=httpx.MockTransport(_handler_retry_12s),
            total_timeout=45.0,
        )
        assert slept == [12.0]
        assert res_ok["tool_calling_models"] == {"m-12s": True}

        # When Retry-After exceeds remaining total probe timeout, mark unverified with explicit reason immediately.
        slept.clear()
        calls = 0
        res_budget = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-ra-budget",
            provider="ra-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-ra",
            base_url="https://ra.acme.test/v1",
            check_result={"success": True},
            model="m-budget",
            custom_provider_entry={"name": "ra-gw", "parent_provider": "openai"},
            transport=httpx.MockTransport(_handler_retry_12s),
            total_timeout=5.0,
        )
        assert slept == []
        assert res_budget["tool_calling_models"] == {"m-budget": None}
        detail = res_budget["tool_calling_details"]["m-budget"]
        assert detail["status"] == "unverified"
        assert "timeout" in detail["reason"].lower()
        assert "12.0s" in detail["reason"]

    @pytest.mark.asyncio
    async def test_core_tool_probe_responses_fallback_400_422_marked_unverified_not_unsupported(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falling back from /chat/completions to /responses and getting 400/422 marks unverified, never unsupported."""
        import httpx

        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))

        for status_code in (400, 422):
            def _handler(request: httpx.Request, code: int = status_code) -> httpx.Response:
                if request.url.path.endswith("/chat/completions"):
                    return httpx.Response(404, json={"error": "not found"})
                if request.url.path.endswith("/responses"):
                    return httpx.Response(code, json={"error": f"bad responses payload ({code})"})
                return httpx.Response(404)

            res = await probe_mod.probe_tool_calling_for_key_test(
                user_id=f"u-fb-{status_code}",
                provider="fb-gw",
                provider_info={"sdk": "openai"},
                api_key="sk-fb",
                base_url="https://fb.acme.test/v1",
                check_result={"success": True},
                model=f"m-{status_code}",
                custom_provider_entry={"name": "fb-gw", "parent_provider": "openai"},
                transport=httpx.MockTransport(_handler),
            )
            assert res["tool_calling_models"] == {f"m-{status_code}": None}
            detail = res["tool_calling_details"][f"m-{status_code}"]
            assert detail["status"] == "unverified"
            assert str(status_code) in detail["reason"]
            assert await probe_mod.get_recorded_tool_probes(f"u-fb-{status_code}") == {}

    @pytest.mark.asyncio
    async def test_core_tool_probe_rate_limit_single_concurrency_and_unverified_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Gateway allowing only 1 concurrent request triggers 429 retries and still resolves all models;
        exhausted 429 records an explicit unverified status and reason instead of bare null."""
        import asyncio
        import httpx

        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))
        assert probe_mod._PROBE_CONCURRENCY == 2

        class _SingleConcurrencyTransport(httpx.AsyncBaseTransport):
            def __init__(self) -> None:
                self.in_flight = 0
                self.max_seen = 0
                self.rate_limited_hits = 0

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                self.in_flight += 1
                self.max_seen = max(self.max_seen, self.in_flight)
                try:
                    if self.in_flight > 1:
                        self.rate_limited_hits += 1
                        return httpx.Response(429, headers={"Retry-After": "0.02"}, json={"error": "too many concurrent"})
                    await asyncio.sleep(0.03)
                    return httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "tool_calls": [
                                            {
                                                "id": "call_rl",
                                                "type": "function",
                                                "function": {"name": "dalton_probe_ping", "arguments": '{"value":"ok"}'},
                                            }
                                        ],
                                    }
                                }
                            ]
                        },
                    )
                finally:
                    self.in_flight -= 1

        rl_transport = _SingleConcurrencyTransport()
        models = ["rl-1", "rl-2", "rl-3", "rl-4"]
        res = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-rl",
            provider="rl-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-rl",
            base_url="https://rl.acme.test/v1",
            check_result={"success": True},
            models=models,
            custom_provider_entry={"name": "rl-gw", "parent_provider": "openai"},
            transport=rl_transport,
        )
        assert rl_transport.max_seen <= 2
        assert rl_transport.rate_limited_hits >= 1
        assert res["tool_calling_models"] == {m: True for m in models}
        assert all(res["tool_calling_details"][m]["status"] == "supported" for m in models)

        # Exhausted 429: retries at most 2 times (3 total attempts) and returns explicit unverified reason.
        attempts = 0

        def _always_429(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "quota exceeded"})

        res_exhausted = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-rl-exhausted",
            provider="rl-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-rl",
            base_url="https://rl.acme.test/v1",
            check_result={"success": True},
            model="m-limited",
            custom_provider_entry={"name": "rl-gw", "parent_provider": "openai"},
            transport=httpx.MockTransport(_always_429),
        )
        assert attempts == 3
        assert res_exhausted["tool_calling_models"] == {"m-limited": None}
        detail = res_exhausted["tool_calling_details"]["m-limited"]
        assert detail["status"] == "unverified"
        assert "429" in detail["reason"]
        assert "429" in res_exhausted["tool_calling_errors"]["m-limited"]

    @pytest.mark.asyncio
    async def test_core_tool_probe_shared_transport_not_closed_while_sibling_in_flight(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Concurrent probes share one outer client so transport.aclose() never runs while another probe is still reading."""
        import asyncio
        import httpx

        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))

        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        fast_finished = asyncio.Event()

        class _StrictCloseTransport(httpx.AsyncBaseTransport):
            def __init__(self) -> None:
                self.closed = False
                self.close_count = 0
                self.in_flight = 0
                self.premature_close = False

            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                if self.closed:
                    raise RuntimeError("transport already closed")
                self.in_flight += 1
                try:
                    body = json.loads(request.content.decode("utf-8"))
                    mid = body.get("model", "")
                    if mid == "slow-model":
                        slow_started.set()
                        await release_slow.wait()
                        if self.closed:
                            raise RuntimeError("transport closed mid-read")
                    else:
                        await slow_started.wait()
                    resp = httpx.Response(
                        200,
                        json={
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "tool_calls": [
                                            {
                                                "id": f"call_{mid}",
                                                "type": "function",
                                                "function": {"name": "dalton_probe_ping", "arguments": '{"value":"ok"}'},
                                            }
                                        ],
                                    }
                                }
                            ]
                        },
                    )
                    if mid == "fast-model":
                        fast_finished.set()
                    return resp
                finally:
                    self.in_flight -= 1

            async def aclose(self) -> None:
                if self.in_flight > 0:
                    self.premature_close = True
                self.closed = True
                self.close_count += 1

        transport = _StrictCloseTransport()

        async def _release_after_fast_completes() -> None:
            await fast_finished.wait()
            # Give the event loop a couple ticks so an improperly scoped inner client would have called aclose().
            await asyncio.sleep(0.02)
            release_slow.set()

        releaser = asyncio.create_task(_release_after_fast_completes())
        res = await probe_mod.probe_tool_calling_for_key_test(
            user_id="u-shared-transport",
            provider="shared-gw",
            provider_info={"sdk": "openai"},
            api_key="sk-shared",
            base_url="https://shared.acme.test/v1",
            check_result={"success": True},
            models=["fast-model", "slow-model"],
            custom_provider_entry={"name": "shared-gw", "parent_provider": "openai"},
            transport=transport,
        )
        await releaser

        assert transport.premature_close is False
        assert transport.closed is True
        assert transport.close_count == 1
        assert res["tool_calling_models"] == {"fast-model": True, "slow-model": True}

    @pytest.mark.asyncio
    async def test_core_tool_probe_persists_each_model_immediately_before_gather_finishes(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Each model verdict is persisted as soon as it finishes, surviving cancellation of sibling probes."""
        import asyncio
        import httpx

        from src.dalton.llm import tool_probe as probe_mod

        _set_profile(monkeypatch, app_env="core")
        probe_mod.clear_tool_probe_cache()
        monkeypatch.setattr(probe_mod, "_get_redis_client", create_autospec(probe_mod._get_redis_client, return_value=None))

        m2_entered = asyncio.Event()
        fast_persisted = asyncio.Event()
        orig_record = probe_mod.record_tool_calling_probe

        async def _spy_record(user_id: str, provider: str, model_id: str, supported: bool) -> None:
            await orig_record(user_id, provider, model_id, supported)
            if model_id == "fast-m1":
                fast_persisted.set()

        monkeypatch.setattr(probe_mod, "record_tool_calling_probe", create_autospec(orig_record, side_effect=_spy_record))

        class _PartialCancelTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                body = json.loads(request.content.decode("utf-8"))
                if body.get("model") == "blocked-m2":
                    m2_entered.set()
                    await asyncio.Event().wait()
                return httpx.Response(
                    200,
                    json={
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_fast",
                                            "type": "function",
                                            "function": {"name": "dalton_probe_ping", "arguments": '{"value":"ok"}'},
                                        }
                                    ],
                                }
                            }
                        ]
                    },
                )

        task = asyncio.create_task(
            probe_mod.probe_tool_calling_for_key_test(
                user_id="u-cancel",
                provider="cancel-gw",
                provider_info={"sdk": "openai"},
                api_key="sk-cancel",
                base_url="https://cancel.acme.test/v1",
                check_result={"success": True},
                models=["fast-m1", "blocked-m2"],
                custom_provider_entry={"name": "cancel-gw", "parent_provider": "openai"},
                transport=_PartialCancelTransport(),
            )
        )
        await asyncio.wait_for(asyncio.gather(fast_persisted.wait(), m2_entered.wait()), timeout=2.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        recorded = await probe_mod.get_recorded_tool_probes("u-cancel")
        assert recorded == {"cancel-gw": {"models": {"fast-m1": True}}}

    @pytest.mark.asyncio
    async def test_language_review_model_follows_customer_model_in_core(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile(monkeypatch, app_env="core")
        _mock_user_prefs(
            monkeypatch,
            {"u-core": {"model_preference": {"preferred_model": "gpt-6.2-astra"}}},
            keys_by_user={"u-core": {"byok_enabled": True, "keys": {"openai": "sk-o"}, "base_urls": {}}},
        )
        assert await language_mod.resolve_dalton_language_model("u-core") == "gpt-6.2-astra"
