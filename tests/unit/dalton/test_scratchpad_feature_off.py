"""SYNC3 B7 (#658): upstream's scratchpad merges but stays off.

The kill switch lives in config.dalton.yaml (APP_ENV=dalton overlay over
config.yaml), inside the features: section the other Dalton flags share.
Upstream ships the scratchpad enabled with an OPT_OUT gate, and a disabled
feature never reaches the per-user resolution — effective_flags answers
False before it reads the user's override — so the Settings toggle cannot
turn it back on (list_user_features hides the entry entirely). These tests
load the real overlay through the same cached loader the runtime uses;
APP_ENV is set and the caches cleared inside the test, so nothing depends
on the outside environment.
"""

from __future__ import annotations

import pytest

from src.config.core import clear_config_cache
from src.config.features import default_feature_enabled, effective_flags


@pytest.fixture()
def dalton_overlay(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dalton")
    clear_config_cache()
    try:
        yield
    finally:
        # Leave the caches empty so no later test in this process resolves
        # against the dalton overlay.
        clear_config_cache()


def test_dalton_overlay_disables_scratchpad(dalton_overlay):
    assert effective_flags(None)["scratchpad"] is False
    # AgentConfig.feature_enabled falls back to this when a turn carries no
    # per-user feature map, so the agent library's gate agrees.
    assert default_feature_enabled("scratchpad") is False


def test_the_kill_switch_beats_the_users_opt_out_toggle(dalton_overlay):
    # Upstream's gate is OPT_OUT (on until the user opts out); the overlay's
    # enabled: false is consulted first, so even a stored True preference
    # from before the switch resolves off.
    assert effective_flags({"scratchpad": True})["scratchpad"] is False


def test_without_the_overlay_the_feature_sits_at_its_upstream_default(monkeypatch):
    # config.yaml carries no features.scratchpad key, so the overlay line is
    # the only thing holding the feature off: this pins the tests above
    # against a future upstream default change making them vacuous.
    monkeypatch.delenv("APP_ENV", raising=False)
    clear_config_cache()
    try:
        assert effective_flags(None)["scratchpad"] is True
    finally:
        clear_config_cache()
