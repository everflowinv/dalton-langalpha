"""External members never see `conversation_search` mounted (tier filter at build time)."""

from __future__ import annotations

import pytest

from src.dalton.tools import build_dalton_tools

_ROLES = ("analyst", "chief_of_staff")


def _tool_names(role: str, user_tier: str | None) -> set[str]:
    return {t.name for t in build_dalton_tools(role, None, user_tier=user_tier)}


@pytest.mark.parametrize("role", _ROLES)
def test_external_member_gets_no_conversation_search(role: str):
    assert "conversation_search" not in _tool_names(role, "external")


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("tier", ["internal", "admin"])
def test_internal_and_admin_keep_conversation_search(role: str, tier: str):
    assert "conversation_search" in _tool_names(role, tier)


@pytest.mark.parametrize("role", _ROLES)
def test_unresolved_tier_mounts_with_runtime_refusal_intact(role: str):
    """None tier (no roster read on this build) keeps today's behavior:
    mounted, with the per-call refusal as the second line of defense."""
    assert "conversation_search" in _tool_names(role, None)
