"""DALTON-HOOK(browser-kill-switch): with ``features.browser`` off, the server
converges no computer onto the toolset table (every unit is the agent's
browser), on the acquire path and in the sweep."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.server.services import toolset


@pytest.fixture
def browser_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(toolset, "_browser_enabled", create_autospec(toolset._browser_enabled, return_value=False))
    toolset.kind_served.cache_clear()
    yield
    toolset.kind_served.cache_clear()


def test_no_kind_is_served_with_the_browser_off(browser_off: None) -> None:
    assert toolset.kind_served("docker") is False


def test_an_acquire_starts_no_converge_with_the_browser_off(browser_off: None) -> None:
    sandbox = SimpleNamespace(serves_toolset=True, sandbox_id="sb-1")
    assert toolset.converge_soon(sandbox, "computer-1", None) is False
    assert not hasattr(sandbox, "_toolset_track")
