"""Bundled vs user-installed plugin subagent assembly isolation (Standalone Core Batch 3)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from src.server.database import account_disables as db_account_disables
from src.server.database import plugin_subagents as db_plugin_subagents
from src.server.services.plugins import bundled as bundled_plugins
from src.server.services.subagents.plugin_bases import plugin_bases
from src.server.services.subagents.view import value_views

pytestmark = pytest.mark.asyncio


async def test_user_installed_plugin_with_dalton_cap_literal_stays_untouched_in_subagent_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-installed plugin subagents bypass Dalton content assembly and researchdata pruning."""
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    monkeypatch.delenv("DALTON_CAPABILITIES", raising=False)

    user_description = "Custom plugin docs mentioning DALTON_CAP_NATIVE_OAUTH_SYNC verbatim."
    user_instructions = (
        "Inspect DALTON_CAP_NATIVE_OAUTH_SYNC and keep <!-- DALTON_CAP:not-a-tag unclosed text literal."
    )
    installed_row = {
        "plugin_name": "acme-user-plugin",
        "plugin_enabled": True,
        "name": "acme-helper",
        "fields": {
            "description": user_description,
            "tools": ["web", "code"],
            "preferred_connectors": ["researchdata", "acme-mcp"],
        },
        "instructions": user_instructions,
        "roles": ["analyst"],
    }

    bundled_plan = SimpleNamespace(
        name="bundled-evidence",
        row={
            "name": "bundled-evidence",
            "fields": {
                "description": (
                    "Base<!-- DALTON_CAP:researchdata_bridge --> proprietary<!-- /DALTON_CAP -->"
                ),
                "tools": ["web", "code"],
                "preferred_connectors": ["researchdata", "sec"],
            },
            "instructions": (
                "Keep me.<!-- DALTON_CAP:researchdata_bridge --> Drop me in core.<!-- /DALTON_CAP -->"
            ),
            "roles": ["analyst"],
        },
    )

    monkeypatch.setattr(
        bundled_plugins,
        "bundled_agent_plans",
        create_autospec(
            bundled_plugins.bundled_agent_plans,
            return_value=[("dalton", bundled_plan)],
        ),
    )
    monkeypatch.setattr(
        db_account_disables,
        "list_account_disables",
        create_autospec(
            db_account_disables.list_account_disables,
            return_value=SimpleNamespace(bundles=frozenset()),
        ),
    )
    monkeypatch.setattr(
        db_plugin_subagents,
        "list_user_plugin_subagents",
        create_autospec(
            db_plugin_subagents.list_user_plugin_subagents,
            return_value=[installed_row],
        ),
    )

    bases_list = await plugin_bases("user-1")
    bases_map = {base.definition.name: base for base, _on in bases_list}
    offered = tuple(base.definition.name for base, on in bases_list if on)

    # Settings-page subagent list builds cleanly without ContentAssemblyError
    views = value_views(bases_map, offered, [])
    by_name = {v["name"]: v for v in views}

    # User-installed plugin subagent text & preferred_connectors are completely unmodified
    assert "acme-helper" in by_name
    assert by_name["acme-helper"]["description"] == user_description
    assert by_name["acme-helper"]["instructions"] == user_instructions
    assert bases_map["acme-helper"].definition.preferred_connectors == ["researchdata", "acme-mcp"]

    # Compiling the user-installed plugin subagent also leaves its text untouched
    from ptc_agent.agent.subagents.compiler import SubagentCompiler

    compiled_user = SubagentCompiler().compile(bases_map["acme-helper"].definition)
    assert compiled_user["description"] == user_description
    assert user_instructions in compiled_user["system_prompt"]

    # Bundled plugin subagent still runs Dalton assembly and strips researchdata in Core
    assert "bundled-evidence" in by_name
    assert by_name["bundled-evidence"]["description"] == "Base"
    assert by_name["bundled-evidence"]["instructions"] == "Keep me."
    assert bases_map["bundled-evidence"].definition.preferred_connectors == ["sec"]
