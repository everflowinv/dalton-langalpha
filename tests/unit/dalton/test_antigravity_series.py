"""Unit tests for Antigravity series grouping, defaults, and /api/v1/models exposure."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest
import pytest_asyncio
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
from src.dalton.llm.antigravity_series import (
    ANTIGRAVITY_SERIES_DEFAULT_EFFORTS,
    FALLBACK_SERIES_DEFAULT_EFFORT,
    REASONING_LEVELS,
    build_antigravity_series_catalog,
    format_series_display_name,
    parse_antigravity_series,
    quota_cooldown_target,
    resolve_antigravity_effort_model,
    resolve_series_default_effort,
)
from src.llms.llm import LLM
from src.llms.reasoning import REASONING_LEVELS as UPSTREAM_REASONING_LEVELS
from tests.conftest import create_test_app


class TestParseAntigravitySeries:
    def test_parses_suffixed_keys(self):
        assert parse_antigravity_series("gemini-4-neon-low-oauth") == ("gemini-4-neon", "low")
        assert parse_antigravity_series("gemini-4-neon-medium-oauth") == ("gemini-4-neon", "medium")
        assert parse_antigravity_series("gemini-4-neon-high-oauth") == ("gemini-4-neon", "high")
        assert parse_antigravity_series("gemini-4-neon-xhigh-oauth") == ("gemini-4-neon", "xhigh")
        assert parse_antigravity_series("claude-opus-5-5-medium-oauth") == ("claude-opus-5-5", "medium")
        assert parse_antigravity_series("gpt-oss-120b-medium-oauth") == ("gpt-oss-120b", "medium")

    def test_parses_unsuffixed_oauth_key_as_none_effort(self):
        assert parse_antigravity_series("gemini-2.5-pro-oauth") == ("gemini-2.5-pro", None)

    def test_rejects_non_oauth_or_empty(self):
        assert parse_antigravity_series("gemini-4-neon") is None
        assert parse_antigravity_series("-oauth") is None
        assert parse_antigravity_series("") is None

    def test_reasoning_levels_stay_aligned_with_upstream(self):
        assert REASONING_LEVELS == UPSTREAM_REASONING_LEVELS


class TestResolveSeriesDefaultEffort:
    def test_confirmed_series_defaults(self):
        assert ANTIGRAVITY_SERIES_DEFAULT_EFFORTS == {
            "gemini-4-neon": "high",
            "gemini-4-hydrogen": "high",
            "gemini-4-argon": "high",
            "gemini-4-carbon": "medium",
        }
        assert FALLBACK_SERIES_DEFAULT_EFFORT == "medium"

        assert resolve_series_default_effort("gemini-4-neon", ["low", "medium", "high", "xhigh"]) == "high"
        assert resolve_series_default_effort("gemini-4-hydrogen", ["low", "medium", "high", "xhigh"]) == "high"
        assert resolve_series_default_effort("gemini-4-argon", ["low", "medium", "high"]) == "high"
        assert resolve_series_default_effort("gemini-4-carbon", ["low", "medium", "high"]) == "medium"
        assert resolve_series_default_effort("claude-opus-5-5", ["low", "medium", "high"]) == "medium"

    def test_clamps_down_when_default_missing_and_takes_lowest_when_all_above(self):
        # Neon defaults to high; if high is missing, step down to medium (not xhigh)
        assert resolve_series_default_effort("gemini-4-neon", ["low", "medium", "xhigh"]) == "medium"
        # If only xhigh exists above target high, take lowest available (xhigh)
        assert resolve_series_default_effort("gemini-4-neon", ["xhigh"]) == "xhigh"
        # Unordered efforts still canonicalize before clamping
        assert resolve_series_default_effort("gemini-4-carbon", ["high", "low"]) == "low"

    def test_dalton_yaml_override(self, tmp_path: Path):
        yaml_file = tmp_path / "dalton.yaml"
        yaml_file.write_text(
            "llm:\n  antigravity_series_default_efforts:\n    gemini-4-carbon: high\n",
            encoding="utf-8",
        )
        assert (
            resolve_series_default_effort(
                "gemini-4-carbon",
                ["low", "medium", "high"],
                dalton_yaml_path=yaml_file,
            )
            == "high"
        )


class TestBuildAntigravitySeriesCatalog:
    def test_builds_seven_visible_series_from_live_catalog(self):
        mc = LLM.get_model_config()
        catalog = build_antigravity_series_catalog(mc)
        by_id = {row["series_id"]: row for row in catalog}

        assert list(by_id.keys()) == [
            "antigravity-oauth:gemini-4-argon",
            "antigravity-oauth:gemini-4-carbon",
            "antigravity-oauth:gemini-4-hydrogen",
            "antigravity-oauth:gemini-4-neon",
            "antigravity-oauth:claude-opus-5-5",
            "antigravity-oauth:claude-sonnet-5-5",
            "antigravity-oauth:gpt-oss-120b",
        ]
        # Hidden gemini-2.5-pro-oauth must be excluded
        assert "antigravity-oauth:gemini-2.5-pro" not in by_id

        assert by_id["antigravity-oauth:gemini-4-neon"] == {
            "series_id": "antigravity-oauth:gemini-4-neon",
            "series_prefix": "gemini-4-neon",
            "provider": "antigravity-oauth",
            "display_name": "Gemini 4 Neon",
            "qualified_display_name": "Gemini 4 Neon (Antigravity)",
            "default_effort": "high",
            "default_model": "gemini-4-neon-high-oauth",
            "efforts": ["low", "medium", "high", "xhigh"],
            "variants": {
                "low": "gemini-4-neon-low-oauth",
                "medium": "gemini-4-neon-medium-oauth",
                "high": "gemini-4-neon-high-oauth",
                "xhigh": "gemini-4-neon-xhigh-oauth",
            },
        }
        assert by_id["antigravity-oauth:gemini-4-hydrogen"]["default_model"] == "gemini-4-hydrogen-high-oauth"
        assert by_id["antigravity-oauth:gemini-4-argon"]["default_model"] == "gemini-4-argon-high-oauth"
        assert by_id["antigravity-oauth:gemini-4-carbon"]["default_model"] == "gemini-4-carbon-medium-oauth"
        assert by_id["antigravity-oauth:claude-opus-5-5"]["display_name"] == "Claude Opus 5.5"
        assert by_id["antigravity-oauth:claude-opus-5-5"]["default_model"] == "claude-opus-5-5-medium-oauth"
        assert by_id["antigravity-oauth:gpt-oss-120b"]["display_name"] == "GPT-OSS 120B"
        assert by_id["antigravity-oauth:gpt-oss-120b"]["efforts"] == ["medium"]

    def test_excludes_hidden_and_retired_and_absorbs_dynamic_variants(self):
        synthetic = {
            "gemini-4-neon-low-oauth": {"provider": "antigravity-oauth", "visible": True},
            "gemini-4-neon-high-oauth": {"provider": "antigravity-oauth", "visible": True, "retired": True},
            "gemini-4-neon-max-oauth": {"provider": "antigravity-oauth", "visible": True},
            "gemini-2.5-pro-high-oauth": {"provider": "antigravity-oauth", "visible": False},
            "gemini-4-nova-low-oauth": {"provider": "antigravity-oauth", "visible": True},
            "gemini-4-nova-xhigh-oauth": {"provider": "antigravity-oauth", "visible": True},
            # Non-antigravity entries must be ignored
            "gemini-4-neon": {"provider": "gemini", "visible": True},
        }
        catalog = build_antigravity_series_catalog(synthetic)
        by_id = {row["series_id"]: row for row in catalog}

        assert "antigravity-oauth:gemini-2.5-pro" not in by_id
        neon = by_id["antigravity-oauth:gemini-4-neon"]
        assert neon["efforts"] == ["low", "max"]
        assert neon["default_effort"] == "low"  # high retired -> steps down to low
        assert neon["default_model"] == "gemini-4-neon-low-oauth"

        nova = by_id["antigravity-oauth:gemini-4-nova"]
        assert nova["display_name"] == format_series_display_name("gemini-4-nova")
        assert nova["efforts"] == ["low", "xhigh"]
        assert nova["default_effort"] == "low"  # fallback medium -> steps down to low


@pytest_asyncio.fixture
async def models_client():
    from src.server.app.api_keys import router

    app = create_test_app(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with (
            patch("src.server.app.api_keys._get_supported_providers", autospec=True, return_value=["antigravity-oauth"]),
            patch(
                "src.server.app.api_keys._get_provider_info_map",
                autospec=True,
                return_value={
                    "antigravity-oauth": {
                        "display_name": "Google Antigravity",
                        "access_type": "oauth",
                        "brand_key": "google",
                    }
                },
            ),
            patch("src.server.app.api_keys.provider_catalog", autospec=True, return_value=[]),
        ):
            yield client


@pytest.mark.asyncio
async def test_models_endpoint_exposes_antigravity_series_and_keeps_legacy_keys(models_client):
    from ptc_agent.config import agent as agent_cfg_mod

    real_mc = LLM.get_model_config()
    agent_cfg = AgentConfig(
        llm=LLMConfig(name="gemini-4-neon-high-oauth"),
        security=SecurityConfig(),
        logging=LoggingConfig(),
        sandbox=SandboxConfig(daytona=DaytonaConfig(api_key="test")),
        mcp=MCPConfig(),
        filesystem=FilesystemConfig(),
    )
    from src.llms import llm as llm_mod

    stub_get_models = create_autospec(
        llm_mod.get_configured_llm_models,
        return_value={"antigravity-oauth": [{"name": "gemini-4-neon-high-oauth", "model_id": "gemini-4-neon-high"}]},
    )

    with (
        patch("src.llms.llm.get_configured_llm_models", stub_get_models),
        patch.object(LLM, "get_model_config", autospec=True, return_value=real_mc),
        patch("src.server.app.setup.agent_config", agent_cfg),
    ):
        resp = await models_client.get("/api/v1/models")

    assert resp.status_code == 200
    body = resp.json()
    assert "antigravity_series" in body
    assert len(body["antigravity_series"]) == 7

    neon = next(s for s in body["antigravity_series"] if s["series_id"] == "antigravity-oauth:gemini-4-neon")
    assert neon["default_effort"] == "high"
    assert neon["default_model"] == "gemini-4-neon-high-oauth"

    # Legacy keys remain unchanged with suffixed entries intact
    assert body["models"]["antigravity-oauth"]["models"] == [
        {"name": "gemini-4-neon-high-oauth", "model_id": "gemini-4-neon-high"}
    ]
    assert "gemini-4-neon-high-oauth" in body["model_metadata"]
    assert "gemini-4-neon-medium-oauth" in body["model_metadata"]
    assert agent_cfg_mod.COMPACTION_PROFILES is not None


class TestQuotaCooldownTarget:
    def test_builtin_antigravity_models_map_to_vendor_pools(self):
        mc = LLM.get_model_config()
        # All built-in Gemini series/tiers share the `google` vendor pool
        for key in (
            "gemini-4-neon-low-oauth",
            "gemini-4-neon-medium-oauth",
            "gemini-4-neon-high-oauth",
            "gemini-4-neon-xhigh-oauth",
            "gemini-4-hydrogen-high-oauth",
            "gemini-4-argon-low-oauth",
            "gemini-4-carbon-medium-oauth",
            "gemini-2.5-pro-oauth",
        ):
            assert quota_cooldown_target("antigravity-oauth", key, model_config=mc) == (
                "vendor",
                "google",
            )
        # Built-in Antigravity Claude models share the `anthropic` vendor pool
        for key in (
            "claude-opus-5-5-low-oauth",
            "claude-opus-5-5-high-oauth",
            "claude-sonnet-5-5-medium-oauth",
        ):
            assert quota_cooldown_target("antigravity-oauth", key, model_config=mc) == (
                "vendor",
                "anthropic",
            )
        # Built-in Antigravity GPT-OSS maps to the `openai` vendor pool
        assert quota_cooldown_target(
            "antigravity-oauth", "gpt-oss-120b-medium-oauth", model_config=mc
        ) == ("vendor", "openai")

    def test_unknown_prefix_custom_same_name_and_other_providers_stay_model_scoped(self):
        mc = LLM.get_model_config()
        # Built-in Antigravity model with an unmapped vendor prefix falls back to per-model
        synthetic = {
            "llama-4-medium-oauth": {
                "provider": "antigravity-oauth",
                "model_id": "llama-4-medium",
            }
        }
        assert quota_cooldown_target(
            "antigravity-oauth", "llama-4-medium-oauth", model_config=synthetic
        ) == ("model", "llama-4-medium-oauth")
        # User custom model shadowing a built-in Antigravity name stays per-model
        assert quota_cooldown_target(
            "antigravity-oauth",
            "gemini-4-neon-high-oauth",
            is_custom=True,
            model_config=mc,
        ) == ("model", "gemini-4-neon-high-oauth")
        # Unknown model name not in built-in catalog stays per-model
        assert quota_cooldown_target(
            "antigravity-oauth",
            "google-vendor-custom",
            is_custom=False,
            model_config=mc,
        ) == ("model", "google-vendor-custom")
        assert quota_cooldown_target(
            "antigravity-oauth",
            "custom-user-model-high-oauth",
            is_custom=False,
            model_config=mc,
        ) == ("model", "custom-user-model-high-oauth")
        # Non-Antigravity providers stay per-model
        assert quota_cooldown_target(
            "claude-oauth", "claude-opus-5-5-oauth", model_config=mc
        ) == ("model", "claude-opus-5-5-oauth")
        assert quota_cooldown_target(
            "gemini", "gemini-4-neon", model_config=mc
        ) == ("model", "gemini-4-neon")


class TestResolveAntigravityEffortModel:
    def test_maps_suffixed_series_model_to_requested_or_clamped_variant(self):
        mc = LLM.get_model_config()
        assert resolve_antigravity_effort_model(
            "gemini-4-neon-high-oauth", "low", model_config=mc
        ) == ("gemini-4-neon-low-oauth", "low")
        assert resolve_antigravity_effort_model(
            "gemini-4-neon-low-oauth", "xhigh", model_config=mc
        ) == ("gemini-4-neon-xhigh-oauth", "xhigh")
        # max steps down to xhigh on gemini-4-neon
        assert resolve_antigravity_effort_model(
            "gemini-4-neon-high-oauth", "max", model_config=mc
        ) == ("gemini-4-neon-xhigh-oauth", "xhigh")
        # xhigh steps down to high on gemini-4-carbon (only low/medium/high)
        assert resolve_antigravity_effort_model(
            "gemini-4-carbon-medium-oauth", "xhigh", model_config=mc
        ) == ("gemini-4-carbon-high-oauth", "high")
        # low steps up to lowest available (medium) on gpt-oss-120b
        assert resolve_antigravity_effort_model(
            "gpt-oss-120b-medium-oauth", "low", model_config=mc
        ) == ("gpt-oss-120b-medium-oauth", "medium")

    def test_returns_none_for_unsuffixed_or_non_antigravity_models(self):
        mc = LLM.get_model_config()
        assert resolve_antigravity_effort_model("gemini-2.5-pro-oauth", "high", model_config=mc) is None
        assert resolve_antigravity_effort_model("claude-opus-5-5-oauth", "low", model_config=mc) is None
        assert resolve_antigravity_effort_model("gpt-6.2-sol", "high", model_config=mc) is None
        assert resolve_antigravity_effort_model("gemini-4-neon-high-oauth", "bogus", model_config=mc) is None

    def test_allowed_filter_steps_down_then_up_or_returns_none(self):
        mc = LLM.get_model_config()
        # Only Neon Low allowed -> high steps down to low
        assert resolve_antigravity_effort_model(
            "gemini-4-neon-low-oauth",
            "high",
            model_config=mc,
            allowed_filter=lambda m: m == "gemini-4-neon-low-oauth",
        ) == ("gemini-4-neon-low-oauth", "low")
        # Low and xhigh allowed -> high prefers lower (low) before higher (xhigh)
        assert resolve_antigravity_effort_model(
            "gemini-4-neon-low-oauth",
            "high",
            model_config=mc,
            allowed_filter=lambda m: m in {"gemini-4-neon-low-oauth", "gemini-4-neon-xhigh-oauth"},
        ) == ("gemini-4-neon-low-oauth", "low")
        # Only high and xhigh allowed -> low steps up to closest higher (high)
        assert resolve_antigravity_effort_model(
            "gemini-4-neon-high-oauth",
            "low",
            model_config=mc,
            allowed_filter=lambda m: m in {"gemini-4-neon-high-oauth", "gemini-4-neon-xhigh-oauth"},
        ) == ("gemini-4-neon-high-oauth", "high")
        # None in series allowed -> returns None
        assert resolve_antigravity_effort_model(
            "gemini-4-neon-low-oauth",
            "high",
            model_config=mc,
            allowed_filter=lambda _m: False,
        ) is None


def _allow_only_policy(*models: str):
    from src.config.models import ModelsPolicy, PolicyLayer
    from src.server.services.policy import Policy

    return Policy(
        models_allowed=frozenset(models),
        allowed_in_order=tuple(models),
        layers=(("deployment", PolicyLayer(models=ModelsPolicy(allowed=list(models)))),),
    )


def test_subagent_roles_antigravity_variant_respects_policy_allowlist():
    from ptc_agent.agent.subagents.definition import SubagentDefinition
    from ptc_agent.agent.subagents.merge import ResolvedSubagents
    from src.server.services.llm.subagents import subagent_roles

    defn = SubagentDefinition(
        name="research",
        description="Research",
        tools=("web_search",),
        model=None,
        reasoning_effort="high",
    )
    roster = ResolvedSubagents(
        enabled=("research",),
        definitions={"research": defn},
        orphaned=(),
        shadowed=(),
    )
    only_low = _allow_only_policy("gemini-4-neon-low-oauth")
    roles = subagent_roles(
        roster,
        model_pref={},
        turn_model="gemini-4-neon-low-oauth",
        policy=only_low,
    )
    assert len(roles) == 1
    assert roles[0].model == "gemini-4-neon-low-oauth"
    assert roles[0].reasoning_effort is None

    # When no variant in the series is allowed at all, no disallowed role is emitted.
    only_other = _allow_only_policy("gpt-6.2-sol")
    skipped = subagent_roles(
        roster,
        model_pref={},
        turn_model="gemini-4-neon-low-oauth",
        policy=only_other,
    )
    assert skipped == []


@pytest.mark.asyncio
async def test_task_model_gate_variant_policy_and_effort_priority(monkeypatch):
    from types import SimpleNamespace
    from ptc_agent.config.agent import CredentialSource, TaskAsk, TaskModel, TaskModelRefused
    from src.server.services.llm import task_model as tm
    from src.server.services.llm import user_models
    from src.server.services.llm.clients import ResolvedClient

    real_mc = LLM.get_model_config()
    captured: list[tuple[str, str | None]] = []

    async def fake_classify(_user, name, _pref_cache=None):
        entry = real_mc.get_model_config(name)
        return (user_models.ModelSource.SYSTEM, entry) if entry else (user_models.ModelSource.UNKNOWN, {})

    async def fake_resolve_client(user_id, name, **kwargs):
        captured.append((name, kwargs.get("reasoning_effort")))
        entry = real_mc.get_model_config(name) or {}
        eff = (entry.get("reasoning") or {}).get("default")
        client = SimpleNamespace(metadata={"manifest_model": name, "reasoning_effort": eff}, model=name)
        return ResolvedClient(client, user_models.ModelSource.SYSTEM, CredentialSource.OAUTH)

    async def fake_plan_covers(*_args, **_kwargs):
        return True

    monkeypatch.setattr(user_models, "classify_model", fake_classify)
    monkeypatch.setattr(user_models, "model_entry", lambda _pref, name: real_mc.get_model_config(name))
    monkeypatch.setattr(user_models, "guidance_for", lambda _pref, name: "")
    monkeypatch.setattr(tm, "model_resolves", lambda _pref, name: real_mc.get_model_config(name) is not None)
    monkeypatch.setattr(tm, "plan_covers", fake_plan_covers)
    monkeypatch.setattr(tm, "resolve_model_client", fake_resolve_client)

    def _make_gate(*, turn_model: str, defn_effort: str | None = None, policy=None):
        monkeypatch.setattr(
            tm,
            "turn_roster",
            lambda _s: SimpleNamespace(
                definitions={"research": SimpleNamespace(model=None, reasoning_effort=defn_effort)}
            ),
        )
        cfg = SimpleNamespace(
            credential_source=CredentialSource.OAUTH,
            compaction=SimpleNamespace(profile="expansive"),
            claude_code=False,
            claude_code_admitted=None,
            claude_code_pins=None,
            subsidiary_llm_clients={},
            subagents=None,
        )
        return tm.TaskModelGate(
            cfg,
            user_id="u-1",
            workspace_id="w-1",
            thread_id="t-1",
            model_pref={},
            permits=SimpleNamespace(),
            is_byok=False,
            turn_model=turn_model,
            plan_exempt=frozenset(),
            policy=policy,
        )

    # 1. Policy allows only Neon Low: explicit Task(effort="high") must stay on Neon Low.
    gate_low_only = _make_gate(
        turn_model="gemini-4-neon-low-oauth",
        policy=_allow_only_policy("gemini-4-neon-low-oauth"),
    )
    res_low = await gate_low_only.resolve(TaskAsk(effort="high"), "research")
    assert isinstance(res_low, TaskModel)
    assert res_low.name == "gemini-4-neon-low-oauth"
    assert captured[-1] == ("gemini-4-neon-low-oauth", None)

    # 2. Policy allows none of Neon: refused clearly without calling resolve_model_client.
    before_calls = len(captured)
    gate_other_only = _make_gate(
        turn_model="gpt-6.2-sol",
        policy=_allow_only_policy("gpt-6.2-sol"),
    )
    refused = await gate_other_only.resolve(
        TaskAsk(model="gemini-4-neon-low-oauth", effort="high"), "research"
    )
    assert isinstance(refused, TaskModelRefused)
    assert "not allowed" in refused.message
    assert len(captured) == before_calls

    # 3. Effort priority: Task(model=Neon Low) without effort inherits saved subagent effort="high".
    gate_saved_high = _make_gate(turn_model="gpt-6.2-sol", defn_effort="high")
    inherited = await gate_saved_high.resolve(TaskAsk(model="gemini-4-neon-low-oauth"), "research")
    assert isinstance(inherited, TaskModel)
    assert inherited.name == "gemini-4-neon-high-oauth"
    assert captured[-1] == ("gemini-4-neon-high-oauth", None)

    # 4. Explicit call effort overrides saved subagent effort="high".
    overridden = await gate_saved_high.resolve(
        TaskAsk(model="gemini-4-neon-low-oauth", effort="medium"), "research"
    )
    assert isinstance(overridden, TaskModel)
    assert overridden.name == "gemini-4-neon-medium-oauth"
    assert captured[-1] == ("gemini-4-neon-medium-oauth", None)


@pytest.mark.asyncio
async def test_resolve_llm_config_antigravity_dispatch_effort_respects_policy(monkeypatch):
    import copy
    from types import SimpleNamespace
    from ptc_agent.config.agent import LLMConfig
    from src.server.services.llm import config as llm_cfg
    from src.server.services.llm import user_models

    only_low = _allow_only_policy("gemini-4-neon-low-oauth")
    resolved_clients_args: list[tuple[str, str | None]] = []

    async def fake_require_policy(_uid):
        return only_low

    async def fake_get_pref(_uid):
        return {}

    async def fake_gate_plan(_base, _uid, pref, _req, **_kw):
        return pref

    async def fake_load_ctx(*_a, **_kw):
        return None

    async def fake_gate_caps(*_a, **_kw):
        return None

    async def fake_check_avail(*_a, **_kw):
        return False

    async def fake_resolve_clients(config, _uid, effective_model, _pref, _roles, **kwargs):
        resolved_clients_args.append((effective_model, kwargs.get("reasoning_effort")))

    monkeypatch.setattr(llm_cfg, "require_policy", fake_require_policy)
    monkeypatch.setattr(user_models, "get_model_preference", fake_get_pref)
    monkeypatch.setattr(llm_cfg, "gate_models_by_plan", fake_gate_plan)
    monkeypatch.setattr(llm_cfg, "load_user_context", fake_load_ctx)
    monkeypatch.setattr(llm_cfg, "gate_capabilities", fake_gate_caps)
    monkeypatch.setattr(llm_cfg, "check_model_available", fake_check_avail)
    monkeypatch.setattr(llm_cfg, "resolve_clients", fake_resolve_clients)

    class _FakeBaseConfig:
        def __init__(self):
            self.llm = LLMConfig(name="gemini-4-neon-low-oauth")
            self.llm_client = None
            self.compaction = SimpleNamespace(native=True, cold_compact_tokens=None, profile="expansive", token_threshold=100_000)
            self.subagents = SimpleNamespace(resolved=None, enabled=(), definitions=None)
            self.subsidiary_llm_clients = {}
            self.role_prompt_guidance = {}
            self.claude_code = False
            self.claude_code_admitted = None
            self.claude_code_pins = None
            self.search_api = "tavily"
            self.search_depth = "basic"

        def model_copy(self, *, deep: bool = False):
            return copy.deepcopy(self) if deep else copy.copy(self)

    base = _FakeBaseConfig()
    cfg = await llm_cfg.resolve_llm_config(
        base,
        user_id="u-1",
        request_model="gemini-4-neon-low-oauth",
        is_byok=False,
        slot="primary",
        reasoning_effort="high",
        user_facing=False,
    )
    assert cfg.llm.name == "gemini-4-neon-low-oauth"
    assert resolved_clients_args == [("gemini-4-neon-low-oauth", None)]


@pytest.mark.asyncio
async def test_role_fallback_re_resolves_effort_in_both_directions(monkeypatch):
    from types import SimpleNamespace
    from ptc_agent.agent.subagents.definition import SubagentDefinition
    from ptc_agent.agent.subagents.merge import ResolvedSubagents
    from ptc_agent.config.agent import CredentialSource
    from src.server.services.llm import clients as llm_clients
    from src.server.services.llm import user_models
    from src.server.services.llm.clients import ResolvedClient
    from src.server.services.llm.subagents import subagent_roles

    real_mc = LLM.get_model_config()

    def _make_roster(pinned_model: str, effort: str):
        defn = SubagentDefinition(
            name="research",
            description="Research",
            tools=("web_search",),
            model=pinned_model,
            reasoning_effort=effort,
        )
        return ResolvedSubagents(
            enabled=("research",),
            definitions={"research": defn},
            orphaned=(),
            shadowed=(),
        )

    monkeypatch.setattr(user_models, "guidance_for", lambda _pref, name: f"g:{name}")
    monkeypatch.setattr(llm_clients, "_stamp", lambda *_a, **_kw: None)

    # Direction 1: non-Antigravity pin (gpt-6.2-sol, effort="high") unavailable ->
    # main_model is Antigravity Neon Low -> fallback maps to Neon High with effort=None.
    calls_d1: list[tuple[str, str | None]] = []

    async def fake_resolve_d1(_uid, model, **kwargs):
        calls_d1.append((model, kwargs.get("reasoning_effort")))
        if model == "gpt-6.2-sol":
            return ResolvedClient(None, user_models.ModelSource.SYSTEM, CredentialSource.NONE)
        client = SimpleNamespace(metadata={"manifest_model": model}, model=model)
        return ResolvedClient(client, user_models.ModelSource.SYSTEM, CredentialSource.OAUTH)

    monkeypatch.setattr(llm_clients, "resolve_model_client", fake_resolve_d1)
    cfg1 = SimpleNamespace(
        credential_source=CredentialSource.OAUTH,
        llm_client=SimpleNamespace(model_copy=lambda: SimpleNamespace(metadata={"manifest_model": "gemini-4-neon-low-oauth"})),
        subsidiary_llm_clients={},
        role_prompt_guidance={},
        prompt_guidance="g:main",
        compaction=SimpleNamespace(profile="expansive"),
    )
    roles_d1 = subagent_roles(_make_roster("gpt-6.2-sol", "high"), {}, "gemini-4-neon-low-oauth")
    await llm_clients._resolve_role_clients(
        cfg1, "u-1", roles_d1, {}, {}, is_byok=False, cache_key=None, main_model="gemini-4-neon-low-oauth"
    )
    assert calls_d1 == [("gpt-6.2-sol", "high"), ("gemini-4-neon-high-oauth", None)]
    assert cfg1.subsidiary_llm_clients["subagent:research"].model == "gemini-4-neon-high-oauth"

    # Direction 1b: same fallback under policy allowing ONLY Neon Low -> stays on Neon Low, effort=None.
    calls_d1_pol: list[tuple[str, str | None]] = []

    async def fake_resolve_d1_pol(_uid, model, **kwargs):
        calls_d1_pol.append((model, kwargs.get("reasoning_effort")))
        if model == "gpt-6.2-sol":
            return ResolvedClient(None, user_models.ModelSource.SYSTEM, CredentialSource.NONE)
        client = SimpleNamespace(metadata={"manifest_model": model}, model=model)
        return ResolvedClient(client, user_models.ModelSource.SYSTEM, CredentialSource.OAUTH)

    monkeypatch.setattr(llm_clients, "resolve_model_client", fake_resolve_d1_pol)
    cfg1_pol = SimpleNamespace(
        credential_source=CredentialSource.OAUTH,
        llm_client=SimpleNamespace(model_copy=lambda: SimpleNamespace(metadata={"manifest_model": "gemini-4-neon-low-oauth"})),
        subsidiary_llm_clients={},
        role_prompt_guidance={},
        prompt_guidance="g:main",
        compaction=SimpleNamespace(profile="expansive"),
    )
    await llm_clients._resolve_role_clients(
        cfg1_pol,
        "u-1",
        roles_d1,
        {},
        {},
        is_byok=False,
        cache_key=None,
        policy=_allow_only_policy("gemini-4-neon-low-oauth"),
        main_model="gemini-4-neon-low-oauth",
    )
    assert calls_d1_pol == [("gpt-6.2-sol", "high"), ("gemini-4-neon-low-oauth", None)]
    assert cfg1_pol.subsidiary_llm_clients["subagent:research"].model == "gemini-4-neon-low-oauth"

    # Direction 2: Antigravity pin (Neon Low + effort="high" -> mapped to Neon High) unavailable ->
    # main_model is non-Antigravity gpt-6.2-sol -> fallback passes reasoning_effort="high" to gpt-6.2-sol.
    calls_d2: list[tuple[str, str | None]] = []

    async def fake_resolve_d2(_uid, model, **kwargs):
        calls_d2.append((model, kwargs.get("reasoning_effort")))
        if model.startswith("gemini-4-neon-"):
            return ResolvedClient(None, user_models.ModelSource.SYSTEM, CredentialSource.NONE)
        client = SimpleNamespace(metadata={"manifest_model": model, "reasoning_effort": kwargs.get("reasoning_effort")}, model=model)
        return ResolvedClient(client, user_models.ModelSource.SYSTEM, CredentialSource.BYOK)

    monkeypatch.setattr(llm_clients, "resolve_model_client", fake_resolve_d2)
    assert real_mc.get_model_config("gemini-4-neon-low-oauth") is not None
    cfg2 = SimpleNamespace(
        credential_source=CredentialSource.BYOK,
        llm_client=SimpleNamespace(model_copy=lambda: SimpleNamespace(metadata={"manifest_model": "gpt-6.2-sol"})),
        subsidiary_llm_clients={},
        role_prompt_guidance={},
        prompt_guidance="g:main",
        compaction=SimpleNamespace(profile="expansive"),
    )
    roles_d2 = subagent_roles(_make_roster("gemini-4-neon-low-oauth", "high"), {}, "gpt-6.2-sol")
    assert roles_d2[0].model == "gemini-4-neon-high-oauth"
    assert roles_d2[0].effort_follows is True
    assert roles_d2[0].follow_effort == "high"
    await llm_clients._resolve_role_clients(
        cfg2, "u-1", roles_d2, {}, {}, is_byok=True, cache_key=None, main_model="gpt-6.2-sol"
    )
    assert calls_d2 == [("gemini-4-neon-high-oauth", None), ("gpt-6.2-sol", "high")]
    assert cfg2.subsidiary_llm_clients["subagent:research"].model == "gpt-6.2-sol"
    assert cfg2.subsidiary_llm_clients["subagent:research"].metadata["reasoning_effort"] == "high"


@pytest.mark.asyncio
async def test_dispatch_result_reports_actual_model_separately_from_requested_model(monkeypatch):
    import json
    from unittest.mock import AsyncMock, patch
    from src.tools.secretary.dispatch import dispatch

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-token")

    class _FakeResp:
        status = 200

        async def json(self):
            return {
                "status": "dispatched",
                "thread_id": "11111111-1111-4111-8111-111111111111",
                "run_id": "run-ag-1",
                "workspace_id": "ws-colleague",
                "llm_model": "gemini-4-neon-high-oauth",
                "reasoning_effort": "high",
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class _FakeSession:
        def post(self, *args, **kwargs):
            return _FakeResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    for target_ws, owner_id in (("ws-colleague", "colleague-user"), ("ws-own", "caller-1")):
        with patch(
            "src.tools.secretary.dispatch._owned_workspace",
            AsyncMock(
                return_value={
                    "workspace_id": target_ws,
                    "user_id": owner_id,
                    "name": "Semis",
                    "_colleague_cos_access": "rw",
                }
            ),
        ), patch(
            "src.dalton.settings.is_colleague_dispatch_enabled", return_value=True
        ), patch(
            "src.dalton.sharing.require_workspace_access", AsyncMock(return_value={})
        ), patch(
            "src.dalton.sharing.build_dispatched_by_meta",
            AsyncMock(return_value={"user_id": "caller-1", "channel": "cos"}),
        ), patch(
            "src.server.database.conversation.create_thread", AsyncMock(return_value=None)
        ), patch(
            "src.tools.secretary.dispatch._unreachable_computer", AsyncMock(return_value=None)
        ), patch(
            "src.tools.secretary.dispatch.hitl_confirm", return_value=(True, {})
        ), patch(
            "aiohttp.ClientSession", return_value=_FakeSession()
        ):
            cmd = await dispatch(
                question="Analyze NVDA",
                config={"configurable": {"user_id": "caller-1", "workspace_id": "home-ws", "thread_id": "cos-1"}},
                workspace_id=target_ws,
                thread_id=None,
                report_back=False,
                tool_call_id="tc-1",
                model="gemini-4-neon-low-oauth",
                effort="high",
            )

        payload = json.loads(cmd.update["messages"][0].content)
        assert payload["success"] is True
        assert payload["model"] == "gemini-4-neon-low-oauth"
        assert payload["actual_model"] == "gemini-4-neon-high-oauth"
        assert payload["effort"] == "high"


@pytest.mark.asyncio
async def test_task_model_gate_maps_antigravity_effort_to_variant_and_omits_raw_effort(monkeypatch):
    from types import SimpleNamespace
    from ptc_agent.config.agent import CredentialSource, TaskAsk, TaskModel
    from src.server.services.llm import task_model as tm
    from src.server.services.llm import user_models
    from src.server.services.llm.clients import ResolvedClient

    real_mc = LLM.get_model_config()
    captured: list[tuple[str, str | None]] = []

    async def fake_classify(_user, name, _pref_cache=None):
        entry = real_mc.get_model_config(name)
        return (user_models.ModelSource.SYSTEM, entry) if entry else (user_models.ModelSource.UNKNOWN, {})

    async def fake_resolve_client(user_id, name, **kwargs):
        captured.append((name, kwargs.get("reasoning_effort")))
        entry = real_mc.get_model_config(name) or {}
        eff = (entry.get("reasoning") or {}).get("default")
        client = SimpleNamespace(metadata={"manifest_model": name, "reasoning_effort": eff}, model=name)
        return ResolvedClient(client, user_models.ModelSource.SYSTEM, CredentialSource.OAUTH)

    monkeypatch.setattr(user_models, "classify_model", fake_classify)
    monkeypatch.setattr(user_models, "model_entry", lambda _pref, name: real_mc.get_model_config(name))
    monkeypatch.setattr(user_models, "guidance_for", lambda _pref, name: "")
    monkeypatch.setattr(tm, "model_resolves", lambda _pref, name: real_mc.get_model_config(name) is not None)
    monkeypatch.setattr(tm, "resolve_model_client", fake_resolve_client)
    monkeypatch.setattr(
        tm, "turn_roster",
        lambda _s: SimpleNamespace(definitions={"research": SimpleNamespace(model=None, reasoning_effort=None)}),
    )

    cfg = SimpleNamespace(
        credential_source=CredentialSource.OAUTH,
        compaction=SimpleNamespace(profile="expansive"),
        claude_code=False,
        claude_code_admitted=None,
        claude_code_pins=None,
        subsidiary_llm_clients={},
        subagents=None,
    )
    gate = tm.TaskModelGate(
        cfg,
        user_id="u-1",
        workspace_id="w-1",
        thread_id="t-1",
        model_pref={},
        permits=SimpleNamespace(),
        is_byok=False,
        turn_model="gemini-4-neon-high-oauth",
        plan_exempt=frozenset(),
    )

    # Own model is gemini-4-neon-high-oauth; asking effort="low" maps to gemini-4-neon-low-oauth
    # and passes reasoning_effort=None (never raw effort) to resolve_model_client.
    resolved = await gate.resolve(TaskAsk(effort="low"), "research")
    assert isinstance(resolved, TaskModel)
    assert resolved.name == "gemini-4-neon-low-oauth"
    assert resolved.effort == "low"
    assert captured[-1] == ("gemini-4-neon-low-oauth", None)

    # Asking effort="max" clamps down to gemini-4-neon-xhigh-oauth.
    clamped = await gate.resolve(TaskAsk(effort="max"), "research")
    assert isinstance(clamped, TaskModel)
    assert clamped.name == "gemini-4-neon-xhigh-oauth"
    assert clamped.effort == "max"
    assert captured[-1] == ("gemini-4-neon-xhigh-oauth", None)
