"""External members never see `conversation_search` mounted (tier filter at build time)."""

from __future__ import annotations

import pytest

from src.dalton.tools import build_dalton_tools

_ROLES = ("analyst", "chief_of_staff")


def _tool_names(
    role: str, user_tier: str | None, *, team_memory_enabled: bool = True
) -> set[str]:
    return {
        t.name
        for t in build_dalton_tools(
            role,
            None,
            user_tier=user_tier,
            team_memory_enabled=team_memory_enabled,
        )
    }


@pytest.mark.parametrize("role", _ROLES)
def test_personal_toggle_off_still_mounts_conversation_search(role: str):
    assert "conversation_search" in {
        t.name
        for t in build_dalton_tools(
            role, None, user_tier="internal", team_memory_enabled=False
        )
    }


@pytest.mark.parametrize("role", _ROLES)
def test_deployment_conversations_disabled_omits_conversation_search(
    role: str, monkeypatch: pytest.MonkeyPatch
):
    from src.dalton import settings as dalton_settings_mod
    from src.dalton import tools as dalton_tools_mod

    cfg = dalton_settings_mod.get_dalton_settings().model_copy(
        update={
            "conversations": dalton_settings_mod.get_dalton_settings().conversations.model_copy(
                update={"enabled": False}
            )
        }
    )
    monkeypatch.setattr(dalton_tools_mod, "get_dalton_settings", lambda: cfg)
    assert "conversation_search" not in {
        t.name for t in build_dalton_tools(role, None, user_tier="internal")
    }


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


@pytest.mark.parametrize("role", _ROLES)
def test_external_member_gets_no_dalton_notify_settings(role: str):
    assert "dalton_notify_settings" not in _tool_names(role, "external")


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("tier", ["internal", "admin", None])
def test_internal_and_admin_mount_dalton_notify_settings(
    role: str, tier: str | None
):
    assert "dalton_notify_settings" in _tool_names(
        role, tier, team_memory_enabled=False
    )
