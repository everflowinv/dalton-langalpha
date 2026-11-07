"""SYNC3 B3 (#647): model-written MCP server descriptions merge but stay off.

The kill switch lives in config.dalton.yaml (APP_ENV=dalton overlay over
config.yaml). These tests load that real overlay through the same cached
loader the runtime uses — APP_ENV is set and the caches cleared inside the
test, so nothing depends on the outside environment — and read the config
through the accessor every gate checks: McpDescriptionWorker.start
(worker.py), jobs.enqueue_if_new, and prompt_text.profiles_by_server.
"""

from __future__ import annotations

import pytest

from src.config.core import clear_config_cache
from src.config.settings import get_mcp_descriptions_config


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


def test_dalton_overlay_disables_mcp_descriptions(dalton_overlay):
    assert get_mcp_descriptions_config().enabled is False


def test_without_the_overlay_the_service_sits_at_its_upstream_default(monkeypatch):
    # config.yaml carries no mcp_descriptions key, so the overlay line is the
    # only thing holding the service off: this pins the test above against a
    # future upstream flip of the code default making it vacuous.
    monkeypatch.delenv("APP_ENV", raising=False)
    clear_config_cache()
    try:
        assert get_mcp_descriptions_config().enabled is True
    finally:
        clear_config_cache()
