"""Unit tests for unified deployment profile resolution, Core YAML overlays, and SEC default identity."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from ptc_agent.config.file_utils import load_yaml_config
from ptc_agent.config.loaders import load_from_dict
from src.config.core import clear_config_cache, load_infrastructure_config
from src.dalton.llm.role_policy import (
    DaltonStartupConfigError,
    _load_default_agent_config,
    enforce_role_policy_on_startup,
)
from src.dalton.profile import (
    DeploymentProfile,
    ProfileConfigError,
    is_core_profile,
    resolve_profile,
    resolve_yaml_overlay_env,
)
from src.dalton.settings import (
    DEFAULT_SEC_USER_AGENT,
    get_dalton_config,
    get_sec_user_agent,
    log_dalton_startup_warnings,
)
from src.tools.web.fetch import get_fetch_chain

REPO_ROOT = Path(__file__).resolve().parents[3]


def _clear_all_caches() -> None:
    clear_config_cache()
    get_dalton_config.cache_clear()


def _set_profile_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_env: str | None,
    dalton_profile: str | None,
) -> None:
    if app_env is None:
        monkeypatch.delenv("APP_ENV", raising=False)
    else:
        monkeypatch.setenv("APP_ENV", app_env)
    if dalton_profile is None:
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
    else:
        monkeypatch.setenv("DALTON_PROFILE", dalton_profile)
    monkeypatch.delenv("DALTON_TIMEZONE", raising=False)
    monkeypatch.delenv("DALTON_PM_USER_IDS", raising=False)
    monkeypatch.delenv("PTC_CONFIG_FILE", raising=False)
    _clear_all_caches()


def _collect_strings(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str):
        found.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            found.extend(_collect_strings(k))
            found.extend(_collect_strings(v))
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            found.extend(_collect_strings(item))
    return found


class TestProfileResolution:
    """Tests for ``resolve_profile()`` and ``resolve_yaml_overlay_env()``."""

    @pytest.mark.parametrize(
        ("app_env", "dalton_profile", "expected_profile", "expected_overlay"),
        [
            ("core", None, DeploymentProfile.CORE, "core"),
            ("core", "", DeploymentProfile.CORE, "core"),
            ("core", "core", DeploymentProfile.CORE, "core"),
            (None, "core", DeploymentProfile.CORE, "core"),
            ("", "core", DeploymentProfile.CORE, "core"),
            ("dalton", None, DeploymentProfile.EVERFLOW, "dalton"),
            ("dalton", "", DeploymentProfile.EVERFLOW, "dalton"),
            ("dalton", "everflow", DeploymentProfile.EVERFLOW, "dalton"),
            (None, None, DeploymentProfile.EVERFLOW, ""),
            ("", "", DeploymentProfile.EVERFLOW, ""),
            ("", "everflow", DeploymentProfile.EVERFLOW, ""),
            ("production", None, DeploymentProfile.EVERFLOW, "production"),
        ],
    )
    def test_valid_entry_combinations(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str | None,
        dalton_profile: str | None,
        expected_profile: DeploymentProfile,
        expected_overlay: str,
    ) -> None:
        _set_profile_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
        assert resolve_profile() is expected_profile
        assert resolve_yaml_overlay_env() == expected_overlay
        assert is_core_profile() is (expected_profile is DeploymentProfile.CORE)

    @pytest.mark.parametrize(
        ("app_env", "dalton_profile", "expected_msg"),
        [
            ("dalton", "core", "APP_ENV='dalton'"),
            ("core", "everflow", "APP_ENV='core'"),
            ("production", "core", "APP_ENV='production'"),
            ("", "unknown-mode", "Invalid DALTON_PROFILE='unknown-mode'"),
        ],
    )
    def test_conflicting_or_invalid_combinations_fail_fast(
        self,
        monkeypatch: pytest.MonkeyPatch,
        app_env: str,
        dalton_profile: str,
        expected_msg: str,
    ) -> None:
        _set_profile_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)

        with pytest.raises(ProfileConfigError, match=expected_msg) as exc_info:
            resolve_profile()
        assert isinstance(exc_info.value, DaltonStartupConfigError)
        assert "DALTON_PROFILE" in str(exc_info.value)

        with pytest.raises(ProfileConfigError):
            load_yaml_config(str(REPO_ROOT / "config.yaml"), use_cache=False)

        with pytest.raises(ProfileConfigError):
            get_dalton_config()

        with pytest.raises(ProfileConfigError):
            _load_default_agent_config()

        with pytest.raises(ProfileConfigError):
            log_dalton_startup_warnings()


class TestCoreAndEverflowOverlayParity:
    """Verify the 4 canonical entry combinations across generic YAML, Dalton settings, and role_policy."""

    def test_both_core_entries_produce_identical_configs_settings_and_roles(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        snapshots: list[dict[str, Any]] = []
        for app_env, dalton_profile in (("core", None), ("", "core")):
            _set_profile_env(monkeypatch, app_env=app_env, dalton_profile=dalton_profile)
            infra = load_infrastructure_config(str(REPO_ROOT / "config.yaml"))
            agent_raw = load_yaml_config(str(REPO_ROOT / "agent_config.yaml"), use_cache=False)
            agent_cfg = load_from_dict(agent_raw)
            dalton_cfg = get_dalton_config()
            role_cfg = _load_default_agent_config()
            # Role policy startup enforcement must pass without raising in core mode.
            enforce_role_policy_on_startup()

            snapshots.append(
                {
                    "infra": infra.model_dump(mode="json"),
                    "agent_raw": agent_raw,
                    "agent_llm": agent_cfg.llm.model_dump(mode="json") if agent_cfg.llm else None,
                    "fetch_chain": get_fetch_chain(),
                    "dalton_cfg": dalton_cfg.model_dump(mode="json"),
                    "role_cfg": role_cfg,
                }
            )

        assert snapshots[0] == snapshots[1]

        core = snapshots[0]
        # Market & news chains: key-free yfinance first, FMP fallback, no ginlix-data / tushare.
        market_names = [p["name"] for p in core["infra"]["market_data"]["providers"]]
        news_names = [p["name"] for p in core["infra"]["news_data"]["providers"]]
        assert market_names == ["yfinance", "fmp"]
        assert news_names == ["yfinance", "fmp"]

        # Web fetch defaults to key-free inhouse only.
        assert core["fetch_chain"] == ["inhouse"]
        assert core["agent_raw"]["x_search"]["enabled"] is False

        # Zero private *-oauth model names anywhere in agent_config or dalton_cfg.
        oauth_in_agent = [s for s in _collect_strings(core["agent_raw"]) if "-oauth" in s]
        oauth_in_dalton = [s for s in _collect_strings(core["dalton_cfg"]) if "-oauth" in s]
        assert oauth_in_agent == []
        assert oauth_in_dalton == []

        # Dalton standalone-core defaults.
        dc = core["dalton_cfg"]
        assert dc["enabled"] is True
        assert dc["timezone"] == "UTC"
        assert dc["budget"]["timezone"] == "UTC"
        assert dc["autonomy"]["timezone"] == "UTC"
        assert dc["data_jobs"]["enabled"] is False
        assert dc["conversations"]["enabled"] is False
        assert dc["network"]["fake_ip_cidrs"] == []
        assert dc["artifact_publishing"]["enabled"] is True
        assert dc["models"]["assist_model"] == ""
        assert dc["publish"]["reviewer_models"] == []
        assert dc["publish"]["audit_model"] == ""
        assert dc["internal_llm"]["allowed_models"] == []

    def test_core_profile_respects_dalton_timezone_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _set_profile_env(monkeypatch, app_env="", dalton_profile="core")
        monkeypatch.setenv("DALTON_TIMEZONE", "America/New_York")
        cfg = get_dalton_config()
        assert cfg.timezone == "America/New_York"
        assert cfg.budget.timezone == "America/New_York"
        assert cfg.autonomy.timezone == "America/New_York"

    def test_both_everflow_entries_match_existing_behavior(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 1. APP_ENV=dalton (production Everflow entry)
        _set_profile_env(monkeypatch, app_env="dalton", dalton_profile=None)
        infra_dalton = load_infrastructure_config(str(REPO_ROOT / "config.yaml"))
        agent_dalton = load_yaml_config(str(REPO_ROOT / "agent_config.yaml"), use_cache=False)
        dalton_cfg_dalton = get_dalton_config()
        role_cfg_dalton = _load_default_agent_config()

        assert [p.name for p in infra_dalton.market_data.providers] == [
            "ginlix-data",
            "tushare",
            "yfinance",
            "yfinance",
            "fmp",
        ]
        assert agent_dalton["search_api"] == "gemini_grounding"
        assert agent_dalton["llm"]["name"] == "claude-opus-5-5-oauth"
        assert dalton_cfg_dalton.data_jobs.enabled is True
        assert dalton_cfg_dalton.conversations.enabled is True
        assert dalton_cfg_dalton.artifact_publishing.enabled is False
        assert dalton_cfg_dalton.network.fake_ip_cidrs == ["198.18.0.0/15"]
        assert dalton_cfg_dalton.timezone == "Asia/Shanghai"
        assert role_cfg_dalton["llm"]["name"] == "claude-opus-5-5-oauth"

        # 2. APP_ENV="" (unit test / local dev Everflow entry):
        # generic YAML stays on upstream base files, dalton_cfg stays on dalton.yaml,
        # and role_policy still overlays agent_config.dalton.yaml fallback.
        _set_profile_env(monkeypatch, app_env="", dalton_profile=None)
        infra_empty = load_infrastructure_config(str(REPO_ROOT / "config.yaml"))
        agent_empty = load_yaml_config(str(REPO_ROOT / "agent_config.yaml"), use_cache=False)
        dalton_cfg_empty = get_dalton_config()
        role_cfg_empty = _load_default_agent_config()

        assert [p.name for p in infra_empty.market_data.providers] == [
            "ginlix-data",
            "tushare",
            "yfinance",
            "fmp",
            "yfinance",
        ]
        assert agent_empty["search_api"] == "tavily"
        assert agent_empty["llm"]["name"] == "claude-opus-5-5"
        assert dalton_cfg_empty.model_dump(mode="json") == dalton_cfg_dalton.model_dump(
            mode="json"
        )
        assert role_cfg_empty["llm"]["name"] == "claude-opus-5-5-oauth"
        assert role_cfg_empty["subagents"]["definitions"]["research"]["model"] == "grok-4.7-oauth"


class TestSecUserAgentSettings:
    """Verify neutral SEC User-Agent fallback and startup warning behavior."""

    def test_sec_user_agent_fallback_is_neutral_and_respects_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        monkeypatch.delenv("EDGAR_IDENTITY", raising=False)

        fallback = get_sec_user_agent()
        assert fallback == DEFAULT_SEC_USER_AGENT
        assert "everflowinv" not in fallback.lower()
        assert "@" in fallback and " " in fallback

        monkeypatch.setenv("EDGAR_IDENTITY", "AcmeFund compliance@acme.test")
        assert get_sec_user_agent() == "AcmeFund compliance@acme.test"

        monkeypatch.setenv("SEC_USER_AGENT", "AcmeResearch sec@acme.test")
        assert get_sec_user_agent() == "AcmeResearch sec@acme.test"

    def test_startup_warns_when_sec_identity_unset_and_stays_quiet_when_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _set_profile_env(monkeypatch, app_env="core", dalton_profile=None)
        monkeypatch.setenv("DALTON_BUDGET_GATE", "1")
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        monkeypatch.delenv("EDGAR_IDENTITY", raising=False)

        with caplog.at_level(logging.WARNING, logger="src.dalton.settings"):
            log_dalton_startup_warnings()

        sec_warnings = [
            rec.getMessage()
            for rec in caplog.records
            if "SEC_USER_AGENT" in rec.getMessage() and "EDGAR_IDENTITY" in rec.getMessage()
        ]
        assert len(sec_warnings) == 1
        assert DEFAULT_SEC_USER_AGENT in sec_warnings[0]
        # Core mode must not emit the Everflow-only G7.1 APP_ENV != 'dalton' warning.
        assert not any("G7.1" in rec.getMessage() for rec in caplog.records)

        caplog.clear()
        monkeypatch.setenv("SEC_USER_AGENT", "AcmeResearch sec@acme.test")
        with caplog.at_level(logging.WARNING, logger="src.dalton.settings"):
            log_dalton_startup_warnings()

        assert not any("SEC_USER_AGENT" in rec.getMessage() for rec in caplog.records)
