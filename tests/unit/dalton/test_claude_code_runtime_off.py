"""SYNC2 decision 13 (#643): the Claude Code runtime merges but stays off.

The kill switch lives in config.dalton.yaml (APP_ENV=dalton overlay over
config.yaml) beside market_watch and wakeups. These tests load that real
overlay through the same loader the runtime uses, so the lock holds the file,
not a stub: with it applied the flag is dead for every user, and it stays dead
even if the code catalog's default later flips to enabled.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import src.config.features as features_module
from src.config.models import InfrastructureConfig
from src.ptc_agent.config.file_utils import load_yaml_config

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def dalton_infra_config(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dalton")
    merged = load_yaml_config(str(REPO_ROOT / "config.yaml"), use_cache=False)
    return InfrastructureConfig(**merged)


def test_dalton_overlay_disables_claude_code_runtime(monkeypatch, dalton_infra_config):
    assert dalton_infra_config.features["claude_code_runtime"].enabled is False
    monkeypatch.setattr(features_module, "get_infrastructure_config", lambda: dalton_infra_config)
    assert features_module.system_flag("claude_code_runtime").enabled is False
    # A user's saved opt-in cannot turn on what the deployment killed.
    assert features_module.effective_flags({"claude_code_runtime": True})["claude_code_runtime"] is False


def test_dalton_overlay_holds_the_flag_off_even_if_the_catalog_default_flips(
    monkeypatch, dalton_infra_config
):
    monkeypatch.setattr(features_module, "get_infrastructure_config", lambda: dalton_infra_config)
    flipped = replace(features_module.FEATURES["claude_code_runtime"], enabled=True)
    monkeypatch.setitem(features_module.FEATURES, "claude_code_runtime", flipped)
    assert features_module.system_flag("claude_code_runtime").enabled is False
    assert features_module.effective_flags({"claude_code_runtime": True})["claude_code_runtime"] is False
