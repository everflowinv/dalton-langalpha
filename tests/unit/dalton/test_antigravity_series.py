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
            patch("src.server.app.api_keys._get_supported_providers", return_value=["antigravity-oauth"]),
            patch(
                "src.server.app.api_keys._get_provider_info_map",
                return_value={
                    "antigravity-oauth": {
                        "display_name": "Google Antigravity",
                        "access_type": "oauth",
                        "brand_key": "google",
                    }
                },
            ),
            patch("src.server.app.api_keys.provider_catalog", return_value=[]),
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
    stub_get_models = create_autospec(
        lambda: {},
        return_value={"antigravity-oauth": [{"name": "gemini-4-neon-high-oauth", "model_id": "gemini-4-neon-high"}]},
    )

    with (
        patch("src.llms.llm.get_configured_llm_models", stub_get_models),
        patch.object(LLM, "get_model_config", return_value=real_mc),
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
