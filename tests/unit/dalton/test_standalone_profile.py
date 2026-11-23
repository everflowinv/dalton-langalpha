"""Unit tests for unified deployment profile resolution, Core YAML overlays, lazy imports, and SEC identity."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
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
from src.dalton.sec_identity import (
    DEFAULT_SEC_USER_AGENT,
    ensure_edgar_identity_env,
    resolve_sec_user_agent,
)
from src.dalton.settings import (
    get_dalton_config,
    get_fake_ip_real_resolvers,
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
    monkeypatch.delenv("DALTON_ENABLED", raising=False)
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

    def test_startup_warnings_skip_profile_check_when_dalton_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """grok 低 6: explicitly disabling Dalton must not fail startup on DALTON_PROFILE."""
        _set_profile_env(monkeypatch, app_env="dalton", dalton_profile="core")
        monkeypatch.setenv("DALTON_ENABLED", "0")
        # Must return quietly without invoking resolve_profile()
        log_dalton_startup_warnings()


class TestLazyImportNoSideEffects:
    """codex 中 1 / grok 低 3: load_yaml_config() must not import src.dalton.settings or src.config."""

    def test_load_yaml_config_does_not_import_dalton_settings_or_config(self) -> None:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / 'src'}",
            "APP_ENV": "core",
            # Invalid server setting that would crash src.config.settings if imported
            "SEARCH_PROVIDER_MIN_TIER": "invalid",
        }
        code = (
            "import json, sys\n"
            "from ptc_agent.config.file_utils import load_yaml_config\n"
            "cfg = load_yaml_config('config.yaml', use_cache=False)\n"
            "print(json.dumps({\n"
            "  'providers': [p['name'] for p in cfg['market_data']['providers']],\n"
            "  'settings_loaded': 'src.dalton.settings' in sys.modules,\n"
            "  'config_settings_loaded': 'src.config.settings' in sys.modules,\n"
            "}))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        payload = json.loads(proc.stdout)
        assert payload["providers"] == ["yfinance", "fmp"]
        assert payload["settings_loaded"] is False
        assert payload["config_settings_loaded"] is False

    def test_src_dalton_package_lazy_exports_still_work(self) -> None:
        import src.dalton as dalton_pkg

        assert dalton_pkg.get_dalton_config is get_dalton_config
        assert dalton_pkg.get_sec_user_agent is get_sec_user_agent
        with pytest.raises(AttributeError):
            _ = dalton_pkg.does_not_exist  # type: ignore[attr-defined]


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
            enforce_role_policy_on_startup()

            snapshots.append(
                {
                    "infra": infra.model_dump(mode="json"),
                    "agent_raw": agent_raw,
                    "agent_llm": agent_cfg.llm.model_dump(mode="json") if agent_cfg.llm else None,
                    "fetch_chain": get_fetch_chain(),
                    "dalton_cfg": dalton_cfg.model_dump(mode="json"),
                    "fake_ip_resolvers": get_fake_ip_real_resolvers(),
                    "role_cfg": role_cfg,
                }
            )

        assert snapshots[0] == snapshots[1]

        core = snapshots[0]
        market_names = [p["name"] for p in core["infra"]["market_data"]["providers"]]
        news_names = [p["name"] for p in core["infra"]["news_data"]["providers"]]
        assert market_names == ["yfinance", "fmp"]
        assert news_names == ["yfinance", "fmp"]

        assert core["fetch_chain"] == ["inhouse"]
        assert core["agent_raw"]["x_search"]["enabled"] is False

        oauth_in_agent = [s for s in _collect_strings(core["agent_raw"]) if "-oauth" in s]
        oauth_in_dalton = [s for s in _collect_strings(core["dalton_cfg"]) if "-oauth" in s]
        assert oauth_in_agent == []
        assert oauth_in_dalton == []

        dc = core["dalton_cfg"]
        assert dc["enabled"] is True
        assert dc["timezone"] == "UTC"
        assert dc["budget"]["timezone"] == "UTC"
        assert dc["autonomy"]["timezone"] == "UTC"
        assert dc["data_jobs"]["enabled"] is False
        assert dc["conversations"]["enabled"] is False
        assert dc["network"]["fake_ip_cidrs"] == []
        assert dc["network"]["fake_ip_real_resolver"] == []
        assert core["fake_ip_resolvers"] == []
        assert dc["artifact_publishing"]["enabled"] is True
        assert dc["models"]["assist_model"] == ""
        assert dc["publish"]["reviewer_models"] == []
        assert dc["publish"]["audit_model"] == ""
        assert dc["internal_llm"]["allowed_models"] == []

    def test_core_overlay_genuinely_clears_base_subagents_definitions(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """grok 低 1: subagents.definitions: {} in core overlay clears non-empty base definitions."""
        base_file = tmp_path / "agent_config.yaml"
        core_file = tmp_path / "agent_config.core.yaml"
        base_file.write_text(
            "llm:\n  name: claude-opus-5-5\n"
            "subagents:\n  enabled: [research]\n"
            "  definitions:\n    research:\n      model: some-upstream-model\n",
            encoding="utf-8",
        )
        core_file.write_text(
            "subagents:\n  definitions: {}\n",
            encoding="utf-8",
        )
        _set_profile_env(monkeypatch, app_env="core", dalton_profile=None)
        merged = load_yaml_config(str(base_file), use_cache=False)
        assert merged["subagents"]["definitions"] == {}
        assert merged["subagents"]["enabled"] == ["research"]

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
    """Verify unified SEC User-Agent resolution across settings.py and src/tools/sec/edgar.py."""

    def test_sec_user_agent_fallback_is_neutral_and_respects_env(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        monkeypatch.delenv("EDGAR_IDENTITY", raising=False)

        fallback, configured = resolve_sec_user_agent()
        assert configured is False
        assert fallback == DEFAULT_SEC_USER_AGENT
        assert get_sec_user_agent() == DEFAULT_SEC_USER_AGENT
        assert "everflowinv" not in fallback.lower()
        assert "@" in fallback and " " in fallback

        monkeypatch.setenv("EDGAR_IDENTITY", "AcmeFund compliance@acme.test")
        assert resolve_sec_user_agent() == ("AcmeFund compliance@acme.test", True)
        assert get_sec_user_agent() == "AcmeFund compliance@acme.test"

        monkeypatch.setenv("SEC_USER_AGENT", "AcmeResearch sec@acme.test")
        assert resolve_sec_user_agent() == ("AcmeResearch sec@acme.test", True)
        assert get_sec_user_agent() == "AcmeResearch sec@acme.test"

    def test_edgar_module_uses_sec_user_agent_when_only_sec_user_agent_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """codex 低 1: setting only SEC_USER_AGENT populates EDGAR_IDENTITY with that value."""
        monkeypatch.setenv("SEC_USER_AGENT", "AcmeResearch sec@acme.test")
        monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
        log = logging.getLogger("test.sec.edgar")
        with caplog.at_level(logging.WARNING, logger="test.sec.edgar"):
            identity = ensure_edgar_identity_env(log)
        assert identity == "AcmeResearch sec@acme.test"
        assert os.environ["EDGAR_IDENTITY"] == "AcmeResearch sec@acme.test"
        assert caplog.records == []

        # Also verify actual import of src.tools.sec.edgar in a fresh subprocess
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": f"{REPO_ROOT}{os.pathsep}{REPO_ROOT / 'src'}",
            "SEC_USER_AGENT": "SubprocessOrg edgar@sub.test",
        }
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os, src.tools.sec.edgar; print(os.environ['EDGAR_IDENTITY'])",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"stderr: {proc.stderr}"
        assert proc.stdout.strip().splitlines()[-1] == "SubprocessOrg edgar@sub.test"

    def test_edgar_module_warns_and_uses_neutral_placeholder_when_both_unset(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.delenv("SEC_USER_AGENT", raising=False)
        monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
        log = logging.getLogger("test.sec.edgar.unset")
        with caplog.at_level(logging.WARNING, logger="test.sec.edgar.unset"):
            identity = ensure_edgar_identity_env(log)
        assert identity == DEFAULT_SEC_USER_AGENT
        assert os.environ["EDGAR_IDENTITY"] == DEFAULT_SEC_USER_AGENT
        assert len(caplog.records) == 1
        assert "SEC_USER_AGENT" in caplog.records[0].getMessage()
        assert "EDGAR_IDENTITY" in caplog.records[0].getMessage()

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
        assert not any("G7.1" in rec.getMessage() for rec in caplog.records)

        caplog.clear()
        monkeypatch.setenv("SEC_USER_AGENT", "AcmeResearch sec@acme.test")
        with caplog.at_level(logging.WARNING, logger="src.dalton.settings"):
            log_dalton_startup_warnings()

        assert not any("SEC_USER_AGENT" in rec.getMessage() for rec in caplog.records)
