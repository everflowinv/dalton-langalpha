"""Fixtures shared by LLM service tests.

The model-resolution tests under this directory were written against an empty
bundled-agent roster, which is what upstream ships: no ``plugins/*/agents/``
bundle of its own. Dalton's evidence and reviewer subagents
(``plugins/dalton/agents/``) make ``bundled_agent_plans()`` non-empty, which
sends ``plugin_bases`` at ``list_account_disables`` — a database query these
tests never mock. Pinning ``bundled_agent_plans()`` empty here restores the
condition the upstream tests were written for while letting tests that patch
``bundled_agent_plans`` themselves override it.
"""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest

from src.server.services.plugins.bundled import bundled_agent_plans


@pytest.fixture(autouse=True)
def _no_bundled_agents(monkeypatch):
    monkeypatch.setattr(
        "src.server.services.plugins.bundled.bundled_agent_plans",
        create_autospec(bundled_agent_plans, return_value=()),
    )
